"""Thinking that arrives inside the reply text, told apart from the answer.

A model's thinking reaches Babo in one of three ways:

- **its own field** (``reasoning_content``): llama.cpp's default with Qwen, DeepSeek, most
  hosted reasoning models. Nothing to do here.
- **inline**, as ``<think>…</think>`` at the start of the reply (llama.cpp with
  ``--reasoning-format none``, Ollama, some gateways).
- **opened by the template**: the chat template ends the prompt with ``<think>``, so the
  reply is ``…thinking…</think>answer`` with no opening tag (Qwen3 thinking models with
  ``--reasoning-format none``). Nothing marks where the thinking starts, so Babo learns
  it: the first reply with a closing tag and no opening one marks that model as one whose
  replies start in thinking, and from then on its replies are read that way.

:class:`ThinkingSplitter` routes a streamed reply into thinking and answer as it arrives;
:func:`split_thinking` does the same for a whole reply. Both are used where replies come in
(the OpenAI-compatible provider), so every place after it (the web interface, the terminal,
channels, Babo's gateway to opencode, memory) sees thinking as thinking.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Literal

from loguru import logger

Part = tuple[Literal["thinking", "answer"], str]

_TAGS = ("think", "thinking", "thought")
_OPEN = re.compile(r"<(think|thinking|thought)>")
_CLOSE = re.compile(r"</(think|thinking|thought)>")
_OPENERS = tuple(f"<{t}>" for t in _TAGS)
_CLOSERS = tuple(f"</{t}>" for t in _TAGS)


def _partial_suffix(text: str, tags: tuple[str, ...]) -> int:
    """How many characters at the end of ``text`` could be the start of one of ``tags``."""
    longest = 0
    for tag in tags:
        for n in range(min(len(tag) - 1, len(text)), 0, -1):
            if text.endswith(tag[:n]):
                longest = max(longest, n)
                break
    return longest


class ThinkingSplitter:
    """Route a streamed reply's text into thinking and answer, as it arrives.

    ``opens_thinking``: the model's template opens thinking itself (learned, see
    :func:`opens_thinking`). ``orphan`` is set when a reply turns out to have done that
    without it being known; the caller remembers it for next time.
    """

    def __init__(self, *, opens_thinking: bool = False) -> None:
        self._state: Literal["start", "thinking", "answer"] = "thinking" if opens_thinking else "start"
        self._buf: str = ""
        self._opened: bool = opens_thinking
        self._answer_started: bool = False
        self.orphan: bool = False
        self.saw_thinking: bool = False

    def feed(self, text: str) -> list[Part]:
        self._buf += text
        out: list[Part] = []
        while self._buf:
            if self._state == "start":
                stripped = self._buf.lstrip()
                if not stripped:
                    return out
                if any(o.startswith(stripped) for o in _OPENERS) and stripped not in _OPENERS:
                    return out  # perhaps the start of an opening tag: wait for more
                m = _OPEN.match(stripped)
                if m:
                    self._buf, self._state, self._opened = stripped[m.end():], "thinking", True
                    continue
                self._state = "answer"
                continue
            if self._state == "thinking":
                m = _CLOSE.search(self._buf)
                if m:
                    self._emit(out, "thinking", self._buf[:m.start()])
                    self._buf, self._state = self._buf[m.end():].lstrip(), "answer"
                    continue
                keep = _partial_suffix(self._buf, _CLOSERS)
                self._emit(out, "thinking", self._buf[:len(self._buf) - keep])
                self._buf = self._buf[len(self._buf) - keep:]
                return out
            # answer
            close = _CLOSE.search(self._buf)
            opening = _OPEN.search(self._buf)
            if close and not self._opened and (opening is None or close.start() < opening.start()):
                # A closing tag with no opening one: what came before was thinking (the template opened it).
                # (While streaming, what already went out as answer can't be taken back; the reply as a
                # whole is read again at the end, and the next reply starts in thinking.)
                self.orphan = True
                self._opened = True
                self._emit(out, "thinking", self._buf[:close.start()])
                self._buf = self._buf[close.end():].lstrip()
                continue
            if opening:
                self._emit(out, "answer", self._buf[:opening.start()])
                self._buf, self._state = self._buf[opening.end():], "thinking"
                continue
            keep = _partial_suffix(self._buf, _OPENERS + (_CLOSERS if not self._opened else ()))
            self._emit(out, "answer", self._buf[:len(self._buf) - keep])
            self._buf = self._buf[len(self._buf) - keep:]
            return out
        return out

    def native(self) -> None:
        """Thinking is arriving in its own field: the reply's text is all answer."""
        if self._state != "answer" and not self.saw_thinking:
            self._state, self._opened = "answer", True

    def finish(self) -> list[Part]:
        """The rest, once the reply is complete (an unfinished tag is just text)."""
        rest: str = self._buf
        self._buf = ""
        out: list[Part] = []
        if self._state == "thinking":
            self._emit(out, "thinking", rest)
        elif rest.strip() or self._state == "answer":
            self._emit(out, "answer", rest)
        return out

    def _emit(self, out: list[Part], kind: Literal["thinking", "answer"], text: str) -> None:
        if not text:
            return
        if kind == "thinking":
            self.saw_thinking = True
        elif not self._answer_started:
            text = text.lstrip()  # whitespace between the thinking and the answer
            if not text:
                return
            self._answer_started = True
        if out and out[-1][0] == kind:
            out[-1] = (kind, out[-1][1] + text)
        else:
            out.append((kind, text))


