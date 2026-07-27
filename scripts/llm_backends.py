"""LLM provider backends — raw urllib, no SDK dependencies.

Supports Anthropic (Claude), OpenAI (GPT / o-series), Google (Gemini),
Venice (https://venice.ai), and OpenRouter (https://openrouter.ai). The
--api-base flag on the OpenAI provider works with any OpenAI-compatible
endpoint (Together, Groq, DeepSeek, Ollama, etc.). Venice and OpenRouter
are first-class shortcuts — both are `call_openai` under the hood with
their respective base URLs pre-filled.

Each call_* function returns ``(text, usage)`` where ``usage`` is a dict
with ``input_tokens`` and ``output_tokens`` (both int, 0 when unavailable).
"""

from __future__ import annotations

import ipaddress
import json
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o",
    "google": "gemini-2.5-flash",
    "venice": "zai-org-glm-5",
    "openrouter": "qwen/qwen3-coder",
}

VENICE_API_BASE = "https://api.venice.ai/api/v1"
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"

# Hostnames that can only mean "a machine on this network" — the rest is
# decided by the address itself (see is_local_api_base).
_LOCAL_HOST_SUFFIXES = (".local", ".internal", ".lan", ".home.arpa")


def is_local_api_base(api_base: str | None) -> bool:
    """Does this OpenAI-compatible endpoint live on the caller's own machine
    or private network?

    Self-hosted inference servers (llama.cpp, vLLM, Ollama, LM Studio) ship
    with no authentication by default, so a missing API key is the normal
    case for them and a misconfiguration for a public provider. Callers use
    this to tell those two apart instead of refusing to launch either.
    """
    if not api_base:
        return False
    raw = api_base.strip()
    if "://" not in raw:
        raw = "http://" + raw  # bare "127.0.0.1:8000" has no scheme to split on
    host = (urllib.parse.urlsplit(raw).hostname or "").strip().lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(_LOCAL_HOST_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # a public DNS name
    return ip.is_loopback or ip.is_private or ip.is_link_local

Usage = dict[str, int]  # {"input_tokens": N, "output_tokens": N}

_ZERO_USAGE: Usage = {"input_tokens": 0, "output_tokens": 0}

_TIMEOUT = 1800
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
    # OpenRouter list price for qwen3-coder (480B A35B). Routed providers vary,
    # so this is a representative estimate, not a billed figure.
    ("qwen3-coder", 0.22, 1.80),
    ("qwen", 0.22, 1.80),
]


def estimate_cost(model: str, usage: Usage) -> float:
    """Estimate USD cost from model name and token usage."""
    m = model.lower()
    # OpenRouter / Together-style "publisher/model" names (e.g.
    # "anthropic/claude-3.5-sonnet") — strip the publisher segment before
    # the prefix lookup so routed Anthropic / OpenAI / Google models still
    # price correctly. Non-routed models (no slash) are unaffected.
    if "/" in m:
        m = m.split("/", 1)[1]
    # Normalize dot vs dash separators on BOTH sides so the matcher
    # tolerates either spelling (OpenRouter uses `claude-3.5-sonnet`
    # while Anthropic's API uses `claude-3-5-sonnet`; Google uses
    # `gemini-2.5-pro` natively too).
    m_norm = m.replace(".", "-")
    for prefix, inp_price, out_price in _PRICING:
        if m_norm.startswith(prefix.replace(".", "-")):
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


def _get_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:800]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None


# CLI-auth providers have no HTTP models endpoint to query. Codex does expose
# the model catalog it currently sees through `codex debug models`; Claude has
# no equivalent machine-readable command.
_CLI_PROVIDERS = ("claude-code", "claude-code-agentic", "codex-agentic")


