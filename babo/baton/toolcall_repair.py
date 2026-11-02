"""Tool calls a local model wrote as text, repaired at the gateway.

A local model server parses a model's tool calls out of what it writes, using
the model's chat template. When the two don't quite agree (a template that
doesn't match the model, a model that writes the XML form its family uses
elsewhere, a call left unclosed at a stop token) the call isn't recognized
and reaches the agent as plain text: the agent sees no call, the work stops,
and nothing says why. Every call in the town passes through the gateway, so
it repairs them here, for every agent and every harness at once.

It recognizes the two forms local models write:

- ``<tool_call>{"name": "...", "arguments": {...}}</tool_call>`` (Hermes, Qwen 2.5 and 3)
- ``<tool_call><function=name><parameter=key>value</parameter></function></tool_call>``
  (Qwen3-Coder and Qwen 3.5/3.6; the outer ``<tool_call>`` is sometimes missing)

and only for tools the request offered. Anything it can't read as a call is
passed on unchanged, so nothing the model wrote is ever lost. A closing tag the
model never wrote (it stopped first) is not needed.

Streams are repaired as they go: ordinary text passes straight through, and
only text that may be the start of a call is held back until it's clear.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, cast

MARKERS = ("<tool_call>", "<function=")
_CALL_BLOCK = re.compile(r"<tool_call>(.*?)(?:</tool_call>|$)", re.S)
_FUNCTION = re.compile(r"<function=([^>\s]+)\s*>(.*?)(?:</function>|$)", re.S)
_PARAMETER = re.compile(r"<parameter=([^>\s]+)\s*>(.*?)(?:</parameter>|(?=<parameter=)|$)", re.S)


def offered_tools(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The tools a request offers: name -> its parameters' JSON schema."""
    out: dict[str, dict[str, Any]] = {}
    for tool in cast(list[Any], body.get("tools") or []):
        if not isinstance(tool, dict):
            continue
        function = cast(dict[str, Any], tool).get("function")
        if isinstance(function, dict):
            fn = cast(dict[str, Any], function)
            if isinstance(fn.get("name"), str):
                params = fn.get("parameters")
                out[fn["name"]] = cast(dict[str, Any], params) if isinstance(params, dict) else {}
    return out


def _typed(value: str, schema: dict[str, Any]) -> object:
    """A parameter's text as the type its schema asks for (the XML form writes everything as text)."""
    text = value.strip("\n")
    kind: object = schema.get("type")
    kinds: set[str] = {str(k) for k in cast(list[object], kind)} if isinstance(kind, list) else {kind} if isinstance(kind, str) else set()
    if kinds and kinds <= {"string", "null"}:
        return text
    try:
        parsed: object = json.loads(text)
    except ValueError:
        return text
    if not kinds:
        return cast(object, parsed) if isinstance(parsed, (dict, list)) else text
    found = ("boolean" if isinstance(parsed, bool) else "integer" if isinstance(parsed, int) else "number" if isinstance(parsed, float)
             else "object" if isinstance(parsed, dict) else "array" if isinstance(parsed, list) else "null" if parsed is None else "string")
    if found in kinds or (found == "integer" and "number" in kinds):
        return cast(object, parsed)
    return text


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"id": f"call_{uuid.uuid4().hex[:24]}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def _from_function(name: str, inner: str, tools: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if name not in tools:
        return None
    props = cast(dict[str, Any], tools[name].get("properties") or {})
    args = {key: _typed(value, cast(dict[str, Any], props.get(key) or {})) for key, value in _PARAMETER.findall(inner)}
    return _call(name, args)


