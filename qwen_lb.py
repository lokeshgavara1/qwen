"""
QWEN AI GATEWAY - Ultra-Fast & Resilient Load Balancer v3.0
Features:
- Dynamic cluster load balancing with automatic offline server failover
- Localhost Ollama priority & fallback
- Fixed Ollama options dictionary formatting (num_predict, temperature, top_p)
- Instant non-blocking startup (<1s) with concurrent health checking
- Chrome Private Network Access (PNA) & CORS preflight support
- Integrated Frontend serving on http://localhost:8000
- Thread-safe API key tracking with expiration & active status validation
- Dual mode HTTP / HTTPS support (use --ssl for HTTPS)
- Phase 1: SQLite-backed rate limiting & per-key quota management
"""

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import secrets
import sys
import time
from typing import Dict, List, Optional, Set, Tuple

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
)
from fastapi.staticfiles import StaticFiles
import httpx

import analytics
import rate_limiter
import security

###############################################################################
# CONFIGURATION & SERVERS
###############################################################################

SERVERS = {
    "local": {
        "url": "http://127.0.0.1:11434",
        "models": ["mistral:7b", "qwen2.5vl:7b"],
        "priority": 1,
    },
    "server1": {
        "url": "http://172.16.8.253:11434",
        "models": ["mistral:7b", "qwen2.5vl:7b", "qwen2.5-coder:32b", "qwen3:32b"],
        "priority": 2,
    },
    "coding": {
        "url": "http://172.16.11.231:11434",
        "models": ["mistral:7b", "qwen2.5-coder:32b", "qwen3:32b"],
        "priority": 3,
    },
    "reasoning": {
        "url": "http://172.16.8.4:11434",
        "models": ["mistral:7b", "qwen2.5vl:7b"],
        "priority": 4,
    },
    "server2": {
        "url": "http://172.16.8.252:11434",
        "models": ["mistral:7b", "qwen2.5vl:7b"],
        "priority": 5,
    },
    "server3": {
        "url": "http://172.16.8.251:11434",
        "models": ["mistral:7b"],
        "priority": 6,
    },
    "server4": {
        "url": "http://172.16.8.112:11434",
        "models": ["mistral:7b"],
        "priority": 7,
    },
    "server5": {
        "url": "http://172.16.8.157:11434",
        "models": ["mistral:7b"],
        "priority": 8,
    },
    "server6": {
        "url": "http://172.16.8.154:11434",
        "models": ["mistral:7b"],
        "priority": 9,
    },
    "vision": {
        "url": "http://172.16.8.249:11434",
        "models": ["mistral:7b", "qwen2.5vl:7b"],
        "priority": 10,
    },
}

MAX_TOKENS_BY_TYPE = {
    "chat": 200,
    "coding": 300,
    "vision": 150,
    "reasoning": 250,
}

MAX_INPUT_TOKENS = 4000

def estimate_tokens(text: str) -> float:
    """Estimates token count as: word_count * 1.3"""
    if not text:
        return 0.0
    words = len(text.split())
    return words * 1.3

###############################################################################
# CONVERSATION MEMORY (STATEFUL CONTEXT LINKING)
###############################################################################

class ConversationMemory:
    """
    In-memory store for tracking conversation history and context linking.
    Stores the previous response to inject into subsequent prompts.
    """
    def __init__(self, max_entries: int = 2000):
        self._memory: Dict[str, Dict[str, Any]] = {}
        self._max_entries = max_entries

    def get_last_response(self, conversation_id: str) -> Optional[str]:
        if not conversation_id or conversation_id not in self._memory:
            return None
        return self._memory[conversation_id].get("last_response")

    def set_last_response(self, conversation_id: str, response_text: str, prompt: Optional[str] = None):
        if not conversation_id or not response_text:
            return
        if len(self._memory) >= self._max_entries and conversation_id not in self._memory:
            oldest = min(self._memory.keys(), key=lambda k: self._memory[k].get("updated_at", 0))
            self._memory.pop(oldest, None)
        self._memory[conversation_id] = {
            "last_response": response_text,
            "last_prompt": prompt,
            "updated_at": time.time(),
        }

    def clear(self, conversation_id: Optional[str] = None):
        if conversation_id:
            self._memory.pop(conversation_id, None)
        else:
            self._memory.clear()

conversation_memory = ConversationMemory()

###############################################################################
# ADMIN CONFIGURATION
###############################################################################

# Name of the API key that has administrator privileges.
# Set this to the "name" field of the key you want to use as admin.
# To grant admin rights: set ADMIN_KEY_NAME to match an existing key's "name" field.
ADMIN_KEY_NAME = "admin"

###############################################################################
# API KEY MANAGEMENT
###############################################################################

API_KEYS_FILE = "api_keys.json"
key_lock = asyncio.Lock()

