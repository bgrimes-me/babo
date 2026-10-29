"""Testing proposed tools in isolation. Results are recorded; nothing is ever promoted.

Preferred: a throwaway podman container with ``--network none``, reached from
inside the distrobox through ``distrobox-host-exec``. Otherwise: a subprocess
in a temporary copy of the tool, with a timeout, a scrubbed environment and,
where ``unshare`` allows it, no network namespace. The report says which
isolation was used.

The test runner needs nothing but the standard library: it imports
``test_tool.py`` and calls every ``test_*`` function, so the container image
needs no packages (and no network to fetch them).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from babo.baton.notes import join_frontmatter, split_frontmatter
from babo.baton.town import Town, atomic_write, now_iso

RUNNER = '''\
import importlib.util, sys, traceback
sys.path.insert(0, ".")
spec = importlib.util.spec_from_file_location("test_tool", "test_tool.py")
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
except Exception:
    traceback.print_exc()
    print("RESULT: import-error")
    sys.exit(2)
tests = [getattr(mod, n) for n in sorted(dir(mod)) if n.startswith("test_") and callable(getattr(mod, n))]
failed = 0
for t in tests:
    try:
        t()
        print(f"PASS {t.__name__}")
    except Exception:
        failed += 1
        print(f"FAIL {t.__name__}")
        traceback.print_exc()
print(f"RESULT: {len(tests) - failed}/{len(tests)} passed")
sys.exit(1 if failed or not tests else 0)
'''


@dataclass
class ToolTestResult:
    passed: bool
    isolation: str
    output: str
    code_sha: str


def code_sha(folder: Path) -> str:
    digest = hashlib.sha256()
    for name in ("tool.py", "test_tool.py"):
        path = folder / name
        digest.update(name.encode())
        digest.update(path.read_bytes() if path.exists() else b"")
    return digest.hexdigest()[:16]


def _podman_available() -> list[str] | None:
    if shutil.which("distrobox-host-exec"):
        probe = subprocess.run(["distrobox-host-exec", "podman", "--version"], capture_output=True, text=True, timeout=30)
        if probe.returncode == 0:
            return ["distrobox-host-exec", "podman"]
    if shutil.which("podman") and not os.path.exists("/run/.containerenv"):
        return ["podman"]
    return None


def _scrubbed_env() -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        # Point any HTTP client at nothing.
        "http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "no_proxy": "",
    }


def run_tool_test(folder: Path, *, timeout_s: int = 120, image: str = "docker.io/library/python:3.12-slim", prefer_container: bool = True) -> ToolTestResult:
    sha = code_sha(folder)
    with tempfile.TemporaryDirectory(prefix="baton-tooltest-") as tmp:
        work = Path(tmp)
        for name in ("tool.py", "test_tool.py"):
            if (folder / name).exists():
                shutil.copy2(folder / name, work / name)
        (work / "_runner.py").write_text(RUNNER, encoding="utf-8")
        podman = _podman_available() if prefer_container else None
        if podman is not None:
            cmd = [*podman, "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp",
                   "--memory", "512m", "--pids-limit", "128", "-v", f"{work}:/tool:ro,Z", "-w", "/tool",
                   image, "python", "-I", "_runner.py"]
            isolation = "podman --network none"
        else:
            python = shutil.which("python3") or "python3"
            cmd = [python, "-I", "_runner.py"]
            isolation = "subprocess (timeout, temp dir, scrubbed env)"
            if shutil.which("unshare") and subprocess.run(["unshare", "-rn", "true"], capture_output=True).returncode == 0:
                cmd = ["unshare", "-rn", *cmd]
                isolation = "subprocess (timeout, temp dir, no network namespace)"
        try:
            proc = subprocess.run(cmd, cwd=work, env=_scrubbed_env(), capture_output=True, text=True, timeout=timeout_s)
            output = (proc.stdout + proc.stderr)[-6000:]
            passed = proc.returncode == 0
        except subprocess.TimeoutExpired as exc:
            output = f"timed out after {timeout_s}s\n{(exc.stdout or '')}"[-6000:]
            passed = False
    return ToolTestResult(passed=passed, isolation=isolation, output=output, code_sha=sha)


def run_pending_tool_tests(town: Town, *, timeout_s: int, image: str, prefer_container: bool = True) -> list[tuple[str, ToolTestResult]]:
    """Test each pending tool whose code changed since its last test. Never promotes."""
    results: list[tuple[str, ToolTestResult]] = []
    for folder in sorted(p for p in (town.tools / "pending").glob("*") if p.is_dir()):
        report = folder / "TEST-RESULT.md"
        sha = code_sha(folder)
        if report.exists() and split_frontmatter(report.read_text(encoding="utf-8"))[0].get("code_sha") == sha:
            continue
        result = run_tool_test(folder, timeout_s=timeout_s, image=image, prefer_container=prefer_container)
        meta = {"code_sha": result.code_sha, "passed": result.passed, "isolation": result.isolation, "tested": now_iso()}
        atomic_write(report, join_frontmatter(meta, f"```\n{result.output.strip()}\n```\n"))
        results.append((folder.name, result))
    return results
