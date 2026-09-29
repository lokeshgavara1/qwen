"""
rate_limiter.py — Phase 1 & 2: Rate Limiting & Storage Layer (MySQL Backend)
=============================================================================
Async MySQL-backed connection pool with sliding-window rate limiter.

Limits (configurable via RATE_LIMIT_CONFIG):
  Public / no key:
    - burst:  10 req / 60 s
    - hourly: 20 req / hour

  Authenticated API key:
    - burst:  10 req / 60 s
    - hourly: 200 req / hour  (overridable per-key via admin endpoint)

Identity:
  - API key present → identity = "key:<api_key_name>" (never stores plaintext key in logs)
  - No key          → identity = "ip:<client_ip>"

MySQL connection pooling with aiomysql.
Records older than 7 days are cleaned up hourly via background task.
"""

import asyncio
import hashlib
import os
import time
from typing import Optional

import aiomysql

###############################################################################
# CONFIGURATION — change these to adjust default limits globally
###############################################################################

RATE_LIMIT_CONFIG = {
    "public": {
        "burst_limit":  10,    # max requests per burst_window_sec
        "burst_window": 60,    # seconds
        "hourly_quota": 20,    # max requests per hour
    },
    "authenticated": {
        "burst_limit":  10,
        "burst_window": 60,
        "hourly_quota": 200,
    },
}

MYSQL_CONFIG = {
    "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("MYSQL_PORT", 3306)),
    "user": os.environ.get("MYSQL_USER", "root"),
    "password": os.environ.get("MYSQL_PASSWORD", "cutmap"),
    "db": os.environ.get("MYSQL_DATABASE", "ai_gateway"),
    "minsize": 2,
    "maxsize": 20,
    "autocommit": True,
}

CLEANUP_INTERVAL_SEC = 3600        # run cleanup every hour
RECORD_RETENTION_DAYS = 7          # keep records for 7 days

# Global connection pool
_pool: Optional[aiomysql.Pool] = None
_pool_lock = asyncio.Lock()


###############################################################################
# IDENTITY HELPERS
###############################################################################

def make_identity(api_key_name: Optional[str], client_ip: str) -> str:
    """
    Builds a stable, opaque identity string for rate-limit tracking.
    Uses key name (not the secret) when a key is authenticated, so
    no sensitive credentials are stored in the DB.
    """
    if api_key_name:
        safe = hashlib.sha256(api_key_name.encode()).hexdigest()[:16]
        return f"key:{safe}"
    return f"ip:{client_ip}"


def is_authenticated(identity: str) -> bool:
    return identity.startswith("key:")


###############################################################################
# CONNECTION POOL MANAGEMENT
###############################################################################

async def get_pool() -> aiomysql.Pool:
    """Returns the active MySQL connection pool, initializing it if needed for the current event loop."""
    global _pool
    current_loop = asyncio.get_running_loop()
    pool_loop = getattr(_pool, "_loop", None) if _pool is not None else None

    if (
        _pool is None
        or _pool._closed
        or pool_loop is not current_loop
        or (pool_loop is not None and pool_loop.is_closed())
    ):
        async with _pool_lock:
            pool_loop = getattr(_pool, "_loop", None) if _pool is not None else None
            if (
                _pool is None
                or _pool._closed
                or pool_loop is not current_loop
                or (pool_loop is not None and pool_loop.is_closed())
            ):
                _pool = await aiomysql.create_pool(
                    host=MYSQL_CONFIG["host"],
                    port=MYSQL_CONFIG["port"],
                    user=MYSQL_CONFIG["user"],
                    password=MYSQL_CONFIG["password"],
                    db=MYSQL_CONFIG["db"],
                    minsize=MYSQL_CONFIG["minsize"],
                    maxsize=MYSQL_CONFIG["maxsize"],
                    autocommit=MYSQL_CONFIG["autocommit"],
                    loop=current_loop,
                )
    return _pool


async def close_pool():
    """Gracefully closes the MySQL connection pool."""
    global _pool
    if _pool is not None:
        try:
            if not _pool._closed:
                _pool.close()
                loop = getattr(_pool, "_loop", None)
                if loop is not None and not loop.is_closed():
                    await _pool.wait_closed()
        except Exception:
            pass
        finally:
            _pool = None


