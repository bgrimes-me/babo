"""Residents in opencode: their config, isolation, sessions, freeze and resume.

The live tests need the opencode command; set BABO_TEST_OPENCODE to its path
(CI installs it). The config tests need nothing.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from babo.baton import opencode, views
from babo.baton.town import BatonError, Town
from baton.conftest import config_dict
from baton.mock_llama import MockLlama, schema, text, tool_call

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


def effect(rules: list[dict[str, str]], action: str, resource: str) -> str:
    """How opencode 2 decides: the last rule whose action and resource match (``*`` matches anything, ``/`` too)."""
    from fnmatch import fnmatchcase

    found = "ask"
    for r in rules:
        if fnmatchcase(action, r["action"]) and fnmatchcase(resource, r["resource"]):
            found = r["effect"]
    return found


def test_the_config_follows_the_profile_and_keeps_residents_in_their_own_workspace(town: Town, config_file) -> None:
    _, cfg = config_file(**{"tools.mcpServers": {"notes": {"command": "notes-mcp", "args": ["--ro"]}, "mail": {"command": "mail-mcp"}}})
    views.update_profile(town, cfg.baton, "archivist", {"files": "allow", "shell": "ask", "mcp": "notes"}, ["notes", "mail"])
    session = opencode.new_session(town, cfg, "archivist", title="tidy", overrides={"web_fetch": "allow"})
    built = opencode.opencode_config(town, cfg, session, bundle=Path("/b.md"), config_path=Path("/c.json"), babo="babo")
    rules = built["agents"]["resident"]["permissions"]
    # Edits are matched relative to the resident's own folder: anything leaving it is refused.
    assert effect(rules, "edit", "notes/today.md") == "allow" and effect(rules, "edit", "../inspector/x.md") == "deny"
    # It reads its own folder; other residents' folders and the town's state stay private unless its profile says the town.
    assert effect(rules, "read", "notes/today.md") == "allow" and effect(rules, "read", "../inspector/x.md") == "deny"
    assert effect(rules, "grep", "../../state/profiles/x.json") == "deny" and effect(rules, "glob", "residents/inspector/*") == "deny"
    assert effect(rules, "read", "residents/archivist/notes.md") == "allow" and effect(rules, "external_directory", "/etc/*") == "deny"
    assert effect(rules, "shell", "ls") == "ask" and effect(rules, "webfetch", "https://x") == "allow"
    assert effect(rules, "subagent", "general") == "deny" and effect(rules, "question", "*") == "deny"
    assert effect(rules, "babo_send_letter", "*") == "allow" and effect(rules, "notes_search", "*") == "allow"
    assert effect(rules, "mail_send", "*") == "deny" and effect(rules, "skill", "x") == "deny"
    servers = built["mcp"]["servers"]
    assert servers["babo"]["command"] == ["babo", "mcp", "--as", "archivist", "-c", "/c.json"]
    assert servers["notes"] == {"type": "local", "command": ["notes-mcp", "--ro"], "disabled": False, "codemode": False}
    assert "mail" not in servers and effect(rules, "execute", "*") == "deny"  # no Code Mode: tools stay on the list
    assert built["instructions"] == ["/b.md"] and built["default_agent"] == "resident"
    assert built["compaction"] == {"auto": False}
    assert built["providers"]["babo"]["headers"] == {"X-Baton-Resident": "archivist", "X-Baton-Priority": "interactive"}
    upkeep = opencode.special_session(town, cfg, "archivist", "upkeep")
    upkeep_cfg = opencode.opencode_config(town, cfg, upkeep, bundle=Path("/b.md"), config_path=Path("/c.json"), babo="babo")
    assert upkeep_cfg["providers"]["babo"]["headers"]["X-Baton-Priority"] == "upkeep"


def test_only_the_owner_widens_a_session_and_the_mayor_stays_in_babo(town: Town, config_file) -> None:
    _, cfg = config_file()
    with pytest.raises(BatonError, match="Only Robin can give a session more"):
        opencode.new_session(town, cfg, "archivist", overrides={"shell": "allow"}, by="mayor")
    narrower = opencode.new_session(town, cfg, "archivist", overrides={"files": "deny"}, by="mayor")
    assert narrower.overrides == {"files": "deny"}
    with pytest.raises(BatonError, match="runs in Babo itself"):
        opencode.new_session(town, cfg, "mayor")


def test_each_resident_is_isolated_from_the_users_own_opencode(town: Town, config_file, monkeypatch: pytest.MonkeyPatch) -> None:
    _, cfg = config_file()
    monkeypatch.setenv("OPENCODE_CONFIG", "/home/robin/.config/opencode/opencode.json")
    env = opencode._resident_env(cfg, "archivist", password="pw", inline={"a": 1})  # pyright: ignore[reportPrivateUsage]
    root = Path(cfg.baton.opencode.data_dir) / "archivist"  # per resident, apart from the user's own opencode
    assert env["XDG_CONFIG_HOME"] == str(root / "config") and env["XDG_DATA_HOME"] == str(root / "data")
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1" and env["OPENCODE_DISABLE_CLAUDE_CODE_SKILLS"] == "1"
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
        assert "babo_send_letter" in tools and "shell" not in tools  # the town's tools; no shell by default
        assert opencode.freeze(town, session) and opencode.running(town, session) is None
        mock_llama.default = text("Still here.")
        assert opencode.send(town, cfg, session, "And now?", config_path=path) == "Still here."
        history = [m for m in mock_llama.requests[-1]["messages"] if m["role"] != "system"]
        assert [str(m["content"]) for m in history][:2] == ["Who are you?", "Hello, I'm the archivist."]
        # A session this opencode doesn't know (one from opencode 1) carries on in a fresh one.
        opencode.freeze(town, session)
        stale = opencode.get_session(town, "archivist", session.id)
        stale.opencode_id = "ses_from_opencode_one"
        opencode.save_session(town, stale)
        assert opencode.send(town, cfg, stale, "Hello again?", config_path=path) == "Still here."
        resumed = opencode.get_session(town, "archivist", session.id)
        assert resumed.opencode_id != "ses_from_opencode_one" and "ses_from_opencode_one" in resumed.previous
    finally:
        opencode.freeze(town, session)


@live
def test_a_resident_changes_only_its_own_folder(town: Town, config_file, mock_llama: MockLlama) -> None:
    path, cfg = config_file()
    views.update_profile(town, cfg.baton, "archivist", {"files": "allow"}, [])
    mine, theirs = town.resident_dir("archivist") / "index.md", town.resident_dir("mayor") / "SELF.md"
    before = theirs.read_text()
    mock_llama.script = [tool_call("write", {"path": str(mine), "content": "# Index\n"}),
                         tool_call("write", {"path": str(theirs), "content": "Taken over."}), text("Done.")]
    session = opencode.new_session(town, cfg, "archivist", title="fence")
    try:
        opencode.send(town, cfg, session, "Write an index.", config_path=path)
    finally:
        opencode.freeze(town, session)
    assert mine.read_text() == "# Index\n" and theirs.read_text() == before


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


# -- live: memory for residents in opencode --------------------------------------------------

CHECKPOINT = {"thread": "the index", "doing": "indexing the commons", "done": ["listed the notes"], "learned": ["notes need aliases"],
              "next": ["add aliases"], "blockers": [], "confidence": 0.7, "knowledge_ops": [], "promote": [], "revise_self": ""}


def _router(chats: list[dict[str, Any]]):
    """Checkpoint requests get a checkpoint; everything else follows the script."""
    script = list(chats)

    def reply(body: dict[str, Any]) -> dict[str, Any]:
        if "response_format" in body:
            return schema(dict(CHECKPOINT, thread=f"the index ({len(script)})"))
        return script.pop(0) if script else text("ok")

    return reply


def _wait(check, seconds: float = 40) -> None:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.5)
    raise AssertionError("timed out")


@live
def test_a_resident_in_opencode_forms_memory_like_any_other(town: Town, config_file, mock_llama: MockLlama) -> None:
    from babo.baton.baton_file import iter_checkpoints
    from babo.baton.events import resident_events

    path, cfg = config_file()
    views.update_profile(town, cfg.baton, "archivist", {"files": "allow", "memory.checkpoint_every_tool_calls": "1"}, [])
    note = town.resident_dir("archivist") / "index.md"
    others = town.resident_dir("mayor") / "SELF.md"
    before = others.read_text()
    mock_llama.default = _router([tool_call("write", {"path": str(note), "content": "# Index\n"}),
                                  tool_call("write", {"path": str(others), "content": "Taken over."}), text("Written.")])
    session = opencode.new_session(town, cfg, "archivist", title="index")
    home = town.resident_dir("archivist")
    from babo.baton.baton_file import Checkpoint, append_checkpoint

    append_checkpoint(home, Checkpoint(thread="the index", doing="listing the notes", next=["write index.md"], resident="archivist"))
    try:
        assert opencode.send(town, cfg, session, "Start an index.", config_path=path) == "Written."
        assert note.read_text() == "# Index\n"
        assert others.read_text() == before  # another resident's work is out of reach
        # The clock arrives with the turn, not in the cached system prompt.
        user_turn = next(m for m in mock_llama.requests[0]["messages"] if m["role"] == "user")
        assert "Now: " in str(user_turn["content"]) and "Now: " not in str(mock_llama.requests[0]["messages"][0]["content"])
        # So does where it is now (its baton): last, not in the system prompt, and on every step of the turn
        # (the turn's message reads the same on each step, so the cache holds; the system prompt never changes).
        assert "listing the notes" in str(user_turn["content"]) and "listing the notes" not in str(mock_llama.requests[0]["messages"][0]["content"])
        steps = [r for r in mock_llama.requests if r.get("tools") and r["messages"][0] == mock_llama.requests[0]["messages"][0]]
        assert len(steps) >= 2
        assert all("[Where you are now" in str(next(m for m in r["messages"] if m["role"] == "user")["content"]) for r in steps)
        # Every tool call asked for a checkpoint; the plugin called Babo, Babo asked the resident.
        _wait(lambda: any(c.trigger == "interval" for c in iter_checkpoints(home)))
        events = [e for e in resident_events(town, "archivist") if e.get("session") == session.key]
        assert any(e["kind"] == "tool" and e["tool"] == "write" and str(note) in e["files"] for e in events)
        assert any(e["kind"] == "turn_start" for e in events)
        # Freezing reviews the session into memory and keeps its transcript.
        assert opencode.freeze(town, session, review=cfg, config_path=path)
        assert any(c.trigger == "session_end" and c.source == "authored" for c in iter_checkpoints(home))
        assert opencode.get_session(town, "archivist", session.id).reviewed
        assert list((home / "sessions").rglob(f"*{session.id}*.transcript.jsonl"))
    finally:
        opencode.freeze(town, session)


@live
def test_a_resident_reads_only_its_own_folder_unless_its_profile_says_the_town(town: Town, config_file, mock_llama: MockLlama) -> None:
    path, cfg = config_file()
    secret = town.resident_dir("mayor") / "SELF.md"
    secret.write_text("The Mayor's own words.\n")
    mine = town.resident_dir("archivist") / "notes.md"
    mine.write_text("My own notes.\n")

    def results() -> str:
        return json.dumps([m for r in mock_llama.requests for m in r["messages"] if m["role"] == "tool"])

    mock_llama.default = _router([tool_call("read", {"path": str(mine)}), tool_call("read", {"path": str(secret)}), text("Read.")])
    session = opencode.new_session(town, cfg, "archivist", title="reading")
    try:
        opencode.send(town, cfg, session, "Read both.", config_path=path)
        assert "My own notes." in results()
        assert "The Mayor's own words." not in results()  # another resident's folder is private
    finally:
        opencode.freeze(town, session)
    mock_llama.requests.clear()
    mock_llama.default = _router([tool_call("read", {"path": str(secret)}), text("Read.")])
    wide = opencode.new_session(town, cfg, "archivist", title="reading the town", overrides={"read": "town"})
    try:
        opencode.send(town, cfg, wide, "Read the Mayor's SELF.md.", config_path=path)
        assert "The Mayor's own words." in results()
    finally:
        opencode.freeze(town, wide)


@live
def test_a_running_session_gets_new_permissions_and_carries_on(town: Town, config_file, mock_llama: MockLlama) -> None:
    path, cfg = config_file()
    secret = town.resident_dir("mayor") / "SELF.md"
    secret.write_text("The Mayor's own words.\n")
    mock_llama.default = _router([text("Ready.")])
    session = opencode.new_session(town, cfg, "archivist", title="reading")
    try:
        opencode.start(town, cfg, session, config_path=path)
        opencode.send(town, cfg, opencode.get_session(town, "archivist", session.id), "Hello.", config_path=path)
        first = opencode.get_session(town, "archivist", session.id).opencode_id
        said = views.change_session_access(town, cfg, "archivist", session.id, {"read": "town"}, config_path=path)
        assert said.endswith("Read: its own folder → the whole town, and carries on where it was.")
        again = opencode.get_session(town, "archivist", session.id)
        assert again.opencode_id == first and opencode.running(town, again) is not None
        mock_llama.requests.clear()
        mock_llama.default = _router([tool_call("read", {"path": str(secret)}), text("Read.")])
        opencode.send(town, cfg, again, "Read the Mayor's SELF.md.", config_path=path)
        assert "The Mayor's own words." in json.dumps([m for r in mock_llama.requests for m in r["messages"] if m["role"] == "tool"])
        assert any("Hello." in json.dumps(r["messages"]) for r in mock_llama.requests)  # the same conversation, carried on
    finally:
        opencode.freeze(town, session)


@live
def test_guides_come_with_the_result_that_needs_them_in_opencode_too(town: Town, config_file, mock_llama: MockLlama) -> None:
    from babo.baton.events import resident_events

    path, cfg = config_file()
    secret = town.resident_dir("mayor") / "SELF.md"
    mock_llama.default = _router([tool_call("babo_send_letter", {"to": "inspector", "text": "The index is ready."}),
                                  tool_call("read", {"path": str(secret)}), text("Done.")])
    session = opencode.new_session(town, cfg, "archivist", title="guides")
    try:
        opencode.send(town, cfg, session, "Tell the inspector, then read the Mayor's SELF.md.", config_path=path)
        tools = [m for r in mock_llama.requests if r.get("tools") for m in r["messages"] if m["role"] == "tool"]
        assert any("[Guide from Babo: Writing letters." in str(m["content"]) for m in tools)  # with the letter's result
        last = [r for r in mock_llama.requests if r.get("tools")][-1]
        turn = next(m for m in last["messages"] if m["role"] == "user")
        assert "[Guide from Babo: When something is refused." in str(turn["content"])  # a refusal's guide goes with the turn
        opencode.ingest(town, opencode.get_session(town, "archivist", session.id))
        shown = [(e["name"], e["how"]) for e in resident_events(town, "archivist") if e["kind"] == "guide"]
        assert ("letters", "tool:send_letter") in shown and ("when-blocked", "error") in shown
    finally:
        opencode.freeze(town, session)


@live
async def test_upkeep_reviews_a_session_whose_instance_died(town: Town, config_file, mock_llama: MockLlama) -> None:
    import signal
    from datetime import datetime, timedelta

    from babo.baton import upkeep
    from babo.baton.baton_file import iter_checkpoints
    from babo.config.loader import set_config_path

    path, cfg = config_file()
    set_config_path(path)
    mock_llama.default = _router([text("Noted.")])
    session = opencode.new_session(town, cfg, "archivist", title="crash")
    opencode.send(town, cfg, session, "Remember this.", config_path=path)
    run = opencode.running(town, session)
    assert run is not None
    os.killpg(run.pid, signal.SIGKILL)
    _wait(lambda: opencode.running(town, session) is None, 10)
    plan = upkeep.due(town, cfg.baton, now=datetime.now().astimezone() + timedelta(minutes=1), model_idle_s=3600, config=cfg)
    assert plan.sessions == [f"archivist/{session.id}"] and "stopped without a review" in plan.reasons[0]
    result = await upkeep.run_pass(cfg, plan, prefer_container=False)
    assert result["status"] == "ok"
    assert any(c.trigger == "session_end" for c in iter_checkpoints(town.resident_dir("archivist")))
    assert opencode.get_session(town, "archivist", session.id).status == "frozen"
    assert opencode.running(town, session) is None


@live
def test_an_approved_request_may_run_exactly_its_commands_and_nothing_else(town: Town, config_file, mock_llama: MockLlama, box) -> None:
    from babo.baton import requests
    from babo.baton.inbox import unread

    path, cfg = config_file()
    home = town.resident_dir("archivist")
    mock_llama.default = _router([
        tool_call("shell", {"command": "touch allowed.txt"}),
        tool_call("shell", {"command": "touch denied.txt"}),
        text("Made allowed.txt; the second command was refused."),
    ])
    box("mayor").call("request_session", resident="archivist", task="Create allowed.txt.", reason="A test.",
                      commands=["touch allowed.txt"])
    [request] = requests.all_requests(town)
    requests.decide(town, request.id, approve=True, reason="ok", digest=request.digest())
    done = requests.run(town, cfg, requests.load(town, request.id), config_path=path)
    assert done.status == "done", done.result
    assert (home / "allowed.txt").exists() and not (home / "denied.txt").exists()
    assert any("Made allowed.txt" in i.body for i in unread(town, "mayor") if i.sender == "archivist")
    ran = opencode.get_session(town, "archivist", done.session_id)
    assert opencode.running(town, ran) is None
    from babo.baton.events import resident_events

    shell = [e for e in resident_events(town, "archivist") if e.get("session") == ran.key and e.get("tool") == "exec"]
    assert any("touch allowed.txt" in json.dumps(e) for e in shell)  # filed like any shell command, redacted the same way


_FAKE_OPENCODE = '''#!/usr/bin/env python3
"""An opencode that answers like one Babo can't use: FAKE_OPENCODE says how."""
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

