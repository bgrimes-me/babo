"""A clock residents can trust, identity in every checkpoint, lanes, plain refusals,
and bringing unedited founding documents up to date."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from babo.baton import founding
from babo.baton.checkpoint import CheckpointService
from babo.baton.clock import ago, clock_lines, stamp
from babo.baton.config import BatonConfig
from babo.baton.founding import found_town, template_text
from babo.baton.registry import Registry
from babo.baton.town import BatonError, Town
from baton.mock_llama import MockLlama, text

OSLO = timezone(timedelta(hours=1))


def test_stamp_and_ago_are_plain() -> None:
    now = datetime(2026, 10, 29, 4, 31, tzinfo=OSLO)
    assert stamp(now) == "Thursday 2026-10-29 04:31 (UTC+01:00)"
    assert ago(now - timedelta(seconds=30), now) == "just now"
    assert ago(now - timedelta(minutes=40), now) == "40 minutes ago"
    assert ago(now - timedelta(hours=5), now) == "5 hours ago"
    assert ago(now - timedelta(days=3, hours=2), now) == "3 days ago"


def test_clock_lines_say_now_and_last_active(town: Town) -> None:
    now = datetime(2026, 10, 29, 4, 31, tzinfo=OSLO)
    assert "not been active before" in clock_lines(town, "mayor", now)[1]
    Registry(town).update("mayor", last_active=(now - timedelta(days=2)).isoformat())
    lines = clock_lines(town, "mayor", now)
    assert lines[0].startswith("Now: Thursday 2026-10-29 04:31")
    assert "2 days ago" in lines[1]


async def test_every_turn_carries_the_clock(make_loop, mock_llama: MockLlama) -> None:
    mock_llama.push(text("Hello."))
    loop = make_loop("mayor")
    await loop.process_direct("Hi there.", session_key="cli:clock")
    sent = mock_llama.requests[0]["messages"]
    assert "Now: " in str(sent[-1]["content"]) and "machine's clock" in str(sent[-1]["content"])
    # The system prompt stays clock-free, so it is byte-stable for the prompt cache.
    assert "Now: " not in str(sent[0]["content"])


def test_checkpoint_request_names_the_resident_and_the_time(town: Town) -> None:
    service = CheckpointService(town, "archivist", BatonConfig())
    request = service.request_text("interval")
    assert "You are the resident `archivist`, office: archivist." in request and "Time now: " in request
    assert "a name, a voice or a job that isn't yours" not in request
    Registry(town).update("archivist", name="Wren")
    fold = service.request_text("fold", fold=True)
    assert "named Wren" in fold and "a name, a voice or a job that isn't yours" in fold


def test_charter_and_offices_draw_lanes() -> None:
    charter = template_text("CHARTER.md")
    assert "when in doubt, route, don't touch" in charter and "## Time" in charter
    for office in ("mayor", "archivist", "inspector"):
        assert "lane" in template_text(f"offices/{office}.md")


def test_refusals_say_what_to_do(box) -> None:
    mayor = box("mayor")
    with pytest.raises(BatonError, match="Blocked: only the archivist .* send_letter"):
        mayor.call("commons_write", title="x", body="y")
    with pytest.raises(BatonError, match="send_letter to them and ask"):
        mayor.read_note("residents/archivist/SELF.md")
    with pytest.raises(BatonError, match="Your tools: "):
        mayor.call("no_such_tool")


def test_refound_updates_only_unedited_founding_documents(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    town, _ = found_town(tmp_path / "town")
    old_charter = "# Charter\n\nAn earlier shipped charter.\n"
    old_role = "# Role: Mayor\n\nAn earlier shipped role.\n"
    edited_role = "# Role: Archivist\n\nMy own words.\n"
    sha = lambda s: hashlib.sha256(s.encode()).hexdigest()  # noqa: E731
    monkeypatch.setitem(founding._EARLIER_SHIPPED, "CHARTER.md", frozenset({sha(old_charter)}))  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setitem(founding._EARLIER_SHIPPED, "offices/mayor.md", frozenset({sha(old_role)}))  # pyright: ignore[reportPrivateUsage]
    (town.root / "CHARTER.md").write_text(old_charter)
    (town.resident_dir("mayor") / "ROLE.md").write_text(old_role)
    (town.resident_dir("archivist") / "ROLE.md").write_text(edited_role)

    _, changed = found_town(town.root)
    assert changed == ["updated CHARTER.md", "updated residents/mayor/ROLE.md"]
    assert (town.root / "CHARTER.md").read_text() == template_text("CHARTER.md")
    assert (town.resident_dir("mayor") / "ROLE.md").read_text() == template_text("offices/mayor.md")
    assert (town.resident_dir("archivist") / "ROLE.md").read_text() == edited_role
    assert found_town(town.root)[1] == []


def test_earlier_shipped_hashes_are_not_the_current_templates() -> None:
    for name, hashes in founding._EARLIER_SHIPPED.items():  # pyright: ignore[reportPrivateUsage]
        assert hashlib.sha256(template_text(name).encode()).hexdigest() not in hashes, name
