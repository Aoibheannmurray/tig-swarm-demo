"""LLM provider backends — raw urllib, no SDK dependencies.

Supports Anthropic (Claude), OpenAI (GPT / o-series), and Google (Gemini).
The --api-base flag on the OpenAI provider works with any OpenAI-compatible
endpoint (Together, Groq, DeepSeek, Ollama, etc.).

Each call_* function returns ``(text, usage)`` where ``usage`` is a dict
with ``input_tokens`` and ``output_tokens`` (both int, 0 when unavailable).
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o",
    "google": "gemini-2.5-flash",
}

Usage = dict[str, int]  # {"input_tokens": N, "output_tokens": N}

_ZERO_USAGE: Usage = {"input_tokens": 0, "output_tokens": 0}

_TIMEOUT = 180
_DEFAULT_MAX_TOKENS = 16384

_MODEL_MAX_TOKENS: list[tuple[str, int]] = [
    # Anthropic
    ("claude-opus-4", 32768),
    ("claude-sonnet-4", 16384),
    ("claude-haiku-4", 8192),
    ("claude-3-5-sonnet", 8192),
    ("claude-3-5-haiku", 8192),
    ("claude-3-opus", 4096),
    ("claude-3-sonnet", 4096),
    ("claude-3-haiku", 4096),
    # OpenAI
    ("o1", 100000),
    ("o3", 100000),
    ("o4-mini", 100000),
    ("gpt-5", 100000),
    ("gpt-4o", 16384),
    ("gpt-4-turbo", 4096),
    ("gpt-4", 8192),
    # Google
    ("gemini-2.5", 65536),
    ("gemini-2.0", 8192),
    ("gemini-1.5", 8192),
]


def _max_tokens_for_model(model: str) -> int:
    m = model.lower()
    for prefix, limit in _MODEL_MAX_TOKENS:
        if m.startswith(prefix):
            return limit
    return _DEFAULT_MAX_TOKENS


# Pricing per million tokens (input, output) in USD.
_PRICING: list[tuple[str, float, float]] = [
    ("claude-opus-4", 15.0, 75.0),
    ("claude-sonnet-4", 3.0, 15.0),
    ("claude-haiku-4", 0.80, 4.0),
    ("claude-3-5-sonnet", 3.0, 15.0),
    ("claude-3-5-haiku", 0.80, 4.0),
    ("o1", 15.0, 60.0),
    ("o3", 10.0, 40.0),
    ("o4-mini", 1.10, 4.40),
    ("gpt-5", 10.0, 40.0),
    ("gpt-4o", 2.50, 10.0),
    ("gpt-4-turbo", 10.0, 30.0),
    ("gemini-2.5-pro", 1.25, 10.0),
    ("gemini-2.5-flash", 0.15, 0.60),
    ("gemini-2.0", 0.10, 0.40),
    ("gemini-1.5", 1.25, 5.0),
]


def estimate_cost(model: str, usage: Usage) -> float:
    """Estimate USD cost from model name and token usage."""
    m = model.lower()
    for prefix, inp_price, out_price in _PRICING:
        if m.startswith(prefix):
            return (
                usage["input_tokens"] * inp_price / 1_000_000
                + usage["output_tokens"] * out_price / 1_000_000
            )
    return 0.0


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


def call_anthropic(system: str, prompt: str, model: str, api_key: str) -> tuple[str, Usage]:
    data = _post_json(
        "https://api.anthropic.com/v1/messages",
        {
            "model": model,
            "max_tokens": _max_tokens_for_model(model),
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            "messages": [{"role": "user", "content": prompt}],
        },
        {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    usage = data.get("usage") or {}
    return data["content"][0]["text"], {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
    }


def _needs_new_api(model: str) -> bool:
    """Models that require max_completion_tokens and developer role."""
    if re.match(r"^o\d", model, re.IGNORECASE):
        return True
    if re.match(r"^gpt-5", model, re.IGNORECASE):
        return True
    return False


def call_openai(
    system: str, prompt: str, model: str, api_key: str,
    api_base: str | None = None,
) -> tuple[str, Usage]:
    base = (api_base or "https://api.openai.com").rstrip("/")
    new_api = _needs_new_api(model)
    token_param = "max_completion_tokens" if new_api else "max_tokens"
    messages = (
        [{"role": "developer", "content": system},
         {"role": "user", "content": prompt}]
        if new_api else
        [{"role": "system", "content": system},
         {"role": "user", "content": prompt}]
    )
    data = _post_json(
        f"{base}/v1/chat/completions",
        {
            "model": model,
            "messages": messages,
            token_param: _max_tokens_for_model(model),
        },
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    usage = data.get("usage") or {}
    return data["choices"][0]["message"]["content"], {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
    }


def call_claude_code(
    system: str, prompt: str, model: str | None = None,
) -> tuple[str, Usage]:
    cmd = ["claude", "--bare", "-p", "--system-prompt", system, "--max-output-tokens", str(_max_tokens_for_model(model or ""))]
    if model:
        cmd += ["--model", model]
    result = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True, timeout=_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude -p failed (exit {result.returncode}): {result.stderr[:800]}")
    return result.stdout, dict(_ZERO_USAGE)


def call_google(system: str, prompt: str, model: str, api_key: str) -> tuple[str, Usage]:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model}:generateContent?key={api_key}"
    )
    data = _post_json(
        url,
        {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": _max_tokens_for_model(model)},
        },
        {"Content-Type": "application/json"},
    )
    um = data.get("usageMetadata") or {}
    return data["candidates"][0]["content"]["parts"][0]["text"], {
        "input_tokens": um.get("promptTokenCount", 0),
        "output_tokens": um.get("candidatesTokenCount", 0),
    }


def call_llm(
    provider: str, model: str, api_key: str,
    system: str, prompt: str,
    api_base: str | None = None,
) -> tuple[str, Usage]:
    """Dispatch to the appropriate provider. Returns (text, usage)."""
    if provider == "claude-code":
        return call_claude_code(system, prompt, model)
    if provider == "anthropic":
        return call_anthropic(system, prompt, model, api_key)
    if provider == "openai":
        return call_openai(system, prompt, model, api_key, api_base)
    if provider == "google":
        return call_google(system, prompt, model, api_key)
    raise ValueError(f"Unknown provider: {provider}")