def _parse_codex_model_catalog(raw: str) -> list[str]:
    """Return visible Codex model slugs in the CLI's preferred order."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Codex CLI returned invalid model JSON: {exc}") from None
    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise RuntimeError("Codex CLI model catalog has no `models` list")

    ranked: list[tuple[int, int, str]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.get("visibility", "list") != "list":
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        priority = entry.get("priority")
        ranked.append((priority if isinstance(priority, int) else 1_000_000,
                       index, slug.strip()))

    # Preserve the best-ranked occurrence if a malformed catalog repeats a slug.
    seen: set[str] = set()
    models: list[str] = []
    for _, _, slug in sorted(ranked):
        if slug not in seen:
            seen.add(slug)
            models.append(slug)
    if not models:
        raise RuntimeError("Codex CLI model catalog contains no visible models")
    return models


def _list_codex_cli_models() -> list[str]:
    """Ask the installed Codex CLI for its refreshed, account-aware catalog."""
    # Reuse the runner's cross-platform CLI resolution, including CODEX_CLI and
    # Windows npm .cmd shims, so setup and actual fleet execution see one binary.
    from agentic_backends import CodexAgent, _scrubbed_env, _wrap_for_windows

    codex_bin = CodexAgent().resolve_cli()
    if codex_bin is None:
        raise RuntimeError("Codex CLI is not installed or is not on PATH")
    cmd = _wrap_for_windows([codex_bin, "debug", "models"])
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            env=_scrubbed_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"could not run `codex debug models`: {exc}") from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-800:]
        raise RuntimeError(
            f"`codex debug models` exited {result.returncode}"
            + (f": {detail}" if detail else "")
        )
    return _parse_codex_model_catalog(result.stdout)


def list_models(
    provider: str, api_key: str | None = None, api_base: str | None = None,
) -> list[str]:
    """Return a sorted list of model IDs `provider` exposes via its API.

    Hits the provider's `/models` (or Gemini's `models`) endpoint live, so
    the result is whatever the account actually has access to today. Most
    providers require `api_key`; OpenRouter's catalog is public so the key
    is optional there.

    For Codex CLI, asks the installed CLI for its current model catalog.
    Raises ``ValueError`` for Claude CLI providers (no endpoint exists) and
    ``RuntimeError`` (with the HTTP status / body) on an API error — typically
    a missing or invalid key."""
    if provider == "codex-agentic":
        return _list_codex_cli_models()
    if provider in _CLI_PROVIDERS:
        raise ValueError(
            f"{provider} authenticates through its own CLI, not an HTTP API, "
            "so there is no models endpoint to query. It accepts any model ID "
            "the CLI knows; choose one of the recommended models or enter a "
            "custom model ID."
        )

    if provider == "google":
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models"
            f"?key={api_key or ''}&pageSize=1000"
        )
        data = _get_json(url, {})
        ids: list[str] = []
        for m in data.get("models", []):
            # Only models that can answer a generateContent call are usable as
            # an agent backend — skip embedding / tuning-only entries.
            if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                continue
            name = m.get("name", "")
            ids.append(name[len("models/"):] if name.startswith("models/") else name)
        return sorted(set(ids))

    if provider == "anthropic":
        data = _get_json(
            "https://api.anthropic.com/v1/models?limit=1000",
            {"x-api-key": api_key or "", "anthropic-version": "2023-06-01"},
        )
        return sorted(m["id"] for m in data.get("data", []) if m.get("id"))

    # OpenAI-compatible providers (openai, venice, openrouter, plus any
    # OpenAI-compatible gateway passed via api_base) all share the /models shape.
    if provider == "venice":
        base = api_base or VENICE_API_BASE
    elif provider == "openrouter":
        base = api_base or OPENROUTER_API_BASE
    elif provider == "openai":
        base = api_base or "https://api.openai.com"
    else:
        raise ValueError(f"Unknown provider: {provider}")
    base = base.rstrip("/")
    url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    data = _get_json(url, headers)
    return sorted(m.get("id", "") for m in data.get("data", []) if m.get("id"))


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
    # Guard the body shape: an empty `content` list (e.g. the model stopped
    # before emitting any block, or the request hit max_tokens mid-thought)
    # would otherwise surface as an opaque IndexError. Mirror the OpenAI
    # path: raise an informative error the caller logs and retries.
    text = ""
    for block in data.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text") or ""
            break
    if not text.strip():
        stop = data.get("stop_reason") or "unknown"
        raise RuntimeError(
            f"Anthropic returned an empty completion (stop_reason={stop})"
        )
    return text, {
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
    # Tolerate api_base supplied with or without a trailing /v1. Third-party
    # docs (Venice, etc.) usually quote the /v1-suffixed URL — pasting that
    # straight in shouldn't double up the version segment.
    chat_url = (
        f"{base}/chat/completions" if base.endswith("/v1")
        else f"{base}/v1/chat/completions"
    )
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
        chat_url,
        {
            "model": model,
            "messages": messages,
            token_param: _max_tokens_for_model(model),
        },
        # No key, no Authorization header: a self-hosted server that checks
        # nothing would still see `Bearer ` and some reject the empty value
        # outright. Same rule list_models applies.
        {"Content-Type": "application/json",
         **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
    )
    # OpenRouter (and some gateways) return provider/moderation errors in the
    # body with HTTP 200 rather than a 4xx/5xx, so _post_json doesn't raise.
    # Surface them as a real error instead of silently treating it as empty.
    if isinstance(data.get("error"), dict):
        err = data["error"]
        raise RuntimeError(f"provider error {err.get('code', '?')}: {err.get('message', err)}")

    usage = data.get("usage") or {}
    choice = data["choices"][0]
    msg = choice.get("message") or {}
    content = msg.get("content") or ""
    if not content.strip():
        # Empty completion with no error. Three common culprits, and
        # finish_reason tells them apart, so include it (plus a hint about a
        # reasoning-only reply) rather than swallowing a bare "". The caller
        # treats this like any transport failure: log it and retry.
        #   - "length": ran out of output budget (often a reasoning model that
        #     spent the whole cap thinking — note if a `reasoning` field exists)
        #   - "content_filter": the provider's moderation blocked the output
        #   - "stop"/None: the routed upstream returned an empty body (flaky)
        reason = choice.get("finish_reason") or choice.get("native_finish_reason") or "unknown"
        had_reasoning = bool((msg.get("reasoning") or "").strip())
        detail = f"finish_reason={reason}"
        if had_reasoning:
            detail += " (model emitted reasoning but no answer — likely hit the output-token cap)"
        raise RuntimeError(f"model returned an empty completion ({detail})")

    return content, {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
    }


def call_claude_code(
    system: str, prompt: str, model: str | None = None,
) -> tuple[str, Usage]:
    # Resolve the CLI the same way the agentic backend does so this single-shot
    # path works on Windows too. npm installs Claude Code as `claude.cmd`
    # (there's no bare `claude.exe`), and `subprocess.run(["claude", …])` with
    # shell=False can't launch a `.cmd` — it raises `[WinError 2] The system
    # cannot find the file specified` every iteration. `_resolve_cli` honors a
    # `CLAUDE_CLI` override plus the `.cmd`/`.exe` fallbacks, and
    # `_wrap_for_windows` routes a `.cmd`/`.bat` through `cmd.exe` so subprocess
    # can execute it. On macOS/Linux this just returns the absolute `claude`
    # path, so behavior is unchanged there.
    from agentic_backends import _resolve_cli, _wrap_for_windows

    claude_bin = _resolve_cli("claude", "CLAUDE_CLI") or "claude"
    # Run from a temp dir so the CLI's CLAUDE.md auto-discovery doesn't pull
    # this repo's docs into every prompt — we supply our own system prompt.
    # We can't use --bare to disable discovery because --bare also disables
    # OAuth and forces ANTHROPIC_API_KEY, defeating the point of the
    # subscription path.
    with tempfile.TemporaryDirectory() as cwd:
        # Pass the system prompt via a file, not `--system-prompt <arg>`. It
        # embeds the full CHALLENGE.md and routinely runs ~8-9 KB, which blows
        # past Windows' cmd.exe command-line limit (8191 chars) once
        # _wrap_for_windows routes the .cmd through cmd.exe — surfacing as
        # `claude -p failed (exit 1): Command line too long`. A file path keeps
        # the command line tiny on every platform. (Named system-prompt.txt,
        # not CLAUDE.md, so it isn't picked up by auto-discovery.)
        sys_file = Path(cwd) / "system-prompt.txt"
        sys_file.write_text(system, encoding="utf-8")
        # --tools "" disables all built-in harness tools (Read/Write/Bash/etc.).
        # Without this, Opus sees Write in scope, treats "return the file" as
        # "write the file," and produces chatty preamble like "It looks like file
        # write permissions need to be granted..." which then breaks `cargo build`.
        cmd = [claude_bin, "-p", "--system-prompt-file", str(sys_file), "--tools", ""]
        if model:
            cmd += ["--model", model]
        cmd = _wrap_for_windows(cmd)
        result = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            timeout=_TIMEOUT, cwd=cwd,
        )
    if result.returncode != 0:
        # `claude` writes auth/usage errors to stdout, so include both streams.
        err = (result.stderr or result.stdout or "").strip()[:800]
        raise RuntimeError(f"claude -p failed (exit {result.returncode}): {err}")
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
    # Guard the body shape: a safety-blocked prompt has no candidates at all,
    # and a blocked/empty candidate can lack content.parts — either way the
    # old chained indexing raised an opaque IndexError/KeyError. Mirror the
    # OpenAI path: raise an informative error the caller logs and retries.
    candidates = data.get("candidates") or []
    candidate = candidates[0] if candidates else {}
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(
        p.get("text", "") for p in parts if isinstance(p, dict)
    )
    if not text.strip():
        finish = candidate.get("finishReason") or "unknown"
        detail = f"finishReason={finish}"
        block = (data.get("promptFeedback") or {}).get("blockReason")
        if block:
            detail += f", blockReason={block}"
        raise RuntimeError(f"Google returned an empty completion ({detail})")
    return text, {
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
    if provider == "venice":
        return call_openai(system, prompt, model, api_key, api_base or VENICE_API_BASE)
    if provider == "openrouter":
        return call_openai(system, prompt, model, api_key, api_base or OPENROUTER_API_BASE)
    if provider == "google":
        return call_google(system, prompt, model, api_key)
    raise ValueError(f"Unknown provider: {provider}")
