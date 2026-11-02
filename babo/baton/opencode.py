"""Residents in opencode.

The Mayor runs in Babo itself. Every other resident runs as an opencode
instance (``opencode serve``) started by Babo for one of its sessions:

- **Identity on load.** What stays the same all session (who it is, its SELF
  and ROLE, its neighbours, the ledger) is written to a file opencode reads as
  instructions, under a short Babo prompt that replaces opencode's own: the
  start of every prompt, which the model server keeps cached. Where it is now
  (its baton, unread letters, notes for its thread) is kept beside it, and
  Babo's plugin adds it at the end of each turn, where it carries most weight.
- **The town's tools** come from ``babo mcp --as <resident>`` (letters,
  memory, proposals, the commons tools of its office), plus web search with
  Babo's key when its profile allows.
- **Permissions** come from the resident's profile and the session's
  overrides: it may change files only in its own workspace, and run commands,
  read web pages or start crew only as allowed. Babo's config is inline and
  project config is switched off, so a resident can't widen its own access.
- **Isolation from the person's own opencode.** Each resident has its own
  opencode config, data and cache folders (``baton.opencode.data_dir``), so
  the user's opencode settings, skills and keys never leak in, and its
  sessions persist there between runs.
- **Freeze and resume.** Freezing stops the instance; resuming starts it
  again on the same opencode session, so the resident is exactly where it was.
  opencode's own lossy auto-compaction is off: a session that fills up ends
  with a review and the next one starts from the resident's baton.

Sessions are recorded in the town under ``state/sessions/<resident>/`` (with
their overrides, so changes have history); what is running right now is in
``state/runtime/opencode/`` (ignored by git).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
from filelock import FileLock
from loguru import logger

from babo.baton import profiles
from babo.baton.registry import Registry
from babo.baton.town import OWNER, BatonError, Town, atomic_write, is_owner, now_iso

if TYPE_CHECKING:
    from babo.config.schema import Config, MCPServerConfig

PROVIDER = "babo"
AGENT = "resident"
# Switched off for residents: anything that would let opencode's own setup,
# the user's Claude Code files, or the resident's workspace change what Babo set.
_ENV_OFF = (
    "OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
    "OPENCODE_DISABLE_AUTOUPDATE", "OPENCODE_DISABLE_MODELS_FETCH", "OPENCODE_DISABLE_FREE_MODELS",
)


# The opencode Babo is built and tested against: opencode 2, whose server API
# (under /api) replaced opencode 1's. opencode answers any path it doesn't know
# with its web app, so an opencode 1 looks healthy until Babo asks it for anything.
MIN_VERSION = (2, 0)
UPGRADE = "Update it with: opencode upgrade"
NOT_OURS = ("isn't the opencode Babo works with (opencode.ai, 2.0 or newer): it may be opencode 1, or another program by "
            "that name may come first on your PATH. Update it with: opencode upgrade (or install opencode.ai's with: "
            "curl -fsSL https://opencode.ai/install | bash, and set baton.opencode.bin in ~/.babo/config.json to it)")


def parse_version(text: str) -> tuple[int, ...] | None:
    import re

    found = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    return tuple(int(n) for n in found.groups() if n is not None) if found else None


def version_problem(version: str) -> str | None:
    """Why this opencode version won't do, or None if it will."""
    parsed = parse_version(version)
    if parsed is None:
        return f"Babo couldn't tell which opencode this is ({version or 'no version'}); it needs {'.'.join(map(str, MIN_VERSION))} or newer. {UPGRADE}"
    if parsed[:2] < MIN_VERSION:
        return f"opencode {version} is too old: Babo needs {'.'.join(map(str, MIN_VERSION))} or newer. {UPGRADE}"
    return None


def _json(resp: httpx.Response, what: str) -> Any:
    """opencode's answer as data; a clear error when it isn't (an unknown path gets its web page)."""
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        raise BatonError(f"opencode answered {what} with {resp.headers.get('content-type') or 'something'} instead of data: "
                         f"this opencode doesn't have the API Babo uses. Babo needs opencode "
                         f"{'.'.join(map(str, MIN_VERSION))} or newer; `babo doctor` shows which one it found. {UPGRADE}") from None


def probe(config: Config) -> str | None:
    """Start opencode the way Babo does, check that it answers like the opencode Babo works with, and stop it.
    Returns what is wrong, or None. For the doctor: a version number alone can belong to another program."""
    import tempfile

    binary = opencode_bin(config)
    if binary is None:
        return "not installed"
    with tempfile.TemporaryDirectory(prefix="babo-opencode-probe-") as scratch:
        root = Path(scratch)
        env = dict(os.environ)
        for name in ("config", "data", "cache", "state"):
            (root / name).mkdir()
            env[f"XDG_{name.upper()}_HOME"] = str(root / name)
        env.update({name: "1" for name in _ENV_OFF})
        password = secrets.token_urlsafe(18)
        env["OPENCODE_PASSWORD"] = password
        port = _free_port()
        log_path = root / "probe.log"
        with log_path.open("ab") as log:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                [binary, "serve", "--port", str(port), "--hostname", "127.0.0.1"], cwd=root, env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
        run = Running(pid=proc.pid, port=port, password=password, started=now_iso())
        try:
            _wait_healthy(run, proc, config.baton.opencode.start_timeout_s, log_path=log_path)
        except BatonError as exc:
            return str(exc)
        finally:
            _stop(run)
    return None


def _data(resp: httpx.Response, what: str) -> Any:
    """opencode 2 wraps each answer in ``{"data": ...}``."""
    body = _json(resp, what)
    if not isinstance(body, dict) or "data" not in body:
        raise BatonError(f"opencode answered {what} in a shape Babo doesn't know. Babo needs opencode "
                         f"{'.'.join(map(str, MIN_VERSION))} or newer; `babo doctor` shows which one it found.")
    return cast(dict[str, Any], body)["data"]


def _has_session(run: Running, opencode_id: str) -> bool:
    with _client(run, 30) as client:
        resp = client.get(f"/api/session/{opencode_id}")
    if resp.status_code in (400, 404):
        return False
    _data(resp, "a session")
    return True


def _new_opencode_session(client: httpx.Client, title: str) -> str:
    return str(_data(client.post("/api/session", json={"title": title, "agent": AGENT}), "a new session")["id"])


def babo_command(config: Config) -> str:
    """The full path of the babo command, for what opencode starts (the town's tools, checkpoints).
    Babo's services may not have it on their PATH, so the configured path comes first, then this very process."""
    import sys

    configured = config.baton.babo_path
    if configured and Path(configured).expanduser().is_file():
        return str(Path(configured).expanduser())
    found = shutil.which("babo")
    if found:
        return found
    me = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if me is not None and me.name == "babo" and me.is_file():
        return str(me)
    sibling = Path(sys.executable).parent / "babo"
    return str(sibling) if sibling.is_file() else "babo"


def runs_in_opencode(office: str) -> bool:
    return office != profiles.MAYOR


def opencode_bin(config: Config) -> str | None:
    configured = config.baton.opencode.bin
    if configured:
        return configured if Path(configured).expanduser().is_file() else shutil.which(configured)
    return shutil.which("opencode")


# -- sessions -------------------------------------------------------------------

