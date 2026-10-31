"""Agent profiles: what each agent may do, and what one session may change.

Settings come in three layers:

1. **Babo-wide** (Babo's config file): model providers and their keys, the web
   search provider and key, MCP server definitions. Each is set once; a profile
   decides which agents may use it.
2. **Agent profile** (``state/profiles/<agent>.json`` in the town, so every
   change has history): capabilities, MCP servers, model and memory timing. The
   Mayor's profile applies to every Mayor session and has no session layer.
3. **Session overrides**: a resident's session starts from its profile. The owner
   may change anything; the Mayor may narrow a session or pick its model, and
   anything wider waits for the owner's review.

An agent without a profile file gets one derived from its office's toolset, so
a town from before profiles behaves exactly as it did.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from babo.baton import gitops
from babo.baton.config import BatonConfig
from babo.baton.town import BatonError, Town

Level = Literal["allow", "ask", "deny"]
LEVELS: tuple[Level, ...] = ("deny", "ask", "allow")  # narrowest first
MAYOR = "mayor"
NEW_RESIDENTS = "_new-residents"  # the template new residents start from; never a resident id


@dataclass(frozen=True)
class Capability:
    key: str
    label: str
    help: str
    tools: tuple[str, ...]  # Babo's own tool names this grants


CAPABILITIES: tuple[Capability, ...] = (
    Capability("files", "Change files", "Write and edit files in its own workspace. Reading is always allowed.",
               ("write_file",)),
    Capability("shell", "Run commands", "Run shell commands in its workspace.", ("exec",)),
    Capability("subagents", "Start crew", "Start temporary helpers for a task; they have no identity or memory of their own.",
               ("subagent",)),
    Capability("web_fetch", "Read web pages", "Fetch a web page by its address.", ("web_fetch",)),
    Capability("web_search", "Search the web", "Uses the search provider and key set once in Settings.", ("web_search",)),
    Capability("image_generation", "Make images", "Uses the image provider set once in Settings.", ("generate_image",)),
)
_BY_KEY = {c.key: c for c in CAPABILITIES}
# Kept short on purpose: a small model chooses better from about a dozen tools.
READ_TOOLS = ("read_file",)
_HARNESS_TOOLS = {t for c in CAPABILITIES for t in c.tools} | set(READ_TOOLS)


class ProfileMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkpoint_every_tool_calls: int | None = Field(default=None, ge=1)
    idle_minutes: int | None = Field(default=None, ge=1)


class Profile(BaseModel):
    """Empty ``model`` means Babo's default model; ``None`` memory values mean Babo-wide."""

    model_config = ConfigDict(extra="forbid")

    capabilities: dict[str, Level] = Field(default_factory=dict)
    mcp: list[str] = Field(default_factory=list)
    model: str = ""
    memory: ProfileMemory = Field(default_factory=ProfileMemory)

    def level(self, capability: str) -> Level:
        return self.capabilities.get(capability, "deny")


def _toolset(cfg: BatonConfig, agent: str, office: str) -> set[str]:
    return set(cfg.toolset_for(agent, office))


ALL_MCP = "*"  # every MCP server set up in Babo: what agents had before profiles


def office_profile(cfg: BatonConfig, agent: str, office: str) -> Profile:
    """The profile an agent has without a profile file: what its office's toolset
    allowed, and every MCP server, as before profiles existed."""
    names = _toolset(cfg, agent, office)
    return Profile(capabilities={c.key: ("allow" if names & set(c.tools) else "deny") for c in CAPABILITIES}, mcp=[ALL_MCP])


def mcp_servers(profile: Profile, known: list[str]) -> list[str]:
    """The MCP servers a profile grants, by name."""
    return sorted(known) if ALL_MCP in profile.mcp else [n for n in profile.mcp if n in known]


def _path(town: Town, agent: str):
    return town.state / "profiles" / f"{agent}.json"


def load(town: Town, cfg: BatonConfig, agent: str, office: str) -> Profile:
    path = _path(town, agent)
    if not path.exists():
        return office_profile(cfg, agent, office if agent != NEW_RESIDENTS else "default")
    try:
        return Profile.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, ValidationError) as exc:
        raise BatonError(f"{path.relative_to(town.root)} is not a valid profile: {exc}") from exc


def save(town: Town, agent: str, profile: Profile, *, message: str, commit: bool = True) -> None:
    path = _path(town, agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile.model_dump(), indent=2) + "\n", encoding="utf-8")
    if commit:
        gitops.commit_paths(town, [path], message)


def harness_tools(profile: Profile, cfg: BatonConfig, agent: str, office: str) -> set[str]:
    """Tool names an agent running in Babo itself may have: its office's town
    tools, reading, and every capability set to allow. Babo's own harness cannot
    stop to ask, so ``ask`` counts as ``deny`` here."""
    town_tools = _toolset(cfg, agent, office) - _HARNESS_TOOLS
    granted = {t for c in CAPABILITIES if profile.level(c.key) == "allow" for t in c.tools}
    return town_tools | set(READ_TOOLS) | granted


def mcp_permitted(profile: Profile, tool_name: str) -> bool:
    if ALL_MCP in profile.mcp:
        return tool_name.startswith("mcp_")
    return any(tool_name.startswith(f"mcp_{server}_") for server in profile.mcp)


# -- changing a profile ----------------------------------------------------

def editable_keys() -> list[str]:
    return [c.key for c in CAPABILITIES] + ["mcp", "model", "memory.checkpoint_every_tool_calls", "memory.idle_minutes"]


