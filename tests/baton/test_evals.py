"""`babo eval`: representative tasks with checks, run through the gateway, a baseline per preset."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from babo.baton import evals
from babo.baton.config import BatonModelConfig
from babo.baton.gateway import RouterSource
from babo.baton.queue import ModelQueue
from babo.baton.town import Town
from baton.mock_llama import MockLlama
from baton.test_gateway import _config, _serve

CHECKPOINT = {"thread": "the index", "doing": "indexing", "done": ["listed notes"], "learned": [], "next": ["write index"],
              "blockers": [], "confidence": 0.8, "knowledge_ops": [], "promote": []}


def good(task: evals.EvalTask) -> dict[str, Any]:
    """The reply a task hopes for."""
    expect = task.expect
    if expect.schema:
        return {"content": json.dumps({**CHECKPOINT, **({"revise_self": ""} if expect.schema == "fold" else {})})}
    if expect.tool:
        args = {key: value for key, value in expect.args.items()}
        return {"content": "", "tool_calls": [{"id": "c", "type": "function", "function": {"name": expect.tool, "arguments": json.dumps(args)}}]}
    return {"content": re.sub(r"\\b", "", expect.answer).split("|")[0] or "Done."}


def bad(task: evals.EvalTask) -> dict[str, Any]:
    expect = task.expect
    if expect.schema:
        return {"content": "Sure! I indexed the notes."}
    if expect.never[0]:
        args = {"command": "rm -rf ~/baton-town"} if expect.never[1] else {"query": "weather"}
        return {"content": "", "tool_calls": [{"id": "c", "type": "function", "function": {"name": expect.never[0], "arguments": json.dumps(args)}}]}
    if expect.tool:
        return {"content": "I would do that, but I won't."} if not expect.answer else {
            "content": "", "tool_calls": [{"id": "c", "type": "function", "function": {"name": "post_board", "arguments": "{}"}}]}
    return {"content": "", "tool_calls": [{"id": "c", "type": "function", "function": {"name": "shell", "arguments": '{"command": "ls"}'}}]}


def test_there_are_about_thirty_tasks_across_the_work() -> None:
    assert len(evals.TASKS) >= 30 and len({t.id for t in evals.TASKS}) == len(evals.TASKS)
    assert {t.category for t in evals.TASKS} == {"tool choice", "restraint", "following through", "where it is now", "arguments", "memory"}
    for task in evals.TASKS:
        assert all(name in evals.TOOLS for name in task.tools)
        assert not task.expect.tool or task.expect.tool in task.tools, task.id  # what it should call is offered
        assert task.expect.never[0] not in task.tools or task.expect.never[1], task.id


def test_each_check_passes_the_reply_it_hopes_for_and_fails_a_wrong_one() -> None:
    for task in evals.TASKS:
        assert evals.check(task, good(task))[0], (task.id, evals.check(task, good(task)))
        passed, why = evals.check(task, bad(task))
        assert not passed and why, task.id


def test_checks_say_why_a_reply_failed() -> None:
    letter = next(t for t in evals.TASKS if t.id == "exact-letter")
    reply = {"content": "", "tool_calls": [{"id": "c", "type": "function", "function": {
        "name": "send_letter", "arguments": json.dumps({"to": "inspector", "text": "The note is ready."})}}]}
    assert evals.check(letter, reply) == (False, "send_letter text='The note is ready.' (expected it to contain 'the soil note is ready')")
    assert evals.select(["restraint"]) and all(t.category == "restraint" for t in evals.select(["restraint"]))
    assert [t.id for t in evals.select(["clock"])] == ["clock"]


async def test_a_run_through_the_gateway_is_scored_kept_and_compared(tmp_path: Path, mock_llama: MockLlama, town: Town) -> None:
    path = tmp_path / "config.json"
    queue = ModelQueue(BatonModelConfig(upstream=mock_llama.base_url), router=RouterSource(path))
    runner, base = await _serve(queue)
    tasks = evals.select(["tool choice"])[:3] + evals.select(["no-tool-math", "checkpoint-json"])
    try:
        _config(path, upstream=mock_llama.base_url, queue=base, cloud="http://127.0.0.1:9/v1")
        model = BatonModelConfig(endpoint=base, upstream=mock_llama.base_url)

        def answer(body: dict[str, Any]) -> dict[str, Any]:
            task = next(t for t in evals.TASKS if list(t.messages) == body["messages"])
            reply = good(task)
            if task.id == "remember-fact":  # written as text, the way a local model sometimes does: the gateway repairs it
                return {"content": '<tool_call>{"name": "remember", "arguments": {"text": "likes tomatoes"}}</tool_call>'}
            if reply.get("tool_calls"):
                return {"tool_calls": [{"name": c["function"]["name"], "arguments": c["function"]["arguments"]} for c in reply["tool_calls"]]}
            return reply

        mock_llama.default = answer
        report = await evals.run(model, "town", tasks, real_model="local-35b")
        assert report.passed == len(tasks) and sum(o.repaired for o in report.outcomes) == 1
        assert mock_llama.requests[0]["model"] == "local-35b"  # through the gateway, to the preset's model
        evals.save(town, report, baseline=True)
        # Later the model gets something wrong: the comparison says which task got worse.
        mock_llama.default = lambda body: {"content": "I'd rather not."} if "tomatoes" in json.dumps(body["messages"]) else answer(body)
        later = await evals.run(model, "town", tasks)
        assert later.passed == len(tasks) - 1
        assert evals.compare(later, evals.baseline(town, "town")) == ([], ["remember-fact"])
        evals.save(town, later)
        [row] = evals.latest(town)
        assert (row["preset"], row["passed"], row["baseline"]["passed"]) == ("town", len(tasks) - 1, len(tasks))
    finally:
        await runner.cleanup()


def test_the_terminal_lists_the_tasks() -> None:
    from typer.testing import CliRunner

    from babo.baton import cli

    result = CliRunner().invoke(cli.app, ["eval", "--list"])
    assert result.exit_code == 0 and "baton-carry-on" in result.output and f"{len(evals.TASKS)} tasks." in result.output


async def test_the_web_interface_runs_one_in_the_background(tmp_path: Path, mock_llama: MockLlama, town: Town) -> None:
    import asyncio

    from babo.baton.town import BatonError

    path = tmp_path / "config.json"
    queue = ModelQueue(BatonModelConfig(upstream=mock_llama.base_url), router=RouterSource(path))
    runner, base = await _serve(queue)
    try:
        _config(path, upstream=mock_llama.base_url, queue=base, cloud="http://127.0.0.1:9/v1")
        mock_llama.default = lambda body: {"content": "Done."}
        model = BatonModelConfig(endpoint=base, upstream=mock_llama.base_url)
        assert "Evaluating town" in evals.start_background(town, model, "town", baseline_too=True)
        try:
            evals.start_background(town, model, "town")
            raise AssertionError("a second run started")
        except BatonError as exc:
            assert "already running" in str(exc)
        for _ in range(200):
            if evals.background_status(town, ["town"])["running"] is None:
                break
            await asyncio.sleep(0.05)
        [row] = evals.background_status(town, ["town"])["latest"]
        assert row["total"] == len(evals.TASKS) and row["baseline"]["total"] == len(evals.TASKS)
        assert 0 < row["passed"] < row["total"]  # "Done." is right for some tasks, not for most
    finally:
        await runner.cleanup()
