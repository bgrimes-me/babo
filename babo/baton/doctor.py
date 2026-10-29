"""``babo doctor``: does the whole town work, end to end?

Prints a pass/fail table and ends with ``OK:`` or ``FAIL:``. It never writes
into a resident's memory: the checkpoint check asks for a checkpoint and
validates it without filing it, and the compaction check backs up and
verifies a throwaway session in a temporary directory.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx

from babo.baton.model_client import BatonModelClient, ModelUnavailableError

if TYPE_CHECKING:
    from babo.config.schema import Config


@dataclass
class Check:
    name: str
    status: str  # PASS | WARN | FAIL
    detail: str


def _base(url: str) -> str:
    url = url.rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


async def _get(url: str) -> tuple[int | None, str]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
        return resp.status_code, resp.text[:300]
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"


async def check_endpoint(cfg: Config) -> Check:
    status, body = await _get(_base(cfg.baton.model.upstream) + "/health")
    return Check("endpoint (llama-server)", "PASS" if status == 200 else "FAIL", f"{cfg.baton.model.upstream} -> {status or body}")


async def check_queue(cfg: Config) -> Check:
    status, body = await _get(_base(cfg.baton.model.endpoint) + "/baton/queue")
    if status != 200:
        return Check("model queue", "FAIL", f"{cfg.baton.model.endpoint}: {status or body} (is babo-queue.service running?)")
    return Check("model queue", "PASS", f"{cfg.baton.model.endpoint} {body}")


async def check_tool_calling(cfg: Config) -> Check:
    client = BatonModelClient(cfg.baton.model, resident="doctor")
    body = {
        "model": cfg.baton.model.model,
        "messages": [{"role": "user", "content": "What is the weather in Oslo? Use the tool."}],
        "tools": [{"type": "function", "function": {"name": "get_weather", "description": "Weather for a city.",
                   "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}],
        "temperature": 0, "max_tokens": 200,
    }
    try:
        data = await client.chat(body)
    except (ModelUnavailableError, httpx.HTTPError) as exc:
        return Check("tool calling", "FAIL", str(exc)[:200])
    choices = cast(list[dict[str, Any]], data.get("choices") or [{}])
    message = cast(dict[str, Any], choices[0].get("message") or {})
    calls = cast(list[dict[str, Any]], message.get("tool_calls") or [])
    function = cast(dict[str, Any], calls[0].get("function") or {}) if calls else {}
    ok = function.get("name") == "get_weather"
    return Check("tool calling", "PASS" if ok else "FAIL", f"{len(calls)} tool call(s)" + ("" if ok else " (llama-server needs --jinja)"))


async def check_schema(cfg: Config) -> Check:
    client = BatonModelClient(cfg.baton.model, resident="doctor")
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}, "word": {"type": "string"}}, "required": ["ok", "word"], "additionalProperties": False}
    try:
        result = await client.chat_json([{"role": "user", "content": "Reply with ok=true and any word."}], schema, max_tokens=60)
    except ModelUnavailableError as exc:
        return Check("schema output", "FAIL", str(exc)[:200])
    data: object = result.data
    ok = isinstance(data, dict) and "ok" in cast(dict[str, Any], data)
    return Check("schema output", "PASS" if ok else "FAIL", result.raw[:120] or result.error)


async def check_checkpoint_turn(cfg: Config) -> Check:
    from babo.baton.checkpoint import CheckpointService
    from babo.baton.town import Town

    town = Town.open(cfg.baton.town_path)
    service = CheckpointService(town, "mayor", cfg.baton)
    messages = [
        {"role": "system", "content": "You are a resident of a small town of agents."},
        {"role": "user", "content": "Please tidy the notes about the garden."},
        {"role": "assistant", "content": "I tidied the garden notes and merged two duplicates."},
    ]
    reply, attempts, error, _ = await service.ask(messages, "doctor")
    if reply is None:
        return Check("checkpoint turn", "FAIL", f"no valid checkpoint after {attempts} attempt(s): {error} (a mechanical checkpoint would be written)")
    return Check("checkpoint turn", "PASS", f"valid checkpoint in {attempts} attempt(s), thread '{reply.thread}' (not filed)")


async def check_mcp(cfg: Config, config_path: Path | None) -> Check:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    args = ["-m", "babo", "mcp", "--as", "mayor"] + (["-c", str(config_path)] if config_path else [])
    try:
        params = StdioServerParameters(command=sys.executable, args=args)
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
    except Exception as exc:
        return Check("MCP (baton-mcp)", "FAIL", f"{type(exc).__name__}: {exc}"[:200])
    ok = {"search_memory", "read_note"} <= names
    return Check("MCP (baton-mcp)", "PASS" if ok else "FAIL", f"{len(names)} tools for mayor")


def check_index(cfg: Config) -> Check:
    from babo.baton.search import open_index, reindex
    from babo.baton.town import Town

    try:
        town = Town.open(cfg.baton.town_path)
        count = reindex(town, cfg.baton.index_file)
        open_index(cfg.baton.index_file).search("town", owners=("commons", "mayor"), k=1)
    except Exception as exc:
        return Check("FTS index", "FAIL", f"{type(exc).__name__}: {exc}")
    return Check("FTS index", "PASS", f"{count} notes indexed at {cfg.baton.index_file}")


def check_residents(cfg: Config) -> Check:
    from babo.baton.registry import Registry
    from babo.baton.town import BatonError, Town

    try:
        town = Town.open(cfg.baton.town_path)
        registry = Registry(town)
        for rid in ("mayor", "archivist", "inspector"):
            registry.require_active(rid)
        residents = [r.display for r in registry.all() if r.status == "active"]
    except BatonError as exc:
        return Check("residents present", "FAIL", str(exc))
    return Check("residents present", "PASS", ", ".join(residents))


def check_backup() -> Check:
    from babo.baton.compaction import read_backup, write_backup

    with tempfile.TemporaryDirectory() as tmp:
        try:
            path = write_backup(Path(tmp), "doctor:check", [{"role": "user", "content": "hello"}])
            header, messages = read_backup(path)
        except Exception as exc:
            return Check("compaction backup", "FAIL", f"{type(exc).__name__}: {exc}")
    ok = header.get("session_key") == "doctor:check" and len(messages) == 1
    return Check("compaction backup", "PASS" if ok else "FAIL", "hashed backup written and verified")


def check_timer() -> Check:
    cmd = ["systemctl", "--user", "is-enabled", "babo-night.timer"]
    if shutil.which("distrobox-host-exec"):
        cmd = ["distrobox-host-exec", *cmd]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("night timer", "WARN", f"could not ask systemd: {exc}")
    state = (result.stdout or result.stderr).strip()
    return Check("night timer", "PASS" if state == "enabled" else "FAIL", state or "unknown")


def check_git(cfg: Config) -> Check:
    from babo.baton import gitops
    from babo.baton.town import BatonError, Town

    try:
        town = Town.open(cfg.baton.town_path)
        log = gitops.git(town, "log", "--oneline", "-1").stdout.strip()
    except BatonError as exc:
        return Check("git", "FAIL", str(exc))
    remote = f", pushes to {cfg.baton.git_remote}" if cfg.baton.git_remote else ", no remote (fine)"
    return Check("git", "PASS" if log else "FAIL", f"last commit: {log or 'none'}{remote}")


def check_config(cfg: Config) -> list[Check]:
    out = [
        Check("Dream off", "PASS" if not cfg.agents.defaults.dream.enabled else "FAIL", "residents author their own memory"),
        Check("file privacy", "PASS" if cfg.tools.restrict_to_workspace else "FAIL", "tools.restrictToWorkspace"),
    ]
    sandbox = cfg.tools.exec.sandbox
    out.append(Check("shell privacy", "PASS" if sandbox else "WARN",
                     f"sandbox: {sandbox}" if sandbox else "no bwrap sandbox: privacy inside the shell is best-effort"))
    return out


async def run_doctor(cfg: Config, *, config_path: Path | None = None, include_timer: bool = True) -> list[Check]:
    checks: list[Check] = [await check_endpoint(cfg), await check_queue(cfg)]
    model_ok = checks[0].status == "PASS" and checks[1].status == "PASS"
    if model_ok:
        checks += [await check_tool_calling(cfg), await check_schema(cfg), await check_checkpoint_turn(cfg)]
    else:
        checks += [Check(n, "FAIL", "skipped: the model isn't reachable") for n in ("tool calling", "schema output", "checkpoint turn")]
    checks += [await check_mcp(cfg, config_path), check_index(cfg), check_residents(cfg), check_backup()]
    if include_timer:
        checks.append(check_timer())
    checks.append(check_git(cfg))
    checks += check_config(cfg)
    return checks
