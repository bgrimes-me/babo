"""Secret redaction for the event log, and the regex fallback for the night scrub."""

from __future__ import annotations

import re
from typing import Any, cast

REDACTED = "[REDACTED]"

_PATTERNS = [
    # Bearer tokens first, so `Authorization: Bearer <t>` loses the token, not the word.
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"),
    # key = value / key: value for secret-looking names
    re.compile(r"(?i)\b([\w-]*(?:api[_-]?key|token|secret|password|passwd|pwd|auth|credential)[\w-]*)\s*([:=])\s*(['\"]?)([^\s'\",;]{4,})\3"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),  # telegram bot tokens
]


def redact_text(text: str) -> str:
    out = text
    for pattern in _PATTERNS:
        if pattern.groups >= 4:
            out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}{m.group(3)}", out)
        elif pattern.groups >= 1:
            out = pattern.sub(lambda m: f"{m.group(1)} {REDACTED}", out)
        else:
            out = pattern.sub(REDACTED, out)
    return out


def redact(value: Any) -> Any:
    """Redact strings anywhere inside a JSON-like value."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in cast(dict[object, Any], value).items():
            name = str(key)
            if re.search(r"(?i)(api[_-]?key|token|secret|password|passwd|credential)", name) and isinstance(item, str) and item:
                out[name] = REDACTED
            else:
                out[name] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in cast(list[Any], value)]
    return value


def find_secrets(text: str) -> list[str]:
    """Return short descriptions of secret-looking matches (for the night scrub report)."""
    found: list[str] = []
    for pattern in _PATTERNS:
        for match in pattern.finditer(text):
            snippet = match.group(0)
            found.append(snippet[:6] + "…" if len(snippet) > 6 else snippet)
    return found
