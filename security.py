"""
security.py — Phase 8: Security Engine, Audit Logging & Credential Hashing
==========================================================================
Provides:
- Append-only MySQL Audit Logging table (`audit_log`) and asynchronous event logger
- Parameterized audit querying with safe pagination and filters
- Constant-time API Key hashing (SHA-256) and display masking
- Centralized security configuration and helper utilities
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

import aiomysql
import rate_limiter

# Security Configuration Constants
MAX_REQUEST_BODY_BYTES = int(os.environ.get("MAX_REQUEST_BODY_BYTES", 10 * 1024 * 1024))  # 10 MB
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:8000,http://127.0.0.1:8000,http://localhost:3000,http://127.0.0.1:3000"
    ).split(",")
    if origin.strip()
]
AUDIT_LOG_RETENTION_DAYS = int(os.environ.get("AUDIT_LOG_RETENTION_DAYS", 90))


# ==============================================================================
# 1. API KEY CRYPTOGRAPHY & MASKING
# ==============================================================================

def hash_key(raw_key: str) -> str:
    """Computes a constant SHA-256 hash of an API key for storage."""
    return hashlib.sha256(raw_key.strip().encode("utf-8")).hexdigest()


def verify_key(raw_key: str, stored_hash: str) -> bool:
    """Verifies a raw API key against a stored SHA-256 hash using constant-time comparison."""
    if not raw_key or not stored_hash:
        return False
    candidate_hash = hash_key(raw_key)
    return secrets.compare_digest(candidate_hash, stored_hash)


def mask_key(raw_key: str) -> str:
    """
    Returns a safe masked representation for display/logging.
    Example: 'sk_live_abc123...4xyz' -> 'sk_live_••••••••••••4xyz'
    """
    if not raw_key:
        return "••••"
    key = raw_key.strip()
    if len(key) <= 8:
        return f"{key[:2]}••••"
    prefix = key[:7]
    suffix = key[-4:]
    return f"{prefix}••••••••••••{suffix}"


# ==============================================================================
# 2. AUDIT LOG DATABASE SCHEMA & INITIALIZATION
# ==============================================================================

async def init_audit_db() -> None:
    """
    Initializes the `audit_log` table in MySQL if it doesn't already exist.
    Thread-safe and idempotent.
    """
    pool = await rate_limiter.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    event_id VARCHAR(64) UNIQUE NOT NULL,
                    timestamp DOUBLE NOT NULL,
                    actor_type VARCHAR(32) NOT NULL,
                    actor_id VARCHAR(128) NOT NULL,
                    action VARCHAR(64) NOT NULL,
                    resource_type VARCHAR(64) NOT NULL,
                    resource_id VARCHAR(128),
                    result VARCHAR(32) NOT NULL,
                    ip_address VARCHAR(45) NOT NULL,
                    user_agent VARCHAR(255),
                    request_id VARCHAR(64) NOT NULL,
                    metadata JSON,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_audit_timestamp (timestamp),
                    INDEX idx_audit_actor (actor_id),
                    INDEX idx_audit_action (action),
                    INDEX idx_audit_result (result)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)


# ==============================================================================
# 3. AUDIT LOG EVENT LOGGING
# ==============================================================================

async def log_audit_event(
    actor_type: str,
    actor_id: str,
    action: str,
    resource_type: str,
    result: str,
    ip_address: str,
    request_id: str,
    resource_id: Optional[str] = None,
    user_agent: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Appends an immutable security audit record to the MySQL `audit_log` table.
    Never logs raw secrets.
    """
    event_id = f"aud_{secrets.token_hex(16)}"
    now_epoch = time.time()
    clean_metadata = metadata or {}
    meta_json = json.dumps(clean_metadata)

    try:
        pool = await rate_limiter.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO audit_log (
                        event_id, timestamp, actor_type, actor_id, action,
                        resource_type, resource_id, result, ip_address,
                        user_agent, request_id, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        event_id,
                        now_epoch,
                        actor_type,
                        actor_id,
                        action,
                        resource_type,
                        resource_id or "",
                        result,
                        ip_address,
                        (user_agent or "")[:255],
                        request_id,
                        meta_json,
                    ),
                )
    except Exception as e:
        # Prevent audit failures from crashing the server, but log diagnostic to stderr
        print(f"[AUDIT LOGGING ERROR] Failed to record audit event {action}: {e}")

    return event_id


# ==============================================================================
# 4. AUDIT LOG QUERYING & PAGINATION (ADMIN ONLY)
# ==============================================================================

async def get_audit_logs(
    action: Optional[str] = None,
    actor_id: Optional[str] = None,
    result: Optional[str] = None,
    resource_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Queries paginated audit logs using safe parameterized filters.
    """
    safe_limit = max(1, min(100, int(limit)))
    safe_offset = max(0, int(offset))

    where_clauses = ["1=1"]
    params: List[Any] = []

    if action:
        where_clauses.append("action = %s")
        params.append(action.strip())
    if actor_id:
        where_clauses.append("actor_id LIKE %s")
        params.append(f"%{actor_id.strip()}%")
    if result:
        where_clauses.append("result = %s")
        params.append(result.strip().lower())
    if resource_type:
        where_clauses.append("resource_type = %s")
        params.append(resource_type.strip())

    where_sql = " AND ".join(where_clauses)
    pool = await rate_limiter.get_pool()

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            # Get total matching count
            count_query = f"SELECT COUNT(*) FROM audit_log WHERE {where_sql}"
            await cur.execute(count_query, params)
            total_count = (await cur.fetchone())[0] or 0

            # Get paginated records
            query = f"""
                SELECT
                    event_id, timestamp, actor_type, actor_id, action,
                    resource_type, resource_id, result, ip_address,
                    user_agent, request_id, metadata, created_at
                FROM audit_log
                WHERE {where_sql}
                ORDER BY timestamp DESC
                LIMIT %s OFFSET %s
            """
            exec_params = params + [safe_limit, safe_offset]
            await cur.execute(query, exec_params)
            rows = await cur.fetchall()

    logs = []
    for r in rows:
        meta_val = r[11]
        if isinstance(meta_val, str):
            try:
                meta_val = json.loads(meta_val)
            except Exception:
                pass

        logs.append({
            "event_id": r[0],
            "timestamp": r[1],
            "actor_type": r[2],
            "actor_id": r[3],
            "action": r[4],
            "resource_type": r[5],
            "resource_id": r[6],
            "result": r[7],
            "ip_address": r[8],
            "user_agent": r[9],
            "request_id": r[10],
            "metadata": meta_val or {},
            "created_at": r[12].isoformat() if hasattr(r[12], "isoformat") else str(r[12]),
        })

    return {
        "total": total_count,
        "limit": safe_limit,
        "offset": safe_offset,
        "logs": logs,
    }
