#!/usr/bin/env bash
# report.sh — with BABO_PUSH_REPORTS=1, commits one report file to the `reports` branch and pushes it. Never touches your checkout.
# Usage: install/report.sh install/reports/<file>.md
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# A report that can't be pushed is not a failed install step: say "note:", never "FAIL:".
fail() { echo "note: $*"; exit 1; }
file="${1:-}"
[ -f "$file" ] || fail "usage: install/report.sh <report file>"
file="$(cd "$(dirname "$file")" && pwd)/$(basename "$file")"
# Reports describe the machine, so they stay local unless the owner asks for them to be pushed.
[ "${BABO_PUSH_REPORTS:-0}" = 1 ] || fail "the report is saved locally (set BABO_PUSH_REPORTS=1 to push reports to the repository's reports branch)"
work="$(mktemp -d)"
trap 'git -C "$REPO_DIR" worktree remove --force "$work/reports" >/dev/null 2>&1 || true; rm -rf "$work"' EXIT
git -C "$REPO_DIR" worktree prune 2>/dev/null || true
git -C "$REPO_DIR" fetch -q origin reports:refs/remotes/origin/reports 2>/dev/null || true
if git -C "$REPO_DIR" rev-parse -q --verify origin/reports >/dev/null; then
  git -C "$REPO_DIR" worktree add -q -B reports "$work/reports" origin/reports || fail "could not check out the reports branch"
elif git -C "$REPO_DIR" rev-parse -q --verify refs/heads/reports >/dev/null; then
  # Made by an earlier run whose push didn't get through; keep adding to it.
  git -C "$REPO_DIR" worktree add -q "$work/reports" reports || fail "could not check out the reports branch"
else
  git -C "$REPO_DIR" worktree add -q --orphan -b reports "$work/reports" || fail "could not create the reports branch (needs git 2.42 or newer)"
fi
mkdir -p "$work/reports/install/reports"
cp "$file" "$work/reports/install/reports/"
git -C "$work/reports" add install/reports
git -C "$work/reports" -c user.name="Baton installer" -c user.email="installer@baton.local" -c commit.gpgsign=false \
  commit -q -m "Install report: $(basename "$file")" || true
git -C "$work/reports" push -q origin reports || fail "could not push to GitHub (this machine may not be logged in to GitHub); the report is still saved locally"
ok "report pushed to the reports branch: install/reports/$(basename "$file")"
