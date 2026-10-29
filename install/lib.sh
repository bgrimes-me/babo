#!/usr/bin/env bash
# Shared helpers for the install steps. Sourced, never run directly.
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$INSTALL_DIR/.." && pwd)"
# The distrobox the town runs in. Inside a box, distrobox sets CONTAINER_ID to its name.
BOX="${BATON_BOX:-${CONTAINER_ID:-Agent}}"

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

babo_bin() {
  if command -v babo >/dev/null 2>&1; then command -v babo; return; fi
  if [ -x "$HOME/.local/bin/babo" ]; then echo "$HOME/.local/bin/babo"; return; fi
  return 1
}
