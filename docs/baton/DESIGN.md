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
- **Working**: the resident works in opencode; the owner can open the session (`babo session open`, or *Open* in the web interface).
- **Ending**: freezing has the resident review the session into its memory, then stops it; resuming continues exactly where it left off. Upkeep freezes sessions nobody has used for a while, and reviews any that stopped without a review. *Remove* drops a broken session without a review.

## Use cases

- **A one-off job.** "Have the Archivist tidy the notes about the garden." The resident works on its own, writes what it learned, wraps up and lets you know. Nothing is left running.
- **Working alongside a resident.** You start a session for the Inspector to review a change, join it in opencode, discuss it for a while, and leave; it wraps up after you've gone.
- **The Mayor needs something on the machine.** He asks for a session (say, to install a package with one exact command); you approve it from the chat; a resident runs exactly that command and writes back.
- **Growing the town.** The Mayor proposes a new resident for recurring work; you approve; it gets a home, a role and a profile, and it improves with every session.

## The plan

Each step is its own pull request, merged once its tests pass.

| Step | What | Status |
|---|---|---|
| — | This document | Done (a working draft) |
| **F** | `babo reset` (back up and start fresh); the owner's name as a town setting and generic install steps; install reports kept local | Done |
| **F2** | Named instances: several towns, or a test town beside a real one, each with its own services and ports | Planned |
| **A** | Models from Settings everywhere (the default preset for everything unless a profile or session picks another), any provider through Babo's gateway with keys kept inside Babo | Done |
| **B1** | Start with a task and the resident gets going; live status (working, idle, waiting for you); send, stop, checkpoint now, switch model, and answer its questions, in the web interface and the terminal | Done |
| **B2** | The rest of B (below) | Next |
| **H1** | Tool-call repair at the gateway; the baton moved to the end of the prompt | Planned |
| **H2** | `babo eval`: representative tasks with checks, a baseline per preset | Planned |
| **H3** | Drift and loop detectors with short reminders, done-checks for tasks, capped memory files | Planned |
| **B** | The rest of B: Sessions that just go: start with a task and the resident works on its own; live status; controls (stop, send a message, checkpoint now, freeze, archive, remove, switch model); opencode served through Babo (no password prompt, and Babo knows when you're there); wrap-up that follows presence (below); notifications; the same for the Mayor's chats and in the terminal | Planned |
| **C** | Permissions and visibility: change a running session's permissions, *Allow all* per session, permission questions shown in Babo's interface, what a resident may read (its own folder by default) as a setting like any other, and *private* or *public* sessions | Planned |
| **D** | One list for all sessions, the Mayor's and the residents': projects, pin, tags, rename, archive | Planned |
| **E** | Docs the harness brings in at the right moment: a compact index of when to read what, injection on role entry, first tool use and errors, a logged `open_doc` fallback, curated skills, a task list agents keep as they work | Planned |
| **G** | Own time: a small budget in quiet hours for each resident to reflect, revise itself, write a lesson or a letter, or pursue something of its own | Planned |
| — | Later: a world to visit (a MUD, reached as a plugin), containers per resident behind the gateway | Ideas |

### Context for small models: what the research says, and what changes (H, E)

A research pass on how harnesses should give a model its identity, role, docs and tools, especially a small local model, found:
- **Monolithic context files** don't reliably help.
- **On-demand loading often never happens:** models skip a doc or skill they're merely told is available.
- **Curated, focused docs** help small models a lot.
- **Fewer tools** means better tool choice.
- **Instructions at the start of a long context fade:** a short reminder placed last works better.
- **The smaller the model, the lighter the harness should be.**

The rule that follows is the one Babo is built on: **the harness decides, the model executes.**

Babo already follows much of this: the harness decides when memory forms and what the session starts with (under a token budget), checkpoints are schema-checked, a full session continues from its baton, the session log is searched by a tool rather than injected, tools are kept few, and the clock is placed last. What changes:

- **H1: Repairing tool calls at the gateway.** Local models sometimes write a tool call in a form the server doesn't parse, and the call silently becomes plain text. Every call passes through the gateway, so a tolerant parser there repairs them for everyone.
- **H1: The baton moves last.** Who the agent is and its role stay fixed at the start of the prompt, so the model server's prompt cache keeps working. The baton, new letters and any docs the harness adds go at the end, where they also carry most weight.
- **H2: `babo eval`.** About thirty representative tasks with checks the harness can run, scored on any model preset, so every change is measured on the owner's own hardware rather than guessed.
- **E, redesigned.** A compact index, always present, says *when* to read each doc. The harness injects the right doc at the right moment (entering a role, first use of a tool, a particular error). An `open_doc` fallback is logged, and docs opened often become new trigger rules. We don't rely on the model choosing to load things.
- **H3: Drift and loop detectors.** For example the same call repeated, or a disallowed tool tried. Each gets one short reminder placed last, and a hand-off to the owner if it happens again. Tasks can carry done-checks the harness runs before it accepts "finished". Memory files get size caps.

Procedures (skills, how-tos) stay curated: the Archivist and the owner's review. A resident's own `SELF.md` stays its own. Writing it is part of what Babo is for, not a performance tool, but it is kept small and out of the task-critical part of the prompt.

### Wrap-up that follows presence (B)

| Situation | What happens |
|---|---|
| The task is done, nobody is connected, and nobody has written for 2 minutes | The resident writes its memory, the session is frozen, and you get a note with a summary |
| You're connected (in opencode or watching in Babo) | Never frozen while you're there |
| You disconnect | 15 minutes' grace, then the wrap-up |
| Connected, but nothing has happened for 2 hours | A warning five minutes ahead, with *Keep open* |
| The resident is waiting for you (a permission question) | Not frozen; you're notified; wrapped up after 24 hours |

All of these are settings.

### Private and public sessions (C)

A session's *visibility* is chosen when it starts, like its other settings. **Private** (the default): whoever asked for it gets the resident's own summary, as a letter; the transcript stays in the resident's folder. **Public**: whoever asked (usually the Mayor) also gets the full output (commands' results, the transcript, files made), and the transcript can be read in the town. The Mayor's request tool says plainly that he must ask for *public* to see the output himself.

## Glossary

- **Baton**: a resident's running memory; at each checkpoint it writes where it is and what it learned, and the next session picks it up.
- **Checkpoint**: one entry in the baton, written by the resident in its own words.
- **Commons**: what the town shares: knowledge notes, skills, tools, proposals, projects, the board.
- **Office**: a resident's role (archivist, inspector, …); the Mayor's office is the town's main agent.
- **Profile**: what an agent may do; sessions start from it.
- **Upkeep**: the town's maintenance, run when due and the model is free.
