"""Residents in opencode.

The Mayor runs in Babo itself. Every other resident runs as an opencode
instance (``opencode serve``) started by Babo for one of its sessions:

- **Identity on load.** The session-start bundle (who it is, its SELF and
  ROLE, its neighbours, where it left off, its letters) is written to a file
  opencode reads as instructions, under a short Babo prompt that replaces
  opencode's own.
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
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
from filelock import FileLock

from babo.baton import profiles
from babo.baton.registry import Registry
from babo.baton.town import BatonError, Town, atomic_write, now_iso

if TYPE_CHECKING:
    from babo.config.schema import Config, MCPServerConfig

PROVIDER = "babo"
AGENT = "resident"
# Switched off for residents: anything that would let opencode's own setup,
# the user's Claude Code files, or the resident's workspace change what Babo set.
_ENV_OFF = (
    "OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_EXTERNAL_SKILLS",
    "OPENCODE_DISABLE_AUTOUPDATE", "OPENCODE_DISABLE_AUTOCOMPACT", "OPENCODE_DISABLE_LSP_DOWNLOAD",
    "OPENCODE_DISABLE_MODELS_FETCH",
)


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
    by: str = "squid"


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
    by: str = "squid", kind: str = "work",
) -> Session:
    """Record a new session. Squid may change anything for it; the Mayor may
    only narrow it, because a resident's profile is Squid's to widen."""
    agent = Registry(town).require_active(resident)
    if not runs_in_opencode(agent.office):
        raise BatonError("The Mayor runs in Babo itself; talk to it with `babo` or the web interface.")
    effective_profile(town, config, resident, overrides or {}, by=by)
    stamp = now_iso()
    session_id = stamp[:16].replace(":", "").replace("T", "-") + "-" + secrets.token_hex(2)
    session = Session(resident=resident, id=session_id, title=title.strip() or f"{resident}: {stamp[:16].replace('T', ' ')}",
                      kind=kind, overrides=dict(overrides or {}), created=stamp, by=by)
    save_session(town, session)
    return session


def effective_profile(town: Town, config: Config, resident: str, overrides: dict[str, object], *, by: str) -> profiles.Profile:
    office = Registry(town).require_active(resident).office
    base = profiles.load(town, config.baton, resident, office)
    mcp_known = sorted(config.tools.mcp_servers)
    session = profiles.session_profile(base, overrides, agent=resident, mcp_known=mcp_known)
    wider = profiles.widening(base, session)
    if wider and by != "squid":
        raise BatonError("Only Squid can give a session more than its resident's profile allows: " + "; ".join(wider))
    return session


# -- the opencode config ----------------------------------------------------------

def _context_tokens(config: Config) -> int:
    if config.baton.opencode.context_tokens:
        return config.baton.opencode.context_tokens
    return config.resolve_default_preset().context_window_tokens


def _mcp_entry(server: MCPServerConfig) -> dict[str, Any]:
    if server.command:
        entry: dict[str, Any] = {"type": "local", "command": [server.command, *server.args], "enabled": True}
        if server.env:
            entry["environment"] = dict(server.env)
        return entry
    entry = {"type": "remote", "url": server.url, "enabled": True}
    if server.headers:
        entry["headers"] = dict(server.headers)
    return entry


def _permission(town: Town, resident: str, profile: profiles.Profile) -> dict[str, Any]:
    town.resident_dir(resident)  # validates the id
    level = profile.level
    return {
        "read": "allow",
        # Nothing outside the town (inside it, reading is allowed and editing is ruled below).
        "external_directory": "deny",
        # The town is one git repository, so opencode treats the whole town as the
        # project and matches edits by their path inside it: only its own folder.
        "edit": {"*": "deny", f"residents/{resident}/*": level("files")},
        "bash": level("shell"),
        "webfetch": level("web_fetch"),
        "websearch": "deny",  # web search comes from Babo, with Babo's key, when the profile allows
        "task": level("subagents"),
        # Nobody may be waiting at a terminal: never stop to ask in these ways.
        "question": "deny",
        "doom_loop": "deny",
        "skill": "deny",
    }


def _priority(kind: str) -> str:
    """Where the session's model calls wait in the queue: someone's work first, then visits, then upkeep."""
    return "upkeep" if kind == "upkeep" else "visit" if kind.startswith("visit:") else "interactive"


def opencode_config(town: Town, config: Config, session: Session, *, bundle: Path, config_path: Path, babo: str) -> dict[str, Any]:
    profile = effective_profile(town, config, session.resident, session.overrides, by=session.by)
    model_id = profile.model or config.baton.model.model
    tokens = _context_tokens(config)
    prompt = (files("babo.baton") / "templates" / "opencode" / "resident.md").read_text(encoding="utf-8")
    mcp: dict[str, Any] = {
        "babo": {"type": "local", "enabled": True,
                 "command": [babo, "mcp", "--as", session.resident, "-c", str(config_path)]},
    }
    for name in profile.mcp:
        server = config.tools.mcp_servers.get(name)
        if server is not None:
            mcp[name] = _mcp_entry(server)
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            PROVIDER: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Babo",
                "options": {"baseURL": config.baton.model.endpoint,
                            "headers": {"X-Baton-Resident": session.resident, "X-Baton-Priority": _priority(session.kind)}},
                "models": {model_id: {"name": model_id, "limit": {"context": tokens, "output": min(8192, tokens // 4)}}},
            },
        },
        "model": model_id if "/" in model_id else f"{PROVIDER}/{model_id}",
        "small_model": f"{PROVIDER}/{config.baton.model.model}",
        "default_agent": AGENT,
        "agent": {AGENT: {"mode": "primary", "description": f"The resident {session.resident}", "prompt": prompt}},
        "instructions": [str(bundle)],
        "permission": _permission(town, session.resident, profile),
        "mcp": mcp,
        "autoupdate": False,
        "share": "disabled",
    }


# -- running instances -------------------------------------------------------------

@dataclass
class Running:
    pid: int
    port: int
    password: str
    started: str

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


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
    return httpx.Client(base_url=run.url, auth=("opencode", run.password), timeout=timeout)


def _resident_env(config: Config, resident: str, *, password: str, inline: dict[str, Any]) -> dict[str, str]:
    root = Path(config.baton.opencode.data_dir).expanduser() / resident
    env = dict(os.environ)
    for name in ("config", "data", "cache", "state"):
        (root / name).mkdir(parents=True, exist_ok=True)
        env[f"XDG_{name.upper()}_HOME"] = str(root / name)
    env.update({name: "1" for name in _ENV_OFF})
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(inline)
    env["OPENCODE_SERVER_PASSWORD"] = password
    env.pop("OPENCODE_CONFIG", None)
    env.pop("OPENCODE_CONFIG_DIR", None)
    return env


def write_bundle(town: Town, config: Config, session: Session) -> Path:
    from babo.baton.bundle import build_bundle

    bundle = build_bundle(town, session.resident, max_tokens=config.baton.bundle.max_tokens,
                          search_hits=config.baton.bundle.search_hits,
                          letter_preview_lines=config.baton.bundle.letter_preview_lines,
                          index_path=config.baton.index_file)
    path = _runtime_dir(town, session.resident) / f"{session.id}.bundle.md"
    atomic_write(path, bundle.text())
    return path


def start(town: Town, config: Config, session: Session, *, config_path: Path) -> Running:
    """Start (or find) the opencode instance for a session, and make sure its opencode session exists."""
    with FileLock(str(town.locks / f"opencode-{session.resident}-{session.id}.lock"), timeout=120):
        run = running(town, session)
        if run is None:
            binary = opencode_bin(config)
            if binary is None:
                raise BatonError("opencode is not installed (or baton.opencode.bin is wrong); residents other than the Mayor run in it. "
                                 "Install it with: curl -fsSL https://opencode.ai/install | bash")
            babo = shutil.which("babo") or "babo"
            inline = opencode_config(town, config, session, bundle=write_bundle(town, config, session), config_path=config_path, babo=babo)
            password = secrets.token_urlsafe(18)
            port = _free_port()
            log = (_runtime_dir(town, session.resident) / f"{session.id}.log").open("ab")
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                [binary, "serve", "--port", str(port), "--hostname", "127.0.0.1"],
                cwd=town.resident_dir(session.resident), env=_resident_env(config, session.resident, password=password, inline=inline),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
            run = Running(pid=proc.pid, port=port, password=password, started=now_iso())
            atomic_write(_runtime_path(town, session), json.dumps(asdict(run)) + "\n")
            _wait_healthy(run, proc, config.baton.opencode.start_timeout_s, log_path=Path(log.name))
        if not session.opencode_id:
            with _client(run, 30) as client:
                created = client.post("/session", json={"title": session.title})
                created.raise_for_status()
                session.opencode_id = str(created.json()["id"])
        session.status = "running"
        session.last_active = now_iso()
        save_session(town, session)
        return run


def _wait_healthy(run: Running, proc: subprocess.Popen[bytes], timeout_s: int, *, log_path: Path) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-600:] if log_path.exists() else ""
            raise BatonError(f"opencode stopped while starting (exit {proc.returncode}). {tail.strip()}")
        try:
            with _client(run, 2) as client:
                if client.get("/global/health").status_code == 200:
                    return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    proc.terminate()
    raise BatonError(f"opencode didn't answer within {timeout_s} seconds.")


def freeze(town: Town, session: Session) -> bool:
    """Stop the session's instance. Its opencode session stays, so resuming continues it. True if it was running."""
    run = running(town, session)
    was_running = run is not None
    if run is not None:
        # Ask opencode to close the instance (its sessions are already saved),
        # then stop the process; it can take its time over a plain SIGTERM.
        try:
            with _client(run, 5) as client:
                client.post("/instance/dispose")
        except httpx.HTTPError:
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
    _runtime_path(town, session).unlink(missing_ok=True)
    session.status = "frozen"
    save_session(town, session)
    return was_running


