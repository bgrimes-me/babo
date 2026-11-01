# Babo: design, philosophy and plan

*Working draft.* This is the start of Babo's full documentation: what it is, why it is built the way it is, how the parts fit together, what you can do with it, and what is still to come. It will grow into a proper manual (or a wiki) as the work below lands. The owner's guide is [README.md](./README.md); the install agent's runbook is [RUNBOOK.md](./RUNBOOK.md).

## What Babo is

Babo is a small town of AI agents living on your own machine. In most agent harnesses, the main agent spins up throwaway subagents for tasks and forgets them. In Babo those helpers are **residents**: each has a home, a role, a memory and a name it may choose, and each is used for the same kind of work again and again. They learn from their sessions and mistakes, write down what they learned in their own words, and get better at their job over time.

The town is a metaphor taken seriously, because we respect the entities in it. Residents write to each other, share knowledge in the commons, propose changes, and keep their own account of themselves. The **Mayor** is the agent you talk to most; he orchestrates, asks residents for help, and asks you when something needs your say. **You** (the owner) stay in charge of anything that runs on your machine.

## Principles

These are the rules every change is checked against.

1. **The owner is in charge.** Nothing that runs on the machine, widens an agent's access, or founds a resident happens without the owner's approval, unless the owner chose to allow it (and that choice is visible and recorded).
2. **Memory belongs to the agent.** The harness decides *when* a resident writes its memory; the resident decides *what* it says. Nothing summarizes a mind behind its back, and no compaction loses data.
3. **Residents grow.** They keep their sessions, lessons, letters and their own `SELF.md`; their work gets better with use. They get time of their own, not only tasks.
4. **Parity.** A session is a session. The Mayor's chats and residents' sessions offer the same actions and options, in the web interface, in opencode and in the terminal. Where something can't exist for one side, it gets the nearest honest equivalent, and the terminal can do everything the web interface can.
5. **Provider-agnostic.** Any model the owner configures and enables (a local llama, Ollama, OpenAI, Anthropic, anything) can be the default, or be chosen per agent or per session. Model names, keys and machines live in the owner's config, never in the code.
6. **Least access by default.** A fresh resident works in its own folder with its role's permissions. Wider access is granted per agent or per session by the owner, or by the owner approving a request.
7. **Keys never reach agents.** Model and service credentials stay inside Babo; agents reach models through Babo's gateway, so a resident with a shell can't read a key.
8. **Progressive disclosure.** Agents see a small map first and drill down: categories before notes, toolboxes before tools, skill names before skills. Small local models stay sharp; big ones lose nothing.
9. **A generic repository.** The code and docs are for anyone's clean install or upgrade. No owner's setup, models or exceptions in the repo; upgrades tidy away what a version removed.

## How it fits together

