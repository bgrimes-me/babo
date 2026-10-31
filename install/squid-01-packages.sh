#!/usr/bin/env bash
# squid-01-packages.sh — the old name of host-packages.sh, kept for one release so update steps written
# before the rename still work. Use host-packages.sh.
set -euo pipefail
exec bash "$(dirname "${BASH_SOURCE[0]}")/host-packages.sh" "$@"