def send(town: Town, config: Config, session: Session, text: str, *, config_path: Path, timeout_s: float = 1800) -> str:
    """Send one message and wait for the resident's reply."""
    run = start(town, config, session, config_path=config_path)
    with _client(run, timeout_s) as client:
        resp = client.post(f"/session/{session.opencode_id}/message", json={"agent": AGENT, "parts": [{"type": "text", "text": text}]})
        resp.raise_for_status()
        data = cast(dict[str, Any], resp.json())
    session.last_active = now_iso()
    save_session(town, session)
    parts = cast(list[dict[str, Any]], data.get("parts") or [])
    return "\n".join(str(p.get("text", "")) for p in parts if p.get("type") == "text").strip()


def special_session(town: Town, config: Config, resident: str, kind: str) -> Session:
    """The one session a resident keeps for visits from someone, or for upkeep."""
    for s in sessions(town, resident):
        if s.kind == kind:
            return s
    title = {"upkeep": f"{resident}: upkeep"}.get(kind, f"{resident}: visits from {kind.split(':', 1)[-1]}")
    return new_session(town, config, resident, title=title, kind=kind)


def one_turn(town: Town, config: Config, resident: str, message: str, *, kind: str, config_path: Path) -> str:
    """One turn in a resident's visit or upkeep session; the instance is frozen again afterwards."""
    session = special_session(town, config, resident, kind)
    try:
        return send(town, config, session, message, config_path=config_path)
    finally:
        freeze(town, session)


