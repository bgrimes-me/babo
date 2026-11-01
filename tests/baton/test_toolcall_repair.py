"""Tool calls a local model wrote as text are repaired at the gateway, for every agent at once."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from babo.baton.config import BatonModelConfig
from babo.baton.gateway import RouterSource
from babo.baton.queue import ModelQueue
from babo.baton.toolcall_repair import StreamRepair, offered_tools, parse_calls, repair_completion
from baton.mock_llama import MockLlama, text, tool_call
from baton.test_gateway import _config, _serve

TOOLS = offered_tools({"tools": [
    {"type": "function", "function": {"name": "read", "parameters": {"type": "object", "properties": {
        "path": {"type": "string"}, "limit": {"type": "integer"}, "all": {"type": "boolean"}}}}},
    {"type": "function", "function": {"name": "shell", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}},
]})


def _args(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return call["function"]["name"], json.loads(call["function"]["arguments"])


def test_the_json_form() -> None:
    rest, calls = parse_calls('Let me look.\n<tool_call>\n{"name": "read", "arguments": {"path": "notes.md"}}\n</tool_call>', TOOLS)
    assert rest == "Let me look." and [_args(c) for c in calls] == [("read", {"path": "notes.md"})]


def test_the_xml_form_with_types_from_the_schema() -> None:
    written = ("<tool_call>\n<function=read>\n<parameter=path>\nnotes.md\n</parameter>\n<parameter=limit>\n20\n</parameter>\n"
               "<parameter=all>\ntrue\n</parameter>\n</function>\n</tool_call>\n"
               "<tool_call><function=shell><parameter=command>ls -la 42</parameter></function></tool_call>")
    rest, calls = parse_calls(written, TOOLS)
    assert rest == ""
    assert [_args(c) for c in calls] == [("read", {"path": "notes.md", "limit": 20, "all": True}), ("shell", {"command": "ls -la 42"})]


def test_unclosed_and_without_the_outer_tag() -> None:
    _, calls = parse_calls("<function=read>\n<parameter=path>\nnotes.md\n", TOOLS)  # the model stopped before closing
    assert [_args(c) for c in calls] == [("read", {"path": "notes.md"})]


def test_what_isnt_a_call_is_left_as_written() -> None:
    for written in ("<tool_call>{not json}</tool_call>", '<tool_call>{"name": "rm_everything", "arguments": {}}</tool_call>',
                    "Use <function=read> in your template.", "Nothing to see here."):
        assert parse_calls(written, TOOLS) == (written, [])
    assert parse_calls('<tool_call>{"name": "read", "arguments": {}}</tool_call>', {})[1] == []  # no tools offered


def test_a_finished_completion_is_repaired() -> None:
    completion = {"choices": [{"index": 0, "finish_reason": "stop", "message": {
        "role": "assistant", "content": '<tool_call>{"name": "read", "arguments": {"path": "a"}}</tool_call>'}}]}
    assert repair_completion(completion, TOOLS) == 1
    choice = completion["choices"][0]
    assert choice["finish_reason"] == "tool_calls" and choice["message"]["content"] is None
    assert _args(choice["message"]["tool_calls"][0]) == ("read", {"path": "a"})


def _stream(*deltas: dict[str, Any], finish: str = "stop") -> list[bytes]:
    head = {"id": "c1", "object": "chat.completion.chunk", "model": "m"}
    events = [{**head, "choices": [{"index": 0, "delta": d, "finish_reason": None}]} for d in deltas]
    events.append({**head, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    return [f"data: {json.dumps(e)}\n\n".encode() for e in events] + [b"data: [DONE]\n\n"]


def _run(chunks: list[bytes], tools: dict[str, dict[str, Any]] = TOOLS) -> tuple[str, list[dict[str, Any]], str, StreamRepair]:
    repair = StreamRepair(tools)
    raw = b"".join(out for chunk in chunks for out in repair.feed(chunk)) + b"".join(repair.close())
    content, calls, finish = "", [], ""
    for line in raw.decode().splitlines():
        if line.startswith("data: {"):
            choice = json.loads(line[6:])["choices"][0]
            content += choice["delta"].get("content") or ""
            calls += choice["delta"].get("tool_calls") or []
            finish = choice["finish_reason"] or finish
    assert raw.endswith(b"data: [DONE]\n\n")
    return content, calls, finish, repair


def test_a_stream_is_repaired_as_it_goes_even_split_mid_marker() -> None:
    pieces = ["Let me ", "look.<tool", "_call>\n<function=re", "ad>\n<parameter=path>notes.md</parameter>\n</function>\n</tool_call>"]
    chunks = [c for piece in pieces for c in _stream({"content": piece})[:1]] + _stream()[-2:]
    content, calls, finish, repair = _run(chunks)
    assert content == "Let me look." and finish == "tool_calls" and repair.repaired == 1
    assert calls[0]["index"] == 0 and _args(calls[0]) == ("read", {"path": "notes.md"})


def test_a_stream_passes_ordinary_text_and_real_calls_unchanged() -> None:
    content, calls, finish, repair = _run(_stream({"content": "a < b, and <tool"}, {"content": "s are fine"}))
    assert (content, calls, finish, repair.repaired) == ("a < b, and <tools are fine", [], "stop", 0)
    native = {"tool_calls": [{"index": 0, "id": "x", "type": "function", "function": {"name": "read", "arguments": "{}"}}]}
    content, calls, finish, _ = _run(_stream({"content": "ok"}, native, finish="tool_calls"))
    assert content == "ok" and calls[0]["id"] == "x" and finish == "tool_calls"
    content, calls, _, _ = _run(_stream({"content": "<tool_call>{broken</tool_call>"}))
    assert content == "<tool_call>{broken</tool_call>" and calls == []  # nothing lost


async def test_the_gateway_repairs_calls_from_the_local_model(tmp_path: Path, mock_llama: MockLlama) -> None:
    path = tmp_path / "config.json"
    queue = ModelQueue(BatonModelConfig(upstream=mock_llama.base_url), router=RouterSource(path))
    runner, base = await _serve(queue)
    tools = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
    written = "<tool_call>\n<function=read>\n<parameter=path>\nnotes.md\n</parameter>\n</function>\n</tool_call>"
    try:
        _config(path, upstream=mock_llama.base_url, queue=base, cloud="http://127.0.0.1:9/v1")
        async with httpx.AsyncClient(timeout=30) as client:
            mock_llama.default = text(written)
            plain = (await client.post(base + "/chat/completions", json={"model": "town", "messages": [], "tools": tools})).json()
            assert _args(plain["choices"][0]["message"]["tool_calls"][0]) == ("read", {"path": "notes.md"})
            streamed = await client.post(base + "/chat/completions", json={"model": "town", "stream": True, "messages": [], "tools": tools})
            assert '"name": "read"' in streamed.text and '"finish_reason": "tool_calls"' in streamed.text and "<function=" not in streamed.text
            # A call the server parsed itself, and a request without tools, are passed on as they were.
            mock_llama.default = tool_call("read", {"path": "x"})
            assert (await client.post(base + "/chat/completions", json={"model": "town", "messages": [], "tools": tools})).json()[
                "choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "read"
            mock_llama.default = text(written)
            untouched = (await client.post(base + "/chat/completions", json={"model": "town", "messages": []})).json()
            assert untouched["choices"][0]["message"]["content"] == written
            assert (await client.get(base.removesuffix("/v1") + "/baton/queue")).json()["repaired_tool_calls"] == 2
    finally:
        await runner.cleanup()
