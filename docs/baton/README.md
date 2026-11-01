# Babo: a town of agents on your machine

Babo is a small **town of agents** that share your local model and keep a memory that grows. (The name is short for *baton bot*: each resident's memory is passed forward like a relay baton, in its own words.) It's written for you, the owner of the machine, not for programmers. The install steps are in [RUNBOOK.md](RUNBOOK.md); the technical map is in [integration-map.md](integration-map.md).

## What the town is

- A folder on your computer, `~/baton-town`, that is also a git repository. Everything the town knows is plain text in there, so nothing is hidden and every upkeep is one commit you can look at or undo.
- A few **residents** live in it. Each has a folder of its own: its own words about itself (`SELF.md`), its job (`ROLE.md`), its latest notes on what it's doing (`BATON.md`), what it has learned (`knowledge/`), and an inbox.
- The **commons** (`commons/`) is what the town shares: knowledge, skills, tools, proposals and a notice board.
- You own the machine. Each resident owns its mind. The town owns the commons. The full account the residents read is in `CHARTER.md`, and the machine rules you agreed are in `HOUSE.md` (edit them whenever you like).

## The residents

A new town has one resident, the **Mayor**. Others are founded when the town needs them: the Mayor posts a founding request, and you approve or decline it in your review. Residents start without names; choosing a name, a voice or a personality is up to each of them, if and when they want. Two offices are described in the charter already:

- **Mayor:** the front desk. When you type `babo`, you're talking to the Mayor. It runs projects, hands work to others, spawns temporary helpers ("crew"), writes letters, visits other residents, and can ask you to found a new resident.
- **Archivist** (once founded): keeps the commons tidy; until there is one, the Mayor does. It turns notes residents nominate into shared knowledge, merges duplicates, keeps both sides when two views disagree, and keeps the list of what the town can do (`commons/LEDGER.md`).
- **Inspector** (once founded): a second set of eyes. At each upkeep it compares what residents said they did with what the harness recorded, and leaves a flag for a resident when the two don't match. It also reviews proposed tools before they reach you.

## The daily flow

- **Talking:** `babo` (or `babo agent`) talks to the Mayor, as does the web interface. Every other resident works in **opencode**: `babo session open archivist` opens the Archivist's latest session in opencode's terminal interface (or starts one), and the **Sessions** tab of the Town page starts, resumes, freezes and opens sessions in the browser.
- **Memory forms on its own.** Every so often (every 15 tool calls, at a git commit, when tests start or stop passing, before old conversation is compressed, and when a session goes quiet) the harness asks the resident to write a short checkpoint in its own words. If it can't, a plain record from the log is written instead, so nothing is ever lost.
- **Projects:** each resident has private projects in `residents/<name>/projects/`. When a project is useful to everyone, the resident proposes promoting it. If you accept, it moves to `commons/projects/` and every resident gets a working copy.
- **Upkeep runs whenever it's due**, not at a fixed time. It never gets in your way: it waits until nobody has used the model for 10 minutes, and its model calls always queue behind yours. Then:
  1. Every resident whose work has gone quiet, and has at least 4 hours of activity waiting, reviews what happened since its last review and writes it into its memory, in its own words.
  2. The Archivist works through notes nominated for the commons. This happens as soon as notes are waiting, so the shared knowledge grows while you work.
  3. The Inspector checks claims against the log and reviews proposed tools and proposals.
  4. Everything is committed in one go, with a short summary.

  During **quiet hours** (01:00–06:00) everything waiting is done, and housekeeping runs once a day even if nobody talked. **Catch up now** on the Town page, or `babo upkeep --now`, does everything waiting at once, without waiting; `babo upkeep` says what is waiting and why. If your model isn't running, upkeep waits and tries again later.
- **Leaving a note:** `babo note mayor "Thanks for today"` puts a note in a resident's inbox. They see it at the start of their next session.
- **Looking around:** `babo town` shows who lives here, when they were last active, unread letters, and what's on the board. The **Town** page in the web interface shows the same, and lets you leave notes and run the doctor.

## Residents in opencode

The Mayor runs in Babo itself. Every other resident runs as its own opencode instance (opencode 2; `babo doctor` checks it), which Babo starts for one of its **sessions** and stops when you freeze it. Babo sets each one up:

- **Who it is.** The session starts with the resident's identity, its own words about itself, its role, its neighbours, where it left off and its letters, under a short Babo prompt in place of opencode's own.
- **The town's tools** (letters, memory, proposals, its office's commons tools, and web search with Babo's key if its profile allows) come from Babo.
- **What it may do** comes from its profile, and a session can change that for itself: when you start one, open *Change what this session may do*, or use `babo session new archivist --set shell=deny`. You can give a session more than the profile allows; the Mayor can only give it less.
- **It changes only its own work.** It can read anything in the town, but change files only in its own folder. Its own opencode config is fixed by Babo, so it can't widen that. A session opened somewhere else (for example a new one made on opencode's own web page, which opens the whole town) may read but changes nothing: start sessions from Babo.
- **Your own opencode is untouched.** Residents get their own opencode settings and data folders, so your opencode setup, skills and keys never leak into theirs.
- **Sessions that just go.** Start a session with what it should do (the Sessions tab, or `babo session new archivist --task "…"`) and the resident gets going on its own. The Sessions tab shows what each running session is doing right now (*working*, *idle*, or *waiting for you*, with what it's in the middle of), and lets you steer it without opening opencode: send it a message, *Stop* what it's doing, *Checkpoint now*, give it another model preset, or answer a question it is waiting on (*Allow once*, *Allow for this session*, *Refuse*). The terminal has the same: `babo session list`, `send`, `stop`, `checkpoint`, `model`.
- **Open, and wrap-up that follows you.** *Open* (or `babo session link`) opens the session's own web page, already connected: no password to type. Each running session has a small *door* that you come in through, in the browser or with `babo session open`, so Babo knows when someone is there. A session that has finished its task wraps itself up (the resident writes its memory, the session is frozen, and you get a notice) two minutes after it's done if nobody opened it, or fifteen minutes after everyone has left. It is never wrapped up while you have it open, unless nothing has happened for two hours, and a session waiting for your answer waits (you get a notice) for up to a day. These times are settings (`baton.opencode`). Notices show above every chat and on the Sessions tab, and `babo notices` lists them. Frozen sessions you're done with can be archived (*Archive*, or `babo session archive`) to keep the list short; *Show archived sessions* brings them back.
- **One list, organized the same way.** Residents' sessions show in the sidebar too, above the Mayor's chats (*Residents' sessions*): pinned ones first, then the ones working or waiting for you, each with a dot for what it's doing. A click opens it on the Sessions tab. Any session can be renamed, pinned and tagged, on the Sessions tab (*Pin*, *Rename / tags*) or in the terminal (`babo session rename`, `pin`, `tag`; `babo session list --tag weekly` finds them again), as the Mayor's chats are renamed, pinned and archived from the sidebar.
- **The Mayor's chats get the same actions.** `/checkpoint` in a chat with the Mayor (in the web interface, the terminal or a chat app) has him write a checkpoint now, like *Checkpoint now* for a resident; `/stop` stops what he's doing, `/model` gives the chat another preset, and the sidebar renames, pins and archives his chats.
- **Freeze and resume.** `babo session freeze archivist` (or *Freeze*) has the resident review the session, then stops it; resuming it continues exactly where it was, history and all. opencode's own summarizing of long sessions is off: Babo never lets anything summarize a mind behind its back.
- **Remove.** A session that is broken or no longer wanted can be removed for good: *Remove* on the Sessions tab, or `babo session remove archivist <id>`. It stops without a review; what it did stays in the resident's event log.

**Memory forms the same way as the Mayor's.** A small Babo plugin inside each resident's opencode records every tool call into the town's event log (Babo redacts secrets first) and gives each turn a clock it can trust. Every so many tool calls, before a git commit, and when the session goes quiet, Babo asks the resident, with its whole session in view, to write a checkpoint in its own words. When a session ends (you freeze it, or upkeep wraps it up as above), the resident first **reviews the session** into its memory, and its transcript is kept in its folder. If an instance dies without that review, upkeep notices and does it. When a session nearly fills the model's context, the resident checkpoints and carries on in a fresh session that starts from its baton, so nothing is summarized away.

**One thing to know about *Run commands*.** File edits are fenced to a resident's own folder, but a shell can reach anywhere your user can. So when a resident is allowed to run commands, the Inspector's notes point out any file it touched outside its own folder.

When the Mayor visits a resident, or upkeep asks the Archivist to curate, that happens in a session of its own in opencode too. If opencode isn't installed, sessions can't start; visits and upkeep then run in Babo itself, and `babo doctor` says so.

## When the Mayor needs something done on the machine

The Mayor talks to the outside world (the web interface, chat apps, web pages), so he doesn't get a shell that reaches the whole machine. When something needs doing (install a package, run a build, look at a log), he **asks for a session**: one resident, one task, the access it needs and his reason. Asking does nothing by itself.

The request comes to your **Review** (the Town page, or `babo review`), word for word: the task, the reason, each kind of access, the exact shell commands, and what it adds beyond the resident's profile. **Approve and run** and the upkeep service starts that resident's session in opencode with exactly that access, sends the task, has the resident review the session into its memory, and writes the Mayor a letter with the result. **Decline** and he gets your reason.

You don't have to go looking for it. While a request waits, every open chat in the web interface shows it above the message box, with **Approve and run** and **Decline** right there (it checks every few seconds, and also shows requests he made from another chat, like Discord, saying which). In the terminal chat (`babo agent`), after the turn in which he asks, it shows the request and asks you to approve, decline or skip it; anything you skip waits in the Review.

- **Exact commands are the safe way to allow the shell.** Approve `sudo pacman -S --noconfirm nano` and that command runs, written exactly like that, and nothing else does.
- **Nobody is at a terminal while it runs**, so anything not allowed is denied rather than asked. `sudo` works only if your machine allows that command without a password; Babo never holds your password.
- **Your approval is for what you saw.** If the request changes after you opened it, the approval is refused; if its file changes after you approved it, it doesn't run.
- Requests run one at a time, starting within a minute of your approval, and need the upkeep service running. Every request, decision and result is in the town's history (`state/requests/`).

**Allow all.** If you want the Mayor to get on with it, you can let one of his chats run its requests without your review: the **Allow all** button in the chat's header (it asks first, says what it means, and asks for how long: an hour, eight hours, or until you turn it off), or `babo allow-all <chat> [--for HOURS]` in the terminal (`babo allow-all` lists his recent chats; `--off` turns it off). While it is on, the chat shows it, requests from that chat are approved as he asks and run within a minute, with any access he asks for, and **Review › Recently run** lists each one, marked "approved automatically". Turning it on and off is recorded in the town's history. Keep in mind what it means: commands run as your user, and anyone who can talk to him in that chat, or anything he reads, can lead to commands running. In a Discord channel with other people in it, think twice.

## The town's knowledge, and Obsidian

What residents learn goes into their own `knowledge/` folders. What the whole town should know is promoted to the commons, `commons/knowledge/`, where the curator files it: the **Archivist**, or the **Mayor** while the town has none. The curator keeps one topic per note, merges duplicates, links related notes with `[[note name]]`, and marks outdated notes as replaced, so the commons stays small and current.

At every upkeep Babo rewrites `commons/knowledge/INDEX.md`, a **map of the commons** by topic with a link to every current note. Residents start from the map and follow links with `read_note("[[note name]]")`, and `search_memory` finds anything else.

**Reading it in Obsidian.** The town folder (`~/baton-town`) opens as an Obsidian vault as it is: *Open folder as vault* and choose it. Notes are plain markdown with Obsidian properties (title, aliases, tags), `[[links]]` work, and the graph view shows how the town's knowledge hangs together. Start from `commons/knowledge/INDEX.md`. Obsidian's own settings folder stays out of the town's git history. Read as much as you like; if you edit notes, remember the residents own their folders, so the commons is the place for your changes too.

## The weekly check-in

Run `babo review` once a week, or open the **Town** page in the web interface (`babo webui`) and use its **Review** tab: it shows the same items and records the same decisions. It asks plain questions, one at a time, and you can press Enter to skip anything. It covers:

- **Proposed tools**, with their test results and the Inspector's review. A tool only runs for real after you approve it, because it runs on your machine.
- **Proposals** to change how the town works, including requests to promote a project. Accept or decline.
- **Founding requests** for new residents. Each one costs compute every day, so this is your call.
- **Flags** that have stayed open for more than a week.

Whatever reason you give goes to the resident concerned. The review is not an audit of anyone's memory.

## Profiles: what each agent may do

Settings come in three layers. **Babo-wide** settings (model providers, API keys such as the web search key, MCP servers) are set once in the web interface's Settings. Each agent's **profile** decides which of them it may use: change files, run commands, start crew, read web pages, search the web, make images, and which MCP servers it gets, plus its own memory timing. A **session** starts from its agent's profile (sessions arrive with opencode residents).

The **Mayor** is the main agent. Its profile applies to every Mayor session (web interface, terminal and chat apps), it uses the default model preset from Settings › Models, and it can't be set to *ask*, because Babo can't stop mid-turn to ask you. Residents can be set to *allow*, *ask* or *deny*. The **new-residents** profile is what a newly founded resident starts with.

**Models.** Every model you set up in Settings › Models (a local model server, Ollama, OpenAI, Anthropic, any provider) is a *preset*. The default preset is what everything uses: the Mayor, every resident, and Babo's own memory work. A resident's profile can pick another preset (*Model* on the Profiles tab, or `babo profile archivist model <preset>`), and so can a single session when you start it. Residents never see a model's real name, its provider or its key: they ask Babo's gateway for a preset, and the gateway makes the call. A change in Settings applies to the next call, without a restart.

`babo profile` shows every profile; `babo profile archivist web_search allow` changes one. The **Profiles** tab of the Town page shows the same profiles and makes the same changes. Each change is committed to the town with what changed, and applies from the agent's next session. A town from before profiles keeps exactly the tools it had until you change something.

## Settings

The town's own settings (how often checkpoints happen, how much a resident sees when a session starts, when upkeep may run and its quiet hours, where to push a backup) are listed by `babo settings`, and changed with `babo settings <setting> <value>`. The **Settings** tab of the Town page in the web interface shows the same list. Your config file is backed up before each change, and each setting says when the change takes effect.

## How well a model does the work: `babo eval`

`babo eval` runs about thirty representative tasks on a model preset and checks each one itself: does the model pick the right tool, with the right arguments; leave tools alone when it should; follow a tool's result to the next step; act on where it left off (its baton); write a checkpoint Babo can file. They go through the gateway like every model call, so the score is what the town's agents actually get, and the report counts the tool calls the gateway had to repair.

- `babo eval` evaluates the default preset; `--preset cloud` (repeatable) another; `--only restraint` (a task or a category) part of it; `--repeat 3` evens out a local model's ups and downs; `--list` lists the tasks.
- `--baseline` keeps the run as the preset's baseline. Later runs say which tasks got better or worse, so a change to Babo, a new model or new settings is measured rather than guessed. `babo eval --last` shows each preset's latest score beside its baseline.
- The **Doctor** tab of the Town page has the same: each preset's latest score and baseline, and *Run the eval* (it runs in the background, task by task).

Results are kept in the town, under `state/eval/`.

## The town's name

A new town has no name. The residents choose one: any of them can propose a name with `propose_change`, and it comes to you in `babo review` like any other proposal. Accept it and the town takes the name at once; every resident sees it at the start of their next session, and `babo town` shows it. You can also name it yourself with `babo town --name "…"`, but it's nicer to let them.

## Founding a new resident

The Mayor posts a founding request to the board when it thinks the town needs someone new. It shows up in your next review. If you approve, the resident is created with a plain description of its job, and chooses its own name later. Nothing else can create a resident.

## When something goes wrong

- **Check everything:** `babo doctor` prints a table of what works and what doesn't.
- **See exactly what the model was sent:** `babo context mayor`.
- **Undo a bad upkeep.** Every upkeep is one commit, so you can undo it with git:

  ```
  cd ~/baton-town
  git log --oneline          # find it, e.g. "a1b2c3d Upkeep 2026-11-03 14:05: 1 memory review(s)"
  git revert a1b2c3d         # undoes it, keeping a record that you did
  ```

- **Services:** they run as systemd user services on the host. Their status is shown by:

  ```
  systemctl --user status babo-queue babo-gateway babo-upkeep
  ```

## Credits

Babo began as a fork of [nanobot](https://github.com/HKUDS/nanobot) by Xubin Ren and the nanobot contributors (MIT). Several ideas come from the [RecursiveRabbit/nanobot](https://github.com/RecursiveRabbit/nanobot) fork (`context-window` branch, MIT), which runs a town of agents on nanobot: no Dream (continuity is authored), restore-or-nothing compaction, an exact-context view, and one process per resident. Those ideas were reimplemented here; no code was copied.
