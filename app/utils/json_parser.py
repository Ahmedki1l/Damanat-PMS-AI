# app/utils/json_parser.py
"""
Helpers for parsing Phase 2 ANPR JSON event payloads.
ANPR cameras send AccessControllerEvent as JSON.
"""

import json
from typing import Optional, Any


def safe_parse_json(raw_body: bytes) -> Optional[dict]:
    """Parse JSON bytes safely. Returns None on error."""
    try:
        return json.loads(raw_body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def get_nested(data: dict, *keys: str, default: Any = None) -> Any:
    """Safely navigate nested dict keys. Returns default if any key is missing."""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key, default)
        if current is default:
            return default
    return current


def is_json_body(raw_body: bytes, content_type: str = "") -> bool:
    """Detect if the raw body is JSON (by content-type or by inspecting first byte)."""
    if "json" in content_type.lower():
        return True
    stripped = raw_body.lstrip()
    return stripped.startswith(b"{") or stripped.startswith(b"[")
