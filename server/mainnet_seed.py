"""Fetch the top-adoption TIG mainnet algorithm for a challenge and reshape it
into the swarm's format — SERVER-SIDE, so the Admin Console (which talks only
to this server, not the host's companion) can seed a running swarm from
mainnet.

Self-contained on purpose: the production image copies only `server/`, so this
must not import anything from `scripts/` or `hostadmin/`. It is a lean port of
the host-side path (`hostadmin.swarm._reshaped_mainnet_algo`,
`scripts/download_algorithm.fetch_algorithm`,
`scripts/challenge_files.reshape_mainnet_for_swarm`) — keep the two in rough
sync when the mainnet API / reshape rules change.

Pure `urllib` HTTP + string manipulation; no third-party deps.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

_MAINNET_API = "https://mainnet-api.tig.foundation"
_GH_REPO = "tig-foundation/tig-monorepo"
_GH_API = f"https://api.github.com/repos/{_GH_REPO}"
# File CONTENTS come from the raw CDN, not the REST API: raw.githubusercontent
# serves public files without drawing on the API quota. Only the one tree
# listing per algorithm costs an API request — this is what keeps a 5-challenge
# seeding run inside the anonymous 60-requests/hour budget (which is per
# egress IP, shared with strangers on a PaaS like Railway).
_GH_RAW = f"https://raw.githubusercontent.com/{_GH_REPO}"
_HTTP_TIMEOUT = 8

_OPTIMIZER_HOOK_CHALLENGES = {"neuralnet_optimizer"}
_HARNESS_OWNED_FNS = ("solve_challenge", "training_loop")
_OPTIMIZER_HOOKS = (
    "fn optimizer_init_state(",
    "fn optimizer_query_at_params(",
    "fn optimizer_step(",
)


class MainnetSeedError(Exception):
    """Raised for an unrecoverable fetch/reshape problem (the caller turns it
    into a per-challenge skip, never a hard failure of the whole request)."""


# ── HTTP ────────────────────────────────────────────────────────────


def _describe_rate_limit(e: urllib.error.HTTPError, url: str) -> str | None:
    """GitHub reports quota exhaustion as 403/429 with X-RateLimit-* headers.
    Name the real cause and when it clears — a bare "403" on a public repo
    reads as a permissions bug and sends the next person down the wrong path
    (this is exactly how it first presented)."""
    if e.code not in (403, 429):
        return None
    remaining = e.headers.get("X-RateLimit-Remaining")
    if remaining is None or remaining.strip() != "0":
        return None
    reset = e.headers.get("X-RateLimit-Reset")
    when = ""
    try:
        mins = max(0, int(int(reset) - time.time()) // 60 + 1)
        when = f" (resets in ~{mins} min)"
    except (TypeError, ValueError):
        pass
    return (
        f"GitHub API rate limit exhausted{when}: {url}. Anonymous quota is "
        "60 requests/hour per IP and is shared with other tenants on the same "
        "egress IP; set a GITHUB_TOKEN env var on the server to raise it to "
        "5000/hour, or retry after the reset."
    )


def _get(url: str, ua: str, *, accept: str) -> bytes:
    headers = {"User-Agent": ua, "Accept": accept}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        limited = _describe_rate_limit(e, url)
        if limited:
            raise MainnetSeedError(limited) from None
        if e.code == 404:
            raise MainnetSeedError(f"not found: {url}") from None
        raise MainnetSeedError(f"HTTP {e.code}: {url}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise MainnetSeedError(f"network error fetching {url}: {e}") from None


def _get_json(url: str, ua: str) -> object:
    try:
        return json.loads(_get(url, ua, accept="application/json"))
    except json.JSONDecodeError as e:
        raise MainnetSeedError(f"bad JSON from {url}: {e}") from None


def _get_raw_text(url: str, ua: str) -> str:
    return _get(url, ua, accept="*/*").decode("utf-8", errors="replace")


def top_algorithm(challenge: str) -> tuple[str, int] | None:
    """`(algorithm_name, adoption_fp)` for the highest-adoption compiled
    mainnet algorithm on `challenge`, or None if none qualifies / the API is
    unreachable. `adoption_fp` is the raw 1e16-scaled integer."""
    try:
        block = _get_json(f"{_MAINNET_API}/get-block", "tig-swarm-server")["block"]
        block_id = block["id"]
        challenges_resp = _get_json(
            f"{_MAINNET_API}/get-challenges?block_id={block_id}", "tig-swarm-server")
        algos_resp = _get_json(
            f"{_MAINNET_API}/get-algorithms?block_id={block_id}", "tig-swarm-server")
    except MainnetSeedError:
        return None

    id_to_name = {c["id"]: c["config"]["name"] for c in challenges_resp["challenges"]}
    target_cid = next((cid for cid, n in id_to_name.items() if n == challenge), None)
    if target_cid is None:
        return None
    compile_ok = {
        b["algorithm_id"]: bool(b.get("details", {}).get("compile_success"))
        for b in algos_resp.get("binarys", [])
    }
    best: tuple[str, int] | None = None
    for algo in algos_resp["codes"]:
        if (algo.get("details") or {}).get("challenge_id") != target_cid:
            continue
        if not compile_ok.get(algo["id"]):
            continue
        try:
            adoption = int((algo.get("block_data") or {}).get("adoption") or 0)
        except (TypeError, ValueError):
            adoption = 0
        name = (algo.get("details") or {}).get("name")
        if adoption > 0 and name and (best is None or adoption > best[1]):
            best = (name, adoption)
    return best


def fetch_algorithm_files(challenge: str, algorithm: str) -> dict[str, str]:
    """The algorithm dir on GitHub → {relative_path: content}.

    ONE API request per algorithm: the git trees API lists the whole branch
    recursively, and file contents come from raw.githubusercontent.com, which
    does not draw on the API quota. The previous implementation walked the
    contents API — one request per directory PLUS one per file — which
    exhausted the anonymous 60/hour/IP budget partway through a five-challenge
    seeding run (the server's PaaS egress IP is shared, so the budget is not
    even all ours) and 403'd the remaining challenges.
    """
    branch = f"{challenge}/{algorithm}"
    subdir = f"tig-algorithms/src/{challenge}/{algorithm}/"
    tree_url = (f"{_GH_API}/git/trees/"
                f"{urllib.parse.quote(branch, safe='')}?recursive=1")
    listing = _get_json(tree_url, "tig-swarm-server")
    if not isinstance(listing, dict) or not isinstance(listing.get("tree"), list):
        raise MainnetSeedError(f"unexpected GitHub tree response for {branch}")
    if listing.get("truncated"):
        # ~100k-entry limit; tig-monorepo is far below it. If this ever fires,
        # fail loudly rather than silently seed a partial algorithm.
        raise MainnetSeedError(f"GitHub tree listing truncated for {branch}")

    files: dict[str, str] = {}
    for entry in listing["tree"]:
        path = entry.get("path", "")
        if entry.get("type") != "blob" or not path.startswith(subdir):
            continue
        rel = path[len(subdir):]
        raw_url = f"{_GH_RAW}/{urllib.parse.quote(branch)}/{urllib.parse.quote(path)}"
        files[rel] = _get_raw_text(raw_url, "tig-swarm-server")
    if not files:
        raise MainnetSeedError(f"upstream returned no files for {branch}")
    return files


# ── Reshape (port of challenge_files.reshape_mainnet_for_swarm) ──────


def _match_brace(code: str, open_idx: int) -> int | None:
    """Index of the `}` matching the `{` at open_idx, skipping string/char
    literals and comments. None if unbalanced."""
    depth, i, n = 0, open_idx, len(code)
    while i < n:
        two = code[i:i + 2]
        if two == "//":
            nl = code.find("\n", i)
            i = n if nl == -1 else nl
            continue
        if two == "/*":
            end = code.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        c = code[i]
        if c in ('"', "'"):
            i += 1
            while i < n:
                if code[i] == "\\":
                    i += 2
                    continue
                if code[i] == c:
                    break
                i += 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _strip_top_level_fn(code: str, name: str) -> str:
    """Remove a `fn <name>` / `pub fn <name>` definition (body + attached
    leading attribute/doc lines) from `code`."""
    m = re.search(r"\b(?:pub\s*(?:\([^)]*\)\s*)?)?fn\s+" + re.escape(name) + r"\b", code)
    if not m:
        return code
    open_idx = code.find("{", m.end())
    if open_idx == -1:
        return code
    close_idx = _match_brace(code, open_idx)
    if close_idx is None:
        return code
    start = code.rfind("\n", 0, m.start()) + 1
    while start > 0:
        prev_begin = code.rfind("\n", 0, start - 1) + 1
        prev = code[prev_begin:start - 1].strip()
        if prev.startswith("#[") or prev.startswith("///") or prev.startswith("//!"):
            start = prev_begin
        else:
            break
    end = close_idx + 1
    if end < len(code) and code[end] == "\n":
        end += 1
    return code[:start] + code[end:]


def _ensure_challenge_import(code: str, challenge: str) -> str:
    anchor = f"use tig_challenges::{challenge}::*;"
    if not code or anchor in code:
        return code
    # TOP-LEVEL only. `use super::*;` is also the ordinary Rust idiom for an
    # inner module pulling in its parent's scope (`mod hpf { use super::*; }`
    # in the current knapsack mainnet winner, for one), and a bare substring
    # replace rewrites the FIRST occurrence wherever it sits. Doing that to a
    # nested one strips the module's access to its parent and drops the
    # challenge glob into the wrong scope — the algorithm then fails to
    # compile for a reason nothing in the diff explains. Only a column-0
    # occurrence is the legacy swarm anchor this is meant to migrate.
    _legacy_anchor = re.compile(r"^use super::\*;[ \t]*$", re.MULTILINE)
    if _legacy_anchor.search(code):
        return _legacy_anchor.sub(anchor, code, count=1)
    lines = code.splitlines(keepends=True)
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("use "):
            lines.insert(idx, anchor + "\n")
            return "".join(lines)
    return anchor + "\n" + code


def _solve_challenge_reachable(entry_code: str, files: dict[str, str]) -> bool:
    """True if `solve_challenge` resolves as `<algorithm>::solve_challenge` —
    defined in the entry file, or defined in a submodule and re-exported from
    it (`pub use solver::{solve_challenge, help};`, mainnet's shape for large
    algorithms). Mirrors `scripts/challenge_files.solve_challenge_reachable`;
    duplicated because server/ ships without scripts/ (see CLAUDE.md)."""
    if "fn solve_challenge(" in entry_code:
        return True
    definers = [
        rel.rsplit("/", 1)[-1][:-3] for rel, content in files.items()
        if rel.endswith(".rs") and "fn solve_challenge(" in content
    ]
    if not definers:
        return False
    for line in entry_code.splitlines():
        stripped = line.strip()
        if not stripped.startswith("pub use "):
            continue
        if "solve_challenge" in stripped:
            return True
        if stripped.rstrip(";").endswith("::*"):
            path = stripped[len("pub use "):].rstrip(";")[:-3]
            if path.rsplit("::", 1)[-1].strip() in definers:
                return True
    return False


def reshape_for_swarm(challenge: str, files: dict[str, str]) -> tuple[dict | None, str]:
    """Reshape a fetched mainnet bundle into the swarm layout. Returns
    (files, "") or (None, reason). Optimizer-hook challenges get their
    harness-owned fns stripped; the entry import is normalized; a light
    structural check replaces the host-side validate_code."""
    if "mod.rs" not in files:
        return None, "bundle has no mod.rs entry file"
    out = dict(files)
    code = out["mod.rs"]
    if challenge in _OPTIMIZER_HOOK_CHALLENGES:
        for fn in _HARNESS_OWNED_FNS:
            code = _strip_top_level_fn(code, fn)
        missing = [h[3:-1] for h in _OPTIMIZER_HOOKS if h not in code]
        if missing:
            return None, f"missing optimizer hook(s): {', '.join(missing)}"
        if "fn solve_challenge(" in code:
            return None, "solve_challenge is harness-owned but could not be stripped"
    else:
        if not _solve_challenge_reachable(code, out):
            return None, ("solve_challenge is not reachable from mod.rs "
                          "(not defined there and not re-exported)")
    # A declarations-only entry names no challenge types — injecting the
    # anchor there would only add an import rustc warns is unused.
    if "fn " in code or "pub mod " not in code:
        code = _ensure_challenge_import(code, challenge)
    out["mod.rs"] = code
    return out, ""


def fetch_top_reshaped(challenge: str) -> tuple[dict | None, str]:
    """End to end: find the top mainnet algo for `challenge`, fetch it, reshape
    it. Returns ({algo_name, adoption, code_files, kernel_code}, "") or
    (None, reason)."""
    top = top_algorithm(challenge)
    if top is None:
        return None, "no compiled mainnet algorithm found"
    name, adoption = top
    try:
        files = fetch_algorithm_files(challenge, name)
    except MainnetSeedError as e:
        return None, f"fetch of {name} failed ({e})"
    code_files = {p: c for p, c in files.items() if p.endswith((".rs", ".cu", ".cuh"))}
    reshaped, err = reshape_for_swarm(challenge, code_files)
    if reshaped is None:
        return None, f"mainnet '{name}' does not fit the swarm format ({err})"
    cu = sorted(p for p in reshaped if p.endswith((".cu", ".cuh")))
    kernel_code = reshaped[cu[0]] if len(cu) == 1 else None
    return (
        {"algo_name": name, "adoption": adoption,
         "code_files": reshaped, "kernel_code": kernel_code},
        "",
    )
