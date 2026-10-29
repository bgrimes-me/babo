"""`babo tidy`: what earlier versions left behind is removed, and nothing else."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from babo.baton.config import BatonConfig
from babo.baton.tidy import tidy
from babo.baton.units import write_units


def test_obsolete_settings_old_paths_and_the_rename_link_go(tmp_path: Path) -> None:
    home = tmp_path / "home"
    babo = home / ".babo"
    (babo / "baton" / "mayor").mkdir(parents=True)
    (home / ".nanobot").symlink_to(".babo")
    config = {"channels": {"telegram": {"token": "${TG}"}},
              "baton": {"generatedDir": f"{home}/.nanobot/baton", "night": {"time": "04:00", "toolTestTimeoutS": 60},
                        "toolsets": {"mayor": ["read_file", "spawn"]}}}
    (babo / "config.json").write_text(json.dumps(config))
    (babo / "baton" / "mayor" / "config.json").write_text(json.dumps({"agents": {"defaults": {"workspace": "~/.nanobot/w"}}}))
    for n in range(5):
        (babo / f"config.json.before-settings-2026010{n}-000000").write_text("{}")

    said = tidy(babo_home=babo, user_home=home, checkout=None)
    data = json.loads((babo / "config.json").read_text())
    assert data["baton"]["generatedDir"] == f"{home}/.babo/baton"
    assert data["baton"]["night"] == {"toolTestTimeoutS": 60} and data["baton"]["toolsets"]["mayor"] == ["read_file", "subagent"]
    assert data["channels"]["telegram"]["token"] == "${TG}"
    assert json.loads((babo / "baton" / "mayor" / "config.json").read_text())["agents"]["defaults"]["workspace"] == "~/.babo/w"
    assert not (home / ".nanobot").exists() and (babo / "config.json").exists()
    assert sorted(p.name for p in babo.glob("config.json.before-settings-*"))[0].endswith("20260102-000000")
    assert any("~/.nanobot link" in line for line in said)
    assert tidy(babo_home=babo, user_home=home, checkout=None) == []  # nothing left the second time


def test_stale_folders_in_the_checkout_go_but_work_stays(tmp_path: Path) -> None:
    repo = tmp_path / "babo"
    (repo / "babo").mkdir(parents=True)
    (repo / "babo" / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    (repo / "nanobot" / "agent" / "__pycache__").mkdir(parents=True)
    (repo / "nanobot" / "agent" / "__pycache__" / "loop.cpython-312.pyc").write_bytes(b"x")
    (repo / "babo" / "__pycache__").mkdir()
    (repo / "babo" / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"x")
    (repo / "notes").mkdir()
    (repo / "notes" / "mine.md").write_text("Squid's own notes")
    said = tidy(babo_home=tmp_path / "none", user_home=tmp_path, checkout=repo)
    assert not (repo / "nanobot").exists() and said == ["removed nanobot/ (only Python's cache of code that no longer exists)"]
    assert (repo / "babo" / "__pycache__").exists() and (repo / "notes" / "mine.md").exists()


def test_units_from_earlier_versions_are_not_left_for_the_host_step(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "babo-night.timer").write_text("old")
    (out / "babo-night.service").write_text("old")
    write_units(BatonConfig(), out)
    assert sorted(p.name for p in out.glob("babo-*")) == ["babo-gateway.service", "babo-queue.service", "babo-upkeep.service"]


def test_the_installer_records_where_opencode_is(tmp_path: Path) -> None:
    from babo.baton.setup import town_config

    data = town_config({}, town="~/t", upstream="http://x", model="m", n_ctx=8192, opencode_path="/home/ben/.opencode/bin/opencode")
    assert data["baton"]["opencode"]["bin"] == "/home/ben/.opencode/bin/opencode"
