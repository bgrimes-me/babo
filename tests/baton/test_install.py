"""Phase 5: init-config, doctor, and the install scripts."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from babo.baton.config import BatonModelConfig
from babo.baton.doctor import run_doctor
from babo.baton.queue import ModelQueue
from babo.baton.setup import town_config, write_town_config
from babo.baton.town import Town
from baton.mock_llama import MockLlama, schema, text, tool_call

REPO = Path(__file__).resolve().parents[2]
CHECKPOINT = {
    "thread": "garden", "doing": "tidying notes", "done": ["merged two notes"], "learned": [], "next": [],
    "blockers": [], "confidence": 0.9, "knowledge_ops": [], "promote": [],
}


def test_town_config_keeps_squids_settings(tmp_path: Path) -> None:
    existing = {"channels": {"telegram": {"enabled": True, "token": "${TG}"}}, "agents": {"defaults": {"timezone": "Europe/Oslo"}}}
    data = town_config(existing, town="~/baton-town", upstream="http://127.0.0.1:8080/", model="qwen", n_ctx=32768, bwrap=True)
    assert data["channels"]["telegram"]["token"] == "${TG}"
    assert data["agents"]["defaults"]["timezone"] == "Europe/Oslo"
    assert data["agents"]["defaults"]["dream"]["enabled"] is False
    assert data["agents"]["defaults"]["workspace"].endswith("baton-town/residents/mayor")
    assert data["tools"]["restrictToWorkspace"] is True and data["tools"]["exec"]["sandbox"] == "bwrap"
    assert data["providers"]["custom"]["apiBase"] == "http://127.0.0.1:8091/v1"
    assert data["baton"]["model"]["upstream"] == "http://127.0.0.1:8080"
    from babo.config.schema import Config

    cfg = Config.model_validate(data)
    assert cfg.resolve_preset().model == "qwen"


def test_write_town_config_backs_up_and_is_repeatable(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"api": {"timeout": 99}}))
    _, backup = write_town_config(path, town=str(tmp_path / "town"), upstream="http://x:1", model="m", n_ctx=8192)
    assert backup is not None and json.loads(backup.read_text()) == {"api": {"timeout": 99}}
    first = path.read_text()
    write_town_config(path, town=str(tmp_path / "town"), upstream="http://x:1", model="m", n_ctx=8192)
    assert path.read_text() == first
    assert oct(path.stat().st_mode)[-3:] == "600"


def _doctor_router(body: dict[str, Any]) -> dict[str, Any]:
    if body.get("tools"):
        return tool_call("get_weather", {"city": "Oslo"})
    if "response_format" in body:
        name = body["response_format"]["json_schema"]["name"]
        return schema(CHECKPOINT) if name == "checkpoint" else schema({"ok": True, "word": "fine"})
    return text("ok")


async def test_doctor_passes_on_a_working_town(town: Town, index_path: Path, mock_llama: MockLlama, tmp_path: Path) -> None:
    mock_llama.default = _doctor_router
    queue = ModelQueue(BatonModelConfig(upstream=mock_llama.base_url))
    runner = web.AppRunner(queue.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        path = tmp_path / "config.json"
        data = town_config({}, town=str(town.root), upstream=mock_llama.base_url, model="mock-model", n_ctx=32768, queue_port=port)
        data["baton"]["indexPath"] = str(index_path)
        path.write_text(json.dumps(data))
        from babo.config.loader import load_config

        cfg = load_config(path)
        checks = await run_doctor(cfg, config_path=path, include_timer=False)
    finally:
        await runner.cleanup()
    by_name = {c.name: c for c in checks}
    failures = {c.name: c.detail for c in checks if c.status == "FAIL"}
    assert failures == {}
    for name in ("endpoint (llama-server)", "model queue", "tool calling", "schema output", "checkpoint turn",
                 "MCP (baton-mcp)", "FTS index", "residents present", "compaction backup", "git"):
        assert by_name[name].status == "PASS", name
    assert by_name["shell privacy"].status == "WARN"  # no bwrap in this config
    # The doctor never writes into a resident's memory.
    assert not list((town.resident_dir("mayor") / "checkpoints").glob("*.jsonl"))


async def test_doctor_fails_clearly_when_the_model_is_down(town: Town, index_path: Path, tmp_path: Path) -> None:
    from babo.config.schema import Config

    data = town_config({}, town=str(town.root), upstream="http://127.0.0.1:9", model="m", n_ctx=4096, queue_port=9)
    data["baton"]["indexPath"] = str(index_path)
    checks = await run_doctor(Config.model_validate(data), include_timer=False)
    failed = {c.name for c in checks if c.status == "FAIL"}
    assert {"endpoint (llama-server)", "model queue", "tool calling"} <= failed
    assert "residents present" not in failed


@pytest.mark.parametrize("script", sorted(p.name for p in (REPO / "install").glob("*.sh")))
def test_install_scripts_are_strict_bash(script: str) -> None:
    path = REPO / "install" / script
    text_ = path.read_text()
    assert text_.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text_
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
    if script[:2].isdigit() or script.startswith("squid-"):
        # Every step ends with OK: or FAIL: (directly, or through lib.sh's ok/fail).
        uses_lib = 'source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"' in text_
        assert ("OK:" in text_ and "FAIL:" in text_) or (uses_lib and re.search(r"^\s*ok ", text_, re.M) is not None and "fail " in text_)
