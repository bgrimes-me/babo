"""The resident's context builder: one source of truth for who it is.

Upstream's ``ContextBuilder`` assembles SOUL.md, USER.md, MEMORY.md and the
skills list. For a resident those are replaced by the session start bundle
(SELF, ROLE, baton, ledger, inbox, search hits). Everything else about message
construction is inherited unchanged.

The system prompt holds only what stays the same all session (who it is,
SELF, ROLE, neighbours, the ledger), so it stays byte-stable and the model
server's prompt cache keeps working. Where the resident is now (its baton,
new letters, notes found for its thread) goes at the end of the conversation
instead: ``now_context`` adds it to the turn's message when it has changed
since the conversation last showed it, and it stays in the history after
that, so the start of the prompt never moves.
"""

from __future__ import annotations

import asyncio
import platform
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from jinja2 import Template

from babo.agent.context import ContextBuilder
from babo.agent.skills import SkillsLoader
from babo.agent.tools.context import current_request_session_key
from babo.baton.bundle import Bundle, build_bundle, stable_inputs
from babo.baton.config import BatonConfig
from babo.baton.registry import Registry
from babo.baton.town import Town, find_town_for_workspace, now_iso, today
from babo.runtime_context import RuntimeContextBlock
from babo.session.summary import SessionSummary
from babo.utils.prompt_templates import render_template

if TYPE_CHECKING:
    from babo.agent.loop import AgentLoop
    from babo.agent.tools.context import RequestContext

_IDENTITY = Template((files("babo.baton") / "templates" / "identity.md").read_text(encoding="utf-8"), trim_blocks=True, lstrip_blocks=True)


@dataclass
class _Cached:
    text: str
    stamp: tuple[float, ...]
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
    def _stamp(self) -> tuple[float, ...]:
        """When what the start of the prompt is made of last changed."""
        out: list[float] = []
        for path in stable_inputs(self.town, self.resident_id):
            try:
                out.append(path.stat().st_mtime)
            except OSError:
                out.append(0.0)
        return tuple(out)

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

    def _bundle(self) -> Bundle:
        cfg = self.baton_config
        return build_bundle(
            self.town,
            self.resident_id,
            max_tokens=cfg.bundle.max_tokens,
            search_hits=cfg.bundle.search_hits,
            letter_preview_lines=cfg.bundle.letter_preview_lines,
            index_path=cfg.index_file,
        )

    def bundle_text(self, session_key: str | None = None, *, use_cache: bool = True) -> str:
        """The start of the prompt: the part of the bundle that stays the same all session."""
        key = session_key or "_none"
        stamp = self._stamp()
        with self._lock:
            cached = self._cache.get(key)
            if use_cache and cached and cached.stamp == stamp and cached.day == today():
                return cached.text
            self._first_session_for(session_key)
            text = self._bundle().stable_text()
            self._cache[key] = _Cached(text, stamp, today())
            return text

    def now_text(self) -> str:
        """Where the resident is now: its baton, unread letters, notes for its thread."""
        return self._bundle().now_text()

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


NOW_SOURCE = "baton_now"


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(cast(dict[str, Any], part).get("text") or "") for part in cast(list[object], content) if isinstance(part, dict))
    return ""


def now_provider(loop: AgentLoop) -> Callable[[RequestContext], Awaitable[RuntimeContextBlock | None]]:
    """Where the resident is now, at the end of the turn's message, whenever the conversation doesn't show it yet:
    at the start of a session, after a checkpoint, when a letter comes. It stays in the history after that,
    so the prompt's start (and the model server's cache of it) never changes."""

    async def provide(request: RequestContext) -> RuntimeContextBlock | None:
        context = loop.context
        if not isinstance(context, BatonContextBuilder):
            return None
        text = await asyncio.to_thread(context.now_text)
        if not text:
            return None
        if request.session_key and request.persist_session:
            session = await loop.sessions.state.read(request.session_key)
            if session is not None and any(
                m.get("role") == "user" and text in _text_of(m.get("content")) for m in session.messages[session.last_archived:]
            ):
                return None  # the conversation already shows it
        return RuntimeContextBlock(source=NOW_SOURCE, content=text)

    return provide


SKILLS_SOURCE = "baton_skills"


def skills_provider(loop: AgentLoop, *, town: Town, resident_id: str, office: str) -> Callable[[RequestContext], Awaitable[RuntimeContextBlock | None]]:
    """A skill the turn's message calls for (one of its keywords is in it), with the message: once in a
    conversation, since it stays in the history after that (see ``babo.baton.guides``)."""
    from babo.baton import guides
    from babo.baton.events import append_event

    async def provide(request: RequestContext) -> RuntimeContextBlock | None:
        message = request.original_user_text or ""
        if not message.strip():
            return None
        found = [g for g in await asyncio.to_thread(guides.load, town) if g.keywords and g.for_office(office) and g.matches(message)]
        if not found:
            return None
        if request.session_key and request.persist_session:
            session = await loop.sessions.state.read(request.session_key)
            if session is not None:
                said = "\n".join(_text_of(m.get("content")) for m in session.messages[session.last_archived:] if m.get("role") == "user")
                found = [g for g in found if g.text() not in said]
        for g in found:
            append_event(town, resident_id, request.session_key, guides.EVENT, name=g.name, how="keyword")
        return RuntimeContextBlock(source=SKILLS_SOURCE, content="\n\n".join(g.text() for g in found)) if found else None

    return provide
