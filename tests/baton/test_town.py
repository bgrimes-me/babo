"""Town founding, registry, and the rule that only founding creates residents."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest

from babo.baton import founding
from babo.baton.founding import (
    approve_founding_request,
    decline_founding_request,
    found_town,
    founding_requests,
)
from babo.baton.registry import Registry
from babo.baton.town import RESIDENT_MARKER, BatonError, Town, find_town_for_workspace

BATON_PKG = Path(founding.__file__).resolve().parent


def test_found_town_creates_layout_and_three_residents(town: Town) -> None:
    for rel in ("CHARTER.md", "HOUSE.md", "REGISTRY.md", "commons/LEDGER.md", "commons/RESPONSES.md",
                "commons/tools/pending", "commons/proposals/open", "commons/promotions", "commons/board",
                "commons/projects", "crew", "state/registry.json"):
        assert (town.root / rel).exists(), rel
    ids = sorted(r.id for r in Registry(town).all())
    assert ids == ["archivist", "inspector", "mayor"]
    for rid in ids:
        home = town.resident_dir(rid)
        for name in ("SELF.md", "ROLE.md", "BATON.md", RESIDENT_MARKER):
            assert (home / name).is_file()
        for sub in ("knowledge", "checkpoints", "inbox", "sessions", "projects"):
            assert (home / sub).is_dir()
        assert Registry(town).get(rid).name == "unchosen"
    assert "| mayor | unchosen | mayor |" in (town.root / "REGISTRY.md").read_text()


def test_found_town_is_idempotent_and_commits_once(town: Town) -> None:
    before = (town.root / "CHARTER.md").read_text()
    (town.root / "HOUSE.md").write_text("Robin's edits\n")
    found_town(town.root)
    log = subprocess.run(["git", "-C", str(town.root), "log", "--oneline"], capture_output=True, text=True).stdout.splitlines()
    assert len(log) == 1 and "Found the town" in log[0]
    assert (town.root / "CHARTER.md").read_text() == before
    assert (town.root / "HOUSE.md").read_text() == "Robin's edits\n"


def test_town_gitignore_keeps_projects_and_derived_data_out(town: Town) -> None:
    ignore = (town.root / ".gitignore").read_text()
    for pattern in ("residents/*/projects/", "commons/projects/", "residents/*/memory/", "residents/*/.babo*/"):
        assert pattern in ignore


def test_require_active_fails_closed_and_creates_nothing(town: Town) -> None:
    with pytest.raises(BatonError, match="not a resident"):
        Registry(town).require_active("ghost")
    assert not town.resident_dir("ghost").exists()


def test_directory_without_registry_entry_is_not_a_resident(town: Town) -> None:
    fake = town.resident_dir("squatter")
    fake.mkdir()
    (fake / RESIDENT_MARKER).write_text("squatter\n")
    with pytest.raises(BatonError):
        Registry(town).require_active("squatter")


def test_founding_refuses_to_adopt_unfounded_directory(town: Town) -> None:
    (town.board / "2026-01-01-founding_request-gardener.md").write_text(
        "---\nkind: founding_request\noffice: gardener\nstatus: open\n---\n\nTends the projects.\n"
    )
    town.resident_dir("gardener").mkdir()
    (town.resident_dir("gardener") / "SELF.md").write_text("planted")
    resident = approve_founding_request(town, town.board / "2026-01-01-founding_request-gardener.md", approved_by="owner")
    # A unique id is chosen; the planted directory is never adopted.
    assert resident.id == "gardener-2"
    assert (town.resident_dir("gardener") / "SELF.md").read_text() == "planted"
    assert Registry(town).get("gardener") is None


def test_approved_founding_request_creates_resident(town: Town) -> None:
    path = town.board / "2026-01-01-founding_request-gardener.md"
    path.write_text("---\nkind: founding_request\noffice: gardener\nstatus: open\n---\n\nTends the projects.\n")
    assert founding_requests(town) == [path]
    resident = approve_founding_request(town, path, approved_by="owner")
    assert resident.id == "gardener"
    Registry(town).require_active("gardener")
    assert "Tends the projects." in (town.resident_dir("gardener") / "ROLE.md").read_text()
    assert "status: approved" in path.read_text()
    with pytest.raises(BatonError, match="already approved"):
        approve_founding_request(town, path, approved_by="owner")


def test_declined_founding_request_creates_nothing(town: Town) -> None:
    path = town.board / "2026-01-01-founding_request-x.md"
    path.write_text("---\nkind: founding_request\noffice: x-office\nstatus: open\n---\n\nwhy\n")
    decline_founding_request(town, path, declined_by="owner", reason="not now")
    assert len(Registry(town).all()) == 3
    assert not any(p.name.startswith("x-office") for p in town.residents_dir.iterdir())


def test_founding_authority_cannot_be_forged(town: Town) -> None:
    with pytest.raises(BatonError):
        founding._authority("forged", "me", object())
    with pytest.raises(BatonError):
        founding._found_resident(town, "forged", "x", "role", authority="owner")  # type: ignore[arg-type]
    assert not town.resident_dir("forged").exists()


def _calls_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    return names


def test_only_founding_module_can_create_residents() -> None:
    """Static guard: no other Baton module touches the creation primitives."""
    offenders = []
    for path in BATON_PKG.rglob("*.py"):
        if path.name == "founding.py":
            continue
        used = _calls_in(path)
        for name in ("_found_resident", "_add", "_authority", "_TOKEN", "_FoundingAuthority"):
            if name in used:
                offenders.append(f"{path.relative_to(BATON_PKG)} uses {name}")
        if "RESIDENT_MARKER" in used and path.name not in ("town.py", "registry.py"):
            offenders.append(f"{path.relative_to(BATON_PKG)} uses RESIDENT_MARKER")
    assert offenders == []


def test_registry_add_only_called_from_founding() -> None:
    callers = [p for p in BATON_PKG.rglob("*.py") if "._add(" in p.read_text(encoding="utf-8")]
    assert [p.name for p in callers] == ["founding.py"]


def test_workspace_detection(town: Town, tmp_path: Path) -> None:
    assert find_town_for_workspace(town.resident_dir("mayor")) == (town, "mayor")
    assert find_town_for_workspace(tmp_path) is None
    assert find_town_for_workspace(town.root) is None
    assert find_town_for_workspace(town.residents_dir / ".founding-x") is None


def test_registry_json_round_trip(town: Town) -> None:
    Registry(town).update("mayor", name="Wren")
    data = json.loads(town.registry_json.read_text())
    mayor = next(r for r in data["residents"] if r["id"] == "mayor")
    assert mayor["name"] == "Wren" and mayor["founding_reason"]
    assert Registry(town).resolve("wren").id == "mayor"
    with pytest.raises(BatonError):
        Registry(town).update("mayor", office="king")
