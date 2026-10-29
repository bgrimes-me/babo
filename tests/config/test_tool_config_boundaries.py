import subprocess
import sys
from importlib import import_module

import pytest


def test_config_base_import_does_not_load_config_schema():
    code = """
import sys
from babo.config_base import Base
print("babo.config.schema" in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "False"


def test_config_validation_and_schema_generation_do_not_import_runtime_implementations():
    code = """
import importlib.abc
import sys

class RuntimeImportBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        config_modules = {
            'babo.agent', 'babo.agent.tools',
            'babo.agent.tools.cli_apps_config',
            'babo.agent.tools.filesystem_config',
            'babo.agent.tools.image_generation_config',
            'babo.agent.tools.self_config',
            'babo.agent.tools.shell_config',
            'babo.agent.tools.web_config',
        }
        if fullname in config_modules:
            return None
        forbidden = (
            'babo.agent', 'babo.api', 'babo.apps', 'babo.channels',
            'babo.cli', 'babo.session', 'babo.webui', 'babo.providers.base',
        )
        if any(fullname == name or fullname.startswith(name + '.') for name in forbidden):
            raise AssertionError('configuration imported runtime implementation: ' + fullname)

sys.meta_path.insert(0, RuntimeImportBlocker())
from babo.config import Config
from babo.config.schema import ModelPresetConfig

config = Config.model_validate({'tools': {
    'exec': {'timeout': 37},
    'imageGeneration': {
        'enabled': True,
        'extraHeaders': {'X-Image-Mode': 'test'},
        'extraBody': {'quality': 'high', 'provider': {'order': ['example']}},
    },
}})
assert config.tools.exec.timeout == 37
restored = Config.model_validate_json(config.model_dump_json(by_alias=True))
assert restored.tools.exec.timeout == 37
assert restored.tools.image_generation.enabled is True
assert restored.tools.image_generation.extra_headers == {'X-Image-Mode': 'test'}
assert restored.tools.image_generation.extra_body == {
    'quality': 'high', 'provider': {'order': ['example']},
}
assert Config.model_json_schema()['properties']['tools']
assert ModelPresetConfig(model='example').to_generation_settings().max_tokens == 8192
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_public_tool_contracts_can_be_imported_without_agent_execution():
    code = """
import sys
from babo.agent.tools import Tool, ToolResult, tool_parameters
from babo.agent.tools.base import Tool as BaseTool

assert Tool is BaseTool
assert callable(tool_parameters)
assert ToolResult
assert 'babo.agent.loop' not in sys.modules
assert 'babo.agent.runner' not in sys.modules
assert 'babo.agent.tools.shell' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("module", "names"),
    [
        ("cli_apps", ("CliAppsToolConfig",)),
        ("filesystem", ("FileToolsConfig",)),
        ("image_generation", ("ImageGenerationToolConfig",)),
        ("self", ("MyToolConfig",)),
        ("shell", ("ExecToolConfig",)),
        ("web", ("WebSearchConfig", "WebFetchConfig", "WebToolsConfig")),
    ],
)
def test_tool_config_imports_keep_the_same_public_class(module, names):
    contracts = import_module(f"babo.agent.tools.{module}_config")
    aggregate = import_module("babo.config.schema")
    implementation = import_module(f"babo.agent.tools.{module}")
    for name in names:
        assert getattr(implementation, name) is getattr(contracts, name)
        assert getattr(aggregate, name) is getattr(contracts, name)
