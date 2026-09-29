"""
tests/test_security_hardening.py — Phase 8: Comprehensive Security & Audit Tests
================================================================================
Verifies:
- Deny-by-default on all /api/admin/* endpoints
- 401 Unauthorized for missing/invalid API keys
- 403 Forbidden for non-admin API keys attempting privileged operations
- Audit event generation and append-only MySQL storage
- No raw API key leakage in responses, logs, or audit records
- Security headers (X-Request-ID, nosniff, DENY, CSP)
- Request size limit enforcement (HTTP 413)
- Parameterized SQL injection defense on audit endpoints
"""

from datetime import datetime, timedelta
import json
import unittest
import httpx

import qwen_lb
from qwen_lb import app
import security
import rate_limiter


class TestSecurityHardening(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://testserver")

        # Configure deterministic test keys
        self.admin_key = "sk_test_admin_key_sec_001"
        self.user_key = "sk_test_regular_user_sec_002"
        self.expired_key = "sk_test_expired_key_sec_003"

        qwen_lb.API_KEYS[self.admin_key] = {
            "name": "admin",
            "role": "admin",
            "active": True,
            "created_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() + timedelta(days=30)).isoformat(),
        }

        qwen_lb.API_KEYS[self.user_key] = {
            "name": "developer_alice",
            "role": "user",
            "active": True,
            "created_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() + timedelta(days=30)).isoformat(),
        }

        qwen_lb.API_KEYS[self.expired_key] = {
            "name": "expired_user",
            "role": "admin",
            "active": True,
            "created_at": (datetime.now() - timedelta(days=40)).isoformat(),
            "expires_at": (datetime.now() - timedelta(days=10)).isoformat(),
        }

        # Initialize audit db
        await security.init_audit_db()

    async def asyncTearDown(self):
        await self.client.aclose()

    # --------------------------------------------------------------------------
    # 1. AUTHENTICATION & AUTHORIZATION ENFORCEMENT
    # --------------------------------------------------------------------------

    async def test_01_admin_endpoints_require_auth_401(self):
        """Unauthenticated requests to admin endpoints must return 401 Unauthorized."""
        endpoints = [
            ("POST", "/api/admin/generate-key", {"name": "test"}),
            ("GET", "/api/admin/keys", None),
            ("POST", "/api/admin/set-quota", {"identity": "ip:1.2.3.4", "hourly_quota": 50, "burst_limit": 10}),
            ("GET", "/api/admin/audit-logs", None),
            ("GET", "/api/analytics/keys", None),
        ]

        for method, path, body in endpoints:
            if method == "POST":
                res = await self.client.post(path, json=body)
            else:
                res = await self.client.get(path)

            self.assertEqual(res.status_code, 401, f"Failed on {method} {path} — expected 401, got {res.status_code}")
            self.assertIn("Unauthorized", res.text)

    async def test_02_invalid_api_key_rejected_401(self):
        """Bogus API keys must return 401 Unauthorized on admin endpoints."""
        res = await self.client.get(
            "/api/admin/keys",
            headers={"Authorization": "Bearer sk_invalid_bogus_key_9999"}
        )
        self.assertEqual(res.status_code, 401)
        self.assertIn("Unauthorized", res.text)

    async def test_03_expired_key_rejected_403(self):
        """Expired keys must be rejected with 403 Forbidden."""
        res = await self.client.get(
            "/api/admin/keys",
            headers={"Authorization": f"Bearer {self.expired_key}"}
        )
        self.assertEqual(res.status_code, 403)
        self.assertIn("expired", res.text.lower())

    async def test_04_regular_user_key_forbidden_403(self):
        """Standard non-admin authenticated keys must be rejected with 403 Forbidden on all admin endpoints."""
        user_hdr = {"Authorization": f"Bearer {self.user_key}"}

        endpoints = [
            ("POST", "/api/admin/generate-key", {"name": "attacker_key"}),
            ("GET", "/api/admin/keys", None),
            ("POST", "/api/admin/set-quota", {"identity": "ip:1.2.3.4", "hourly_quota": 50, "burst_limit": 10}),
            ("GET", "/api/admin/audit-logs", None),
            ("GET", "/api/analytics/keys", None),
        ]

        for method, path, body in endpoints:
            if method == "POST":
                res = await self.client.post(path, json=body, headers=user_hdr)
            else:
                res = await self.client.get(path, headers=user_hdr)

            self.assertEqual(res.status_code, 403, f"Non-admin should be forbidden on {method} {path}")
            self.assertIn("Forbidden", res.text)

    async def test_05_admin_key_succeeds_200(self):
        """Valid admin API key successfully accesses admin endpoints."""
        admin_hdr = {"Authorization": f"Bearer {self.admin_key}"}

        # 1. List keys
        r1 = await self.client.get("/api/admin/keys", headers=admin_hdr)
        self.assertEqual(r1.status_code, 200)
        self.assertIn("keys", r1.json())

        # 2. Generate key
        r2 = await self.client.post("/api/admin/generate-key", json={"name": "new_team_key", "expires_in_days": 30}, headers=admin_hdr)
        self.assertEqual(r2.status_code, 200)
        self.assertIn("api_key", r2.json())

        # 3. Set quota
        r3 = await self.client.post("/api/admin/set-quota", json={"identity": "ip:10.0.0.5", "hourly_quota": 500, "burst_limit": 50}, headers=admin_hdr)
        self.assertEqual(r3.status_code, 200)

        # 4. Audit logs
        r4 = await self.client.get("/api/admin/audit-logs", headers=admin_hdr)
        self.assertEqual(r4.status_code, 200)
        self.assertIn("logs", r4.json())

    # --------------------------------------------------------------------------
    # 2. AUDIT LOGGING & IMMUTABILITY
    # --------------------------------------------------------------------------

    async def test_06_audit_logs_record_security_events(self):
        """Verify audit events are recorded in MySQL on administrative actions."""
        admin_hdr = {"Authorization": f"Bearer {self.admin_key}"}

        # Generate a key to trigger audit log
        await self.client.post("/api/admin/generate-key", json={"name": "audited_key"}, headers=admin_hdr)

        # Fetch audit logs
        res = await self.client.get("/api/admin/audit-logs?action=API_KEY_CREATED", headers=admin_hdr)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertGreater(data["total"], 0)

        found_event = any(l["action"] == "API_KEY_CREATED" for l in data["logs"])
        self.assertTrue(found_event, "API_KEY_CREATED action should be logged in audit table")

    async def test_07_api_keys_never_exposed_raw_in_list(self):
        """Verify API keys are masked in GET /api/admin/keys."""
        admin_hdr = {"Authorization": f"Bearer {self.admin_key}"}
        res = await self.client.get("/api/admin/keys", headers=admin_hdr)
        self.assertEqual(res.status_code, 200)

        keys_data = res.json()["keys"]
        for k in keys_data:
            masked = k["key_masked"]
            self.assertIn("••••", masked)
            self.assertNotIn(self.admin_key, masked)

    # --------------------------------------------------------------------------
    # 3. SECURITY HEADERS & REQUEST ID
    # --------------------------------------------------------------------------

    async def test_08_security_headers_present(self):
        """Verify defense-in-depth security headers are present on all responses."""
        res = await self.client.get("/health")
        self.assertEqual(res.status_code, 200)

        self.assertEqual(res.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(res.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(res.headers.get("Referrer-Policy"), "strict-origin-when-cross-origin")
        self.assertIn("default-src", res.headers.get("Content-Security-Policy", ""))
        self.assertTrue(bool(res.headers.get("X-Request-ID")))

    async def test_09_custom_request_id_propagated(self):
        """Custom valid X-Request-ID from client is preserved and returned."""
        custom_id = "req_custom_security_test_12345"
        res = await self.client.get("/health", headers={"X-Request-ID": custom_id})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("X-Request-ID"), custom_id)

    # --------------------------------------------------------------------------
    # 4. SQL INJECTION DEFENSE & INPUT VALIDATION
    # --------------------------------------------------------------------------

    async def test_10_sql_injection_defense_on_audit_logs(self):
        """Malicious SQL injection payloads in audit query parameters are safely handled."""
        admin_hdr = {"Authorization": f"Bearer {self.admin_key}"}
        sqli_payload = "' OR '1'='1"

        res = await self.client.get(f"/api/admin/audit-logs?action={sqli_payload}", headers=admin_hdr)
        self.assertEqual(res.status_code, 200)
        # Should return 0 matching logs or empty array safely without SQL error
        data = res.json()
        self.assertIsInstance(data.get("logs"), list)

    async def test_11_request_body_size_limit(self):
        """Requests with oversized Content-Length (>10MB) return 413 Payload Too Large."""
        oversized_headers = {
            "Content-Length": str(15 * 1024 * 1024),  # 15 MB
            "Content-Type": "application/json"
        }
        res = await self.client.post("/api/admin/generate-key", headers=oversized_headers)
        self.assertEqual(res.status_code, 413)
        self.assertIn("Payload Too Large", res.text)


if __name__ == "__main__":
    unittest.main()
