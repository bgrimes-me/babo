"""The commons as a knowledge base: tags and a map for Obsidian and residents, [[links]], and who curates."""

from __future__ import annotations

from pathlib import Path

from babo.baton.knowledge_map import render_map, write_map
from babo.baton.night import curator_of
from babo.baton.notes import Note, split_frontmatter, write_note
from babo.baton.registry import Registry
from babo.baton.search import open_index, reindex
from babo.baton.town import TOWN_GITIGNORE, Town


def test_notes_carry_obsidian_tags_from_their_keywords(town: Town) -> None:
    note = write_note(town.knowledge, title="Raised beds", body="Build them 30 cm high.", author="archivist",
                      keywords=["Garden", "soil care"])
    meta, _ = split_frontmatter(note.path.read_text())
    assert meta["tags"] == ["garden", "soil-care"]
    assert Note.load(note.path).keywords == ["Garden", "soil care"]


def test_the_map_lists_current_notes_by_topic_and_leads_away_from_old_ones(town: Town, index_path: Path) -> None:
    write_note(town.knowledge, title="Raised beds", body="Build them 30 cm high.", author="archivist", keywords=["garden"])
    write_note(town.knowledge, title="Compost", body="Turn it weekly.", author="archivist", keywords=["garden", "soil"])
    write_note(town.knowledge, title="Old compost advice", body="Never turn it.", author="archivist", keywords=["garden"],
               status="superseded")
    write_note(town.knowledge, title="Tea", body="Squid drinks green tea.", author="mayor")
    text = render_map(town)
    garden = text.split("## Garden", 1)[1].split("##", 1)[0]
    assert "[[compost|Compost]] — Turn it weekly." in garden and "[[raised-beds|Raised beds]]" in garden
    assert "Old compost advice" not in garden
    assert "[[old-compost-advice|Old compost advice]] — Never turn it. (superseded)" in text.split("## Replaced or contested", 1)[1]
    assert "## Other notes" in text and "[[tea|Tea]]" in text
    write_map(town)
    reindex(town, index_path)
    assert all(hit.path != "commons/knowledge/INDEX.md" for hit in open_index(index_path).search("compost", owners=("commons",), k=10))


def test_residents_follow_links_to_their_own_notes_first_then_the_commons(town: Town, box) -> None:
    write_note(town.knowledge, title="Compost", body="The town's compost note.", author="archivist", aliases=["humus"])
    write_note(town.resident_dir("mayor") / "knowledge", title="Compost", body="My own compost note.", author="mayor")
    mayor, archivist = box("mayor"), box("archivist")
    assert "My own compost note." in mayor.read_note("[[compost]]")
    assert "The town's compost note." in archivist.read_note("[[Compost|the heap]]")
    assert "The town's compost note." in archivist.read_note("[[humus]]")


def test_the_mayor_curates_while_the_town_has_no_archivist(town: Town, box, make_loop) -> None:
    assert curator_of(town) == "archivist"
    assert "commons_write" not in box("mayor").available()
    Registry(town).update("archivist", status="resting")
    assert curator_of(town) == "mayor"
    mayor = box("mayor")
    assert "commons_write" in mayor.available()
    mayor.call("commons_write", title="Tea", body="Squid drinks green tea.", keywords=["Squid"])
    assert (town.knowledge / "tea.md").exists()
    assert "commons_write" in make_loop("mayor").tools.tool_names


def test_obsidians_own_settings_stay_out_of_the_town_history() -> None:
    assert ".obsidian/" in TOWN_GITIGNORE.splitlines()