def load_api_keys():
    if Path(API_KEYS_FILE).exists():
        try:
            with open(API_KEYS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

async def save_api_keys_async(keys):
    async with key_lock:
        with open(API_KEYS_FILE, "w") as f:
            json.dump(keys, f, indent=2)

API_KEYS = load_api_keys()

def find_api_key(potential_key: str) -> Optional[dict]:
    """Constant-time lookup supporting both hashed and plaintext stored keys."""
    if not potential_key:
        return None
    p_hash = security.hash_key(potential_key)
    for k, info in API_KEYS.items():
        if secrets.compare_digest(k, p_hash) or secrets.compare_digest(k, potential_key):
            return info
    return None

###############################################################################
# SERVER STATE & STATS
###############################################################################

SERVER_STATE = {}
for role, cfg in SERVERS.items():
    SERVER_STATE[role] = {
        "role": role,
        "url": cfg["url"],
        "models": list(cfg["models"]),
        "priority": cfg["priority"],
        "online": False,
        "queue": 0,
        "requests": 0,
        "errors": 0,
        "latency": 0.0,
        "last_seen": None,
    }

STATS = {
    "total_requests": 0,
    "chat_requests": 0,
    "coding_requests": 0,
    "vision_requests": 0,
    "reasoning_requests": 0,
    "failed_requests": 0,
}

http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=3.0, read=120.0, write=120.0, pool=120.0)
)

###############################################################################
# INTENTS & DETECTOR
###############################################################################

CHAT = "chat"
CODING = "coding"
VISION = "vision"
REASONING = "reasoning"

class FastIntentDetector:
    CODING_KEYWORDS = {
        "code", "python", "java", "javascript", "c++", "cpp", "c#", "debug",
        "function", "class", "api", "database", "sql", "react", "node",
        "spring", "fastapi", "django", "flask", "git", "docker", "error",
        "exception", "algorithm", "implement", "deploy", "test", "refactor"
    }
    REASONING_KEYWORDS = {
        "explain", "analyze", "research", "mathematics", "physics", "quantum",
        "philosophy", "economics", "medical", "strategy", "theory", "complex"
    }
    VISION_KEYWORDS = {
        "image", "photo", "screenshot", "ocr", "read", "chart", "graph",
        "diagram", "pdf", "document", "visual", "describe", "extract"
    }

    @staticmethod
    def detect(prompt: str, images: Optional[List] = None) -> str:
        if images:
            return VISION
        text = (prompt or "").lower().strip()
        if any(p in text for p in ['def ', 'class ', 'function ', '{', '}', '```']):
            return CODING
        coding_score = sum(1 for kw in FastIntentDetector.CODING_KEYWORDS if kw in text)
        reasoning_score = sum(1 for kw in FastIntentDetector.REASONING_KEYWORDS if kw in text)
        vision_score = sum(1 for kw in FastIntentDetector.VISION_KEYWORDS if kw in text)

        if vision_score > 0:
            return VISION
        if coding_score >= reasoning_score and coding_score > 0:
            return CODING
        if reasoning_score > coding_score and reasoning_score > 0:
            return REASONING
        return CHAT

###############################################################################
# CACHE
###############################################################################

class ResponseCache:
    def __init__(self, ttl_seconds=300):
        self.cache = {}
        self.ttl = ttl_seconds

    def get_key(self, model: str, prompt: str) -> str:
        return f"{model}:{hash(prompt)}"

    def get(self, model: str, prompt: str) -> Optional[Dict]:
        key = self.get_key(model, prompt)
        data = self.cache.get(key)
        if not data:
            return None
        if time.time() - data["time"] > self.ttl:
            del self.cache[key]
            return None
        return data["response"]

    def set(self, model: str, prompt: str, response: Dict):
        key = self.get_key(model, prompt)
        self.cache[key] = {"time": time.time(), "response": response}

    def clear(self):
        self.cache.clear()

cache = ResponseCache(ttl_seconds=300)

def log(msg: str):
    clean_msg = str(msg).replace("\u2713", "[OK]").replace("✓", "[OK]").replace("✗", "[FAIL]")
    try:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {clean_msg}", flush=True)
    except Exception:
        safe_str = clean_msg.encode("ascii", "replace").decode("ascii")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {safe_str}", flush=True)

###############################################################################
# LOAD BALANCER & SCHEDULER
###############################################################################

class SmartScheduler:
    @staticmethod
    def get_available_workers(model: str) -> List[str]:
        return [
            role for role, s in SERVER_STATE.items()
            if s["online"] and model in s["models"]
        ]

    @staticmethod
    def select_worker(model: str) -> Optional[Dict]:
        candidates = SmartScheduler.get_available_workers(model)
        if not candidates:
            # Fallback 1: local worker
            if SERVER_STATE.get("local", {}).get("online"):
                return {"url": SERVER_STATE["local"]["url"], "role": "local"}
            # Fallback 2: any online server
            any_online = [role for role, s in SERVER_STATE.items() if s["online"]]
            if any_online:
                return {"url": SERVER_STATE[any_online[0]]["url"], "role": any_online[0]}
            return None

        # Pick candidate with lowest queue, then lowest latency, then priority
        best_role = min(
            candidates,
            key=lambda r: (
                SERVER_STATE[r]["queue"],
                SERVER_STATE[r]["latency"],
                SERVER_STATE[r]["priority"]
            )
        )
        return {"url": SERVER_STATE[best_role]["url"], "role": best_role}

    @staticmethod
    def start_request(role: str):
        if role in SERVER_STATE:
            SERVER_STATE[role]["queue"] += 1
            SERVER_STATE[role]["requests"] += 1

    @staticmethod
    def finish_request(role: str, latency_ms: float = 0.0):
        if role in SERVER_STATE:
            SERVER_STATE[role]["queue"] = max(0, SERVER_STATE[role]["queue"] - 1)
            if latency_ms > 0:
                # Exponential moving average for latency
                curr = SERVER_STATE[role]["latency"]
                SERVER_STATE[role]["latency"] = round(latency_ms if curr == 0 else (curr * 0.7 + latency_ms * 0.3), 1)

