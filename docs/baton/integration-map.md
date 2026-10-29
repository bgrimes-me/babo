# Baton integration map

Phase 0 deliverable. It records what upstream nanobot actually does today, where each part of the Baton spec attaches to it, what the spec assumed that the code contradicts, and the routing decision.

- Upstream read: `HKUDS/nanobot` at `b1cc89e` (version 0.4.0).
- Reference read: `RecursiveRabbit/nanobot`, branch `context-window`, at `bcd562a` (MIT). Ideas borrowed are credited in the table at the end. No code is copied.

## 1. How upstream is put together (the parts Baton touches)

| Area | Upstream owner | What matters for Baton |
|---|---|---|
| Turn orchestration | `agent/loop.py` `AgentLoop` | One loop is bound to **one workspace**: it builds one `ContextBuilder`, `MemoryStore`, `SkillsLoader`, `SessionManager`, `Consolidator` and `SubagentManager` in `__init__`. Every composition root (gateway, `agent`, `serve`, SDK) creates it via `AgentLoop.from_config(...)`. |
| Model/tool loop | `agent/runner.py`, `agent/tools/execution.py` | Tools run through `ToolRegistry.prepare_call` then `tool.execute`. Hooks are called around each tool. |
| Hooks | `agent/hook.py`, `agent/turn_hooks.py` | `AgentHook` has `before_execute_tool` / `after_execute_tool` / `after_run` etc. Composition roots pass `hooks=` and `hook_factories=`. **`CompositeHook` swallows hook exceptions, so a hook cannot veto a tool call.** |
| System prompt | `agent/context.py` `ContextBuilder.build_system_prompt` | Hard-codes: identity template, `AGENTS.md` (project), `SOUL.md` + `USER.md` (workspace), `memory/MEMORY.md`, always-on skills, skill summary, archived summary. |
| Memory | `agent/memory.py` | `MemoryStore` (files + `history.jsonl`), `MemoryArchiver` (writes the compaction summary), `Consolidator` (compaction coordinator), Dream (prompt + tools, run as a gateway cron job named `dream`). |
| Compaction | `agent/context_governance.py`, `agent/autocompact.py`, `session/state.py` | In-turn compaction calls `consolidate_history` (= `Consolidator.summarize_transcript`). If it returns nothing, the runner raises `ContextWindowExceededError` and **nothing is mutated**. Idle and `/compact` compaction use `Consolidator.compact_idle_session`, which commits through a SQLite transaction. Original messages are **kept**: a summary checkpoint plus the `last_archived` watermark hide them from replay. |
| Sessions | `session/manager.py`, `session/sqlite_store.py` | Transcripts live in **SQLite** (`<config dir>/sessions/<workspace-id>/sessions.sqlite3`, WAL, `synchronous=FULL`), not in files. Messages flagged `_command` (or summary checkpoints) are stored but excluded from model replay. |
| Subagents | `agent/subagent.py`, `agent/subagent_sessions.py` | `spawn` tool; subagent sessions are `subagent:<id>` keys in the same store. |
| Skills | `agent/skills.py` | Reads `<workspace>/skills/` plus built-ins; agentskills.io `SKILL.md` format. |
| MCP | `agent/tools/mcp.py` | `MCPProvider` owned by the composition root, sharing the `ToolRegistry`. Stdio servers configured under `tools.mcpServers`. |
| Workspace bootstrap | `utils/helpers.py` `sync_workspace_templates` | Called by `agent`/`serve`/onboard. Writes `AGENTS.md`, `SOUL.md`, `USER.md`, `HEARTBEAT.md`, `memory/MEMORY.md`, `memory/history.jsonl`, `skills/` and **initialises a git repo inside the workspace**. |
| Shell | `agent/tools/shell.py`, `agent/tools/sandbox.py` | With `tools.restrictToWorkspace`, a best-effort guard blocks `../` and absolute paths outside the workspace. Real isolation needs `tools.exec.sandbox: "bwrap"`. |
| Provider | `providers/openai_compat_provider.py` | llama.cpp goes through the OpenAI-compatible provider. **No per-call `response_format`** is exposed. |
| Config | `config/schema.py` `Config` | Pydantic settings, camelCase aliases, `NANOBOT_` env prefix. Dream is `agents.defaults.dream.enabled`. |
| CLI | `cli/entry.py` (fast dispatch), `cli/commands.py` (Typer app), `cli/agent.py` | Sub-apps are added with `app.add_typer(...)`. `nanobot agent` is dispatched early by `entry.py`. Interactive `agent` attaches to a per-(config, workspace) background gateway. |
| Cron | `cron/service.py` | Workspace-scoped `cron/jobs.json`. |

