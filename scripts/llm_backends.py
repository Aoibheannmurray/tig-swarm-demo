"""LLM provider backends — raw urllib, no SDK dependencies.

Supports Anthropic (Claude), OpenAI (GPT / o-series), and Google (Gemini).
The --api-base flag on the OpenAI provider works with any OpenAI-compatible
endpoint (Together, Groq, DeepSeek, Ollama, etc.).
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


def _base_model_name(model: str) -> str:
    return model.rsplit("/", 1)[-1]


def _max_tokens_for_model(model: str) -> int:
    m = _base_model_name(model).lower()
    for prefix, limit in _MODEL_MAX_TOKENS:
        if m.startswith(prefix):
            return limit
    return _DEFAULT_MAX_TOKENS


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
    return data["content"][0]["text"]


def _needs_new_api(model: str) -> bool:
    """Models that require max_completion_tokens and developer role."""
    base_model = _base_model_name(model)
    if re.match(r"^o\d", base_model, re.IGNORECASE):
        return True
    if re.match(r"^gpt-5", base_model, re.IGNORECASE):
        return True
    return False


def _is_max_tokens_unsupported(error: str) -> bool:
    return (
        "Unsupported parameter" in error
        and "'max_tokens'" in error
        and "max_completion_tokens" in error
    )


def _openai_chat_body(
    system: str,
    prompt: str,
    model: str,
    token_param: str,
    new_api: bool,
) -> dict:
    messages = (
        [{"role": "developer", "content": system},
         {"role": "user", "content": prompt}]
        if new_api else
        [{"role": "system", "content": system},
         {"role": "user", "content": prompt}]
    )
    return {
        "model": model,
        "messages": messages,
        token_param: _max_tokens_for_model(model),
    }


def call_openai(
    system: str, prompt: str, model: str, api_key: str,
    api_base: str | None = None,
) -> str:
    base = (api_base or "https://api.openai.com").rstrip("/")
    new_api = _needs_new_api(model)
    token_param = "max_completion_tokens" if new_api else "max_tokens"
    url = f"{base}/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    try:
        data = _post_json(
            url,
            _openai_chat_body(system, prompt, model, token_param, new_api),
            headers,
        )
    except RuntimeError as e:
        if token_param != "max_tokens" or not _is_max_tokens_unsupported(str(e)):
            raise
        data = _post_json(
            url,
            _openai_chat_body(system, prompt, model, "max_completion_tokens", new_api),
            headers,
        )
    return data["choices"][0]["message"]["content"]


def call_claude_code(
    system: str, prompt: str, model: str | None = None,
) -> str:
    cmd = ["claude", "--bare", "-p", "--system-prompt", system, "--max-output-tokens", str(_max_tokens_for_model(model or ""))]
    if model:
        cmd += ["--model", model]
    result = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True, timeout=_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude -p failed (exit {result.returncode}): {result.stderr[:800]}")
    return result.stdout


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
            "generationConfig": {"maxOutputTokens": _max_tokens_for_model(model)},
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
    if provider == "claude-code":
        return call_claude_code(system, prompt, model)
    if provider == "anthropic":
        return call_anthropic(system, prompt, model, api_key)
    if provider == "openai":
        return call_openai(system, prompt, model, api_key, api_base)
    if provider == "google":
        return call_google(system, prompt, model, api_key)
    raise ValueError(f"Unknown provider: {provider}")