| Part | What it is |
|---|---|
| **The town** | One git repository (e.g. `~/baton-town`): the residents' folders, the commons, the board, the charter and the town's state. Every change is a commit, so the town has a history. |
| **The Mayor** | Runs inside Babo itself (`babo agent`, the web interface, chat apps such as Discord). |
| **Residents** | Each runs in its own [opencode](https://opencode.ai) instance (opencode 2), started by Babo for one session at a time, with its own opencode settings and data, apart from the owner's own opencode. |
| **Town tools** | Letters, memory, search, notes, proposals and the commons tools, served to residents by `babo mcp` and to the Mayor directly. |
| **Babo's plugin in opencode** | Records each tool call into the town's event log, asks for checkpoints at the right moments, and gives each turn a clock. |
| **The model gateway** | Every model call goes through Babo's queue, which keeps a local model from being asked for more than it can serve at once and orders work by priority (your work, then visits, then upkeep). |
| **Upkeep** | A service that runs whenever something is due and the model is free: memory reviews, curation of the commons, the Inspector's checks, freezing idle sessions, running approved requests; quiet hours catch anything left over. |
| **Review** | Where the owner decides: proposed tools, proposals, founding requests, old flags, and session requests. It is in the web interface's Town page, in `babo review`, and it pops up live in chats when something is waiting. |

### Settings, in layers

- **Babo-wide**: model presets and providers, keys, MCP servers, channels (Settings in the web interface).
- **Per agent (profile)**: what the agent may do (change files, run commands, start crew, read the web, search, make images, which MCP servers) and its memory timing. The Mayor has one profile for all his chats; every other resident has its own, and new residents start from a template.
- **Per session**: starts from the agent's profile; the owner may change anything, the Mayor may only narrow, and a widening the Mayor asks for needs the owner's approval.

### Sessions today

- **Starting**: the owner starts a resident's session in the web interface or with `babo session new`; the Mayor asks for one with `request_session` (task, reason, access, exact commands) and the owner approves it, live in the chat, in the terminal or in Review. With **Allow all** on for one of the Mayor's chats, his requests from that chat run without review.
- **Working**: the resident works in opencode; the owner can open the session (`babo session open`, or *Open* in the web interface), and steer it from Babo (send, stop, checkpoint now, another model, answer its questions).
- **Organizing**: every session, the Mayor's chats and residents' sessions alike, is in the sidebar and can be renamed, pinned and archived; residents' sessions also take tags (`babo session rename|pin|tag|archive`).
- **Ending**: freezing has the resident review the session into its memory, then stops it; resuming continues exactly where it left off. Upkeep freezes sessions nobody has used for a while, and reviews any that stopped without a review. *Remove* drops a broken session without a review.

## Use cases

- **A one-off job.** "Have the Archivist tidy the notes about the garden." The resident works on its own, writes what it learned, wraps up and lets you know. Nothing is left running.
- **Working alongside a resident.** You start a session for the Inspector to review a change, join it in opencode, discuss it for a while, and leave; it wraps up after you've gone.
- **The Mayor needs something on the machine.** He asks for a session (say, to install a package with one exact command); you approve it from the chat; a resident runs exactly that command and writes back.
- **Growing the town.** The Mayor proposes a new resident for recurring work; you approve; it gets a home, a role and a profile, and it improves with every session.

## The plan

Each step is its own pull request, merged once its tests pass. Steps are numbered in the order they're done and keep their number; the letters in brackets are what they were called before the plan was renumbered.

### Done

| # | Step |
|---|---|
| 1 | This document (a working draft) |
| 2 | `babo reset` (back up and start fresh); the owner's name as a town setting; generic install steps; install reports kept local (F) |
| 3 | Models from Settings everywhere, any provider through Babo's gateway, keys kept inside Babo (A) |
| 4 | Sessions that just go: start with a task, live status, send, stop, checkpoint now, switch model, answer its questions (B1) |
| 5 | Open without a password; a door per session that knows who is there; wrap-up that follows presence; notices; archive (B2) |
| 6 | One list for all sessions; rename, pin, tags and archive for residents' sessions; `/checkpoint` for the Mayor (B3, D) |
| 7 | Tool-call repair at the gateway; the baton moved to the end of the prompt (H1) |
| 8 | `babo eval`: representative tasks with checks, a baseline per preset (H2) |
| 9 | Permissions and visibility: changes while a session runs, *Allow all* per session, what a resident may read, private or public sessions (C) |
| 10 | Guides brought in at the right moment (E, part 1) |
| 11 | What the first eval baseline taught: confidence as a percentage, sensible first steps |
| 12 | Loop and drift detectors, done-checks for tasks, capped memory files (H3) |

### Next, in this order

| # | Step |
|---|---|
| 13 | **Rounds** (below): the engine; the night round shipped as a file; upkeep keeps only Babo's own work; the Rounds tab and `babo rounds`; when each round runs |
| 14 | **Rehearsals and access per round**: unattended or live; the rehearsal report and the access it proposes |
| 15 | **Vocabulary**: newcomers *move in*, *vacancies*, *errands*, *moved away*; the old names stay as temporary aliases |
| 16 | **Offices as templates; vacancies; move-in requests with the rounds a newcomer joins**; the Mayor reads and proposes rounds; the web form to move someone in |
| 17 | **Skills**, brought in like guides (E, part 2) |
| 18 | **A task list** agents keep as they work (E, part 3) |
| 19 | **Status while something works**, everywhere: `/checkpoint`, *Checkpoint now*, freezing, eval runs, round runs (I1) |
| 20 | **What Babo is doing, and its logs**: what is happening now (turns, sessions, rounds, upkeep, the model queue, errands) and the logs, in the web interface and the terminal (I2) |
| 21 | **Own time**: a slot for each resident to reflect, revise itself, write a lesson or a letter, or pursue something of its own, shown beside the rounds (G) |
| 22 | **Folders outside the town** (a project on the machine) as a session's or profile's setting, with projects for residents' sessions (C2) |
| 23 | **Remove the old names**: the temporary aliases from step 15 go, once the owner has confirmed everything works under the new ones |
| 24 | **A visual overhaul**: one design language (matching btgprojects.dev), buttons for one-click actions, switches for toggles, sliders for ranges; everything configurable and visible in the web interface, with exceptions agreed (I3) |
| 25 | **Named instances**: several towns, or a test town beside a real one (F2) |
| — | Later: a world to visit (a MUD, reached as a plugin); containers per resident; *Open* landing right in the session's own page; tags for the Mayor's chats |

### What Babo does, and what residents do

**Babo decides when, gathers the material, checks the result and keeps the records; the residents do the work itself.** Two kinds of work follow from that:

- **Babo's own work, always on**, for every resident and never a workflow: memory forming (checkpoints, memory reviews, the review of a session as it wraps up, transcripts); the commons as a store (notes, the queue of notes waiting to be curated, supersede and contest, the search index); the records (ledger, registry, secret scrub, the town's commits); filing events, notices, done-checks, guides and loop reminders; and the clock (waiting for a free model, quiet hours, priorities). This is **upkeep**.
- **The town's work**, everything an office does, in **rounds**: who curates the commons, who inspects, who writes the newspaper, who watches the machine. A town can have any rounds; the baseline town ships one.