###############################################################################
# DATABASE INIT & MIGRATION
###############################################################################

async def init_db():
    """
    Ensures MySQL database and tables exist.
    Also migrates legacy SQLite records from gateway_usage.db if found.
    Idempotent and safe to run on startup.
    """
    current_loop = asyncio.get_running_loop()
    # 1. Connect without db selected to ensure database exists
    conn = await aiomysql.connect(
        host=MYSQL_CONFIG["host"],
        port=MYSQL_CONFIG["port"],
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"],
        loop=current_loop,
    )
    async with conn.cursor() as cur:
        await cur.execute(f"CREATE DATABASE IF NOT EXISTS `{MYSQL_CONFIG['db']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
    conn.close()

    # 2. Get connection pool for the target database
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            # Create request_log table
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS request_log (
                    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
                    identity    VARCHAR(64) NOT NULL,
                    model       VARCHAR(64) NULL,
                    status      VARCHAR(32) NOT NULL DEFAULT 'ok',
                    tokens      INT NULL,
                    latency_ms  DOUBLE NULL,
                    intent      VARCHAR(32) NULL,
                    created_at  DOUBLE NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            # Create indexes if they do not exist
            # MySQL handles indexes via helper checking information_schema
            indexes = [
                ("idx_rl_identity_time", "request_log", "(identity, created_at)"),
                ("idx_rl_created_at", "request_log", "(created_at)"),
                ("idx_rl_model_time", "request_log", "(model, created_at)"),
                ("idx_rl_status", "request_log", "(status)"),
            ]
            for idx_name, tbl, cols in indexes:
                await cur.execute(f"""
                    SELECT COUNT(1) FROM information_schema.statistics
                    WHERE table_schema = '{MYSQL_CONFIG['db']}' AND table_name = '{tbl}' AND index_name = '{idx_name}';
                """)
                exists = (await cur.fetchone())[0]
                if not exists:
                    await cur.execute(f"CREATE INDEX {idx_name} ON {tbl} {cols};")

            # Create key_quotas table
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS key_quotas (
                    identity        VARCHAR(64) PRIMARY KEY,
                    hourly_quota    INT NOT NULL,
                    burst_limit     INT NOT NULL,
                    burst_window    INT NOT NULL,
                    updated_at      DOUBLE NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

    # 3. Migrate from SQLite if gateway_usage.db exists and MySQL is empty
    await _migrate_from_sqlite_if_needed()


async def _migrate_from_sqlite_if_needed():
    """One-time migration of existing records from gateway_usage.db to MySQL."""
    sqlite_db = "gateway_usage.db"
    if not os.path.exists(sqlite_db):
        return

    try:
        import aiosqlite
        pool = await get_pool()
        async with pool.acquire() as m_conn:
            async with m_conn.cursor() as m_cur:
                await m_cur.execute("SELECT COUNT(*) FROM request_log;")
                m_count = (await m_cur.fetchone())[0]
                if m_count > 0:
                    return  # already populated

                async with aiosqlite.connect(sqlite_db) as s_db:
                    async with s_db.execute("SELECT identity, model, status, tokens, latency_ms, intent, created_at FROM request_log") as s_cur:
                        rows = await s_cur.fetchall()
                        if rows:
                            await m_cur.executemany(
                                """INSERT INTO request_log (identity, model, status, tokens, latency_ms, intent, created_at)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                                rows
                            )
                            print(f"[MIGRATION] Migrated {len(rows)} records from SQLite to MySQL ai_gateway.", flush=True)

                    async with s_db.execute("SELECT identity, hourly_quota, burst_limit, burst_window, updated_at FROM key_quotas") as s_cur:
                        q_rows = await s_cur.fetchall()
                        if q_rows:
                            await m_cur.executemany(
                                """INSERT INTO key_quotas (identity, hourly_quota, burst_limit, burst_window, updated_at)
                                   VALUES (%s, %s, %s, %s, %s)
                                   ON DUPLICATE KEY UPDATE
                                     hourly_quota=VALUES(hourly_quota),
                                     burst_limit=VALUES(burst_limit),
                                     burst_window=VALUES(burst_window),
                                     updated_at=VALUES(updated_at)""",
                                q_rows
                            )
    except Exception as e:
        print(f"[MIGRATION WARNING] Failed to migrate from SQLite: {e}", flush=True)


###############################################################################
# PER-KEY QUOTA LOOKUP
###############################################################################

async def get_limits(identity: str):
    """
    Returns (burst_limit, burst_window, hourly_quota) for the identity.
    Custom quotas stored in key_quotas take precedence; otherwise
    defaults from RATE_LIMIT_CONFIG are used.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT burst_limit, burst_window, hourly_quota FROM key_quotas WHERE identity = %s",
                (identity,)
            )
            row = await cur.fetchone()

    if row:
        return row[0], row[1], row[2]

    tier = "authenticated" if is_authenticated(identity) else "public"
    cfg = RATE_LIMIT_CONFIG[tier]
    return cfg["burst_limit"], cfg["burst_window"], cfg["hourly_quota"]


###############################################################################
# RATE LIMIT CHECK (sliding-window counter)
###############################################################################

async def check_rate_limit(identity: str) -> dict:
    """
    Checks burst and hourly windows for the given identity.

    Returns a dict:
        {
          "allowed": bool,
          "limit_type": None | "burst" | "hourly",
          "retry_after": int (seconds),
          "burst_used": int, "burst_limit": int,
          "hourly_used": int, "hourly_quota": int,
        }
    """
    burst_limit, burst_window, hourly_quota = await get_limits(identity)
    now = time.time()
    burst_since = now - burst_window
    hour_since  = now - 3600

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            # Count recent requests in burst window
            await cur.execute(
                """SELECT COUNT(*) FROM request_log
                   WHERE identity = %s AND created_at >= %s AND status != 'rate_limited'""",
                (identity, burst_since)
            )
            burst_used = (await cur.fetchone())[0]

            # Count recent requests in hourly window
            await cur.execute(
                """SELECT COUNT(*) FROM request_log
                   WHERE identity = %s AND created_at >= %s AND status != 'rate_limited'""",
                (identity, hour_since)
            )
            hourly_used = (await cur.fetchone())[0]

            # Check burst first (shorter window)
            if burst_used >= burst_limit:
                await cur.execute(
                    """SELECT MIN(created_at) FROM request_log
                       WHERE identity = %s AND created_at >= %s AND status != 'rate_limited'""",
                    (identity, burst_since)
                )
                oldest = (await cur.fetchone())[0] or now
                retry_after = max(1, int(oldest + burst_window - now) + 1)
                return {
                    "allowed": False,
                    "limit_type": "burst",
                    "retry_after": retry_after,
                    "burst_used": burst_used,
                    "burst_limit": burst_limit,
                    "hourly_used": hourly_used,
                    "hourly_quota": hourly_quota,
                }

            # Check hourly quota
            if hourly_used >= hourly_quota:
                await cur.execute(
                    """SELECT MIN(created_at) FROM request_log
                       WHERE identity = %s AND created_at >= %s AND status != 'rate_limited'""",
                    (identity, hour_since)
                )
                oldest = (await cur.fetchone())[0] or now
                retry_after = max(1, int(oldest + 3600 - now) + 1)
                return {
                    "allowed": False,
                    "limit_type": "hourly",
                    "retry_after": retry_after,
                    "burst_used": burst_used,
                    "burst_limit": burst_limit,
                    "hourly_used": hourly_used,
                    "hourly_quota": hourly_quota,
                }

    return {
        "allowed": True,
        "limit_type": None,
        "retry_after": 0,
        "burst_used": burst_used,
        "burst_limit": burst_limit,
        "hourly_used": hourly_used,
        "hourly_quota": hourly_quota,
    }


###############################################################################
# RECORD REQUEST
###############################################################################

async def record_request(
    identity: str,
    model: str = None,
    status: str = "ok",
    tokens: int = None,
    latency_ms: float = None,
    intent: str = None,
):
    """
    Persists a single request record for usage tracking.
    Gracefully handles failures so that tracking never breaks generation.
    status values: 'ok', 'error', 'rate_limited'
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO request_log
                       (identity, model, status, tokens, latency_ms, intent, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (identity, model, status, tokens, latency_ms, intent, time.time())
                )
    except Exception as e:
        pass  # never fail a request because of logging


###############################################################################
# USAGE STATS
###############################################################################

async def get_usage_stats(identity: str) -> dict:
    """
    Returns current-window usage statistics for the given identity.
    Includes both structured and flat properties for flexible consumer usage.
    """
    burst_limit, burst_window, hourly_quota = await get_limits(identity)
    now = time.time()
    burst_since = now - burst_window
    hour_since  = now - 3600

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT COUNT(*) FROM request_log
                   WHERE identity = %s AND created_at >= %s AND status != 'rate_limited'""",
                (identity, burst_since)
            )
            burst_used = (await cur.fetchone())[0]

            await cur.execute(
                """SELECT COUNT(*) FROM request_log
                   WHERE identity = %s AND created_at >= %s AND status != 'rate_limited'""",
                (identity, hour_since)
            )
            hourly_used = (await cur.fetchone())[0]

            await cur.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM request_log WHERE identity = %s AND created_at >= %s",
                (identity, hour_since)
            )
            tokens_used = (await cur.fetchone())[0]

            await cur.execute(
                """SELECT COUNT(*) FROM request_log
                   WHERE identity = %s AND created_at >= %s AND status = 'ok'""",
                (identity, hour_since)
            )
            successful = (await cur.fetchone())[0]

            await cur.execute(
                "SELECT COUNT(*) FROM request_log WHERE identity = %s",
                (identity,)
            )
            total_all_time = (await cur.fetchone())[0]

    return {
        "identity": identity,
        "burst": {
            "used": burst_used,
            "limit": burst_limit,
            "remaining": max(0, burst_limit - burst_used),
            "window_sec": burst_window,
        },
        "hourly": {
            "used": hourly_used,
            "limit": hourly_quota,
            "remaining": max(0, hourly_quota - hourly_used),
        },
        "tokens": {
            "used_this_hour": int(tokens_used or 0),
        },
        "successful_this_hour": successful,
        # Flat convenience fields
        "requests_last_minute": burst_used,
        "requests_last_hour": hourly_used,
        "burst_limit": burst_limit,
        "hourly_limit": hourly_quota,
        "remaining_burst": max(0, burst_limit - burst_used),
        "remaining_hourly": max(0, hourly_quota - hourly_used),
        "total_tokens": int(tokens_used or 0),
        "total_requests_all_time": total_all_time,
    }


###############################################################################
# ADMIN: SET CUSTOM QUOTA
###############################################################################

async def set_quota(
    identity: str,
    hourly_quota: int,
    burst_limit: int,
    burst_window: int = 60,
) -> bool:
    """
    Persists a custom quota for a specific identity.
    Call only from admin-authorized endpoints.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO key_quotas (identity, hourly_quota, burst_limit, burst_window, updated_at)
                   VALUES (%s, %s, %s, %s, %s) AS new_q
                   ON DUPLICATE KEY UPDATE
                     hourly_quota = new_q.hourly_quota,
                     burst_limit  = new_q.burst_limit,
                     burst_window = new_q.burst_window,
                     updated_at   = new_q.updated_at""",
                (identity, hourly_quota, burst_limit, burst_window, time.time())
            )
    return True


###############################################################################
# CLEANUP
###############################################################################

async def cleanup_old_records(retention_days: int = RECORD_RETENTION_DAYS) -> int:
    """
    Deletes records older than retention_days.
    Runs as a periodic background task; failures are logged but never fatal.
    Returns the number of deleted records.
    """
    cutoff = time.time() - (retention_days * 86400)
    deleted = 0
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM request_log WHERE created_at < %s", (cutoff,)
                )
                deleted = cur.rowcount
        if deleted:
            from datetime import datetime
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [CLEANUP] Deleted {deleted} old rate-limit records from MySQL.", flush=True)
    except Exception as exc:
        print(f"[CLEANUP ERROR] {exc}", flush=True)
    return deleted


async def periodic_cleanup():
    """Background task: cleanup every CLEANUP_INTERVAL_SEC."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SEC)
        await cleanup_old_records()
