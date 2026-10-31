#!/usr/bin/env bash
# 00-recon.sh — read-only reconnaissance of the owner's machine for the Baton install.
#
# Run on: HOST, from the root of the babo checkout.
# Changes nothing on the system. It only reads, sends a few small test requests
# to llama-server, and writes one report file:
#   install/reports/00-recon-<date>.md
#
# Ends with exactly one line starting "OK:" or "FAIL: <reason>".
#
# Optional overrides (normally not needed):
#   BATON_LLAMA_URL   e.g. http://127.0.0.1:8080   (skip container discovery)
#   BATON_BOX         distrobox name (default: babo)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPORT_DIR="$SCRIPT_DIR/reports"
STAMP="$(date +%Y-%m-%d)"
REPORT="$REPORT_DIR/00-recon-$STAMP.md"
BOX="${BATON_BOX:-babo}"

fail() { echo "FAIL: $*"; exit 1; }

# --- preconditions -----------------------------------------------------------
[ -f "$REPO_DIR/pyproject.toml" ] || fail "run this from the babo checkout (pyproject.toml not found)"
if [ -f /run/.containerenv ] || [ -n "${DISTROBOX_ENTER_PATH:-}" ]; then
  fail "this step runs on the HOST, not inside the distrobox. Open a host terminal and run it again."
fi
command -v curl >/dev/null 2>&1 || fail "curl not found on host"
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
[ -n "$PY" ] || fail "python3 not found on host"
mkdir -p "$REPORT_DIR"

export BATON_BOX="$BOX"
export BATON_REPORT="$REPORT"
export BATON_STAMP="$STAMP"

# The rest is Python for reliable JSON handling. It writes the report and
# prints the final OK:/FAIL: line itself.
exec "$PY" - <<'PYEOF'
import getpass, json, os, re, shlex, shutil, subprocess, sys, time, urllib.request, urllib.error

BOX = os.environ["BATON_BOX"]
REPORT = os.environ["BATON_REPORT"]
STAMP = os.environ["BATON_STAMP"]

rows = []      # (check, status, detail)   status: PASS | WARN | FAIL | INFO
sections = []  # (title, body)
critical = []  # reasons that make the whole recon FAIL


def add(check, status, detail):
    rows.append((check, status, str(detail).replace("\n", " ").replace("|", "/")[:300]))
    if status == "FAIL":
        critical.append(f"{check}: {detail}")


