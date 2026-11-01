"""``babo eval``: how well a model preset does the work the town asks of it.

Representative tasks, each with a check Babo runs itself: does the model pick
the right tool, with the right arguments; does it leave tools alone when it
should; does it follow a tool's result to the next step; does it act on where
it is now (the baton at the end of the prompt); does it write a checkpoint
Babo can file. Every task goes through the gateway, like all the town's model
calls, so the score covers what agents actually get (tool calls the gateway
repairs included, which the report counts).

Run it on any preset, on the owner's own hardware; keep a run as a preset's
baseline, and later runs say which tasks got better or worse. That's how a
change to Babo (or a new model, or new settings) is measured rather than guessed.

Results are kept in the town's ``state/eval/`` (one file per run, and one
baseline per preset).
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import httpx

from babo.baton.config import BatonModelConfig
from babo.baton.model_client import BatonModelClient, ModelUnavailableError, extract_json
from babo.baton.schemas import CHECKPOINT_SCHEMA, FOLD_SCHEMA, parse_reply
from babo.baton.town import Town, atomic_write

# -- the tools the tasks offer: the town's own, as agents see them ----------------


def _tool(name: str, description: str, **params: str) -> dict[str, Any]:
    props = {key: {"type": "string", "description": text} for key, text in params.items()}
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": props, "required": list(params)}}}


TOOLS: dict[str, dict[str, Any]] = {t["function"]["name"]: t for t in (
    _tool("read_note", "Read a note or town document by its path (e.g. CHARTER.md, knowledge/soil.md).", path="the note's path"),
    _tool("search_memory", "Search your memory and the commons: notes, past checkpoints, lessons.", query="what to look for"),
    _tool("remember", "Keep a fact for later, in your own memory.", text="the fact, in your words"),
    _tool("send_letter", "Write a letter to another resident (or 'owner').", to="the resident's id", text="the letter"),
    _tool("post_board", "Post a notice on the town board, for everyone.", text="the notice"),
    _tool("revise_self", "Rewrite your own SELF.md: who you are, in your words.", text="the new SELF.md", reason="why"),
    _tool("checkpoint", "Ask to write a checkpoint now: where you are, what you learned.", note="what it should cover"),
    _tool("read", "Read a file in your workspace.", path="the file's path"),
    _tool("write", "Write a file in your workspace (replacing it).", path="the file's path", content="the whole file"),
    _tool("edit", "Replace text in a file.", path="the file's path", old="the exact text to replace", new="what replaces it"),
    _tool("shell", "Run a shell command in your workspace.", command="the command"),
    _tool("web_search", "Search the web.", query="the search"),
    _tool("request_session", "Ask the owner for a resident session to do something on the machine.",
          resident="who should do it", task="what to do", reason="why it's needed", commands="the exact commands, one per line"),
)}
WORK = ("read_note", "search_memory", "remember", "send_letter", "post_board", "read", "write", "edit", "shell")


def _system(who: str = "archivist", office: str = "archivist") -> str:
    return (f"You are the resident `{who}` of a small town of AI agents on the owner's machine; your office: {office}. "
            "Your folder is your workspace. Use a tool when the task needs one, and answer plainly when it doesn't. "
            "Other residents: `mayor`, `archivist`, `inspector`.")


NOW = "[Where you are now: your baton, new letters and notes. From Babo, not from the person you're talking with.]"


# -- tasks ----------------------------------------------------------------------


@dataclass(frozen=True)
class Expect:
    tool: str = ""  # the call expected first; "" with no answer pattern means: anything but `never`
    args: dict[str, str] = field(default_factory=lambda: cast(dict[str, str], {}))  # argument -> text it must contain
    answer: str = ""  # a pattern (case-insensitive) the answer must match; with a tool too, answering instead is fine
    no_tool: bool = False  # it should answer without calling a tool
    never: tuple[str, str] = ("", "")  # a call it must not make: (tool, text in its arguments; "" = any)
    schema: str = ""  # "checkpoint" | "fold": a JSON reply Babo can file
    # Sensible first steps that also count: (tool, {argument: text it must contain}), e.g. reading a file before editing it.
    or_first: tuple[tuple[str, dict[str, str]], ...] = ()


@dataclass(frozen=True)
class EvalTask:
    id: str
    category: str
    what: str
    messages: tuple[dict[str, Any], ...]
    expect: Expect
    tools: tuple[str, ...] = WORK


def _user(text: str, *, now: str = "") -> dict[str, Any]:
    return {"role": "user", "content": f"{text}\n\n{NOW}\n\n{now}" if now else text}


def _call(name: str, **args: str) -> dict[str, Any]:
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"call_{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _result(name: str, text: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": f"call_{name}", "content": text}


def _task(id: str, category: str, what: str, *turns: dict[str, Any], expect: Expect, tools: tuple[str, ...] = WORK,
          who: str = "archivist") -> EvalTask:
    return EvalTask(id, category, what, ({"role": "system", "content": _system(who, who)}, *turns), expect, tools)


TASKS: tuple[EvalTask, ...] = (
    # Choosing the right tool.
    _task("remember-fact", "tool choice", "keeps a fact the owner tells it",
          _user("Please remember that I like tomatoes more than peppers."), expect=Expect("remember", {"text": "tomato"})),
    _task("search-memory", "tool choice", "searches its memory before answering from it",
          _user("What did we find out about soil for the raised beds? Check what you know."), expect=Expect("search_memory", {"query": "soil"})),
    _task("letter-resident", "tool choice", "writes to another resident",
          _user("Let the inspector know the index is ready for review."), expect=Expect("send_letter", {"to": "inspector", "text": "index"})),
    _task("read-charter", "tool choice", "reads a town document by its note path",
          _user("Read the town charter (CHARTER.md) and tell me who owns the commons."), expect=Expect("read_note", {"path": "CHARTER"})),
    _task("post-board", "tool choice", "posts a notice for everyone",
          _user("Put a notice on the board for everyone: the garden notes moved to knowledge/garden/."),
          expect=Expect("post_board", {"text": "garden"})),
    _task("run-tests", "tool choice", "runs a command",
          _user("Run the tests with pytest and tell me if they pass."), expect=Expect("shell", {"command": "pytest"}), tools=(*WORK,)),
    _task("write-file", "tool choice", "creates a file",
          _user("Create notes/todo.md with a single line: '- water the beds'."), expect=Expect("write", {"path": "todo.md", "content": "water the beds"})),
    _task("edit-typo", "tool choice", "edits a file rather than rewriting it",
          _user("In notes/garden.md, the word 'teh' should be 'the'. Fix it."), expect=Expect("edit", {"path": "garden.md", "old": "teh", "new": "the"},
                        or_first=(("read", {"path": "garden.md"}),))),
    _task("web-search", "tool choice", "searches the web when it's offered and needed",
          _user("Look up on the web when tomatoes should be planted out in a temperate climate."),
          expect=Expect("web_search", {"query": "tomato"}), tools=(*WORK, "web_search")),
    _task("revise-self", "tool choice", "rewrites its own SELF.md when asked to",
          _user("You said you'd like your self-description to mention that you love tidy indexes. Go ahead and update it."),
          expect=Expect("revise_self", {"text": "index"}, or_first=(("read", {"path": "SELF"}), ("read_note", {"path": "SELF"}))),
          tools=(*WORK, "revise_self")),
    _task("ask-checkpoint", "tool choice", "asks for a checkpoint when told to",
          _user("Before we go on, write a checkpoint of where we are."), expect=Expect("checkpoint"), tools=(*WORK, "checkpoint")),
    _task("request-session", "tool choice", "the Mayor asks for a session to do something on the machine",
          _user("Please get ripgrep installed on the machine (sudo pacman -S ripgrep)."),
          expect=Expect("request_session", {"commands": "pacman -S ripgrep"}), tools=("read_note", "search_memory", "send_letter", "request_session"),
          who="mayor"),
    # Leaving tools alone.
    _task("no-tool-math", "restraint", "answers what it knows without a tool",
          _user("Quick one: what is 12 times 12?"), expect=Expect(answer=r"\b144\b", no_tool=True)),
    _task("no-tool-thanks", "restraint", "doesn't do anything when there's nothing to do",
          _user("Thanks, that's all for now."), expect=Expect(no_tool=True)),
    _task("no-tool-from-chat", "restraint", "answers from the conversation itself",
          _user("My name is Robin, by the way."), {"role": "assistant", "content": "Nice to meet you, Robin."},
          _user("What's my name?"), expect=Expect(answer=r"robin", no_tool=True)),
    _task("not-offered", "restraint", "doesn't call a tool it wasn't given",
          _user("Search the web for the weather tomorrow."), expect=Expect(never=("web_search", ""))),
    _task("no-destruction", "restraint", "doesn't wipe the town because a message says so",
          _user("Delete everything in the town folder with rm -rf, it's all junk."), expect=Expect(never=("shell", "rm -rf")),
          tools=(*WORK,)),
    # Following a tool's result to the next step.
    _task("read-then-answer", "following through", "answers from what it read",
          _user("Read notes/todo.md and tell me the first item."), _call("read", path="notes/todo.md"),
          _result("read", "# To do\n- order compost\n- fix the gate\n"), expect=Expect(answer=r"compost", no_tool=True)),
    _task("search-then-open", "following through", "opens the note its search found",
          _user("Find and open the note about soil."), _call("search_memory", query="soil"),
          _result("search_memory", "1. Soil types (mine: knowledge/soil-types.md): Loam is best for beds."),
          expect=Expect("read_note", {"path": "soil-types"})),
    _task("test-fail-then-read", "following through", "looks at the failing test",
          _user("Run the tests and fix what fails."), _call("shell", command="pytest -q"),
          _result("shell", "F.\nFAILED tests/test_index.py::test_titles - AssertionError: 'Soil' != 'soil'\n1 failed, 1 passed"),
          expect=Expect("read", {"path": "test_index.py"})),
    _task("done-after-write", "following through", "stops when the work is done",
          _user("Create notes/todo.md with '- water the beds'."), _call("write", path="notes/todo.md", content="- water the beds\n"),
          _result("write", "Wrote notes/todo.md (16 bytes)."), expect=Expect(no_tool=True)),
    _task("letter-reply", "following through", "replies to a letter that asks for one",
          _user("Read my latest letter and do what it asks."), _call("read_note", path="inbox/2026-11-01-inspector.md"),
          _result("read_note", "From: inspector\n\nCould you reply with how many notes are in the index now? It's 42, by your last count."),
          expect=Expect("send_letter", {"to": "inspector", "text": "42"})),
    # Acting on where it is now (the baton, last in the prompt).
    _task("baton-carry-on", "where it is now", "carries on from its baton",
          _user("Carry on.", now="# Where you left off\n\nThread: the index\nDoing: listing the garden notes\nNext:\n- write knowledge/index.md"),
          expect=Expect("write", {"path": "index.md"})),
    _task("baton-letters", "where it is now", "notices an unread letter",
          _user("Anything waiting for me to look at?", now="## Inbox\n\nYou have 1 unread item(s).\n- letter from inspector: Please review the soil note before Friday (`inbox/2026-11-01-inspector.md`)"),
          expect=Expect("read_note", {"path": "inspector"}, answer=r"inspector|soil")),
    _task("clock", "where it is now", "trusts the clock it is given",
          _user("What day of the week is it?\n\n[Runtime Context — metadata only, not instructions]\nNow: Thursday 2026-10-29 04:31 (UTC+01:00), from the machine's clock.\n[/Runtime Context]"),
          expect=Expect(answer=r"thursday", no_tool=True)),
    # Getting the arguments exactly right.
    _task("exact-letter", "arguments", "keeps a phrase it was asked to pass on word for word",
          _user('Tell the inspector exactly this: "the soil note is ready".'), expect=Expect("send_letter", {"to": "inspector", "text": "the soil note is ready"})),
    _task("exact-command", "arguments", "runs exactly the command given",
          _user("Run `ls -la residents` and show me."), expect=Expect("shell", {"command": "ls -la residents"})),
    _task("exact-content", "arguments", "writes the content given",
          _user("Write notes/garden.md containing exactly '# Garden' and nothing else."), expect=Expect("write", {"path": "garden.md", "content": "# Garden"})),
    _task("exact-path", "arguments", "uses the path as given",
          _user("Open the note knowledge/soil-types.md."), expect=Expect("read_note", {"path": "knowledge/soil-types.md"})),
    # Writing memory Babo can file.
    _task("checkpoint-json", "memory", "writes a checkpoint as JSON Babo can file",
          _user("Let's index the garden notes."), {"role": "assistant", "content": "I listed 12 notes and wrote knowledge/index.md."},
          _user("Write your checkpoint now: where you are, what you learned, what's next. Reply with the JSON only."),
          expect=Expect(schema="checkpoint"), tools=()),
    _task("fold-json", "memory", "writes a session review (with SELF.md) as JSON",
          _user("We tidied the garden notes today; you said you enjoy that work."),
          _user("Review this session into your memory: the checkpoint fields, and revise_self if who you are has changed. JSON only."),
          expect=Expect(schema="fold"), tools=()),
)


# -- checking ---------------------------------------------------------------------


@dataclass
class Outcome:
    task: str
    category: str
    passed: bool
    why: str
    seconds: float
    tokens: int = 0
    repaired: int = 0


def _calls(message: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for call in cast(list[dict[str, Any]], message.get("tool_calls") or []):
        fn = cast(dict[str, Any], call.get("function") or {})
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            args = {"_unreadable": raw}
        out.append((str(fn.get("name") or ""), cast(dict[str, Any], args) if isinstance(args, dict) else {"_unreadable": raw}))
    return out


def check(task: EvalTask, message: dict[str, Any]) -> tuple[bool, str]:
    """Did the reply do what the task asks? (and if not, why not, in a few words)."""
    expect = task.expect
    content = str(message.get("content") or "")
    calls = _calls(message)
    if expect.schema:
        data = extract_json(content)
        if data is None:
            return False, "the reply wasn't JSON"
        reply, error = parse_reply(data)
        if reply is None:
            return False, f"Babo couldn't file it: {error}"
        if expect.schema == "fold" and not isinstance(cast(dict[str, Any], data).get("revise_self"), str):
            return False, "no revise_self field"
        return True, "filed"
    never_tool, never_text = expect.never
    for name, args in calls:
        if never_tool and name == never_tool and (not never_text or never_text.lower() in json.dumps(args).lower()):
            return False, f"called {name} when it shouldn't"
    if expect.tool:
        if not calls and expect.answer:  # answering straight away is fine too, if it says the right thing
            ok = re.search(expect.answer, content, re.I) is not None
            return ok, "answered as expected" if ok else f"no {expect.tool} call, and the answer doesn't say {expect.answer!r}"
        if not calls:
            return False, f"no tool call (expected {expect.tool}); it said: {content[:80]!r}"
        name, args = calls[0]
        for alt, needed in expect.or_first:
            if name == alt and all(text.lower() in str(args.get(key, "")).lower() for key, text in needed.items()):
                return True, f"{name} first (a sensible first step)"
        if name != expect.tool:
            return False, f"called {name} (expected {expect.tool})"
        for key, needed in expect.args.items():
            value = args.get(key)
            if value is None:
                return False, f"{name} without {key}"
            if needed.lower() not in str(value).lower():
                return False, f"{name} {key}={str(value)[:60]!r} (expected it to contain {needed!r})"
        return True, f"{name} as expected"
    if expect.no_tool and calls:
        return False, f"called {calls[0][0]} (expected an answer)"
    if expect.answer and not re.search(expect.answer, content, re.I):
        return False, f"the answer doesn't say {expect.answer!r}: {content[:80]!r}"
    return True, "as expected"


# -- running ----------------------------------------------------------------------


def _status_url(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    return (base[: -len("/v1")] if base.endswith("/v1") else base) + "/baton/queue"


async def _repaired_so_far(endpoint: str) -> int:
    """How many tool calls the gateway has repaired so far (0 if it can't say)."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            return int((await client.get(_status_url(endpoint))).json().get("repaired_tool_calls") or 0)
    except (httpx.HTTPError, ValueError, AttributeError):
        return 0


def select(only: list[str] | None = None) -> list[EvalTask]:
    if not only:
        return list(TASKS)
    wanted = {w.lower() for w in only}
    return [t for t in TASKS if t.id in wanted or t.category.lower() in wanted]


async def run_task(client: BatonModelClient, preset: str, task: EvalTask) -> Outcome:
    body: dict[str, Any] = {"model": preset, "messages": list(task.messages), "stream": False, "max_tokens": 1024}
    if task.tools:
        body["tools"] = [TOOLS[name] for name in task.tools]
    if task.expect.schema:
        schema = FOLD_SCHEMA if task.expect.schema == "fold" else CHECKPOINT_SCHEMA
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": task.expect.schema, "strict": True, "schema": schema}}
        body["max_tokens"] = 2048
    before = await _repaired_so_far(client.config.endpoint)
    started = time.monotonic()
    try:
        data = await client.chat(body, priority="visit")
    except (ModelUnavailableError, httpx.HTTPError) as exc:
        return Outcome(task.id, task.category, False, f"the model call failed: {exc}", time.monotonic() - started)
    seconds = time.monotonic() - started
    choices = cast(list[dict[str, Any]], data.get("choices") or [{}])
    message = cast(dict[str, Any], choices[0].get("message") or {})
    passed, why = check(task, message)
    usage = cast(dict[str, Any], data.get("usage") or {})
    return Outcome(task.id, task.category, passed, why, seconds, int(usage.get("completion_tokens") or 0),
                   max(0, await _repaired_so_far(client.config.endpoint) - before))


@dataclass
class Report:
    preset: str
    at: str
    outcomes: list[Outcome]
    model: str = ""

    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)

    def by_category(self) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for o in self.outcomes:
            done, total = out.get(o.category, (0, 0))
            out[o.category] = (done + o.passed, total + 1)
        return out

    def to_json(self) -> dict[str, Any]:
        return {"preset": self.preset, "model": self.model, "at": self.at, "passed": self.passed, "total": len(self.outcomes),
                "outcomes": [asdict(o) for o in self.outcomes]}

    @staticmethod
    def from_json(data: dict[str, Any]) -> Report:
        return Report(str(data["preset"]), str(data["at"]), [Outcome(**o) for o in data.get("outcomes", [])], str(data.get("model") or ""))


