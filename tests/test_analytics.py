"""
test_analytics.py — Phase 2 Usage Analytics Comprehensive Test Suite (MySQL Backend)
===================================================================================
Tests analytics aggregations, time-series generation, model breakdowns, error tracking,
recent activity masking, user data isolation, and admin authorization.
"""

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
import httpx
import analytics
import qwen_lb
import rate_limiter

TEST_DB = "ai_gateway_test_analytics"


class TestAnalyticsService(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        rate_limiter.MYSQL_CONFIG["db"] = TEST_DB
        await rate_limiter.close_pool()
        await rate_limiter.init_db()

        # Clean test database table
        pool = await rate_limiter.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("TRUNCATE TABLE request_log;")
                await cur.execute("TRUNCATE TABLE key_quotas;")

    async def asyncTearDown(self):
        await rate_limiter.close_pool()

    async def test_01_summary_empty_database(self):
        """Verify summary returns valid zero-valued structure when no records exist."""
        summary = await analytics.get_summary(time_range="24h")
        self.assertEqual(summary["period"], "24h")
        self.assertEqual(summary["requests"]["total"], 0)
        self.assertEqual(summary["requests"]["successful"], 0)
        self.assertEqual(summary["requests"]["failed"], 0)
        self.assertEqual(summary["tokens"]["total"], 0)
        self.assertEqual(summary["latency"]["average_ms"], 0.0)
        self.assertEqual(summary["errors"]["rate"], 0.0)
        self.assertEqual(summary["rate_limiting"]["total_blocked"], 0)

    async def test_02_summary_with_traffic_and_metrics(self):
        """Verify summary computes totals, tokens, error rate, and min/avg/max latency."""
        now = time.time()
        pool = await rate_limiter.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                # 3 successful requests
                await cur.execute(
                    """INSERT INTO request_log (identity, model, status, tokens, latency_ms, intent, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    ("ip:1.1.1.1", "mistral:7b", "ok", 100, 200.0, "chat", now - 300)
                )
                await cur.execute(
                    """INSERT INTO request_log (identity, model, status, tokens, latency_ms, intent, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    ("ip:1.1.1.1", "mistral:7b", "ok", 200, 400.0, "chat", now - 200)
                )
                # 1 failed request
                await cur.execute(
                    """INSERT INTO request_log (identity, model, status, tokens, latency_ms, intent, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    ("ip:1.1.1.1", "mistral:7b", "error", 0, 50.0, "chat", now - 100)
                )
                # 1 burst rate limit rejection
                await cur.execute(
                    """INSERT INTO request_log (identity, model, status, tokens, latency_ms, intent, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    ("ip:1.1.1.1", None, "rate_limited", None, None, "burst", now - 50)
                )
                # 1 quota rate limit rejection
                await cur.execute(
                    """INSERT INTO request_log (identity, model, status, tokens, latency_ms, intent, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    ("ip:1.1.1.1", None, "rate_limited", None, None, "hourly", now - 10)
                )

        summary = await analytics.get_summary(time_range="24h")
        self.assertEqual(summary["requests"]["total"], 5)
        self.assertEqual(summary["requests"]["successful"], 2)
        self.assertEqual(summary["requests"]["failed"], 1)
        self.assertEqual(summary["requests"]["rate_limited"], 2)
        self.assertEqual(summary["tokens"]["total"], 300)
        self.assertEqual(summary["errors"]["count"], 1)
        # 1 error out of 3 completed = 33.33%
        self.assertAlmostEqual(summary["errors"]["rate"], 33.33, places=1)
        self.assertEqual(summary["latency"]["min_ms"], 50.0)
        self.assertEqual(summary["latency"]["max_ms"], 400.0)
        self.assertEqual(summary["latency"]["average_ms"], 216.7)
        self.assertEqual(summary["rate_limiting"]["rate_limit_events"], 1)
        self.assertEqual(summary["rate_limiting"]["quota_exceeded_events"], 1)
        self.assertEqual(summary["rate_limiting"]["total_blocked"], 2)

    async def test_03_time_range_filtering(self):
        """Verify queries only include records within the requested time range."""
        now = time.time()
        pool = await rate_limiter.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                # 1 record 30 minutes ago (in 1h, 24h, 7d)
                await cur.execute(
                    """INSERT INTO request_log (identity, model, status, tokens, latency_ms, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    ("ip:2.2.2.2", "mistral:7b", "ok", 50, 100.0, now - 1800)
                )
                # 1 record 5 hours ago (in 24h, 7d, NOT in 1h)
                await cur.execute(
                    """INSERT INTO request_log (identity, model, status, tokens, latency_ms, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    ("ip:2.2.2.2", "mistral:7b", "ok", 50, 100.0, now - 18000)
                )
                # 1 record 3 days ago (in 7d, NOT in 1h or 24h)
                await cur.execute(
                    """INSERT INTO request_log (identity, model, status, tokens, latency_ms, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    ("ip:2.2.2.2", "mistral:7b", "ok", 50, 100.0, now - (3 * 86400))
                )

        s_1h = await analytics.get_summary(time_range="1h")
        self.assertEqual(s_1h["requests"]["total"], 1)

        s_24h = await analytics.get_summary(time_range="24h")
        self.assertEqual(s_24h["requests"]["total"], 2)

        s_7d = await analytics.get_summary(time_range="7d")
        self.assertEqual(s_7d["requests"]["total"], 3)

    async def test_04_model_analytics(self):
        """Verify model-level breakdown of requests, tokens, latency, and error rate."""
        now = time.time()
        pool = await rate_limiter.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO request_log (identity, model, status, tokens, latency_ms, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    ("ip:3.3.3.3", "qwen2.5vl:7b", "ok", 80, 1200.0, now - 100)
                )
                await cur.execute(
                    """INSERT INTO request_log (identity, model, status, tokens, latency_ms, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    ("ip:3.3.3.3", "mistral:7b", "ok", 150, 800.0, now - 50)
                )

        models = await analytics.get_model_stats(time_range="24h")
        model_names = [m["model"] for m in models]
        self.assertIn("qwen2.5vl:7b", model_names)
        self.assertIn("mistral:7b", model_names)

        qwen_stat = next(m for m in models if m["model"] == "qwen2.5vl:7b")
        self.assertEqual(qwen_stat["requests"], 1)
        self.assertEqual(qwen_stat["tokens"], 80)
        self.assertEqual(qwen_stat["average_latency_ms"], 1200.0)

    async def test_05_timeseries_buckets(self):
        """Verify time-series returns structured continuous buckets."""
        now = time.time()
        pool = await rate_limiter.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO request_log (identity, model, status, tokens, latency_ms, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    ("ip:4.4.4.4", "mistral:7b", "ok", 50, 300.0, now - 60)
                )

        ts = await analytics.get_request_timeseries(time_range="24h")
        self.assertEqual(ts["range"], "24h")
        self.assertEqual(ts["interval_seconds"], 3600)
        self.assertGreaterEqual(len(ts["data"]), 24)

    async def test_06_recent_activity_and_masking(self):
        """Verify recent activity bounds limit and masks sensitive IP info."""
        now = time.time()
        pool = await rate_limiter.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                for i in range(15):
                    await cur.execute(
                        """INSERT INTO request_log (identity, model, status, tokens, latency_ms, intent, created_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        ("ip:192.168.1.50", "mistral:7b", "ok", 40, 150.0, "chat", now - (15 - i))
                    )

        recent = await analytics.get_recent_activity(limit=10)
        self.assertEqual(len(recent), 10)
        # IP masking check
        self.assertEqual(recent[0]["identity"], "ip:192.168.*.*")
        self.assertIn("timestamp", recent[0])
        self.assertEqual(recent[0]["status"], "ok")

    async def test_07_invalid_time_range_raises_error(self):
        """Verify unsupported time range throws ValueError."""
        with self.assertRaises(ValueError):
            await analytics.get_summary(time_range="10years")


class TestAnalyticsEndpoints(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        rate_limiter.MYSQL_CONFIG["db"] = TEST_DB
        await rate_limiter.close_pool()
        await rate_limiter.init_db()

        qwen_lb.API_KEYS = {
            "sk_admin_key_phase2": {
                "name": "admin",
                "active": True,
                "expires_at": None,
                "usage_count": 0,
            },
            "sk_user_key_phase2": {
                "name": "dev_alice",
                "active": True,
                "expires_at": None,
                "usage_count": 0,
            },
        }
        self.transport = httpx.ASGITransport(app=qwen_lb.app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()
        await rate_limiter.close_pool()

    async def test_08_summary_endpoint_public_scoped(self):
        res = await self.client.get("/api/analytics/summary?range=24h")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("requests", data)
        self.assertIn("tokens", data)
        self.assertIn("latency", data)

    async def test_09_user_isolation(self):
        """Normal authenticated user can only access their own scoped metrics."""
        user_alice_id = rate_limiter.make_identity("dev_alice", "127.0.0.1")
        user_bob_id = rate_limiter.make_identity("dev_bob", "127.0.0.1")

        pool = await rate_limiter.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO request_log (identity, model, status, tokens, latency_ms, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                    (user_alice_id, "mistral:7b", "ok", 100, 200.0, time.time())
                )
                await cur.execute(
                    "INSERT INTO request_log (identity, model, status, tokens, latency_ms, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                    (user_bob_id, "mistral:7b", "ok", 300, 200.0, time.time())
                )

        # Call summary as Alice
        res = await self.client.get(
            "/api/analytics/summary?range=24h",
            headers={"Authorization": "Bearer sk_user_key_phase2"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["requests"]["total"], 1)
        self.assertEqual(data["tokens"]["total"], 100)

    async def test_10_admin_global_analytics(self):
        """Admin caller sees system-wide aggregate totals."""
        res = await self.client.get(
            "/api/analytics/summary?range=24h",
            headers={"Authorization": "Bearer sk_admin_key_phase2"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["identity_scope"], "global")
        self.assertGreaterEqual(data["requests"]["total"], 0)

    async def test_11_admin_key_usage_endpoint(self):
        """Only Admin can access /api/analytics/keys."""
        # Regular user should get 403
        r_user = await self.client.get(
            "/api/analytics/keys",
            headers={"Authorization": "Bearer sk_user_key_phase2"}
        )
        self.assertEqual(r_user.status_code, 403)

        # Admin user should get 200
        r_admin = await self.client.get(
            "/api/analytics/keys",
            headers={"Authorization": "Bearer sk_admin_key_phase2"}
        )
        self.assertEqual(r_admin.status_code, 200)
        keys_data = r_admin.json()
        self.assertIsInstance(keys_data, list)


if __name__ == "__main__":
    import httpx
    unittest.main()
