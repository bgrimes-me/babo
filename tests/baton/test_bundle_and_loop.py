"""Session start bundles, the resident AgentLoop, MCP, and --as."""

from __future__ import annotations

import pytest

from babo.agent.tools.context import RequestContext, bind_request_context, reset_request_context
from babo.baton.baton_file import Checkpoint, append_checkpoint
from babo.baton.bundle import build_bundle
from babo.baton.context import BatonContextBuilder
from babo.baton.inbox import deliver
from babo.baton.notes import write_note
from babo.baton.registry import Registry
from babo.baton.search import reindex
from babo.baton.tokens import estimate_tokens
from babo.baton.town import BatonError, Town


def _names(bundle) -> list[str]:
    return [name for name, text in bundle.sections if text.strip()]


def test_first_session_bundle_has_charter_house_role_and_no_demand(town: Town) -> None:
    b = build_bundle(town, "mayor")
    names = _names(b)
    assert names[:3] == ["who", "self", "role"]
    assert {"charter", "house", "welcome"} <= set(names)
    assert "Nothing is expected of you yet" in b.text()
    assert "Charter" in b.text() and "House agreements" in b.text()


def test_bundle_names_the_neighbours(town: Town) -> None:
    Registry(town).update("archivist", name="Wren")
    text = build_bundle(town, "mayor").text()
    assert "## Neighbours" in text and "`archivist`, named Wren, office: archivist" in text
    assert "`inspector`, office: inspector" in text and "`mayor`, office" not in text


def test_later_sessions_point_to_charter_in_one_line(town: Town) -> None:
    Registry(town).update("mayor", first_session_done=True)
    text = build_bundle(town, "mayor").text()
    assert "## Who owns what" not in text
    assert 'read_note("CHARTER.md")' in text and 'read_note("HOUSE.md")' in text


def test_bundle_order_and_contents(town: Town, index_path) -> None:
    Registry(town).update("mayor", first_session_done=True)
    home = town.resident_dir("mayor")
    append_checkpoint(home, Checkpoint(thread="garden", doing="planning beds", next=["order soil"], resident="mayor"))
    write_note(home / "knowledge", title="Soil types", body="Loam is best for beds.", author="mayor", keywords=["soil", "beds"])
    reindex(town, index_path)
    deliver(town, "mayor", kind="letter", sender="archivist", text="The ledger is tidy now.")
    b = build_bundle(town, "mayor", index_path=index_path)
    names = _names(b)
    assert names.index("self") < names.index("role") < names.index("baton") < names.index("ledger") < names.index("letters") < names.index("search")
    text = b.text()
    assert "planning beds" in text and "Soil types" in text and "1 unread item" in text


def test_budget_trims_search_first_and_never_self_or_role(town: Town, index_path) -> None:
    Registry(town).update("mayor", first_session_done=True)
    home = town.resident_dir("mayor")
    long_self = "I am patient. " * 300
    (home / "SELF.md").write_text(long_self)
    append_checkpoint(home, Checkpoint(thread="garden", doing="planning beds " * 50, resident="mayor"))
    for i in range(5):
        write_note(home / "knowledge", title=f"Beds {i}", body="planning beds " * 40, author="mayor")
    reindex(town, index_path)
    b = build_bundle(town, "mayor", index_path=index_path, max_tokens=estimate_tokens(long_self) + 700)
    assert b.trimmed[0] == "search"
    assert long_self.strip() in b.text()
    assert (home / "ROLE.md").read_text().strip() in b.text()


def test_bundle_refuses_unknown_resident(town: Town) -> None:
    with pytest.raises(BatonError):
        build_bundle(town, "ghost")


# -- the resident's AgentLoop -------------------------------------------------
def test_resident_loop_uses_baton_context_and_pruned_tools(make_loop, town: Town) -> None:
    loop = make_loop("mayor")
    assert isinstance(loop.context, BatonContextBuilder)
    names = set(loop.tools.tool_names)
    assert {"search_memory", "read_note", "remember", "revise_self", "send_letter", "post_board"} <= names
    assert "commons_write" not in names and "web_search" not in names and "cron" not in names
    assert "subagent" in names  # the Mayor spawns crew
    assert "request_session" in names  # how the Mayor gets things done on the box
    assert len(names) <= 15
    prompt = loop.context.build_system_prompt(channel="cli")
    assert "# Self" in prompt and "Role: Mayor" in prompt
    for upstream_only in ("SOUL.md", "USER.md", "MEMORY.md", "Dream"):
        assert upstream_only not in prompt
    # Upstream's template sync never ran inside the resident directory.
    home = town.resident_dir("mayor")
    assert not (home / "SOUL.md").exists() and not (home / "AGENTS.md").exists()
    assert not (home / ".git").exists()


def test_archivist_and_inspector_toolsets(make_loop) -> None:
    arch = set(make_loop("archivist").tools.tool_names)
    assert {"commons_write", "commons_supersede", "commons_contest"} <= arch and "post_board" not in arch
    insp = set(make_loop("inspector").tools.tool_names)
    assert {"file_flag", "review_item"} <= insp and "commons_write" not in insp


