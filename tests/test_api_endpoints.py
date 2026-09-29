"""
test_api_endpoints.py — Integration tests for Phase 1 & 2 endpoints (MySQL Backend)
Tests /api/usage, /api/admin/set-quota (auth enforcement), /health, /status, /dashboard
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx
import qwen_lb
import rate_limiter

TEST_DB = "ai_gateway_test_endpoints"


class TestGatewayEndpoints(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        rate_limiter.MYSQL_CONFIG["db"] = TEST_DB
        await rate_limiter.close_pool()
        await rate_limiter.init_db()

        # Inject test API keys
        qwen_lb.API_KEYS = {
            "sk_test_admin_key_12345": {
                "name": "admin",
                "active": True,
                "expires_at": None,
                "usage_count": 0,
            },
            "sk_test_regular_user_key": {
                "name": "developer-john",
                "active": True,
                "expires_at": None,
                "usage_count": 0,
            },
            "sk_test_expired_key": {
                "name": "admin",
                "active": True,
                "expires_at": "2020-01-01T00:00:00",
                "usage_count": 0,
            },
        }
        self.transport = httpx.ASGITransport(app=qwen_lb.app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()
        await rate_limiter.close_pool()

    async def test_01_health_endpoint(self):
        res = await self.client.get("/health")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("healthy", data)
        self.assertIn("servers", data)

    async def test_02_usage_endpoint_anonymous(self):
        res = await self.client.get("/api/usage")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["identity"].startswith("ip:"))
        self.assertEqual(data["burst_limit"], 10)
        self.assertEqual(data["hourly_limit"], 20)

    async def test_03_usage_endpoint_authenticated(self):
        res = await self.client.get(
            "/api/usage",
            headers={"Authorization": "Bearer sk_test_regular_user_key"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["identity"].startswith("key:"))
        self.assertEqual(data["burst_limit"], 10)
        self.assertEqual(data["hourly_limit"], 200)

    async def test_04_admin_set_quota_no_auth(self):
        res = await self.client.post("/api/admin/set-quota", json={"identity": "ip:1.1.1.1", "hourly_quota": 50, "burst_limit": 15})
        self.assertEqual(res.status_code, 401)

    async def test_05_admin_set_quota_regular_key_rejected(self):
        """Regular non-admin API key must be rejected with 403."""
        res = await self.client.post(
            "/api/admin/set-quota",
            headers={"Authorization": "Bearer sk_test_regular_user_key"},
            json={"identity": "ip:1.1.1.1", "hourly_quota": 50, "burst_limit": 15}
        )
        self.assertEqual(res.status_code, 403)
        self.assertIn("admin key required", res.json()["error"])

    async def test_06_admin_set_quota_expired_admin_key_rejected(self):
        """Expired admin key must be rejected with 403."""
        res = await self.client.post(
            "/api/admin/set-quota",
            headers={"Authorization": "Bearer sk_test_expired_key"},
            json={"identity": "ip:1.1.1.1", "hourly_quota": 50, "burst_limit": 15}
        )
        self.assertEqual(res.status_code, 403)

    async def test_07_admin_set_quota_valid_admin_success(self):
        """Valid admin key sets quota successfully."""
        res = await self.client.post(
            "/api/admin/set-quota",
            headers={"Authorization": "Bearer sk_test_admin_key_12345"},
            json={
                "identity": "ip:8.8.8.8",
                "hourly_quota": 100,
                "burst_limit": 20,
                "burst_window": 60,
            }
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])
        self.assertEqual(res.json()["hourly_quota"], 100)
        self.assertEqual(res.json()["burst_limit"], 20)

    async def test_08_admin_set_quota_validation_errors(self):
        """Invalid fields return 422 Unprocessable Entity."""
        admin_hdr = {"Authorization": "Bearer sk_test_admin_key_12345"}
        
        # Missing identity
        r1 = await self.client.post("/api/admin/set-quota", headers=admin_hdr, json={"hourly_quota": 10, "burst_limit": 5})
        self.assertEqual(r1.status_code, 422)

        # Invalid quota
        r2 = await self.client.post("/api/admin/set-quota", headers=admin_hdr, json={"identity": "ip:1.1.1.1", "hourly_quota": -5, "burst_limit": 5})
        self.assertEqual(r2.status_code, 422)

    async def test_09_dashboard_html_renders(self):
        """Verify dashboard HTML endpoint renders successfully."""
        res = await self.client.get("/dashboard")
        self.assertEqual(res.status_code, 200)
        self.assertIn("AI Gateway Analytics", res.text)
        self.assertIn("timeseries-chart-wrap", res.text)


if __name__ == "__main__":
    unittest.main()