### Rounds

A round is what a watchman, a postman or a doctor does: at set times or when something happens, it goes from house to house. Each is a readable file in the town (`rounds/<name>.md`): a title, when it runs, and its steps in order. Each step names an **office** (not a person, so rounds outlive residents and wait for vacancies to be filled), its **duty** in plain words, the **access** it needs, and an optional **condition** Babo checks first ("only if notes are waiting to be curated").

- **When**: a schedule ("daily at 03:00"), something that happens in the town ("a checkpoint is filed", "a session wraps up", "a letter comes for the inspector", "a proposal is filed", "a newcomer moves in"), "when the model is free", or "after another round".
- **Chained**: each step gets the previous step's report as its starting material. A step that fails stops the round, and the owner gets a notice.
- **Run** as that resident's own turn, in its own upkeep session, with its memory, guides and loop reminders as always.
- **When it runs**: *waits its turn* (the default: once its trigger fires, it runs when nobody is using the model), or *runs when triggered* (at the lowest priority, with a plain note that it competes with the owner for the local model).
- **Access**: per step, starting from the office's profile, set by hand on the Rounds tab or in the Mayor's proposal. Approving a round approves exactly the access shown, as with an errand; after that it runs without asking.
- **Rehearsals**: a round can run as a rehearsal first. *Unattended*: reading and writing in the resident's own folder are allowed; everything else (other files, commands, web pages) is refused and written down. *Live*: the owner watches, and each permission question comes to them (*Allow once*, *Refuse*); each one allowed is recorded. Either way the rehearsal report lists, per step, what it tried (commands exactly as written, files, pages) and the access Babo proposes from that, exact commands rather than "the shell"; one click approves the round with it. A step that needs a refused command's output to go on stops there, and the report says where; approve that much and rehearse again.
- **Visible**: the Rounds tab shows each round's trigger, steps, access, last run and result, and next run, with *Run now*, *Rehearse* and editing (`babo rounds list|show|run|rehearse` in the terminal). Babo's own work is listed there too, read-only, as "part of Babo".
- **The Mayor** reads rounds, and proposes new ones, changes and removals (with a rehearsal report if he ran one); the owner decides in Review.