## 2. Where the spec and the code disagree (trusting the code)

1. **Transcripts are SQLite, not files.** Upstream's session store is the runtime source of truth and is transactional. Baton keeps it. `residents/<id>/sessions/YYYY-MM-DD/` becomes an **export**: the harness writes the event log there live and exports each session transcript there (at session end and in the night pre-pass). "Files are truth" holds for everything a resident authors (SELF, ROLE, BATON, knowledge, checkpoints, letters); the transcript archive is a faithful copy, never indexed.
2. **Upstream compaction is already mostly non-destructive.** Messages are never deleted from the store; a failed in-turn compaction already changes nothing. The real gap is `MemoryArchiver.archive()`: when the model fails, it substitutes a **truncated raw dump** as the summary and commits it. Baton's restore-or-nothing (section 3.5) therefore wraps the consolidator: pre-compaction checkpoint turn → hashed, verified backup of the session (export from SQLite) → upstream summary → **reject the raw-dump fallback** (return nothing, so upstream's own "nothing happens" path runs) → append the latest baton entry to the accepted summary. A forced-failure test proves the session is byte-identical afterwards.
3. **Hooks cannot block tools.** The commit gate (3.4) cannot be an `AgentHook`. It is implemented by wrapping the shell tools (`exec` and the exec-session tools) in the registry with a gating proxy that returns a plain tool error. No upstream edit needed.
4. **No `response_format` through the provider.** Checkpoint, fold, Archivist and Inspector turns need llama.cpp `json_schema`. Baton uses its own small OpenAI-compatible client (httpx, already a dependency) for schema turns, sending the resident's own current context plus the checkpoint instruction. Ordinary turns stay on the upstream provider.
5. **Dream is a gateway cron job, not a module you can unplug.** Rather than excising it (as the reference fork did — a large, rebase-hostile diff), Baton leaves the code in place and **requires** `agents.defaults.dream.enabled: false`. Founding writes that into the town config; Baton refuses to start a resident if Dream is on; `doctor` checks it. Nothing behind a resident's back.
6. **Template sync writes `SOUL.md`/`USER.md` and a nested git repo into the workspace.** For a resident workspace that lives inside the town repo this would create a second, conflicting source of truth and a nested `.git`. A two-line `# BATON:` guard makes `sync_workspace_templates` skip resident workspaces (identified by a `.baton-resident` marker written only by founding).
7. **`ContextBuilder` hard-codes SOUL/USER/MEMORY.** Baton supplies a `BatonContextBuilder` subclass whose `build_system_prompt` emits the session start bundle (section 5) instead. It must be the instance the loop is built with, because `Consolidator`, `SubagentManager` and token estimation all hold bound references to it — so it is selected with one `# BATON:` line in `AgentLoop.__init__`, not swapped afterwards.
8. **Upstream registers ~20 tools.** The ~12-tool budget is enforced by Baton pruning the registry to a per-office allowlist after construction (plus the baton tools and approved commons tools).
9. **llama-server already queues requests per slot.** The model queue is still needed — for one global order across processes (gateway, `agent --as`, night, later OpenCode), interactive-over-night priority, turn-level grouping (one resident's turn finishes before another's starts, which keeps the KV cache useful), and timeouts. See 3.4 below.
10. **Privacy against the shell is only as strong as the sandbox.** File tools respect `restrictToWorkspace`; the shell guard is best-effort. If `bwrap` works inside the `arch` box (recon checks), Baton turns on `tools.exec.sandbox: "bwrap"` for residents and isolation is real. If not, `doctor` reports privacy as best-effort and the charter says so honestly.