@dataclass
class Session:
    resident: str
    id: str
    title: str
    kind: str = "work"  # work | visit:<visitor> | upkeep
    opencode_id: str = ""
    status: str = "new"  # new | running | frozen
    overrides: dict[str, object] = field(default_factory=lambda: cast(dict[str, object], {}))
    created: str = ""
    last_active: str = ""
    by: str = OWNER
    commands: list[str] = field(default_factory=lambda: cast(list[str], []))  # exact shell commands allowed, from an approved request
    reviewed: str = ""  # when the resident last reviewed this session into its memory
    previous: list[str] = field(default_factory=lambda: cast(list[str], []))  # earlier opencode sessions, after one filled up
    task: str = ""  # what it was started with, if anything: sent as its first message
    opened: str = ""  # when someone last opened it from Babo
    archived: bool = False
    notified: str = ""  # what the owner was last told about it ("waiting", "wrapped up")
    pinned: bool = False  # kept at the top of the session lists
    tags: list[str] = field(default_factory=lambda: cast(list[str], []))  # the owner's own labels, for finding it again
    done_check: str = ""  # the owner's command that must succeed before the task counts as done
    done_status: str = ""  # "" (not yet) | passed | failed
    done_tries: int = 0
    done_checked: str = ""  # when it last ran
    rehearsal: str = ""  # "<rehearsal id>:<step>" while it rehearses a round's step: the owner's answers are recorded

    @property
    def key(self) -> str:
        """The session's name in the town's event log, checkpoints and transcripts."""
        return f"opencode:{self.id}"


def _sessions_dir(town: Town, resident: str) -> Path:
    town.resident_dir(resident)  # validates the id
    return town.state / "sessions" / resident


def _session_path(town: Town, resident: str, session_id: str) -> Path:
    if not session_id or "/" in session_id or session_id.startswith("."):
        raise BatonError(f"'{session_id}' is not a session")
    return _sessions_dir(town, resident) / f"{session_id}.json"


def save_session(town: Town, session: Session) -> None:
    atomic_write(_session_path(town, session.resident, session.id), json.dumps(asdict(session), indent=2) + "\n")


