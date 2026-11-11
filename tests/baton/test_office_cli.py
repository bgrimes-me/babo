"""The office's terminal half: same views and verbs as the page (parity)."""

from __future__ import annotations

from pathlib import Path

import pytest

from babo.baton import cli
from babo.baton import projects as project_board
from babo.baton.town import Town
from babo.office.model import AgentObserved
from baton.office_fakes import FakeCore


def _configure(town: Town, index_path: Path, monkeypatch: pytest.MonkeyPatch, manager: FakeCore) -> None:
    from rich.console import Console

    from babo.config.schema import Config

    cfg = Config.model_validate({"baton": {"town": str(town.root), "indexPath": str(index_path)}})
    monkeypatch.setattr(cli, "_town", lambda _config: (town, cfg))
    monkeypatch.setattr(cli, "_office_core", lambda _cfg: manager)
    monkeypatch.setattr(cli, "console", Console(width=200, force_terminal=False))


def test_the_board_shows_lead_crew_and_the_managers_state(town: Town, index_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    slug = project_board.new_project(town, "mayor", "Control plane").slug
    manager = FakeCore(agents_by_handle={"lead-1": AgentObserved(handle="lead-1", state="alive"),
                                  "crew-2": AgentObserved(handle="crew-2", state="frozen")})
    _configure(town, index_path, monkeypatch, manager)
    run = CliRunner().invoke
    assert "lead lead-1, crew of 1" in run(cli.app, ["office", "crew", slug, "--lead", "lead-1", "--crew", "crew-2"]).output
    assert "Recorded" in run(cli.app, ["office", "record", "lead-1", "--resident", "archivist", "--project", slug]).output
    assert "Recorded" in run(cli.app, ["office", "record", "crew-2", "--resident", "inspector", "--project", slug]).output
    out = run(cli.app, ["office"]).output
    assert "lead: lead-1" in out and "crew: crew-2" in out
    assert "lead-1 (lead): running" in out


def test_wake_reports_the_managers_verdict_in_its_own_words(town: Town, index_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    slug = project_board.new_project(town, "mayor", "Control plane").slug
    manager = FakeCore(agents_by_handle={"crew-2": AgentObserved(handle="crew-2", state="frozen")})
    _configure(town, index_path, monkeypatch, manager)
    run = CliRunner().invoke
    run(cli.app, ["office", "record", "crew-2", "--resident", "inspector", "--project", slug])
    result = run(cli.app, ["office", "wake", "crew-2"])
    assert result.exit_code == 0
    assert "succeeded" in result.output and "woke crew-2" in result.output
    # the actor is an obtained identity with a source, not a constant default
    handle, action, name, source = manager.audit[-1]
    assert (action, source) == ("wake", "terminal") and name


def test_an_unrecorded_agent_fails_in_the_terminal_too(town: Town, index_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    _configure(town, index_path, monkeypatch, FakeCore())
    result = CliRunner().invoke(cli.app, ["office", "wake", "ghost"])
    assert result.exit_code == 1 and "refused" in result.output and "record it first" in result.output


def test_the_ui_verb_targets_the_webui_listener_not_the_health_server() -> None:
    """In a pod the two are different ports (UI 8843, health 18790); pointing the
    revoke verb at gateway.host/port would POST at a health endpoint."""
    from babo.baton import cli as cli_module
    from babo.config.schema import Config

    cfg = Config.model_validate({
        "gateway": {"host": "10.0.0.5", "port": 18790},
        "channels": {"websocket": {"host": "10.0.0.5", "port": 8843}},
    })
    assert cli_module._webui_base(cfg) == "http://10.0.0.5:8843"
    wildcard = Config.model_validate({"channels": {"websocket": {"host": "0.0.0.0", "port": 8843}}})
    assert cli_module._webui_base(wildcard) == "http://127.0.0.1:8843"


def test_the_ui_verb_prefers_the_listeners_unix_socket(tmp_path) -> None:
    """In a pod the CLI must reach the gateway over its own socket, not a network
    peer: nothing is bound on loopback there, and 10.0.0.5 is not a trusted source."""
    from babo.baton import cli as cli_module
    from babo.config.schema import Config

    socket_path = str(tmp_path / "ui.sock")
    cfg = Config.model_validate({
        "gateway": {"host": "10.0.0.5", "port": 18790},
        "channels": {"websocket": {"host": "10.0.0.5", "port": 8843,
                                   "unix_socket_path": socket_path,
                                   "trusted_sources": ["10.0.0.2/32"]}},
    })
    assert cli_module._webui_base(cfg) == f"unix:{socket_path}"
    # and without a socket, the previous behaviour stands
    plain = Config.model_validate({"channels": {"websocket": {"host": "10.0.0.5", "port": 8843}}})
    assert cli_module._webui_base(plain) == "http://10.0.0.5:8843"
