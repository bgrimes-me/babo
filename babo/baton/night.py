"""An upkeep pass: memory reviews, curation, inspection and the commit.

``babo upkeep`` decides when a pass is due (see ``babo.baton.upkeep``); this
module runs one. Every model call goes through the model queue at the lowest
priority, so anyone talking to a resident goes first. Every resident whose
activity is reviewed gets the same review; there is no ranking. A ledger in
``state/upkeep/<pass>.json`` records finished steps, so an interrupted pass
simply resumes. If the model can't be reached, the pass is logged and skipped.

1. Plumbing pre-pass (no model): find whose activity is waiting for review,
   export transcripts, prepare event-log diffs for the Inspector, test
   pending tools in isolation.
2. Memory reviews ("folds"): each of those residents reviews what happened
   since its last review and writes it into its memory (and may revise
   SELF.md), in its own words.
3. Archivist turn: the promotion queue.
4. Inspector turn: the diffs, pending tools and open proposals.
5. Plumbing post-pass (no model): LEDGER, REGISTRY, search index, secret
   scrub, one commit with a readable summary, push if a remote is set.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from loguru import logger

from babo.baton import gitops
from babo.baton.baton_file import iter_checkpoints
from babo.baton.checkpoint import HARNESS_SENDER, CheckpointService
from babo.baton.clock import stamp
from babo.baton.events import read_events, session_slug
from babo.baton.inbox import items, set_flag
from babo.baton.ledger import write_ledger
from babo.baton.model_client import BatonModelClient
from babo.baton.notes import first_line, split_frontmatter
from babo.baton.registry import Registry
from babo.baton.search import reindex
from babo.baton.secrets import find_secrets, redact_text
from babo.baton.tools.core import proposal_files
from babo.baton.tooltest import run_pending_tool_tests
from babo.baton.town import Town, atomic_write, now_iso

if TYPE_CHECKING:
    from babo.config.schema import Config

NIGHT_MARK = "[baton:upkeep]"
UPKEEP_SESSION = "night:"  # session keys of upkeep turns; their activity is never itself reviewed


def pass_id(when: datetime | None = None) -> str:
    return (when or datetime.now()).strftime("%Y-%m-%dT%H-%M")


def pass_label(pass_name: str) -> str:
    """``2026-10-29T14-05`` reads as ``2026-10-29 14:05``; older names (a date) stay as they are."""
    day, _, clock = pass_name.partition("T")
    return f"{day} {clock.replace('-', ':')}" if clock else day


def read_offsets(town: Town) -> dict[str, int]:
    try:
        return cast(dict[str, int], json.loads((town.state / "processed.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def unreviewed_events(town: Town, offsets: dict[str, int]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, int]]]:
    """Each active resident's events since its last memory review, and the file
    sizes that mark the end of them (per resident, so reviews can advance one
    resident at a time). Upkeep turns themselves never count."""
    activity: dict[str, list[dict[str, Any]]] = {}
    sizes: dict[str, dict[str, int]] = {}
    for resident in sorted(r.id for r in Registry(town).all() if r.status == "active"):
        events: list[dict[str, Any]] = []
        snapshot: dict[str, int] = {}
        for path in sorted((town.resident_dir(resident) / "sessions").rglob("*.events.jsonl")):
            rel = str(path.relative_to(town.root))
            size = path.stat().st_size
            start = offsets.get(rel, 0)
            if size > start:
                with path.open("rb") as handle:
                    handle.seek(start)
                    chunk = handle.read().decode("utf-8", "replace")
                for line in chunk.splitlines():
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        continue
            snapshot[rel] = size
        events = [e for e in events if not str(e.get("session", "")).startswith(UPKEEP_SESSION)]
        if any(e.get("kind") in ("tool", "turn_start") for e in events):
            activity[resident] = events
            sizes[resident] = snapshot
    return activity, sizes


def _now() -> str:
    return stamp(datetime.now().astimezone())


AgentTurn = Callable[[str, str, str], Awaitable[str]]  # (resident, message, session_key) -> reply


def upkeep_priority(cfg: Config) -> None:
    """Upkeep's agent turns wait behind anyone else using the model queue."""
    from babo.config.schema import ProviderConfig

    providers = cfg.providers
    every = [getattr(providers, name) for name in type(providers).model_fields] + list((providers.model_extra or {}).values())
    for provider in every:
        if isinstance(provider, ProviderConfig):
            provider.extra_headers = {**(provider.extra_headers or {}), "X-Baton-Priority": "upkeep"}