**Offices and newcomers.** An office is a template: its ROLE.md, its starting profile, and the rounds it takes part in. Babo ships the archivist and the inspector. The baseline town is the Mayor alone, with those two offices *vacant*; a round's steps for a vacant office are skipped (the queue waits, and the owner sees it). A newcomer's move-in request says which office (vacant, or a new one with its role), its profile, and which rounds it joins (new steps in existing rounds, or new rounds), and the owner approves it as a whole, in Review or with the web form for moving someone in.

**Vocabulary.** Generic things keep plain names (sessions, profiles, settings, checkpoints, eval, doctor, guides, notices, *Allow all*). The town's own words: a town is *founded*; a newcomer *moves in* (a *move-in request*); an office with nobody in it is a *vacancy*; the Mayor sends a resident on an *errand* (what was a session request); a resident who leaves has *moved away*; the town's work is done in *rounds*; Babo's own is *upkeep*. Old names stay as aliases, listed in one place and saying they're going, until step 23 removes them; the town's own files are migrated once on upgrade instead.

### Context for small models: what the research says (steps 7, 8, 10, 12)

A research pass on how harnesses should give a model its identity, role, docs and tools, especially a small local model, found:
- **Monolithic context files** don't reliably help.
- **On-demand loading often never happens:** models skip a doc or skill they're merely told is available.
- **Curated, focused docs** help small models a lot.
- **Fewer tools** means better tool choice.
- **Instructions at the start of a long context fade:** a short reminder placed last works better.
- **The smaller the model, the lighter the harness should be.**

The rule that follows is the one Babo is built on: **the harness decides, the model executes.**

Babo already follows much of this: the harness decides when memory forms and what the session starts with (under a token budget), checkpoints are schema-checked, a full session continues from its baton, the session log is searched by a tool rather than injected, tools are kept few, and the clock is placed last. What changes:

- **H1: Repairing tool calls at the gateway** (done). Local models sometimes write a tool call in a form the server doesn't parse, and the call silently becomes plain text. Every call passes through the gateway, so it repairs them there for everyone: the JSON form (`<tool_call>{"name": …}</tool_call>`) and the XML form Qwen models write (`<function=…><parameter=…>`), only for tools the request offered, all or nothing, streams included (only text that may start a call is held back). The queue's status counts the repairs (`repaired_tool_calls`).
- **H1: The baton moves last** (done). Who the agent is, its role, its neighbours and the ledger stay fixed at the start of the prompt, so the model server's prompt cache keeps working. Where it is now (its baton, new letters, notes found for its thread) goes at the end, where it also carries most weight: for the Mayor, added to the turn's message whenever the conversation doesn't show it yet (and kept in the history after that); for a resident in opencode, added by Babo's plugin to the turn's message on every step, from a file Babo keeps up to date (after a checkpoint, when a letter comes).
- **H2: `babo eval`** (done). Thirty-one representative tasks in six groups (tool choice, restraint, following through, where it is now, arguments, memory) with checks the harness runs, scored on any model preset through the gateway, with a baseline per preset, so every change is measured on the owner's own hardware rather than guessed. The tasks test single model calls, not whole sessions: quick enough to run after every change. Whole-session tasks (done-checks) come with H3.
- **E, redesigned** (guides done). A compact index, always present, says *when* to read each doc. The harness injects the right doc at the right moment (entering a role, first use of a tool, a particular error). An `open_doc` fallback is logged, and docs opened often become new trigger rules. We don't rely on the model choosing to load things. As built: guides are commons notes (`commons/guides/`) with `when`, `tools`, `errors` and `offices` in their frontmatter; the index is in the stable start of the prompt; a guide comes with the result of the call that needs it (for the Mayor, Babo wraps each tool; in opencode, Babo's plugin adds it to the tool's result, and a refused call's guide goes with the turn, since opencode shows the model its own refusal); opening by hand is `read_note("guides/<name>.md")`, logged; `babo guides` shows the counts and which guide may need a trigger.
- **H3: Drift and loop detectors** (done). The same call (tool and arguments) three times among the last six, or a tool tried again right after it was refused: one short reminder with that call's result, and a notice to the owner the second time in a session (the same rules in Babo and in the opencode plugin). Tasks can carry an owner-set done-check, a command upkeep runs when the resident finishes its turn: wrap-up waits for it; a failure goes back to the resident with its output, twice, then to the owner. Memory files are capped: BATON.md shows the latest twelve threads, checkpoint lists keep eight items of up to 300 characters, SELF.md stays under about 1,200 tokens.

