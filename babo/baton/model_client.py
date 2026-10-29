"""Baton's own structured calls to the model (JSON-schema output).

Upstream's provider layer doesn't expose ``response_format``, so checkpoint,
fold, Archivist and Inspector turns use this small client. It talks to the
model queue (``baton.model.endpoint``), never straight to llama-server, so it
waits its turn like every other call.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, cast

import httpx

from babo.baton.config import BatonModelConfig

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{[\s\S]*\})\s*```")


class ModelUnavailableError(RuntimeError):
    """The model endpoint could not be reached."""


@dataclass
class SchemaResult:
    data: Any | None
    raw: str
    error: str = ""


def extract_json(text: str) -> Any | None:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    fenced = _FENCE_RE.search(text)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except ValueError:
            return None
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start : end + 1])
        except ValueError:
            return None
    return None


def plain_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip babo-internal keys so the payload is plain OpenAI chat format."""
    out: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            continue
        content = message.get("content")
        if isinstance(content, list):
            parts = [cast(dict[str, Any], p) for p in cast(list[object], content) if isinstance(p, dict)]
            content = "\n".join(str(p.get("text", "")) for p in parts if p.get("type") == "text")
        entry: dict[str, Any] = {"role": role, "content": content if content is not None else ""}
        if role == "assistant" and message.get("tool_calls"):
            entry["tool_calls"] = message["tool_calls"]
        if role == "tool":
            entry["tool_call_id"] = message.get("tool_call_id", "")
            if message.get("name"):
                entry["name"] = message["name"]
        out.append(entry)
    return out


def flatten_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rewrite tool calls/results as text, for servers whose template rejects
    tool history when no tools are offered."""
    out: list[dict[str, Any]] = []
    for message in plain_messages(messages):
        role = message["role"]
        if role == "tool":
            out.append({"role": "user", "content": f"[tool result {message.get('name', '')}]\n{message['content']}"})
            continue
        if role == "assistant" and message.get("tool_calls"):
            calls = "; ".join(
                f"{c.get('function', {}).get('name')}({c.get('function', {}).get('arguments')})"
                for c in cast(list[dict[str, Any]], message["tool_calls"])
            )
            out.append({"role": "assistant", "content": f"{message['content']}\n[called tools: {calls}]".strip()})
            continue
        out.append({"role": role, "content": message["content"]})
    # Chat templates require alternation after the system prompt; merge repeats.
    merged: list[dict[str, Any]] = []
    for message in out:
        if merged and merged[-1]["role"] == message["role"] and message["role"] != "system":
            merged[-1]["content"] = f"{merged[-1]['content']}\n\n{message['content']}"
        else:
            merged.append(dict(message))
    return merged


class BatonModelClient:
    def __init__(self, config: BatonModelConfig, *, resident: str = "", transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.resident = resident
        self._transport = transport

    def _headers(self, priority: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "X-Baton-Priority": priority}
        if self.resident:
            headers["X-Baton-Resident"] = self.resident
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    async def chat(self, body: dict[str, Any], *, priority: str = "interactive") -> dict[str, Any]:
        url = self.config.endpoint.rstrip("/") + "/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=self.config.request_timeout_s, transport=self._transport) as client:
                resp = await client.post(url, json=body, headers=self._headers(priority))
        except httpx.HTTPError as exc:
            raise ModelUnavailableError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {resp.status_code}: {resp.text[:200]}", request=resp.request, response=resp)
        return cast(dict[str, Any], resp.json())

    async def chat_json(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        *,
        name: str = "reply",
        priority: str = "interactive",
        max_tokens: int | None = None,
        flatten: bool = False,
    ) -> SchemaResult:
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": flatten_messages(messages) if flatten else plain_messages(messages),
            "temperature": self.config.temperature,
            "max_tokens": max_tokens or self.config.schema_max_tokens,
            "stream": False,
            "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
        }
        try:
            data = await self.chat(body, priority=priority)
        except httpx.HTTPStatusError as exc:
            return SchemaResult(None, "", str(exc))
        choices = cast(list[dict[str, Any]], data.get("choices") or [{}])
        message = cast(dict[str, Any], choices[0].get("message") or {})
        raw = str(message.get("content") or "")
        parsed = extract_json(raw)
        if parsed is None:
            return SchemaResult(None, raw, "the reply was not valid JSON")
        return SchemaResult(parsed, raw)

    async def health(self) -> bool:
        base = self.config.endpoint.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        try:
            async with httpx.AsyncClient(timeout=5, transport=self._transport) as client:
                resp = await client.get(base + "/health")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