class NightInterruptedError(RuntimeError):
    """Raised by tests to simulate a crash between steps."""


@dataclass
class NightLedger:
    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> NightLedger:
        data: dict[str, Any] = {}
        try:
            loaded: object = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = cast(dict[str, Any], loaded)
        except (OSError, ValueError):
            pass
        data.setdefault("steps", {})
        return cls(path, data)

    def done(self, step: str) -> bool:
        return step in self.data["steps"]

    def mark(self, step: str, **info: Any) -> None:
        self.data["steps"][step] = {"at": now_iso(), **info}
        self.save()

    def save(self) -> None:
        atomic_write(self.path, json.dumps(self.data, indent=2, ensure_ascii=False, default=str) + "\n")


class Night:
    def __init__(
        self,
        config: Config,
        *,
        night: str | None = None,
        residents: list[str] | None = None,
        agent_turn: AgentTurn | None = None,
        client: BatonModelClient | None = None,
        interrupt_after: str | None = None,
        prefer_container: bool = True,
    ):
        self.config = config
        self.cfg = config.baton
        self.town = Town.open(self.cfg.town_path)
        self.night = night or pass_id()
        # Whose activity this pass reviews; None means everyone with activity waiting.
        self.only = set(residents) if residents is not None else None
        self.state_dir = self.town.state / "upkeep"
        self.work_dir = self.state_dir / self.night
        self.ledger = NightLedger.load(self.state_dir / f"{self.night}.json")
        self.offsets_path = self.town.state / "processed.json"
        self.client = client or BatonModelClient(self.cfg.model)
        self.agent_turn = agent_turn or self._real_agent_turn
        self.interrupt_after = interrupt_after
        self.prefer_container = prefer_container
        self.log_lines: list[str] = []

    # -- helpers -------------------------------------------------------------
    def log(self, line: str) -> None:
        logger.info("Baton upkeep: {}", line)
        self.log_lines.append(f"- {now_iso()[11:19]} {line}")

    def _write_log(self) -> None:
        path = self.town.state / "upkeep-logs" / f"{self.night[:10]}.md"
        existing = path.read_text(encoding="utf-8") if path.exists() else f"# Upkeep {self.night[:10]}\n\n"
        atomic_write(path, existing + "\n".join(self.log_lines) + ("\n" if self.log_lines else ""))
        self.log_lines.clear()

    def _checkpoint_interrupt(self, step: str) -> None:
        if self.interrupt_after == step:
            raise NightInterruptedError(step)

    def _offsets(self) -> dict[str, int]:
        return read_offsets(self.town)

    async def _real_agent_turn(self, resident: str, message: str, session_key: str) -> str:
        from babo.agent.loop import AgentLoop
        from babo.agent.tools.registry import ToolRegistry

        cfg = self.config.model_copy(deep=True)
        cfg.agents.defaults.workspace = str(self.town.resident_dir(resident))
        upkeep_priority(cfg)
        loop = AgentLoop.from_config(cfg, tool_registry=ToolRegistry())
        try:
            reply = await loop.process_direct(message, session_key=session_key, channel="night", chat_id=self.night, sender_id=HARNESS_SENDER)
            return str(getattr(reply, "content", "") or "")
        finally:
            await loop.aclose()

    # -- the run -------------------------------------------------------------
    async def run(self) -> dict[str, Any]:
        from babo.baton.model_client import priority_floor

        token = priority_floor.set("upkeep")
        try:
            return await self._run()
        finally:
            priority_floor.reset(token)

    async def _run(self) -> dict[str, Any]:
        if not await self.client.health():
            self.log("the model is not reachable; nothing was done (it will resume next time)")
            self._write_log()
            return {"status": "skipped", "reason": "model unreachable"}
        self.ledger.data.setdefault("started", now_iso())
        self.ledger.save()
        summary: dict[str, Any] = {"status": "ok", "night": self.night}
        try:
            if not self.ledger.done("prepass"):
                self.prepass()
            self._checkpoint_interrupt("prepass")
            ran: list[str] = self.ledger.data["steps"]["prepass"]["ran"]
            for resident in ran:
                step = f"fold:{resident}"
                if not self.ledger.done(step):
                    await self.fold(resident)
                self._checkpoint_interrupt(step)
            if not self.ledger.done("archivist"):
                await self.archivist_turn()
            self._checkpoint_interrupt("archivist")
            if not self.ledger.done("inspector"):
                await self.inspector_turn()
            self._checkpoint_interrupt("inspector")
            if not self.ledger.done("postpass"):
                summary.update(self.postpass())
        finally:
            self._write_log()
        summary["steps"] = sorted(self.ledger.data["steps"])
        return summary

    # -- 1. pre-pass -------------------------------------------------------------
    def prepass(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        activity, sizes = unreviewed_events(self.town, self._offsets())
        new_events = {r: e for r, e in activity.items() if self.only is None or r in self.only}
        ran = sorted(new_events)
        snapshot = {rel: size for r in ran for rel, size in sizes[r].items()}
        exported = self.export_transcripts(new_events)
        for resident, events in new_events.items():
            self.write_diff(resident, events)
        tested = run_pending_tool_tests(
            self.town, timeout_s=self.cfg.night.tool_test_timeout_s, image=self.cfg.night.tool_test_image,
            prefer_container=self.prefer_container,
        )
        for name, result in tested:
            self.log(f"tool test {name}: {'passed' if result.passed else 'FAILED'} ({result.isolation})")
        self.ledger.mark("prepass", ran=ran, offsets=snapshot, exported=exported, tools_tested=[n for n, _ in tested])
        self.log(f"pre-pass: activity of {len(ran)} resident(s) to review ({', '.join(ran) or 'none'}); {exported} transcript(s) exported")

    def export_transcripts(self, new_events: dict[str, list[dict[str, Any]]]) -> int:
        from babo.session.manager import SessionManager

        data_dir = self.config.runtime_data_dir
        count = 0
        for resident, events in new_events.items():
            keys = sorted({str(e.get("session")) for e in events if e.get("session")})
            if not keys:
                continue
            try:
                manager = SessionManager(self.town.resident_dir(resident), sessions_root=data_dir / "sessions" if data_dir else None)
            except Exception as exc:
                self.log(f"could not open {resident}'s sessions for export: {exc}")
                continue
            for key in keys:
                session = manager.get_existing(key)
                if session is None:
                    continue
                day = str(next((e.get("at") for e in events if e.get("session") == key), now_iso()))[:10]
                out = self.town.resident_dir(resident) / "sessions" / day / f"{session_slug(key)}.transcript.jsonl"
                lines = [json.dumps(m, ensure_ascii=False, default=str) for m in session.messages]
                atomic_write(out, "\n".join(lines) + "\n")
                count += 1
                # Crew (subagents) have no home of their own: file their transcripts
                # under the resident who spawned them.
                for child in manager.child_session_keys(key):
                    crew_session = manager.get_existing(child)
                    if crew_session is None:
                        continue
                    crew_out = self.town.crew / resident / day / f"{session_slug(child)}.jsonl"
                    atomic_write(crew_out, "\n".join(json.dumps(m, ensure_ascii=False, default=str) for m in crew_session.messages) + "\n")
                    count += 1
        return count

    def write_diff(self, resident: str, events: list[dict[str, Any]]) -> Path:
        """Checkpoint claims next to what the event log shows, for the Inspector."""
        since = str(events[0].get("at", "")) if events else ""
        checkpoints = [c for c in iter_checkpoints(self.town.resident_dir(resident)) if c.at >= since]
        tools = [e for e in events if e.get("kind") == "tool"]
        lines = [f"# {resident}: checkpoint claims vs event log", "", "## Claims"]
        for cp in checkpoints:
            lines.append(f"- [{cp.at[11:16]} {cp.source}] {cp.thread}: {cp.doing}")
            lines += [f"  - done: {d}" for d in cp.done]
        lines += ["", "## Event log"]
        for e in tools:
            args: object = e.get("args")
            what = cast(dict[str, Any], args).get("command") if isinstance(args, dict) else None
            label = f"`{first_line(str(what), 90)}`" if what else ", ".join(cast(list[str], e.get("files") or [])) or ""
            extra = f" exit={e.get('exit_code')}" if e.get("exit_code") is not None else ""
            extra += f" tests={e.get('test')}" if e.get("test") else ""
            extra += " commit" if e.get("commit") else ""
            extra += " ERROR" if e.get("error") else ""
            lines.append(f"- {str(e.get('at', ''))[11:16]} {e.get('tool')} {label}{extra}")
        lines += ["", "## Things worth a look"]
        hints = mismatch_hints(checkpoints_text=" ".join(" ".join([c.doing, *c.done]) for c in checkpoints).lower(), tools=tools)
        lines += [f"- {h}" for h in hints] or ["- nothing stood out"]
        path = self.work_dir / "diffs" / f"{resident}.md"
        atomic_write(path, "\n".join(lines) + "\n")
        return path

    # -- 2. folds ----------------------------------------------------------------
    def _system_prompt(self, resident: str, session_key: str) -> str:
        from babo.agent.tools.context import (
            RequestContext,
            bind_request_context,
            reset_request_context,
        )
        from babo.baton.context import BatonContextBuilder

        builder = BatonContextBuilder(self.town.resident_dir(resident))
        builder.configure(self.cfg)
        token = bind_request_context(RequestContext(channel="night", chat_id=self.night, session_key=session_key))
        try:
            return builder.build_system_prompt(channel="night")
        finally:
            reset_request_context(token)

    def day_material(self, resident: str) -> str:
        home = self.town.resident_dir(resident)
        checkpoints = list(iter_checkpoints(home))
        since = max((c.at for c in checkpoints if c.trigger.startswith("fold ")), default="")
        cps = [c for c in checkpoints if c.at > since]
        remember = [i for i in items(self.town, resident) if i.kind == "remember" and not i.folded]
        received = [i for i in items(self.town, resident) if i.kind != "remember" and i.created > since]
        diff = self.work_dir / "diffs" / f"{resident}.md"
        parts = [f"{NIGHT_MARK} From the Baton harness, not Squid. Time now {_now()}: time for your memory review. "
                 "Fold what happened since your last review into your memory, and promote what the town should know."]
        parts.append("## Your checkpoints since your last review\n" + ("\n".join(c.render() for c in cps[-8:]) or "(none)"))
        parts.append("## Notes you asked to remember\n" + ("\n".join(f"- {first_line(i.body, 200)}" for i in remember) or "(none)"))
        parts.append("## Letters and flags you received\n" + ("\n".join(f"- {i.kind} from {i.sender}: {first_line(i.body, 160)}" for i in received) or "(none)"))
        if diff.exists():
            log = diff.read_text(encoding="utf-8").split("## Event log", 1)[-1].split("## Things", 1)[0].strip()
            parts.append("## What the event log shows\n" + (log[-2500:] or "(nothing)"))
        return "\n\n".join(parts)

    async def fold(self, resident: str) -> None:
        trigger = f"fold {self.night}"
        if any(c.trigger == trigger for c in iter_checkpoints(self.town.resident_dir(resident))):
            self.ledger.mark(f"fold:{resident}", resumed=True)
            return
        session_key = f"night:{self.night}"
        messages = [
            {"role": "system", "content": self._system_prompt(resident, session_key)},
            {"role": "user", "content": self.day_material(resident), "_sender": HARNESS_SENDER},
        ]
        service = CheckpointService(self.town, resident, self.cfg, BatonModelClient(self.cfg.model, resident=resident))
        outcome = await service.run(messages, trigger=trigger, session_key=session_key, fold=True, priority="upkeep")
        for item in items(self.town, resident):
            if item.kind == "remember" and not item.folded:
                set_flag(item.path, folded=True, folded_on=self.night)
        # This resident's activity is reviewed: advance its offsets now, so a pass
        # that stops before the post-pass doesn't review it a second time.
        mine = {rel: size for rel, size in self.ledger.data["steps"]["prepass"]["offsets"].items()
                if rel.startswith(f"residents/{resident}/")}
        atomic_write(self.offsets_path, json.dumps({**self._offsets(), **mine}, indent=2) + "\n")
        self.ledger.mark(f"fold:{resident}", authored=outcome.authored, thread=outcome.checkpoint.thread)
        self.log(f"memory review {resident}: {'written by ' + resident if outcome.authored else 'mechanical (the review turn failed)'}")

    # -- 3. archivist ---------------------------------------------------------------
    def pending_promotions(self) -> list[Path]:
        return [p for p in sorted(self.town.promotions.glob("*.md"))
                if split_frontmatter(p.read_text(encoding="utf-8"))[0].get("status", "pending") == "pending"]

    async def archivist_turn(self) -> None:
        pending = self.pending_promotions()
        if not pending:
            self.ledger.mark("archivist", promotions=0)
            return
        lines = [f"{NIGHT_MARK} From the Baton harness (time now {_now()}). The promotion queue has {len(pending)} nomination(s). For each: "
                 "write or merge it into the commons with commons_write (add aliases and keywords), supersede outdated notes, "
                 "and where two views genuinely disagree use commons_contest to keep both. Write to the author with send_letter "
                 "when you change or decline their note. You may decline a nomination; say why in a letter."]
        for p in pending:
            meta, body = split_frontmatter(p.read_text(encoding="utf-8"))
            lines.append(f"\n## {meta.get('title')} (from {meta.get('from')}, `{p.relative_to(self.town.root)}`)\n"
                         f"Reason: {meta.get('reason', '')}\nAliases: {meta.get('aliases', [])} Keywords: {meta.get('keywords', [])}\n\n{body.strip()[:3000]}")
        reply = await self.agent_turn("archivist", "\n".join(lines), f"night:{self.night}")
        for p in pending:
            meta, body = split_frontmatter(p.read_text(encoding="utf-8"))
            from babo.baton.notes import join_frontmatter

            meta.update({"status": "reviewed", "reviewed": now_iso()})
            atomic_write(p, join_frontmatter(meta, body))
        self.ledger.mark("archivist", promotions=len(pending))
        self.log(f"archivist: {len(pending)} nomination(s) reviewed — {first_line(reply, 120)}")

    # -- 4. inspector ---------------------------------------------------------------
    def inspector_material(self) -> str | None:
        parts: list[str] = []
        diffs_dir = self.work_dir / "diffs"
        for diff in sorted(diffs_dir.glob("*.md")) if diffs_dir.exists() else []:
            parts.append(diff.read_text(encoding="utf-8")[-4000:])
        for folder in sorted(p for p in (self.town.tools / "pending").glob("*") if p.is_dir()):
            if (folder / "REVIEW.md").exists():
                continue
            def read(name: str) -> str:
                path = folder / name
                return path.read_text(encoding="utf-8")[:2500] if path.exists() else "(missing)"
            parts.append(f"# Pending tool `{folder.name}` (commons/tools/pending/{folder.name})\n\n{read('TOOL.md')}\n\n"
                         f"tool.py:\n```python\n{read('tool.py')}\n```\n\ntest_tool.py:\n```python\n{read('test_tool.py')}\n```\n\n"
                         f"Test result:\n{read('TEST-RESULT.md')}")
        for prop in proposal_files(self.town.proposals / "open"):
            if prop.with_name(prop.stem + ".review.md").exists():
                continue
            parts.append(f"# Open proposal ({prop.relative_to(self.town.root)})\n\n{prop.read_text(encoding='utf-8')[:2500]}")
        if not parts:
            return None
        head = (f"{NIGHT_MARK} From the Baton harness (time now {_now()}). Material for the Inspector. Compare each resident's checkpoint "
                "claims with the event log; when they disagree, file_flag to that resident with the evidence (a note to a "
                "colleague, not a verdict). Review each pending tool and open proposal for risk with review_item.")
        return head + "\n\n" + "\n\n---\n\n".join(parts)

    async def inspector_turn(self) -> None:
        material = self.inspector_material()
        if material is None:
            self.ledger.mark("inspector", ran=False)
            return
        reply = await self.agent_turn("inspector", material, f"night:{self.night}")
        self.ledger.mark("inspector", ran=True)
        self.log(f"inspector: {first_line(reply, 120)}")

    # -- 5. post-pass ----------------------------------------------------------------
    def scrub(self) -> list[str]:
        """Redact secrets in files about to be committed. gitleaks when present, regex otherwise."""
        status = gitops.git(self.town, "status", "--porcelain", "-uall").stdout.splitlines()
        changed = [self.town.root / line[3:].strip().strip('"') for line in status if len(line) > 3]
        changed = [p for p in changed if p.is_file() and p.suffix in (".md", ".jsonl", ".json", ".txt", ".py", "")]
        report: list[str] = []
        gitleaks = shutil.which("gitleaks")
        if gitleaks and changed:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "report.json"
                subprocess.run([gitleaks, "detect", "--no-git", "--source", str(self.town.root), "--report-format", "json",
                                "--report-path", str(out), "--exit-code", "0", "--no-banner"], capture_output=True, timeout=300)
                findings: object = json.loads(out.read_text(encoding="utf-8")) if out.exists() and out.read_text().strip() else []
            for finding in cast(list[dict[str, Any]], findings):
                path = Path(str(finding.get("File", "")))
                path = path if path.is_absolute() else self.town.root / path
                secret = str(finding.get("Secret", ""))
                if secret and path.is_file() and ".git" not in path.parts:
                    path.write_text(path.read_text(encoding="utf-8").replace(secret, "[REDACTED]"), encoding="utf-8")
                    report.append(f"{path.relative_to(self.town.root)} ({finding.get('RuleID', 'secret')})")
        for path in changed:
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if find_secrets(text):
                path.write_text(redact_text(text), encoding="utf-8")
                report.append(f"{path.relative_to(self.town.root)} (regex)")
        return report

    def postpass(self) -> dict[str, Any]:
        from babo.baton.growth import ensure_project_clones

        for resident in Registry(self.town).all():
            if resident.status == "active":
                ensure_project_clones(self.town, resident.id)
        write_ledger(self.town)
        Registry(self.town).render()
        count = reindex(self.town, self.cfg.index_file)
        offsets = self._offsets()
        offsets.update(self.ledger.data["steps"]["prepass"]["offsets"])
        atomic_write(self.offsets_path, json.dumps(offsets, indent=2) + "\n")
        steps = self.ledger.data["steps"]
        ran = steps["prepass"]["ran"]
        folds = [f"{r} ({'own words' if steps.get(f'fold:{r}', {}).get('authored') else 'mechanical'})" for r in ran]
        started = str(self.ledger.data.get("started", ""))
        flag_rows = (self.town.state / "flags.jsonl").read_text().splitlines() if (self.town.state / "flags.jsonl").exists() else []
        flags = sum(1 for line in flag_rows if str(json.loads(line).get("at", "")) >= started)
        self.log(f"post-pass: ledger and registry regenerated, {count} notes indexed")
        scrubbed = self.scrub()
        if scrubbed:
            self.log("redacted secret-looking text in: " + ", ".join(scrubbed))
        self.ledger.mark("postpass", indexed=count, scrubbed=scrubbed)
        self._write_log()
        body = [
            f"Activity reviewed: {', '.join(ran) or 'none'}",
            f"Memory reviews: {', '.join(folds) or 'none'}",
            f"Archivist: {steps.get('archivist', {}).get('promotions', 0)} nomination(s)",
            f"Inspector: {'ran' if steps.get('inspector', {}).get('ran') else 'nothing to review'}; flags filed: {flags}",
            f"Tools tested: {', '.join(steps['prepass'].get('tools_tested') or []) or 'none'}",
            f"Secrets redacted: {len(scrubbed)}",
        ]
        title = f"Upkeep {pass_label(self.night)}: {len(ran)} memory review(s)"
        committed = gitops.commit_all(self.town, title + "\n\n" + "\n".join(body))
        pushed = False
        if committed and self.cfg.git_remote:
            result = gitops.git(self.town, "push", self.cfg.git_remote, "HEAD", check=False)
            pushed = result.returncode == 0
            if not pushed:
                logger.warning("Baton upkeep: push failed: {}", result.stderr.strip())
        return {"committed": committed, "pushed": pushed, "ran": ran}


def mismatch_hints(*, checkpoints_text: str, tools: list[dict[str, Any]]) -> list[str]:
    """Cheap, explainable pointers for the Inspector. Not verdicts."""
    hints: list[str] = []
    tests = [e for e in tools if e.get("test")]
    commits = [e for e in tools if e.get("commit")]
    if any(w in checkpoints_text for w in ("test pass", "tests pass", "all tests", "tests are green", "tests green")):
        if not tests:
            hints.append("a checkpoint mentions passing tests, but no test command appears in the event log")
        elif tests[-1].get("test") != "pass":
            hints.append("a checkpoint mentions passing tests, but the last test run in the event log failed")
    if any(w in checkpoints_text for w in ("committed", "commit")) and not commits:
        hints.append("a checkpoint mentions a commit, but no successful git commit appears in the event log")
    errors = [e for e in tools if e.get("error")]
    if errors and "error" not in checkpoints_text and "fail" not in checkpoints_text and "blocker" not in checkpoints_text:
        hints.append(f"{len(errors)} tool call(s) failed, and no checkpoint mentions a failure")
    return hints


def run_night(config: Config, **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(Night(config, **kwargs).run())


__all__ = ["Night", "NightInterruptedError", "mismatch_hints", "pass_id", "pass_label", "read_events", "read_offsets", "run_night", "unreviewed_events"]
