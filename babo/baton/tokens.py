"""Offline token estimates.

Baton budgets must work with no network (tiktoken downloads its tables on
first use). Qwen-family tokenizers average a little over 4 characters per
token on English prose; dividing by 3.5 errs on the generous side, so a
budget is never silently exceeded.
"""

from __future__ import annotations

import math

CHARS_PER_TOKEN = 3.5


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


def trim_to_tokens(text: str, max_tokens: int, marker: str = "\n[…trimmed]") -> str:
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    limit = max(0, int(max_tokens * CHARS_PER_TOKEN) - len(marker))
    cut = text[:limit]
    newline = cut.rfind("\n")
    if newline > limit // 2:
        cut = cut[:newline]
    return cut + marker
