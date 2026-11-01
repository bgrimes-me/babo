"""Guides the harness brings in at the right moment: an index always there, a guide with the result that needs it."""

from __future__ import annotations

import json

from babo.baton import guides
from babo.baton.bundle import build_bundle
from babo.baton.founding import found_town
from babo.baton.registry import Registry
from babo.baton.town import Town


def test_babo_ships_guides_and_keeps_them_up_to_date_until_someone_edits_one(town: Town) -> None:
    names = {g.name for g in guides.load(town)}
    assert {"letters", "checkpoints", "commons-notes", "session-requests", "when-blocked", "reviews"} <= names
    letters = guides.folder(town) / "letters.md"
    assert "{{ owner }}" not in letters.read_text() and "Robin" in letters.read_text()
    letters.write_text(letters.read_text().replace("One letter, one matter", "One matter per letter"))
    found_town(town.root, offices=("mayor",))
    assert "One matter per letter" in letters.read_text()  # someone's own words are kept


def test_the_index_is_in_the_prompts_start_and_lists_the_offices_guides(town: Town) -> None:
    Registry(town).update("mayor", first_session_done=True)
    mayor = build_bundle(town, "mayor").stable_text()
    assert "## Guides" in mayor and "`session-requests`: before you ask for a session" in mayor
    assert "`commons-notes`" not in mayor  # the Archivist's, not the Mayor's
    assert "## Guides" not in build_bundle(town, "archivist").stable_text()  # its first session: the charter in full instead
    Registry(town).update("archivist", first_session_done=True)
    assert "`commons-notes`" in build_bundle(town, "archivist").stable_text()
    assert "## Guides" not in build_bundle(town, "mayor").now_text()


def test_a_guide_comes_once_per_session_on_its_tool_or_its_error(town: Town) -> None:
    shown = guides.Shown()
    every = guides.load(town)
    assert [g.name for g, _ in shown.due("s1", every, "babo_send_letter", "Delivered.")] == ["letters"]
    assert shown.due("s1", every, "send_letter", "Delivered.") == []  # once
    assert [g.name for g, _ in shown.due("s2", every, "send_letter", "Delivered.")] == ["letters"]  # another session
    # Error triggers look only at calls that failed: a file that mentions "permission denied" brings nothing in.
    assert shown.due("s1", every, "read", "Notes: what to do when permission denied appears.", failed=False) == []
    [(blocked, how)] = shown.due("s1", every, "read", "Error: Permission denied: read")
    assert blocked.name == "when-blocked" and how == "error"
    assert blocked.text().startswith("[Guide from Babo: When something is refused.")


async def test_the_mayor_gets_the_guide_with_the_result_of_his_first_letter(make_loop, mock_llama, town: Town) -> None:
    from baton.mock_llama import text, tool_call

    mock_llama.push(tool_call("send_letter", {"to": "archivist", "text": "Hello."}), text("Sent."),
                    tool_call("send_letter", {"to": "archivist", "text": "Again."}), text("Sent again."))
    loop = make_loop("mayor")
    await loop.process_direct("Write to the archivist.", session_key="cli:guides")
    first = [m for m in mock_llama.requests[1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "[Guide from Babo: Writing letters." in first
    await loop.process_direct("Write again.", session_key="cli:guides")
    second = [m for m in mock_llama.requests[3]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "[Guide from Babo" not in second
    from babo.baton.events import resident_events

    assert [e["how"] for e in resident_events(town, "mayor") if e["kind"] == "guide"] == ["tool:send_letter"]


def test_a_guide_opened_by_hand_is_logged_and_one_looked_for_often_gets_a_hint(town: Town, box) -> None:
    mayor = box("mayor")
    for _ in range(3):
        assert "Writing letters" in mayor.call("read_note", path="guides/letters.md")
    [row] = [r for r in guides.stats(town) if r["name"] == "letters"]
    assert (row["opened"], row["brought"]) == (3, 0) and "may need a trigger" in row["hint"]


def test_opencode_gets_its_guides_with_their_triggers(town: Town) -> None:
    data = json.loads(guides.plugin_file(town, "archivist"))
    names = {g["name"] for g in data}
    assert "commons-notes" in names and "session-requests" not in names
    blocked = next(g for g in data if g["name"] == "when-blocked")
    assert blocked["errors"] and blocked["text"].startswith("[Guide from Babo")


def test_the_terminal_lists_the_guides(town: Town, make_config, monkeypatch) -> None:
    from typer.testing import CliRunner

    from babo.baton import cli

    monkeypatch.setattr(cli, "_town", lambda _config: (town, make_config("mayor")))
    from rich.console import Console

    monkeypatch.setattr(cli, "console", Console(width=300, force_terminal=False))  # a wide table, whatever the terminal
    result = CliRunner().invoke(cli.app, ["guides"])
    assert result.exit_code == 0 and "letters" in result.output and "first send_letter" in result.output
