"""H3: going round in circles is noticed, tasks are checked before they count as done, memory stays small."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from babo.baton import drift, notices, opencode
from babo.baton.baton_file import BATON_THREADS, Checkpoint, append_checkpoint
from babo.baton.schemas import parse_reply
from babo.baton.town import BatonError, Town


def test_the_same_call_three_times_or_a_refused_tool_again_trips_a_rule() -> None:
    watch = drift.Watch()
    see = lambda tool, args, failed=False, result="ok": watch.see("s", tool, args, failed=failed, result=result)  # noqa: E731
    assert see("read", {"path": "a"}) is None and see("read", {"path": "a"}) is None
    assert see("read", {"path": "a"}) == ("repeat", 1)
    assert see("read", {"path": "a"}) is None  # one reminder per run of repeats
    assert see("shell", {"command": "rm x"}, True, "Error: Permission denied") is None
    assert see("shell", {"command": "rm y"}, True, "Blocked: not allowed") == ("refused_again", 1)
    assert see("shell", {"command": "ls"}, False, "a b") is None  # a call that works is fine
    assert drift.reminder("repeat", "read").startswith("[Babo: You've made the same `read` call 3 times")


async def test_the_mayor_is_reminded_and_the_owner_told_the_second_time(make_loop, mock_llama, town: Town) -> None:
    from baton.mock_llama import text, tool_call

    same = tool_call("search_memory", {"query": "soil"})
    mock_llama.push(same, same, same, same, same, same, text("I'm stuck."))
    loop = make_loop("mayor")
    await loop.process_direct("What do we know about soil?", session_key="cli:loop")
    results = [m["content"] for m in mock_llama.requests[-1]["messages"] if m["role"] == "tool"]
    assert sum("[Babo: You've made the same `search_memory` call 3 times" in r for r in results) == 2
    [stuck] = [n for n in notices.recent(town) if n["kind"] == "stuck"]
    assert "mayor seems stuck" in stuck["text"] and "search_memory" in stuck["text"]


def test_memory_files_stay_small(town: Town, box) -> None:
    home = town.resident_dir("archivist")
    for i in range(BATON_THREADS + 3):
        append_checkpoint(home, Checkpoint(thread=f"thread {i}", doing="x", resident="archivist", at=f"2026-11-01T10:{i:02d}:00+00:00"))
    baton = (home / "BATON.md").read_text()
    assert baton.count("thread ") == BATON_THREADS and "3 older thread(s) are in checkpoints/" in baton
    reply, _ = parse_reply({"thread": "t", "doing": "", "done": [f"item {i}" * 100 for i in range(20)], "learned": [], "next": [],
                            "blockers": [], "confidence": 0.5, "knowledge_ops": [], "promote": []})
    assert reply is not None and len(reply.done) == 8 and all(len(item) <= 300 for item in reply.done)
    with pytest.raises(BatonError, match="at most about 1200 tokens"):
        box("archivist").call("revise_self", new_text="word " * 3000, reason="everything about me")


def _running_idle(monkeypatch: pytest.MonkeyPatch, prompts: list[str]) -> None:
    monkeypatch.setattr(opencode, "running", lambda *_a, **_k: object())
    monkeypatch.setattr(opencode, "live", lambda *_a, **_k: {"state": "idle", "activity": "", "questions": []})

    def prompt(town: Town, config: Any, session: opencode.Session, text: str, *, config_path: Any) -> str:
        prompts.append(text)
        session = opencode.get_session(town, session.resident, session.id)
        session.last_active = (datetime.now().astimezone() + timedelta(seconds=len(prompts))).isoformat()  # it worked on it
        opencode.save_session(town, session)
        return "m"

    monkeypatch.setattr(opencode, "prompt", prompt)


def test_a_task_with_a_done_check_waits_for_it_and_hears_what_failed(town: Town, make_config, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = make_config("mayor")
    prompts: list[str] = []
    _running_idle(monkeypatch, prompts)
    session = opencode.new_session(town, cfg, "archivist", title="Index", done_check="test -f index.md")
    session.task, session.opencode_id, session.last_active = "Write index.md", "ses_1", "2026-11-01T10:00:00+00:00"
    opencode.save_session(town, session)
    now = datetime.now().astimezone() + timedelta(hours=1)
    assert opencode.wrap_up_reason(town, cfg, opencode.get_session(town, "archivist", session.id), now=now) == ""  # not before its check
    assert opencode.check_done(town, cfg, config_path=tmp_path) == [f"archivist/{session.id}: done-check failed (1)"]
    assert "its check `test -f index.md` failed" in prompts[0]
    (town.resident_dir("archivist") / "index.md").write_text("# Index\n")
    assert opencode.check_done(town, cfg, config_path=tmp_path) == [f"archivist/{session.id}: done-check passed"]
    assert opencode.check_done(town, cfg, config_path=tmp_path) == []  # once passed, done
    assert opencode.wrap_up_reason(town, cfg, opencode.get_session(town, "archivist", session.id), now=now) != ""


def test_a_done_check_that_keeps_failing_tells_the_owner_and_lets_it_wrap_up(town: Town, make_config, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = make_config("mayor")
    prompts: list[str] = []
    _running_idle(monkeypatch, prompts)
    session = opencode.new_session(town, cfg, "archivist", title="Index", done_check="false")
    session.task, session.opencode_id, session.last_active = "Write index.md", "ses_1", "2026-11-01T10:00:00+00:00"
    opencode.save_session(town, session)
    opencode.check_done(town, cfg, config_path=tmp_path)
    opencode.check_done(town, cfg, config_path=tmp_path)
    assert len(prompts) == 1 and opencode.get_session(town, "archivist", session.id).done_status == "failed"
    [note] = [n for n in notices.recent(town) if n["kind"] == "not_done"]
    assert "still fails after 2 tries" in note["text"]


def test_only_the_owner_gives_a_session_a_done_check(town: Town, make_config) -> None:
    with pytest.raises(BatonError, match="Only Robin can give a session a done-check"):
        opencode.new_session(town, make_config("mayor"), "archivist", done_check="rm -rf ~", by="mayor")
