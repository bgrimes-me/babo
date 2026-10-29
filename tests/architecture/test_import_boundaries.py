"""Regression coverage for the executable Python import boundaries."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_import_boundaries import Violation, find_violations


@pytest.mark.parametrize("module", [
    "babo.agent.loop", "babo.session.state", "babo.runtime_admission",
    "babo.data_access", "babo.installation_lock",
])
def test_runtime_owners_do_not_depend_on_update_policy(module: str) -> None:
    assert find_violations("from babo.update_service import UpdateService", module) == [
        Violation("update-policy-direction", "babo.update_service.UpdateService", 1),
    ]


def test_updater_uses_process_contracts_not_agent_or_client_implementations() -> None:
    assert find_violations("from babo.agent.loop import AgentLoop", "babo.update_stop") == [
        Violation("update-domain", "babo.agent.loop.AgentLoop", 1),
    ]
    assert find_violations(
        "from babo.gateway.runtime import GatewayRuntime", "babo.update_stop",
    ) == []
    assert find_violations(
        "from babo.update_stop import watch_gateway_stop", "babo.cli.gateway_runtime",
    ) == []
    assert find_violations(
        "from babo.cli.gateway_runtime import _run_gateway", "babo.update_restart",
    ) == []
    assert find_violations(
        "from babo.agent.loop import AgentLoop", "babo.update_restart",
    ) == [Violation("update-restart-adapter", "babo.agent.loop.AgentLoop", 1)]


@pytest.mark.parametrize(
    ("source", "target", "line"),
    [
        ("import babo.webui.metadata", "babo.webui.metadata", 1),
        ("from babo import webui", "babo.webui", 1),
        ("from babo.webui import metadata", "babo.webui.metadata", 1),
        ("from .. import webui", "babo.webui", 1),
        ("from ..webui import metadata", "babo.webui.metadata", 1),
        (
            "def load():\n    from babo.cli import gateway",
            "babo.cli.gateway", 2,
        ),
        (
            "import importlib\nimportlib.import_module('babo.webui.metadata')",
            "babo.webui.metadata", 2,
        ),
        (
            "import importlib as modules\n"
            "modules.import_module(name='babo.webui.metadata')",
            "babo.webui.metadata", 2,
        ),
        (
            "from importlib import import_module as load\n"
            "load('..webui.metadata', __package__)",
            "babo.webui.metadata", 2,
        ),
        (
            "from importlib import import_module\n"
            "import_module('..webui.metadata', package='babo.agent')",
            "babo.webui.metadata", 2,
        ),
        ("__import__('babo.webui.metadata')", "babo.webui.metadata", 1),
        (
            "import builtins as imports\nimports.__import__('babo.webui.metadata')",
            "babo.webui.metadata", 2,
        ),
    ],
)
def test_core_rejects_adapter_imports_in_supported_spellings(
    source: str, target: str, line: int,
) -> None:
    assert find_violations(source, "babo.agent.loop") == [
        Violation("core-execution", target, line),
    ]


@pytest.mark.parametrize(
    "source",
    [
        "from typing import TYPE_CHECKING as TC\n"
        "if TC:\n    from babo.webui import metadata",
        "import typing as t\n"
        "if t.TYPE_CHECKING:\n    from babo.webui import metadata",
    ],
)
def test_core_boundary_includes_type_dependencies(source: str) -> None:
    assert find_violations(source, "babo.agent.runner") == [
        Violation("core-execution", "babo.webui.metadata", 3),
    ]


@pytest.mark.parametrize(
    ("module", "rule"),
    [
        ("babo.session.manager", "session-storage"),
        ("babo.session.recovery", "session-storage"),
        ("babo.security.workspace_policy", "security-primitives"),
        ("babo.agent.turn_delivery", "core-execution"),
        ("babo.channels.notification_routes", "channel-routing-contracts"),
        ("babo.config.schema", "configuration-contracts"),
        ("babo.agent.tools.cli_apps_config", "tool-configuration-contracts"),
        ("babo.utils.file_io", "shared-primitives"),
    ],
)
def test_storage_and_security_do_not_depend_on_adapters(module: str, rule: str) -> None:
    assert find_violations("from babo.cli import gateway", module) == [
        Violation(rule, "babo.cli.gateway", 1),
    ]


@pytest.mark.parametrize(
    "dependency",
    [
        "babo.bus.outbound_events.ProgressEvent",
        "babo.command.router",
        "babo.pairing.events.PairingChanged",
        "babo.session.webui_turns.clear_websocket_turn_if_current",
        "babo.webui.transcript.WebUITranscriptRecorder",
        "babo.webui.outbound_wire.project_notification",
    ],
)
def test_websocket_transport_leaves_event_and_history_policy_to_delivery(dependency: str) -> None:
    module, name = dependency.rsplit(".", 1)
    source = f"from {module} import {name}\n"
    assert find_violations(source, "babo.channels.websocket.runtime") == [
        Violation("websocket-transport", dependency, 1),
    ]
    assert find_violations(source, "babo.webui.outbound_delivery") == []


@pytest.mark.parametrize(
    "source",
    [
        "from babo.config.schema import Base",
        "from ...config.schema import Base as ConfigBase",
    ],
)
def test_tool_configs_import_the_shared_base_directly(source: str) -> None:
    assert find_violations(source, "babo.agent.tools.shell") == [
        Violation("tool-config-base", "babo.config.schema.Base", 1),
    ]
    assert find_violations(
        "from babo.config_base import Base", "babo.agent.tools.shell",
    ) == []


def test_configuration_imports_the_six_tool_owned_config_contracts() -> None:
    source = (
        "from babo.agent.tools.cli_apps_config import CliAppsToolConfig\n"
        "from babo.agent.tools.filesystem_config import FileToolsConfig\n"
        "from babo.agent.tools.image_generation_config import ImageGenerationToolConfig\n"
        "from babo.agent.tools.self_config import MyToolConfig\n"
        "from babo.agent.tools.shell_config import ExecToolConfig\n"
        "from babo.agent.tools.web_config import WebToolsConfig\n"
    )
    assert find_violations(source, "babo.config.schema") == []


@pytest.mark.parametrize(
    ("module", "source"),
    [
        ("babo.agent.loop", "from babo.session.recovery import RecoveryAdmission"),
        ("babo.session.manager", "from babo.providers.base import ProviderConversationState"),
        ("babo.session.webui_turns", "from babo.webui import transcript"),
        ("babo.cli.gateway_runtime", "from babo.agent.loop import AgentLoop"),
        ("babo.channels.websocket.runtime", "from babo.webui import inbound_commands"),
        ("babo.agent.tools.cli_apps_config", "from babo.config_base import Base"),
        (
            "babo.agent.turn_delivery",
            "from babo.channels.notification_routes import notification_metadata",
        ),
    ],
)
def test_existing_runtime_and_composition_dependencies_remain_allowed(
    module: str, source: str,
) -> None:
    assert find_violations(source, module) == []


@pytest.mark.parametrize(
    ("relative", "source", "rule", "target", "line"),
    [
        (
            "babo/agent/runner.py", "import babo.webui.metadata",
            "core-execution", "babo.webui.metadata", 1,
        ),
        (
            "babo/agent/loop.py",
            "import importlib\nif importlib.import_module('babo.webui.metadata'):\n    pass",
            "core-execution", "babo.webui.metadata", 2,
        ),
        (
            "babo/agent/loop.py", "if __import__('babo.webui.metadata'):\n    pass",
            "core-execution", "babo.webui.metadata", 1,
        ),
        (
            "babo/session/manager.py", "from babo.cli import gateway",
            "session-storage", "babo.cli.gateway", 1,
        ),
        (
            "babo/channels/notification_routes.py",
            "from babo.channels.slack.runtime import SlackChannel",
            "channel-routing-contracts", "babo.channels.slack.runtime.SlackChannel", 1,
        ),
        (
            "babo/channels/notification_routes.py",
            "from babo.agent.turn_delivery import TurnRoute",
            "channel-routing-contracts", "babo.agent.turn_delivery.TurnRoute", 1,
        ),
        (
            "babo/security/__init__.py", "from ..cli import gateway",
            "security-primitives", "babo.cli.gateway", 1,
        ),
        (
            "babo/channels/websocket/runtime.py",
            "import importlib as modules\nmodules.import_module('babo.command.router')",
            "websocket-transport", "babo.command.router", 2,
        ),
        (
            "babo/agent/tools/shell.py", "from ...config.schema import Base as ConfigBase",
            "tool-config-base", "babo.config.schema.Base", 1,
        ),
        (
            "babo/channels/telegram/runtime.py", "from babo.config.schema import Base",
            "tool-config-base", "babo.config.schema.Base", 1,
        ),
        (
            "babo/config/schema.py", "from babo.agent.tools.shell import ExecTool",
            "configuration-contracts", "babo.agent.tools.shell.ExecTool", 1,
        ),
        (
            "babo/agent/tools/cli_apps_config.py", "from .cli_apps import CliAppsTool",
            "tool-configuration-contracts", "babo.agent.tools.cli_apps.CliAppsTool", 1,
        ),
        (
            "babo/agent/tools/web_config.py", "from babo.config.schema import Config",
            "tool-configuration-contracts", "babo.config.schema.Config", 1,
        ),
        (
            "babo/providers/oauth_catalog_cache.py",
            "from babo.providers import openai_codex_provider",
            "oauth-catalog-cache", "babo.providers.openai_codex_provider", 1,
        ),
    ],
)
def test_cli_rejects_a_violation_and_reports_its_source_location(
    tmp_path: Path, relative: str, source: str, rule: str, target: str, line: int,
) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(source + "\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "scripts.check_import_boundaries", "--root", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stderr.strip() == f"{relative}:{line}: [{rule}] must not import {target}"


def test_cli_accepts_a_tool_owned_config_import(tmp_path: Path) -> None:
    path = tmp_path / "babo/config/schema.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "from babo.agent.tools.cli_apps_config import CliAppsToolConfig\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "scripts.check_import_boundaries", "--root", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
