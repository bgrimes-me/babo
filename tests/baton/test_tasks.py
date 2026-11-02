"""A task list an agent keeps as it works: shown back as it goes, and to the owner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from babo.baton import tasks
from babo.baton.town import BatonError, Town


def test_the_whole_list_is_given_each_time_and_shown_back(town: Town) -> None:
    said = tasks.update(town, "archivist", "s1", [{"task": "Read the notes", "status": "done"}, {"task": "Write the index", "status": "doing"},
                                                  "Tell the mayor"])
    assert said == "Your task list (1 of 3 done):\n1. [x] Read the notes\n2. [>] Write the index\n3. [ ] Tell the mayor"
    listed = tasks.load(town, "archivist", "s1")
    assert [i.status for i in listed.items] == ["done", "doing", "todo"] and listed.reminder().startswith("[Your task list, from Babo")
    assert tasks.reminder_path(town, "archivist", "s1").read_text() == listed.reminder()
    done = tasks.update(town, "archivist", "s1", [{"task": "Read the notes", "status": "done"}, {"task": "Write the index", "status": "dropped"}])
    assert "Everything on it is done." in done and tasks.load(town, "archivist", "s1").reminder() == ""
    assert tasks.reminder_path(town, "archivist", "s1").read_text() == ""  # nothing open: nothing comes with the next message


def test_bad_lists_are_refused_plainly(town: Town) -> None:
    with pytest.raises(BatonError, match="isn't a status"):
        tasks.update(town, "mayor", "s", [{"task": "x", "status": "maybe"}])
    with pytest.raises(BatonError, match="needs its task"):
        tasks.update(town, "mayor", "s", [{"task": " ", "status": "todo"}])
    with pytest.raises(BatonError, match="keep the list to 20"):
        tasks.update(town, "mayor", "s", [f"step {i}" for i in range(21)])
    assert "Several are marked doing" in tasks.update(town, "mayor", "s", [{"task": "a", "status": "doing"}, {"task": "b", "status": "doing"}])


def test_a_residents_tools_keep_the_list_of_their_session(town: Town, box) -> None:
    archivist = box("archivist")
    archivist.session = "abc123"
    assert "1 of 2 done" in archivist.call("update_tasks", items=[{"task": "a", "status": "done"}, {"task": "b", "status": "todo"}])
    assert tasks.path(town, "archivist", "abc123").exists()
    [listed] = tasks.all_lists(town, "archivist")
    assert tasks.overview(listed)["open"] == 1 and listed.session == "abc123"


async def test_the_mayor_sees_his_list_with_each_new_message_while_something_is_open(make_loop, mock_llama, town: Town) -> None:
    from baton.mock_llama import text, tool_call

    mock_llama.push(tool_call("update_tasks", {"items": [{"task": "Check the board", "status": "doing"}, {"task": "Write to Robin", "status": "todo"}]}),
                    text("Started."), text("Going on."))
    loop = make_loop("mayor")
    await loop.process_direct("Sort out the board, then write to Robin.", session_key="cli:tasks")
    result = [m for m in mock_llama.requests[1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "Your task list (0 of 2 done)" in result
    await loop.process_direct("How is it going?", session_key="cli:tasks")
    last_user = [m for m in mock_llama.requests[-1]["messages"] if m["role"] == "user"][-1]
    assert "[Your task list, from Babo" in json.dumps(last_user) and "Write to Robin" in json.dumps(last_user)
    assert tasks.load(town, "mayor", "cli:tasks").items[0].task == "Check the board"


def test_the_owner_sees_the_open_lists(town: Town, make_config, monkeypatch, tmp_path: Path) -> None:
    from rich.console import Console
    from typer.testing import CliRunner

    from babo.baton import cli

    tasks.update(town, "mayor", "websocket:abc", [{"task": "Check the board", "status": "done"}, {"task": "Write to Robin", "status": "doing"}])
    tasks.update(town, "archivist", "s9", [{"task": "Done already", "status": "done"}])
    monkeypatch.setattr(cli, "_town", lambda _config: (town, make_config("mayor")))
    monkeypatch.setattr(cli, "console", Console(width=200, force_terminal=False))
    shown = CliRunner().invoke(cli.app, ["tasks"]).output
    assert "mayor" in shown and "1 of 2 done" in shown and "[>] Write to Robin" in shown and "Done already" not in shown
    assert "Done already" in CliRunner().invoke(cli.app, ["tasks", "--all"]).output
