"""Phase 2: event log, triggers, checkpoint turns, fallback, gate, compaction, queue, context view."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from baton.mock_llama import MockLlama, http_error, malformed, schema, text, tool_call
from nanobot.agent.hook import AgentHookContext, AgentRunHookContext
from nanobot.baton.baton_file import current_entry, iter_checkpoints
from nanobot.baton.checkpoint import CHECKPOINT_MARK
from nanobot.baton.compaction import read_backup, restore_session_backup, write_backup
from nanobot.baton.config import BatonConfig, BatonModelConfig
from nanobot.baton.events import classify, resident_events
from nanobot.baton.hooks import BatonTurnHook
from nanobot.baton.queue import ModelQueue, SlotQueue, resident_of
from nanobot.baton.runtime_state import CounterStore
from nanobot.baton.town import Town
from nanobot.providers.base import ToolCallRequest

GOOD = {
    "thread": "garden",
    "doing": "laying out raised beds",
    "done": ["measured the plot"],
    "learned": ["the plot drains to the east"],
    "next": ["order soil"],
    "blockers": [],
    "confidence": 0.8,
    "knowledge_ops": [
        {"op": "create", "title": "Plot drainage", "body": "The plot drains to the east.", "path": "",
         "aliases": ["runoff"], "keywords": ["drainage", "plot"]},
    ],
    "promote": [{"path": "knowledge/plot-drainage.md", "reason": "useful to anyone gardening"}],
}


def router(*, checkpoint: list[dict[str, Any]] | None = None, chat: list[dict[str, Any]] | None = None):
    """Answer schema requests from one script and ordinary chat from another."""
    cps = list(checkpoint or [])
    chats = list(chat or [])

    def reply(body: dict[str, Any]) -> dict[str, Any]:
        if "response_format" in body:
            return cps.pop(0) if cps else malformed()
        return chats.pop(0) if chats else text("done")

    return reply


def _formation(make_loop, resident: str = "mayor", **overrides: Any):
    loop = make_loop(resident, **overrides)
    return loop, loop.baton.formation  # type: ignore[attr-defined]


def _call(name: str, **args: Any) -> ToolCallRequest:
    return ToolCallRequest(id=f"c-{name}", name=name, arguments=args)


# -- event log -------------------------------------------------------------------
def test_classify_commit_tests_exit_codes_and_redaction() -> None:
    commit = classify("exec", {"command": "git add -A && git commit -m 'x'"}, "\n\nExit code: 0", error=False)
    assert commit.commit and commit.exit_code == 0
    failed_commit = classify("exec", {"command": "git commit -m x"}, "Exit code: 1", error=False)
    assert not failed_commit.commit and failed_commit.error
    tests = classify("exec", {"command": "python -m pytest -q tests/"}, "1 failed\nExit code: 1", error=False)
    assert tests.test == "fail"
    assert classify("exec", {"command": "pytest"}, "Exit code: 0", error=False).test == "pass"
    assert classify("read_file", {"path": "a.py"}, "...", error=False).files == ["a.py"]
    secret = classify("exec", {"command": "curl -H 'Authorization: Bearer abcdefghijklmnopqrstu' x", "api_key": "sk-123"}, "", error=False)
    dumped = json.dumps(secret.args)
    assert "abcdefghijklmnopqrstu" not in dumped and "sk-123" not in dumped


async def test_hook_logs_every_tool_call(make_loop, town: Town) -> None:
    _, f = _formation(make_loop)
    hook = BatonTurnHook(f, "cli:t")
    await hook.before_run(AgentRunHookContext(messages=[]))
    await hook.after_execute_tool(AgentHookContext(iteration=0, messages=[]), _call("read_file", path="SELF.md"), None, {}, "text")
    await hook.on_execute_tool_error(AgentHookContext(iteration=0, messages=[]), _call("exec", command="false"), None, {}, "Exit code: 1")
    kinds = [(e["kind"], e.get("tool"), e.get("error")) for e in resident_events(town, "mayor")]
    assert kinds == [("turn_start", None, None), ("tool", "read_file", False), ("tool", "exec", True)]
    assert all(e["resident"] == "mayor" and e["session"] == "cli:t" for e in resident_events(town, "mayor"))


# -- triggers ------------------------------------------------------------------
async def test_interval_commit_and_test_flip_triggers(make_loop) -> None:
    _, f = _formation(make_loop, **{"baton.memory": {"checkpointEveryToolCalls": 3}})
    hook = BatonTurnHook(f, "cli:t")
    ctx = AgentHookContext(iteration=0, messages=[])
    for _ in range(2):
        await hook.after_execute_tool(ctx, _call("read_file", path="x"), None, {}, "ok")
    assert f.due == {}
    await hook.after_execute_tool(ctx, _call("read_file", path="x"), None, {}, "ok")
    assert f.due == {"cli:t": "interval"}
    f.due.clear()
    await hook.after_execute_tool(ctx, _call("exec", command="git commit -m done"), None, {}, "Exit code: 0")
    assert f.due["cli:t"] == "commit"
    f.due.clear()
    f.counters.update(calls_since_checkpoint=0)
    await hook.after_execute_tool(ctx, _call("exec", command="pytest"), None, {}, "Exit code: 0")
    assert f.due == {}
    await hook.after_execute_tool(ctx, _call("exec", command="pytest"), None, {}, "Exit code: 1")
    assert f.due["cli:t"] == "tests_flipped"


async def test_checkpoint_tool_requests_a_turn(make_loop) -> None:
    loop, f = _formation(make_loop)
    from nanobot.agent.tools.context import (
        RequestContext,
        bind_request_context,
        reset_request_context,
    )

    token = bind_request_context(RequestContext(channel="cli", chat_id="d", session_key="cli:x"))
    try:
        out = await loop.tools.get("checkpoint").execute(note="wrapping up")
    finally:
        reset_request_context(token)
    assert "checkpoint turn will follow" in out
    assert f.due == {"cli:x": "requested"}


# -- checkpoint turns ----------------------------------------------------------
async def test_authored_checkpoint_is_filed(make_loop, mock_llama: MockLlama, town: Town, index_path: Path) -> None:
    _, f = _formation(make_loop)
    mock_llama.push(schema(GOOD))
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "plan the garden"}]
    outcome = await f.checkpoint_now("cli:t", messages, "requested")
    assert outcome.authored and outcome.checkpoint.thread == "garden"
    home = town.resident_dir("mayor")
    assert "laying out raised beds" in (home / "BATON.md").read_text()
    assert (home / "knowledge" / "plot-drainage.md").exists()
    promo = next(town.promotions.glob("*.md")).read_text()
    assert "The plot drains to the east." in promo and "from: mayor" in promo
    assert f.counters.load().calls_since_checkpoint == 0
    request = mock_llama.requests[-1]
    assert request["messages"][-1]["content"].startswith(CHECKPOINT_MARK)
    assert request["response_format"]["type"] == "json_schema"
    assert all("_sender" not in m for m in request["messages"])
    archived = list((home / "sessions").rglob("*.checkpoints.jsonl"))
    assert archived and "baton-harness" in archived[0].read_text()
    from nanobot.baton.search import open_index

    assert open_index(index_path).search("runoff", owners=("mayor",), k=1)[0].title == "Plot drainage"


async def test_checkpoint_retries_once_then_succeeds(make_loop, mock_llama: MockLlama) -> None:
    _, f = _formation(make_loop)
    mock_llama.push(malformed(), schema(GOOD))
    outcome = await f.checkpoint_now("cli:t", [{"role": "user", "content": "x"}], "interval")
    assert outcome.authored and outcome.attempts == 2
    assert "could not be filed" in mock_llama.requests[-1]["messages"][-1]["content"]


async def test_two_failures_write_a_mechanical_checkpoint(make_loop, mock_llama: MockLlama, town: Town) -> None:
    _, f = _formation(make_loop)
    hook = BatonTurnHook(f, "cli:t")
    ctx = AgentHookContext(iteration=0, messages=[])
    await hook.after_execute_tool(ctx, _call("exec", command="pytest -q"), None, {}, "Exit code: 1")
    await hook.after_execute_tool(ctx, _call("write_file", path="projects/g/beds.md"), None, {}, "ok")
    mock_llama.push(malformed(), schema({"thread": ""}))
    outcome = await f.checkpoint_now("cli:t", [{"role": "user", "content": "x"}], "interval")
    assert not outcome.authored
    cp = current_entry(town.resident_dir("mayor"))
    assert cp is not None and cp.source == "mechanical" and cp.confidence == 0.0
    assert any("pytest" in d and "exit 1" in d for d in cp.done)
    assert any("beds.md" in d for d in cp.done)
    assert "mechanical record" in (town.resident_dir("mayor") / "BATON.md").read_text()


async def test_unreachable_model_falls_back_to_mechanical(make_loop, town: Town) -> None:
    _, f = _formation(make_loop, **{"baton.model": {"endpoint": "http://127.0.0.1:9/v1", "requestTimeoutS": 10}})
    outcome = await f.checkpoint_now("cli:t", [{"role": "user", "content": "x"}], "idle")
    assert not outcome.authored and "unavailable" in outcome.error


async def test_after_iteration_runs_due_checkpoint_with_live_messages(make_loop, mock_llama: MockLlama) -> None:
    _, f = _formation(make_loop)
    hook = BatonTurnHook(f, "cli:t")
    mock_llama.push(schema(GOOD))
    f.mark_due("cli:t", "commit")
    live = [{"role": "system", "content": "S"}, {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "1", "name": "exec", "content": "Exit code: 0"}]
    await hook.after_iteration(AgentHookContext(iteration=1, messages=live, tool_calls=[_call("exec")]))
    sent = mock_llama.requests[-1]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool", "user"]
    assert f.due == {}


async def test_full_turn_checkpoint_stays_out_of_carried_context(make_loop, mock_llama: MockLlama, town: Town) -> None:
    mock_llama.default = router(
        checkpoint=[schema(GOOD)],
        chat=[tool_call("read_note", {"path": "SELF.md"}), tool_call("read_note", {"path": "ROLE.md"}), text("All set.")],
    )
    loop = make_loop("mayor", **{"baton.memory": {"checkpointEveryToolCalls": 2}})
    reply = await loop.process_direct("Look around.", session_key="cli:full")
    assert "All set" in str(reply)
    assert any("response_format" in r for r in mock_llama.requests)
    assert current_entry(town.resident_dir("mayor")).thread == "garden"
    session = await loop.sessions.state.read("cli:full")
    replay = json.dumps(session.get_history())
    assert CHECKPOINT_MARK not in replay and "laying out raised beds" not in replay
    await loop.aclose()


async def test_session_end_flushes_checkpoint(make_loop, mock_llama: MockLlama, town: Town) -> None:
    mock_llama.default = router(checkpoint=[schema(GOOD)], chat=[tool_call("read_note", {"path": "SELF.md"}), text("ok")])
    loop = make_loop("mayor")
    await loop.process_direct("hi", session_key="cli:end")
    assert iter_checkpoints(town.resident_dir("mayor")) == []
    await loop.aclose()
    assert [c.trigger for c in iter_checkpoints(town.resident_dir("mayor"))] == ["session_end"]


async def test_idle_timer_checkpoints_after_work(make_loop, mock_llama: MockLlama, town: Town) -> None:
    _, f = _formation(make_loop)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    f.sleep = fake_sleep
    f.counters.update(work_since_checkpoint=True)
    mock_llama.push(schema(GOOD))
    f.schedule_idle("cli:i", [{"role": "user", "content": "x"}])
    await asyncio.sleep(0.5)
    for _ in range(50):
        if iter_checkpoints(town.resident_dir("mayor")):
            break
        await asyncio.sleep(0.1)
    assert slept and [c.trigger for c in iter_checkpoints(town.resident_dir("mayor"))] == ["idle"]


# -- commit gate -----------------------------------------------------------------
async def test_commit_gate_blocks_stale_commit_until_checkpoint(make_loop, mock_llama: MockLlama, town: Town) -> None:
    loop, f = _formation(make_loop, **{"baton.memory": {"commitGateAfterToolCalls": 3}})
    project = town.resident_dir("mayor") / "projects" / "g"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / "a.txt").write_text("a")
    exec_tool = loop.tools.get("exec")
    from nanobot.agent.tools.shell import ExecTool

    assert isinstance(exec_tool, ExecTool)  # still the same tool object
    f.counters.update(calls_since_checkpoint=4)
    cmd = "git -c user.name=t -c user.email=t@t -c commit.gpgsign=false add -A && git -c user.name=t -c user.email=t@t -c commit.gpgsign=false commit -qm first"
    blocked = await exec_tool.execute(command=cmd, working_dir=str(project))
    assert "Commit paused by the Baton harness" in blocked and "checkpoint()" in blocked
    assert subprocess.run(["git", "log"], cwd=project, capture_output=True).returncode != 0  # nothing committed
    assert any(e["kind"] == "gate" for e in resident_events(town, "mayor"))
    mock_llama.push(schema(GOOD))
    await f.checkpoint_now("cli:g", [{"role": "user", "content": "x"}], "requested")
    allowed = await exec_tool.execute(command=cmd, working_dir=str(project))
    assert "Exit code: 0" in allowed
    assert "ls" != (await exec_tool.execute(command="ls", working_dir=str(project)))  # non-commit commands never gated


# -- restore-or-nothing compaction -----------------------------------------------
async def _seed_session(loop, key: str, n: int = 6) -> None:
    session = await loop.sessions.state.get(key)
    for i in range(n):
        session.add_message("user" if i % 2 == 0 else "assistant", f"message {i}")
    await loop.sessions.state.finish_turn(session)


async def _snapshot(loop, key: str) -> tuple[list[dict[str, Any]], int]:
    s = await loop.sessions.state.read(key)
    return json.loads(json.dumps(s.messages, default=str)), s.last_consolidated


async def test_idle_compaction_success_keeps_baton_and_backs_up(make_loop, mock_llama: MockLlama, town: Town) -> None:
    mock_llama.default = router(checkpoint=[schema(GOOD)], chat=[text("We planned the garden beds.")])
    loop = make_loop("mayor")
    await _seed_session(loop, "cli:c")
    runtime = await loop.runtime_for_session(await loop.sessions.state.get("cli:c"))
    summary = await loop.consolidator.compact_idle_session("cli:c", runtime=runtime)
    assert summary and "We planned the garden beds." in summary
    assert "[Latest baton entry]" in summary and "laying out raised beds" in summary
    backups = list((town.resident_dir("mayor") / "sessions").rglob("*.pre-compact-*.jsonl"))
    assert len(backups) == 1
    header, messages = read_backup(backups[0])
    assert header["session_key"] == "cli:c" and len(messages) == 6
    after, _ = await _snapshot(loop, "cli:c")
    assert [m["content"] for m in after[:6]] == [f"message {i}" for i in range(6)]  # journal never truncated
    await loop.aclose()


async def test_idle_compaction_forced_failure_changes_nothing(make_loop, mock_llama: MockLlama, town: Town) -> None:
    mock_llama.default = router(checkpoint=[malformed(), malformed()], chat=[http_error(500)] * 4)
    loop = make_loop("mayor")
    await _seed_session(loop, "cli:f")
    before = await _snapshot(loop, "cli:f")
    runtime = await loop.runtime_for_session(await loop.sessions.state.get("cli:f"))
    result = await loop.consolidator.compact_idle_session("cli:f", runtime=runtime)
    assert not result
    assert await _snapshot(loop, "cli:f") == before
    events = resident_events(town, "mayor")
    assert any(e["kind"] == "compaction" and e.get("phase") == "refused" for e in events)
    assert current_entry(town.resident_dir("mayor")).source == "mechanical"  # the baton is never blank
    await loop.aclose()


async def test_in_turn_compaction_uses_authored_checkpoint_when_summary_fails(make_loop, mock_llama: MockLlama) -> None:
    mock_llama.default = router(checkpoint=[schema(GOOD)], chat=[http_error(500)] * 4)
    loop = make_loop("mayor")
    await _seed_session(loop, "cli:s")
    runtime = await loop.runtime_for_session(await loop.sessions.state.get("cli:s"))
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    summary = await loop.consolidator.summarize_transcript(msgs, None, runtime=runtime, session_key="cli:s", tools=[])
    assert summary and "stands in for it" in summary and "laying out raised beds" in summary
    await loop.aclose()


async def test_in_turn_compaction_refuses_when_nothing_sound(make_loop, mock_llama: MockLlama) -> None:
    mock_llama.default = router(checkpoint=[malformed(), malformed()], chat=[http_error(500)] * 4)
    loop = make_loop("mayor")
    runtime = await loop.runtime_for_session(await loop.sessions.state.get("cli:r"))
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    assert await loop.consolidator.summarize_transcript(msgs, None, runtime=runtime, session_key="cli:r", tools=[]) is None
    await loop.aclose()


def test_backup_hash_is_verified_and_offline_restore(tmp_path: Path, town: Town) -> None:
    from nanobot.session.manager import Session, SessionManager

    home = town.resident_dir("mayor")
    manager = SessionManager(home, sessions_root=tmp_path / "sessions")
    session = Session(key="cli:b", messages=[{"role": "user", "content": f"m{i}"} for i in range(4)])
    manager.save(session)
    backup = write_backup(home, "cli:b", session.messages)
    damaged = manager.get_or_create("cli:b")
    damaged.messages = damaged.messages[:1]
    manager.save(damaged)
    assert restore_session_backup(SessionManager(home, sessions_root=tmp_path / "sessions"), backup) == 4
    assert [m["content"] for m in SessionManager(home, sessions_root=tmp_path / "sessions").get_or_create("cli:b").messages] == ["m0", "m1", "m2", "m3"]
    backup.write_text(backup.read_text() + "tampered\n")
    with pytest.raises(Exception, match="hash check"):
        read_backup(backup)


# -- model queue -----------------------------------------------------------------
async def test_slot_queue_priority_and_limit() -> None:
    q = SlotQueue(1)
    order: list[str] = []
    await q.acquire(0)

    async def take(name: str, priority: int) -> None:
        await q.acquire(priority)
        order.append(name)
        await asyncio.sleep(0.01)
        q.release()

    tasks = [asyncio.create_task(take("night", 2)), asyncio.create_task(take("visit", 1)), asyncio.create_task(take("chat", 0))]
    await asyncio.sleep(0.01)
    q.release()
    await asyncio.gather(*tasks)
    assert order == ["chat", "visit", "night"]
    assert q.max_active == 1


async def test_proxy_never_runs_two_calls_on_one_slot_and_captures_exact_bytes(mock_llama: MockLlama, tmp_path: Path) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    mock_llama.delay_s = 0.15
    queue = ModelQueue(BatonModelConfig(upstream=mock_llama.base_url, slots=1), capture_dir=tmp_path / "cap")
    async with TestClient(TestServer(queue.app())) as client:
        bodies = [
            json.dumps({"model": "m", "messages": [{"role": "system", "content": f"You are the resident `r{i}` of this town"}]}).encode()
            for i in range(4)
        ]
        responses = await asyncio.gather(*[client.post("/v1/chat/completions", data=b, headers={"Content-Type": "application/json"}) for b in bodies])
        assert all(r.status == 200 for r in responses)
        assert mock_llama.max_in_flight == 1
        assert (tmp_path / "cap" / "r2.json").read_bytes() == bodies[2]
        health = await client.get("/health")
        assert health.status == 200
    assert isinstance(web, object)


def test_resident_identified_from_header_or_bundle() -> None:
    assert resident_of({"X-Baton-Resident": "inspector"}, b"") == "inspector"
    assert resident_of({}, b'{"content":"You are the resident `mayor` of this town"}') == "mayor"
    assert resident_of({}, b"{}") == "unknown"


def test_context_view_prints_last_request(town: Town, index_path: Path) -> None:
    from nanobot.baton.context_view import last_request_text
    from nanobot.config.schema import Config

    cfg = Config.model_validate({"baton": {"town": str(town.root), "indexPath": str(index_path)}})
    assert last_request_text(cfg, "mayor") is None
    cfg.baton.capture_dir.mkdir(parents=True)
    raw = b'{"model":"m","messages":[]}'
    (cfg.baton.capture_dir / "mayor.json").write_bytes(raw)
    assert last_request_text(cfg, "mayor", raw=True) == raw.decode()
    assert '"model": "m"' in last_request_text(cfg, "mayor")


async def test_assemble_next_matches_the_loops_system_prompt(make_config, town: Town) -> None:
    from nanobot.baton.context_view import assemble_next

    out = json.loads(await assemble_next(make_config("mayor"), "archivist", "hello", raw=True))
    assert out["messages"][0]["role"] == "system" and "resident `archivist`" in out["messages"][0]["content"]
    assert out["messages"][-1]["content"].endswith("hello") or "hello" in json.dumps(out["messages"][-1])
    assert {t["function"]["name"] for t in out["tools"]} >= {"commons_write", "checkpoint"}


def test_counters_survive_a_new_store(town: Town) -> None:
    CounterStore(town, "mayor").bump()
    assert CounterStore(town, "mayor").load().calls_since_checkpoint == 1
    assert BatonConfig().memory.checkpoint_every_tool_calls == 15
