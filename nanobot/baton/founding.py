"""Founding: the only code path that creates a town or a resident.

There are exactly two ways a resident comes to exist:

1. ``found_town`` founds the three founding residents (Mayor, Archivist,
   Inspector) when the town itself is created at install time.
2. ``approve_founding_request`` founds one resident from a founding request on
   the board, and is only called from Squid's review.

Both go through ``_found_resident``, which demands a ``_FoundingAuthority``
that only this module creates. Nothing else in Baton writes into
``residents/`` without that resident already being in the registry.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from jinja2 import Template

from nanobot.baton import gitops
from nanobot.baton.notes import join_frontmatter, split_frontmatter
from nanobot.baton.registry import Registry, Resident
from nanobot.baton.town import (
    COMMONS_DIRS,
    RESIDENT_DIRS,
    RESIDENT_ID_RE,
    RESIDENT_MARKER,
    TOWN_GITIGNORE,
    TOWN_MARKER,
    BatonError,
    Town,
    atomic_write,
    now_iso,
    slugify,
)

FOUNDING_OFFICES = ("mayor", "archivist", "inspector")
_TEMPLATES = files("nanobot.baton") / "templates"


def template_text(name: str) -> str:
    return (_TEMPLATES / name).read_text(encoding="utf-8")


# sha256 of every earlier shipped version of the founding documents. A town file
# that still matches one of these was never edited, so re-running found_town may
# bring it up to date; anything else is someone's own words and is left alone.
_EARLIER_SHIPPED: dict[str, frozenset[str]] = {
    "CHARTER.md": frozenset({"a14b2f93fb1930c0d611b0cbe733cbc398f83b7ad5bc2f57ab406d169c530ebb"}),
    "HOUSE.md": frozenset({"f5db7f23720080bdc08805b3c0f64b6186e2edefd06367035ab70333490dd81e"}),
    "offices/mayor.md": frozenset({"796e6bb50d56aa9dac20b24712e6fee0f89ed7542111eadb9020ca3c22ade724"}),
    "offices/archivist.md": frozenset({"f2ae5b783190f844e40284333105bce537920f877599b809ba7c1df436236df5"}),
    "offices/inspector.md": frozenset({"da6fd4cb1521656dfb05df33dcfffe2d69bf0a3f88915222f36d90eb2e0af87d"}),
}


def _refresh_if_untouched(path: Path, template: str) -> bool:
    """Replace ``path`` with the current template if it is an unedited earlier version."""
    current = template_text(template)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if text == current or hashlib.sha256(text.encode("utf-8")).hexdigest() not in _EARLIER_SHIPPED.get(template, frozenset()):
        return False
    atomic_write(path, current)
    return True


@dataclass(frozen=True)
class _FoundingAuthority:
    reason: str
    approved_by: str


_TOKEN = object()


def _authority(reason: str, approved_by: str, token: object) -> _FoundingAuthority:
    if token is not _TOKEN:
        raise BatonError("Founding authority can only be issued by nanobot.baton.founding")
    return _FoundingAuthority(reason, approved_by)


def _found_resident(
    town: Town,
    resident_id: str,
    office: str,
    role_text: str,
    authority: _FoundingAuthority,
) -> Resident:
    if not isinstance(authority, _FoundingAuthority):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise BatonError("A resident can only be founded with founding authority")
    if not RESIDENT_ID_RE.match(resident_id):
        raise BatonError(f"'{resident_id}' is not a valid resident id")
    registry = Registry(town)
    if registry.get(resident_id) is not None:
        raise BatonError(f"Resident '{resident_id}' already exists")
    home = town.resident_dir(resident_id)
    marker = home / RESIDENT_MARKER
    resident = Resident(
        id=resident_id,
        office=office,
        founded=now_iso(),
        extra={"founding_reason": authority.reason, "approved_by": authority.approved_by},
    )
    if home.exists():
        # Only a directory this module finished building (marker matches) may
        # be adopted, e.g. after an interrupted founding. Anything else is refused.
        if not (marker.is_file() and marker.read_text(encoding="utf-8").strip() == resident_id):
            raise BatonError(f"{home} already exists but was not founded; refusing to adopt it")
        registry._add(resident)  # pyright: ignore[reportPrivateUsage]
        return resident
    staging = town.residents_dir / f".founding-{resident_id}"
    if staging.exists():
        import shutil

        shutil.rmtree(staging)
    for sub in RESIDENT_DIRS:
        (staging / sub).mkdir(parents=True, exist_ok=True)
        if sub != "projects":
            (staging / sub / ".gitkeep").write_text("", encoding="utf-8")
    atomic_write(staging / "SELF.md", template_text("SELF.md"))
    atomic_write(staging / "ROLE.md", role_text)
    atomic_write(staging / "BATON.md", "# Baton\n\nNo checkpoints yet.\n")
    atomic_write(staging / RESIDENT_MARKER, resident_id + "\n")
    staging.rename(home)
    registry._add(resident)  # pyright: ignore[reportPrivateUsage]
    return resident


def found_town(root: str | Path) -> tuple[Town, list[str]]:
    """Create the town and its three founding residents. Idempotent: an
    existing town is left as it is, and only missing founding residents are
    founded (an interrupted first run can simply be repeated). The one change
    to an existing town: founding documents nobody has edited (CHARTER.md,
    HOUSE.md, the founding residents' ROLE.md) are brought up to date."""
    town = Town.at(root)
    created: list[str] = []
    updated: list[str] = []
    town.root.mkdir(parents=True, exist_ok=True)
    for rel in COMMONS_DIRS:
        (town.root / rel).mkdir(parents=True, exist_ok=True)
    for rel, name in (("CHARTER.md", "CHARTER.md"), ("HOUSE.md", "HOUSE.md")):
        if not (town.root / rel).exists():
            atomic_write(town.root / rel, template_text(name))
            created.append(rel)
        elif _refresh_if_untouched(town.root / rel, name):
            updated.append(rel)
    if not town.responses.exists():
        atomic_write(town.responses, "# Responses to the charter\n\nResidents may answer the charter here.\n")
    gitignore = town.root / ".gitignore"
    if not gitignore.exists():
        atomic_write(gitignore, TOWN_GITIGNORE)
    for keep_dir in ("commons/knowledge", "commons/skills", "commons/promotions", "commons/board", "crew"):
        keep = town.root / keep_dir / ".gitkeep"
        if not keep.exists():
            keep.write_text("", encoding="utf-8")
    if not (town.root / TOWN_MARKER).exists():
        atomic_write(town.root / TOWN_MARKER, "baton town v1\n")
    registry = Registry(town)
    for office in FOUNDING_OFFICES:
        if registry.get(office) is None:
            authority = _authority("founding resident of the town", "squid (install)", _TOKEN)
            _found_resident(town, office, office, template_text(f"offices/{office}.md"), authority)
            created.append(f"residents/{office}")
        elif _refresh_if_untouched(town.resident_dir(office) / "ROLE.md", f"offices/{office}.md"):
            updated.append(f"residents/{office}/ROLE.md")
    registry.render()
    from nanobot.baton.ledger import write_ledger

    write_ledger(town)
    gitops.ensure_repo(town)
    if created:
        gitops.commit_all(town, "Found the town" if "residents/mayor" in created else "Finish founding the town")
    elif updated:
        gitops.commit_all(town, "Update unedited founding documents: " + ", ".join(updated))
    return town, created + [f"updated {rel}" for rel in updated]


# -- founding requests ----------------------------------------------------
def founding_requests(town: Town, status: str = "open") -> list[Path]:
    out: list[Path] = []
    for path in sorted(town.board.glob("*.md")):
        meta, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        if meta.get("kind") == "founding_request" and meta.get("status", "open") == status:
            out.append(path)
    return out


def _unique_id(registry: Registry, town: Town, base: str) -> str:
    base = slugify(base, max_len=28) or "resident"
    if not base[0].isalpha():
        base = "r-" + base
    candidate, n = base, 2
    while registry.get(candidate) is not None or town.resident_dir(candidate).exists():
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def approve_founding_request(town: Town, request: Path, *, approved_by: str, note: str = "") -> Resident:
    """Found the resident a founding request asks for. Called only from review."""
    request = request.resolve()
    if request.parent != town.board.resolve():
        raise BatonError("Founding requests live on the board")
    meta, body = split_frontmatter(request.read_text(encoding="utf-8"))
    if meta.get("kind") != "founding_request":
        raise BatonError(f"{request.name} is not a founding request")
    if meta.get("status", "open") != "open":
        raise BatonError(f"{request.name} is already {meta.get('status')}")
    office = str(meta.get("office") or "").strip() or "resident"
    registry = Registry(town)
    resident_id = _unique_id(registry, town, office)
    role = Template(template_text("offices/default.md")).render(
        office_title=office.replace("-", " ").title(), description=body.strip(),
    )
    authority = _authority(f"founding request {request.name}", approved_by, _TOKEN)
    resident = _found_resident(town, resident_id, slugify(office), role, authority)
    meta.update({"status": "approved", "resident": resident_id, "decided": now_iso(), "decided_by": approved_by})
    if note:
        meta["decision_note"] = note
    atomic_write(request, join_frontmatter(meta, body))
    return resident


def decline_founding_request(town: Town, request: Path, *, declined_by: str, reason: str) -> None:
    meta, body = split_frontmatter(request.read_text(encoding="utf-8"))
    if meta.get("kind") != "founding_request" or meta.get("status", "open") != "open":
        raise BatonError(f"{request.name} is not an open founding request")
    meta.update({"status": "declined", "decided": now_iso(), "decided_by": declined_by, "decision_note": reason})
    atomic_write(request, join_frontmatter(meta, body))
