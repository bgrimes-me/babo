"""Sessions are organized the same way everywhere: rename, pin, tag (the parity principle)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from babo.baton import opencode, views
from babo.baton.town import BatonError, Town


def test_rename_pin_and_tag_a_session(town: Town, make_config) -> None:
    cfg = make_config("mayor")
    first = opencode.new_session(town, cfg, "archivist", title="Garden notes")
    second = opencode.new_session(town, cfg, "archivist", title="Old work")
    assert views.organize_session(town, "archivist", second.id, pinned=True) == "archivist's session is pinned."
    said = views.organize_session(town, "archivist", first.id, title="  The   garden ", tags=["#garden", "notes", "Garden", " "])
    assert said == "archivist's session is renamed “The garden” and tagged garden, notes."
    [top, below] = opencode.overview(town, "archivist")
    assert top["id"] == second.id and top["pinned"]  # pinned first
    assert below["title"] == "The garden" and below["tags"] == ["garden", "notes"]
    assert views.organize_session(town, "archivist", first.id, tags=[]) == "archivist's session is untagged."
    with pytest.raises(BatonError, match="needs a name"):
        views.organize_session(town, "archivist", first.id, title=" ")
    with pytest.raises(BatonError, match="Nothing to change"):
        views.organize_session(town, "archivist", first.id)


def test_the_terminal_has_the_same_actions(town: Town, make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    from babo.baton import cli

    cfg = make_config("mayor")
    session = opencode.new_session(town, cfg, "archivist", title="Garden notes")
    monkeypatch.setattr(cli, "_town", lambda _config: (town, cfg))
    runner = CliRunner()
    assert runner.invoke(cli.session_app, ["tag", "archivist", session.id, "garden", "weekly"]).exit_code == 0
    assert runner.invoke(cli.session_app, ["pin", "archivist", session.id]).exit_code == 0
    assert runner.invoke(cli.session_app, ["rename", "archivist", session.id, "Tidy"]).exit_code == 0
    [listed] = opencode.overview(town, "archivist")
    assert (listed["title"], listed["pinned"], listed["tags"]) == ("Tidy", True, ["garden", "weekly"])
    # --tag finds it (case and '#' don't matter); short words, so a narrow terminal's table doesn't wrap them
    found = runner.invoke(cli.session_app, ["list", "--tag", "#Garden"])
    assert found.exit_code == 0 and "Tidy" in found.output
    assert "Tidy" not in runner.invoke(cli.session_app, ["list", "--tag", "other"]).output


def test_what_a_session_may_do_changes_now_in_the_terminal_too(town: Town, make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    from babo.baton import cli

    cfg = make_config("mayor")
    session = opencode.new_session(town, cfg, "archivist", title="Garden notes")
    said = views.change_session_access(town, cfg, "archivist", session.id, {"shell": "allow", "read": "town"}, config_path=None)
    assert said == "“Garden notes” now: Run commands: deny → allow; Read: its own folder → the whole town."
    assert opencode.get_session(town, "archivist", session.id).overrides == {"shell": "allow", "read": "town"}
    assert views.change_session_access(town, cfg, "archivist", session.id, {"shell": "allow"}, config_path=None) == "“Garden notes” already had those settings."
    monkeypatch.setattr(cli, "_town", lambda _config: (town, cfg))
    runner = CliRunner()
    everything = runner.invoke(cli.session_app, ["set", "archivist", session.id, "--all"], env={"COLUMNS": "300"})
    assert everything.exit_code == 0, everything.output
    overrides = opencode.get_session(town, "archivist", session.id).overrides
    assert overrides == {**{c: "allow" for c in ("files", "shell", "subagents", "web_fetch", "web_search", "image_generation")}, "read": "town"}
    assert runner.invoke(cli.session_app, ["set", "archivist", session.id, "--reset"]).exit_code == 0
    assert opencode.get_session(town, "archivist", session.id).overrides == {}
    bad = runner.invoke(cli.session_app, ["set", "archivist", session.id, "read=everything"])
    assert bad.exit_code == 1 and "read must be own" in bad.output


def test_only_the_owner_widens_a_session(town: Town, make_config) -> None:
    cfg = make_config("mayor")
    session = opencode.new_session(town, cfg, "archivist", title="Garden notes")
    with pytest.raises(BatonError, match="Only Robin can give a session more"):
        opencode.change_session(town, cfg, session, {"read": "town"}, by="mayor", config_path=None)  # type: ignore[arg-type]
