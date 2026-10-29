"""Privacy among residents, search scoping, and the Baton tools."""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import pytest

from nanobot.baton import tools as tools_pkg
from nanobot.baton.inbox import items, unread
from nanobot.baton.notes import Note, write_note
from nanobot.baton.registry import Registry
from nanobot.baton.scope import Scope
from nanobot.baton.search import fts_query, reindex
from nanobot.baton.town import BatonError, Town


def _seed(town: Town) -> None:
    write_note(town.resident_dir("mayor") / "knowledge", title="Garden beds", body="Raised beds need drainage.",
               author="mayor", aliases=["planter boxes"], keywords=["soil"])
    write_note(town.resident_dir("archivist") / "knowledge", title="Secret archive", body="Only for the archivist. drainage",
               author="archivist", aliases=["planter boxes"])
    write_note(town.knowledge, title="Composting", body="Compost needs drainage too.", author="archivist", keywords=["soil"])


# -- privacy -------------------------------------------------------------------
@pytest.mark.parametrize(
    "rel, readable",
    [
        ("commons/knowledge/x.md", True),
        ("CHARTER.md", True),
        ("HOUSE.md", True),
        ("REGISTRY.md", True),
        ("residents/mayor/SELF.md", True),
        ("residents/mayor/inbox/a.md", True),
        ("crew/mayor/t.md", True),
        ("residents/archivist/SELF.md", False),
        ("residents/archivist/knowledge/a.md", False),
        ("crew/archivist/t.md", False),
        ("state/registry.json", False),
        (".git/config", False),
        ("residents/mayor/../archivist/SELF.md", False),
    ],
)
def test_scope_matrix(town: Town, rel: str, readable: bool) -> None:
    assert Scope(town, "mayor").can_read(town.root / rel) is readable


def test_outside_town_is_never_readable(town: Town, tmp_path: Path) -> None:
    assert not Scope(town, "mayor").can_read(tmp_path / "elsewhere.md")
    assert not Scope(town, "mayor").can_read(Path("/etc/passwd"))


def test_read_note_refuses_other_residents_and_symlink_escape(town: Town, box) -> None:
    mayor = box("mayor")
    with pytest.raises(BatonError, match="outside what you can read"):
        mayor.read_note("residents/archivist/SELF.md")
    with pytest.raises(BatonError, match="outside what you can read"):
        mayor.read_note("../archivist/SELF.md")
    link = town.resident_dir("mayor") / "knowledge" / "peek.md"
    os.symlink(town.resident_dir("archivist") / "SELF.md", link)
    with pytest.raises(BatonError, match="outside what you can read"):
        mayor.read_note("knowledge/peek.md")
    assert "Self" in mayor.read_note("SELF.md")
    assert "Charter" in mayor.read_note("CHARTER.md")


def test_search_is_scoped_to_caller_and_commons(town: Town, box, index_path: Path) -> None:
    _seed(town)
    reindex(town, index_path)
    result = box("mayor").search_memory("drainage", k=10)
    assert "Garden beds" in result and "Composting" in result
    assert "Secret archive" not in result
    assert "Garden beds" not in box("archivist").search_memory("drainage", k=10)
    assert "Composting" not in box("mayor").search_memory("drainage", scope="self", k=10)
    assert "Garden beds" not in box("mayor").search_memory("drainage", scope="commons", k=10)


def test_search_alias_hit_and_ranking(town: Town, box, index_path: Path) -> None:
    _seed(town)
    reindex(town, index_path)
    first = box("mayor").search_memory("planter boxes", k=3).splitlines()[0]
    assert "Garden beds" in first
    assert fts_query('"; DROP TABLE docs; --') is not None  # quoted, never raw
    assert box("mayor").search_memory('NEAR(" bad syntax ***') is not None


def test_superseded_notes_rank_below_active(town: Town, box, index_path: Path) -> None:
    _seed(town)
    reindex(town, index_path)
    arch = box("archivist")
    arch.commons_supersede("commons/knowledge/composting.md", "Composting v2", "Compost: drainage and air.", "outdated")
    lines = [line for line in box("mayor").search_memory("compost drainage", scope="commons", k=5).splitlines() if line.startswith("- ")]
    assert "Composting v2" in lines[0]
    assert "[superseded]" in lines[1]


def test_no_tool_writes_another_residents_self(town: Town, box) -> None:
    before = (town.resident_dir("archivist") / "SELF.md").read_text()
    mayor = box("mayor")
    mayor.revise_self("I keep the front desk.", reason="first words")
    assert (town.resident_dir("mayor") / "SELF.md").read_text() == "I keep the front desk.\n"
    assert (town.resident_dir("archivist") / "SELF.md").read_text() == before
    # The commit contains only the mayor's SELF.md.
    show = subprocess.run(["git", "-C", str(town.root), "show", "--name-only", "--format=%s", "HEAD"], capture_output=True, text=True).stdout
    assert "residents/mayor/SELF.md" in show and "archivist" not in show


