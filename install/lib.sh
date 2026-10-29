#!/usr/bin/env bash
# Shared helpers for the install steps. Sourced, never run directly.
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$INSTALL_DIR/.." && pwd)"
BOX="${BATON_BOX:-arch}"

fail() { echo "FAIL: $*"; exit 1; }
ok() { echo "OK: $*"; exit 0; }

in_box() { [ -f /run/.containerenv ] || [ -n "${DISTROBOX_ENTER_PATH:-}" ]; }

need_box() {
  in_box || fail "this step runs inside the distrobox. Run: distrobox enter $BOX   then run the step again."
}

need_host() {
  in_box && fail "this step runs on the HOST, not inside the distrobox. Open a host terminal and run it again."
  return 0
}

nanobot_bin() {
  if command -v nanobot >/dev/null 2>&1; then command -v nanobot; return; fi
  if [ -x "$HOME/.local/bin/nanobot" ]; then echo "$HOME/.local/bin/nanobot"; return; fi
  return 1
}