def _from_json(text: str, tools: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    try:
        data = json.loads(text.strip())
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    obj = cast(dict[str, Any], data)
    name, args = obj.get("name"), obj.get("arguments", obj.get("parameters", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    if not isinstance(name, str) or name not in tools or not isinstance(args, dict):
        return None
    return _call(name, cast(dict[str, Any], args))


def parse_calls(text: str, tools: dict[str, dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """The tool calls written in ``text``, and the text without them.

    All or nothing: if any call in it can't be read, nothing is taken out, so
    the agent sees what the model wrote rather than a part of it."""
    if not tools or not any(marker in text for marker in MARKERS):
        return text, []
    calls: list[dict[str, Any]] = []
    blocks = list(_CALL_BLOCK.finditer(text))
    spans: list[tuple[int, int]] = []
    if blocks:
        for block in blocks:
            inner = block.group(1)
            functions = list(_FUNCTION.finditer(inner))
            found = [_from_function(f.group(1), f.group(2), tools) for f in functions] if functions else [_from_json(inner, tools)]
            if not found or any(call is None for call in found):
                return text, []
            calls += cast(list[dict[str, Any]], found)
            spans.append(block.span())
    else:  # <function=…> without the outer <tool_call>: only with parameters or its closing tag, not a mention in prose
        for function in _FUNCTION.finditer(text):
            inner, closed = function.group(2), function.group(0).endswith("</function>")
            if not closed and not _PARAMETER.search(inner):
                return text, []
            call = _from_function(function.group(1), inner, tools)
            if call is None:
                return text, []
            calls.append(call)
            spans.append(function.span())
    rest = text
    for start, end in reversed(spans):
        rest = rest[:start] + rest[end:]
    return rest.strip(), calls


def separate_thinking(completion: dict[str, Any], *, opens: bool = False) -> bool:
    """Thinking inside a finished completion's text moved to ``reasoning_content``, in place (see
    ``babo.providers.thinking``). Returns whether the reply showed its template opens thinking."""
    from babo.providers.thinking import split_thinking

    orphan = False
    for choice in cast(list[dict[str, Any]], completion.get("choices") or []):
        message = cast(dict[str, Any], choice.get("message") or {})
        content = message.get("content")
        if not isinstance(content, str) or not content:
            continue
        thought, answer, found = split_thinking(content, opens_thinking=opens and not message.get("reasoning_content"))
        orphan = orphan or found
        if thought is not None:
            earlier = message.get("reasoning_content")
            message["reasoning_content"] = f"{earlier}\n\n{thought}" if isinstance(earlier, str) and earlier else thought
            message["content"] = answer or None
    return orphan


def repair_completion(completion: dict[str, Any], tools: dict[str, dict[str, Any]]) -> int:
    """Repair a finished (non-streamed) completion in place. Returns how many calls were repaired."""
    repaired = 0
    for choice in cast(list[dict[str, Any]], completion.get("choices") or []):
        message = cast(dict[str, Any], choice.get("message") or {})
        if message.get("tool_calls") or not isinstance(message.get("content"), str):
            continue
        rest, calls = parse_calls(message["content"], tools)
        if calls:
            message["content"] = rest or None
            message["tool_calls"] = calls
            choice["finish_reason"] = "tool_calls"
            repaired += len(calls)
    return repaired


def _held_prefix(text: str) -> int:
    """How much of the end of ``text`` could be the start of a call marker."""
    for size in range(min(len(text), max(map(len, MARKERS)) - 1), 0, -1):
        if any(marker.startswith(text[-size:]) for marker in MARKERS):
            return size
    return 0


@dataclass
class StreamRepair:
    """Repairs an OpenAI-style event stream as it passes. Feed it the upstream bytes; it yields what to send on."""

    tools: dict[str, dict[str, Any]]
    repaired: int = 0
    opens_thinking: bool = False  # learned: the model's template opens thinking (see babo.providers.thinking)
    _buffer: bytes = b""
    _pending: str = ""  # text not sent yet: maybe the start of a marker
    _held: str = ""  # text from a marker on: maybe a call
    _native: bool = False  # the server parsed calls itself: nothing to repair
    _head: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))
    _done: bool = False
    _thinking: Any = None

    def __post_init__(self) -> None:
        from babo.providers.thinking import ThinkingSplitter

        self._thinking = ThinkingSplitter(opens_thinking=self.opens_thinking)

    @property
    def orphan(self) -> bool:
        """This reply showed its model's template opens thinking (remember it)."""
        return bool(self._thinking.orphan)

    def _content(self, parts: list[tuple[str, str]]) -> list[bytes]:
        """The reply's text, split: thinking goes on as ``reasoning_content``, the answer as content."""
        out: list[bytes] = []
        for kind, text in parts:
            out += [self._event({"reasoning_content": text})] if kind == "thinking" else self._text(text)
        return out

    def _event(self, delta: dict[str, Any], finish: str | None = None, **extra: Any) -> bytes:
        payload = {**self._head, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()

    def _text(self, delta: str) -> list[bytes]:
        out: list[bytes] = []
        if self._held:
            self._held += delta
            return out
        text = self._pending + delta
        self._pending = ""
        if not self.tools:  # nothing to repair
            return [self._event({"content": text})] if text else out
        at = min((i for i in (text.find(m) for m in MARKERS) if i >= 0), default=-1)
        if at >= 0:
            before, self._held = text[:at], text[at:]
        else:
            keep = _held_prefix(text)
            before, self._pending = (text[:-keep], text[-keep:]) if keep else (text, "")
        if before:
            out.append(self._event({"content": before}))
        return out

    def _finish(self, finish: str | None, extra: dict[str, Any]) -> list[bytes]:
        out: list[bytes] = self._content(self._thinking.finish())
        if self._held:
            rest, calls = parse_calls(self._held, self.tools)
            if calls:
                if rest:
                    out.append(self._event({"content": rest}))
                out += [self._event({"tool_calls": [{"index": i, **call}]}) for i, call in enumerate(calls)]
                finish = "tool_calls"
                self.repaired += len(calls)
            else:
                out.append(self._event({"content": self._held}))
            self._held = ""
        if self._pending:
            out.append(self._event({"content": self._pending}))
            self._pending = ""
        out.append(self._event({}, finish, **extra))
        return out

    def _line(self, line: bytes) -> list[bytes]:
        if not line.startswith(b"data:"):
            return [line + b"\n"] if line.strip() else []
        data = line[5:].strip()
        if data == b"[DONE]":
            out = [] if self._done or self._native else self._finish("stop", {})
            self._done = True
            return [*out, b"data: [DONE]\n\n"]
        try:
            event = cast(dict[str, Any], json.loads(data))
            choice = cast(dict[str, Any], event["choices"][0]) if event.get("choices") else None
        except (ValueError, KeyError, IndexError, TypeError):
            return [line + b"\n\n"]
        if choice is None or self._native:
            return [line + b"\n\n"]
        self._head = {k: v for k, v in event.items() if k not in ("choices", "usage")}
        delta = cast(dict[str, Any], choice.get("delta") or {})
        out: list[bytes] = []
        if delta.get("tool_calls"):
            self._native = True  # the server found the calls; send on what was held, as it was
            out += [self._event({"content": text}) for text in (self._pending + self._held,) if text]
            self._pending = self._held = ""
            return [*out, line + b"\n\n"]
        content = delta.get("content")
        others = {k: v for k, v in delta.items() if k != "content"}
        if others and any(v not in (None, "") for v in others.values()):
            if others.get("reasoning_content") or others.get("reasoning"):
                self._thinking.native()  # thinking comes in its own field: the text is all answer
            out.append(self._event(others))
        if isinstance(content, str) and content:
            out += self._content(self._thinking.feed(content))
        if choice.get("finish_reason") is not None:
            extra = {"usage": event["usage"]} if event.get("usage") else {}
            out += self._finish(cast(str, choice["finish_reason"]), extra)
            self._done = True
        elif event.get("usage") and not content and not others:
            out.append(line + b"\n\n")
        return out

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer += chunk
        out: list[bytes] = []
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            out += self._line(line.rstrip(b"\r"))
        return out

    def close(self) -> list[bytes]:
        out = self._line(self._buffer.rstrip(b"\r")) if self._buffer.strip() else []
        self._buffer = b""
        if not self._done and not self._native and (self._held or self._pending):
            out += self._finish("stop", {})
            self._done = True
        return out


async def repair_stream(chunks: AsyncIterator[bytes], repair: StreamRepair) -> AsyncIterator[bytes]:
    async for chunk in chunks:
        for out in repair.feed(chunk):
            yield out
    for out in repair.close():
        yield out