def sessions(town: Town, resident: str) -> list[Session]:
    out: list[Session] = []
    for path in sorted(_sessions_dir(town, resident).glob("*.json")):
        try:
            out.append(Session(**json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError):
            continue
    return sorted(out, key=lambda s: s.last_active or s.created, reverse=True)


def get_session(town: Town, resident: str, session_id: str | None = None) -> Session:
    found = sessions(town, resident)
    work = [s for s in found if s.kind == "work"]
    if session_id is None:
        if not work:
            raise BatonError(f"{resident} has no sessions yet; start one with `babo session new {resident}`.")
        return work[0]
    for s in found:
        if s.id == session_id or s.opencode_id == session_id:
            return s
    raise BatonError(f"{resident} has no session '{session_id}'.")


def new_session(
    town: Town, config: Config, resident: str, *, title: str = "", overrides: dict[str, object] | None = None,
    by: str = OWNER, kind: str = "work", commands: list[str] | None = None, done_check: str = "",
) -> Session:
    """Record a new session. The owner may change anything for it; the Mayor may
    only narrow it, because a resident's profile is the owner's to widen."""
    agent = Registry(town).require_active(resident)
    if not runs_in_opencode(agent.office):
        raise BatonError("The Mayor runs in Babo itself; talk to it with `babo` or the web interface.")
    effective_profile(town, config, resident, overrides or {}, by=by)
    if done_check.strip() and not is_owner(by):
        raise BatonError(f"Only {town.owner_name} can give a session a done-check: it runs as a command on the machine.")
    stamp = now_iso()
    session_id = stamp[:16].replace(":", "").replace("T", "-") + "-" + secrets.token_hex(2)
    session = Session(resident=resident, id=session_id, title=title.strip() or f"{resident}: {stamp[:16].replace('T', ' ')}",
                      kind=kind, overrides=dict(overrides or {}), created=stamp, by=by, commands=list(commands or []),
                      done_check=done_check.strip())
    save_session(town, session)
    return session


def effective_profile(town: Town, config: Config, resident: str, overrides: dict[str, object], *, by: str) -> profiles.Profile:
    office = Registry(town).require_active(resident).office
    base = profiles.load(town, config.baton, resident, office)
    mcp_known = sorted(config.tools.mcp_servers)
    from babo.baton.gateway import preset_names

    session = profiles.session_profile(base, overrides, agent=resident, mcp_known=mcp_known, presets_known=preset_names(config))
    wider = profiles.widening(base, session)
    if wider and not is_owner(by):
        raise BatonError(f"Only {town.owner_name} can give a session more than its resident's profile allows: " + "; ".join(wider))
    return session


# -- the opencode config ----------------------------------------------------------

def preset_for(config: Config, profile: profiles.Profile) -> str:
    """The model preset a session uses: its profile's (or the session's) choice, else the default preset.
    A preset that no longer exists falls back to the default, as it does in the gateway."""
    from babo.baton.gateway import default_preset_name

    chosen = profile.model.strip()
    return chosen if chosen in config.model_presets else default_preset_name(config)


def _context_tokens(config: Config, preset: str | None = None) -> int:
    if config.baton.opencode.context_tokens:
        return config.baton.opencode.context_tokens
    return config.resolve_preset(preset).context_window_tokens


def _mcp_entry(server: MCPServerConfig) -> dict[str, Any]:
    if server.command:
        entry: dict[str, Any] = {"type": "local", "command": [server.command, *server.args], "disabled": False, "codemode": False}
        if server.env:
            entry["environment"] = dict(server.env)
        return entry
    entry = {"type": "remote", "url": server.url, "disabled": False, "codemode": False}
    if server.headers:
        entry["headers"] = dict(server.headers)
    return entry


# The town's own files and folders, as seen from its root.
_TOWN_OWN = ("CHARTER.md", "HOUSE.md", "REGISTRY.md", ".baton-town", ".gitignore", ".git/*", "commons/*", "state/*",
             "crew/*", "residents/*", "rounds/*", "offices/*")


def _tool_prefix(server: str) -> str:
    """How opencode names an MCP server's tools (and their permission action): ``<server>_<tool>``."""
    import re

    return re.sub(r"[^A-Za-z0-9_-]", "_", server) + "_"


def _command_pieces(command: str) -> list[str]:
    """opencode checks a compound command one piece at a time (``a && b`` is ``a`` and ``b``),
    so an approved command is allowed as a whole and as its pieces."""
    import re

    pieces = [p.strip() for p in re.split(r"\s*(?:&&|\|\||;|\|)\s*", command) if p.strip()]
    return list(dict.fromkeys([command.strip(), *pieces]))


_READING = ("read", "glob", "grep")


def _permissions(town: Town, resident: str, profile: profiles.Profile, *, unattended: bool = False,
                 commands: list[str] | None = None, mcp: list[str] | None = None) -> list[dict[str, str]]:
    """opencode's ordered permission rules for a session (the last matching rule wins).

    Everything not named here is denied. ``unattended``: nobody is at a terminal
    (an approved request), so whatever would ask is denied instead. ``commands``:
    exact shell commands allowed even where the shell is not. ``mcp``: the MCP
    servers whose tools it may use (Babo's own town tools always)."""
    town.resident_dir(resident)  # validates the id

    def level(capability: str) -> str:
        value = profile.level(capability)
        return "deny" if unattended and value == "ask" else value

    def rule(action: str, resource: str, effect: str) -> dict[str, str]:
        return {"action": action, "resource": resource, "effect": effect}

    rules = [
        rule("*", "*", "deny"),
        # Reading, finding and searching: in its own folder, or anywhere in the town when its
        # profile says so (the town is one git repository, so opencode treats it as the
        # project); never outside it.
        *[rule(action, "*", "allow") for action in _READING],
        *([] if profile.read == "town" else [
            # Paths are matched relative to the resident's own folder, where it works ...
            *[rule(action, "../*", "deny") for action in _READING],
            # ... and by name, for a session seen from the town's root.
            *[rule(action, pattern, "deny") for action in _READING for pattern in ("residents/*", "state/*", ".git/*")],
            *[rule(action, f"residents/{resident}/*", "allow") for action in _READING],
        ]),
        rule("external_directory", "*", "deny"),
        # Edits are matched by their path relative to the resident's own folder,
        # where it works: anything that leaves it (``../``) is another resident's.
        rule("edit", "*", level("files")), rule("edit", "../*", "deny"),
        # And by name, for a session seen from the town's root (Babo's plugin refuses those too):
        # the town's own files and every resident's folder but its own.
        *[rule("edit", pattern, "deny") for pattern in _TOWN_OWN],
        rule("edit", f"residents/{resident}/*", level("files")),
        rule("shell", "*", level("shell")),
        rule("webfetch", "*", level("web_fetch")),
        rule("subagent", "*", level("subagents")),
        rule(f"{_tool_prefix(PROVIDER)}*", "*", "allow"),
    ]
    rules += [rule(f"{_tool_prefix(name)}*", "*", "allow") for name in mcp or []]
    if commands and level("shell") != "allow":
        rules += [rule("shell", piece, "allow") for command in commands for piece in _command_pieces(command)]
    return rules


def _priority(kind: str) -> str:
    """Where the session's model calls wait in the queue: someone's work first, then visits, then upkeep."""
    if kind == "upkeep" or kind.startswith(("round:", "rehearsal:")):
        return "upkeep"
    return "visit" if kind.startswith("visit:") else "interactive"


def opencode_config(town: Town, config: Config, session: Session, *, bundle: Path, config_path: Path, babo: str) -> dict[str, Any]:
    """The resident's whole opencode configuration, in opencode 2's own format, passed inline."""
    profile = effective_profile(town, config, session.resident, session.overrides, by=session.by)
    # opencode is told a preset name only; Babo's gateway knows the real model, its provider and its key.
    model_id = preset_for(config, profile)
    tokens = _context_tokens(config, model_id)
    prompt = (files("babo.baton") / "templates" / "opencode" / "resident.md").read_text(encoding="utf-8")
    servers: dict[str, Any] = {
        # Tools stay on the model's own tool list (not behind opencode's Code Mode, which is denied).
        PROVIDER: {"type": "local", "disabled": False, "codemode": False,
                   "command": [babo, "mcp", "--as", session.resident, "--session", session.id, "-c", str(config_path)]},
    }
    allowed_mcp: list[str] = []
    for name in profiles.mcp_servers(profile, sorted(config.tools.mcp_servers)):
        server = config.tools.mcp_servers.get(name)
        if server is not None and name != PROVIDER:
            servers[name] = _mcp_entry(server)
            allowed_mcp.append(name)
    model = f"{PROVIDER}/{model_id}"
    return {
        "$schema": "https://opencode.ai/config.json",
        "providers": {
            PROVIDER: {
                "package": "aisdk:@ai-sdk/openai-compatible",
                "name": "Babo",
                "settings": {"baseURL": config.baton.model.endpoint},
                "headers": {"X-Baton-Resident": session.resident, "X-Baton-Priority": _priority(session.kind)},
                "models": {model_id: {"name": model_id, "limit": {"context": tokens, "output": min(8192, tokens // 4)}}},
            },
        },
        "model": model,
        "default_agent": AGENT,
        "agents": {
            AGENT: {"mode": "primary", "description": f"The resident {session.resident}", "system": prompt,
                    "permissions": _permissions(town, session.resident, profile, unattended=session.kind.startswith(("request:", "round:")),
                                                commands=session.commands, mcp=allowed_mcp)},
            # Titles come from the same model, through the same gateway.
            "title": {"model": model},
        },
        "instructions": [str(bundle)],
        "mcp": {"servers": servers},
        # opencode's own summarizing is off: Babo never lets anything summarize a mind behind its back.
        "compaction": {"auto": False},
        "autoupdate": False,
    }


# -- running instances -------------------------------------------------------------

@dataclass
class Running:
    pid: int
    port: int
    password: str
    started: str
    door_port: int = 0  # where people reach it (the door counts who is there); 0 before doors
    door_pid: int = 0

    @property
    def api_url(self) -> str:
        """opencode itself, for Babo's own calls (they never count as someone being there)."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        """Where a person opens it: through the door."""
        return f"http://127.0.0.1:{self.door_port or self.port}"


def _runtime_dir(town: Town, resident: str) -> Path:
    return town.state / "runtime" / "opencode" / resident


def _runtime_path(town: Town, session: Session) -> Path:
    return _runtime_dir(town, session.resident) / f"{session.id}.json"


def _alive(pid: int) -> bool:
    try:  # reap it if it is our own child that has exited, so it doesn't linger as a zombie
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # someone else's exited child: still in the table, but not running
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def running(town: Town, session: Session) -> Running | None:
    try:
        data = json.loads(_runtime_path(town, session).read_text(encoding="utf-8"))
        run = Running(**data)
    except (OSError, ValueError, TypeError):
        return None
    return run if _alive(run.pid) else None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _client(run: Running, timeout: float) -> httpx.Client:
    return httpx.Client(base_url=run.api_url, auth=("opencode", run.password), timeout=timeout)


def _resident_env(config: Config, resident: str, *, password: str, inline: dict[str, Any]) -> dict[str, str]:
    root = Path(config.baton.opencode.data_dir).expanduser() / resident
    env = dict(os.environ)
    for name in ("config", "data", "cache", "state"):
        (root / name).mkdir(parents=True, exist_ok=True)
        env[f"XDG_{name.upper()}_HOME"] = str(root / name)
    env.update({name: "1" for name in _ENV_OFF})
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(inline)
    env["OPENCODE_PASSWORD"] = password
    env.pop("OPENCODE_CONFIG", None)
    env.pop("OPENCODE_CONFIG_DIR", None)
    return env


def _memory_env(town: Town, config: Config, session: Session, *, config_path: Path, babo: str) -> dict[str, str]:
    """What Babo's plugin needs to know: where to spool events, and when to ask for a checkpoint."""
    profile = effective_profile(town, config, session.resident, session.overrides, by=session.by)
    memory = config.baton.memory
    return {
        "BABO_BIN": babo, "BABO_CONFIG": str(config_path), "BABO_RESIDENT": session.resident, "BABO_SESSION": session.id,
        "BABO_TASKS": str(_tasks_reminder(town, session)),
        "BABO_HOME": str(town.resident_dir(session.resident)),
        "BABO_SPOOL": str(spool_path(town, session)),
        "BABO_NOW": str(now_path(town, session)),
        "BABO_GUIDES": str(guides_path(town, session)),
        "BABO_CHECKPOINT_EVERY": str(profile.memory.checkpoint_every_tool_calls or memory.checkpoint_every_tool_calls),
        "BABO_COMMIT_GATE": str(memory.commit_gate_after_tool_calls),
        "BABO_IDLE_MINUTES": str(profile.memory.idle_minutes or memory.idle_minutes),
        "BABO_CONTEXT_TOKENS": str(_context_tokens(config, preset_for(config, profile))),
    }


def _install_plugin(config: Config, resident: str) -> None:
    plugins = Path(config.baton.opencode.data_dir).expanduser() / resident / "config" / "opencode" / "plugins"
    (plugins / "babo.js").unlink(missing_ok=True)  # opencode 1's plugin file; opencode 2 loads a folder
    (plugins / "babo").mkdir(parents=True, exist_ok=True)
    atomic_write(plugins / "babo" / "index.js", (files("babo.baton") / "templates" / "opencode" / "babo-plugin.js").read_text(encoding="utf-8"))


def _bundle(town: Town, config: Config, resident: str) -> Any:
    from babo.baton.bundle import build_bundle

    return build_bundle(town, resident, max_tokens=config.baton.bundle.max_tokens,
                        search_hits=config.baton.bundle.search_hits,
                        letter_preview_lines=config.baton.bundle.letter_preview_lines,
                        index_path=config.baton.index_file)


def now_path(town: Town, session: Session) -> Path:
    """Where the resident is now (its baton, unread letters): Babo's plugin adds it at the end of each turn."""
    return _runtime_dir(town, session.resident) / f"{session.id}.now.md"


def write_bundle(town: Town, config: Config, session: Session) -> Path:
    """The session's instructions (what stays the same all session: the start of every prompt, which the
    model server keeps cached) and, beside it, where the resident is now (which goes last)."""
    bundle = _bundle(town, config, session.resident)
    path = _runtime_dir(town, session.resident) / f"{session.id}.bundle.md"
    atomic_write(path, bundle.stable_text())
    atomic_write(now_path(town, session), bundle.now_text())
    from babo.baton import guides

    atomic_write(guides_path(town, session), guides.plugin_file(town, Registry(town).require_active(session.resident).office))
    return path


def _tasks_reminder(town: Town, session: Session) -> Path:
    from babo.baton import tasks

    return tasks.reminder_path(town, session.resident, session.id)


def guides_path(town: Town, session: Session) -> Path:
    """The guides Babo's plugin brings in for this session (their triggers and text)."""
    return _runtime_dir(town, session.resident) / f"{session.id}.guides.json"


def refresh_now(town: Town, config: Config, session: Session) -> None:
    """Bring "where you are now" up to date: after a checkpoint, when a letter comes."""
    text = _bundle(town, config, session.resident).now_text()
    path = now_path(town, session)
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    atomic_write(path, text)


def refresh_running_now(town: Town, config: Config) -> None:
    for resident in Registry(town).all():
        for session in sessions(town, resident.id):
            if running(town, session) is not None:
                try:
                    refresh_now(town, config, session)
                except (OSError, BatonError):
                    logger.exception("Baton: could not refresh where {} is now", resident.id)


def start(town: Town, config: Config, session: Session, *, config_path: Path) -> Running:
    """Start (or find) the opencode instance for a session, and make sure its opencode session exists."""
    with FileLock(str(town.locks / f"opencode-{session.resident}-{session.id}.lock"), timeout=120):
        run = running(town, session)
        if run is None:
            binary = opencode_bin(config)
            if binary is None:
                raise BatonError("opencode is not installed (or baton.opencode.bin is wrong); residents other than the Mayor run in it. "
                                 "Install it with: curl -fsSL https://opencode.ai/install | bash")
            babo = babo_command(config)
            inline = opencode_config(town, config, session, bundle=write_bundle(town, config, session), config_path=config_path, babo=babo)
            password = secrets.token_urlsafe(18)
            port = _free_port()
            log = (_runtime_dir(town, session.resident) / f"{session.id}.log").open("ab")
            env = _resident_env(config, session.resident, password=password, inline=inline)
            env.update(_memory_env(town, config, session, config_path=config_path, babo=babo))
            _install_plugin(config, session.resident)
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                [binary, "serve", "--port", str(port), "--hostname", "127.0.0.1"],
                cwd=town.resident_dir(session.resident), env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
            run = Running(pid=proc.pid, port=port, password=password, started=now_iso())
            atomic_write(_runtime_path(town, session), json.dumps(asdict(run)) + "\n")
            try:
                _wait_healthy(run, proc, config.baton.opencode.start_timeout_s, log_path=Path(log.name))
                _wait_for_mcp(run, config.baton.opencode.start_timeout_s)
                run = _open_door(town, session, run, log)
                atomic_write(_runtime_path(town, session), json.dumps(asdict(run)) + "\n")
            except BatonError:
                _stop(run)
                _runtime_path(town, session).unlink(missing_ok=True)
                raise
        if session.opencode_id and not _has_session(run, session.opencode_id):
            # Not in this opencode (one from opencode 1, say): carry on in a fresh session,
            # which starts from the resident's baton like any other.
            session.previous.append(session.opencode_id)
            session.opencode_id = ""
        if not session.opencode_id:
            try:
                with _client(run, 30) as client:
                    session.opencode_id = _new_opencode_session(client, session.title)
            except (BatonError, httpx.HTTPError):
                # Don't leave an instance running for a session that never started.
                _stop(run)
                _runtime_path(town, session).unlink(missing_ok=True)
                raise
        session.status = "running"
        session.last_active = now_iso()
        save_session(town, session)
        return run


def door_status_path(town: Town, session: Session) -> Path:
    return _runtime_dir(town, session.resident) / f"{session.id}.door.json"


def _open_door(town: Town, session: Session, run: Running, log: Any) -> Running:
    """Start the session's door (``babo/baton/door.py``), the way people reach it."""
    import sys

    door_port = _free_port()
    status = door_status_path(town, session)
    status.unlink(missing_ok=True)
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "babo.baton.door", str(door_port), str(run.port), str(status), str(run.pid)],
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if status.exists():
            return Running(pid=run.pid, port=run.port, password=run.password, started=run.started,
                           door_port=door_port, door_pid=proc.pid)
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    proc.kill()
    raise BatonError("The session's door didn't open (see the session's log).")


def presence(town: Town, session: Session) -> dict[str, Any]:
    """Who is in the session through its door: how many connections are open, and when the last one was."""
    try:
        data = json.loads(door_status_path(town, session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"connections": 0, "last_seen": ""}
    return {"connections": int(data.get("connections") or 0), "last_seen": str(data.get("last_seen") or "")}


def pairing_link(town: Town, session: Session) -> str:
    """A one-time link that opens the session's web page, connected, with no password to type."""
    run = running(town, session)
    if run is None:
        raise BatonError("That session isn't running; resume it first.")
    with _client(run, 10) as client:
        code = str(cast(dict[str, Any], _json(client.post("/api/pair", json={}), "a pairing code"))["code"])
    session = get_session(town, session.resident, session.id)
    session.opened = now_iso()
    save_session(town, session)
    return f"{run.url}/auth/connect/{code}"


def _wait_healthy(run: Running, proc: subprocess.Popen[bytes], timeout_s: int, *, log_path: Path) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-600:] if log_path.exists() else ""
            raise BatonError(f"opencode stopped while starting (exit {proc.returncode}). {tail.strip()}")
        try:
            with _client(run, 2) as client:
                resp = client.get("/api/info")
        except httpx.HTTPError:
            time.sleep(0.3)
            continue
        if resp.status_code == 200:
            try:
                health = resp.json()
            except ValueError:
                health = None
            if not isinstance(health, dict):
                raise BatonError(f"This opencode started but doesn't answer /api/info (it answered with a web page): it {NOT_OURS}.")
            problem = version_problem(str(cast(dict[str, Any], health).get("version", "")))
            if problem:
                raise BatonError(problem)
            return
        time.sleep(0.3)
    proc.terminate()
    raise BatonError(f"opencode didn't answer within {timeout_s} seconds.")


def _wait_for_mcp(run: Running, timeout_s: int) -> None:
    """opencode connects MCP servers in the background; the town's tools must be there for the first turn."""
    deadline = time.monotonic() + timeout_s
    with _client(run, 10) as client:
        while time.monotonic() < deadline:
            servers = cast(list[dict[str, Any]], _data(client.get("/api/mcp"), "the MCP servers"))
            status = {str(s.get("name")): cast(dict[str, Any], s.get("status") or {}) for s in servers}
            if PROVIDER in status and status[PROVIDER].get("status") not in ("pending", "connected"):
                raise BatonError(f"The town's tools (babo mcp) didn't connect in opencode: {json.dumps(status[PROVIDER])}")
            if not any(st.get("status") == "pending" for st in status.values()):
                return
            time.sleep(0.2)
    raise BatonError(f"opencode's MCP servers didn't connect within {timeout_s} seconds (the town's tools among them).")


def remove_session(town: Town, session: Session) -> None:
    """Remove a session from the list for good: for one that is broken or not wanted. It is stopped
    without a review; what it did is already in the resident's event log, and stays there."""
    run = running(town, session)
    if run is not None:
        _stop(run)
    ingest(town, session)
    runtime = _runtime_dir(town, session.resident)
    for leftover in (_runtime_path(town, session), spool_path(town, session),
                     runtime / f"{session.id}.bundle.md", now_path(town, session), guides_path(town, session),
                     runtime / f"{session.id}.log"):
        leftover.unlink(missing_ok=True)
    _session_path(town, session.resident, session.id).unlink(missing_ok=True)


def _stop(run: Running) -> None:
    """Stop the instance (its sessions are already saved), and kill it if it takes its time."""
    if run.door_pid:
        try:
            os.killpg(run.door_pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        os.killpg(run.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + 3
    while _alive(run.pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    if _alive(run.pid):
        try:
            os.killpg(run.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def freeze(town: Town, session: Session, *, review: Config | None = None, config_path: Path | None = None) -> bool:
    """Stop the session's instance. Its opencode session stays, so resuming
    continues it. With ``review`` (the config), the resident first reviews the
    session into its memory. True if it was running."""
    run = running(town, session)
    was_running = run is not None
    if run is not None and review is not None and config_path is not None and session.kind == "work" and session.opencode_id:
        try:
            checkpoint(town, review, session, trigger="session_end", config_path=config_path)
        except (BatonError, httpx.HTTPError) as exc:
            # The safety net in upkeep reviews it later; stopping must not depend on the model.
            logger.warning("Babo: {}'s session {} was frozen without its review: {}", session.resident, session.id, exc)
        session = get_session(town, session.resident, session.id)
    if run is not None:
        _stop(run)
    _runtime_path(town, session).unlink(missing_ok=True)
    ingest(town, session)
    session.status = "frozen"
    save_session(town, session)
    return was_running


def send(town: Town, config: Config, session: Session, text: str, *, config_path: Path, timeout_s: float = 1800) -> str:
    """Send one message and wait for the resident's reply."""
    run = start(town, config, session, config_path=config_path)
    deadline = time.monotonic() + timeout_s
    with _client(run, timeout_s) as client:
        sent = str(_data(client.post(f"/api/session/{session.opencode_id}/prompt", json={"text": text}), "a message")["id"])
        while True:
            # Wait for the session to go idle; the answer comes with an "idle" message after ours.
            client.post(f"/api/experimental/session/{session.opencode_id}/wait").raise_for_status()
            after = _after(_messages(run, session), sent)
            if any(m.get("type") == "idle" for m in after):
                break
            if time.monotonic() > deadline:
                raise BatonError(f"{session.resident} didn't finish its turn within {int(timeout_s)} seconds.")
            time.sleep(0.5)
    session.last_active = now_iso()
    save_session(town, session)
    failed = [m for m in after if m.get("type") == "idle" and m.get("outcome") not in (None, "succeeded")]
    if failed:
        error = cast(dict[str, Any], failed[-1].get("error") or {})
        raise BatonError(f"{session.resident}'s turn ended {failed[-1].get('outcome')}: {error.get('message', '')}".strip(": "))
    return "\n".join(_text(m) for m in after if m.get("type") == "assistant" and _text(m)).strip()


def prompt(town: Town, config: Config, session: Session, text: str, *, config_path: Path) -> str:
    """Send a message without waiting: the resident works on it in the background. Returns the message's id."""
    if not text.strip():
        raise BatonError("The message is empty.")
    run = start(town, config, session, config_path=config_path)
    try:
        with _client(run, 90) as client:
            sent = str(_data(client.post(f"/api/session/{session.opencode_id}/prompt", json={"text": text}), "a message")["id"])
    except httpx.TimeoutException as exc:
        raise BatonError(f"{session.resident}'s opencode didn't take the message within 90 seconds. "
                         f"{_town_tools_problem(run) or 'Its log is in the town under state/runtime/opencode/.'}") from exc
    session = get_session(town, session.resident, session.id)
    session.last_active = now_iso()
    save_session(town, session)
    return sent


def _town_tools_problem(run: Running) -> str:
    """Why the town's tools aren't there, when they aren't (shown with errors and in the session's status)."""
    try:
        with _client(run, 5) as client:
            servers = cast(list[dict[str, Any]], _data(client.get("/api/mcp"), "the MCP servers"))
    except (BatonError, httpx.HTTPError):
        return ""
    town = next((s for s in servers if s.get("name") == PROVIDER), None)
    status = cast(dict[str, Any], (town or {}).get("status") or {})
    if town is None or status.get("status") == "connected":
        return ""
    return f"The town's tools (babo mcp) are {status.get('status')}: {status.get('error') or status.get('message') or ''}".strip(": ")


def interrupt(town: Town, session: Session) -> bool:
    """Stop the resident's current turn; the session stays open. True if it was working."""
    run = running(town, session)
    if run is None or not session.opencode_id:
        return False
    with _client(run, 30) as client:
        resp = client.post(f"/api/session/{session.opencode_id}/interrupt")
    resp.raise_for_status()
    return bool(cast(dict[str, Any], resp.json()).get("interrupted"))


def permission_requests(run: Running, session: Session) -> list[dict[str, Any]]:
    with _client(run, 10) as client:
        return cast(list[dict[str, Any]], _data(client.get(f"/api/session/{session.opencode_id}/permission"), "permission requests"))


def answer_permission(town: Town, session: Session, request_id: str, decision: str, *, message: str = "") -> None:
    """Answer a question the resident's opencode is waiting on: once, always (for this session) or reject
    (with ``message``, the reason it's told, so it carries on rather than stop)."""
    if decision not in ("once", "always", "reject"):
        raise BatonError("Answer with once, always or reject.")
    run = running(town, session)
    if run is None:
        raise BatonError("That session isn't running.")
    with _client(run, 30) as client:
        body = {"decision": decision, **({"message": message} if message else {})}
        client.post(f"/api/session/{session.opencode_id}/permission/{request_id}/reply", json=body).raise_for_status()


def allow_everything() -> dict[str, str]:
    """*Allow all* for one session: every capability allowed, and reading the whole town."""
    return {**{c.key: "allow" for c in profiles.CAPABILITIES}, "read": "town"}


def change_session(town: Town, config: Config, session: Session, changes: dict[str, str], *, by: str = OWNER,
                   config_path: Path) -> Session:
    """Change what a session may do (or its model), now. A running session restarts with the new
    settings and carries on exactly where it was. A value of "" (or "profile") goes back to the
    resident's profile. Only the owner may give a session more than its resident's profile allows."""
    overrides = dict(session.overrides)
    for key, value in changes.items():
        value = str(value).strip()
        if value in ("", "profile") or (key == "model" and value == "default"):
            overrides.pop(key, None)
        else:
            overrides[key] = value
    effective_profile(town, config, session.resident, overrides, by=by)  # validates, and refuses a widening by anyone else
    session.overrides = overrides
    if is_owner(by):
        session.by = OWNER  # its access is now the owner's choice
    save_session(town, session)
    run = running(town, session)
    if run is not None:
        _stop(run)
        _runtime_path(town, session).unlink(missing_ok=True)
        start(town, config, session, config_path=config_path)
    return get_session(town, session.resident, session.id)


def switch_model(town: Town, config: Config, session: Session, preset: str, *, config_path: Path) -> Session:
    """Give a session another model preset. A running session restarts on it and carries on where it was."""
    return change_session(town, config, session, {"model": preset}, by=session.by, config_path=config_path)


def _activity(message: dict[str, Any]) -> str:
    """What a message shows the resident doing, in a few words."""
    if message.get("type") == "user":
        return "reading your message"
    for part in reversed(cast(list[dict[str, Any]], message.get("content") or [])):
        if part.get("type") == "tool":
            state = cast(dict[str, Any], part.get("state") or {})
            data = cast(dict[str, Any], state.get("input") or {}) if isinstance(state.get("input"), dict) else {}
            detail = data.get("command") or data.get("path") or data.get("pattern") or data.get("url") or ""
            return f"{part.get('name')} {str(detail)[:80]}".strip()
        if part.get("type") in ("text", "reasoning") and part.get("text"):
            return "writing" if part.get("type") == "text" else "thinking"
    return "thinking"


def live(town: Town, session: Session) -> dict[str, Any]:
    """What a session is doing now: frozen or new; or, while it runs, working, idle, or waiting for you."""
    run = running(town, session)
    if run is None:
        return {"state": "frozen" if session.status != "new" else "new", "activity": "", "questions": []}
    if not session.opencode_id:
        return {"state": "starting", "activity": "", "questions": []}
    try:
        questions: list[dict[str, Any]] = [
            {"id": q.get("id"), "action": q.get("action"), "resources": q.get("resources") or [], "message": q.get("message") or ""}
            for q in permission_requests(run, session)]
        with _client(run, 10) as client:
            body = _json(client.get(f"/api/session/{session.opencode_id}/message", params={"order": "desc", "limit": 5}), "messages")
    except (BatonError, httpx.HTTPError):
        return {"state": "running", "activity": "", "questions": []}
    recent = cast(list[dict[str, Any]], cast(dict[str, Any], body).get("data") or [])
    if questions:
        return {"state": "waiting", "activity": "waiting for your answer", "questions": questions}
    shown = [m for m in recent if m.get("type") in ("user", "assistant", "idle")]
    if not shown or shown[0].get("type") == "idle":
        outcome = str(shown[0].get("outcome") or "") if shown else ""
        return {"state": "idle", "activity": "" if outcome in ("", "succeeded") else f"its last turn {outcome}", "questions": []}
    return {"state": "working", "activity": _activity(shown[0]), "questions": []}


def _after(messages: list[dict[str, Any]], message_id: str) -> list[dict[str, Any]]:
    ids = [str(m.get("id")) for m in messages]
    return messages[ids.index(message_id) + 1:] if message_id in ids else []


def _text(message: dict[str, Any]) -> str:
    parts = cast(list[dict[str, Any]], message.get("content") or [])
    return "\n".join(str(p.get("text", "")) for p in parts if p.get("type") == "text" and p.get("text")).strip()


# -- memory: events, checkpoints, reviews -------------------------------------------------

def spool_path(town: Town, session: Session) -> Path:
    return _runtime_dir(town, session.resident) / f"{session.id}.spool.jsonl"


_TOOL_ALIASES = {"shell": "exec"}  # the event log's names for opencode's tools, so its rules apply alike


def ingest(town: Town, session: Session) -> list[dict[str, Any]]:
    """Move what Babo's plugin spooled into the town's event log, redacted
    and classified the same way as for residents running in Babo."""
    from babo.baton.events import append_event, classify, tool_record

    spool = spool_path(town, session)
    if not spool.exists():
        return []
    taking = spool.with_suffix(".ingesting")
    spool.replace(taking)
    written: list[dict[str, Any]] = []
    for line in taking.read_text(encoding="utf-8").splitlines():
        try:
            raw = cast(dict[str, Any], json.loads(line))
        except ValueError:
            continue
        at = str(raw.get("at", "")) or now_iso()
        if raw.get("kind") == "tool":
            tool = _TOOL_ALIASES.get(str(raw.get("tool", "")), str(raw.get("tool", "")))
            args = cast(dict[str, Any], raw.get("args") if isinstance(raw.get("args"), dict) else {})
            code = raw.get("exit_code")
            result = (f"Exit code: {code}\n" if isinstance(code, int) else "") + str(raw.get("output", ""))
            event = classify(tool, args, result, error=False)
            written.append(append_event(town, session.resident, session.key, "tool", at=at, via="opencode", **tool_record(event)))
        elif raw.get("kind") == "turn_start":
            written.append(append_event(town, session.resident, session.key, "turn_start", at=at, via="opencode"))
        elif raw.get("kind") == "drift":
            from babo.baton import drift

            rule, tool, times = str(raw.get("rule", ""))[:40], str(raw.get("tool", ""))[:80], raw.get("times")
            append_event(town, session.resident, session.key, drift.EVENT, at=at, via="opencode", rule=rule, tool=tool, times=times)
            if times == 2:
                drift.tell_owner(town, session.resident, session.id, session.title, rule, tool)
        elif raw.get("kind") == "guide":
            append_event(town, session.resident, session.key, "guide", at=at, via="opencode",
                         name=str(raw.get("name", ""))[:80], how=str(raw.get("how", ""))[:80])
    taking.unlink(missing_ok=True)
    if written:
        latest = max(str(r["at"]) for r in written)
        fresh = get_session(town, session.resident, session.id)
        if _when(latest) > _when(fresh.last_active):
            fresh.last_active = latest
            save_session(town, fresh)
    return written


def _when(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.astimezone()


def wrap_up_reason(town: Town, config: Config, session: Session, *, now: datetime) -> str:
    """Why upkeep should wrap a running session up now, or "" to leave it be.

    It never wraps up a session while the resident is working, or while someone has it
    open and something has happened lately; a session waiting on the owner's answer waits."""
    oc = config.baton.opencode
    state = live(town, session)
    if state["state"] in ("working", "starting", "running"):
        return ""
    active = _when(session.last_active)
    if state["state"] == "waiting":
        return "it waited a day for an answer" if now - active >= timedelta(hours=oc.wrap_up_waiting_hours) else ""
    if session.done_check and not session.done_status:
        return ""  # its done-check hasn't passed (or given up) yet: upkeep runs it first
    here = presence(town, session)
    if here["connections"]:
        quiet = now - active >= timedelta(minutes=oc.freeze_after_idle_minutes)
        return "open, but nothing has happened for a long while" if quiet else ""
    seen = max(_when(here["last_seen"]), _when(session.opened))
    if seen == _when(""):
        return "done with its task" if now - active >= timedelta(minutes=oc.wrap_up_after_task_minutes) else ""
    left = max(seen, active)
    return "done, and everyone has left" if now - left >= timedelta(minutes=oc.wrap_up_after_leaving_minutes) else ""


DONE_TRIES = 2
DONE_TIMEOUT_S = 120


def run_done_check(town: Town, session: Session) -> tuple[bool, str]:
    """Run a session's done-check in the resident's folder: (passed, the end of what it printed)."""
    try:
        done = subprocess.run(["bash", "-lc", session.done_check], cwd=town.resident_dir(session.resident),
                              capture_output=True, text=True, timeout=DONE_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return False, f"(it took longer than {DONE_TIMEOUT_S} seconds)"
    except OSError as exc:
        return False, f"(it could not run: {exc})"
    output = (done.stdout + done.stderr).strip()
    return done.returncode == 0, (output[-1500:] or f"(exit code {done.returncode}, no output)")


def check_done(town: Town, config: Config, *, config_path: Path) -> list[str]:
    """Run the done-check of each running session that has finished its turn since the last check.
    Passed: it may wrap up. Failed: the resident hears what failed and carries on, up to DONE_TRIES
    times; then the owner hears that it isn't done, and it may wrap up."""
    from babo.baton import notices

    said: list[str] = []
    for resident in (r.id for r in Registry(town).all() if r.status == "active" and runs_in_opencode(r.office)):
        for session in sessions(town, resident):
            if session.kind != "work" or not session.done_check or session.done_status or running(town, session) is None:
                continue
            if live(town, session)["state"] != "idle" or _when(session.done_checked) >= _when(session.last_active):
                continue
            passed, output = run_done_check(town, session)
            session = get_session(town, resident, session.id)
            session.done_checked = now_iso()
            if passed:
                session.done_status = "passed"
                said.append(f"{resident}/{session.id}: done-check passed")
            else:
                session.done_tries += 1
                if session.done_tries >= DONE_TRIES:
                    session.done_status = "failed"
                    notices.add(town, "not_done", f"{resident}'s “{session.title}” isn't done: its done-check `{session.done_check}` "
                                f"still fails after {session.done_tries} tries. It will wrap up; resume it, or look at it with them.",
                                resident=resident, session=session.id)
                else:
                    message = (f"[From Babo] The task isn't done yet: its check `{session.done_check}` failed:\n\n{output}\n\n"
                               "Carry on until it passes (or say plainly what stands in the way).")
                    save_session(town, session)
                    prompt(town, config, session, message, config_path=config_path)
                    session = get_session(town, resident, session.id)
                    session.done_checked = now_iso()
                said.append(f"{resident}/{session.id}: done-check failed ({session.done_tries})")
            save_session(town, session)
    return said


def notify_waiting(town: Town) -> list[str]:
    """Tell the owner (once) about each running session waiting on their answer."""
    from babo.baton import notices

    told: list[str] = []
    for resident in (r.id for r in Registry(town).all() if r.status == "active" and runs_in_opencode(r.office)):
        for session in sessions(town, resident):
            if session.kind != "work" or running(town, session) is None:
                continue
            state = live(town, session)
            if state["state"] == "waiting" and session.notified != "waiting":
                question = cast(list[dict[str, Any]], state["questions"])[0]
                notices.add(town, "waiting", f"{resident} is waiting for your answer in “{session.title}”: may it "
                            f"{question.get('action')} {' · '.join(cast(list[str], question.get('resources') or []))}?",
                            resident=resident, session=session.id)
                session.notified = "waiting"
                save_session(town, session)
                told.append(f"{resident}/{session.id}")
            elif state["state"] != "waiting" and session.notified == "waiting":
                session.notified = ""
                save_session(town, session)
    return told


def needing_review(town: Town, config: Config, *, now: datetime) -> list[tuple[Session, str]]:
    """Work sessions upkeep should review and freeze now (see ``wrap_up_reason``), and
    sessions whose instance stopped without a review."""
    out: list[tuple[Session, str]] = []
    for resident in (r.id for r in Registry(town).all() if r.status == "active" and runs_in_opencode(r.office)):
        for session in sessions(town, resident):
            if session.kind != "work" or not session.opencode_id:
                continue
            if running(town, session) is not None:
                why = wrap_up_reason(town, config, session, now=now)
                if why:
                    out.append((session, why))
            elif _when(session.last_active) > _when(session.reviewed) and session.status == "running":
                out.append((session, "stopped without a review"))
    return out


def ingest_all(town: Town) -> int:
    """Every resident's spooled events into the event log (upkeep does this before it looks)."""
    count = 0
    root = town.state / "runtime" / "opencode"
    for spool in sorted(root.glob("*/*.spool.jsonl")) if root.exists() else []:
        resident, session_id = spool.parent.name, spool.name.removesuffix(".spool.jsonl")
        try:
            count += len(ingest(town, get_session(town, resident, session_id)))
        except BatonError:
            continue
    return count


def _messages(run: Running, session: Session) -> list[dict[str, Any]]:
    """The session's whole history, oldest first."""
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    with _client(run, 60) as client:
        while True:
            # A cursor carries the order with it.
            params: dict[str, str | int] = {"cursor": cursor} if cursor else {"order": "asc", "limit": 200}
            body = _json(client.get(f"/api/session/{session.opencode_id}/message", params=params), "the session's messages")
            page = cast(dict[str, Any], body)
            data = cast(list[dict[str, Any]], page.get("data") or [])
            out += data
            cursor = cast(dict[str, Any], page.get("cursor") or {}).get("next")
            if not cursor or not data:
                return out


def conversation(messages: list[dict[str, Any]], *, budget_tokens: int) -> list[dict[str, Any]]:
    """The session as plain chat messages (tool calls written out), newest kept within the budget."""
    from babo.baton.tokens import estimate_tokens

    out: list[dict[str, Any]] = []
    for message in messages:
        kind = message.get("type")
        pieces: list[str] = []
        if kind == "user" and message.get("text"):
            pieces.append(str(message["text"]))
        elif kind == "assistant":
            for part in cast(list[dict[str, Any]], message.get("content") or []):
                if part.get("type") == "text" and part.get("text"):
                    pieces.append(str(part["text"]))
                elif part.get("type") == "tool":
                    pieces.append(f"[{part.get('name')}] {json.dumps(_tool_input(part), ensure_ascii=False)[:400]} → {_tool_output(part)[:600]}")
        if pieces:
            out.append({"role": "user" if kind == "user" else "assistant", "content": "\n".join(pieces)})
    kept: list[dict[str, Any]] = []
    used = 0
    for message in reversed(out):
        cost = estimate_tokens(str(message["content"]))
        if kept and used + cost > budget_tokens:
            break
        kept.append(message)
        used += cost
    return list(reversed(kept))


def _tool_input(part: dict[str, Any]) -> object:
    state = cast(dict[str, Any], part.get("state") or {})
    return state.get("input", {})


def _tool_output(part: dict[str, Any]) -> str:
    state = cast(dict[str, Any], part.get("state") or {})
    if state.get("status") == "error":
        return str(cast(dict[str, Any], state.get("error") or {}).get("message", "error"))
    content = cast(list[dict[str, Any]], state.get("content") or [])
    return "\n".join(str(c.get("text", "")) for c in content if c.get("type") == "text")


def public_output(town: Town, session: Session, *, budget_tokens: int = 3000) -> str:
    """A public session's output for whoever asked for it: each step it took (tool calls with their
    results) and what it said, the newest kept within the budget, with secrets redacted."""
    from babo.baton.secrets import redact_text

    run = running(town, session)
    if run is None:
        return ""
    steps = conversation(_messages(run, session), budget_tokens=budget_tokens)
    return redact_text("\n\n".join(str(m["content"]) for m in steps if m["role"] == "assistant"))


def export_transcript(town: Town, session: Session, messages: list[dict[str, Any]]) -> Path:
    from babo.baton.events import session_slug
    from babo.baton.town import today

    path = town.resident_dir(session.resident) / "sessions" / today() / f"{session_slug(session.key)}.transcript.jsonl"
    atomic_write(path, "".join(json.dumps(m, ensure_ascii=False, default=str) + "\n" for m in messages))
    return path


def checkpoint(town: Town, config: Config, session: Session, *, trigger: str, config_path: Path) -> str:
    """Ask the resident, with its own session in view, to write a checkpoint in
    its own words; at the end of a session this is its review of the session.
    Starts the instance if it isn't running (that is how a session that ended
    without one is reviewed)."""
    import asyncio

    from babo.baton.checkpoint import CheckpointService

    with FileLock(str(town.locks / f"opencode-checkpoint-{session.resident}-{session.id}.lock"), timeout=600):
        session = get_session(town, session.resident, session.id)
        events = ingest(town, session)
        run = running(town, session) or start(town, config, session, config_path=config_path)
        messages = _messages(run, session)
        bundle_path = _runtime_dir(town, session.resident) / f"{session.id}.bundle.md"
        bundle = bundle_path.read_text(encoding="utf-8") if bundle_path.exists() else write_bundle(town, config, session).read_text(encoding="utf-8")
        try:
            bundle += "\n\n" + now_path(town, session).read_text(encoding="utf-8")
        except OSError:
            pass
        prompt = (files("babo.baton") / "templates" / "opencode" / "resident.md").read_text(encoding="utf-8")
        profile = effective_profile(town, config, session.resident, session.overrides, by=session.by)
        preset = preset_for(config, profile)
        convo = [{"role": "system", "content": prompt + "\n\n" + bundle},
                 *conversation(messages, budget_tokens=int(_context_tokens(config, preset) * 0.6))]
        overrides = {k: v for k, v in profile.memory.model_dump().items() if v is not None}
        baton_cfg = config.baton.model_copy(update={
            "memory": config.baton.memory.model_copy(update=overrides),
            # The resident writes its checkpoint with the model it works with.
            "model": config.baton.model.model_copy(update={"model": preset}),
        })
        outcome = asyncio.run(CheckpointService(town, session.resident, baton_cfg).run(
            convo, trigger=trigger, session_key=session.key, events=events))
        if trigger in ("session_end", "context_full"):
            export_transcript(town, session, messages)
            session.reviewed = now_iso()
        if trigger == "context_full":
            _rotate(town, session, run)
        save_session(town, session)
        refresh_now(town, config, session)  # the new baton, at the end of its next turn
        return f"{'Written by ' + session.resident if outcome.authored else 'Recorded from the event log'}: {outcome.checkpoint.thread}"


def _rotate(town: Town, session: Session, run: Running) -> None:
    """A full session continues in a fresh opencode session that starts from the resident's baton."""
    with _client(run, 30) as client:
        created = _new_opencode_session(client, session.title)
    session.previous.append(session.opencode_id)
    session.opencode_id = created


def special_session(town: Town, config: Config, resident: str, kind: str, *, access: dict[str, str] | None = None,
                    commands: list[str] | None = None) -> Session:
    """The one session a resident keeps for visits from someone, for upkeep, or for one step of a round
    (with the access its round gives it, which the owner approved with the round)."""
    for s in sessions(town, resident):
        if s.kind == kind:
            if access is not None and (s.overrides != access or s.commands != list(commands or [])):
                effective_profile(town, config, resident, cast(dict[str, object], dict(access)), by=OWNER)  # validates
                s.overrides, s.commands, s.by = cast(dict[str, object], dict(access)), list(commands or []), OWNER
                save_session(town, s)
            return s
    if kind.startswith("round:"):
        _, name, index = kind.split(":", 2)
        title = f"{resident}: round {name}, step {index}"
    else:
        title = {"upkeep": f"{resident}: upkeep"}.get(kind, f"{resident}: visits from {kind.split(':', 1)[-1]}")
    return new_session(town, config, resident, title=title, kind=kind, overrides=cast(dict[str, object], dict(access or {})),
                       commands=list(commands or []), by=OWNER)


def one_turn(town: Town, config: Config, resident: str, message: str, *, kind: str, config_path: Path,
             access: dict[str, str] | None = None, commands: list[str] | None = None) -> str:
    """One turn in a resident's visit, upkeep or round-step session; the instance is frozen again afterwards."""
    session = special_session(town, config, resident, kind, access=access, commands=commands)
    try:
        return send(town, config, session, message, config_path=config_path)
    finally:
        freeze(town, session)


def attach_argv(config: Config, session: Session, run: Running) -> tuple[list[str], dict[str, str]]:
    """The command (and environment) that opens the session in opencode's terminal interface."""
    binary = opencode_bin(config) or "opencode"
    env = dict(os.environ)
    env["OPENCODE_PASSWORD"] = run.password
    return [binary, "--server", run.url, "--session", session.opencode_id], env


def describe(town: Town, session: Session) -> dict[str, Any]:
    run = running(town, session)
    return {**asdict(session), "status": "running" if run else ("frozen" if session.status != "new" else "new"),
            "url": run.url if run else "", "password": run.password if run else "", "live": live(town, session),
            "presence": presence(town, session) if run else {"connections": 0, "last_seen": ""}}


def overview(town: Town, resident: str, *, archived: bool = False) -> list[dict[str, Any]]:
    """A resident's work sessions, pinned ones first; archived ones only when asked for."""
    found = [s for s in sessions(town, resident) if s.kind == "work" and s.archived == archived]
    return [describe(town, s) for s in sorted(found, key=lambda s: not s.pinned)]  # pinned first, then the latest


async def resident_turn(config: Config, town: Town, resident: str, message: str, *, kind: str,
                        in_babo: Callable[[], Awaitable[str]], access: dict[str, str] | None = None,
                        commands: list[str] | None = None) -> str:
    """One turn of a resident: in opencode when it lives there and opencode is
    installed, otherwise in Babo itself (``in_babo``)."""
    from babo.config.loader import get_config_path

    agent = Registry(town).require_active(resident)
    if runs_in_opencode(agent.office) and opencode_bin(config) is not None:
        return await asyncio.to_thread(one_turn, town, config, resident, message, kind=kind, config_path=get_config_path(),
                                       access=access, commands=commands)
    return await in_babo()
