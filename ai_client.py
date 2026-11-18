"""AI model client — Groq primary, Gemini fallback.

/ask and /watch resolve plain words through a model with a
single method: generate_content(prompt) -> a response with
.text. Groq is the primary (free tier, no card: a key from
console.groq.com, GROQ_API_KEY in .env) because Gemini kept
failing /ask parses (18 Nov 2026: "football match" ->
"I couldn't understand that right now"); Gemini stays as the
fallback so a Groq outage or a missing key never leaves /ask
with no model at all. No new pip dependency: Groq is called
over its OpenAI-compatible endpoint with the standard library
(urllib).
"""
import json
import logging
import urllib.error
import urllib.request
from types import SimpleNamespace

from config import GEMINI_API_KEY, GEMINI_MODEL, GROQ_API_KEY, GROQ_MODEL

log = logging.getLogger("ai")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Free-tier chat models (Nov 2026). The old Llama 3.1 8B /
# 3.3 70B moved to Enterprise in Aug 2026 — never use them.
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"


class GroqError(RuntimeError):
    """Groq answered with an error status or an unusable body
    (429, timeout, empty reply). The chained model treats this
    as "fall back to Gemini"."""


class GroqModel:
    """generate_content(prompt) over Groq's OpenAI-compatible
    chat endpoint. response_format json_object enforces the
    STRICT JSON the parsers expect; temperature 0 keeps the
    parse deterministic. Raises GroqError on any failure."""

    def __init__(self, api_key, model=None, timeout=10.0):
        self.api_key = api_key
        self.model = model or DEFAULT_GROQ_MODEL
        self.timeout = timeout

    def generate_content(self, prompt):
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 512,
            "response_format": {"type": "json_object"},
        }).encode("utf-8")
        req = urllib.request.Request(
            GROQ_URL, data=body, method="POST",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Log the status + a short slice of the server's error
            # body so a 429 / 400 is diagnosable from the journal
            # (18 Nov 2026: Groq failed at the same moment as a
            # Discord timeout and left only "GroqError" behind).
            # The body is the server's error JSON — it cannot
            # contain the API key, which only ever travels in the
            # request header. Truncated so a long body can't flood
            # the log.
            try:
                detail = exc.read().decode("utf-8", "replace").strip()[:200]
            except Exception:
                detail = ""
            if not detail:
                detail = str(getattr(exc, "msg", "") or "")
            log.warning("[ai] Groq HTTP %s: %s", exc.code, detail)
            raise GroqError(f"Groq HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError,
                json.JSONDecodeError) as exc:
            raise GroqError(
                f"Groq call failed ({type(exc).__name__})") from exc
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise GroqError("Groq reply had no message content") from exc
        if not isinstance(text, str) or not text.strip():
            raise GroqError("Groq reply was empty")
        return SimpleNamespace(text=text.strip())


class ChainedModel:
    """One generate_content(prompt): primary first, fallback on
    any failure. .provider names the provider that answered the
    last call ("groq" / "gemini") so the journal shows which
    one actually parsed the request."""

    def __init__(self, primary, primary_name, fallback=None,
                 fallback_name=None):
        self.primary = primary
        self.primary_name = primary_name
        self.fallback = fallback
        self.fallback_name = fallback_name
        self.provider = primary_name

    def generate_content(self, prompt):
        try:
            resp = self.primary.generate_content(prompt)
        except Exception as exc:
            # Log the failure itself, not just its type: the
            # GroqError message now carries the HTTP status + a
            # short error body, which is what diagnoses a 429.
            # Groq is called exactly once per request — no retry
            # here, the fallback is the only second chance.
            log.warning("[ai] %s failed (%s) — trying %s",
                        self.primary_name,
                        _short_error(exc),
                        self.fallback_name or "nothing")
            if self.fallback is None:
                log.error("[ai] both providers failed (%s; no "
                          "fallback configured)", _short_error(exc))
                raise
            try:
                resp = self.fallback.generate_content(prompt)
            except Exception as fb_exc:
                log.error("[ai] both providers failed (%s then %s)",
                          _short_error(exc), _short_error(fb_exc))
                raise
            self.provider = self.fallback_name
            log.info("[ai] fell back to %s", self.fallback_name)
            return resp
        self.provider = self.primary_name
        log.info("[ai] answered by %s", self.primary_name)
        return resp


def _short_error(exc: Exception) -> str:
    """One short log-safe line for an exception: its message
    truncated, or its type name when it has no message."""
    text = str(exc).strip()
    return (text[:200] if text else type(exc).__name__)


def build_ai_model():
    """The model bot.py hands to /ask and /watch: Groq primary
    when GROQ_API_KEY is set, Gemini fallback when GEMINI_API_KEY
    is set. None when neither key is set — the callers already
    answer plainly without a model."""
    primary = None
    primary_name = None
    fallback = None
    fallback_name = None
    if GROQ_API_KEY:
        primary = GroqModel(GROQ_API_KEY, GROQ_MODEL)
        primary_name = "groq"
    if GEMINI_API_KEY:
        import google.generativeai as genai

        genai.configure(api_key=GEMINI_API_KEY)
        gemini = genai.GenerativeModel(GEMINI_MODEL)
        if primary is None:
            primary, primary_name = gemini, "gemini"
        else:
            fallback, fallback_name = gemini, "gemini"
    if primary is None:
        log.warning("[ai] no AI provider configured "
                    "(set GROQ_API_KEY or GEMINI_API_KEY)")
        return None
    log.info("[ai] primary=%s fallback=%s model=%s", primary_name,
             fallback_name or "none",
             GROQ_MODEL if primary_name == "groq" else GEMINI_MODEL)
    return ChainedModel(primary, primary_name, fallback, fallback_name)
