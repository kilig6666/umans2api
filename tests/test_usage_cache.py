import importlib
import os
import unittest
from unittest import mock

os.environ["UMANS2API_DISABLE_BACKGROUND_THREADS"] = "1"
appmod = importlib.import_module("umasn2api")


class UsageAndCacheRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as sess:
            sess["admin_authed"] = True

    def test_usage_route(self):
        with mock.patch.object(appmod, "summarize_request_logs", return_value={"total_requests": 3, "total_tokens": 42}), \
             mock.patch.object(appmod, "response_cache_stats", return_value={"entries": 2, "hits": 5}):
            resp = self.client.get("/api/usage")
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["usage"]["total_requests"], 3)
        self.assertEqual(payload["cache"]["entries"], 2)

    def test_chat_completions_cache_hit(self):
        cached_payload = {
            "api_format": "openai",
            "response": {
                "id": "chatcmpl-cache",
                "object": "chat.completion",
                "created": 1,
                "model": "umans-coding-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "pong"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 1,
                    "total_tokens": 5,
                },
            },
        }
        with mock.patch.object(appmod, "get_cached_response", return_value=cached_payload), \
             mock.patch.object(appmod, "_attempt_upstream_request", side_effect=AssertionError("should not call upstream")), \
             mock.patch.object(appmod, "insert_request_log", return_value=None):
            resp = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer sk-umans-2api-local"},
                json={
                    "model": "umans-coding-model",
                    "messages": [{"role": "user", "content": "reply with exactly pong"}],
                    "stream": False,
                },
            )
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["choices"][0]["message"]["content"], "pong")
        self.assertEqual(payload["usage"]["prompt_tokens_details"]["cached_tokens"], 4)


if __name__ == "__main__":
    unittest.main()
