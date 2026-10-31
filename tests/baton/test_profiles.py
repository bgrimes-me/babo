"""Agent profiles: what each agent may do, the Mayor's separate profile, the
template for new residents, and what a session may change."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from babo.agent.tools.base import Tool
from babo.baton import growth, profiles, views
from babo.baton.config import BatonConfig
from babo.baton.town import BatonError, Town


class _McpTool(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "An MCP tool."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        return "ok"


def test_without_profile_files_each_office_keeps_its_tools(make_loop) -> None:
    mayor = set(make_loop("mayor").tools.tool_names)
    assert {"read_file", "write_file", "exec", "subagent", "visit"} <= mayor
    assert "web_search" not in mayor
    archivist = set(make_loop("archivist").tools.tool_names)
    assert "read_file" in archivist and "commons_write" in archivist
    assert not {"write_file", "exec", "subagent"} & archivist


def test_a_profile_decides_the_tools_and_memory_timing(town: Town, make_loop) -> None:
    cfg = BatonConfig(town=str(town.root))
    views.update_profile(town, cfg, "mayor", {"shell": "deny", "web_search": "allow", "memory.idle_minutes": "5"}, [])
    loop = make_loop("mayor")
    names = set(loop.tools.tool_names)
    assert "exec" not in names and "web_search" in names
    assert loop.baton.config.memory.idle_minutes == 5  # type: ignore[attr-defined]


def test_mcp_tools_that_connect_later_need_the_profile(town: Town, make_loop) -> None:
    cfg = BatonConfig(town=str(town.root))
    views.update_profile(town, cfg, "archivist", {"mcp": "notes"}, ["notes", "mail"])
    loop = make_loop("archivist")
    loop.tools.register(_McpTool("mcp_notes_search"))
    loop.tools.register(_McpTool("mcp_mail_send"))
    loop.tools.register(_McpTool("cron"))
    names = set(loop.tools.tool_names)
    assert "mcp_notes_search" in names
    assert "mcp_mail_send" not in names and "cron" not in names


def test_the_mayor_has_one_profile_and_says_what_it_cannot_do(town: Town) -> None:
    cfg = BatonConfig(town=str(town.root))
    with pytest.raises(BatonError, match="can't stop to ask"):
        views.update_profile(town, cfg, "mayor", {"shell": "ask"}, [])
    with pytest.raises(BatonError, match="Settings › Models"):
        views.update_profile(town, cfg, "mayor", {"model": "other"}, [])
    with pytest.raises(BatonError, match="No MCP server named mail"):
        views.update_profile(town, cfg, "mayor", {"mcp": "mail"}, [])
    rows = views.profiles_overview(town, cfg, [])
    assert [r["agent"] for r in rows][0] == "mayor" and rows[0]["main"]
    assert rows[-1]["agent"] == profiles.NEW_RESIDENTS


def test_changes_are_committed_with_what_changed(town: Town) -> None:
    from babo.baton import gitops

    cfg = BatonConfig(town=str(town.root))
    message = views.update_profile(town, cfg, "archivist", {"web_fetch": "allow"}, [])
    assert message.startswith("Profile of archivist: Read web pages: deny → allow")
    assert gitops.git(town, "log", "-1", "--format=%s").stdout.strip() == "Profile of archivist: Read web pages: deny → allow"
    assert views.update_profile(town, cfg, "archivist", {"web_fetch": "allow"}, []) == "Nothing changed."


def test_new_residents_start_from_the_template(town: Town, box) -> None:
    cfg = BatonConfig(town=str(town.root))
    views.update_profile(town, cfg, "new-residents", {"shell": "deny", "web_search": "ask"}, [])
    box("mayor").post_board("founding_request", "Someone to look after the garden.", office="gardener")
    request = next(town.board.glob("*founding_request*"))
    new_id = growth.decide_founding(town, request, approve=True, reason="the garden needs it")
    profile = profiles.load(town, cfg, new_id, "gardener")
    assert profile.level("shell") == "deny" and profile.level("web_search") == "ask"


def test_a_session_may_narrow_but_widening_needs_review(town: Town) -> None:
    cfg = BatonConfig(town=str(town.root))
    base = profiles.load(town, cfg, "archivist", "archivist")
    narrower = profiles.session_profile(base, {"files": "deny", "model": "small"}, agent="archivist", mcp_known=["notes"])
    assert profiles.widening(base, narrower) == []
    wider = profiles.session_profile(base, {"shell": "ask", "mcp": "notes"}, agent="archivist", mcp_known=["notes"])
    assert profiles.widening(base, wider) == ["Run commands: deny → ask"]  # a town from before profiles has every MCP server
    narrow_mcp = profiles.with_changes(base, {"mcp": ""}, agent="archivist", mcp_known=["notes"])
    assert profiles.widening(narrow_mcp, wider) == ["Run commands: deny → ask", "MCP servers added: notes"]
    with pytest.raises(BatonError, match="same profile in every session"):
        profiles.session_profile(base, {"files": "deny"}, agent="mayor", mcp_known=[])


def test_without_a_profile_every_mcp_server_stays_as_before_profiles(town: Town, make_loop) -> None:
    loop = make_loop("mayor")
    loop.tools.register(_McpTool("mcp_tavily_search"))
    assert "mcp_tavily_search" in loop.tools.tool_names
    cfg = BatonConfig(town=str(town.root))
    row = views.profiles_overview(town, cfg, ["tavily", "notes"])[0]
    assert row["agent"] == "mayor" and row["mcp"] == ["notes", "tavily"]
    message = views.update_profile(town, cfg, "mayor", {"mcp": "tavily"}, ["tavily", "notes"])
    assert "MCP servers: all → tavily" in message and "systemctl --user restart babo-gateway" in message


def test_a_resident_works_with_a_preset_from_settings(town: Town, make_config) -> None:
    from babo.baton import opencode

    cfg = make_config("mayor", **{"modelPresets": {"local": {"provider": "custom", "model": "mock-model", "contextWindowTokens": 32768},
                                                   "big": {"provider": "custom", "model": "big.gguf", "contextWindowTokens": 65536}}})
    with pytest.raises(BatonError, match="No model preset named 'huge'"):
        views.update_profile(town, cfg.baton, "archivist", {"model": "huge"}, [], ["default", "local", "big"])
    views.update_profile(town, cfg.baton, "archivist", {"model": "big"}, [], ["default", "local", "big"])
    session = opencode.new_session(town, cfg, "archivist", title="t")
    built = opencode.opencode_config(town, cfg, session, bundle=Path("/b.md"), config_path=Path("/c.json"), babo="babo")
    # opencode only ever hears the preset's name; the gateway knows the real model, its provider and key.
    assert built["model"] == "babo/big" and list(built["providers"]["babo"]["models"]) == ["big"]
    assert built["providers"]["babo"]["models"]["big"]["limit"]["context"] == 65536
    # Without a choice, the default preset (Settings › Models).
    other = opencode.new_session(town, cfg, "inspector", title="t")
    assert opencode.opencode_config(town, cfg, other, bundle=Path("/b.md"), config_path=Path("/c.json"), babo="babo")["model"] == "babo/local"
