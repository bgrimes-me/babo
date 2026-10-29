<p align="center"><img alt="babo" src="./images/babo_logo.png" width="420"></p>

# Babo

**Babo** is a small town of AI agents that lives on your own Linux machine. The residents share one local model, keep their own memory, and grow the town together. You talk to them in a browser, in the terminal, or from a chat app, and you stay in charge of what runs on your machine.

The name is short for *baton bot*. A resident's memory is passed forward like a relay baton: at regular moments the harness asks the resident to write down, in its own words, where it is and what it learned, and the next session picks the baton up from there.

## What a town is

- **Residents** each have a home directory, their own notes, and a private memory. Three are founded with the town: the **Mayor** (the front desk), the **Archivist** (keeps the shared knowledge) and the **Inspector** (a second set of eyes). New residents are founded only when the Mayor asks and you approve.
- **The commons** is what the town shares: knowledge, skills, tools, proposals, projects and a notice board.
- **Memory is the residents' own.** The harness decides *when* a checkpoint happens; the resident decides *what* it says. Nothing summarizes a resident's mind behind its back, and compaction never loses data.
- **The night** runs once a day: each resident folds its day into memory, the Archivist tidies the commons, the Inspector checks what residents said against what actually happened, and the town makes one commit.
- **You review** proposed tools, proposals, founding requests and old flags with `babo review` (or the review page in the web interface). Nothing that runs on your machine is approved without you.

## Getting started

Babo is installed from this repository by an install agent following [`docs/baton/RUNBOOK.md`](./docs/baton/RUNBOOK.md). The person who owns the machine reads [`docs/baton/README.md`](./docs/baton/README.md).

Everyday commands:

| Command | What it does |
|---|---|
| `babo agent` | Chat with the Mayor in the terminal (`--as archivist` to talk to another resident) |
| `babo webui` | Open the web interface |
| `babo residents` | Who lives in the town |
| `babo review` | Decide on tools, proposals, founding requests and flags |
| `babo profile` | What each agent may do (tools, web, MCP servers, memory timing) |
| `babo doctor` | Check that every part of the town works |
| `babo night` | Run the night cycle now |

## Where it came from, and thanks

Much of Babo's philosophy comes from [RecursiveRabbit's nanobot fork](https://github.com/RecursiveRabbit/nanobot/tree/context-window) (`context-window` branch, MIT), which runs a town of agents and is home to residents who live in it today. The ideas at the heart of Babo were proven there first: residents own their minds and author their own continuity (no "Dream" summarizing a mind behind its back), the harness owns *when* memory forms while the resident owns *what* it says, compaction restores or does nothing and never loses data, you can always see the exact context a resident was given, and each resident runs as its own process. A resident of that town also reviewed Babo's design and suggested the trusted clock, the identity check in the nightly fold, "when in doubt, route, don't touch", and plain refusal messages. Babo reimplements these ideas; no code was copied.

Babo's code began as a fork of [nanobot](https://github.com/HKUDS/nanobot) by Xubin Ren and the nanobot contributors, released under the MIT license. Much of the agent runtime, the channels and the web interface come from that project; the town, its memory and its governance were built for Babo. It is now maintained as its own project and does not track nanobot. See [`LICENSE`](./LICENSE) and [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md).

## License

MIT. See [`LICENSE`](./LICENSE).
