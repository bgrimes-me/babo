"""The resident's context builder: one source of truth for who it is.

Upstream's ``ContextBuilder`` assembles SOUL.md, USER.md, MEMORY.md and the
skills list. For a resident those are replaced by the session start bundle
(SELF, ROLE, baton, ledger, inbox, search hits). Everything else about message
construction is inherited unchanged.

The bundle is built once per session and reused, so the system prompt stays
byte-stable within a session (good for llama.cpp's prompt cache). It is rebuilt
when the resident writes a checkpoint (BATON.md changes) or the day changes.
"""

from __future__ import annotations

import platform
import threading
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from jinja2 import Template

from babo.agent.context import ContextBuilder
from babo.agent.skills import SkillsLoader
from babo.agent.tools.context import current_request_session_key
from babo.baton.bundle import build_bundle
from babo.baton.config import BatonConfig
from babo.baton.registry import Registry
from babo.baton.town import Town, find_town_for_workspace, now_iso, today
from babo.session.summary import SessionSummary
from babo.utils.prompt_templates import render_template

_IDENTITY = Template((files("babo.baton") / "templates" / "identity.md").read_text(encoding="utf-8"), trim_blocks=True, lstrip_blocks=True)


@dataclass
class _Cached:
    text: str
    baton_mtime: float
    day: str


class BatonContextBuilder(ContextBuilder):
    """ContextBuilder for a resident workspace."""

    def __init__(self, workspace: Path, timezone: str | None = None, disabled_skills: list[str] | None = None):
        super().__init__(workspace, timezone=timezone, disabled_skills=disabled_skills)
        found = find_town_for_workspace(workspace)
        if found is None:
            raise ValueError(f"{workspace} is not a resident workspace")
        self.town: Town = found[0]
        self.resident_id: str = found[1]
        self.baton_config = BatonConfig()
        # Skills come from the commons only; upstream's built-in skills are not
        # in a resident's context (the ledger lists what the town can do).
        # (A path that does not exist means "no built-in skills" to SkillsLoader.)
        self.skills = SkillsLoader(workspace, builtin_skills_dir=self.town.state / "no-builtin-skills", disabled_skills=set(disabled_skills or ()))
        self.skills.workspace_skills = self.town.skills
        self._cache: dict[str, _Cached] = {}
        self._lock = threading.Lock()

    def configure(self, config: BatonConfig) -> None:
        self.baton_config = config
        self._cache.clear()

    # -- bundle ----------------------------------------------------------
    def _baton_mtime(self) -> float:
        try:
            return (self.town.resident_dir(self.resident_id) / "BATON.md").stat().st_mtime
        except OSError:
            return 0.0

    def _first_session_for(self, session_key: str | None) -> None:
        """Keep the registry's first-session state in step with sessions."""
        if session_key is None:
            return
        registry = Registry(self.town)
        resident = registry.require_active(self.resident_id)
        first_key = str(resident.extra.get("first_session_key") or "")
        if resident.first_session_done:
            return
        if not first_key:
            registry.set_extra(self.resident_id, first_session_key=session_key, first_session_started=now_iso())
        elif first_key != session_key:
            registry.update(self.resident_id, first_session_done=True)

    def bundle_text(self, session_key: str | None = None, *, use_cache: bool = True) -> str:
        key = session_key or "_none"
        mtime = self._baton_mtime()
        with self._lock:
            cached = self._cache.get(key)
            if use_cache and cached and cached.baton_mtime == mtime and cached.day == today():
                return cached.text
            self._first_session_for(session_key)
            cfg = self.baton_config
            bundle = build_bundle(
                self.town,
                self.resident_id,
                max_tokens=cfg.bundle.max_tokens,
                search_hits=cfg.bundle.search_hits,
                letter_preview_lines=cfg.bundle.letter_preview_lines,
                index_path=cfg.index_file,
            )
            text = bundle.text()
            self._cache[key] = _Cached(text, mtime, today())
            return text

    # -- system prompt ---------------------------------------------------
    def _get_identity(self, channel: str | None = None, workspace: Path | None = None) -> str:
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"
        return _IDENTITY.render(
            runtime=runtime,
            platform_policy=render_template("agent/platform_policy.md", system=system),
            channel=channel or "",
            home=str(self.town.resident_dir(self.resident_id)),
            town=str(self.town.root),
        ).strip()

    def build_system_prompt(
        self,
        *,
        channel: str | None = None,
        session_summary: SessionSummary | None = None,
        workspace: Path | None = None,
        include_memory: bool = True,
    ) -> str:
        parts = [self.bundle_text(current_request_session_key()), self._get_identity(channel=channel, workspace=workspace)]
        root = (workspace or self.workspace).expanduser().resolve()
        if root != self.workspace.expanduser().resolve():
            parts.append(f"# Current project\n\nWorking directory: {root}\nUse it as the default root for project files.")
        if session_summary and session_summary["text"] != "(nothing)":
            parts.append(
                "[Archived Context Summary]\n\n"
                f"Earlier in this session (last active {session_summary['last_active']}):\n"
                f"{session_summary['text']}"
            )
        return "\n\n---\n\n".join(parts)