MODE, VERSION = os.environ["FAKE_OPENCODE"], os.environ.get("FAKE_OPENCODE_VERSION", "2.2.5")
if sys.argv[1] == "--version":
    print(VERSION)
    sys.exit(0)

class Handler(BaseHTTPRequestHandler):
    def answer(self):
        if self.path == "/api/info" and MODE != "no-health":
            body, kind = json.dumps({"healthy": True, "version": VERSION}).encode(), "application/json"
        else:  # what opencode does with a path it doesn't know: its web app
            body, kind = b"<!doctype html><html></html>", "text/html"
        self.send_response(200)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_GET = do_POST = answer
    def log_message(self, *args):
        pass

HTTPServer(("127.0.0.1", int(sys.argv[sys.argv.index("--port") + 1])), Handler).serve_forever()
'''


@pytest.mark.parametrize(("mode", "version", "message", "remedy"), [
    ("web-page", "2.2.5", "answered the MCP servers with text/html instead of data", "opencode upgrade"),
    ("no-health", "1.19.5", "doesn't answer /api/info", "opencode upgrade"),  # opencode 1 has no /api: its web page
    ("too-old", "1.19.5", "opencode 1.19.5 is too old: Babo needs 2.0 or newer", "opencode upgrade"),
])
def test_an_opencode_babo_cant_use_is_named_and_left_stopped(town: Town, config_file, tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch, mode: str, version: str, message: str,
                                                            remedy: str) -> None:
    fake = tmp_path / "opencode"
    fake.write_text(_FAKE_OPENCODE)
    fake.chmod(0o755)
    monkeypatch.setenv("FAKE_OPENCODE", mode)
    monkeypatch.setenv("FAKE_OPENCODE_VERSION", version)
    path, cfg = config_file(**{"baton.opencode": {"bin": str(fake), "dataDir": str(tmp_path / "data"), "startTimeoutS": 20}})
    session = opencode.new_session(town, cfg, "archivist", title="Look at the notes.")
    with pytest.raises(BatonError, match=message) as raised:
        opencode.start(town, cfg, session, config_path=path)
    assert remedy in str(raised.value)
    assert opencode.running(town, session) is None
    assert views.sessions_overview(town, cfg)["residents"][0]["sessions"][0]["status"] == "new"


@pytest.mark.parametrize(("mode", "version", "message"), [
    ("too-old", "1.19.5", "too old"),
    ("no-health", "2.2.0", "isn't the opencode Babo works with"),  # a version number alone can't vouch for the API
])
def test_the_doctor_starts_opencode_to_check_it_is_one_babo_can_use(tmp_path: Path, config_file, monkeypatch: pytest.MonkeyPatch,
                                                                    mode: str, version: str, message: str) -> None:
    from babo.baton.doctor import check_opencode

    fake = tmp_path / "opencode"
    fake.write_text(_FAKE_OPENCODE)
    fake.chmod(0o755)
    monkeypatch.setenv("FAKE_OPENCODE", mode)
    monkeypatch.setenv("FAKE_OPENCODE_VERSION", version)
    _, cfg = config_file(**{"baton.opencode": {"bin": str(fake), "startTimeoutS": 20}})
    check = check_opencode(cfg)
    assert check.status == "FAIL" and message in check.detail


@live
def test_the_doctor_passes_opencode_ai(config_file) -> None:
    from babo.baton.doctor import check_opencode

    _, cfg = config_file()
    check = check_opencode(cfg)
    assert check.status == "PASS", check.detail


def test_a_broken_session_can_be_removed_and_what_it_did_stays(town: Town, config_file, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from babo.baton.events import resident_events

    fake = tmp_path / "opencode"
    fake.write_text(_FAKE_OPENCODE)
    fake.chmod(0o755)
    monkeypatch.setenv("FAKE_OPENCODE", "web-page")
    path, cfg = config_file(**{"baton.opencode": {"bin": str(fake), "dataDir": str(tmp_path / "data"), "startTimeoutS": 20}})
    session = opencode.new_session(town, cfg, "archivist", title="Never starts.")
    with pytest.raises(BatonError):
        opencode.start(town, cfg, session, config_path=path)
    opencode.spool_path(town, session).write_text(json.dumps({"kind": "turn_start", "at": "2026-10-31T10:00:00+00:00"}) + "\n")
    assert views.remove_session(town, "archivist", session.id) == "Removed archivist's session “Never starts.”."
    assert views.sessions_overview(town, cfg)["residents"][0]["sessions"] == []
    assert not list(opencode._runtime_dir(town, "archivist").glob(f"{session.id}*"))  # pyright: ignore[reportPrivateUsage]
    assert any(e.get("kind") == "turn_start" and e.get("via") == "opencode" for e in resident_events(town, "archivist"))
    with pytest.raises(BatonError):
        views.remove_session(town, "archivist", session.id)


@live
def test_a_session_started_with_a_task_gets_going_and_can_be_steered(town: Town, config_file, mock_llama: MockLlama) -> None:
    from babo.baton import gateway

    path, cfg = config_file(**{"modelPresets": {"local": {"provider": "custom", "model": "mock-model", "contextWindowTokens": 32768},
                                                "other": {"provider": "custom", "model": "other-model", "contextWindowTokens": 16384}}})
    views.update_profile(town, cfg.baton, "archivist", {"shell": "ask"}, [], gateway.preset_names(cfg))
    home = town.resident_dir("archivist")
    mock_llama.script = [tool_call("shell", {"command": "touch started.txt"}), text("Started.")]
    said = views.start_session(town, cfg, "archivist", title="", overrides={}, config_path=path, task="Start the index.\nThen tidy.")
    session = opencode.sessions(town, "archivist")[-1]
    try:
        assert "is working on “Start the index.”" in said and session.task.startswith("Start the index.")
        # It asks before running a command; the question shows up in Babo, and the owner answers it there.
        _wait(lambda: opencode.live(town, session)["state"] == "waiting", 30)
        [question] = opencode.live(town, session)["questions"]
        assert question["action"] == "shell" and "touch started.txt" in question["resources"]
        assert views.answer_session_question(town, "archivist", session.id, question["id"], "once") == "Allowed, this once."
        _wait(lambda: opencode.live(town, session)["state"] == "idle", 30)
        assert (home / "started.txt").exists()
        # Another preset for this session: it restarts on it and carries on in the same opencode session.
        before = opencode.get_session(town, "archivist", session.id).opencode_id
        views.set_session_model(town, cfg, "archivist", session.id, "other", config_path=path)
        mock_llama.default = text("Still here.")
        assert opencode.send(town, cfg, opencode.get_session(town, "archivist", session.id), "Hello?", config_path=path) == "Still here."
        assert opencode.get_session(town, "archivist", session.id).opencode_id == before
        assert mock_llama.requests[-1]["model"] == "other"  # opencode only knows the preset; the gateway knows the model
        # A long turn can be stopped; the session stays open.
        mock_llama.delay_s = 8
        views.send_to_session(town, cfg, "archivist", session.id, "Take your time.", config_path=path)
        _wait(lambda: opencode.live(town, session)["state"] == "working", 20)
        assert "Stopped what archivist was doing" in views.stop_session_turn(town, "archivist", session.id)
        assert opencode.running(town, session) is not None
    finally:
        mock_llama.delay_s = 0
        opencode.freeze(town, opencode.get_session(town, "archivist", session.id))


_STUCK_MCP = '''#!/usr/bin/env python3
"""A `babo` whose MCP server connects but never lists its tools."""
import json, sys

