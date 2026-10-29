"""Residents in opencode: their config, isolation, sessions, freeze and resume.

The live tests need the opencode command; set BABO_TEST_OPENCODE to its path
(CI installs it). The config tests need nothing.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from babo.baton import opencode, views
from babo.baton.town import BatonError, Town
from baton.conftest import config_dict
from baton.mock_llama import MockLlama, text, tool_call

OPENCODE = os.environ.get("BABO_TEST_OPENCODE") or shutil.which("opencode")
live = pytest.mark.skipif(not OPENCODE, reason="opencode is not installed (set BABO_TEST_OPENCODE)")


@pytest.fixture(scope="session")
def opencode_data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Shared between tests, so opencode fetches its model adapter once."""
    return tmp_path_factory.mktemp("opencode-data")


@pytest.fixture
def config_file(town: Town, index_path: Path, mock_llama: MockLlama, tmp_path: Path, opencode_data: Path):
    def make(**overrides: Any) -> tuple[Path, Any]:
        from babo.config.schema import Config

        opencode_cfg = {"bin": OPENCODE or "", "dataDir": str(opencode_data), "contextTokens": 32768}
        data = config_dict(town, "mayor", api_base=mock_llama.v1, index_path=index_path, **{"baton.opencode": opencode_cfg, **overrides})
        path = tmp_path / "config.json"
        path.write_text(json.dumps(data))
        return path, Config.model_validate(data)

    return make


def test_the_config_follows_the_profile_and_keeps_residents_in_their_own_workspace(town: Town, config_file) -> None:
    _, cfg = config_file(**{"tools.mcpServers": {"notes": {"command": "notes-mcp", "args": ["--ro"]}}})
    views.update_profile(town, cfg.baton, "archivist", {"files": "allow", "shell": "ask", "mcp": "notes"}, ["notes"])
    session = opencode.new_session(town, cfg, "archivist", title="tidy", overrides={"web_fetch": "allow"})
    built = opencode.opencode_config(town, cfg, session, bundle=Path("/b.md"), config_path=Path("/c.json"), babo="babo")
    perm = built["permission"]
    home = str(town.resident_dir("archivist"))
    assert perm["edit"] == {"*": "deny", f"{home}/**": "allow"}
    assert perm["bash"] == "ask" and perm["webfetch"] == "allow" and perm["task"] == "deny"
    assert perm["external_directory"] == {"*": "deny", f"{town.root}/**": "allow"}
    assert built["mcp"]["babo"]["command"] == ["babo", "mcp", "--as", "archivist", "-c", "/c.json"]
    assert built["mcp"]["notes"] == {"type": "local", "command": ["notes-mcp", "--ro"], "enabled": True}
    assert built["instructions"] == ["/b.md"] and built["default_agent"] == "resident"
    assert built["provider"]["babo"]["options"]["headers"] == {"X-Baton-Resident": "archivist", "X-Baton-Priority": "interactive"}
    upkeep = opencode.special_session(town, cfg, "archivist", "upkeep")
    upkeep_cfg = opencode.opencode_config(town, cfg, upkeep, bundle=Path("/b.md"), config_path=Path("/c.json"), babo="babo")
    assert upkeep_cfg["provider"]["babo"]["options"]["headers"]["X-Baton-Priority"] == "upkeep"


def test_only_squid_widens_a_session_and_the_mayor_stays_in_babo(town: Town, config_file) -> None:
    _, cfg = config_file()
    with pytest.raises(BatonError, match="Only Squid can give a session more"):
        opencode.new_session(town, cfg, "archivist", overrides={"shell": "allow"}, by="mayor")
    narrower = opencode.new_session(town, cfg, "archivist", overrides={"files": "deny"}, by="mayor")
    assert narrower.overrides == {"files": "deny"}
    with pytest.raises(BatonError, match="runs in Babo itself"):
        opencode.new_session(town, cfg, "mayor")


