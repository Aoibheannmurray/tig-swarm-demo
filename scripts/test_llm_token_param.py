#!/usr/bin/env python3
"""Self-running tests for OpenAI-family token-parameter selection.

No pytest in this repo (see scripts/CLAUDE.md) — run directly:

    python scripts/test_llm_token_param.py

The o-series and gpt-5 models reject `max_tokens` and require
`max_completion_tokens` (plus the `developer` system role). `call_openai`
picks between them by prefix-matching the model id — which broke for gateway
ids of the form `publisher/name`. OpenRouter is a supported provider and the
README documents its ids as exactly that, so `openai/gpt-5` was matching
neither `^gpt-5` nor `^o\\d`: the request went out with `max_tokens` (rejected
by the API) and a 16k cap instead of 100k.

Covers:
  - bare and publisher-prefixed ids both resolve to the new-API payload
  - legacy ids keep `max_tokens` + the `system` role
  - the token cap is read off the base name, not the prefixed string
  - the retry fallback fires on the provider's "Unsupported parameter"
    complaint, for ids no prefix rule recognises
  - api_base is honoured without doubling the /v1 segment
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import llm_backends

_failures = 0


def check(cond: bool, label: str) -> None:
    global _failures
    if not cond:
        _failures += 1
    print(f"  [{'ok  ' if cond else 'FAIL'}] {label}")


def _capture(model: str, api_base: str | None = None) -> tuple[str, dict]:
    """Run call_openai against a stubbed transport, returning (content, request)."""
    captured: dict = {}
    original = llm_backends._post_json

    def fake_post_json(url, body, headers):
        captured.update(url=url, body=body, headers=headers)
        return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    llm_backends._post_json = fake_post_json
    try:
        content, _usage = llm_backends.call_openai("sys", "prompt", model, "key", api_base)
    finally:
        llm_backends._post_json = original
    return content, captured


def test_model_name_normalisation() -> None:
    print("model-name normalisation")
    check(llm_backends._base_model_name("openai/gpt-5") == "gpt-5",
          "publisher prefix stripped")
    check(llm_backends._base_model_name("gpt-5") == "gpt-5",
          "bare id unchanged")
    for mid in ("gpt-5", "openai/gpt-5", "o3", "openai/o3"):
        check(llm_backends._needs_new_api(mid), f"{mid} -> new API")
    check(not llm_backends._needs_new_api("gpt-4o"), "gpt-4o -> legacy API")
    # The regression: the cap was read off the raw string, so a prefixed id
    # silently fell through to the default instead of the model's real limit.
    check(llm_backends._max_tokens_for_model("openai/gpt-5")
          == llm_backends._max_tokens_for_model("gpt-5"),
          "prefixed id gets the same token cap as the bare id")


def test_new_api_payload() -> None:
    print("new-API payload")
    content, cap = _capture("gpt-5.5")
    check(content == "ok", "content returned")
    check("max_completion_tokens" in cap["body"], "uses max_completion_tokens")
    check("max_tokens" not in cap["body"], "does not send max_tokens")
    check(cap["body"]["messages"][0]["role"] == "developer", "developer role")

    _, cap = _capture("openai/gpt-5.5", api_base="https://example.test/openai-compatible")
    check(cap["url"] == "https://example.test/openai-compatible/v1/chat/completions",
          "api_base honoured without doubling /v1")
    check("max_completion_tokens" in cap["body"],
          "publisher-prefixed id uses max_completion_tokens")
    check(cap["body"]["max_completion_tokens"] == 100000,
          "publisher-prefixed id gets the full 100k cap, not the default")
    check(cap["body"]["messages"][0]["role"] == "developer",
          "publisher-prefixed id gets the developer role")


def test_legacy_payload_unchanged() -> None:
    print("legacy payload")
    _, cap = _capture("gpt-4o")
    check("max_tokens" in cap["body"], "uses max_tokens")
    check("max_completion_tokens" not in cap["body"], "no max_completion_tokens")
    check(cap["body"]["messages"][0]["role"] == "system", "system role")


def test_retry_on_unsupported_parameter() -> None:
    """Fallback for ids no prefix rule knows: the provider says which parameter
    it wants, so retry once rather than failing the whole iteration."""
    print("unsupported-parameter retry")
    bodies: list[dict] = []
    original = llm_backends._post_json

    def fake_post_json(_url, body, _headers):
        bodies.append(body)
        if len(bodies) == 1:
            raise RuntimeError(
                "HTTP 400: Unsupported parameter: 'max_tokens' is not supported "
                "with this model. Use 'max_completion_tokens' instead."
            )
        return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    llm_backends._post_json = fake_post_json
    try:
        content, _usage = llm_backends.call_openai("sys", "prompt", "future-model", "key")
    finally:
        llm_backends._post_json = original

    check(content == "ok", "retry succeeds")
    check("max_tokens" in bodies[0], "first attempt sends max_tokens")
    check("max_completion_tokens" in bodies[1], "retry sends max_completion_tokens")

    # An unrelated failure must not be swallowed by the retry path.
    def always_fails(_url, _body, _headers):
        raise RuntimeError("HTTP 500: upstream exploded")

    llm_backends._post_json = always_fails
    try:
        llm_backends.call_openai("sys", "prompt", "gpt-4o", "key")
        check(False, "unrelated error propagates")
    except RuntimeError as e:
        check("upstream exploded" in str(e), "unrelated error propagates")
    finally:
        llm_backends._post_json = original


def main() -> int:
    test_model_name_normalisation()
    test_new_api_payload()
    test_legacy_payload_unchanged()
    test_retry_on_unsupported_parameter()
    print()
    if _failures:
        print(f"FAILED: {_failures} check(s) failed")
        return 1
    print("all token-parameter checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