def run(cmd, timeout=30):
    """Run a read-only command; never raise."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"


def http(method, url, body=None, timeout=20):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, raw, time.monotonic() - start
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), time.monotonic() - start
    except Exception as e:  # connection refused, timeout, ...
        return None, f"{type(e).__name__}: {e}", time.monotonic() - start


def jparse(raw):
    try:
        return json.loads(raw)
    except Exception:
        return None


# --- host basics ---------------------------------------------------------------
rc, out, _ = run(["cat", "/etc/os-release"])
os_name = next((l.split("=", 1)[1].strip('"') for l in out.splitlines() if l.startswith("PRETTY_NAME=")), "unknown")
add("host OS", "INFO", os_name)
USER = os.environ.get("USER") or getpass.getuser()
add("host user", "INFO", USER)

# --- llama-server container --------------------------------------------------
container = None
args = []
port_map = {}
rc, out, err = run(["podman", "ps", "--format", "json"])
if rc != 0:
    add("podman (host)", "FAIL", err or "podman ps failed")
else:
    add("podman (host)", "PASS", "podman ps works")
    try:
        running = json.loads(out or "[]")
    except Exception:
        running = []
    for c in running:
        blob = " ".join([str(c.get("Image", "")), " ".join(map(str, c.get("Command") or [])), " ".join(c.get("Names") or [])]).lower()
        if "llama" in blob:
            container = c
            break
    if container is None:
        add("llama container", "FAIL" if not os.environ.get("BATON_LLAMA_URL") else "WARN",
            f"no running container with 'llama' in its image/command/name ({len(running)} running)")
    else:
        name = (container.get("Names") or ["?"])[0]
        rc, iout, _ = run(["podman", "inspect", name])
        info = (jparse(iout) or [{}])[0]
        cfg = info.get("Config", {}) or {}
        args = list(info.get("Args") or []) or list(cfg.get("Cmd") or [])
        entry = cfg.get("Entrypoint") or []
        if isinstance(entry, str):
            entry = shlex.split(entry)
        full = list(entry) + [a for a in args if a not in entry]
        args = full
        ports = (info.get("NetworkSettings", {}) or {}).get("Ports") or {}
        for cport, binds in ports.items():
            for b in binds or []:
                port_map[cport.split("/")[0]] = b.get("HostPort")
        netmode = (info.get("HostConfig", {}) or {}).get("NetworkMode", "")
        add("llama container", "PASS", f"{name} image={container.get('Image')}")
        sections.append(("llama container command", "```\n" + " ".join(shlex.quote(a) for a in full) + "\n```\n"
                         f"network mode: `{netmode}`  published ports: `{port_map or 'none'}`"))


def arg_value(names):
    for i, a in enumerate(args):
        for n in names:
            if a == n and i + 1 < len(args):
                return args[i + 1]
            if a.startswith(n + "="):
                return a.split("=", 1)[1]
    return None


jinja_flag = any(a == "--jinja" for a in args)
cfg_port = arg_value(["--port"]) or "8080"
cfg_ctx = arg_value(["-c", "--ctx-size"])
cfg_slots = arg_value(["-np", "--parallel"])
cfg_model = arg_value(["-m", "--model"])
if container is not None:
    add("--jinja flag", "PASS" if jinja_flag else "WARN",
        "present" if jinja_flag else "not in command line (tool calling usually needs --jinja)")

# --- endpoint ------------------------------------------------------------------
base = os.environ.get("BATON_LLAMA_URL", "").rstrip("/")
candidates = [base] if base else []
host_port = port_map.get(cfg_port) or cfg_port
for p in dict.fromkeys([host_port, cfg_port, "8080", "8000", "11434"]):
    if p:
        candidates.append(f"http://127.0.0.1:{p}")
endpoint = None
for c in dict.fromkeys(candidates):
    status, raw, _ = http("GET", c + "/health", timeout=5)
    if status == 200:
        endpoint = c
        break
if endpoint is None:
    add("endpoint", "FAIL", f"no llama-server /health answered on {', '.join(dict.fromkeys(candidates))}")
else:
    add("endpoint", "PASS", endpoint)

model_id = None
n_ctx = cfg_ctx
slots = cfg_slots
if endpoint:
    status, raw, _ = http("GET", endpoint + "/v1/models")
    models = jparse(raw) or {}
    ids = [m.get("id") for m in models.get("data", []) if isinstance(m, dict)]
    model_id = ids[0] if ids else None
    add("model", "PASS" if model_id else "WARN", model_id or (cfg_model or "unknown"))
    status, raw, _ = http("GET", endpoint + "/props")
    props = jparse(raw) or {}
    if props:
        dgs = props.get("default_generation_settings", {}) or {}
        n_ctx = dgs.get("n_ctx") or props.get("n_ctx") or n_ctx
        slots = props.get("total_slots") or slots
        tmpl = props.get("chat_template") or ""
        sections.append(("llama-server /props (excerpt)", "```json\n" + json.dumps(
            {k: props.get(k) for k in ("total_slots", "model_path", "build_info")} |
            {"n_ctx": n_ctx, "chat_template_chars": len(tmpl)}, indent=2) + "\n```"))
    add("n_ctx", "PASS" if n_ctx else "WARN", n_ctx or "unknown")
    add("slots", "PASS" if slots else "WARN", f"{slots or 'unknown'} (Baton assumes 1 unless config says otherwise)")

# --- real tool-call test -------------------------------------------------------
tok_s = None
if endpoint:
    body = {
        "model": model_id or "default",
        "messages": [
            {"role": "system", "content": "You can call tools."},
            {"role": "user", "content": "What is the weather in Oslo? Use the tool."},
        ],
        "tools": [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the weather for a city.",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
            },
        }],
        "tool_choice": "auto",
        "temperature": 0,
        "max_tokens": 256,
    }
    status, raw, secs = http("POST", endpoint + "/v1/chat/completions", body, timeout=180)
    resp = jparse(raw) or {}
    calls = (((resp.get("choices") or [{}])[0].get("message") or {}).get("tool_calls")) or []
    ok = bool(calls) and calls[0].get("function", {}).get("name") == "get_weather"
    detail = f"HTTP {status}, {len(calls)} tool call(s)"
    if ok:
        detail += f", args={calls[0]['function'].get('arguments')}"
    elif status != 200:
        detail += f": {raw[:160]}"
    add("tool-call test", "PASS" if ok else "FAIL", detail)

# --- schema-output test --------------------------------------------------------
if endpoint:
    schema = {
        "type": "object",
        "properties": {
            "thread": {"type": "string"},
            "done": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
        },
        "required": ["thread", "done", "confidence"],
        "additionalProperties": False,
    }
    body = {
        "model": model_id or "default",
        "messages": [{"role": "user", "content": "Report on a task called 'recon': you checked the GPU and the port. Answer as JSON."}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "checkpoint", "strict": True, "schema": schema}},
        "temperature": 0,
        "max_tokens": 256,
    }
    status, raw, secs = http("POST", endpoint + "/v1/chat/completions", body, timeout=180)
    resp = jparse(raw) or {}
    content = (((resp.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
    parsed = jparse(content)
    ok = isinstance(parsed, dict) and all(k in parsed for k in schema["required"]) and isinstance(parsed.get("done"), list)
    add("schema-output test", "PASS" if ok else "FAIL", f"HTTP {status}, " + (content[:160] if content else raw[:160]))
    timings = resp.get("timings") or {}
    if timings.get("predicted_per_second"):
        tok_s = round(float(timings["predicted_per_second"]), 1)
    else:
        usage = resp.get("usage") or {}
        if usage.get("completion_tokens") and secs:
            tok_s = round(usage["completion_tokens"] / secs, 1)

# --- throughput ------------------------------------------------------------------
if endpoint:
    body = {"model": model_id or "default", "messages": [{"role": "user", "content": "Count from 1 to 60, separated by spaces."}],
            "temperature": 0, "max_tokens": 200}
    status, raw, secs = http("POST", endpoint + "/v1/chat/completions", body, timeout=180)
    resp = jparse(raw) or {}
    timings = resp.get("timings") or {}
    if timings.get("predicted_per_second"):
        tok_s = round(float(timings["predicted_per_second"]), 1)
    elif (resp.get("usage") or {}).get("completion_tokens") and secs:
        tok_s = round(resp["usage"]["completion_tokens"] / secs, 1)
    prompt_tok_s = timings.get("prompt_per_second")
    add("tokens/sec", "INFO" if tok_s else "WARN",
        f"generation {tok_s or 'unknown'} tok/s" + (f", prompt {round(prompt_tok_s, 1)} tok/s" if prompt_tok_s else ""))

# --- GPU -------------------------------------------------------------------------
rc, out, err = run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,driver_version", "--format=csv,noheader"])
add("GPU / VRAM", "INFO" if rc == 0 else "WARN", out if rc == 0 else (err or "nvidia-smi not available on host"))

# --- distrobox ---------------------------------------------------------------------
rc, out, err = run(["distrobox", "list", "--no-color"], timeout=30)
if rc != 0:
    add("distrobox", "FAIL", err or "distrobox list failed")
    box_ok = False
else:
    box_ok = any(re.search(rf"\|\s*{re.escape(BOX)}\s*\|", l) for l in out.splitlines())
    add("distrobox", "PASS" if box_ok else "FAIL", f"box '{BOX}' " + ("found" if box_ok else "not found"))

if box_ok:
    probe = r'''
echo "python=$(python3 --version 2>&1 || echo missing)"
echo "uv=$(uv --version 2>&1 || echo missing)"
echo "git=$(git --version 2>&1 || echo missing)"
echo "bwrap=$(command -v bwrap || echo missing)"
echo "gitleaks=$(command -v gitleaks || echo missing)"
echo "fts5=$(python3 -c "import sqlite3;c=sqlite3.connect(':memory:');c.execute('create virtual table t using fts5(x)');print('yes')" 2>&1 | tail -1)"
echo "hostexec=$(command -v distrobox-host-exec || echo missing)"
echo "hostpodman=$(distrobox-host-exec podman --version 2>&1 | tail -1 || echo failed)"
echo "babo=$(command -v babo || echo missing)"
if command -v bwrap >/dev/null 2>&1; then
  if bwrap --ro-bind / / --dev /dev --proc /proc true 2>/dev/null; then echo "bwrap_works=yes"; else echo "bwrap_works=no"; fi
else echo "bwrap_works=n/a"; fi
'''
    rc, out, err = run(["distrobox", "enter", BOX, "--", "bash", "-c", probe], timeout=180)
    vals = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    if rc != 0 and not vals:
        add("box probe", "FAIL", err[:200] or f"distrobox enter {BOX} failed")
    py = vals.get("python", "missing")
    m = re.search(r"(\d+)\.(\d+)", py)
    py_ok = bool(m) and (int(m.group(1)), int(m.group(2))) >= (3, 11)
    add("box python", "PASS" if py_ok else "WARN", f"{py} (need 3.11+; uv can install one)")
    add("box uv", "PASS" if "missing" not in vals.get("uv", "missing") else "WARN", vals.get("uv", "missing"))
    add("box git", "PASS" if "missing" not in vals.get("git", "missing") else "FAIL", vals.get("git", "missing"))
    add("box sqlite FTS5", "PASS" if vals.get("fts5") == "yes" else "FAIL", vals.get("fts5", "unknown"))
    add("box bwrap sandbox", "PASS" if vals.get("bwrap_works") == "yes" else "WARN",
        f"bwrap={vals.get('bwrap')} works={vals.get('bwrap_works')} (without it, resident privacy in the shell is best-effort)")
    add("box gitleaks", "PASS" if "missing" not in vals.get("gitleaks", "missing") else "WARN",
        vals.get("gitleaks", "missing") + " (regex fallback is used if missing)")
    hp = vals.get("hostpodman", "")
    add("distrobox-host-exec podman", "PASS" if "podman version" in hp.lower() else "WARN",
        (hp or "failed") + " (needed for isolated tool tests; subprocess fallback otherwise)")
    add("babo in box", "INFO", vals.get("babo", "missing"))

# --- systemd / linger ------------------------------------------------------------
rc, out, err = run(["loginctl", "show-user", USER, "-p", "Linger"])
linger = out.split("=", 1)[1] if "=" in out else "unknown"
add("linger", "PASS" if linger == "yes" else "WARN",
    f"Linger={linger}" + ("" if linger == "yes" else " (the owner runs: loginctl enable-linger — needed for upkeep while logged out)"))
rc, out, err = run(["systemctl", "--user", "is-system-running"])
add("systemd --user", "PASS" if out in ("running", "degraded") else "WARN", out or err or "unknown")

# --- write report ----------------------------------------------------------------
verdict = "FAIL" if critical else "OK"
lines = [f"# Baton recon — {STAMP}", "", f"Verdict: **{verdict}**", "",
         "| check | status | detail |", "|---|---|---|"]
lines += [f"| {c} | {s} | {d} |" for c, s, d in rows]
lines.append("")
for title, body in sections:
    lines += [f"## {title}", "", body, ""]
lines += ["## Values for the Baton config", "", "```json", json.dumps({
    "endpoint": endpoint, "model": model_id, "nCtx": n_ctx, "slots": slots,
    "jinja": jinja_flag, "tokensPerSecond": tok_s}, indent=2), "```", ""]
with open(REPORT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))

for c, s, d in rows:
    print(f"{s:5} {c}: {d}")
print(f"report: {REPORT}")
if critical:
    print("FAIL: " + "; ".join(critical)[:400])
    sys.exit(1)
print("OK: recon finished, report written to " + REPORT)
PYEOF
