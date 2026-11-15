"""Sign laptop agent commands: HMAC-SHA256 over (id, tool, args, expires_at).

The secret is ``LAPTOP_AGENT_SECRET`` (server/.env and laptop_agent/.env); it is
never stored in Supabase or logged. Must stay byte-for-byte compatible with
``laptop_agent/laptop_agent/signing.py`` (both test suites check the same vector).
"""

import hashlib
import hmac
import json

MIN_SECRET_LENGTH = 32


def canonical(command_id: str, tool: str, args: dict, expires_at: int) -> bytes:
    payload = {"args": args, "expires_at": int(expires_at), "id": str(command_id), "tool": tool}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sign(secret: str, command_id: str, tool: str, args: dict, expires_at: int) -> str:
    if not secret or len(secret) < MIN_SECRET_LENGTH:
        raise ValueError(f"LAPTOP_AGENT_SECRET must be at least {MIN_SECRET_LENGTH} characters")
    key = secret.encode()
    return hmac.new(key, canonical(command_id, tool, args, expires_at), hashlib.sha256).hexdigest()
