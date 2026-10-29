#!/usr/bin/env bash
# 04-found-town.sh — BOX. Founds the town: repo, CHARTER, HOUSE, REGISTRY and its first resident, the Mayor.
# Safe to run again: an existing town is left as it is.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_box
bin="$(babo_bin)" || fail "babo is not installed; run 02-install-babo.sh first"
[ -f "$HOME/.babo/config.json" ] || fail "no config; run 03-config.sh first"
"$bin" found || fail "babo found failed"
# Remove what earlier versions left behind (obsolete settings, old backups, stale folders).
"$bin" tidy --checkout "$REPO_DIR" || fail "babo tidy failed"
"$bin" residents
ok "the town is founded"
