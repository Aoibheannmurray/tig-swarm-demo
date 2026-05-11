from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import llm_backends


class CallOpenAITests(unittest.TestCase):
    def capture_call(self, model: str, api_base: str | None = None):
        captured = {}
        original_post_json = llm_backends._post_json

        def fake_post_json(url, body, headers):
            captured["url"] = url
            captured["body"] = body
            captured["headers"] = headers
            return {"choices": [{"message": {"content": "ok"}}]}

        llm_backends._post_json = fake_post_json
        try:
            content = llm_backends.call_openai("sys", "prompt", model, "key", api_base)
        finally:
            llm_backends._post_json = original_post_json

        return content, captured

    def test_gpt_5_models_use_completion_tokens_and_developer_role(self):
        content, captured = self.capture_call("gpt-5.5")

        self.assertEqual(content, "ok")
        self.assertIn("max_completion_tokens", captured["body"])
        self.assertNotIn("max_tokens", captured["body"])
        self.assertEqual(captured["body"]["messages"][0]["role"], "developer")

    def test_provider_prefixed_gpt_5_models_are_detected(self):
        _, captured = self.capture_call(
            "openai/gpt-5.5",
            api_base="https://example.test/openai-compatible",
        )

        self.assertEqual(
            captured["url"],
            "https://example.test/openai-compatible/v1/chat/completions",
        )
        self.assertIn("max_completion_tokens", captured["body"])
        self.assertEqual(captured["body"]["max_completion_tokens"], 100000)
        self.assertEqual(captured["body"]["messages"][0]["role"], "developer")

    def test_legacy_models_keep_legacy_payload(self):
        _, captured = self.capture_call("gpt-4o")

        self.assertIn("max_tokens", captured["body"])
        self.assertNotIn("max_completion_tokens", captured["body"])
        self.assertEqual(captured["body"]["messages"][0]["role"], "system")

    def test_unsupported_max_tokens_error_retries_with_completion_tokens(self):
        bodies = []
        original_post_json = llm_backends._post_json

        def fake_post_json(_url, body, _headers):
            bodies.append(body)
            if len(bodies) == 1:
                raise RuntimeError(
                    "HTTP 400: Unsupported parameter: 'max_tokens' is not "
                    "supported with this model. Use 'max_completion_tokens' "
                    "instead."
                )
            return {"choices": [{"message": {"content": "ok"}}]}

        llm_backends._post_json = fake_post_json
        try:
            content = llm_backends.call_openai("sys", "prompt", "future-model", "key")
        finally:
            llm_backends._post_json = original_post_json

        self.assertEqual(content, "ok")
        self.assertIn("max_tokens", bodies[0])
        self.assertIn("max_completion_tokens", bodies[1])


if __name__ == "__main__":
    unittest.main()
