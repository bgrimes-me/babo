"""`babo reset`: everything is backed up to one archive, then a fresh install can start."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from babo.baton import reset as resetting
from babo.baton.cli import app
from babo.baton.founding import found_town
from babo.baton.town import BatonError


def _install(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "babo-home"
    town, _ = found_town(tmp_path / "town", owner="Robin")
    data = tmp_path / "opencode-data"
    (data / "archivist" / "data").mkdir(parents=True)
    (home / "sessions").mkdir(parents=True)
    (home / "sessions" / "chat.sqlite3").write_text("the Mayor's chats")
    config = home / "config.json"
    config.write_text(json.dumps({"baton": {"town": str(town.root), "opencode": {"dataDir": str(data)}},
                                  "providers": {"custom": {"apiKey": "sk-test"}}}))
    return config, town.root, data


def test_reset_backs_everything_up_and_can_keep_the_config(tmp_path: Path) -> None:
    config, town, data = _install(tmp_path)
    runner = CliRunner()
    declined = runner.invoke(app, ["reset", "-c", str(config), "--backup-dir", str(tmp_path / "backups")], input="n\n")
    assert "Nothing changed." in declined.output and town.exists() and (config.parent / "sessions").exists()

    done = runner.invoke(app, ["reset", "--keep-config", "--yes", "-c", str(config), "--backup-dir", str(tmp_path / "backups")])
    assert done.exit_code == 0, done.output
    assert not town.exists() and not data.exists() and not (config.parent / "sessions").exists()
    assert json.loads(config.read_text())["providers"]["custom"]["apiKey"] == "sk-test"  # kept: models and keys
    [archive] = (tmp_path / "backups").glob("babo-*.tar.gz")
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "town/CHARTER.md" in names and "babo-home/sessions/chat.sqlite3" in names and "opencode-data/archivist/data" in names
    # A fresh town can be founded where the old one was.
    assert found_town(town)[0].exists


def test_reset_refuses_to_take_the_whole_home_folder(tmp_path: Path) -> None:
    with pytest.raises(BatonError, match="whole home folder"):
        resetting.plan(home=tmp_path / ".babo", town=Path.home(), opencode_data=tmp_path / "data", keep_config=False)
