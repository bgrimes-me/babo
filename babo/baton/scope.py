"""What a resident may read and write.

Privacy among residents is enforced here, in one place, for every Baton tool
and for the search index. A resident can read the commons, the town's top-level
documents, its own directory and its own crew transcripts. It cannot read
another resident's directory or the harness's private state.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from babo.baton.town import BatonError, Town

PUBLIC_TOP_LEVEL = ("CHARTER.md", "HOUSE.md", "REGISTRY.md")


@dataclass(frozen=True)
class Scope:
    town: Town
    resident_id: str

    @property
    def home(self) -> Path:
        return self.town.resident_dir(self.resident_id)

    def _inside(self, path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def can_read(self, path: Path) -> bool:
        try:
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError):
            return False
        root = self.town.root
        if not self._inside(resolved, root):
            return False
        rel = resolved.relative_to(root)
        if not rel.parts:
            return False
        if len(rel.parts) == 1:
            return rel.parts[0] in PUBLIC_TOP_LEVEL
        head = rel.parts[0]
        if head == "commons":
            return True
        if head == "residents":
            return rel.parts[1] == self.resident_id
        if head == "crew":
            return rel.parts[1] == self.resident_id
        return False

    def can_write_own(self, path: Path) -> bool:
        resolved = path.resolve(strict=False)
        return self._inside(resolved, self.home.resolve())

    def resolve_ref(self, ref: str) -> Path:
        """Turn what a resident typed into a path, then check it is readable."""
        ref = ref.strip().strip("`\"'")
        if not ref:
            raise BatonError("Give a note path, for example knowledge/topic.md or commons/knowledge/topic.md")
        if ref.startswith("skill:"):
            name = ref.split(":", 1)[1].strip()
            path = self.town.skills / name / "SKILL.md"
        elif ref.startswith(("commons/", "residents/", "crew/")) or ref in PUBLIC_TOP_LEVEL:
            path = self.town.root / ref
        elif Path(ref).is_absolute():
            path = Path(ref)
        else:
            path = self.home / ref
        if not path.suffix and path.with_suffix(".md").exists():
            path = path.with_suffix(".md")
        if not self.can_read(path):
            raise BatonError(f"Blocked: '{ref}' is outside what you can read (the commons, your own files, and letters to you). If you need something from another resident, send_letter to them and ask.")
        if not path.is_file():
            raise BatonError(f"No note at '{ref}'. Use search_memory to find the right path.")
        return path

    def display(self, path: Path) -> str:
        """Show a path the way the resident would type it."""
        resolved = path.resolve(strict=False)
        home = self.home.resolve()
        if self._inside(resolved, home):
            return str(resolved.relative_to(home))
        return str(resolved.relative_to(self.town.root))
