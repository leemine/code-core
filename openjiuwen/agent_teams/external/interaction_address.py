"""Stateless member interaction addresses; pending ownership stays in the IO adapter."""
from __future__ import annotations

import base64
import json

PREFIX = "team-interaction:"


def encode_interaction_address(team: str, member: str, session: str, cycle: str, request: str) -> str:
    data = json.dumps([team, member, session, cycle, request], separators=(",", ":")).encode()
    return PREFIX + base64.urlsafe_b64encode(data).decode().rstrip("=")


def decode_interaction_address(value: str) -> tuple[str, str, str, str, str] | None:
    if not isinstance(value, str) or not value.startswith(PREFIX) or len(value) > 8192:
        return None
    try:
        raw = value[len(PREFIX):]
        parts = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(parts, list) or len(parts) != 5 or any(not isinstance(p, str) or not p for p in parts):
        return None
    return tuple(parts)
