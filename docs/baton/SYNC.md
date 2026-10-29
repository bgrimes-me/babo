# Keeping the Baton fork in sync with upstream

This fork tracks `HKUDS/nanobot`. Baton code lives in its own places so upstream changes rarely collide with it:

- `nanobot/baton/` — all Baton runtime code
- `tests/baton/` — Baton tests
- `install/` — the install kit
- `docs/baton/` — Baton docs
- `.github/workflows/baton.yml` — Baton CI

Edits to upstream files are kept to a handful of lines, each marked with a `# BATON:` comment. List them at any time:

```bash
git grep -n "BATON:" -- ':!nanobot/baton' ':!tests/baton' ':!docs/baton' ':!install'
```

The expected list is in [integration-map.md](integration-map.md#4-planned-upstream-edits-all-marked--baton). If a sync adds or loses one, update that table.

## One-time setup

```bash
git remote add upstream https://github.com/HKUDS/nanobot   # skip if `git remote -v` already shows it
```

## Syncing

Do this on a branch and land it through a PR, never directly on `main`.

```bash
git fetch upstream main
git checkout -b sync/upstream-$(date +%Y-%m-%d) origin/main
git merge upstream/main          # merge, not rebase: keeps history readable for everyone
```

If there are conflicts, they will almost always be in one of the `# BATON:` lines. Resolve them by keeping upstream's new code and re-applying the Baton line in the equivalent place. If upstream has restructured the surrounding code so the line no longer makes sense, stop and re-read the relevant row of the integration map: the point is the behaviour, not the exact line.

Then check:

```bash
uv sync --all-extras --dev
uv run --no-sync python -m scripts.check_import_boundaries
uv run --no-sync pytest tests/baton -q        # Baton must stay green
uv run --no-sync pytest -q -x                 # upstream suite must stay green
```

Things to look at specifically after a sync:

| Upstream area | Why Baton cares |
|---|---|
| `agent/hook.py`, `agent/turn_hooks.py` | Event log and triggers are hooks. |
| `agent/memory.py` (`Consolidator`, `MemoryArchiver`) | `BatonConsolidator` subclasses these. A renamed method or a changed fallback breaks restore-or-nothing; the forced-failure test should catch it. |
| `agent/context.py` (`ContextBuilder`) | `BatonContextBuilder` subclasses it. |
| `agent/tools/shell.py`, `exec_session.py` | The commit gate wraps these tools by name. |
| `session/manager.py`, `session/state.py` | Checkpoint turns rely on `_command` messages being excluded from replay. |
| `config/schema.py` | The `baton` field and the Dream switch. |
| `utils/helpers.py` (`sync_workspace_templates`) | Must still skip resident workspaces. |

Push the branch and open a PR into `main`. CI runs both the Baton workflow and upstream's suite.