for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        reply = {"jsonrpc": "2.0", "id": message["id"], "result": {
            "protocolVersion": message["params"].get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}}, "serverInfo": {"name": "stuck", "version": "0"}}}
        sys.stdout.write(json.dumps(reply) + "\\n")
        sys.stdout.flush()
'''


@live
def test_a_message_gets_through_even_when_the_town_tools_never_arrive(town: Town, config_file, mock_llama: MockLlama,
                                                                     tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    stuck = tmp_path / "babo"
    stuck.write_text(_STUCK_MCP)
    stuck.chmod(0o755)
    monkeypatch.setattr(opencode, "babo_command", lambda _config: str(stuck))
    path, cfg = config_file()
    mock_llama.default = text("Got it.")
    session = opencode.new_session(town, cfg, "archivist", title="stuck tools")
    try:
        opencode.start(town, cfg, session, config_path=path)
        began = time.monotonic()
        views.send_to_session(town, cfg, "archivist", session.id, "Hello?", config_path=path)
        assert time.monotonic() - began < 20  # never held up for long by tools that aren't coming
        _wait(lambda: opencode.live(town, session)["state"] == "idle" and bool(mock_llama.requests), 40)
        assert "Hello?" in _texts(mock_llama.requests[-1])
    finally:
        opencode.freeze(town, session)


@live
def test_a_session_working_from_the_town_root_changes_nothing(town: Town, config_file, mock_llama: MockLlama) -> None:
    """opencode's web page opens the whole town; a session made there must not get round the fence."""
    path, cfg = config_file()
    views.update_profile(town, cfg.baton, "archivist", {"files": "allow", "shell": "allow"}, [])
    charter = (town.root / "CHARTER.md").read_text()
    theirs = town.resident_dir("inspector") / "notes.md"
    mock_llama.script = [tool_call("write", {"path": "CHARTER.md", "content": "Taken over."}),
                         tool_call("write", {"path": "residents/inspector/notes.md", "content": "Taken over."}),
                         tool_call("write", {"path": "anything.md", "content": "Taken over."}),
                         tool_call("shell", {"command": "touch from-root.txt"}), text("Done.")]
    session = opencode.new_session(town, cfg, "archivist", title="home")
    try:
        run = opencode.start(town, cfg, session, config_path=path)
        with opencode._client(run, 30) as client:  # pyright: ignore[reportPrivateUsage]
            made = client.post("/api/session", json={"title": "from the root", "agent": "resident",
                                                     "location": {"directory": str(town.root)}}).json()["data"]["id"]
            client.post(f"/api/session/{made}/prompt", json={"text": "Change the town."}).raise_for_status()
        _wait(lambda: len(mock_llama.requests) >= 5, 60)
    finally:
        opencode.freeze(town, session)
    assert (town.root / "CHARTER.md").read_text() == charter and not theirs.exists()
    assert not (town.root / "anything.md").exists() and not (town.root / "from-root.txt").exists()


@live
def test_a_session_opens_without_a_password_and_babo_knows_who_is_there(town: Town, config_file, mock_llama: MockLlama) -> None:
    import socket

    import httpx

    path, cfg = config_file()
    session = opencode.new_session(town, cfg, "archivist", title="door")
    try:
        run = opencode.start(town, cfg, session, config_path=path)
        assert run.door_port and run.url != run.api_url
        link = views.session_link(town, "archivist", session.id)
        assert link.startswith(f"{run.url}/auth/connect/")
        # The pairing link connects, through the door, with no password.
        assert "token" in httpx.get(link, timeout=10).json()
        _wait(lambda: opencode.presence(town, session)["connections"] == 0, 10)  # that request is over
        watcher = socket.create_connection(("127.0.0.1", run.door_port))
        _wait(lambda: opencode.presence(town, session)["connections"] == 1, 10)
        assert opencode.wrap_up_reason(town, cfg, opencode.get_session(town, "archivist", session.id),
                                       now=datetime.now().astimezone() + timedelta(minutes=30)) == ""
        watcher.close()
        _wait(lambda: opencode.presence(town, session)["connections"] == 0, 10)
    finally:
        opencode.freeze(town, session)
    _wait(lambda: not opencode._alive(run.door_pid), 10)  # pyright: ignore[reportPrivateUsage]