def test_each_resident_is_isolated_from_the_users_own_opencode(town: Town, config_file, monkeypatch: pytest.MonkeyPatch) -> None:
    _, cfg = config_file()
    monkeypatch.setenv("OPENCODE_CONFIG", "/home/squid/.config/opencode/opencode.json")
    env = opencode._resident_env(cfg, "archivist", password="pw", inline={"a": 1})  # pyright: ignore[reportPrivateUsage]
    root = Path(cfg.baton.opencode.data_dir) / "archivist"  # per resident, apart from the user's own opencode
    assert env["XDG_CONFIG_HOME"] == str(root / "config") and env["XDG_DATA_HOME"] == str(root / "data")
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1" and env["OPENCODE_DISABLE_AUTOCOMPACT"] == "1"
    assert env["OPENCODE_DISABLE_CLAUDE_CODE"] == "1" and "OPENCODE_CONFIG" not in env
    assert json.loads(env["OPENCODE_CONFIG_CONTENT"]) == {"a": 1}


async def test_without_opencode_a_visit_runs_in_babo_itself(town: Town, mock_llama: MockLlama, make_loop) -> None:
    mock_llama.script = [tool_call("visit", {"resident": "archivist", "message": "How is the commons?"}), text("Filed."), text("It says: filed.")]
    loop = make_loop("mayor", **{"baton.opencode.bin": "/nonexistent/opencode"})
    reply = await loop.process_direct("Ask the archivist.", session_key="cli:visit")
    await loop.aclose()
    assert str(getattr(reply, "content", "")) == "It says: filed."
    assert opencode.sessions(town, "archivist") == []


# -- live: a real opencode against the mock model ------------------------------------------

def _texts(body: dict[str, Any]) -> str:
    return " ".join(str(m.get("content")) for m in body.get("messages", []))


@live
def test_a_session_starts_with_its_identity_and_resumes_exactly_where_it_was(town: Town, config_file, mock_llama: MockLlama) -> None:
    path, cfg = config_file()
    mock_llama.default = text("Hello, I'm the archivist.")
    session = opencode.new_session(town, cfg, "archivist", title="first")
    try:
        assert opencode.send(town, cfg, session, "Who are you?", config_path=path) == "Hello, I'm the archivist."
        first = mock_llama.requests[-1]
        assert "You are the resident `archivist`" in _texts(first)
        assert {k.lower(): v for k, v in mock_llama.request_headers[-1].items()}["x-baton-resident"] == "archivist"
        tools = {t["function"]["name"] for t in first.get("tools", [])}
        assert "babo_send_letter" in tools and "bash" not in tools  # the town's tools; no shell by default
        assert opencode.freeze(town, session) and opencode.running(town, session) is None
        mock_llama.default = text("Still here.")
        assert opencode.send(town, cfg, session, "And now?", config_path=path) == "Still here."
        history = [m for m in mock_llama.requests[-1]["messages"] if m["role"] != "system"]
        assert [str(m["content"]) for m in history][:2] == ["Who are you?", "Hello, I'm the archivist."]
    finally:
        opencode.freeze(town, session)


@live
def test_a_resident_in_opencode_uses_the_town_tools(town: Town, config_file, mock_llama: MockLlama) -> None:
    from babo.baton.inbox import unread

    path, cfg = config_file()
    mock_llama.script = [tool_call("babo_send_letter", {"to": "mayor", "text": "The commons is tidy."}), text("Done.")]
    session = opencode.new_session(town, cfg, "archivist", title="letters")
    try:
        assert opencode.send(town, cfg, session, "Tell the Mayor the commons is tidy.", config_path=path) == "Done."
    finally:
        opencode.freeze(town, session)
    assert [(i.sender, i.body.strip()) for i in unread(town, "mayor")] == [("archivist", "The commons is tidy.")]


@live
async def test_the_mayor_visits_a_resident_in_opencode(town: Town, config_file, mock_llama: MockLlama, make_loop, opencode_data: Path) -> None:
    from babo.config.loader import set_config_path

    path, _ = config_file()
    set_config_path(path)
    mock_llama.script = [tool_call("visit", {"resident": "archivist", "message": "How is the commons?"}), text("Tidy, it says.")]
    mock_llama.default = text("Tidy.")
    loop = make_loop("mayor", **{"baton.opencode": {"bin": OPENCODE, "dataDir": str(opencode_data)}})
    reply = await loop.process_direct("Ask the archivist about the commons.", session_key="cli:visit")
    await loop.aclose()
    assert "Tidy" in str(getattr(reply, "content", ""))
    visits = [s for s in opencode.sessions(town, "archivist") if s.kind == "visit:mayor"]
    assert len(visits) == 1 and visits[0].status == "frozen"
    assert any("[visit from mayor] How is the commons?" in _texts(r) for r in mock_llama.requests)
