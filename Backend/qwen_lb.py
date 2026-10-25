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
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
)
from fastapi.staticfiles import StaticFiles
import httpx

import image_generator

# ── Faster-Whisper STT Integration Support ─────────────────────────────────
try:
    import av
    # PyAV 19.0+ compatibility shim for faster-whisper audio decoding
    _orig_av_open = av.open
    def _safe_av_open(*args, **kwargs):
        kwargs.pop("metadata_errors", None)
        return _orig_av_open(*args, **kwargs)
    av.open = _safe_av_open

    from faster_whisper import WhisperModel
    FASTER_WHISPER_AVAILABLE = True
except ImportError:
    WhisperModel = None
    FASTER_WHISPER_AVAILABLE = False

WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "small")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_CPU_THREADS = int(os.getenv("WHISPER_CPU_THREADS", "4"))
whisper_model: Optional[Any] = None

###############################################################################
# CONFIGURATION & SERVERS
###############################################################################

SERVERS = {
    "local": {
        "url": "http://127.0.0.1:11434",
        "models": ["mistral:7b", "qwen2.5vl:7b"],
        "priority": 1,
    },
    "gpu_workstation": {
        "url": "http://172.16.8.170:11434",
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
# PATH RESOLUTION & API KEY MANAGEMENT
###############################################################################

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent if BASE_DIR.name == "Backend" else BASE_DIR

API_KEYS_FILE = BASE_DIR / "api_keys.json"
if not API_KEYS_FILE.exists() and (ROOT_DIR / "api_keys.json").exists():
    API_KEYS_FILE = ROOT_DIR / "api_keys.json"

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
    p_hash = hashlib.sha256(potential_key.encode("utf-8")).hexdigest()
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
IMAGE_GEN = "image_gen"

class FastIntentDetector:
    IMAGE_PREFIXES = ("/image", "/img", "/draw", "draw ", "paint ", "sketch ", "/generate_image", "/generate image", "generate image", "generate an image")
    IMAGE_PHRASES = [
        "generate an image", "generate image", "create an image", "create a picture",
        "draw a picture", "draw an image", "draw an ", "draw a ", "generate a photo", "create a photo",
        "generate a drawing", "generate wallpaper", "paint a ", "paint an ", "sketch a "
    ]
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
        
        # Check image generation triggers
        if any(text.startswith(p) for p in FastIntentDetector.IMAGE_PREFIXES):
            return IMAGE_GEN
        if any(phrase in text for phrase in FastIntentDetector.IMAGE_PHRASES):
            return IMAGE_GEN

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

        # Prefer local GPU worker when available for instant sub-second responses
        if "local" in candidates and SERVER_STATE.get("local", {}).get("queue", 0) < 2:
            return {"url": SERVER_STATE["local"]["url"], "role": "local"}

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
        res = await http_client.get(f"{state['url']}/api/tags", timeout=0.8)
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
    await asyncio.sleep(0.5)
    for role, state in SERVER_STATE.items():
        if not state["online"]:
            continue
        keep_alive = "24h"
        for model in state["models"]:
            try:
                await http_client.post(
                    f"{state['url']}/api/generate",
                    json={"model": model, "prompt": "hi", "stream": False, "keep_alive": keep_alive},
                    timeout=15.0
                )
                log(f"[OK] Warmed {model} on {role} (keep_alive={keep_alive})")
            except Exception as e:
                pass

###############################################################################
# FASTAPI LIFESPAN & APP
###############################################################################

@asynccontextmanager
async def lifespan(app: FastAPI):
    log("=" * 70)
    log("AI GATEWAY STARTING")
    log("=" * 70)

    # Faster-Whisper Model Startup Loading
    global whisper_model
    if FASTER_WHISPER_AVAILABLE:
        try:
            log(f"Loading Faster-Whisper STT model (size: '{WHISPER_MODEL_SIZE}', device: '{WHISPER_DEVICE}', compute: '{WHISPER_COMPUTE_TYPE}')...")
            whisper_model = WhisperModel(
                WHISPER_MODEL_SIZE,
                device=WHISPER_DEVICE,
                compute_type=WHISPER_COMPUTE_TYPE,
                cpu_threads=WHISPER_CPU_THREADS,
            )
            log(f"Faster-Whisper STT model [{WHISPER_MODEL_SIZE}] loaded successfully [OK]")
        except Exception as e:
            log(f"[WARNING] Faster-Whisper model failed to load on startup: {e}")
            whisper_model = None
    else:
        log("[INFO] 'faster-whisper' package is not installed. STT endpoints will require installation.")

    # Quick non-blocking initial ping
    await check_server_health_all()
    online = [r for r, s in SERVER_STATE.items() if s["online"]]
    log(f"Initial online workers ({len(online)}/{len(SERVER_STATE)}): {', '.join(online)}")

    async def prewarm_image_pipeline():
        await asyncio.sleep(1.5)
        if image_generator.is_available():
            try:
                log("Pre-warming FLUX image generation pipeline in background...")
                await asyncio.to_thread(image_generator.get_pipeline)
                log("FLUX image generation pipeline pre-warmed and ready [OK]")
            except Exception as e:
                log(f"FLUX pipeline pre-warm deferred: {e}")

    health_task = asyncio.create_task(periodic_health_check())
    warmup_task = asyncio.create_task(warmup_models())
    flux_prewarm_task = asyncio.create_task(prewarm_image_pipeline())

    log("=" * 70)
    log("GATEWAY READY [OK]")
    log("=" * 70)
    yield
    health_task.cancel()
    warmup_task.cancel()
    flux_prewarm_task.cancel()
    await http_client.aclose()


app = FastAPI(
    title="CUTM AI Gateway API",
    description="Enterprise AI Gateway & Load Balancer providing Ollama native endpoints (/api/generate), OpenAI-compatible endpoints (/v1/chat/completions, /v1/models, /v1/images/generations), FLUX.1 image generation, and Faster-Whisper Speech-to-Text.",
    version="3.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc"
)

# Standard CORS Middleware (allows all origins, headers, methods)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Server", "X-Model", "X-Request-ID", "X-Conversation-ID", "Content-Disposition"],
)

###############################################################################
# MOUNT FRONTEND
###############################################################################

frontend_dir = ROOT_DIR / "Frontend"
if not frontend_dir.exists():
    frontend_dir = BASE_DIR / "Frontend"

if frontend_dir.exists():
    if (frontend_dir / "css").exists():
        app.mount("/css", StaticFiles(directory=str(frontend_dir / "css")), name="css")
    if (frontend_dir / "js").exists():
        app.mount("/js", StaticFiles(directory=str(frontend_dir / "js")), name="js")
    app.mount("/chat", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
    log("Mounted static Frontend at / and /chat")

generated_images_dir = BASE_DIR / "generated_images" if (BASE_DIR / "generated_images").exists() else ROOT_DIR / "generated_images"
generated_images_dir.mkdir(parents=True, exist_ok=True)
app.mount("/generated_images", StaticFiles(directory=str(generated_images_dir)), name="generated_images")
log("Mounted /generated_images static directory [OK]")

@app.get("/")
async def root():
    if (frontend_dir / "index.html").exists():
        return FileResponse(str(frontend_dir / "index.html"))
    return HTMLResponse("<h1>AI Gateway Ready</h1><p><a href='/chat'>Open Chat Interface</a></p>")

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
    elif intent == IMAGE_GEN:
        STATS["image_gen_requests"] = STATS.get("image_gen_requests", 0) + 1

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
        info = find_api_key(potential_key)
        if info and info.get("active", True):
            expires = info.get("expires_at")
            if not expires or datetime.fromisoformat(expires) > datetime.now():
                api_key      = potential_key
                api_key_name = info.get("name", potential_key[:8])
                info["usage_count"] = info.get("usage_count", 0) + 1
                info["last_used"] = datetime.now().isoformat()
                asyncio.create_task(save_api_keys_async(API_KEYS))

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
        if requested_model and requested_model in ["mistral:7b", "qwen2.5vl:7b", "qwen2.5-coder:32b", "qwen3:32b", "flux", "flux-schnell", "flux.1", "stable-diffusion", "sd15", "diffusion", "image-gen"]:
            model = requested_model
        else:
            model = select_model(intent)
        body["model"] = model

        # ── Fast-path for Image Generation Intent ────────────────────────────
        is_explicit_image_req = requested_model in ["flux", "flux-schnell", "flux.1", "stable-diffusion", "sd15", "diffusion", "image-gen"]
        if intent == IMAGE_GEN or is_explicit_image_req:
            if not image_generator.is_available():
                if is_explicit_image_req:
                    return JSONResponse(
                        {"error": "Image generation engine (FLUX.1 [schnell] 4-bit) is not installed on this server.", "conversation_id": conversation_id},
                        status_code=503
                    )
                else:
                    log(f"[IMAGE_GEN] Image generation requested but FLUX model weights are not loaded. Routing to LLM...")
                    intent = CHAT
            else:
                clean_prompt = prompt
                for prefix in FastIntentDetector.IMAGE_PREFIXES:
                    if clean_prompt.lower().startswith(prefix):
                        clean_prompt = clean_prompt[len(prefix):].strip()
                        break
                for phrase in FastIntentDetector.IMAGE_PHRASES:
                    if phrase in clean_prompt.lower():
                        idx = clean_prompt.lower().find(phrase)
                        after = clean_prompt[idx + len(phrase):].strip()
                        if after.lower().startswith("of "):
                            after = after[3:].strip()
                        if after:
                            clean_prompt = after
                        break

                try:
                    gen_result = await image_generator.generate_image_async(
                        prompt=clean_prompt or prompt,
                        num_inference_steps=3,
                        guidance_scale=0.0,
                    )
                    elapsed = gen_result["latency_ms"]
                    img_url = gen_result["image_url"]
                    formatted_md = (
                        f"![Generated Image]({img_url})\n\n"
                        f"**Prompt:** *{gen_result['prompt']}*\n"
                        f"**Seed:** `{gen_result['seed']}` | **Steps:** `{gen_result['steps']}` | **Latency:** `{elapsed}ms`"
                    )

                    conversation_memory.set_last_response(conversation_id, f"[GENERATED IMAGE]: {gen_result['prompt']}", prompt)
                    log(f"[IMAGE_GEN] Generated '{gen_result['prompt'][:35]}...' in {elapsed}ms (seed={gen_result['seed']})")

                    if stream:
                        async def img_stream():
                            chunk_dict = {
                                "model": "FLUX.1-schnell",
                                "response": formatted_md,
                                "done": True,
                                "eval_count": 50,
                                "image_url": img_url,
                                "seed": gen_result["seed"],
                                "latency_ms": elapsed,
                                "conversation_id": conversation_id,
                            }
                            yield (json.dumps(chunk_dict) + "\n").encode("utf-8")

                        return StreamingResponse(
                            img_stream(),
                            media_type="application/x-ndjson",
                            headers={"X-Server": "local-diffusion", "X-Model": "FLUX.1-schnell", "X-Conversation-ID": conversation_id}
                        )
                    else:
                        return JSONResponse(
                            content={
                                "model": "FLUX.1-schnell",
                                "response": formatted_md,
                                "image_url": img_url,
                                "seed": gen_result["seed"],
                                "latency_ms": elapsed,
                                "_intent": IMAGE_GEN,
                                "_server": "local-diffusion",
                                "conversation_id": conversation_id,
                            },
                            headers={"X-Server": "local-diffusion", "X-Model": "stable-diffusion-v1.5", "X-Conversation-ID": conversation_id}
                        )
                except Exception as e:
                    log(f"[IMAGE_GEN ERROR] {e}")
                    STATS["failed_requests"] += 1
                    return JSONResponse({"error": f"Image generation failed: {str(e)}", "conversation_id": conversation_id}, status_code=500)

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
        body["keep_alive"] = "24h"
        if "options" not in body or not isinstance(body["options"], dict):
            body["options"] = {}
        body["options"]["num_predict"] = max_tokens
        body["options"]["temperature"] = 0.3
        body["options"]["top_p"] = 0.8
        body["options"]["repeat_penalty"] = 1.1
        body["options"]["num_ctx"] = 2048
        body["options"]["f16_kv"] = True
        body["options"]["num_thread"] = 8

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
                else:
                    elapsed = round((time.perf_counter() - req_start) * 1000, 1)
                    full_resp = "".join(accumulated_text)
                    if full_resp:
                        conversation_memory.set_last_response(conversation_id, full_resp, prompt)
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
# SPEECH-TO-TEXT (STT) TRANSCRIPTION ENGINE & ENDPOINTS
###############################################################################

ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".webm", ".flac", ".aac", ".wma"}

def _transcribe_audio_file(file_path: str, language: Optional[str] = None) -> Dict[str, Any]:
    """
    Synchronous audio transcription worker function executed in background threadpool.
    Supports MP3, WAV, M4A, OGG, WEBM, FLAC, AAC formats.
    Runs on CPU with int8 quantization to prevent cublas64_12.dll missing library errors.
    """
    global whisper_model
    if whisper_model is None:
        if not FASTER_WHISPER_AVAILABLE:
            raise RuntimeError("Faster-Whisper model is not installed or failed to initialize.")
        whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8", cpu_threads=WHISPER_CPU_THREADS)

    try:
        segments, info = whisper_model.transcribe(
            file_path,
            language=language if (language and language.strip().lower() != "auto") else None,
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        segment_list = list(segments)
    except Exception as e:
        log(f"[STT WARNING] Transcribe error ({e}), reinitializing Whisper on CPU (int8)...")
        whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8", cpu_threads=WHISPER_CPU_THREADS)
        segments, info = whisper_model.transcribe(
            file_path,
            language=language if (language and language.strip().lower() != "auto") else None,
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        segment_list = list(segments)

    transcribed_text = " ".join(seg.text.strip() for seg in segment_list).strip()

    return {
        "text": transcribed_text,
        "language": getattr(info, "language", "en"),
        "language_probability": round(getattr(info, "language_probability", 1.0), 4),
        "duration": round(getattr(info, "duration", 0.0), 2),
        "segments_count": len(segment_list),
    }

async def _handle_transcription_request(
    request: Request,
    file: UploadFile,
    language: Optional[str],
    conversation_id: Optional[str],
    test_type: str,
) -> JSONResponse:
    """
    Shared handler for /api/tests/listening/transcribe and /api/tests/speaking/transcribe.
    Performs file validation, asynchronous thread-pool offloading, and deterministic cleanup.
    """
    if not file or not file.filename:
        return JSONResponse(
            {"error": "No audio file uploaded", "message": "Please provide an audio file in form-data ('file')."},
            status_code=400
        )

    file_ext = Path(file.filename).suffix.lower()
    if file_ext not in ALLOWED_AUDIO_EXTENSIONS:
        return JSONResponse(
            {
                "error": "Unsupported audio format",
                "message": f"File extension '{file_ext}' is not supported. Supported: {', '.join(sorted(ALLOWED_AUDIO_EXTENSIONS))}",
                "supported_formats": sorted(list(ALLOWED_AUDIO_EXTENSIONS)),
            },
            status_code=400
        )

    if not FASTER_WHISPER_AVAILABLE or whisper_model is None:
        return JSONResponse(
            {
                "error": "Faster-Whisper engine unavailable",
                "message": "Faster-Whisper is not installed or model failed to initialize on startup."
            },
            status_code=503
        )

    # Conversation ID resolution
    conv_id = conversation_id or request.headers.get("X-Conversation-ID")
    if not conv_id or not str(conv_id).strip():
        conv_id = f"conv_{secrets.token_hex(12)}"
    else:
        conv_id = str(conv_id).strip()

    req_start = time.perf_counter()
    temp_file_path = None

    try:
        # Create a secure temporary file with the matching audio extension
        with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as tmp:
            temp_file_path = tmp.name
            shutil.copyfileobj(file.file, tmp)

        # Offload synchronous CTranslate2 inference to worker thread pool
        result = await asyncio.to_thread(_transcribe_audio_file, temp_file_path, language)
        elapsed_ms = round((time.perf_counter() - req_start) * 1000, 1)

        # Link transcript into conversation memory if text exists
        if result["text"]:
            conversation_memory.set_last_response(
                conv_id,
                f"[{test_type.upper()} TEST AUDIO TRANSCRIPT]: {result['text']}",
                prompt=f"Uploaded audio {file.filename}"
            )

        log(f"[{test_type.upper()}_STT] Transcribed '{file.filename}' ({result.get('duration', 0)}s audio) in {elapsed_ms}ms: \"{result['text'][:60]}...\"")

        response_payload = {
            "text": result["text"],
            "language": result["language"],
            "language_probability": result["language_probability"],
            "duration_seconds": result["duration"],
            "latency_ms": elapsed_ms,
            "test_type": test_type,
            "conversation_id": conv_id,
        }

        return JSONResponse(
            content=response_payload,
            headers={"X-Conversation-ID": conv_id, "X-STT-Model": WHISPER_MODEL_SIZE}
        )

    except Exception as e:
        log(f"[{test_type.upper()}_STT ERROR] Transcription failed for '{file.filename}': {e}")
        return JSONResponse(
            {
                "error": "Transcription failed",
                "message": str(e),
                "conversation_id": conv_id,
            },
            status_code=500,
            headers={"X-Conversation-ID": conv_id}
        )

    finally:
        # Guarantee deletion of temporary audio file
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except Exception as e:
                log(f"[CLEANUP WARNING] Could not remove temp file {temp_file_path}: {e}")
        await file.close()


@app.post("/api/tests/listening/transcribe")
async def transcribe_listening_test(
    request: Request,
    file: UploadFile = File(...),
    language: Optional[str] = Form(None),
    conversation_id: Optional[str] = Form(None),
):
    """
    Transcribes audio for Listening tests.
    Accepts: MP3, WAV, M4A, OGG, WEBM, FLAC, AAC.
    Returns: JSON with transcribed text, detected/specified language, and audio metadata.
    """
    return await _handle_transcription_request(
        request=request,
        file=file,
        language=language,
        conversation_id=conversation_id,
        test_type="listening",
    )


@app.post("/api/tests/speaking/transcribe")
async def transcribe_speaking_test(
    request: Request,
    file: UploadFile = File(...),
    language: Optional[str] = Form(None),
    conversation_id: Optional[str] = Form(None),
):
    """
    Transcribes candidate audio recordings for Speaking tests.
    Accepts: MP3, WAV, M4A, OGG, WEBM, FLAC, AAC.
    Returns: JSON with candidate speech text, detected/specified language, and audio metadata.
    """
    return await _handle_transcription_request(
        request=request,
        file=file,
        language=language,
        conversation_id=conversation_id,
        test_type="speaking",
    )

###############################################################################
# TEST EVALUATION MODULES (READING, WRITING, LISTENING, SPEAKING)
###############################################################################

async def _evaluate_test_submission(
    test_type: str,
    prompt: str,
    conversation_id: Optional[str] = None,
    system_prompt: Optional[str] = None,
) -> JSONResponse:
    """Helper that routes test evaluation to the LLM cluster (Mistral:7b)."""
    conv_id = conversation_id or f"conv_{secrets.token_hex(12)}"
    
    # Check conversation memory if prior transcript/context exists
    prev_context = conversation_memory.get_last_response(conv_id)
    combined_prompt = prompt
    if prev_context:
        combined_prompt = f"{prev_context}\n\nCandidate Submission & Evaluation Request:\n{prompt}"
    
    body = {
        "model": "mistral:7b",
        "prompt": combined_prompt,
        "stream": False,
        "keep_alive": "24h",
        "options": {
            "num_predict": 400,
            "temperature": 0.2,
            "top_p": 0.85
        }
    }
    if system_prompt:
        body["system"] = system_prompt

    worker = SmartScheduler.select_worker("mistral:7b")
    if not worker:
        return JSONResponse(
            {"error": "No LLM worker server available for evaluation", "test_type": test_type, "conversation_id": conv_id},
            status_code=503,
            headers={"X-Conversation-ID": conv_id}
        )

    role = worker["role"]
    url = worker["url"]
    SmartScheduler.start_request(role)
    t0 = time.perf_counter()

    try:
        resp = await http_client.post(f"{url}/api/generate", json=body, timeout=120.0)
        data = resp.json()
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        SmartScheduler.finish_request(role, elapsed_ms)

        eval_text = data.get("response", "")
        if eval_text:
            conversation_memory.set_last_response(conv_id, eval_text, prompt)

        return JSONResponse(
            content={
                "test_type": test_type,
                "evaluation": eval_text,
                "model": "mistral:7b",
                "server": role,
                "latency_ms": elapsed_ms,
                "conversation_id": conv_id,
            },
            headers={"X-Server": role, "X-Conversation-ID": conv_id, "X-Test-Type": test_type}
        )
    except Exception as e:
        SmartScheduler.finish_request(role)
        return JSONResponse(
            {"error": f"Evaluation failed on worker {role}: {str(e)}", "test_type": test_type, "conversation_id": conv_id},
            status_code=500,
            headers={"X-Conversation-ID": conv_id}
        )


@app.post("/api/tests/reading", tags=["Language Assessment"])
async def evaluate_reading_test(request: Request):
    """
    Evaluates Reading test answers (passage comprehension, questions, vocabulary).
    Requires JSON: { "passage": "...", "answers": "..." }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    conv_id = body.get("conversation_id")
    passage = str(body.get("passage", "")).strip()
    answers = str(body.get("answers", "")).strip()
    prompt = str(body.get("prompt", "")).strip()

    if not passage:
        return JSONResponse({"error": "Missing required field: 'passage' cannot be empty."}, status_code=400)
    if not answers:
        return JSONResponse({"error": "Missing required field: 'answers' cannot be empty."}, status_code=400)

    eval_prompt = (
        f"READING PASSAGE:\n{passage}\n\n"
        f"CANDIDATE ANSWERS:\n{answers}\n\n"
        f"TASK: Grade ONLY the candidate answers provided. Do NOT invent, hallucinate, or generate any other questions or answers."
    )

    sys_prompt = (
        "You are an English Reading Examiner. Grade ONLY the exact candidate answers provided against the passage.\n"
        "DO NOT invent, complete, or generate any additional questions (e.g. do NOT create Q2, Q3, etc.).\n"
        "You MUST follow this EXACT output format:\n"
        "Grade: <Correct / Partially Correct / Incorrect, or X/Y score>\n"
        "Explanation: <concise factual feedback explaining why the answer is correct or incorrect>"
    )
    return await _evaluate_test_submission("reading", eval_prompt, conversation_id=conv_id, system_prompt=sys_prompt)


@app.post("/api/tests/writing", tags=["Language Assessment"])
async def evaluate_writing_test(request: Request):
    """
    Evaluates Writing test submissions (essays, reports).
    Requires JSON: { "task_prompt": "...", "essay": "..." }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    conv_id = body.get("conversation_id")
    essay = str(body.get("essay", "") or body.get("submission", "")).strip()
    task_prompt = str(body.get("task_prompt", "")).strip()
    prompt = str(body.get("prompt", "")).strip()

    if not task_prompt:
        return JSONResponse({"error": "Missing required field: 'task_prompt' cannot be empty."}, status_code=400)
    if not essay:
        return JSONResponse({"error": "Missing required field: 'essay' cannot be empty."}, status_code=400)

    eval_prompt = (
        f"WRITING PROMPT / TOPIC:\n{task_prompt}\n\n"
        f"CANDIDATE ESSAY:\n{essay}\n\n"
        f"TASK: Evaluate this essay strictly according to official IELTS band descriptors."
    )

    sys_prompt = (
        "You are an official IELTS English Writing Examiner. Assess the candidate essay strictly.\n"
        "Do NOT invent or write text for the essay.\n"
        "You MUST follow this EXACT output format without deviating:\n"
        "Overall Band: <X.X>\n"
        "Task Achievement: <X.X>\n"
        "Coherence and Cohesion: <X.X>\n"
        "Lexical Resource: <X.X>\n"
        "Grammatical Range and Accuracy: <X.X>\n"
        "Feedback: <concise actionable feedback>"
    )
    return await _evaluate_test_submission("writing", eval_prompt, conversation_id=conv_id, system_prompt=sys_prompt)


@app.post("/api/tests/listening", tags=["Language Assessment"])
async def evaluate_listening_test(request: Request):
    """
    Evaluates Listening test answers against audio transcript.
    Requires JSON: { "transcript": "...", "answers": "..." }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    conv_id = body.get("conversation_id")
    transcript = str(body.get("transcript", "")).strip()
    answers = str(body.get("answers", "")).strip()
    prompt = str(body.get("prompt", "")).strip()

    if not transcript:
        return JSONResponse({"error": "Missing required field: 'transcript' cannot be empty."}, status_code=400)
    if not answers:
        return JSONResponse({"error": "Missing required field: 'answers' cannot be empty."}, status_code=400)

    eval_prompt = (
        f"AUDIO TRANSCRIPT:\n{transcript}\n\n"
        f"CANDIDATE ANSWERS:\n{answers}\n\n"
        f"TASK: Compare candidate answers strictly against the audio transcript. Grade only the answers provided."
    )

    sys_prompt = (
        "You are an English Listening Examiner. Compare candidate answers strictly against the audio transcript.\n"
        "DO NOT invent or generate any additional questions or answers.\n"
        "You MUST follow this EXACT output format:\n"
        "Score: <X/Y>\n"
        "Explanation: <concise feedback on factual correctness and details>"
    )
    return await _evaluate_test_submission("listening", eval_prompt, conversation_id=conv_id, system_prompt=sys_prompt)


@app.post("/api/tests/speaking", tags=["Language Assessment"])
async def evaluate_speaking_test(request: Request):
    """
    Evaluates candidate Speaking responses (transcribed speech).
    Requires JSON: { "topic": "...", "transcript": "..." }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    conv_id = body.get("conversation_id")
    transcript = str(body.get("transcript", "")).strip()
    topic = str(body.get("topic", "")).strip()
    prompt = str(body.get("prompt", "")).strip()

    if not topic:
        return JSONResponse({"error": "Missing required field: 'topic' cannot be empty."}, status_code=400)
    if not transcript:
        return JSONResponse({"error": "Missing required field: 'transcript' cannot be empty."}, status_code=400)

    eval_prompt = (
        f"SPEAKING TOPIC / QUESTION:\n{topic}\n\n"
        f"CANDIDATE SPOKEN TRANSCRIPT:\n{transcript}\n\n"
        f"TASK: Evaluate this candidate spoken response strictly according to official IELTS speaking descriptors."
    )

    sys_prompt = (
        "You are an official IELTS English Speaking Examiner. Assess the candidate spoken transcript.\n"
        "Do NOT invent or write text for the candidate.\n"
        "You MUST follow this EXACT output format without deviating:\n"
        "Estimated Band: <X.X>\n"
        "Fluency and Coherence: <X.X>\n"
        "Lexical Resource: <X.X>\n"
        "Grammatical Accuracy: <X.X>\n"
        "Topical Relevance: <X.X>\n"
        "Feedback: <concise actionable feedback>"
    )
    return await _evaluate_test_submission("speaking", eval_prompt, conversation_id=conv_id, system_prompt=sys_prompt)


###############################################################################
# IMAGE GENERATION (STABLE DIFFUSION v1.5) ENDPOINTS
###############################################################################

@app.post("/api/image/generate")
@app.post("/api/generate-image")
async def api_generate_image(request: Request):
    """
    Dedicated endpoint for generating images via Stable Diffusion v1.5.
    Accepts JSON:
    {
        "prompt": "...",
        "negative_prompt": "...",
        "width": 512,
        "height": 512,
        "steps": 25,
        "guidance_scale": 7.5,
        "seed": null,
        "conversation_id": "..."
    }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    prompt = body.get("prompt", "").strip()
    if not prompt:
        return JSONResponse({"error": "Prompt is required"}, status_code=400)

    conv_id = body.get("conversation_id") or f"conv_{secrets.token_hex(12)}"
    neg_prompt = body.get("negative_prompt", "")
    width = int(body.get("width", 512))
    height = int(body.get("height", 512))
    steps = int(body.get("steps", 3))
    if steps > 4:
        steps = 3
    guidance = float(body.get("guidance_scale", 0.0))
    seed = body.get("seed")
    if seed is not None:
        try:
            seed = int(seed)
        except Exception:
            seed = None

    if not image_generator.is_available():
        return JSONResponse(
            {"error": "FLUX.1 [schnell] 4-bit model is not available or cache is missing."},
            status_code=503
        )

    try:
        result = await image_generator.generate_image_async(
            prompt=prompt,
            negative_prompt=neg_prompt,
            width=width,
            height=height,
            num_inference_steps=steps,
            guidance_scale=guidance,
            seed=seed,
        )
        elapsed = result["latency_ms"]
        log(f"[IMAGE_GEN] Generated image for '{prompt[:40]}...' in {elapsed}ms (seed={result['seed']})")

        return JSONResponse({
            "success": True,
            "image_url": result["image_url"],
            "image_base64": result["image_base64"],
            "prompt": result["prompt"],
            "negative_prompt": result["negative_prompt"],
            "seed": result["seed"],
            "width": result["width"],
            "height": result["height"],
            "steps": result["steps"],
            "guidance_scale": result["guidance_scale"],
            "latency_ms": elapsed,
            "conversation_id": conv_id,
        })
    except Exception as e:
        log(f"[IMAGE_GEN ERROR] {e}")
        return JSONResponse({"error": f"Image generation failed: {str(e)}"}, status_code=500)


@app.post("/v1/images/generations")
async def openai_images_generations(request: Request):
    """OpenAI-compatible image generation endpoint."""
    if not image_generator.is_available():
        return JSONResponse(
            {"error": "FLUX.1 [schnell] 4-bit model is not available or cache is missing."},
            status_code=503
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    prompt = body.get("prompt", "").strip()
    if not prompt:
        return JSONResponse({"error": "Prompt is required"}, status_code=400)

    size = body.get("size", "512x512")
    try:
        w_str, h_str = size.split("x")
        w, h = int(w_str), int(h_str)
    except Exception:
        w, h = 512, 512

    try:
        base_url = str(request.base_url).rstrip("/")
        result = await image_generator.generate_image_async(prompt=prompt, width=w, height=h)
        full_url = f"{base_url}{result['image_url']}"
        return JSONResponse({
            "created": int(time.time()),
            "data": [
                {
                    "url": full_url,
                    "b64_json": result["image_base64"],
                    "revised_prompt": prompt,
                }
            ]
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/v1/models", tags=["OpenAI Compatible"])
async def openai_list_models():
    """
    OpenAI-compatible models list endpoint.
    Used by OpenAI SDK (`client.models.list()`) to inspect available models.
    """
    model_ids = ["mistral:7b", "qwen2.5vl:7b", "qwen2.5-coder:32b", "qwen3:32b", "flux-schnell"]
    data = [
        {
            "id": m,
            "object": "model",
            "created": 1700000000,
            "owned_by": "cutm-ai",
            "permission": [],
            "root": m,
            "parent": None
        }
        for m in model_ids
    ]
    return JSONResponse({"object": "list", "data": data})


@app.post("/v1/chat/completions", tags=["OpenAI Compatible"])
async def openai_chat_completions(request: Request):
    """
    OpenAI-compatible chat completions endpoint.
    Compatible with the official OpenAI Python/Node/cURL clients.
    Supports both non-streaming (`stream=False`) and SSE streaming (`stream=True`).
    """
    api_key = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        potential_key = auth_header.replace("Bearer ", "").strip()
        info = find_api_key(potential_key)
        if info and info.get("active", True):
            expires = info.get("expires_at")
            if not expires or datetime.fromisoformat(expires) > datetime.now():
                api_key = potential_key
                info["usage_count"] = info.get("usage_count", 0) + 1
                info["last_used"] = datetime.now().isoformat()
                asyncio.create_task(save_api_keys_async(API_KEYS))

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
            status_code=400
        )

    requested_model = body.get("model", "mistral:7b")
    if requested_model in ("gpt-3.5-turbo", "gpt-4", "gpt-4o", "gpt-4-turbo", "default", "auto", ""):
        model = "mistral:7b"
    elif requested_model in ["mistral:7b", "qwen2.5vl:7b", "qwen2.5-coder:32b", "qwen3:32b"]:
        model = requested_model
    else:
        model = "mistral:7b"
    body["model"] = model

    stream = bool(body.get("stream", False))

    worker = SmartScheduler.select_worker(model)
    if not worker:
        return JSONResponse(
            {"error": {"message": f"No online server currently available for model '{model}'", "type": "server_error"}},
            status_code=503
        )

    role = worker["role"]
    url = worker["url"]
    SmartScheduler.start_request(role)
    req_start = time.perf_counter()

    if stream:
        async def openai_sse_stream():
            try:
                async with http_client.stream(
                    "POST", f"{url}/v1/chat/completions", json=body, timeout=120.0
                ) as response:
                    if response.status_code == 200:
                        async for chunk in response.aiter_bytes():
                            yield chunk
                    else:
                        err_content = await response.aread()
                        log(f"Worker {role} /v1/chat/completions returned {response.status_code}: {err_content.decode('utf-8', errors='ignore')}")
            except Exception as e:
                SERVER_STATE[role]["errors"] += 1
                STATS["failed_requests"] += 1
                log(f"Stream error on {role} (/v1/chat/completions): {e}")
            finally:
                elapsed = round((time.perf_counter() - req_start) * 1000, 1)
                SmartScheduler.finish_request(role, elapsed)

        return StreamingResponse(
            openai_sse_stream(),
            media_type="text/event-stream",
            headers={"X-Server": role, "X-Model": model}
        )

    try:
        response = await http_client.post(f"{url}/v1/chat/completions", json=body, timeout=120.0)
        elapsed = round((time.perf_counter() - req_start) * 1000, 1)
        SmartScheduler.finish_request(role, elapsed)

        if response.status_code == 200:
            data = response.json()
            return JSONResponse(
                content=data,
                headers={"X-Server": role, "X-Model": model}
            )

        chat_body = {
            "model": model,
            "messages": body.get("messages", []),
            "stream": False,
            "options": {
                "temperature": body.get("temperature", 0.3),
                "num_predict": body.get("max_tokens", 400),
                "top_p": body.get("top_p", 0.8),
            }
        }
        fallback_resp = await http_client.post(f"{url}/api/chat", json=chat_body, timeout=120.0)
        fb_data = fallback_resp.json()
        translated = {
            "id": f"chatcmpl-{secrets.token_hex(8)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": fb_data.get("message", {"role": "assistant", "content": ""}),
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": fb_data.get("prompt_eval_count", 0),
                "completion_tokens": fb_data.get("eval_count", 0),
                "total_tokens": fb_data.get("prompt_eval_count", 0) + fb_data.get("eval_count", 0)
            }
        }
        return JSONResponse(
            content=translated,
            headers={"X-Server": role, "X-Model": model}
        )

    except Exception as e:
        SERVER_STATE[role]["errors"] += 1
        STATS["failed_requests"] += 1
        SmartScheduler.finish_request(role)
        log(f"OpenAI chat error on {role}: {e}")
        return JSONResponse(
            {"error": {"message": str(e), "type": "server_error"}},
            status_code=500
        )


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

@app.get("/api/usage")
async def api_usage():
    return JSONResponse({"status": "ok", "usage": "unlimited"})


###############################################################################
# ENTRY POINT
###############################################################################

if __name__ == "__main__":
    import uvicorn
    use_ssl = "--ssl" in sys.argv
    ssl_kwargs = {}

    cert_path = BASE_DIR / "cert.pem" if (BASE_DIR / "cert.pem").exists() else (ROOT_DIR / "cert.pem")
    key_path = BASE_DIR / "key.pem" if (BASE_DIR / "key.pem").exists() else (ROOT_DIR / "key.pem")

    if use_ssl and cert_path.exists() and key_path.exists():
        ssl_kwargs = {
            "ssl_keyfile": str(key_path),
            "ssl_certfile": str(cert_path)
        }
        log(f"Running with SSL (HTTPS) [cert={cert_path.name}]")
    else:
        log("Running in standard HTTP mode (no self-signed certificate errors)")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
        **ssl_kwargs
    )