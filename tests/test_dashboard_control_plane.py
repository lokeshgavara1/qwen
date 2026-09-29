"""
Test Suite: AI Gateway Control Plane Dashboard UI Validation
Verifies dashboard route, static assets, HTML structure, design system tokens, and API integration.
"""

import os
import unittest
from pathlib import Path
import httpx
from qwen_lb import app


class TestDashboardControlPlane(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_01_dashboard_html_served_with_200(self):
        """Verify /dashboard returns 200 OK and serves complete control plane HTML."""
        res = await self.client.get("/dashboard")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers.get("content-type", ""))
        self.assertIn("AI Gateway Analytics", res.text)
        self.assertIn("timeseries-chart-wrap", res.text)

    async def test_02_css_asset_served_with_tokens(self):
        """Verify /css/dashboard.css is mounted and contains design system tokens."""
        res = await self.client.get("/css/dashboard.css")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/css", res.headers.get("content-type", ""))
        self.assertIn("--bg-app", res.text)
        self.assertIn("--status-success", res.text)
        self.assertIn("--border-subtle", res.text)

    async def test_03_js_asset_served(self):
        """Verify /js/dashboard.js is mounted and contains client controller logic."""
        res = await self.client.get("/js/dashboard.js")
        self.assertEqual(res.status_code, 200)
        self.assertIn("javascript", res.headers.get("content-type", ""))
        self.assertIn("loadDashboardData", res.text)
        self.assertIn("renderTrafficChart", res.text)

    async def test_04_html_structure_completeness(self):
        """Verify all essential control plane elements are present in dashboard HTML."""
        dashboard_path = Path("Frontend/dashboard.html")
        self.assertTrue(dashboard_path.exists())
        content = dashboard_path.read_text(encoding="utf-8")

        required_elements = [
            'id="sidebar"',
            'id="mobile-menu-btn"',
            'id="gateway-status-dot"',
            'id="gateway-status-label"',
            'data-range="1h"',
            'data-range="24h"',
            'data-range="7d"',
            'data-range="30d"',
            'id="refresh-interval"',
            'id="refresh-btn"',
            'id="kpi-total-reqs"',
            'id="kpi-success-rate"',
            'id="kpi-avg-latency"',
            'id="kpi-total-tokens"',
            'id="timeseries-chart-wrap"',
            'id="chart-tooltip"',
            'id="latency-dist-wrap"',
            'id="err-rate-val"',
            'id="models-tbody"',
            'id="workers-tbody"',
            'id="recent-tbody"',
            'id="policy-public-burst"',
            'id="policy-auth-hourly"'
        ]

        for elem in required_elements:
            self.assertIn(elem, content, f"Missing required element {elem} in dashboard.html")


if __name__ == "__main__":
    unittest.main()
