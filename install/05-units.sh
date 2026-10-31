#!/usr/bin/env bash
# 05-units.sh — BOX. Writes the systemd user units (queue, gateway, upkeep) into install/out/systemd.
# Installing them on the host is the owner's step (host-services.sh).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_box
bin="$(babo_bin)" || fail "babo is not installed; run 02-install-babo.sh first"
out="$INSTALL_DIR/out/systemd"
"$bin" configs || fail "could not write gateway configs"
"$bin" units --out "$out" || fail "could not write the units"
ls "$out"/babo-upkeep.service "$out"/babo-queue.service >/dev/null 2>&1 || fail "units missing in $out"
ok "units written to $out; next: ask the owner to run install/host-services.sh on the host"
