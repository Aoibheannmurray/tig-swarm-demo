"""Symmetric encryption for contributor secrets at rest (hosted runner, P3).

Contributor LLM/C3 keys are stored encrypted in the runner's own SQLite. The
Fernet key comes from the `RUNNER_SECRET_KEY` environment variable (a Railway
service secret), never from disk or the repo. Losing it means every stored
secret is unreadable — which is the point: a leaked DB volume without the env
secret discloses nothing.

Fernet gives us AES-128-CBC + HMAC authentication and a urlsafe-base64 token,
so ciphertext is safe to keep in a TEXT column.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


class VaultUnavailable(RuntimeError):
    """Raised when RUNNER_SECRET_KEY is missing or malformed. Callers turn
    this into a 503 so enrollment fails loudly instead of silently storing
    plaintext or an undecryptable blob."""


def _fernet() -> Fernet:
    key = os.environ.get("RUNNER_SECRET_KEY", "").strip()
    if not key:
        raise VaultUnavailable(
            "RUNNER_SECRET_KEY is not set. Generate one with "
            "`python -m runner.vault --generate` and set it as a service "
            "secret before enrolling contributors."
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as e:
        raise VaultUnavailable(f"RUNNER_SECRET_KEY is not a valid Fernet key: {e}")


def available() -> bool:
    """True when a usable key is configured — for a health/readiness check."""
    try:
        _fernet()
        return True
    except VaultUnavailable:
        return False


def encrypt(plaintext: str) -> str:
    """Encrypt a secret to a urlsafe-base64 token (str)."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a token from `encrypt`. Raises InvalidToken if the ciphertext
    was tampered with or was written under a different key."""
    return _fernet().decrypt(token.encode()).decode()


def encrypt_map(secrets: dict[str, str]) -> str:
    """Encrypt a name→value secret map as a single token (JSON then Fernet)."""
    import json
    return encrypt(json.dumps(secrets))


def decrypt_map(token: str) -> dict[str, str]:
    """Inverse of encrypt_map. Returns {} on an unreadable/tampered token so a
    corrupt row degrades to "no keys" rather than crashing the supervisor."""
    import json
    try:
        raw = decrypt(token)
    except InvalidToken:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


if __name__ == "__main__":
    import sys
    if "--generate" in sys.argv:
        # Print a fresh key for the operator to paste into RUNNER_SECRET_KEY.
        print(Fernet.generate_key().decode())
    else:
        print("usage: python -m runner.vault --generate", file=sys.stderr)
        sys.exit(2)
