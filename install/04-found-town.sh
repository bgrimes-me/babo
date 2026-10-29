#!/usr/bin/env bash
# 04-found-town.sh — BOX. Founds the town: repo, CHARTER, HOUSE, REGISTRY and the three founding residents.
# Safe to run again: an existing town is left as it is.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_box
bin="$(babo_bin)" || fail "babo is not installed; run 02-install-babo.sh first"
[ -f "$HOME/.babo/config.json" ] || fail "no config; run 03-config.sh first"
"$bin" found || fail "babo found failed"
"$bin" residents
ok "the town is founded"
