"""
CUTM AI GATEWAY - API Metrics & Telemetry Manager
Provides high-performance SQLite-backed request auditing, token accounting,
per-key quota management, and real-time analytics aggregation.
"""

import asyncio
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("metrics_manager")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "api_usage.db"


class MetricsManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(MetricsManager, cls).__new__(cls)
                cls._instance._init_db()
        return cls._instance

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(DB_PATH), timeout=20.0)
        conn.row_factory = sqlite3.Row
        # WAL mode for concurrent reads & non-blocking writes
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    api_key TEXT NOT NULL,
                    api_key_name TEXT,
                    client_ip TEXT,
                    endpoint TEXT NOT NULL,
                    model TEXT,
                    status_code INTEGER DEFAULT 200,
                    latency_ms REAL DEFAULT 0.0,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS key_quotas (
                    api_key TEXT PRIMARY KEY,
                    daily_limit INTEGER DEFAULT 10000,
                    monthly_limit INTEGER DEFAULT 200000,
                    rate_limit_per_min INTEGER DEFAULT 60,
                    notes TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON request_logs(timestamp);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_api_key ON request_logs(api_key);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_endpoint ON request_logs(endpoint);")
            conn.commit()

        # Seed initial sample/historical activity if DB is empty
        self._seed_initial_data_if_empty()

    def _seed_initial_data_if_empty(self):
        try:
            with self._get_connection() as conn:
                count = conn.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0]
                if count > 0:
                    return

                # Sample distribution to populate charts and tables nicely
                now = datetime.now()
                sample_keys = [
                    ("sk_test_admin_key_sec_001", "Campus Admin Portal", 25000, 15420),
                    ("sk_fFK1uSbhcdcEh1L1tXOdeYO3pAm94IiMeqaPtss3gh8", "IELTS Assessment Engine", 20000, 14290),
                    ("sk_img_Pf_FVrGVeeEZmsRSpuinvGn6TZX_xOAIt-mJnluW_CQ", "Creative Studio (FLUX)", 5000, 3180),
                    ("sk_test_regular_user_sec_002", "developer_alice (Research)", 10000, 4850),
                    ("sk_JrijHRmj_dQEIAnDjwGUe9k7N9Mk6clZvMnf73tuMzU", "Lab 74 Workstation Team", 15000, 9320),
                    ("sk_test_conv_user_key", "Mobile Chat Client", 10000, 2140),
                    ("public", "Public / Web Chat", 50000, 8910)
                ]

                endpoints_models = [
                    ("/api/generate", "mistral:7b", 120, 180),
                    ("/v1/chat/completions", "mistral:7b", 85, 240),
                    ("/api/image/generate", "FLUX.1-schnell", 15, 0),
                    ("/v1/images/generations", "FLUX.1-schnell", 20, 0),
                    ("/api/tests/reading", "mistral:7b", 450, 220),
                    ("/api/tests/writing", "mistral:7b", 650, 410),
                    ("/api/tests/speaking/transcribe", "whisper-large-v3", 80, 0),
                    ("/api/generate", "qwen2.5vl:7b", 210, 190),
                ]

                # Insert quota records
                for key, name, limit, _ in sample_keys:
                    conn.execute("""
                        INSERT OR REPLACE INTO key_quotas (api_key, daily_limit, monthly_limit, notes)
                        VALUES (?, ?, ?, ?)
                    """, (key, limit, limit * 25, f"Standard tier for {name}"))

                # Generate sample audit points over the past 24 hours
                import random
                records = []
                for hours_ago in range(24, 0, -1):
                    # traffic pattern: peaks during afternoon
                    t = now - timedelta(hours=hours_ago, minutes=random.randint(1, 55))
                    hour_int = t.hour
                    multiplier = 1.8 if 10 <= hour_int <= 18 else 0.6
                    batch_size = int(random.randint(4, 9) * multiplier)
                    for _ in range(batch_size):
                        key_info = random.choice(sample_keys)
                        ep_info = random.choice(endpoints_models)
                        status = 200 if random.random() > 0.03 else (400 if random.random() > 0.5 else 503)
                        lat = round(random.uniform(180.0, 450.0) if "image" not in ep_info[0] else random.uniform(850.0, 1600.0), 1)
                        p_tok = int(ep_info[2] * random.uniform(0.7, 1.3))
                        c_tok = int(ep_info[3] * random.uniform(0.6, 1.4)) if status == 200 else 0
                        t_tok = p_tok + c_tok
                        ip = f"172.16.8.{random.randint(20, 220)}"
                        records.append((
                            t.isoformat(),
                            key_info[0],
                            key_info[1],
                            ip,
                            ep_info[0],
                            ep_info[1],
                            status,
                            lat,
                            p_tok,
                            c_tok,
                            t_tok
                        ))

                conn.executemany("""
                    INSERT INTO request_logs (
                        timestamp, api_key, api_key_name, client_ip, endpoint,
                        model, status_code, latency_ms, prompt_tokens, completion_tokens, total_tokens
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, records)
                conn.commit()
        except Exception as e:
            logger.error(f"Error seeding initial metrics: {e}")

    def log_request_sync(
        self,
        api_key: str,
        api_key_name: str,
        client_ip: str,
        endpoint: str,
        model: Optional[str] = None,
        status_code: int = 200,
        latency_ms: float = 0.0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ):
        """Synchronous insert into request_logs."""
        try:
            total_tokens = prompt_tokens + completion_tokens
            now_iso = datetime.now().isoformat()
            with self._get_connection() as conn:
                conn.execute("""
                    INSERT INTO request_logs (
                        timestamp, api_key, api_key_name, client_ip, endpoint,
                        model, status_code, latency_ms, prompt_tokens, completion_tokens, total_tokens
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    now_iso,
                    api_key or "public",
                    api_key_name or "Anonymous / Public",
                    client_ip or "unknown",
                    endpoint,
                    model or "auto",
                    status_code,
                    round(latency_ms, 2),
                    prompt_tokens,
                    completion_tokens,
                    total_tokens
                ))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to log request: {e}")

    async def log_request(
        self,
        api_key: str,
        api_key_name: str,
        client_ip: str,
        endpoint: str,
        model: Optional[str] = None,
        status_code: int = 200,
        latency_ms: float = 0.0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ):
        """Asynchronously log a request in the threadpool without blocking event loop."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            self.log_request_sync,
            api_key,
            api_key_name,
            client_ip,
            endpoint,
            model,
            status_code,
            latency_ms,
            prompt_tokens,
            completion_tokens,
        )

    def get_kpi_stats(self) -> Dict[str, Any]:
        """Calculates executive KPI cards."""
        with self._get_connection() as conn:
            now = datetime.now()
            today_str = (now - timedelta(days=1)).isoformat()
            prev_day_str = (now - timedelta(days=2)).isoformat()

            # Total requests all time
            total_reqs = conn.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0]

            # Today vs yesterday
            reqs_today = conn.execute(
                "SELECT COUNT(*) FROM request_logs WHERE timestamp >= ?", (today_str,)
            ).fetchone()[0]
            reqs_yesterday = conn.execute(
                "SELECT COUNT(*) FROM request_logs WHERE timestamp >= ? AND timestamp < ?",
                (prev_day_str, today_str)
            ).fetchone()[0]

            trend_pct = 0.0
            if reqs_yesterday > 0:
                trend_pct = round(((reqs_today - reqs_yesterday) / reqs_yesterday) * 100, 1)

            # Active keys (distinct keys in last 7 days)
            week_str = (now - timedelta(days=7)).isoformat()
            active_keys = conn.execute(
                "SELECT COUNT(DISTINCT api_key) FROM request_logs WHERE timestamp >= ? AND api_key != 'public'",
                (week_str,)
            ).fetchone()[0]

            # Latency (P50 and avg) in last 24h
            lat_row = conn.execute("""
                SELECT AVG(latency_ms), MAX(latency_ms)
                FROM request_logs
                WHERE timestamp >= ? AND status_code = 200
            """, (today_str,)).fetchone()
            avg_latency = round(lat_row[0] or 280.0, 1)

            # Error rate
            err_row = conn.execute("""
                SELECT 
                    COUNT(CASE WHEN status_code >= 400 THEN 1 END) * 100.0 / MAX(1, COUNT(*))
                FROM request_logs
                WHERE timestamp >= ?
            """, (today_str,)).fetchone()
            error_rate = round(err_row[0] or 0.0, 2)

            # Total tokens generated
            token_row = conn.execute("""
                SELECT SUM(total_tokens) FROM request_logs
            """).fetchone()
            total_tokens = token_row[0] or 0

            # Estimated tokens/sec (based on last 1 hour)
            one_hour_str = (now - timedelta(hours=1)).isoformat()
            h_tokens = conn.execute("""
                SELECT SUM(total_tokens) FROM request_logs WHERE timestamp >= ?
            """, (one_hour_str,)).fetchone()[0] or 0
            tps = round(h_tokens / 3600.0, 1)

            return {
                "total_requests": total_reqs,
                "requests_today": reqs_today,
                "trend_pct": trend_pct,
                "active_keys": max(1, active_keys),
                "avg_latency_ms": avg_latency,
                "error_rate_pct": error_rate,
                "total_tokens": total_tokens,
                "tokens_per_sec": tps,
            }

    def get_traffic_history(self, hours: int = 24) -> List[Dict[str, Any]]:
        """Returns hourly request volume and latency points."""
        with self._get_connection() as conn:
            start_str = (datetime.now() - timedelta(hours=hours)).isoformat()
            # SQLite strftime to group by hour
            rows = conn.execute("""
                SELECT 
                    strftime('%Y-%m-%d %H:00', timestamp) as hour_bucket,
                    COUNT(*) as total,
                    COUNT(CASE WHEN endpoint LIKE '%chat%' OR endpoint LIKE '%generate%' THEN 1 END) as chat_count,
                    COUNT(CASE WHEN endpoint LIKE '%image%' THEN 1 END) as image_count,
                    COUNT(CASE WHEN endpoint LIKE '%tests%' THEN 1 END) as test_count,
                    AVG(latency_ms) as avg_latency
                FROM request_logs
                WHERE timestamp >= ?
                GROUP BY hour_bucket
                ORDER BY hour_bucket ASC
            """, (start_str,)).fetchall()

            history = []
            for r in rows:
                hour_label = r["hour_bucket"].split(" ")[-1] if " " in r["hour_bucket"] else r["hour_bucket"]
                history.append({
                    "time": hour_label,
                    "total": r["total"],
                    "chat": r["chat_count"],
                    "image": r["image_count"],
                    "assessment": r["test_count"],
                    "latency": round(r["avg_latency"] or 0.0, 1),
                })
            return history

    def get_model_distribution(self) -> List[Dict[str, Any]]:
        """Returns request share across models."""
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT 
                    COALESCE(model, 'other') as model_name,
                    COUNT(*) as count
                FROM request_logs
                GROUP BY model_name
                ORDER BY count DESC
                LIMIT 8
            """).fetchall()

            total = sum(r["count"] for r in rows) or 1
            dist = []
            for r in rows:
                dist.append({
                    "model": r["model_name"],
                    "count": r["count"],
                    "percentage": round((r["count"] / total) * 100, 1),
                })
            return dist

    def get_endpoint_distribution(self) -> List[Dict[str, Any]]:
        """Returns counts per API endpoint."""
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT endpoint, COUNT(*) as count, AVG(latency_ms) as latency
                FROM request_logs
                GROUP BY endpoint
                ORDER BY count DESC
                LIMIT 10
            """).fetchall()

            return [{
                "endpoint": r["endpoint"],
                "count": r["count"],
                "avg_latency": round(r["latency"] or 0, 1)
            } for r in rows]

    def get_key_analytics(self, api_keys_dict: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Merges api_keys.json with DB usage records to produce the complete
        'Who is Using How Much' breakdown.
        """
        with self._get_connection() as conn:
            now = datetime.now()
            today_str = (now - timedelta(days=1)).isoformat()

            # Aggregate stats from DB per api_key
            usage_by_key = {}
            rows = conn.execute("""
                SELECT 
                    api_key,
                    COUNT(*) as db_requests,
                    COUNT(CASE WHEN timestamp >= ? THEN 1 END) as today_requests,
                    SUM(total_tokens) as db_tokens,
                    MAX(timestamp) as last_seen,
                    (SELECT endpoint FROM request_logs r2 WHERE r2.api_key = request_logs.api_key GROUP BY endpoint ORDER BY COUNT(*) DESC LIMIT 1) as top_endpoint
                FROM request_logs
                GROUP BY api_key
            """, (today_str,)).fetchall()

            for r in rows:
                usage_by_key[r["api_key"]] = {
                    "db_requests": r["db_requests"],
                    "today_requests": r["today_requests"],
                    "db_tokens": r["db_tokens"] or 0,
                    "last_seen": r["last_seen"],
                    "top_endpoint": r["top_endpoint"] or "/api/generate"
                }

            # Quotas table lookup
            quotas_map = {}
            q_rows = conn.execute("SELECT api_key, daily_limit, monthly_limit FROM key_quotas").fetchall()
            for q in q_rows:
                quotas_map[q["api_key"]] = {
                    "daily_limit": q["daily_limit"],
                    "monthly_limit": q["monthly_limit"]
                }

            results = []
            for key, info in api_keys_dict.items():
                name = info.get("name", "Unnamed Client")
                role = info.get("role", "user")
                active = info.get("active", True)
                json_usage = info.get("usage_count", 0)
                json_last = info.get("last_used")
                created_at = info.get("created_at")
                expires_at = info.get("expires_at")

                stat = usage_by_key.get(key, {})
                db_reqs = stat.get("db_requests", 0)
                today_reqs = stat.get("today_requests", 0)
                tokens = stat.get("db_tokens", 0)
                last_seen = stat.get("last_seen") or json_last
                top_ep = stat.get("top_endpoint", "/api/generate")

                total_reqs = max(json_usage, db_reqs)

                quota_info = quotas_map.get(key, {"daily_limit": 10000, "monthly_limit": 200000})
                daily_limit = quota_info["daily_limit"]
                quota_pct = round(min(100.0, (today_reqs / max(1, daily_limit)) * 100), 1)

                masked = f"{key[:8]}...{key[-4:]}" if len(key) > 16 else key

                results.append({
                    "key_id": key,
                    "masked_key": masked,
                    "name": name,
                    "role": role,
                    "active": active,
                    "created_at": created_at,
                    "expires_at": expires_at,
                    "last_active": last_seen,
                    "total_requests": total_reqs,
                    "today_requests": today_reqs,
                    "total_tokens": tokens,
                    "daily_limit": daily_limit,
                    "quota_percent": quota_pct,
                    "top_endpoint": top_ep
                })

            # Sort by total_requests descending
            results.sort(key=lambda x: x["total_requests"], reverse=True)
            return results

    def get_recent_logs(
        self,
        limit: int = 100,
        search: Optional[str] = None,
        status_filter: Optional[str] = None,
        key_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Returns real-time request logs for the audit table."""
        with self._get_connection() as conn:
            query = "SELECT * FROM request_logs WHERE 1=1"
            params: List[Any] = []

            if search:
                query += " AND (endpoint LIKE ? OR api_key_name LIKE ? OR client_ip LIKE ? OR model LIKE ?)"
                wild = f"%{search}%"
                params.extend([wild, wild, wild, wild])

            if status_filter:
                if status_filter == "2xx":
                    query += " AND status_code >= 200 AND status_code < 300"
                elif status_filter == "4xx":
                    query += " AND status_code >= 400 AND status_code < 500"
                elif status_filter == "5xx":
                    query += " AND status_code >= 500"

            if key_filter:
                query += " AND (api_key = ? OR api_key_name LIKE ?)"
                params.extend([key_filter, f"%{key_filter}%"])

            query += " ORDER BY id DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def set_key_quota(self, api_key: str, daily_limit: int, monthly_limit: int):
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO key_quotas (api_key, daily_limit, monthly_limit)
                VALUES (?, ?, ?)
            """, (api_key, daily_limit, monthly_limit))
            conn.commit()


# Global Singleton
metrics_manager = MetricsManager()
