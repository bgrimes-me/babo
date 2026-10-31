"""Approved commons tools (``commons/tools/active/<name>``) as resident tools.

Only tools the owner approved in review are here. Each call runs ``run(input)``
from the tool's ``tool.py`` in a fresh subprocess, in a temporary copy of the
tool, with a timeout, so a broken tool can't hang or corrupt a turn.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from babo.agent.tools.base import Tool
from babo.baton.ledger import active_tools
from babo.baton.town import Town

_RUNNER = "import sys\nsys.path.insert(0, '.')\nimport tool\nresult = tool.run(sys.stdin.read())\nsys.stdout.write('' if result is None else str(result))\n"
_SECRET_ENV = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


class ActiveCommonsTool(Tool):
    _plugin_discoverable = False

    def __init__(self, name: str, description: str, folder: Path, timeout_s: int = 60):
        self._name = name
        self._description = description
        self._folder = folder
        self._timeout = timeout_s

    @property
    def name(self) -> str:  # pyright: ignore[reportIncompatibleMethodOverride]
        return self._name

    @property
    def description(self) -> str:  # pyright: ignore[reportIncompatibleMethodOverride]
        return f"(town tool) {self._description}"

    @property
    def parameters(self) -> dict[str, Any]:  # pyright: ignore[reportIncompatibleMethodOverride]
        return {"type": "object", "properties": {"input": {"type": "string", "description": "Input text for the tool."}}, "required": ["input"]}

    async def execute(self, **kwargs: Any) -> Any:
        text = str(kwargs.get("input", ""))
        with tempfile.TemporaryDirectory(prefix=f"baton-{self._name}-") as tmp:
            shutil.copy2(self._folder / "tool.py", Path(tmp) / "tool.py")
            env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in _SECRET_ENV)}
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-c", _RUNNER, cwd=tmp, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(text.encode()), timeout=self._timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return Tool.error(f"Error: {self._name} took longer than {self._timeout}s and was stopped.")
        if proc.returncode != 0:
            return Tool.error(f"Error: {self._name} failed: {err.decode('utf-8', 'replace')[-800:]}")
        return out.decode("utf-8", "replace")[:20000]


def active_commons_tools(town: Town) -> list[ActiveCommonsTool]:
    return [ActiveCommonsTool(c.name, c.description, c.path.parent) for c in active_tools(town) if (c.path.parent / "tool.py").exists()]
