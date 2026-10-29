"""Town settings: one list, the same in `babo settings` and the web interface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from babo.baton import settings
from babo.baton.config import BatonConfig
from babo.baton.town import BatonError


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"channels": {"telegram": {"token": "${TG}"}}, "baton": {"town": "~/baton-town", "memory": {"idleMinutes": 20}}}))
    return path


def test_update_changes_only_the_named_setting_and_keeps_a_backup(tmp_path: Path) -> None:
    path = _config(tmp_path)
    said = settings.update(path, {"memory.idle_minutes": "30", "night.time": "03:15"})
    data = json.loads(path.read_text())
    assert data["baton"]["memory"]["idleMinutes"] == 30 and data["baton"]["night"]["time"] == "03:15"
    assert data["channels"]["telegram"]["token"] == "${TG}" and data["baton"]["town"] == "~/baton-town"
    assert any("20 → 30" in s for s in said) and any("05:" not in s and "03:15" in s for s in said)
    assert len(list(tmp_path.glob("config.json.before-settings-*"))) == 1
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert settings.update(path, {"memory.idle_minutes": 30}) == []


@pytest.mark.parametrize(("key", "value", "message"), [
    ("night.time", "25:00", "isn't allowed"),
    ("memory.idle_minutes", "0", "isn't allowed"),
    ("memory.idle_minutes", "soon", "whole number"),
    ("model.upstream", "http://x", "Not a town setting"),
])
def test_bad_values_change_nothing(tmp_path: Path, key: str, value: str, message: str) -> None:
    path = _config(tmp_path)
    before = path.read_text()
    with pytest.raises(BatonError, match=message):
        settings.update(path, {key: value})
    assert path.read_text() == before


def test_current_lists_every_setting_with_its_value() -> None:
    rows = settings.current(BatonConfig.model_validate({"memory": {"checkpointEveryToolCalls": 9}}))
    by_key = {r["key"]: r for r in rows}
    assert by_key["memory.checkpoint_every_tool_calls"]["value"] == 9
    assert all(r["label"] and r["help"] and r["applies"] for r in rows)


def test_cli_shows_and_changes_settings(tmp_path: Path) -> None:
    from babo.baton.cli import app

    path = _config(tmp_path)
    runner = CliRunner()
    shown = runner.invoke(app, ["settings", "-c", str(path)])
    assert shown.exit_code == 0 and "memory.idle_minutes" in shown.output
    changed = runner.invoke(app, ["settings", "memory.idle_minutes", "45", "-c", str(path)])
    assert changed.exit_code == 0, changed.output
    assert json.loads(path.read_text())["baton"]["memory"]["idleMinutes"] == 45
    assert runner.invoke(app, ["settings", "night.time", "7pm", "-c", str(path)]).exit_code == 1
