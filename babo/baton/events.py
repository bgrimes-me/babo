"""The event log: the harness's ground truth of what a resident actually did.

One JSON object per line in ``residents/<id>/sessions/YYYY-MM-DD/<session>.events.jsonl``.
The format is part of Baton's external contract (the OpenCode plugin writes
the same records):

``{"v": 1, "at": iso, "resident": id, "session": key, "kind": "tool" | "turn_start" |
"turn_end" | "checkpoint" | "compaction" | "gate", "tool": name, "args": {...redacted},
"files": [paths], "exit_code": int | null, "test": "pass" | "fail" | null,
"commit": bool, "error": bool, "preview": str}``
"""

from __future__ import annotations

import json
import re
import shlex
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from babo.baton.secrets import redact
from babo.baton.town import Town, now_iso, slugify, today

EVENT_VERSION = 1
_EXIT_RE = re.compile(r"Exit code:\s*(-?\d+)")
_TEST_RE = re.compile(
    r"(?:^|[\s;&|(])(?:python3?\s+-m\s+)?(?:pytest|py\.test|unittest|tox|nox|npm\s+(?:run\s+)?test|pnpm\s+test|yarn\s+test|"
    r"bun\s+test|vitest|jest|cargo\s+test|go\s+test|make\s+(?:check|test)|ctest|mvn\s+test|gradle\s+test)\b"
)
_COMMIT_RE = re.compile(r"(?:^|[\s;&|(])git\s+(?:-[cC]\s+\S+\s+)*commit\b")
_PATH_KEYS = ("path", "file_path", "filename", "target", "directory", "dir", "working_dir", "cwd")
_SHELL_TOOLS = ("exec", "exec_session")
_LOCK = threading.Lock()


def session_slug(session_key: str | None) -> str:
    return slugify((session_key or "direct").replace(":", "-"), max_len=60)


def events_path(town: Town, resident_id: str, session_key: str | None, day: str | None = None) -> Path:
    return town.resident_dir(resident_id) / "sessions" / (day or today()) / f"{session_slug(session_key)}.events.jsonl"


def command_of(tool: str, args: dict[str, Any]) -> str:
    if tool in _SHELL_TOOLS:
        for key in ("command", "cmd", "input", "keys"):
            value = args.get(key)
            if isinstance(value, str):
                return value
    return ""


def is_commit_command(command: str) -> bool:
    return bool(_COMMIT_RE.search(command))


def is_test_command(command: str) -> bool:
    return bool(_TEST_RE.search(command))


def exit_code_of(result: Any) -> int | None:
    match = _EXIT_RE.search(str(result)) if result is not None else None
    return int(match.group(1)) if match else None


def files_of(tool: str, args: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            files.append(value)
    paths = args.get("paths")
    if isinstance(paths, list):
        files += [str(p) for p in cast(list[object], paths)]
    command = command_of(tool, args)
    if command:
        try:
            words = shlex.split(command, posix=True)
        except ValueError:
            words = command.split()
        for word in words:
            if "/" in word or re.search(r"\.[A-Za-z0-9]{1,5}$", word):
                if not word.startswith("-") and len(word) < 200:
                    files.append(word)
    seen: list[str] = []
    for f in files:
        if f not in seen:
            seen.append(f)
    return seen[:20]


@dataclass(frozen=True)
class ToolEvent:
    tool: str
    args: dict[str, Any]
    files: list[str]
    exit_code: int | None
    test: str | None
    commit: bool
    error: bool
    preview: str

    @property
    def command(self) -> str:
        return command_of(self.tool, self.args)


def classify(tool: str, args: dict[str, Any], result: Any, *, error: bool) -> ToolEvent:
    command = command_of(tool, args)
    code = exit_code_of(result) if command else None
    failed = error or (code is not None and code != 0)
    test = ("fail" if failed else "pass") if command and is_test_command(command) and (code is not None or error) else None
    commit = bool(command) and is_commit_command(command) and not failed
    preview = str(result if result is not None else "")[:200]
    return ToolEvent(
        tool=tool,
        args=cast(dict[str, Any], redact(dict(args))),
        files=files_of(tool, args),
        exit_code=code,
        test=test,
        commit=commit,
        error=failed,
        preview=cast(str, redact(preview)),
    )


def append_event(town: Town, resident_id: str, session_key: str | None, kind: str, **fields: Any) -> dict[str, Any]:
    record: dict[str, Any] = {"v": EVENT_VERSION, "at": now_iso(), "resident": resident_id, "session": session_key or "direct", "kind": kind}
    record.update(fields)
    path = events_path(town, resident_id, session_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _LOCK, path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return record


def tool_record(event: ToolEvent) -> dict[str, Any]:
    return {
        "tool": event.tool,
        "args": event.args,
        "files": event.files,
        "exit_code": event.exit_code,
        "test": event.test,
        "commit": event.commit,
        "error": event.error,
        "preview": event.preview,
    }


def read_events(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(cast(dict[str, Any], obj))
    return out


def resident_events(town: Town, resident_id: str, day: str | None = None) -> list[dict[str, Any]]:
    root = town.resident_dir(resident_id) / "sessions"
    days = [root / day] if day else sorted(p for p in root.glob("*") if p.is_dir())
    out: list[dict[str, Any]] = []
    for d in days:
        for path in sorted(d.glob("*.events.jsonl")):
            out += read_events(path)
    out.sort(key=lambda r: str(r.get("at", "")))
    return out