###############################################################################
# HEALTH CHECK & WARMUP
###############################################################################

async def ping_server(role: str, state: Dict):
    t0 = time.perf_counter()
    try:
        res = await http_client.get(f"{state['url']}/api/tags", timeout=1.8)
        if res.status_code == 200:
            elapsed = round((time.perf_counter() - t0) * 1000, 1)
            state["online"] = True
            state["latency"] = elapsed
            state["last_seen"] = datetime.now().isoformat()
            try:
                tags = res.json().get("models", [])
                live_models = [m["name"] for m in tags if "name" in m]
                if live_models:
                    state["models"] = list(set(state["models"] + live_models))
            except Exception:
                pass
            return True
    except Exception:
        pass
    state["online"] = False
    return False

async def check_server_health_all():
    tasks = [ping_server(role, state) for role, state in SERVER_STATE.items()]
    await asyncio.gather(*tasks, return_exceptions=True)

async def periodic_health_check():
    while True:
        await check_server_health_all()
        await asyncio.sleep(15)

async def warmup_models():
    await asyncio.sleep(1)
    for role, state in SERVER_STATE.items():
        if not state["online"]:
            continue
        for model in state["models"][:1]:
            try:
                await http_client.post(
                    f"{state['url']}/api/generate",
                    json={"model": model, "prompt": "hi", "stream": False},
                    timeout=5.0
                )
                log(f"✓ Warmed {model} on {role}")
            except Exception as e:
                pass

###############################################################################
# FASTAPI LIFESPAN & APP
###############################################################################

@asynccontextmanager
async def lifespan(app: FastAPI):
    log("=" * 70)
    log("AI GATEWAY v3.0 STARTING")
    log("=" * 70)

    # Phase 1 & Phase 8: initialise rate-limit and security audit databases
    await rate_limiter.init_db()
    log("Rate-limit DB initialised [OK]")
    await security.init_audit_db()
    log("Security Audit DB initialised [OK]")

    # Quick non-blocking initial ping
    await check_server_health_all()
    online = [r for r, s in SERVER_STATE.items() if s["online"]]
    log(f"Initial online workers ({len(online)}/{len(SERVER_STATE)}): {', '.join(online)}")

    health_task  = asyncio.create_task(periodic_health_check())
    warmup_task  = asyncio.create_task(warmup_models())
    cleanup_task = asyncio.create_task(rate_limiter.periodic_cleanup())

    log("=" * 70)
    log("GATEWAY READY [OK] (Listening on http://0.0.0.0:8000)")
    log("=" * 70)
    yield
    health_task.cancel()
    warmup_task.cancel()
    cleanup_task.cancel()
    await http_client.aclose()
    await rate_limiter.close_pool()

app = FastAPI(title="AI Gateway v3.0", version="3.0.0", lifespan=lifespan)

# Security Middleware: Request-ID, Body Size Limits, Security Headers, CORS, Chrome PNA
@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # 1. Request ID Generation / Validation
    req_id_hdr = request.headers.get("X-Request-ID", "").strip()
    if req_id_hdr and len(req_id_hdr) <= 64 and req_id_hdr.replace("-", "").replace("_", "").isalnum():
        request_id = req_id_hdr
    else:
        request_id = f"req_{secrets.token_hex(12)}"
    request.state.request_id = request_id

    # 2. Body Size Limit Enforcement (10 MB default)
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > security.MAX_REQUEST_BODY_BYTES:
                return JSONResponse(
                    {"error": "Payload Too Large — request body exceeds 10MB limit", "request_id": request_id},
                    status_code=413,
                    headers={"X-Request-ID": request_id}
                )
        except ValueError:
            pass

    # 3. OPTIONS Preflight / Handle
    if request.method == "OPTIONS":
        response = JSONResponse(content={})
    else:
        response = await call_next(request)

    # 4. Security Headers (Defense-in-depth)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )

    # 5. Configurable CORS & PNA Headers
    origin = request.headers.get("Origin")
    if origin and (origin in security.CORS_ALLOWED_ORIGINS or "*" in security.CORS_ALLOWED_ORIGINS or origin.startswith("http://localhost:") or origin.startswith("http://127.0.0.1:")):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    else:
        response.headers["Access-Control-Allow-Origin"] = "*"

    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Expose-Headers"] = "X-Server, X-Model, X-Request-ID, X-Conversation-ID, Content-Disposition"

    if request.headers.get("Access-Control-Request-Private-Network") == "true":
        response.headers["Access-Control-Allow-Private-Network"] = "true"

    return response

###############################################################################
# MOUNT FRONTEND
###############################################################################

