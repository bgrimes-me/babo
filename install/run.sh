#!/usr/bin/env bash
# run.sh — runs one install step, saves its full output, and on FAIL pushes the report.
# Usage: install/run.sh 03-config        (or squid-01-packages, 00-recon, ...)
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
    if [ "${step%.sh}" = "00-recon" ]; then bash "$INSTALL_DIR/report.sh" "$log" || true; fi
    echo "$last"
    ;;
  *)
    bash "$INSTALL_DIR/report.sh" "$log" || echo "note: could not push the report; it is saved at $log"
    echo "FAIL: step ${step%.sh} failed. The report is saved at $log and pushed to the reports branch. Stop here and tell Squid."
    exit 1
    ;;
esac
