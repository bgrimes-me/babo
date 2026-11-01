"""The checkpoint schema: what a resident writes at a checkpoint or a fold.

Plain JSON Schema (for llama.cpp ``response_format``) plus a pydantic model
that validates and normalizes the reply. Kept dependency-free of babo's
agent loop so other harnesses can reuse it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

_STR_LIST = {"type": "array", "items": {"type": "string"}}

KNOWLEDGE_OP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": ["create", "update", "supersede"]},
        "title": {"type": "string"},
        "body": {"type": "string"},
        "path": {"type": "string"},
        "aliases": _STR_LIST,
        "keywords": _STR_LIST,
    },
    "required": ["op", "title", "body", "path", "aliases", "keywords"],
    "additionalProperties": False,
}

PROMOTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["path", "reason"],
    "additionalProperties": False,
}

CHECKPOINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thread": {"type": "string"},
        "doing": {"type": "string"},
        "done": _STR_LIST,
        "learned": _STR_LIST,
        "next": _STR_LIST,
        "blockers": _STR_LIST,
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "knowledge_ops": {"type": "array", "items": KNOWLEDGE_OP_SCHEMA},
        "promote": {"type": "array", "items": PROMOTE_SCHEMA},
    },
    "required": ["thread", "doing", "done", "learned", "next", "blockers", "confidence", "knowledge_ops", "promote"],
    "additionalProperties": False,
}

# The fold adds an optional SELF.md revision.
FOLD_SCHEMA: dict[str, Any] = {
    **CHECKPOINT_SCHEMA,
    "properties": {
        **CHECKPOINT_SCHEMA["properties"],
        "revise_self": {"type": "string"},
    },
    "required": [*CHECKPOINT_SCHEMA["required"], "revise_self"],
}


class KnowledgeOp(BaseModel):
    op: Literal["create", "update", "supersede"]
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    path: str = ""
    aliases: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class Promote(BaseModel):
    path: str = Field(min_length=1)
    reason: str = ""


# Memory stays small: a checkpoint's lists keep their first items, each a line or two (H3's caps).
LIST_ITEMS = 8
ITEM_CHARS = 300


def _clean(items: list[str]) -> list[str]:
    return [s.strip()[:ITEM_CHARS] for s in items if s and s.strip()][:LIST_ITEMS]


class CheckpointReply(BaseModel):
    thread: str = Field(min_length=1, max_length=120)
    doing: str = ""
    done: list[str] = Field(default_factory=list)
    learned: list[str] = Field(default_factory=list)
    next: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)
    knowledge_ops: list[KnowledgeOp] = Field(default_factory=list)
    promote: list[Promote] = Field(default_factory=list)
    revise_self: str = ""

    @field_validator("done", "learned", "next", "blockers")
    @classmethod
    def _strip(cls, value: list[str]) -> list[str]:
        return _clean(value)

    @field_validator("thread")
    @classmethod
    def _thread(cls, value: str) -> str:
        return value.strip()[:120]


def parse_reply(data: Any) -> tuple[CheckpointReply | None, str]:
    """Validate a model reply. Returns (reply, "") or (None, a short error for the retry)."""
    try:
        return CheckpointReply.model_validate(data), ""
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        return None, f"{where}: {first.get('msg', 'invalid')}"
