"""Verify the server's HMAC-SHA256 signature on a command.

Must stay byte-for-byte compatible with ``server/jarvis/laptop_signing.py``
(both test suites check the same test vector).
"""

import hashlib
import hmac
import json
from datetime import datetime
from typing import Any

MIN_SECRET_LENGTH = 32


def canonical(command_id: str, tool: str, args: dict, expires_at: int) -> bytes:
    payload = {"args": args, "expires_at": int(expires_at), "id": str(command_id), "tool": tool}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sign(secret: str, command_id: str, tool: str, args: dict, expires_at: int) -> str:
    key = secret.encode()
    return hmac.new(key, canonical(command_id, tool, args, expires_at), hashlib.sha256).hexdigest()


def expires_epoch(value: Any) -> int:
    """``expires_at`` from a row (ISO 8601 text or a number) as whole epoch seconds."""
    if isinstance(value, int | float):
        return int(value)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("expires_at has no time zone")
    return int(dt.timestamp())


def verify_row(secret: str, row: dict) -> bool:
    """True only if the row carries a valid signature over its id, tool, args and expiry."""
    signature = row.get("signature")
    args = row.get("args")
    if not secret or len(secret) < MIN_SECRET_LENGTH:
        return False
    if not isinstance(signature, str) or not signature or not isinstance(args, dict):
        return False
    try:
        expected = sign(secret, row["id"], row["tool"], args, expires_epoch(row["expires_at"]))
    except (KeyError, TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature)
