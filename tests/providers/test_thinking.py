"""Thinking inside the reply's text: told apart from the answer, whichever way the model sends it."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from babo.providers import thinking
from babo.providers.openai_compat_provider import OpenAICompatProvider
from babo.providers.registry import ModelAPICapabilities, find_by_name
from babo.providers.thinking import ThinkingSplitter, split_thinking

BASE = "http://127.0.0.1:8080/v1"
MODEL = "qwen3-30b-a3b-thinking.gguf"


@pytest.fixture(autouse=True)
def _learned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(thinking, "_path", lambda: tmp_path / "opens-thinking.json")
    monkeypatch.setattr(thinking, "_known", None)


@pytest.mark.parametrize(("reply", "expected"), [
    ("<think>Which file?</think>\n\nHere it is.", ("Which file?", "Here it is.")),
    ("<thinking>a</thinking>b", ("a", "b")),
    ("First this.\nThen that.</think>\n\nThe answer.", ("First this.\nThen that.", "The answer.")),  # opened by the template
    ("Plain answer.", (None, "Plain answer.")),
    ("<div>html stays</div>", (None, "<div>html stays</div>")),
    ("a < b, and b > c", (None, "a < b, and b > c")),
    ("<think>cut off before the answer", ("cut off before the answer", "")),
])
def test_a_whole_reply_is_split(reply: str, expected: tuple[str | None, str]) -> None:
    assert split_thinking(reply)[:2] == expected


def test_a_model_known_to_open_thinking_still_answers_plainly_without_a_closing_tag() -> None:
    assert split_thinking("Just an answer.", opens_thinking=True)[:2] == (None, "Just an answer.")
    assert split_thinking("hmm</think>Yes.", opens_thinking=True)[:2] == ("hmm", "Yes.")


def test_a_stream_is_routed_as_it_arrives_even_with_tags_split_across_chunks() -> None:
    s = ThinkingSplitter()
    parts = [p for chunk in ["<thi", "nk>reas", "oning</th", "ink>\n\nAns", "wer"] for p in s.feed(chunk)] + s.finish()
    assert "".join(t for k, t in parts if k == "thinking") == "reasoning"
    assert "".join(t for k, t in parts if k == "answer") == "Answer"
    assert [k for k, _ in parts][0] == "thinking"


def test_a_template_opened_stream_is_thinking_until_its_closing_tag() -> None:
    s = ThinkingSplitter(opens_thinking=True)
    parts = s.feed("Let me ") + s.feed("check.</thi") + s.feed("nk>\nDone.") + s.finish()
    assert "".join(t for k, t in parts if k == "thinking") == "Let me check."
    assert [p for p in parts if p[0] == "answer"] == [("answer", "Done.")]


def _chunk(delta: dict[str, object], finish: str | None = None) -> ChatCompletionChunk:
    return ChatCompletionChunk.model_validate({
        "id": "c", "object": "chat.completion.chunk", "created": 0, "model": MODEL,
        "choices": [{"index": 0, "finish_reason": finish, "delta": delta}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2} if finish else None,
    })


def _stream(*pieces: str, reasoning: str = ""):
    async def gen():
        if reasoning:
            yield _chunk({"reasoning_content": reasoning})
        for piece in pieces:
            yield _chunk({"content": piece})
        yield _chunk({}, finish="stop")
    return gen()


def _provider() -> OpenAICompatProvider:
    return OpenAICompatProvider(api_key="x", api_base=BASE, default_model=MODEL, spec=find_by_name("custom"),
                                model_api=ModelAPICapabilities(("chat_completions",), "chat_completions"))


async def _run(create: AsyncMock) -> tuple[list[str], list[str], object]:
    seen_thinking: list[str] = []
    seen_answer: list[str] = []

    async def on_thinking(d: str) -> None:
        seen_thinking.append(d)

    async def on_content(d: str) -> None:
        seen_answer.append(d)

    with patch("babo.providers.openai_compat_provider.AsyncOpenAI") as client:
        client.return_value.chat.completions.create = create
        result = await _provider().chat_stream(messages=[{"role": "user", "content": "hi"}], model=MODEL,
                                               on_content_delta=on_content, on_thinking_delta=on_thinking)
    return seen_thinking, seen_answer, result


async def test_inline_thinking_in_a_stream_is_shown_as_thinking_and_kept_out_of_the_answer() -> None:
    seen_thinking, seen_answer, result = await _run(AsyncMock(return_value=_stream("<think>Look at", " the board.</think>", "\n\nNothing open.")))
    assert "".join(seen_thinking) == "Look at the board."
    assert "".join(seen_answer) == "Nothing open."
    assert result.content == "Nothing open." and result.reasoning_content == "Look at the board."  # type: ignore[attr-defined]


async def test_a_template_that_opens_thinking_is_learned_and_read_that_way_next_time() -> None:
    _, _, first = await _run(AsyncMock(return_value=_stream("Look at the board.", "</think>\n\nNothing open.")))
    assert first.content == "Nothing open." and first.reasoning_content == "Look at the board."  # type: ignore[attr-defined]
    assert thinking.opens_thinking(BASE, MODEL)
    seen_thinking, seen_answer, second = await _run(AsyncMock(return_value=_stream("Check ", "letters.</think>", "One letter.")))
    assert "".join(seen_thinking) == "Check letters." and "".join(seen_answer) == "One letter."  # this time, as it streamed
    assert second.content == "One letter."  # type: ignore[attr-defined]


async def test_thinking_in_its_own_field_passes_through_and_unlearns_the_template_guess() -> None:
    thinking.remember_opens_thinking(BASE, MODEL)
    seen_thinking, seen_answer, result = await _run(AsyncMock(return_value=_stream("The answer.", reasoning="Separate thoughts.")))
    assert seen_thinking == ["Separate thoughts."] and "".join(seen_answer) == "The answer."
    assert result.content == "The answer." and not thinking.opens_thinking(BASE, MODEL)  # type: ignore[attr-defined]


async def test_a_whole_reply_with_inline_thinking_is_split_too() -> None:
    completion = ChatCompletion.model_validate({
        "id": "c", "object": "chat.completion", "created": 0, "model": MODEL,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "<think>Hmm.</think>Yes."}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })
    with patch("babo.providers.openai_compat_provider.AsyncOpenAI") as client:
        client.return_value.chat.completions.create = AsyncMock(return_value=completion)
        result = await _provider().chat(messages=[{"role": "user", "content": "hi"}], model=MODEL)
    assert result.content == "Yes." and result.reasoning_content == "Hmm."
