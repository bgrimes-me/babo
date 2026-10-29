"""Git operations on the town repo, serialized by a town-wide file lock."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock

from babo.baton.town import BatonError, Town

_ENV_DEFAULTS = {
    "GIT_AUTHOR_NAME": "Baton",
    "GIT_AUTHOR_EMAIL": "baton@town.local",
    "GIT_COMMITTER_NAME": "Baton",
    "GIT_COMMITTER_EMAIL": "baton@town.local",
}


def _git_env(author: tuple[str, str] | None) -> dict[str, str]:
    env = dict(os.environ)
    for key, value in _ENV_DEFAULTS.items():
        env.setdefault(key, value)
    if author:
        env["GIT_AUTHOR_NAME"], env["GIT_AUTHOR_EMAIL"] = author
    return env


def git(town: Town, *args: str, check: bool = True, author: tuple[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    # The town repo never signs commits: it is a local record, and signing
    # prompts would hang unattended night runs.
    cmd = ["git", "-c", "commit.gpgsign=false", "-c", "core.quotepath=off", "-C", str(town.root), *args]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=_git_env(author), timeout=120)
    except FileNotFoundError as exc:
        raise BatonError("git is not installed in this environment") from exc
    if check and result.returncode != 0:
        raise BatonError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


@contextmanager
def town_lock(town: Town, timeout: float = 120):
    lock = FileLock(str(town.locks / "town-git.lock"), timeout=timeout)
    with lock:
        yield


def ensure_repo(town: Town) -> None:
    if (town.root / ".git").exists():
        return
    git(town, "init", "-q", "-b", "main")


def commit_paths(
    town: Town,
    paths: Iterable[Path],
    message: str,
    *,
    author: tuple[str, str] | None = None,
) -> bool:
    """Commit exactly ``paths`` (relative or absolute inside the town). Returns
    False when there was nothing to commit."""
    rel = [str(Path(p).resolve().relative_to(town.root)) if Path(p).is_absolute() else str(p) for p in paths]
    with town_lock(town):
        git(town, "add", "-A", "--", *rel)
        staged = git(town, "diff", "--cached", "--quiet", "--", *rel, check=False)
        if staged.returncode == 0:
            return False
        git(town, "commit", "-q", "-m", message, "--", *rel, author=author)
    return True


def commit_all(town: Town, message: str) -> bool:
    with town_lock(town):
        git(town, "add", "-A")
        staged = git(town, "diff", "--cached", "--quiet", check=False)
        if staged.returncode == 0:
            return False
        git(town, "commit", "-q", "-m", message)
    return True
