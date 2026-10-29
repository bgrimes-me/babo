"""An upkeep pass (formerly the night cycle): its idempotency and resume, tool quarantine, units."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from babo.baton.baton_file import iter_checkpoints
from babo.baton.inbox import items, unread
from babo.baton.night import Night, NightInterruptedError, mismatch_hints, outside_hints
from babo.baton.notes import split_frontmatter, write_note
from babo.baton.tooltest import run_tool_test
from babo.baton.town import Town
from baton.mock_llama import MockLlama, malformed, schema, text, tool_call

FOLD = {
    "thread": "garden", "doing": "resting after planning beds", "done": ["planned beds"], "learned": ["east drainage"],
    "next": ["order soil"], "blockers": [], "confidence": 0.7, "knowledge_ops": [], "promote": [], "revise_self": "",
}


def _router(chats: list[dict[str, Any]], folds: list[dict[str, Any]] | None = None):
    chats = list(chats)
    folds = list(folds or [])

    def reply(body: dict[str, Any]) -> dict[str, Any]:
        if "response_format" in body:
            name = body["response_format"]["json_schema"]["name"]
            if name == "fold" and folds:
                return folds.pop(0)
            return malformed()
        return chats.pop(0) if chats else text("done")

    return reply


def _commits(town: Town) -> list[str]:
    return subprocess.run(["git", "-C", str(town.root), "log", "--format=%s"], capture_output=True, text=True).stdout.splitlines()


async def _a_day_in_town(make_loop, mock_llama: MockLlama, town: Town) -> None:
    """The Mayor works, remembers something, and nominates a note."""
    mock_llama.default = _router([tool_call("remember", {"text": "Squid prefers tomatoes."}), text("Noted.")])
    loop = make_loop("mayor")
    await loop.process_direct("Remember I like tomatoes.", session_key="cli:day")
    await loop.aclose()
    write_note(town.resident_dir("mayor") / "knowledge", title="Tomatoes", body="Squid prefers tomatoes.", author="mayor")
    (town.promotions / "2026-01-01-mayor-tomatoes.md").write_text(
        "---\nfrom: mayor\nnote: residents/mayor/knowledge/tomatoes.md\ntitle: Tomatoes\nreason: town-wide preference\nstatus: pending\n---\n\nSquid prefers tomatoes.\n"
    )
    pending = town.tools / "pending" / "shout"
    pending.mkdir(parents=True)
    (pending / "tool.py").write_text("def run(input: str) -> str:\n    return input.upper()\n")
    (pending / "test_tool.py").write_text("from tool import run\n\ndef test_upper():\n    assert run('a') == 'A'\n")
    (pending / "TOOL.md").write_text("---\nname: shout\ndescription: Upper-cases text\nauthor: mayor\nstatus: pending\n---\n\nUpper-cases text.\n")


def _night_chats() -> list[dict[str, Any]]:
    return [
        tool_call("commons_write", {"title": "Tomatoes", "body": "Squid prefers tomatoes.", "authors": ["mayor"], "keywords": ["food"]}),
        text("Filed the tomato note."),
        tool_call("file_flag", {"resident": "mayor", "claim": "said it ran tests", "evidence": "no test command in the log"}),
        tool_call("review_item", {"path": "commons/tools/pending/shout", "review": "Pure function, low risk."}),
        text("One flag filed, one tool reviewed."),
    ]


async def test_full_night_cycle(make_loop, make_config, mock_llama: MockLlama, town: Town) -> None:
    await _a_day_in_town(make_loop, mock_llama, town)
    commits_before = len(_commits(town))
    mock_llama.default = _router(_night_chats(), folds=[schema(FOLD)])
    result = await Night(make_config("mayor"), night="2026-01-02", prefer_container=False).run()
    assert result["status"] == "ok" and result["ran"] == ["mayor"]

    home = town.resident_dir("mayor")
    folds = [c for c in iter_checkpoints(home) if c.trigger == "fold 2026-01-02"]
    assert len(folds) == 1 and folds[0].source == "authored"
    assert all(i.folded for i in items(town, "mayor") if i.kind == "remember")
    # Archivist: the nomination became a commons note and was marked reviewed.
    assert (town.knowledge / "tomatoes.md").exists()
    assert split_frontmatter((town.promotions / "2026-01-01-mayor-tomatoes.md").read_text())[0]["status"] == "reviewed"
    # Inspector: the flag reached the Mayor's inbox and the pending tool has a review.
    assert any(i.kind == "flag" for i in unread(town, "mayor"))
    assert (town.tools / "pending" / "shout" / "REVIEW.md").exists()
    # Quarantine: tested, never promoted.
    meta = split_frontmatter((town.tools / "pending" / "shout" / "TEST-RESULT.md").read_text())[0]
    assert meta["passed"] is True and "subprocess" in meta["isolation"]
    assert not (town.tools / "active" / "shout").exists()
    # Post-pass: transcripts exported, one night commit with a readable summary.
    assert list((home / "sessions").rglob("*.transcript.jsonl"))
    assert (town.state / "upkeep" / "2026-01-02" / "diffs" / "mayor.md").exists()
    log = _commits(town)
    assert len(log) == commits_before + 1 and log[0].startswith("Upkeep 2026-01-02: 1 memory review")
    body = subprocess.run(["git", "-C", str(town.root), "log", "-1", "--format=%b"], capture_output=True, text=True).stdout
    assert "Memory reviews: mayor (own words)" in body and "Tools tested: shout" in body
    assert subprocess.run(["git", "-C", str(town.root), "status", "--porcelain"], capture_output=True, text=True).stdout.strip() == ""


async def test_night_is_idempotent(make_loop, make_config, mock_llama: MockLlama, town: Town) -> None:
    await _a_day_in_town(make_loop, mock_llama, town)
    mock_llama.default = _router(_night_chats(), folds=[schema(FOLD)])
    await Night(make_config("mayor"), night="2026-01-02", prefer_container=False).run()
    commits = _commits(town)
    requests = len(mock_llama.requests)
    again = await Night(make_config("mayor"), night="2026-01-02", prefer_container=False).run()
    assert again["status"] == "ok"
    assert _commits(town) == commits
    assert len(mock_llama.requests) == requests  # nothing re-asked of the model
    # The next night finds nothing new: nobody ran.
    mock_llama.default = _router([])
    nxt = await Night(make_config("mayor"), night="2026-01-03", prefer_container=False).run()
    assert nxt["ran"] == []


async def test_interrupted_night_resumes_without_repeating(make_loop, make_config, mock_llama: MockLlama, town: Town) -> None:
    await _a_day_in_town(make_loop, mock_llama, town)
    mock_llama.default = _router(_night_chats(), folds=[schema(FOLD)])
    with pytest.raises(NightInterruptedError):
        await Night(make_config("mayor"), night="2026-01-02", prefer_container=False, interrupt_after="fold:mayor").run()
    assert not (town.knowledge / "tomatoes.md").exists()  # stopped before the Archivist
    result = await Night(make_config("mayor"), night="2026-01-02", prefer_container=False).run()
    assert result["status"] == "ok"
    folds = [c for c in iter_checkpoints(town.resident_dir("mayor")) if c.trigger == "fold 2026-01-02"]
    assert len(folds) == 1
    assert (town.knowledge / "tomatoes.md").exists()
    assert _commits(town)[0].startswith("Upkeep 2026-01-02")


async def test_unreachable_model_skips_the_night(make_config, town: Town) -> None:
    cfg = make_config("mayor", **{"baton.model": {"endpoint": "http://127.0.0.1:9/v1"}})
    result = await Night(cfg, night="2026-01-02").run()
    assert result["status"] == "skipped"
    assert "not reachable" in (town.state / "upkeep-logs" / "2026-01-02.md").read_text()


async def test_night_scrubs_secrets_before_committing(make_loop, make_config, mock_llama: MockLlama, town: Town) -> None:
    await _a_day_in_town(make_loop, mock_llama, town)
    note = town.resident_dir("mayor") / "knowledge" / "keys.md"
    note.write_text("The token is ghp_" + "a" * 36 + " and api_key=sekrit1234\n")
    mock_llama.default = _router(_night_chats(), folds=[schema(FOLD)])
    await Night(make_config("mayor"), night="2026-01-02", prefer_container=False).run()
    committed = subprocess.run(["git", "-C", str(town.root), "show", "HEAD:residents/mayor/knowledge/keys.md"], capture_output=True, text=True).stdout
    assert "ghp_" not in committed and "sekrit1234" not in committed and "[REDACTED]" in committed


def test_tool_test_isolation_reports_failures(tmp_path: Path) -> None:
    good = tmp_path / "good"
    good.mkdir()
    (good / "tool.py").write_text("def run(input):\n    return input[::-1]\n")
    (good / "test_tool.py").write_text("from tool import run\n\ndef test_rev():\n    assert run('ab') == 'ba'\n")
    assert run_tool_test(good, prefer_container=False).passed
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "tool.py").write_text("def run(input):\n    return input\n")
    (bad / "test_tool.py").write_text("from tool import run\n\ndef test_rev():\n    assert run('ab') == 'ba'\n")
    result = run_tool_test(bad, prefer_container=False)
    assert not result.passed and "FAIL test_rev" in result.output
    hang = tmp_path / "hang"
    hang.mkdir()
    (hang / "tool.py").write_text("def run(input):\n    return input\n")
    (hang / "test_tool.py").write_text("import time\n\ndef test_slow():\n    time.sleep(30)\n")
    slow = run_tool_test(hang, prefer_container=False, timeout_s=2)
    assert not slow.passed and "timed out" in slow.output


def test_mismatch_hints() -> None:
    assert mismatch_hints(checkpoints_text="all tests pass", tools=[]) != []
    assert mismatch_hints(checkpoints_text="all tests pass", tools=[{"test": "pass"}]) == []
    assert any("commit" in h for h in mismatch_hints(checkpoints_text="committed the fix", tools=[]))


def test_units_render_for_the_host() -> None:
    from babo.baton.config import BatonConfig
    from babo.baton.units import render_units

    cfg = BatonConfig.model_validate({"box": "Agent", "baboPath": "/home/squid/.local/bin/babo"})
    units = render_units(cfg)
    assert "ExecStart=/usr/bin/distrobox enter Agent -- /home/squid/.local/bin/babo upkeep --watch" in units["babo-upkeep.service"]
    assert sorted(units) == ["babo-gateway.service", "babo-queue.service", "babo-upkeep.service"]
    assert "babo queue" in units["babo-queue.service"] and "gateway --foreground" in units["babo-gateway.service"]


def test_files_outside_a_residents_own_folder_are_pointed_out(town: Town) -> None:
    mine = str(town.resident_dir("archivist") / "notes.md")
    theirs = str(town.resident_dir("mayor") / "SELF.md")
    tools = [{"tool": "exec", "files": ["notes.md", mine]}, {"tool": "exec", "files": [theirs]}]
    assert outside_hints(town, "archivist", tools) == [f"touched files outside its own folder: {theirs}"]
    assert outside_hints(town, "archivist", tools[:1]) == []
