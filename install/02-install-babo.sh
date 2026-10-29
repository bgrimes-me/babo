#!/usr/bin/env bash
# 02-install-babo.sh — BOX. Installs this checkout of babo (with Baton) as the `babo` command.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_box
command -v uv >/dev/null 2>&1 || fail "uv is missing; run 01-box-check.sh first"
[ -f "$REPO_DIR/babo/baton/__init__.py" ] || fail "$REPO_DIR is not a Babo checkout"

# Building the WebUI downloads Bun; if that is not possible the install continues without the WebUI.
if ! uv tool install --force --python 3.12 "$REPO_DIR"; then
  echo "note: the WebUI build failed; installing without the WebUI (the command line and chat channels still work)"
  BABO_SKIP_WEBUI_BUILD=1 uv tool install --force --python 3.12 "$REPO_DIR" || fail "uv tool install failed (see the output above)"
fi
bin="$(babo_bin)" || fail "babo was installed but is not on PATH; add \$HOME/.local/bin to PATH"
"$bin" review --help >/dev/null || fail "babo does not run"

ok "babo installed at $bin"
