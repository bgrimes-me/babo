"""Fixtures for Baton tests: a founded town, a mock llama-server, resident loops."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from baton.mock_llama import MockLlama

# Baton targets Linux only (Squid's house has no Windows). Upstream's Windows CI
# jobs still run babo's own suite; they just don't collect Baton's.
if sys.platform == "win32":
    collect_ignore_glob = ["test_*.py"]
from babo.baton.founding import found_town
from babo.baton.tools.core import Toolbox
from babo.baton.town import Town


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The town repo must not depend on (or sign with) the developer's git config."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def town(tmp_path: Path) -> Town:
    # Most tests need the Archivist and the Inspector too; a new town has only the Mayor.
    t, _ = found_town(tmp_path / "town", offices=("mayor", "archivist", "inspector"))
    return t


@pytest.fixture
def index_path(tmp_path: Path) -> Path:
    return tmp_path / "index" / "index.sqlite3"


@pytest.fixture
def box(town: Town, index_path: Path):
    def make(resident_id: str) -> Toolbox:
        return Toolbox(town, resident_id, index_path)

    return make


@pytest.fixture
def mock_llama():
    server = MockLlama().start()
    try:
        yield server
    finally:
        server.stop()


def config_dict(town: Town, resident: str, *, api_base: str, index_path: Path, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "agents": {"defaults": {"workspace": str(town.resident_dir(resident)), "modelPreset": "local"}},
        "providers": {"custom": {"apiBase": api_base, "apiKey": "test"}},
        "modelPresets": {"local": {"provider": "custom", "model": "mock-model", "contextWindowTokens": 32768}},
        "tools": {"restrictToWorkspace": True},
        "baton": {
            "town": str(town.root),
            "indexPath": str(index_path),
            "model": {"endpoint": api_base, "model": "mock-model"},
            # Residents run in Babo itself unless a test gives them opencode (tests/baton/test_opencode.py).
            "opencode": {"bin": "/nonexistent/opencode"},
        },
    }
    for dotted, value in overrides.items():
        node = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return data


@pytest.fixture
def make_config(town: Town, index_path: Path, mock_llama: MockLlama):
    from babo.config.schema import Config

    def make(resident: str = "mayor", **overrides: Any) -> Config:
        return Config.model_validate(config_dict(town, resident, api_base=mock_llama.v1, index_path=index_path, **overrides))

    return make


@pytest.fixture
def make_loop(make_config):
    from babo.agent.loop import AgentLoop
    from babo.agent.tools.registry import ToolRegistry

    def make(resident: str = "mayor", **overrides: Any) -> AgentLoop:
        return AgentLoop.from_config(make_config(resident, **overrides), tool_registry=ToolRegistry())

    return make