def test_resident_loop_refuses_dream_and_unrestricted_tools(make_loop) -> None:
    with pytest.raises(BatonError, match="Dream"):
        make_loop("mayor", **{"agents.defaults.dream": {"enabled": True}})
    with pytest.raises(BatonError, match="restrictToWorkspace"):
        make_loop("mayor", **{"tools.restrictToWorkspace": False})


def test_loop_for_unfounded_resident_fails_and_creates_nothing(make_config, town: Town) -> None:
    from babo.agent.loop import AgentLoop
    from babo.agent.tools.registry import ToolRegistry

    cfg = make_config("mayor")
    cfg.agents.defaults.workspace = str(town.residents_dir / "ghost")
    with pytest.raises(BatonError, match="not a resident"):
        AgentLoop.from_config(cfg, tool_registry=ToolRegistry())
    assert not (town.residents_dir / "ghost").exists()


def test_bundle_is_stable_within_a_session_and_first_session_advances(make_loop, town: Town) -> None:
    loop = make_loop("mayor")
    ctx = loop.context
    token = bind_request_context(RequestContext(channel="cli", chat_id="direct", session_key="cli:one"))
    try:
        first = ctx.build_system_prompt(channel="cli")
        assert "This is your first session" in first
        assert ctx.build_system_prompt(channel="cli") == first
    finally:
        reset_request_context(token)
    token = bind_request_context(RequestContext(channel="cli", chat_id="direct", session_key="cli:two"))
    try:
        second = ctx.build_system_prompt(channel="cli")
    finally:
        reset_request_context(token)
    assert "This is your first session" not in second
    assert Registry(town).get("mayor").first_session_done


def test_ordinary_workspace_is_untouched(tmp_path) -> None:
    from babo.agent.context import ContextBuilder
    from babo.agent.memory import Consolidator
    from babo.baton.loop_support import loop_classes

    assert loop_classes(tmp_path, ContextBuilder, Consolidator) == (ContextBuilder, Consolidator)


async def test_resident_turn_runs_against_mock_llama(make_loop, mock_llama) -> None:
    from baton.mock_llama import text, tool_call

    mock_llama.push(tool_call("remember", {"text": "Robin likes tomatoes."}), text("Noted."))
    loop = make_loop("mayor")
    reply = await loop.process_direct("Remember that I like tomatoes.", session_key="cli:test")
    assert "Noted" in str(reply)
    first_request = mock_llama.requests[0]
    assert first_request["messages"][0]["role"] == "system"
    assert "# Self" in first_request["messages"][0]["content"]
    tool_names = {t["function"]["name"] for t in first_request.get("tools", [])}
    assert "remember" in tool_names and "cron" not in tool_names


# -- MCP ------------------------------------------------------------------------
async def test_mcp_server_lists_and_calls_office_tools(town: Town, box) -> None:
    from mcp.shared.memory import create_connected_server_and_client_session

    from babo.baton.mcp_server import build_server

    server = build_server(box("mayor"))
    async with create_connected_server_and_client_session(server) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        assert "post_board" in tools and "commons_write" not in tools
        result = await client.call_tool("send_letter", {"to": "archivist", "text": "hello"})
        assert "delivered" in result.content[0].text
        denied = await client.call_tool("commons_write", {"title": "t", "body": "b"})
        assert "not available" in denied.content[0].text


# -- --as ------------------------------------------------------------------------
def test_as_rewrites_to_resident_workspace(town: Town, tmp_path, index_path) -> None:
    import json

    from babo.baton.cli_support import rewrite_as_args

    cfg = tmp_path / "config.json"
    without_opencode = {"town": str(town.root), "indexPath": str(index_path), "opencode": {"bin": str(tmp_path / "none")}}
    cfg.write_text(json.dumps({"baton": without_opencode}))
    assert rewrite_as_args(["agent", "--as", "archivist", "--config", str(cfg)]) == [
        "agent", "--config", str(cfg), "--workspace", str(town.resident_dir("archivist")),
    ]
    Registry(town).update("inspector", name="Ash")
    assert rewrite_as_args(["agent", "--as=Ash", "-c", str(cfg)])[-1] == str(town.resident_dir("inspector"))
    with pytest.raises(SystemExit):
        rewrite_as_args(["agent", "--as", "ghost", "--config", str(cfg)])
    assert not (town.residents_dir / "ghost").exists()
    # With opencode installed, a resident who lives there is opened there.
    (tmp_path / "opencode").write_text("#!/bin/sh\n")
    cfg.write_text(json.dumps({"baton": {**without_opencode, "opencode": {"bin": str(tmp_path / "opencode")}}}))
    assert rewrite_as_args(["agent", "--as", "archivist", "--config", str(cfg)]) == ["session", "open", "archivist", "--config", str(cfg)]
    assert rewrite_as_args(["agent", "--as", "archivist", "-m", "hi", "-c", str(cfg)]) == ["session", "send", "archivist", "hi", "--config", str(cfg)]
