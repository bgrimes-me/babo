"""/checkpoint: the Mayor's chats get the residents' "checkpoint now"."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from babo.bus.events import InboundMessage
from babo.command.builtin import build_help_text, builtin_command_palette, cmd_checkpoint
from babo.command.router import CommandContext


class _Formation:
    def __init__(self, last: dict[str, list[dict[str, Any]]] | None = None, authored: bool = True, error: str = ""):
        self.last_messages = last or {}
        self.calls: list[tuple[str, list[dict[str, Any]], str]] = []
        self.authored, self.error = authored, error

    async def checkpoint_now(self, key: str, messages: list[dict[str, Any]], trigger: str) -> Any:
        self.calls.append((key, messages, trigger))
        return SimpleNamespace(authored=self.authored, error=self.error, checkpoint=SimpleNamespace(thread="tidying the garden notes"))


class _Session:
    def __init__(self, history: list[dict[str, Any]]):
        self.history = history

    def get_history(self) -> list[dict[str, Any]]:
        return self.history


def _ctx(loop: Any, session: Any = None) -> CommandContext:
    msg = InboundMessage(channel="websocket", sender_id="owner", chat_id="abc", content="/checkpoint")
    return CommandContext(msg=msg, session=session, key=msg.session_key, raw="/checkpoint", loop=loop)


def _loop(formation: Any) -> Any:
    context = SimpleNamespace(build_system_prompt=lambda channel=None: "You are the Mayor.")
    return SimpleNamespace(baton=SimpleNamespace(formation=formation) if formation else None, context=context)


def test_checkpoint_uses_the_last_turn_when_there_is_one() -> None:
    formation = _Formation({"websocket:abc": [{"role": "system", "content": "x"}, {"role": "user", "content": "hi"}]})
    out = asyncio.run(cmd_checkpoint(_ctx(_loop(formation))))
    assert formation.calls[0][0] == "websocket:abc" and formation.calls[0][2] == "requested"
    assert formation.calls[0][1][1]["content"] == "hi"
    assert out.content == "Checkpoint written: tidying the garden notes"


def test_checkpoint_falls_back_to_the_chat_history() -> None:
    formation = _Formation()
    session = _Session([{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}])
    asyncio.run(cmd_checkpoint(_ctx(_loop(formation), session)))
    messages = formation.calls[0][1]
    assert messages[0] == {"role": "system", "content": "You are the Mayor."}
    assert [m["content"] for m in messages[1:]] == ["hello", "hi"]


def test_checkpoint_says_why_when_nothing_is_written() -> None:
    assert "nothing to checkpoint" in asyncio.run(cmd_checkpoint(_ctx(_loop(_Formation()), _Session([])))).content
    assert "isn't part of a town" in asyncio.run(cmd_checkpoint(_ctx(_loop(None)))).content
    mechanical = _Formation({"websocket:abc": [{"role": "user", "content": "x"}]}, authored=False, error="no JSON")
    assert "(no JSON)" in asyncio.run(cmd_checkpoint(_ctx(_loop(mechanical)))).content


def test_checkpoint_is_offered_in_menus_and_help() -> None:
    assert any(spec["command"] == "/checkpoint" for spec in builtin_command_palette())
    assert "/checkpoint" in build_help_text()