def test_self_md_only_written_by_revise_self_and_founding() -> None:
    """Static guard: SELF.md is written in exactly two places."""
    pkg = Path(tools_pkg.__file__).resolve().parent.parent
    writers = []
    for path in pkg.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "SELF.md":
                writers.append(path.name)
    assert sorted(set(writers)) == ["bundle.py", "core.py", "founding.py"]
    core = (pkg / "tools" / "core.py").read_text(encoding="utf-8")
    assert core.count('"SELF.md"') == 2  # both inside revise_self


def test_revise_self_records_chosen_name(town: Town, box) -> None:
    box("mayor").revise_self("text", reason="naming", name="Wren")
    assert Registry(town).get("mayor").name == "Wren"
    assert "| mayor | Wren |" in town.registry_md.read_text()
    with pytest.raises(BatonError, match="already called"):
        box("archivist").revise_self("text", reason="copy", name="wren")


# -- letters, flags, remember ----------------------------------------------------
def test_letters_reach_the_right_inbox(town: Town, box) -> None:
    box("mayor").send_letter("archivist", "Please tidy the ledger.")
    assert [i.sender for i in unread(town, "archivist")] == ["mayor"]
    assert unread(town, "inspector") == []
    with pytest.raises(BatonError, match="No resident called"):
        box("mayor").send_letter("ghost", "hi")
    assert not town.resident_dir("ghost").exists()
    path = items(town, "archivist")[0].path
    box("archivist").read_note(str(path.relative_to(town.resident_dir("archivist"))))
    assert unread(town, "archivist") == []


def test_inspector_flag_reaches_the_concerned_resident(town: Town, box) -> None:
    box("inspector").file_flag("mayor", "ran the tests", "no test command in the event log")
    flags = [i for i in unread(town, "mayor") if i.kind == "flag"]
    assert len(flags) == 1 and "no test command" in flags[0].body
    assert unread(town, "archivist") == []
    assert (town.state / "flags.jsonl").exists()


def test_remember_goes_to_own_inbox_not_announced(town: Town, box) -> None:
    box("mayor").remember("The garden faces south.")
    kinds = [i.kind for i in items(town, "mayor")]
    assert kinds == ["remember"]
    assert unread(town, "mayor") == []


# -- office restrictions -------------------------------------------------------
@pytest.mark.parametrize(
    "resident, tool, kwargs",
    [
        ("mayor", "commons_write", {"title": "t", "body": "b"}),
        ("inspector", "commons_write", {"title": "t", "body": "b"}),
        ("archivist", "file_flag", {"resident": "mayor", "claim": "c", "evidence": "e"}),
        ("archivist", "post_board", {"kind": "request", "text": "x"}),
    ],
)
def test_office_tools_are_refused_to_others(box, resident: str, tool: str, kwargs: dict) -> None:
    with pytest.raises(BatonError, match="Only the"):
        box(resident).call(tool, **kwargs)


def test_available_tools_per_office(box) -> None:
    assert "post_board" in box("mayor").available()
    assert "commons_write" in box("archivist").available()
    assert "commons_write" not in box("mayor").available()
    assert "file_flag" in box("inspector").available()


# -- commons flow --------------------------------------------------------------
def test_contested_note_keeps_both_views_and_writes_to_authors(town: Town, box) -> None:
    arch = box("archivist")
    arch.commons_write("Watering", "Water in the morning.", authors=["mayor"])
    result = arch.commons_contest("commons/knowledge/watering.md", "Water in the evening.", "inspector", note="measure evaporation")
    note = Note.load(town.knowledge / "watering.md")
    assert note.status == "contested"
    assert "Water in the morning." in note.body and "Water in the evening." in note.body
    assert set(note.authors) >= {"archivist", "mayor", "inspector"}
    assert "mayor" in result and "inspector" in result
    assert any("contested" in i.subject for i in unread(town, "mayor"))
    assert any("contested" in i.subject for i in unread(town, "inspector"))


def test_proposals_tools_and_board(town: Town, box) -> None:
    mayor = box("mayor")
    mayor.propose_change("Quieter nights", "Night runs are long", "Run folds in parallel")
    assert len(list((town.proposals / "open").glob("*.md"))) == 1
    mayor.propose_tool("word_count", "def run(input: str) -> str:\n    return str(len(input.split()))", "from tool import run\n\ndef test_it():\n    assert run('a b') == '2'", "Counts words")
    assert (town.tools / "pending" / "word_count" / "TOOL.md").exists()
    with pytest.raises(BatonError, match="already pending"):
        mayor.propose_tool("word_count", "def run(input): return ''", "", "dup")
    with pytest.raises(BatonError):
        mayor.propose_tool("Bad Name", "def run(input): return ''", "", "x")
    out = mayor.post_board("founding_request", "We need someone to tend the garden.", office="gardener")
    assert "Squid decides" in out
    assert len(Registry(town).all()) == 3  # posting never founds
    box("inspector").review_item("commons/tools/pending/word_count", "Safe; pure function.")
    assert (town.tools / "pending" / "word_count" / "REVIEW.md").exists()
    prop = next((town.proposals / "open").glob("*.md"))
    box("inspector").review_item(str(prop.relative_to(town.root)), "Low risk.")
    assert prop.with_name(prop.stem + ".review.md").exists()
    with pytest.raises(BatonError):
        box("inspector").review_item("residents/mayor/SELF.md", "x")
