#!/usr/bin/env bash
# report.sh — commits one report file to the `reports` branch and pushes it. Never touches your checkout.
# Usage: install/report.sh install/reports/<file>.md
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
file="${1:-}"
[ -f "$file" ] || fail "usage: install/report.sh <report file>"
file="$(cd "$(dirname "$file")" && pwd)/$(basename "$file")"
work="$(mktemp -d)"
trap 'git -C "$REPO_DIR" worktree remove --force "$work/reports" >/dev/null 2>&1 || true; rm -rf "$work"' EXIT
git -C "$REPO_DIR" fetch -q origin reports 2>/dev/null || true
if git -C "$REPO_DIR" rev-parse -q --verify origin/reports >/dev/null; then
  git -C "$REPO_DIR" worktree add -q -B reports "$work/reports" origin/reports
else
  git -C "$REPO_DIR" worktree add -q --orphan -b reports "$work/reports" || fail "could not create the reports branch (needs git 2.42 or newer)"
fi
mkdir -p "$work/reports/install/reports"
cp "$file" "$work/reports/install/reports/"
git -C "$work/reports" add install/reports
git -C "$work/reports" -c user.name="Baton installer" -c user.email="installer@baton.local" -c commit.gpgsign=false \
  commit -q -m "Install report: $(basename "$file")" || true
git -C "$work/reports" push -q origin reports || fail "git push to the reports branch failed"
ok "report pushed to the reports branch: install/reports/$(basename "$file")"
