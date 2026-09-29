"""
test_audit_comprehensive.py — Deep System Audit and Stress Test (MySQL Backend)
Audits rate limiter, concurrency, security, error handling, and scheduler invariants.
"""

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx
import qwen_lb
import rate_limiter

TEST_DB = "ai_gateway_test_audit"


class TestComprehensiveAudit(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        rate_limiter.MYSQL_CONFIG["db"] = TEST_DB
        await rate_limiter.close_pool()
        await rate_limiter.init_db()

        qwen_lb.API_KEYS = {
            "sk_admin_audit_secret_key_999": {
                "name": "admin",
                "active": True,
                "expires_at": None,
                "usage_count": 0,
            },
            "sk_user_audit_secret_key_111": {
                "name": "regular_dev",
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

    async def test_01_mysql_concurrent_writes(self):
        """Audit concurrent writes to rate_limiter MySQL pool under high concurrency."""
        tasks = []
        for i in range(50):
            identity = f"ip:10.0.0.{i % 5}"
            tasks.append(rate_limiter.record_request(
                identity=identity,
                model="mistral:7b",
                status="ok",
                tokens=100,
                latency_ms=50.0,
                intent="coding"
            ))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            self.assertNotIsInstance(r, Exception)

    def test_02_ip_spoofing_protection(self):
        """Direct external clients must NOT be allowed to spoof X-Forwarded-For."""
        class MockClient:
            host = "203.0.113.195"  # Remote external IP

        class MockRequest:
            client = MockClient()
            headers = {"X-Forwarded-For": "1.1.1.1, 10.0.0.1"}

        client_ip = qwen_lb.get_client_ip(MockRequest())
        self.assertEqual(client_ip, "203.0.113.195")

    def test_03_loopback_proxy_forwarding_trusted(self):
        """Requests from loopback (e.g. NGINX) should respect X-Forwarded-For."""
        class MockClient:
            host = "127.0.0.1"

        class MockRequest:
            client = MockClient()
            headers = {"X-Forwarded-For": "198.51.100.42, 127.0.0.1"}

        client_ip = qwen_lb.get_client_ip(MockRequest())
        self.assertEqual(client_ip, "198.51.100.42")

    async def test_04_admin_authorization_matrix(self):
        """Exhaustive check of admin key authorization permutations."""
        # 1. No Authorization header
        r1 = await self.client.post("/api/admin/set-quota", json={"identity": "ip:1.1.1.1", "hourly_quota": 50, "burst_limit": 10})
        self.assertEqual(r1.status_code, 401)

        # 2. Invalid Bearer token
        r2 = await self.client.post("/api/admin/set-quota", headers={"Authorization": "Bearer non_existent_key"}, json={"identity": "ip:1.1.1.1", "hourly_quota": 50, "burst_limit": 10})
        self.assertEqual(r2.status_code, 401)

        # 3. Valid user key with non-admin name
        r3 = await self.client.post("/api/admin/set-quota", headers={"Authorization": "Bearer sk_user_audit_secret_key_111"}, json={"identity": "ip:1.1.1.1", "hourly_quota": 50, "burst_limit": 10})
        self.assertEqual(r3.status_code, 403)

        # 4. Valid admin key
        r4 = await self.client.post("/api/admin/set-quota", headers={"Authorization": "Bearer sk_admin_audit_secret_key_999"}, json={"identity": "ip:9.9.9.9", "hourly_quota": 500, "burst_limit": 25, "burst_window": 60})
        self.assertEqual(r4.status_code, 200)
        self.assertTrue(r4.json()["ok"])

    async def test_05_malformed_json_resilience(self):
        """Audit resilience against malformed JSON or unprocessable requests."""
        # Invalid JSON to /api/generate
        res1 = await self.client.post("/api/generate", content="not json", headers={"Content-Type": "application/json"})
        self.assertEqual(res1.status_code, 500)

        # Missing/malformed body to /api/admin/set-quota
        res2 = await self.client.post(
            "/api/admin/set-quota",
            headers={"Authorization": "Bearer sk_admin_audit_secret_key_999", "Content-Type": "application/json"},
            content="bad json"
        )
        self.assertIn(res2.status_code, [400, 422])

    def test_06_fast_intent_detector(self):
        """Verify keyword and structure detection across all 4 intents."""
        d = qwen_lb.FastIntentDetector
        self.assertEqual(d.detect("def calculate_sum(a, b): return a + b"), "coding")
        self.assertEqual(d.detect("How do I write a python function to parse JSON?"), "coding")
        self.assertEqual(d.detect("Explain the theory of general relativity and quantum entanglement"), "reasoning")
        self.assertEqual(d.detect("What is a good recipe for pasta?"), "chat")
        self.assertEqual(d.detect("Please analyze this diagram", images=["data:image/png;base64,..."]), "vision")

    def test_07_response_cache_lifecycle(self):
        """Verify TTL expiration and cache hit/miss behavior."""
        c = qwen_lb.ResponseCache(ttl_seconds=1)
        c.set("mistral:7b", "hello", {"response": "world"})

        self.assertEqual(c.get("mistral:7b", "hello"), {"response": "world"})
        self.assertIsNone(c.get("mistral:7b", "different prompt"))

        time.sleep(1.1)
        self.assertIsNone(c.get("mistral:7b", "hello"))

    def test_08_smart_scheduler_invariants(self):
        """Audit SmartScheduler queue tracking and recovery."""
        role = "local"
        qwen_lb.SmartScheduler.start_request(role)
        self.assertGreaterEqual(qwen_lb.SERVER_STATE[role]["queue"], 1)

        qwen_lb.SmartScheduler.finish_request(role, latency_ms=45.0)
        self.assertGreaterEqual(qwen_lb.SERVER_STATE[role]["latency"], 0.0)

        qwen_lb.SmartScheduler.finish_request(role, latency_ms=0)
        qwen_lb.SmartScheduler.finish_request(role, latency_ms=0)
        self.assertEqual(qwen_lb.SERVER_STATE[role]["queue"], 0)


if __name__ == "__main__":
    unittest.main()