## 3. Routing decision

**Decision: one workspace per resident (`baton-town/residents/<id>/` *is* that resident's nanobot workspace), selected by `--workspace`. Resident identity is derived from the workspace path, never from a session key. Processes follow upstream's existing rule: one gateway per (config, workspace).**

Why this, and not one gateway that switches resident per session:

- Upstream binds memory, context, skills, sessions, consolidator, subagents and cron to the loop's workspace in `__init__`. Switching resident per session would mean threading a resident through all of those — a large edit to `loop.py`/`runner.py`, which upstream's own design notes call the critical core path. Per-workspace routing needs **zero** changes there beyond the context-builder line.
- Upstream already gives per-workspace isolation for free: separate session stores (`sessions/<workspace-id>/`), separate cron stores, and `restrictToWorkspace` scoping file tools to that resident's own directory — which is exactly the privacy boundary the spec asks for.
- It is the reference fork's model too ("per-resident processes"), which is proven to run a town.
- It makes the "no accidental residents" guarantee structural: a workspace is only accepted as a resident if `REGISTRY.md` lists it as active **and** its `.baton-resident` marker exists. Both are written only by an approved founding. Any other path fails closed with an error; nothing is created. Session keys (Bryn's bug) carry no identity at all.

How the required behaviours map onto it:

| Requirement | How |
|---|---|
| Channels and CLI default to the Mayor | The town config sets `agents.defaults.workspace` to `residents/mayor`. The always-on gateway (systemd user unit) is the Mayor's. |
| `nanobot agent --as <resident>` | `cli/entry.py` rewrites `--as <id>` to `--workspace <town>/residents/<id>` (after checking the registry). Upstream then starts/attaches that workspace's own gateway. |
| Bind a channel to a resident | `baton.channelBindings: {"telegram": "archivist"}`. `nanobot baton serve` starts one gateway per resident that has a binding, each with a generated per-resident config containing only its bound channels; the Mayor's config gets the rest. Generated configs live under `~/.nanobot/baton/` (they hold secrets), never in the town repo. |
| Visits, night turns, checkpoint/fold turns | Run in-process by Baton through the SDK-style `AgentLoop` for the target workspace, on a dedicated session key (`visit:<from>`, `night:<date>`, etc.) so they never contend with a live chat session in another process. |
| Global model queue | `nanobot baton queue`: a tiny local OpenAI-compatible proxy in front of llama-server (aiohttp, already a dependency). Every resident config points at it. It holds a semaphore of `slots` (default 1), orders requests by priority (interactive > visit > night), groups by turn via a header Baton sets, and enforces timeouts. Because it is a process boundary, it covers every process and will cover OpenCode in phase two. Run as a systemd user unit next to the Mayor's gateway. `doctor` checks it. |
| Everything tagged with resident id | The event log, checkpoints and exports live inside the resident's directory and also carry `resident` in every record; queue logs carry it from the header. |

Cost: each live gateway is a separate Python process (~150–250 MB RAM). Only the Mayor's runs all the time by default; others start on `--as`, on a channel binding, or for the night.

## 4. Upstream edits (all marked `# BATON:` / `// BATON:`)

| File | Edit | Why it can't live in `nanobot/baton/` |
|---|---|---|
| `nanobot/config/schema.py` | `baton: BatonConfig` field on `Config`; Dream `enabled` defaults to `false` | Root schema assembles sub-configs; `BatonConfig` itself lives in `nanobot/baton/config.py`. |
| `nanobot/agent/loop.py` | (a) `loop_classes(...)` picks `BatonContextBuilder`/`BatonConsolidator` for a resident (and fails closed for an unfounded one); (b) `attach(loop, config)` at the end of `from_config` | Only point every composition root shares; both classes must be chosen at construction because `AutoCompact` and `SubagentManager` keep references to them. Ordinary workspaces get upstream's own classes back, so upstream tests that patch them are unaffected. |
| `nanobot/session/location.py` | refuse to create session storage for a workspace inside `residents/` that isn't a founded resident | The one place every composition root creates a workspace directory; keeps "nothing creates a resident" true even for a mistyped `--workspace`. |
| `nanobot/utils/helpers.py` | skip `sync_workspace_templates` for resident workspaces | Avoids SOUL/USER duplicates and a nested git repo. |
| `nanobot/cli/commands.py` | `app.add_typer(baton_app, name="baton")` | CLI registration. |
| `nanobot/cli/entry.py` | `--as <id>` → `--workspace …` rewrite | `agent` is dispatched before the Typer app loads. |
| `nanobot/command/builtin.py`, `nanobot/channels/telegram/runtime.py` | Dream commands neither offered nor routed | Dream is off **and hidden** (Squid's decision). |
| `nanobot/webui/settings_runtime.py`, `webui/src/components/settings/SettingsPage.tsx`, `webui/src/components/settings/system/runtime-config-fields.ts` | no Dream switch in Settings; the setting is not editable from the WebUI | Same. |
| `pyproject.toml` | ship `nanobot/baton/templates/**` in the wheel | Packaging. |
| upstream tests | tests updated to expect Dream off and hidden: `tests/command/*`, `tests/cli/test_commands.py` (gateway stubs), `tests/webui/test_settings_*.py`, the runtime-config keys fixture, one WebSocket channel test, two WebUI settings tests | They asserted the old default (Dream on and visible). |

Everything else — hooks, tools, gate, queue, search, night, review, MCP server, installer — is new code under `nanobot/baton/`, `install/`, `docs/baton/` and `tests/baton/`.

## Decisions made with Squid after phase 0

- **Dream:** left in the code (rebase-friendly) but off by default and hidden from menus, help, Telegram's command list and the WebUI settings.
- **Projects:** every resident has a private `residents/<id>/projects/<name>/`. A promoted project moves to `commons/projects/<name>/`, where every resident can work on it. Project folders are their own git repositories and are ignored by the town repo, so the town's history never embeds another repository.
- **Tools: one implementation, two adapters.** Residents get the Baton tools in-process (identity is the loop's workspace, so it can't be spoofed, and there's no extra process per resident). `nanobot baton mcp --as <id>` serves the same tools over MCP for OpenCode. This is how upstream nanobot treats its own tools (in-process registry, MCP for external servers) and how the reference fork treats its house tools (an MCP server with identity resolved from the environment).
- **Tool budget:** to stay near ~12 tools, `read_skill` is folded into `read_note("skill:<name>")`.

## 5. Spec → module map

| Spec | Module (planned) | Attaches via |
|---|---|---|
| 1 Town layout, registry, founding, privacy | `baton/town.py`, `baton/registry.py`, `baton/founding.py`, `baton/scope.py` | CLI; `attach()` validation |
| 1 SOUL/USER/MEMORY mapping | `baton/context.py` (`BatonContextBuilder`) | loop edit (a) |
| 2 Offices, crew | `baton/offices/*.md`, `baton/crew.py` | subagent hook factory files crew transcripts under `crew/<resident>/` |
| 3.1 Event log | `baton/events.py` (`EventLogHook`) | `hook_factories` added by `attach()` |
| 3.2 Triggers | `baton/triggers.py` | same hook; pre-compaction trigger from the consolidator wrapper |
| 3.3 Checkpoint turns + mechanical fallback | `baton/checkpoint.py`, `baton/schema_client.py`, `baton/schemas.py` | hook `after_run` / `after_execute_tool` |
| 3.4 Commit gate | `baton/gate.py` | shell tools wrapped in the registry |
| 3.5 Restore-or-nothing | `baton/compaction.py` (`BatonConsolidator`) | loop edit (a) |
| 3.6 Exact-context view | `baton/context_view.py` | `nanobot baton context <id>` builds messages exactly as `AgentLoop` would, prints bytes |
| 4 Night cycle | `baton/night/*.py`, `install/systemd/*` | CLI + systemd user timer |
| 5 Search | `baton/search.py` (FTS5, one `SearchBackend` interface) | tools + `reindex` |
| 5 Tools / `baton-mcp` | `baton/tools/core.py` (one implementation), `baton/tools/nanobot_adapter.py`, `baton/mcp_server.py` (stdio adapter) | registered in-process for residents; MCP server for OpenCode/phase two |
| 5 Start bundle | `baton/bundle.py` | `BatonContextBuilder` |
| 6 Growth, review, notes, town | `baton/growth.py`, `baton/review.py`, `baton/cli.py` | CLI |
| 7 Charter, HOUSE | `baton/templates/CHARTER.md`, `HOUSE.md` | founding |
| 8 Mock server | `tests/baton/mock_llama.py` | tests |
| 9 Install kit, doctor | `install/NN-*.sh`, `baton/doctor.py` | runbook |

**Baton tools: in-process, not a subprocess.** The spec puts the resident tools in an MCP server. For residents inside nanobot, Baton registers the same tool implementations directly in the loop's registry: identity comes from the loop's workspace (it can't be spoofed by a tool argument), there is no extra process per resident, and failures surface as ordinary tool errors. `nanobot baton mcp --as <id>` exposes the identical tools over stdio for OpenCode (phase two) and for `doctor`. One implementation, two adapters.

## 6. Ideas borrowed from the reference fork

| Idea | Their form | Baton's form |
|---|---|---|
| No Dream; continuity is authored | Dream excised | Dream left in code, forced off; checkpoint/fold turns authored by the resident |
| Restore-or-nothing compaction | Byte backup of JSONL session, restore on exception | Hashed export from SQLite + reject raw-dump fallback, relying on upstream's transactional commit |
| Exact-context view | WebUI endpoint | CLI `baton context` (works for a non-programmer over SSH) |
| Per-resident processes | `--workspace`/`--port` per resident | Same, plus registry-gated identity and a shared model queue |

Not brought over: the signing/witness system, the newspaper (townline), the Evennia bridge, the strings layer and WebUI changes.

## 7. Phase-two design constraints (OpenCode plugin)

Kept callable from outside nanobot so `integrations/opencode/` can reuse them without nanobot's loop: town layout and registry (`baton/town.py`), event-log record format (`baton/events.py`, JSONL, documented schema), checkpoint schema (`baton/schemas.py`, plain JSON Schema), bundle assembly (`baton/bundle.py`, a pure function of the town directory and resident id), and the tools via `baton mcp`.


## 8. What was built, by phase

| Phase | Modules |
|---|---|
| 1 | `town`, `registry`, `founding`, `scope`, `notes`, `search`, `ledger`, `inbox`, `bundle`, `context`, `tools/core`, `tools/nanobot_adapter`, `mcp_server`, `attach`, `loop_support`, `cli_support`, templates |
| 2 | `events`, `secrets`, `runtime_state`, `schemas`, `model_client`, `checkpoint`, `hooks`, `gate`, `compaction`, `queue`, `context_view` |
| 3 | `night`, `tooltest`, `units`, systemd templates |
| 4 | `growth` (review decisions, project promotion, notes, flags), `tools/active`, `configs` (channel bindings), visits in `attach` |
| 5 | `setup` (init-config), `doctor`, `install/`, `docs/baton/RUNBOOK.md`, `docs/baton/README.md` |

Commands: `nanobot baton found | residents | town | bundle | context | search | reindex | mcp | queue | night | review | note | units | configs | init-config | doctor | restore-session`, and `nanobot agent --as <resident>`.