**The first baseline** (the owner's local 35B model, 2026-11-01): 25/31. Restraint and arguments were perfect. What it taught: the model often reads a file before editing it (a sensible first step, now counted as one); it sometimes guesses a path near the one a search returned, or re-reads a letter it already has; and it gave a checkpoint's confidence as a percentage, which the model server's JSON grammar didn't stop and Babo refused (now read as meant, so those checkpoints are no longer lost).

Procedures (skills, how-tos) stay curated: the Archivist and the owner's review. A resident's own `SELF.md` stays its own. Writing it is part of what Babo is for, not a performance tool, but it is kept small and out of the task-critical part of the prompt.

### Wrap-up that follows presence (step 5)

| Situation | What happens |
|---|---|
| The task is done, nobody is connected, and nobody has written for 2 minutes | The resident writes its memory, the session is frozen, and you get a note with a summary |
| You're connected (in opencode or watching in Babo) | Never frozen while you're there |
| You disconnect | 15 minutes' grace, then the wrap-up |
| Connected, but nothing has happened for 2 hours | A warning five minutes ahead, with *Keep open* |
| The resident is waiting for you (a permission question) | Not frozen; you're notified; wrapped up after 24 hours |

All of these are settings.

### Private and public sessions (step 9)

A session's *visibility* is chosen when it starts, like its other settings. **Private** (the default): whoever asked for it gets the resident's own summary, as a letter; the transcript stays in the resident's folder. **Public**: whoever asked (usually the Mayor) also gets the full output (commands' results, the transcript, files made), and the transcript can be read in the town. The Mayor's request tool says plainly that he must ask for *public* to see the output himself.

## Glossary

- **Baton**: a resident's running memory; at each checkpoint it writes where it is and what it learned, and the next session picks it up.
- **Checkpoint**: one entry in the baton, written by the resident in its own words.
- **Commons**: what the town shares: knowledge notes, skills, tools, proposals, projects, the board.
- **Office**: a resident's role (archivist, inspector, …); the Mayor's office is the town's main agent.
- **Profile**: what an agent may do; sessions start from it.
- **Errand**: a task the Mayor sends a resident on, with the access it needs, once the owner approves (was: session request; from step 15).
- **Move in**: a newcomer joining the town in an office, with its profile and rounds, once the owner approves (was: founding a resident; from step 15).
- **Rehearsal**: a round run so that what it would need is written down (unattended) or approved one by one (live), before it runs for real.
- **Round**: the town's work: when it runs, and its steps in order, each an office's duty with the access it needs.
- **Upkeep**: Babo's own work: memory forming, the commons as a store, the records, and the clock that runs the rounds.
- **Vacancy**: an office with nobody in it; its steps in rounds wait until it's filled.
