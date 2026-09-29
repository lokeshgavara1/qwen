"""
tests/test_conversation_and_limits.py — Tests for increased token limits, conversation history, and input validation
===================================================================================================================
Verifies:
1. MAX_TOKENS_BY_TYPE updated limits: chat=200, coding=300, vision=150, reasoning=250.
2. Token estimation formula (word_count * 1.3).
3. Input validation: rejecting prompts with > 4000 estimated tokens (HTTP 400).
4. ConversationMemory: storing, retrieving, and linking context for follow-up questions.
5. End-to-end /api/generate conversation_id propagation and header exposure.
"""

import asyncio
from datetime import datetime
import json
import unittest
import httpx

import qwen_lb
from qwen_lb import app, MAX_TOKENS_BY_TYPE, estimate_tokens, ConversationMemory, conversation_memory
import rate_limiter
import security


class TestConversationAndLimits(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://testserver")

        # Configure deterministic test key
        self.test_key = "sk_test_conv_user_key"
        qwen_lb.API_KEYS[self.test_key] = {
            "name": "conv_tester",
            "role": "user",
            "active": True,
            "created_at": datetime.now().isoformat(),
            "expires_at": None,
        }

        # Clear conversation memory before each test
        conversation_memory.clear()

        # Init DBs
        await rate_limiter.init_db()
        await security.init_audit_db()

    async def asyncTearDown(self):
        await self.client.aclose()

    def test_01_token_limits_values(self):
        """Verify the increased token limits according to specifications."""
        self.assertEqual(MAX_TOKENS_BY_TYPE.get("chat"), 200)
        self.assertEqual(MAX_TOKENS_BY_TYPE.get("coding"), 300)
        self.assertEqual(MAX_TOKENS_BY_TYPE.get("vision"), 150)
        self.assertEqual(MAX_TOKENS_BY_TYPE.get("reasoning"), 250)

    def test_02_token_estimation_formula(self):
        """Verify token estimation calculation: word_count * 1.3."""
        self.assertEqual(estimate_tokens(""), 0.0)
        
        # 10 words -> 13.0 tokens
        sample_text = "one two three four five six seven eight nine ten"
        self.assertAlmostEqual(estimate_tokens(sample_text), 13.0, places=2)

        # 100 words -> 130.0 tokens
        sample_100 = " ".join(["word"] * 100)
        self.assertAlmostEqual(estimate_tokens(sample_100), 130.0, places=2)

    def test_03_conversation_memory_store_and_eviction(self):
        """Verify ConversationMemory set, get, and eviction mechanics."""
        mem = ConversationMemory(max_entries=3)
        
        # Store entries
        mem.set_last_response("conv_1", "Response 1", "Question 1")
        mem.set_last_response("conv_2", "Response 2", "Question 2")
        self.assertEqual(mem.get_last_response("conv_1"), "Response 1")
        self.assertEqual(mem.get_last_response("conv_2"), "Response 2")
        self.assertIsNone(mem.get_last_response("conv_unknown"))

        # Update entry
        mem.set_last_response("conv_1", "Updated Response 1", "Question 1b")
        self.assertEqual(mem.get_last_response("conv_1"), "Updated Response 1")

        # Test capacity & eviction
        mem.set_last_response("conv_3", "Response 3")
        mem.set_last_response("conv_4", "Response 4")  # Should evict oldest (conv_2)
        self.assertEqual(mem.get_last_response("conv_4"), "Response 4")
        self.assertEqual(mem.get_last_response("conv_1"), "Updated Response 1")
        self.assertIsNone(mem.get_last_response("conv_2"))

    async def test_04_input_validation_oversized_prompt_rejected_400(self):
        """Prompts with estimated tokens > 4000 are rejected with HTTP 400."""
        # 3100 words * 1.3 = 4030 tokens (> 4000)
        huge_prompt = " ".join(["algorithm"] * 3100)
        
        res = await self.client.post(
            "/api/generate",
            json={"prompt": huge_prompt, "stream": False},
            headers={"Authorization": f"Bearer {self.test_key}"}
        )
        self.assertEqual(res.status_code, 400)
        data = res.json()
        self.assertIn("error", data)
        self.assertIn("4000", data.get("error", "") + data.get("message", ""))
        self.assertGreater(data.get("estimated_tokens", 0), 4000)

    async def test_05_input_validation_normal_prompt_allowed(self):
        """Prompts within limit (< 4000 tokens) pass validation."""
        normal_prompt = "What is the speed of light?"
        
        # When online servers are simulated/not reachable, it will either hit cache or return 503
        # but NOT 400 validation error
        res = await self.client.post(
            "/api/generate",
            json={"prompt": normal_prompt, "stream": False},
            headers={"Authorization": f"Bearer {self.test_key}"}
        )
        self.assertNotEqual(res.status_code, 400, "Normal prompt should pass validation")
        self.assertTrue(res.headers.get("X-Conversation-ID") is not None)

    async def test_06_conversation_context_linking_and_id_propagation(self):
        """Verify conversation_id tracking and context linking in generate endpoint."""
        conv_id = "test_session_abc123"
        
        # Preset conversation memory to simulate a prior turn
        conversation_memory.set_last_response(
            conv_id,
            "Paris is the capital of France, known for the Eiffel Tower.",
            "What is the capital of France?"
        )

        # Pre-seed cache with the combined prompt to test cache hit with context
        combined_prompt = f"Previous response context:\nParis is the capital of France, known for the Eiffel Tower.\n\nUser follow-up question:\nWhat is its population?"
        cached_response = {"response": "The population of Paris is approximately 2.1 million.", "eval_count": 12}
        qwen_lb.cache.set("mistral:7b", combined_prompt, cached_response)

        # Send follow-up request with conversation_id
        res = await self.client.post(
            "/api/generate",
            json={
                "prompt": "What is its population?",
                "conversation_id": conv_id,
                "stream": False,
                "model": "mistral:7b"
            },
            headers={"Authorization": f"Bearer {self.test_key}"}
        )

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("_cached"))
        self.assertEqual(data.get("conversation_id"), conv_id)
        self.assertEqual(res.headers.get("X-Conversation-ID"), conv_id)

        # Verify conversation memory updated with the new turn
        latest_memory = conversation_memory.get_last_response(conv_id)
        self.assertEqual(latest_memory, "The population of Paris is approximately 2.1 million.")


if __name__ == "__main__":
    unittest.main()