def with_changes(profile: Profile, changes: dict[str, object], *, agent: str, mcp_known: list[str],
                 presets_known: list[str] | None = None) -> Profile:
    """Apply ``{key: value}`` changes as typed by a person (CLI or web) and validate them.
    ``presets_known``: the model presets in Settings › Models, when the caller has them."""
    data: dict[str, Any] = profile.model_dump()
    for key, raw in changes.items():
        value = raw.strip() if isinstance(raw, str) else raw
        if key in _BY_KEY:
            if value not in LEVELS:
                raise BatonError(f"{key} must be allow, ask or deny")
            if value == "ask" and agent == MAYOR:
                raise BatonError("The Mayor runs in Babo itself, which can't stop to ask; choose allow or deny.")
            data["capabilities"][key] = value
        elif key == "mcp":
            names = [str(n).strip() for n in cast(list[object], value)] if isinstance(value, list) else str(value).split(",")
            names = [n.strip() for n in names if n.strip()]
            unknown = sorted(set(names) - set(mcp_known))
            if unknown:
                raise BatonError(f"No MCP server named {', '.join(unknown)} is set up in Babo (known: {', '.join(mcp_known) or 'none'}).")
            data["mcp"] = sorted(set(names))
        elif key == "model":
            if agent == MAYOR:
                raise BatonError("The Mayor uses Babo's default model; change it in Settings › Models.")
            name = "" if value in (None, "default") else str(value)
            if name and presets_known is not None and name not in presets_known:
                raise BatonError(f"No model preset named {name!r} in Settings › Models (there are: {', '.join(presets_known) or 'none'}).")
            data["model"] = name
        elif key.startswith("memory.") and key.split(".", 1)[1] in ProfileMemory.model_fields:
            field = key.split(".", 1)[1]
            data["memory"][field] = None if value in ("", None, "default") else value
        else:
            raise BatonError(f"Unknown profile setting {key!r}. Profile settings: {', '.join(editable_keys())}")
    try:
        return Profile.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise BatonError(f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}") from exc


def describe_changes(before: Profile, after: Profile) -> list[str]:
    said: list[str] = []
    for c in CAPABILITIES:
        if before.level(c.key) != after.level(c.key):
            said.append(f"{c.label}: {before.level(c.key)} → {after.level(c.key)}")
    if before.mcp != after.mcp:
        def names(mcp: list[str]) -> str:
            return "all" if ALL_MCP in mcp else ", ".join(mcp) or "none"

        said.append(f"MCP servers: {names(before.mcp)} → {names(after.mcp)}")
    if before.model != after.model:
        said.append(f"Model: {before.model or 'Babo default'} → {after.model or 'Babo default'}")
    for name in ProfileMemory.model_fields:
        old, new = getattr(before.memory, name), getattr(after.memory, name)
        if old != new:
            said.append(f"{name.replace('_', ' ')}: {old or 'Babo-wide'} → {new or 'Babo-wide'}")
    return said


# -- sessions ---------------------------------------------------------------

def session_profile(profile: Profile, overrides: dict[str, object], *, agent: str, mcp_known: list[str],
                    presets_known: list[str] | None = None) -> Profile:
    """A session's effective profile: the agent's profile with the session's changes."""
    if agent == MAYOR and overrides:
        raise BatonError("The Mayor has the same profile in every session; change the Mayor's profile instead.")
    return with_changes(profile, overrides, agent=agent, mcp_known=mcp_known, presets_known=presets_known)


def widening(base: Profile, session: Profile) -> list[str]:
    """What a session grants beyond its agent's profile. The Mayor may change a
    session only when this is empty; anything listed goes to the owner's review."""
    wider = [
        f"{c.label}: {base.level(c.key)} → {session.level(c.key)}"
        for c in CAPABILITIES
        if LEVELS.index(session.level(c.key)) > LEVELS.index(base.level(c.key))
    ]
    extra = [] if ALL_MCP in base.mcp else sorted(set(session.mcp) - set(base.mcp))
    if extra:
        wider.append(f"MCP servers added: {', '.join(extra)}")
    return wider


# -- views shared by the CLI and the web UI ----------------------------------

def describe(town: Town, cfg: BatonConfig, agent: str, office: str, *, mcp_known: list[str]) -> dict[str, Any]:
    profile = load(town, cfg, agent, office)
    return {
        "agent": agent,
        "office": office,
        "main": agent == MAYOR,
        "saved": _path(town, agent).exists(),
        "capabilities": [
            {"key": c.key, "label": c.label, "help": c.help, "level": profile.level(c.key),
             "levels": ["allow", "deny"] if agent == MAYOR else list(reversed(LEVELS))}
            for c in CAPABILITIES
        ],
        "mcp": mcp_servers(profile, mcp_known),
        "mcp_known": mcp_known,
        "model": profile.model,
        "memory": profile.memory.model_dump(),
        "memory_defaults": {"checkpoint_every_tool_calls": cfg.memory.checkpoint_every_tool_calls,
                            "idle_minutes": cfg.memory.idle_minutes},
    }


def update(town: Town, cfg: BatonConfig, agent: str, office: str, changes: dict[str, object], *, mcp_known: list[str],
           presets_known: list[str] | None = None) -> list[str]:
    before = load(town, cfg, agent, office)
    after = with_changes(before, changes, agent=agent, mcp_known=mcp_known, presets_known=presets_known)
    said = describe_changes(before, after)
    if said:
        who = "new residents" if agent == NEW_RESIDENTS else agent
        save(town, agent, after, message=f"Profile of {who}: " + "; ".join(said))
    return said