def split_thinking(text: str, *, opens_thinking: bool = False) -> tuple[str | None, str, bool]:
    """``(thinking, answer, orphan)`` for a whole reply. ``thinking`` is None when there was none."""
    if "<" not in text:
        return None, text, False
    if opens_thinking and not _CLOSE.search(text):
        opens_thinking = False  # no closing tag: this reply didn't think (or its template didn't open it)
    splitter = ThinkingSplitter(opens_thinking=opens_thinking)
    parts = splitter.feed(text) + splitter.finish()
    if splitter.orphan:  # all of it before the closing tag was thinking
        parts = (again := ThinkingSplitter(opens_thinking=True)).feed(text) + again.finish()
    thinking = "".join(t for k, t in parts if k == "thinking").strip()
    answer = "".join(t for k, t in parts if k == "answer").strip()
    return (thinking or None), answer, splitter.orphan


# -- which models' templates open thinking (learned) ------------------------------------

_lock = threading.Lock()
_known: set[str] | None = None


def _path() -> Path:
    from babo.config.paths import get_runtime_subdir

    return get_runtime_subdir("models") / "opens-thinking.json"


def _key(api_base: str | None, model: str | None) -> str:
    return f"{(api_base or '').rstrip('/')}|{model or ''}"


def _load() -> set[str]:
    global _known
    if _known is None:
        try:
            _known = set(json.loads(_path().read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            _known = set()
    return _known


def opens_thinking(api_base: str | None, model: str | None) -> bool:
    with _lock:
        return _key(api_base, model) in _load()


def forget_opens_thinking(api_base: str | None, model: str | None) -> None:
    """The model's thinking now arrives some other way (its own field, or not at all)."""
    with _lock:
        known = _load()
        key = _key(api_base, model)
        if key not in known:
            return
        known.discard(key)
        try:
            _path().write_text(json.dumps(sorted(known)), encoding="utf-8")
        except OSError:
            pass


def remember_opens_thinking(api_base: str | None, model: str | None) -> None:
    """This model's replies start in thinking (its template opens it): read them that way from now on."""
    with _lock:
        known = _load()
        key = _key(api_base, model)
        if key in known:
            return
        known.add(key)
        try:
            _path().write_text(json.dumps(sorted(known)), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not remember that {} opens thinking: {}", model, exc)
    logger.info("Babo: {}'s replies start in thinking (its template opens it); reading them that way from now on.", model)
