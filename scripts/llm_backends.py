"""LLM provider backends — raw urllib, no SDK dependencies.

Supports Anthropic (Claude), OpenAI (GPT / o-series), and Google (Gemini).
The --api-base flag on the OpenAI provider works with any OpenAI-compatible
endpoint (Together, Groq, DeepSeek, Ollama, etc.).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o",
    "google": "gemini-2.5-flash",
}

_TIMEOUT = 180
_MAX_TOKENS = 16384


def _post_json(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:800]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None


def call_anthropic(system: str, prompt: str, model: str, api_key: str) -> str:
    data = _post_json(
        "https://api.anthropic.com/v1/messages",
        {
            "model": model,
            "max_tokens": _MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        },
        {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    return data["content"][0]["text"]


def call_openai(
    system: str, prompt: str, model: str, api_key: str,
    api_base: str | None = None,
) -> str:
    base = (api_base or "https://api.openai.com").rstrip("/")
    data = _post_json(
        f"{base}/v1/chat/completions",
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": _MAX_TOKENS,
        },
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    return data["choices"][0]["message"]["content"]


def call_google(system: str, prompt: str, model: str, api_key: str) -> str:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model}:generateContent?key={api_key}"
    )
    data = _post_json(
        url,
        {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": _MAX_TOKENS},
        },
        {"Content-Type": "application/json"},
    )
    return data["candidates"][0]["content"]["parts"][0]["text"]


def call_llm(
    provider: str, model: str, api_key: str,
    system: str, prompt: str,
    api_base: str | None = None,
) -> str:
    """Dispatch to the appropriate provider. Returns raw text response."""
    if provider == "anthropic":
        return call_anthropic(system, prompt, model, api_key)
    if provider == "openai":
        return call_openai(system, prompt, model, api_key, api_base)
    if provider == "google":
        return call_google(system, prompt, model, api_key)
    raise ValueError(f"Unknown provider: {provider}")