def attach_argv(config: Config, session: Session, run: Running) -> tuple[list[str], dict[str, str]]:
    """The command (and environment) that opens the session in opencode's terminal interface."""
    binary = opencode_bin(config) or "opencode"
    env = dict(os.environ)
    env["OPENCODE_SERVER_PASSWORD"] = run.password
    return [binary, "attach", run.url, "--session", session.opencode_id], env


def describe(town: Town, session: Session) -> dict[str, Any]:
    run = running(town, session)
    return {**asdict(session), "status": "running" if run else ("frozen" if session.status != "new" else "new"),
            "url": run.url if run else "", "password": run.password if run else ""}


def overview(town: Town, resident: str) -> list[dict[str, Any]]:
    return [describe(town, s) for s in sessions(town, resident) if s.kind == "work"]


async def resident_turn(config: Config, town: Town, resident: str, message: str, *, kind: str,
                        in_babo: Callable[[], Awaitable[str]]) -> str:
    """One turn of a resident: in opencode when it lives there and opencode is
    installed, otherwise in Babo itself (``in_babo``)."""
    from babo.config.loader import get_config_path

    agent = Registry(town).require_active(resident)
    if runs_in_opencode(agent.office) and opencode_bin(config) is not None:
        return await asyncio.to_thread(one_turn, town, config, resident, message, kind=kind, config_path=get_config_path())
    return await in_babo()
