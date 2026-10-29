#!/usr/bin/env bash
# 03-config.sh — BOX. Writes the town's settings into ~/.babo/config.json, using the recon report.
# Backs the old config up first. Safe to run again.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_box
bin="$(babo_bin)" || fail "babo is not installed; run 02-install-babo.sh first"
report="$(ls -1t "$INSTALL_DIR"/reports/00-recon-*.md 2>/dev/null | head -1 || true)"
[ -n "$report" ] || fail "no recon report found in install/reports/; run 00-recon.sh on the host first"

values="$(python3 - "$report" <<'PY'
import json, re, sys
text = open(sys.argv[1], encoding="utf-8").read()
block = re.search(r"## Values for the Baton config\s+```json\s+(\{.*?\})\s+```", text, re.S)
if not block:
    sys.exit("no values block")
v = json.loads(block.group(1))
bwrap = "yes" if re.search(r"\| box bwrap sandbox \| PASS \|", text) else "no"
if not v.get("endpoint"):
    sys.exit("recon found no llama-server endpoint")
print(v["endpoint"], v.get("model") or "local", v.get("nCtx") or 8192, v.get("slots") or 1, bwrap)
PY
)" || fail "could not read the recon report $report"
read -r endpoint model nctx slots bwrap <<<"$values"
flag="--no-bwrap"; [ "$bwrap" = "yes" ] && flag="--bwrap"
"$bin" init-config --upstream "$endpoint" --model "$model" --ctx "$nctx" --slots "$slots" --box "$BOX" "$flag" \
  || fail "babo init-config failed"
ok "config written (llama-server $endpoint, model $model, n_ctx $nctx, slots $slots, bwrap $bwrap)"