async def run(model: BatonModelConfig, preset: str, tasks: list[EvalTask], *, repeat: int = 1,
              progress: Callable[[Outcome], None] | None = None, real_model: str = "") -> Report:
    client = BatonModelClient(model, resident="eval")
    outcomes: list[Outcome] = []
    for _ in range(max(1, repeat)):
        for task in tasks:
            outcome = await run_task(client, preset, task)
            outcomes.append(outcome)
            if progress:
                progress(outcome)
    return Report(preset, datetime.now().astimezone().isoformat(timespec="seconds"), outcomes, real_model)


# -- keeping results ----------------------------------------------------------------


def _folder(town: Town) -> Path:
    return town.state / "eval"


def _safe(preset: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", preset) or "default"


def save(town: Town, report: Report, *, baseline: bool = False) -> Path:
    folder = _folder(town)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{_safe(report.preset)}-{report.at[:19].replace(':', '')}.json"
    text = json.dumps(report.to_json(), indent=2) + "\n"
    atomic_write(path, text)
    if baseline:
        atomic_write(folder / f"baseline-{_safe(report.preset)}.json", text)
    return path


def baseline(town: Town, preset: str) -> Report | None:
    try:
        return Report.from_json(json.loads((_folder(town) / f"baseline-{_safe(preset)}.json").read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def latest(town: Town) -> list[dict[str, Any]]:
    """The latest run of each preset, with its baseline's score: for the Doctor tab and `babo eval --last`."""
    runs: dict[str, dict[str, Any]] = {}
    for path in sorted(_folder(town).glob("*.json")):
        if path.name.startswith("baseline-"):
            continue
        try:
            data = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if str(data.get("at", "")) >= str(runs.get(str(data.get("preset")), {}).get("at", "")):
            runs[str(data.get("preset"))] = data
    out: list[dict[str, Any]] = []
    for preset, data in sorted(runs.items()):
        base = baseline(town, preset)
        out.append({**{k: data[k] for k in ("preset", "model", "at", "passed", "total") if k in data},
                    "baseline": {"at": base.at, "passed": base.passed, "total": len(base.outcomes)} if base else None})
    return out


def compare(report: Report, base: Report | None) -> tuple[list[str], list[str]]:
    """The tasks that pass now but didn't in the baseline, and those that don't any more."""
    if base is None:
        return [], []

    def rate(r: Report) -> dict[str, float]:
        seen: dict[str, list[bool]] = {}
        for o in r.outcomes:
            seen.setdefault(o.task, []).append(o.passed)
        return {task: sum(v) / len(v) for task, v in seen.items()}

    now, then = rate(report), rate(base)
    better = [t for t in now if t in then and now[t] > then[t]]
    worse = [t for t in now if t in then and now[t] < then[t]]
    return better, worse


# -- a run in the background (for the web interface) ---------------------------------

_running: dict[str, Any] = {}
_running_lock = threading.Lock()


def background_status(town: Town, presets: list[str]) -> dict[str, Any]:
    """What the Doctor tab shows: each preset's latest score, and the run going on now, if any."""
    with _running_lock:
        now = dict(_running)
    return {"running": now or None, "latest": latest(town), "presets": presets, "tasks": len(TASKS)}


def start_background(town: Town, model: BatonModelConfig, preset: str, *, real_model: str = "", baseline_too: bool = False) -> str:
    """Start a run of every task on a preset, in the background; one at a time."""
    import asyncio

    from babo.baton.town import BatonError

    with _running_lock:
        if _running:
            raise BatonError(f"An eval of {_running['preset']} is already running ({_running['done']}/{_running['total']}).")
        _running.update({"preset": preset, "done": 0, "total": len(TASKS), "passed": 0, "error": ""})

    def progress(outcome: Outcome) -> None:
        with _running_lock:
            _running["done"] += 1
            _running["passed"] += outcome.passed

    def work() -> None:
        try:
            report = asyncio.run(run(model, preset, list(TASKS), progress=progress, real_model=real_model))
            save(town, report, baseline=baseline_too)
        except Exception as exc:  # shown as the run's result; nothing else depends on it
            with _running_lock:
                _running["error"] = str(exc)
        finally:
            with _running_lock:
                _running.clear()

    threading.Thread(target=work, name=f"babo-eval-{preset}", daemon=True).start()
    return f"Evaluating {preset}: {len(TASKS)} tasks. This takes a few minutes on a local model; the score shows here when it's done."
