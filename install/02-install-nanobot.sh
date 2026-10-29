#!/usr/bin/env bash
# 02-install-nanobot.sh — BOX. Installs this checkout of nanobot (with Baton) as the `nanobot` command.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_box
command -v uv >/dev/null 2>&1 || fail "uv is missing; run 01-box-check.sh first"
[ -f "$REPO_DIR/nanobot/baton/__init__.py" ] || fail "$REPO_DIR is not the Baton fork of nanobot"

# Building the WebUI downloads Bun; if that is not possible the install continues without the WebUI.
if ! uv tool install --force --python 3.12 "$REPO_DIR"; then
  echo "note: the WebUI build failed; installing without the WebUI (the command line and chat channels still work)"
  NANOBOT_SKIP_WEBUI_BUILD=1 uv tool install --force --python 3.12 "$REPO_DIR" || fail "uv tool install failed (see the output above)"
fi
bin="$(nanobot_bin)" || fail "nanobot was installed but is not on PATH; add \$HOME/.local/bin to PATH"
"$bin" baton --help >/dev/null || fail "nanobot baton does not run"
ok "nanobot installed at $bin"
