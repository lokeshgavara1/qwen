"""
test_rate_limit.py — Comprehensive Unit & Integration Tests for Rate Limiting & Quota Management (MySQL Backend)
Phase 1 Requirements Verification on MySQL
"""

import asyncio
import os
import sys
import time
import unittest

# Add parent directory to python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import rate_limiter

TEST_DB = "ai_gateway_test_rl"


class TestRateLimiter(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # Override DB name for test isolation
        rate_limiter.MYSQL_CONFIG["db"] = TEST_DB
        await rate_limiter.close_pool()
        await rate_limiter.init_db()

        pool = await rate_limiter.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("TRUNCATE TABLE request_log;")
                await cur.execute("TRUNCATE TABLE key_quotas;")

    async def asyncTearDown(self):
        await rate_limiter.close_pool()

    async def test_01_init_db_creates_tables(self):
        """Verify request_log and key_quotas tables and indexes exist in MySQL."""
        pool = await rate_limiter.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SHOW TABLES;")
                tables = [row[0] for row in await cur.fetchall()]
        self.assertIn("request_log", tables)
        self.assertIn("key_quotas", tables)

    async def test_02_identity_generation(self):
        """Verify opaque identity generation for public IP vs API key."""
        ip_id = rate_limiter.make_identity(None, "192.168.1.100")
        self.assertEqual(ip_id, "ip:192.168.1.100")
        self.assertFalse(rate_limiter.is_authenticated(ip_id))

        key_id = rate_limiter.make_identity("test-user-key", "192.168.1.100")
        self.assertTrue(key_id.startswith("key:"))
        self.assertTrue(rate_limiter.is_authenticated(key_id))
        # Deterministic hash
        self.assertEqual(key_id, rate_limiter.make_identity("test-user-key", "10.0.0.1"))

    async def test_03_public_burst_limit_10_per_minute(self):
        """Verify 10 requests allowed, 11th rejected with burst limit in under 60s."""
        identity = rate_limiter.make_identity(None, "1.2.3.4")

        # Record 10 requests
        for _ in range(10):
            res = await rate_limiter.check_rate_limit(identity)
            self.assertTrue(res["allowed"])
            await rate_limiter.record_request(identity, model="mistral:7b", status="ok")

        # 11th request should be blocked
        res11 = await rate_limiter.check_rate_limit(identity)
        self.assertFalse(res11["allowed"])
        self.assertEqual(res11["limit_type"], "burst")
        self.assertGreater(res11["retry_after"], 0)
        self.assertEqual(res11["burst_used"], 10)

    async def test_04_public_hourly_quota_20_per_hour(self):
        """Verify 20 requests allowed across the hour, 21st rejected with hourly quota."""
        identity = rate_limiter.make_identity(None, "5.6.7.8")

        # Spread 20 requests across the last 30 minutes
        now = time.time()
        pool = await rate_limiter.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                for i in range(20):
                    ts = now - 1800 + (i * 60)
                    await cur.execute(
                        "INSERT INTO request_log (identity, model, status, created_at) VALUES (%s, %s, %s, %s)",
                        (identity, "mistral:7b", "ok", ts)
                    )

        # Check limit: hourly used should be 20
        res = await rate_limiter.check_rate_limit(identity)
        self.assertFalse(res["allowed"])
        self.assertEqual(res["limit_type"], "hourly")
        self.assertEqual(res["hourly_used"], 20)
        self.assertGreater(res["retry_after"], 0)

    async def test_05_authenticated_burst_limit_10_per_minute(self):
        """Verify authenticated key burst limit (10/min)."""
        identity = rate_limiter.make_identity("user-premium", "10.0.0.2")

        for _ in range(10):
            res = await rate_limiter.check_rate_limit(identity)
            self.assertTrue(res["allowed"])
            await rate_limiter.record_request(identity, model="mistral:7b", status="ok")

        res11 = await rate_limiter.check_rate_limit(identity)
        self.assertFalse(res11["allowed"])
        self.assertEqual(res11["limit_type"], "burst")

    async def test_06_authenticated_hourly_quota_200_per_hour(self):
        """Verify authenticated key hourly limit is 200."""
        identity = rate_limiter.make_identity("user-dev", "10.0.0.3")

        now = time.time()
        pool = await rate_limiter.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                for i in range(200):
                    ts = now - 3500 + (i * 15)
                    await cur.execute(
                        "INSERT INTO request_log (identity, model, status, created_at) VALUES (%s, %s, %s, %s)",
                        (identity, "mistral:7b", "ok", ts)
                    )

        res = await rate_limiter.check_rate_limit(identity)
        self.assertFalse(res["allowed"])
        self.assertEqual(res["limit_type"], "hourly")
        self.assertEqual(res["hourly_used"], 200)

    async def test_07_identity_isolation(self):
        """Requests from identity A must not affect identity B."""
        id_a = rate_limiter.make_identity(None, "1.1.1.1")
        id_b = rate_limiter.make_identity(None, "2.2.2.2")

        for _ in range(10):
            await rate_limiter.record_request(id_a, status="ok")

        # id_a is blocked
        res_a = await rate_limiter.check_rate_limit(id_a)
        self.assertFalse(res_a["allowed"])

        # id_b is fresh and allowed
        res_b = await rate_limiter.check_rate_limit(id_b)
        self.assertTrue(res_b["allowed"])
        self.assertEqual(res_b["burst_used"], 0)
        self.assertEqual(res_b["hourly_used"], 0)

    async def test_08_rate_limited_requests_not_counted_against_quota(self):
        """429 (rate_limited) status records do not increment usage counters."""
        identity = rate_limiter.make_identity(None, "3.3.3.3")

        # Record 5 ok requests + 5 rate_limited records
        for _ in range(5):
            await rate_limiter.record_request(identity, status="ok")
        for _ in range(5):
            await rate_limiter.record_request(identity, status="rate_limited")

        res = await rate_limiter.check_rate_limit(identity)
        self.assertTrue(res["allowed"])
        self.assertEqual(res["burst_used"], 5)
        self.assertEqual(res["hourly_used"], 5)

    async def test_09_admin_custom_quota_override(self):
        """Admin can set custom burst and hourly quota for any identity in MySQL."""
        identity = rate_limiter.make_identity("enterprise-corp", "10.0.0.5")

        b_lim, b_win, h_quota = await rate_limiter.get_limits(identity)
        self.assertEqual(b_lim, 10)
        self.assertEqual(h_quota, 200)

        # Override to 500/hr and 25 burst
        ok = await rate_limiter.set_quota(identity, hourly_quota=500, burst_limit=25, burst_window=60)
        self.assertTrue(ok)

        b_lim, b_win, h_quota = await rate_limiter.get_limits(identity)
        self.assertEqual(b_lim, 25)
        self.assertEqual(h_quota, 500)

    async def test_10_get_usage_stats(self):
        """Usage stats structure correctly reports limits and remaining quotas."""
        identity = rate_limiter.make_identity(None, "4.4.4.4")

        for _ in range(3):
            await rate_limiter.record_request(identity, model="mistral:7b", status="ok", tokens=50, latency_ms=120.5)

        stats = await rate_limiter.get_usage_stats(identity)
        self.assertEqual(stats["identity"], identity)
        self.assertEqual(stats["requests_last_minute"], 3)
        self.assertEqual(stats["requests_last_hour"], 3)
        self.assertEqual(stats["burst_limit"], 10)
        self.assertEqual(stats["hourly_limit"], 20)
        self.assertEqual(stats["remaining_burst"], 7)
        self.assertEqual(stats["remaining_hourly"], 17)
        self.assertEqual(stats["total_tokens"], 150)
        self.assertEqual(stats["total_requests_all_time"], 3)

    async def test_11_cleanup_old_records(self):
        """Old records (> 7 days) are purged from MySQL, newer records are preserved."""
        identity = rate_limiter.make_identity(None, "5.5.5.5")
        now = time.time()
        eight_days_ago = now - (8 * 86400)
        one_day_ago = now - (1 * 86400)

        pool = await rate_limiter.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("INSERT INTO request_log (identity, created_at) VALUES (%s, %s)", (identity, eight_days_ago))
                await cur.execute("INSERT INTO request_log (identity, created_at) VALUES (%s, %s)", (identity, eight_days_ago))
                await cur.execute("INSERT INTO request_log (identity, created_at) VALUES (%s, %s)", (identity, one_day_ago))

        deleted = await rate_limiter.cleanup_old_records(retention_days=7)
        self.assertEqual(deleted, 2)

        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT COUNT(*) FROM request_log")
                count = (await cur.fetchone())[0]
        self.assertEqual(count, 1)

    async def test_12_mysql_persistence_across_reconnect(self):
        """Data persists in MySQL across pool reconnections."""
        identity = rate_limiter.make_identity(None, "6.6.6.6")
        await rate_limiter.record_request(identity, status="ok")

        # Reopen pool
        await rate_limiter.close_pool()
        pool = await rate_limiter.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT COUNT(*) FROM request_log WHERE identity = %s", (identity,))
                count = (await cur.fetchone())[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
