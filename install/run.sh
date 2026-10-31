#!/usr/bin/env bash
# run.sh — runs one install step, saves its full output, and on FAIL pushes the report.
# Usage: install/run.sh 03-config        (or host-packages, 00-recon, ...)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
step="${1:-}"
[ -n "$step" ] || fail "usage: install/run.sh <step>, e.g. install/run.sh 01-box-check"
script="$INSTALL_DIR/${step%.sh}.sh"
[ -f "$script" ] || fail "no such step: $step"
mkdir -p "$INSTALL_DIR/reports"
stamp="$(date +%Y-%m-%d-%H%M%S)"
log="$INSTALL_DIR/reports/${step%.sh}-$stamp.md"
{
  echo "# Install step ${step%.sh} — $stamp"
  echo
  echo "Ran on: $(in_box && echo BOX || echo HOST) ($(uname -n))"
  echo
  echo '```'
} > "$log"
set +e
bash "$script" 2>&1 | tee -a "$log"
set -e
echo '```' >> "$log"
last="$(grep -E '^(OK|FAIL):' "$log" | tail -1 || true)"
case "$last" in
  OK:*)
    if [ "${step%.sh}" = "00-recon" ]; then bash "$INSTALL_DIR/report.sh" "$log" | sed 's/^OK:/note:/' || true; fi
    echo "$last"
    ;;
  *)
    where="saved at $log"
    if pushed="$(bash "$INSTALL_DIR/report.sh" "$log" 2>&1)"; then where="$where and pushed to the reports branch"; fi
    printf '%s\n' "$pushed" | sed 's/^OK:/note:/'
    echo "FAIL: step ${step%.sh} failed. The report is $where. Stop here and tell the owner."
    exit 1
    ;;
esac
