"""The town directory: its layout and the paths inside it.

This module only knows where things are. It never creates a resident; that is
``founding.py``'s job and nobody else's.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import cast

TOWN_MARKER = ".baton-town"
RESIDENT_MARKER = ".baton-resident"
RESIDENT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")

COMMONS_DIRS = (
    "commons/knowledge",
    "commons/skills",
    "commons/tools/pending",
    "commons/tools/active",
    "commons/tools/rejected",
    "commons/proposals/open",
    "commons/proposals/accepted",
    "commons/proposals/declined",
    "commons/promotions",
    "commons/board",
    "commons/projects",
    "residents",
    "crew",
    "state",
)

RESIDENT_DIRS = ("knowledge", "checkpoints", "inbox", "sessions", "projects")

# Derived or private-runtime files that must never be committed to the town repo.
TOWN_GITIGNORE = """\
# Written by Baton. Derived data and nested project repos stay out of the town history.
residents/*/memory/
residents/*/cron/
residents/*/skills/
residents/*/prompts/
residents/*/projects/
residents/*/.babo*/
residents/*/.nanobot*/
commons/projects/
state/locks/
state/tmp/
state/runtime/
# Obsidian's own settings, when the town is opened as a vault.
.obsidian/
.trash/
*.pyc
__pycache__/
"""


class BatonError(RuntimeError):
    """A Baton invariant was violated. The message is written for a human."""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today() -> str:
    return date.today().isoformat()


def slugify(text: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:max_len].rstrip("-")) or "untitled"


_UMASK = os.umask(0)
os.umask(_UMASK)


def atomic_write(path: Path, content: str) -> bool:
    """Write ``content`` to ``path`` atomically (temp file, fsync, rename).

    Returns False (and touches nothing) when the file already has that content.
    """
    try:
        if path.read_text(encoding="utf-8") == content:
            return False
    except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    os.chmod(tmp, 0o666 & ~_UMASK)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return True
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


@dataclass(frozen=True)
class Town:
    root: Path

    @classmethod
    def at(cls, root: str | Path) -> Town:
        return cls(Path(root).expanduser().resolve())

    @classmethod
    def open(cls, root: str | Path) -> Town:
        town = cls.at(root)
        if not (town.root / TOWN_MARKER).is_file():
            raise BatonError(f"No town at {town.root} (missing {TOWN_MARKER}). Run `babo found` first.")
        return town

    # -- the town's name ----------------------------------------------------
    @property
    def name_file(self) -> Path:
        return self.root / "state" / "town.json"

    @property
    def name(self) -> str:
        """The name the town chose, or "" while it has none."""
        try:
            data: object = json.loads(self.name_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        if not isinstance(data, dict):
            return ""
        return str(cast(dict[str, object], data).get("name") or "")

    def set_name(self, name: str, *, by: str) -> str:
        clean = " ".join(name.split())
        if not clean or len(clean) > 60 or not clean.isprintable():
            raise BatonError("A town name is 1 to 60 printable characters on one line.")
        atomic_write(self.name_file, json.dumps({"name": clean, "named": now_iso(), "named_by": by}, indent=2) + "\n")
        return clean

    @property
    def display_name(self) -> str:
        return self.name or "this town"

    # -- top level -------------------------------------------------------
    @property
    def exists(self) -> bool:
        return (self.root / TOWN_MARKER).is_file()

    @property
    def charter(self) -> Path:
        return self.root / "CHARTER.md"

    @property
    def house(self) -> Path:
        return self.root / "HOUSE.md"

    @property
    def registry_md(self) -> Path:
        return self.root / "REGISTRY.md"

    @property
    def registry_json(self) -> Path:
        return self.root / "state" / "registry.json"

    @property
    def commons(self) -> Path:
        return self.root / "commons"

    @property
    def knowledge(self) -> Path:
        return self.commons / "knowledge"

    @property
    def skills(self) -> Path:
        return self.commons / "skills"

    @property
    def tools(self) -> Path:
        return self.commons / "tools"

    @property
    def proposals(self) -> Path:
        return self.commons / "proposals"

    @property
    def promotions(self) -> Path:
        return self.commons / "promotions"

    @property
    def board(self) -> Path:
        return self.commons / "board"

    @property
    def commons_projects(self) -> Path:
        return self.commons / "projects"

    @property
    def ledger(self) -> Path:
        return self.commons / "LEDGER.md"

    @property
    def responses(self) -> Path:
        return self.commons / "RESPONSES.md"

    @property
    def residents_dir(self) -> Path:
        return self.root / "residents"

    @property
    def crew(self) -> Path:
        return self.root / "crew"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def locks(self) -> Path:
        path = self.state / "locks"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- residents -------------------------------------------------------
    def resident_dir(self, resident_id: str) -> Path:
        if not RESIDENT_ID_RE.match(resident_id):
            raise BatonError(f"'{resident_id}' is not a valid resident id")
        return self.residents_dir / resident_id

    def resident_from_workspace(self, workspace: Path) -> str | None:
        """Return the resident id if ``workspace`` is a resident directory of this town."""
        try:
            relative = workspace.expanduser().resolve().relative_to(self.residents_dir)
        except ValueError:
            return None
        parts = relative.parts
        if len(parts) != 1 or not RESIDENT_ID_RE.match(parts[0]):
            return None
        return parts[0]


def find_town_for_workspace(workspace: Path) -> tuple[Town, str] | None:
    """If ``workspace`` looks like ``<town>/residents/<id>``, return (town, id).

    This is a path test only; it does not prove the resident was founded.
    ``registry.require_active`` does that.
    """
    workspace = workspace.expanduser().resolve()
    parent = workspace.parent
    if parent.name != "residents":
        return None
    town = Town(parent.parent)
    if not town.exists:
        return None
    resident_id = town.resident_from_workspace(workspace)
    if resident_id is None:
        return None
    return town, resident_id


def is_resident_workspace(workspace: Path) -> bool:
    """True when ``workspace`` sits where a resident lives (founded or not).

    Used to keep upstream behaviour (template sync, Dream) away from resident
    directories even before validation runs.
    """
    return find_town_for_workspace(workspace) is not None
