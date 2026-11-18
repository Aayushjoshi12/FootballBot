"""Groq AI upgrade (Nov 2026): Groq is the primary /ask parser,
Gemini the fallback. No network, no real keys — urllib is stubbed
and the Gemini model is a fake object.

Run:  python3 -m pytest tests/test_groq.py -q
from the footballbot directory.
"""
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_client as ac


class FakeResponse:
    """Stub for urllib's response: a context manager with read()."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _groq_payload(content='{"intent": "LIVE_MATCHES"}'):
    return {"choices": [{"message": {"content": content}}]}


class FakeFallback:
    """Stand-in for the Gemini model: generate_content returns
    whatever text it was constructed with."""

    def __init__(self, text='{"intent": "HELP"}'):
        self.text = text
        self.calls = 0

    def generate_content(self, prompt: str):
        self.calls += 1
        return SimpleNamespace(text=self.text)


class TestGroqModel(unittest.TestCase):
    def test_posts_json_object_and_returns_text(self):
        with patch("urllib.request.urlopen",
                   return_value=FakeResponse(_groq_payload())) as mock_open:
            resp = ac.GroqModel("test-key").generate_content(
                '{"intent": "LIVE_MATCHES"}')
        self.assertEqual(resp.text, '{"intent": "LIVE_MATCHES"}')
        req = mock_open.call_args[0][0]
        self.assertEqual(req.full_url, ac.GROQ_URL)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["model"], "openai/gpt-oss-120b")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["messages"],
                         [{"role": "user",
                           "content": '{"intent": "LIVE_MATCHES"}'}])

    def test_model_override_is_used(self):
        with patch("urllib.request.urlopen",
                   return_value=FakeResponse(_groq_payload())) as mock_open:
            ac.GroqModel("k", model="openai/gpt-oss-20b").generate_content("x")
        body = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(body["model"], "openai/gpt-oss-20b")

    def test_http_429_raises_groq_error(self):
        err = urllib.error.HTTPError(ac.GROQ_URL, 429, "Too Many Requests",
                                     {}, None)
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(ac.GroqError) as ctx:
                ac.GroqModel("k").generate_content("x")
        self.assertIn("429", str(ctx.exception))

    def test_http_error_logs_status_and_body_without_key(self):
        # 18 Nov 2026: Groq failed at the same moment as a
        # Discord timeout and left only "GroqError" behind. The
        # journal must carry the status + a short error body —
        # and never the API key.
        body = b'{"error": {"message": "Rate limit exceeded"}}'
        err = urllib.error.HTTPError(ac.GROQ_URL, 429, "Too Many Requests",
                                     {}, io.BytesIO(body))
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertLogs("ai", level="WARNING") as logs:
                with self.assertRaises(ac.GroqError) as ctx:
                    ac.GroqModel("gsk-secret-test-key").generate_content("x")
        output = "\n".join(logs.output)
        self.assertIn("429", output)
        self.assertIn("Rate limit exceeded", output)
        self.assertNotIn("gsk-secret-test-key", output)
        self.assertIn("429", str(ctx.exception))
        self.assertIn("Rate limit exceeded", str(ctx.exception))
        self.assertNotIn("gsk-secret-test-key", str(ctx.exception))

    def test_empty_content_raises_groq_error(self):
        with patch("urllib.request.urlopen",
                   return_value=FakeResponse(_groq_payload(""))):
            with self.assertRaises(ac.GroqError):
                ac.GroqModel("k").generate_content("x")

    def test_missing_content_raises_groq_error(self):
        with patch("urllib.request.urlopen",
                   return_value=FakeResponse({"choices": []})):
            with self.assertRaises(ac.GroqError):
                ac.GroqModel("k").generate_content("x")


class TestChainedModel(unittest.TestCase):
    def test_groq_success_never_calls_fallback(self):
        groq = FakeFallback('{"intent": "LIVE_MATCHES"}')
        fallback = FakeFallback()
        model = ac.ChainedModel(groq, "groq", fallback, "gemini")
        resp = model.generate_content("football match")
        self.assertEqual(resp.text, '{"intent": "LIVE_MATCHES"}')
        self.assertEqual(model.provider, "groq")
        self.assertEqual(fallback.calls, 0)

    def test_groq_429_falls_back_to_gemini(self):
        groq = FakeFallback()
        groq.generate_content = lambda prompt: (_ for _ in ()).throw(
            ac.GroqError("Groq HTTP 429"))
        fallback = FakeFallback('{"intent": "HELP"}')
        model = ac.ChainedModel(groq, "groq", fallback, "gemini")
        resp = model.generate_content("help")
        self.assertEqual(resp.text, '{"intent": "HELP"}')
        self.assertEqual(model.provider, "gemini")
        self.assertEqual(fallback.calls, 1)

    def test_groq_error_without_fallback_raises(self):
        groq = FakeFallback()
        groq.generate_content = lambda prompt: (_ for _ in ()).throw(
            ac.GroqError("Groq HTTP 500"))
        model = ac.ChainedModel(groq, "groq")
        with self.assertRaises(ac.GroqError):
            model.generate_content("help")

    def test_both_providers_failing_logs_both_failed(self):
        # 18 Nov 2026: Groq raised GroqError and Gemini raised
        # ResourceExhausted at the same moment — the journal must
        # say both failed, not just the last one.
        groq = FakeFallback()
        groq.generate_content = lambda prompt: (_ for _ in ()).throw(
            ac.GroqError("Groq HTTP 429: Rate limit exceeded"))
        fallback = FakeFallback()
        fallback.generate_content = lambda prompt: (_ for _ in ()).throw(
            RuntimeError("ResourceExhausted: quota exceeded"))
        model = ac.ChainedModel(groq, "groq", fallback, "gemini")
        with self.assertLogs("ai", level="ERROR") as logs:
            with self.assertRaises(RuntimeError):
                model.generate_content("help")
        output = "\n".join(logs.output)
        self.assertIn("both providers failed", output)
        self.assertIn("429", output)
        self.assertIn("ResourceExhausted", output)


class TestBuildAiModel(unittest.TestCase):
    def test_no_keys_returns_none(self):
        with patch.object(ac, "GROQ_API_KEY", ""), \
                patch.object(ac, "GEMINI_API_KEY", ""):
            self.assertIsNone(ac.build_ai_model())

    def test_groq_only_is_primary(self):
        with patch.object(ac, "GROQ_API_KEY", "groq-key"), \
                patch.object(ac, "GEMINI_API_KEY", ""):
            model = ac.build_ai_model()
        self.assertIsInstance(model, ac.ChainedModel)
        self.assertEqual(model.primary_name, "groq")
        self.assertIsNone(model.fallback)
        self.assertIsInstance(model.primary, ac.GroqModel)
        self.assertEqual(model.primary.model, "openai/gpt-oss-120b")

    def test_both_keys_groq_primary_gemini_fallback(self):
        with patch.object(ac, "GROQ_API_KEY", "groq-key"), \
                patch.object(ac, "GEMINI_API_KEY", "gemini-key"):
            model = ac.build_ai_model()
        self.assertEqual(model.primary_name, "groq")
        self.assertEqual(model.fallback_name, "gemini")
        self.assertIsInstance(model.primary, ac.GroqModel)

    def test_gemini_only_still_works(self):
        with patch.object(ac, "GROQ_API_KEY", ""), \
                patch.object(ac, "GEMINI_API_KEY", "gemini-key"):
            model = ac.build_ai_model()
        self.assertEqual(model.primary_name, "gemini")
        self.assertIsNone(model.fallback)


if __name__ == "__main__":
    unittest.main()