frontend_dir = Path("Frontend")
if frontend_dir.exists():
    if (frontend_dir / "css").exists():
        app.mount("/css", StaticFiles(directory=str(frontend_dir / "css")), name="css")
    if (frontend_dir / "js").exists():
        app.mount("/js", StaticFiles(directory=str(frontend_dir / "js")), name="js")
    app.mount("/chat", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
    log("Mounted static Frontend at / and /chat")

@app.get("/")
async def root():
    if (frontend_dir / "index.html").exists():
        return FileResponse(str(frontend_dir / "index.html"))
    return RedirectResponse("/docs")

###############################################################################
# CLIENT IP HELPER
###############################################################################

def get_client_ip(request: Request) -> str:
    """
    Determines the real client IP.
    Trusts X-Forwarded-For only for requests arriving from localhost
    (i.e. behind a local NGINX/reverse-proxy on the same machine).
    Otherwise uses the direct connection IP to prevent spoofing.
    """
    peer = request.client.host if request.client else "unknown"
    # Only trust the proxy header when the direct connection is from loopback
    # (i.e. an NGINX/proxy running on the same host)
    if peer in ("127.0.0.1", "::1", "localhost"):
        forwarded_for = request.headers.get("X-Forwarded-For", "")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
    return peer

###############################################################################
# HELPER FUNCTIONS
###############################################################################

def update_stats(intent: str):
    STATS["total_requests"] += 1
    if intent == CHAT:
        STATS["chat_requests"] += 1
    elif intent == CODING:
        STATS["coding_requests"] += 1
    elif intent == VISION:
        STATS["vision_requests"] += 1
    elif intent == REASONING:
        STATS["reasoning_requests"] += 1

def select_model(intent: str) -> str:
    if intent == VISION:
        return "qwen2.5vl:7b"
    return "mistral:7b"

###############################################################################
# MAIN ENDPOINT: /api/generate
###############################################################################

@app.post("/api/generate")
async def generate(request: Request):
    api_key      = None
    api_key_name = None
    auth_header  = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        potential_key = auth_header.replace("Bearer ", "").strip()
        info = API_KEYS.get(potential_key)
        if info and info.get("active", True):
            expires = info.get("expires_at")
            if not expires or datetime.fromisoformat(expires) > datetime.now():
                api_key      = potential_key
                api_key_name = info.get("name", potential_key[:8])
                info["usage_count"] = info.get("usage_count", 0) + 1
                info["last_used"] = datetime.now().isoformat()
                asyncio.create_task(save_api_keys_async(API_KEYS))

    # ── Phase 1: Rate limiting ──────────────────────────────────────────────
    client_ip = get_client_ip(request)
    identity  = rate_limiter.make_identity(api_key_name, client_ip)

    rl = await rate_limiter.check_rate_limit(identity)
    if not rl["allowed"]:
        limit_type = rl["limit_type"]
        # Record the rejected attempt with specific limit_type in intent (does NOT count against quota)
        asyncio.create_task(rate_limiter.record_request(
            identity, model=None, status="rate_limited", intent=limit_type
        ))
        if limit_type == "burst":
            msg      = "Too many requests. Slow down and try again shortly."
            err_code = "rate_limit_exceeded"
        else:
            msg      = "Hourly request quota exceeded. Please try again later."
            err_code = "quota_exceeded"
        log(f"[RATE LIMIT] {identity} blocked ({limit_type}), retry_after={rl['retry_after']}s")
        STATS["failed_requests"] += 1
        return JSONResponse(
            {
                "error": err_code,
                "message": msg,
                "retry_after": rl["retry_after"],
            },
            status_code=429,
            headers={"Retry-After": str(rl["retry_after"])},
        )
    # ── End rate limit check ─────────────────────────────────────────────────

    try:
        body = await request.json()
        prompt = body.get("prompt", "")
        raw_images = body.get("images", [])
        stream = body.get("stream", True)
        
        # ── Conversation ID extraction / generation ─────────────────────────
        conversation_id = body.get("conversation_id") or request.headers.get("X-Conversation-ID")
        if not conversation_id or not str(conversation_id).strip():
            conversation_id = f"conv_{secrets.token_hex(12)}"
        else:
            conversation_id = str(conversation_id).strip()

        # ── Input Token Validation (Word Count * 1.3 <= 4000) ───────────────
        est_tokens = round(estimate_tokens(prompt))
        if est_tokens > MAX_INPUT_TOKENS:
            STATS["failed_requests"] += 1
            log(f"[VALIDATION] Rejected prompt exceeding token limit: {est_tokens} > {MAX_INPUT_TOKENS}")
            return JSONResponse(
                {
                    "error": "Input prompt exceeds 4000 token limit",
                    "message": f"Input prompt length exceeds maximum allowed limit of {MAX_INPUT_TOKENS} tokens (estimated: {est_tokens} tokens). Please shorten your input.",
                    "estimated_tokens": est_tokens,
                    "max_tokens": MAX_INPUT_TOKENS,
                    "conversation_id": conversation_id,
                },
                status_code=400,
                headers={"X-Conversation-ID": conversation_id}
            )

        # Sanitize images: strip data:image/...;base64, prefix if present
        clean_images = []
        for img in raw_images:
            if isinstance(img, str) and "," in img and "base64" in img:
                clean_images.append(img.split(",", 1)[1])
            else:
                clean_images.append(img)
        if clean_images:
            body["images"] = clean_images

        # Intent detection
        intent = FastIntentDetector.detect(prompt, clean_images)
        update_stats(intent)

        # Respect explicit model if provided and valid, otherwise auto-select
        requested_model = body.get("model")
        if requested_model and requested_model in ["mistral:7b", "qwen2.5vl:7b", "qwen2.5-coder:32b", "qwen3:32b"]:
            model = requested_model
        else:
            model = select_model(intent)
        body["model"] = model

        # ── Conversation History & Context Linking ──────────────────────────
        prev_response = conversation_memory.get_last_response(conversation_id)
        if prev_response:
            ollama_prompt = f"Previous response context:\n{prev_response}\n\nUser follow-up question:\n{prompt}"
            log(f"[CONTEXT] Linked context from conversation {conversation_id[:12]} ({len(prev_response)} chars)")
        else:
            ollama_prompt = prompt
        body["prompt"] = ollama_prompt

        # CRITICAL FIX: Pass inference parameters inside Ollama's "options" object
        max_tokens = MAX_TOKENS_BY_TYPE.get(intent, 200)
        if "options" not in body or not isinstance(body["options"], dict):
            body["options"] = {}
        body["options"]["num_predict"] = max_tokens
        body["options"]["temperature"] = 0.3
        body["options"]["top_p"] = 0.8
        body["options"]["repeat_penalty"] = 1.1

        # Check cache (non-streaming only)
        cached = cache.get(model, ollama_prompt)
        if cached and not stream:
            cached.update({
                "_intent": intent,
                "_model": model,
                "_cached": True,
                "_has_api_key": bool(api_key),
                "conversation_id": conversation_id,
            })
            cached_resp = cached.get("response", "")
            if cached_resp:
                conversation_memory.set_last_response(conversation_id, cached_resp, prompt)
            log("Cache HIT")
            return JSONResponse(
                content=cached,
                headers={"X-Server": "cache", "X-Model": model, "X-Conversation-ID": conversation_id}
            )

        # Select healthiest online worker
        worker = SmartScheduler.select_worker(model)
        if not worker:
            STATS["failed_requests"] += 1
            return JSONResponse(
                {"error": f"No online server currently available for model {model}", "conversation_id": conversation_id},
                status_code=503,
                headers={"X-Conversation-ID": conversation_id}
            )

        role = worker["role"]
        url = worker["url"]
        SmartScheduler.start_request(role)
        req_start = time.perf_counter()

        key_status = "WITH KEY" if api_key else "PUBLIC"
        log(f"[{role.upper()}] Request: model={model}, tokens<={max_tokens}, {key_status}, conv={conversation_id}")

        # Streaming response
        if stream:
            async def stream_generator():
                final_tokens = None
                accumulated_text = []
                try:
                    async with http_client.stream(
                        "POST", f"{url}/api/generate", json=body, timeout=120.0
                    ) as response:
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes():
                            if b'"response"' in chunk or b'"eval_count"' in chunk:
                                try:
                                    for line in chunk.split(b"\n"):
                                        if line.strip():
                                            cd = json.loads(line.decode("utf-8", errors="ignore"))
                                            if "response" in cd:
                                                accumulated_text.append(cd["response"])
                                            if "eval_count" in cd:
                                                final_tokens = cd["eval_count"]
                                except Exception:
                                    pass
                            yield chunk
                except Exception as e:
                    SERVER_STATE[role]["errors"] += 1
                    STATS["failed_requests"] += 1
                    elapsed = round((time.perf_counter() - req_start) * 1000, 1)
                    log(f"Stream error on {role}: {e}")
                    asyncio.create_task(rate_limiter.record_request(
                        identity, model=model, status="error", latency_ms=elapsed, intent=intent
                    ))
                else:
                    elapsed = round((time.perf_counter() - req_start) * 1000, 1)
                    full_resp = "".join(accumulated_text)
                    if full_resp:
                        conversation_memory.set_last_response(conversation_id, full_resp, prompt)
                    asyncio.create_task(rate_limiter.record_request(
                        identity, model=model, status="ok", tokens=final_tokens, latency_ms=elapsed, intent=intent
                    ))
                finally:
                    elapsed = round((time.perf_counter() - req_start) * 1000, 1)
                    SmartScheduler.finish_request(role, elapsed)

            return StreamingResponse(
                stream_generator(),
                media_type="application/x-ndjson",
                headers={"X-Server": role, "X-Model": model, "X-Conversation-ID": conversation_id},
            )

        # Non-streaming response
        try:
            response = await http_client.post(f"{url}/api/generate", json=body, timeout=120.0)
            data = response.json()
            elapsed = round((time.perf_counter() - req_start) * 1000, 1)
            SmartScheduler.finish_request(role, elapsed)

            resp_text = data.get("response", "")
            if resp_text:
                conversation_memory.set_last_response(conversation_id, resp_text, prompt)

            tokens = data.get("eval_count") or None
            asyncio.create_task(rate_limiter.record_request(
                identity, model=model, status="ok",
                tokens=tokens, latency_ms=elapsed, intent=intent
            ))

            data.update({
                "_intent": intent,
                "_model": model,
                "_server": role,
                "_cached": False,
                "_has_api_key": bool(api_key),
                "conversation_id": conversation_id,
            })
            cache.set(model, ollama_prompt, data)
            log(f"Response from {role}: {data.get('eval_count', 0)} tokens in {elapsed}ms")

            return JSONResponse(
                content=data,
                headers={"X-Server": role, "X-Model": model, "X-Conversation-ID": conversation_id}
            )
        except Exception as e:
            SERVER_STATE[role]["errors"] += 1
            STATS["failed_requests"] += 1
            SmartScheduler.finish_request(role)
            asyncio.create_task(rate_limiter.record_request(
                identity, model=model, status="error", latency_ms=None, intent=intent
            ))
            log(f"Error on {role}: {e}")
            return JSONResponse(
                {"error": str(e), "server": role, "conversation_id": conversation_id},
                status_code=500,
                headers={"X-Conversation-ID": conversation_id}
            )

    except Exception as e:
        STATS["failed_requests"] += 1
        log(f"Gateway Error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

###############################################################################
# PHASE 1 & 2: USAGE & ANALYTICS ENDPOINTS
###############################################################################

def resolve_caller(request: Request) -> Tuple[str, bool]:
    """
    Identifies the caller and determines admin status.
    Returns (identity, is_admin).
    """
    api_key_name = None
    is_admin = False
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        potential_key = auth_header.replace("Bearer ", "").strip()
        info = find_api_key(potential_key)
        if info and info.get("active", True):
            expires = info.get("expires_at")
            if not expires or datetime.fromisoformat(expires) > datetime.now():
                api_key_name = info.get("name", potential_key[:8])
                if info.get("name") == ADMIN_KEY_NAME or info.get("role") == "admin":
                    is_admin = True

    client_ip = get_client_ip(request)
    identity  = rate_limiter.make_identity(api_key_name, client_ip)
    return identity, is_admin


async def authenticate_admin_request(
    request: Request, action: str, resource_type: str = "admin", resource_id: Optional[str] = None
) -> Tuple[Optional[str], Optional[JSONResponse]]:
    """
    Strictly authenticates an administrator request.
    Enforces 401 Unauthorized if token missing/invalid, 403 Forbidden if not admin.
    Logs security audit events automatically.
    """
    request_id = getattr(request.state, "request_id", f"req_{secrets.token_hex(8)}")
    client_ip = get_client_ip(request)
    user_agent = request.headers.get("User-Agent", "")

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        await security.log_audit_event(
            actor_type="anonymous",
            actor_id=f"ip:{client_ip}",
            action=f"{action}_DENIED",
            resource_type=resource_type,
            resource_id=resource_id,
            result="denied",
            ip_address=client_ip,
            request_id=request_id,
            user_agent=user_agent,
            metadata={"reason": "Missing Bearer token"}
        )
        return None, JSONResponse({"error": "Unauthorized — Bearer token required"}, status_code=401)

    potential_key = auth_header.replace("Bearer ", "").strip()
    info = find_api_key(potential_key)

    if not info or not info.get("active", True):
        await security.log_audit_event(
            actor_type="anonymous",
            actor_id=f"ip:{client_ip}",
            action="AUTH_FAILURE",
            resource_type=resource_type,
            resource_id=resource_id,
            result="failed",
            ip_address=client_ip,
            request_id=request_id,
            user_agent=user_agent,
            metadata={"reason": "Invalid or inactive API key"}
        )
        return None, JSONResponse({"error": "Unauthorized — invalid or inactive API key"}, status_code=401)

    expires = info.get("expires_at")
    if expires and datetime.fromisoformat(expires) <= datetime.now():
        await security.log_audit_event(
            actor_type="user",
            actor_id=f"key:{info.get('name', 'unknown')}",
            action="AUTH_FAILURE",
            resource_type=resource_type,
            resource_id=resource_id,
            result="failed",
            ip_address=client_ip,
            request_id=request_id,
            user_agent=user_agent,
            metadata={"reason": "API key expired"}
        )
        return None, JSONResponse({"error": "Forbidden — API key expired"}, status_code=403)

    is_admin = (info.get("role") == "admin" or info.get("name") == ADMIN_KEY_NAME)
    key_name = info.get("name", "admin")

    if not is_admin:
        await security.log_audit_event(
            actor_type="user",
            actor_id=f"key:{key_name}",
            action="ADMIN_ACCESS_DENIED",
            resource_type=resource_type,
            resource_id=resource_id,
            result="denied",
            ip_address=client_ip,
            request_id=request_id,
            user_agent=user_agent,
            metadata={"reason": "Non-admin key attempted privileged operation"}
        )
        return None, JSONResponse({"error": "Forbidden — admin key required"}, status_code=403)

    # Authorized admin
    await security.log_audit_event(
        actor_type="admin",
        actor_id=f"key:{key_name}",
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        result="success",
        ip_address=client_ip,
        request_id=request_id,
        user_agent=user_agent,
    )
    return key_name, None


@app.get("/api/usage")
async def api_usage(request: Request):
    """
    Returns usage stats for the calling identity (API key or IP).
    No authentication required — returns stats for whoever is calling.
    """
    identity, _ = resolve_caller(request)
    stats = await rate_limiter.get_usage_stats(identity)
    return JSONResponse(stats)


@app.get("/api/analytics/summary")
async def analytics_summary(request: Request, range: str = "24h"):
    """
    Aggregated usage summary. Admin gets global data (or filtered);
    standard caller gets scoped data for their identity.
    """
    identity, is_admin = resolve_caller(request)
    scope_identity = None if is_admin else identity
    try:
        data = await analytics.get_summary(identity=scope_identity, time_range=range)
        return JSONResponse(data)
    except ValueError as ve:
        return JSONResponse({"error": str(ve)}, status_code=400)
    except Exception as e:
        log(f"[ANALYTICS ERROR] Summary: {e}")
        return JSONResponse({"error": "Failed to fetch analytics summary"}, status_code=500)


@app.get("/api/analytics/requests")
async def analytics_requests(request: Request, range: str = "24h"):
    """Time-series request trend buckets for charts."""
    identity, is_admin = resolve_caller(request)
    scope_identity = None if is_admin else identity
    try:
        data = await analytics.get_request_timeseries(identity=scope_identity, time_range=range)
        return JSONResponse(data)
    except ValueError as ve:
        return JSONResponse({"error": str(ve)}, status_code=400)
    except Exception as e:
        log(f"[ANALYTICS ERROR] Requests: {e}")
        return JSONResponse({"error": "Failed to fetch request time-series"}, status_code=500)


@app.get("/api/analytics/models")
async def analytics_models(request: Request, range: str = "24h"):
    """Model-level usage, tokens, latency, and error breakdown."""
    identity, is_admin = resolve_caller(request)
    scope_identity = None if is_admin else identity
    try:
        data = await analytics.get_model_stats(identity=scope_identity, time_range=range)
        return JSONResponse(data)
    except ValueError as ve:
        return JSONResponse({"error": str(ve)}, status_code=400)
    except Exception as e:
        log(f"[ANALYTICS ERROR] Models: {e}")
        return JSONResponse({"error": "Failed to fetch model analytics"}, status_code=500)


@app.get("/api/analytics/latency")
async def analytics_latency(request: Request, range: str = "24h"):
    """Latency distribution and stats."""
    identity, is_admin = resolve_caller(request)
    scope_identity = None if is_admin else identity
    try:
        data = await analytics.get_latency_stats(identity=scope_identity, time_range=range)
        return JSONResponse(data)
    except ValueError as ve:
        return JSONResponse({"error": str(ve)}, status_code=400)
    except Exception as e:
        log(f"[ANALYTICS ERROR] Latency: {e}")
        return JSONResponse({"error": "Failed to fetch latency analytics"}, status_code=500)


@app.get("/api/analytics/errors")
async def analytics_errors(request: Request, range: str = "24h"):
    """Error categories and rate limit event breakdown."""
    identity, is_admin = resolve_caller(request)
    scope_identity = None if is_admin else identity
    try:
        data = await analytics.get_error_stats(identity=scope_identity, time_range=range)
        return JSONResponse(data)
    except ValueError as ve:
        return JSONResponse({"error": str(ve)}, status_code=400)
    except Exception as e:
        log(f"[ANALYTICS ERROR] Errors: {e}")
        return JSONResponse({"error": "Failed to fetch error analytics"}, status_code=500)


@app.get("/api/analytics/recent")
async def analytics_recent(request: Request, limit: int = 20):
    """Recent activity log (bounded and masked)."""
    identity, is_admin = resolve_caller(request)
    scope_identity = None if is_admin else identity
    try:
        data = await analytics.get_recent_activity(identity=scope_identity, limit=limit)
        return JSONResponse(data)
    except Exception as e:
        log(f"[ANALYTICS ERROR] Recent: {e}")
        return JSONResponse({"error": "Failed to fetch recent activity"}, status_code=500)


@app.get("/api/analytics/keys")
async def analytics_keys(request: Request, range: str = "24h"):
    """Aggregated usage per API key (Admin only)."""
    _, err_resp = await authenticate_admin_request(request, action="ADMIN_ACCESS", resource_type="analytics")
    if err_resp:
        return err_resp
    try:
        data = await analytics.get_key_usage(time_range=range)
        return JSONResponse(data)
    except ValueError as ve:
        return JSONResponse({"error": str(ve)}, status_code=400)
    except Exception as e:
        log(f"[ANALYTICS ERROR] Key usage: {e}")
        return JSONResponse({"error": "Failed to fetch key usage"}, status_code=500)


@app.post("/api/admin/set-quota")
async def admin_set_quota(request: Request):
    """
    Allows an administrator to override quota limits for a specific identity.
    SECURITY: Enforces valid administrator authentication and logs audit events.
    """
    admin_user, err_resp = await authenticate_admin_request(request, action="QUOTA_CHANGED", resource_type="quota")
    if err_resp:
        return err_resp

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    identity      = body.get("identity", "").strip()
    hourly_quota  = body.get("hourly_quota")
    burst_limit   = body.get("burst_limit")
    burst_window  = body.get("burst_window", 60)

    if not identity:
        return JSONResponse({"error": "'identity' is required"}, status_code=422)
    if not isinstance(hourly_quota, int) or hourly_quota < 1 or hourly_quota > 100_000:
        return JSONResponse({"error": "'hourly_quota' must be an integer between 1 and 100000"}, status_code=422)
    if not isinstance(burst_limit, int) or burst_limit < 1 or burst_limit > 1000:
        return JSONResponse({"error": "'burst_limit' must be an integer between 1 and 1000"}, status_code=422)
    if not isinstance(burst_window, int) or burst_window < 10 or burst_window > 3600:
        return JSONResponse({"error": "'burst_window' must be an integer between 10 and 3600"}, status_code=422)

    await rate_limiter.set_quota(identity, hourly_quota, burst_limit, burst_window)
    log(f"[ADMIN] Quota updated: identity={identity}, hourly={hourly_quota}, burst={burst_limit}/{burst_window}s")
    return JSONResponse({
        "ok": True,
        "identity": identity,
        "hourly_quota": hourly_quota,
        "burst_limit": burst_limit,
        "burst_window": burst_window,
    })


###############################################################################
# HEALTH & STATUS ENDPOINTS
###############################################################################

@app.get("/health")
async def health():
    online_count = sum(1 for s in SERVER_STATE.values() if s["online"])
    return {
        "healthy": online_count > 0,
        "online_count": online_count,
        "total_servers": len(SERVER_STATE),
        "servers": {role: s["online"] for role, s in SERVER_STATE.items()}
    }

@app.get("/status")
async def status():
    workers = []
    for role, s in SERVER_STATE.items():
        workers.append({
            "role": role,
            "url": s["url"],
            "online": s["online"],
            "queue": s["queue"],
            "latency_ms": s["latency"],
            "requests": s["requests"],
            "errors": s["errors"],
            "models": s["models"],
        })
    return {
        "gateway": "Running (Ultra-Fast v3.0)",
        "workers": workers,
        "statistics": STATS,
        "token_limits": MAX_TOKENS_BY_TYPE,
    }


###############################################################################
# PHASE 2: USAGE ANALYTICS DASHBOARD UI
###############################################################################

@app.get("/dashboard")
async def dashboard():
    """Serves the Production AI Gateway Control Plane Dashboard."""
    dashboard_file = Path("Frontend/dashboard.html")
    if dashboard_file.exists():
        return FileResponse(str(dashboard_file))
    return HTMLResponse("<h1>AI Gateway Dashboard</h1><p>Dashboard file not found.</p>", status_code=404)

###############################################################################
# ADMIN KEY MANAGEMENT & AUDIT LOGS
###############################################################################

@app.post("/api/admin/generate-key")
async def generate_api_key(request: Request):
    """Generates a new API key. SECURITY: Admin authentication required."""
    admin_user, err_resp = await authenticate_admin_request(request, action="API_KEY_CREATED", resource_type="api_key")
    if err_resp:
        return err_resp

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    name = str(body.get("name", "Unnamed Key")).strip()
    expires_in_days = body.get("expires_in_days", 365)
    role = str(body.get("role", "user")).strip()

    if not isinstance(expires_in_days, int) or expires_in_days < 1 or expires_in_days > 3650:
        return JSONResponse({"error": "'expires_in_days' must be an integer between 1 and 3650"}, status_code=422)

    api_key = f"sk_{secrets.token_urlsafe(32)}"
    expires_at = (datetime.now() + timedelta(days=expires_in_days)).isoformat()
    API_KEYS[api_key] = {
        "name": name,
        "role": role,
        "created_at": datetime.now().isoformat(),
        "expires_at": expires_at,
        "active": True,
        "usage_count": 0,
        "last_used": None,
    }
    await save_api_keys_async(API_KEYS)
    return {"api_key": api_key, "name": name, "role": role, "expires_at": expires_at}


@app.get("/api/admin/keys")
async def list_api_keys(request: Request):
    """Lists all registered API keys. SECURITY: Admin authentication required."""
    admin_user, err_resp = await authenticate_admin_request(request, action="ADMIN_ACCESS", resource_type="api_key")
    if err_resp:
        return err_resp

    keys_info = []
    for key, info in API_KEYS.items():
        keys_info.append({
            "key_masked": security.mask_key(key),
            "name": info.get("name"),
            "role": info.get("role", "admin" if info.get("name") == ADMIN_KEY_NAME else "user"),
            "active": info.get("active"),
            "usage_count": info.get("usage_count"),
            "last_used": info.get("last_used"),
            "created_at": info.get("created_at"),
            "expires_at": info.get("expires_at"),
        })
    return {"keys": keys_info}


@app.get("/api/admin/audit-logs")
async def admin_audit_logs(
    request: Request,
    action: Optional[str] = None,
    actor: Optional[str] = None,
    result: Optional[str] = None,
    resource_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """Queries security audit logs. SECURITY: Admin authentication required."""
    admin_user, err_resp = await authenticate_admin_request(request, action="ADMIN_ACCESS", resource_type="audit_log")
    if err_resp:
        return err_resp

    logs = await security.get_audit_logs(
        action=action,
        actor_id=actor,
        result=result,
        resource_type=resource_type,
        limit=limit,
        offset=offset,
    )
    return JSONResponse(logs)

###############################################################################
# ENTRY POINT
###############################################################################

if __name__ == "__main__":
    import uvicorn
    use_ssl = "--ssl" in sys.argv
    ssl_kwargs = {}
    if use_ssl and Path("cert.pem").exists() and Path("key.pem").exists():
        ssl_kwargs = {
            "ssl_keyfile": "key.pem",
            "ssl_certfile": "cert.pem"
        }
        log("Running with SSL (HTTPS)")
    else:
        log("Running in standard HTTP mode (no self-signed certificate errors)")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
        **ssl_kwargs
    )