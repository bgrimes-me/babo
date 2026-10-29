#!/usr/bin/env bash
# 07-doctor.sh — BOX. The last step: checks the whole town end to end.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_box
bin="$(nanobot_bin)" || fail "nanobot is not installed; run 02-install-nanobot.sh first"
if "$bin" baton doctor; then
  ok "doctor passed: the town is working"
fi
fail "doctor found problems (see the table above)"
