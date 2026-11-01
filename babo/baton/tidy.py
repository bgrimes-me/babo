"""``babo tidy``: remove what earlier versions of Babo left behind.

Run by the installer at every install and upgrade (step 04), and safe to run
any time. It only removes things Babo itself made and no longer uses, says what
it removed, and never touches the town's history or anyone's work:

- settings that no longer mean anything (``baton.night.time``, an install's copy
  of the model name in ``baton.model.model``) and names that
  changed (the crew tool ``spawn`` is now ``subagent``; the Mayor's
  ``request_session`` is now ``send_errand``) in Babo's config;
- paths through ``~/.nanobot`` (the folder's name before the rename) in Babo's
  configs, and then the ``~/.nanobot`` link itself;
- old config backups beyond the newest few;
- folders in the Babo checkout that only hold Python's cache of code that no
  longer exists (such as ``nanobot/`` after the rename).

Kept on purpose: the town's own history (old night ledgers and logs are part
of its git history), residents' files, and anything Babo didn't make.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

KEEP_BACKUPS = 3
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "out", "reports"}


def _rewrite(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("/.nanobot/", "/.babo/").replace("~/.nanobot", "~/.babo")
    if isinstance(value, list):
        return [_rewrite(v) for v in cast(list[Any], value)]
    if isinstance(value, dict):
        return {k: _rewrite(v) for k, v in cast(dict[str, Any], value).items()}
    return value


def _tidy_config(path: Path, said: list[str]) -> None:
    try:
        data = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return
    before = json.dumps(data, sort_keys=True)
    data = cast(dict[str, Any], _rewrite(data))
    baton = data.get("baton")
    if isinstance(baton, dict):
        baton = cast(dict[str, Any], baton)
        night = baton.get("night")
        if isinstance(night, dict) and "time" in night:
            del cast(dict[str, Any], night)["time"]
            said.append(f"{path}: removed baton.night.time (upkeep replaced the nightly timer)")
            if not night:
                del baton["night"]
        model = baton.get("model")
        presets = cast(dict[str, Any], data.get("modelPresets")) if isinstance(data.get("modelPresets"), dict) else {}
        if isinstance(model, dict) and cast(dict[str, Any], model).get("model") and cast(dict[str, Any], model)["model"] not in presets:
            stale = cast(dict[str, Any], model).pop("model")
            said.append(f"{path}: removed baton.model.model ({stale}); Babo's own memory work now uses the default model preset from Settings › Models")
        toolsets = baton.get("toolsets")
        if isinstance(toolsets, dict):
            for office, names in cast(dict[str, Any], toolsets).items():
                if isinstance(names, list) and "spawn" in names:
                    cast(dict[str, Any], toolsets)[office] = ["subagent" if n == "spawn" else n for n in cast(list[Any], names)]
                    said.append(f"{path}: the {office} toolset's 'spawn' is now 'subagent'")
                names = cast(dict[str, Any], toolsets)[office]
                if isinstance(names, list) and "request_session" in names:
                    cast(dict[str, Any], toolsets)[office] = ["send_errand" if n == "request_session" else n for n in cast(list[Any], names)]
                    said.append(f"{path}: the {office} toolset's 'request_session' is now 'send_errand'")
    if json.dumps(data, sort_keys=True) != before:
        if ".nanobot" in before and ".nanobot" not in json.dumps(data):
            said.append(f"{path}: paths through ~/.nanobot now point to ~/.babo")
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)


def _prune_backups(folder: Path, said: list[str]) -> None:
    for pattern in ("config.json.before-baton-*", "config.json.before-settings-*"):
        backups = sorted(folder.glob(pattern), key=lambda p: p.name, reverse=True)
        for old in backups[KEEP_BACKUPS:]:
            old.unlink()
            said.append(f"removed an old config backup: {old.name}")


def _only_cache(folder: Path) -> bool:
    files = [p for p in folder.rglob("*") if p.is_file()]
    return all(p.suffix in (".pyc", ".pyo") for p in files)


def _tidy_checkout(checkout: Path, said: list[str]) -> None:
    try:
        tracked = subprocess.run(["git", "-C", str(checkout), "ls-files"], capture_output=True, text=True, check=True).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return
    keep = {checkout}
    for name in tracked:
        parent = (checkout / name).parent
        while parent not in keep and parent != checkout.parent:
            keep.add(parent)
            parent = parent.parent
    candidates = [p for p in checkout.rglob("*") if p.is_dir() and not (_SKIP_DIRS & set(p.relative_to(checkout).parts))]
    for folder in sorted(candidates, key=lambda p: len(p.parts)):
        if not folder.exists() or folder in keep or folder.name == "__pycache__" and folder.parent in keep:
            continue
        if _only_cache(folder):
            shutil.rmtree(folder)
            said.append(f"removed {folder.relative_to(checkout)}/ (only Python's cache of code that no longer exists)")


def tidy(*, babo_home: Path, user_home: Path, checkout: Path | None) -> list[str]:
    """Remove what earlier versions left behind. Returns what was removed, in words."""
    said: list[str] = []
    configs = [babo_home / "config.json", *sorted((babo_home / "baton").glob("*/config.json"))]
    for path in configs:
        if path.is_file():
            _tidy_config(path, said)
    if babo_home.is_dir():
        _prune_backups(babo_home, said)
    link = user_home / ".nanobot"
    if link.is_symlink() and link.resolve() == babo_home.resolve():
        still = [p.name for p in configs if p.is_file() and ".nanobot" in p.read_text(encoding="utf-8")]
        if not still:
            link.unlink()
            said.append("removed the ~/.nanobot link left by the rename (nothing uses it any more)")
    if checkout is not None and (checkout / ".git").exists():
        _tidy_checkout(checkout, said)
    return said
