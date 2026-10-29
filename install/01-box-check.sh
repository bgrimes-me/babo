#!/usr/bin/env bash
# 01-box-check.sh — BOX. Read-only: checks the distrobox has what Baton needs.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_box

missing=()
command -v git >/dev/null 2>&1 || missing+=(git)
command -v uv >/dev/null 2>&1 || missing+=(uv)
if command -v python3 >/dev/null 2>&1; then
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || missing+=("python (3.11 or newer)")
  python3 -c "import sqlite3; sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)')" 2>/dev/null || missing+=("sqlite with FTS5")
else
  missing+=(python)
fi
command -v bwrap >/dev/null 2>&1 || echo "note: bubblewrap (bwrap) is not installed; shell privacy will be best-effort"
command -v gitleaks >/dev/null 2>&1 || echo "note: gitleaks is not installed; the night scrub will use its regex fallback"

if [ ${#missing[@]} -gt 0 ]; then
  fail "missing in the box: ${missing[*]}. Ask Squid to run install/squid-01-packages.sh inside the box, then run this step again."
fi
ok "the box has git, uv, python $(python3 -c 'import platform; print(platform.python_version())') and FTS5"
