"""Thinking in channels without their own way to show it: one labelled message, set apart from the answer."""

from __future__ import annotations

from typing import Any

import pytest

from babo.bus.events import OutboundMessage
from babo.bus.queue import MessageBus
from babo.channels.base import BaseChannel


class _Chat(BaseChannel):
    name = "chat"

    def __init__(self) -> None:
        super().__init__({}, MessageBus())
        self.sent: list[OutboundMessage] = []

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


@pytest.mark.asyncio
async def test_thinking_is_sent_once_it_ends_labelled_and_quoted() -> None:
    chat = _Chat()
    await chat.send_reasoning_delta("c1", "First the board.\n", stream_id="s")
    await chat.send_reasoning_delta("c1", "Then the letters.", stream_id="s")
    assert chat.sent == []  # not one message per piece
    await chat.send_reasoning_end("c1", {"thread": "t"}, stream_id="s")
    [msg] = chat.sent
    assert msg.content == "💭 *Thinking*\n> First the board.\n> Then the letters."
    assert msg.chat_id == "c1" and msg.metadata == {"thread": "t"}
    await chat.send_reasoning_end("c1", stream_id="s")  # nothing left: nothing sent
    assert len(chat.sent) == 1


@pytest.mark.asyncio
async def test_long_thinking_is_cut_short_and_a_one_shot_block_works_too() -> None:
    chat = _Chat()
    await chat.send_reasoning(OutboundMessage(channel="chat", chat_id="c1", content="x" * 5000))
    [msg] = chat.sent
    assert msg.content.endswith(" …") and len(msg.content) < 2000


@pytest.mark.asyncio
async def test_email_never_sends_a_message_per_thought() -> None:
    from babo.channels.email.runtime import EmailChannel

    assert EmailChannel.reasoning_as_message is False


def test_discord_hides_thinking_behind_a_spoiler_with_a_small_label() -> None:
    from babo.channels.discord.runtime import DiscordChannel

    text: Any = DiscordChannel.format_reasoning(object.__new__(DiscordChannel), "Is it || this?")
    assert text == "-# 💭 Thinking (click to show)\n||Is it | | this?||"
