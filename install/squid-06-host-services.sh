#!/usr/bin/env bash
# squid-06-host-services.sh — the old name of host-services.sh, kept for one release so update steps written
# before the rename still work. Use host-services.sh.
set -euo pipefail
exec bash "$(dirname "${BASH_SOURCE[0]}")/host-services.sh" "$@"
