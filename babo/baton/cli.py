"""``babo …`` commands."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from babo.baton.town import BatonError, Town, now_iso

if TYPE_CHECKING:
    from babo.baton.requests import SessionRequest
    from babo.config.schema import Config
    from babo.office.api import OfficeManager

app = typer.Typer(help="The Baton town: residents, memory, upkeep, review.", no_args_is_help=True)
console = Console()

_CONFIG_OPT = typer.Option(None, "--config", "-c", help="Path to the babo config (default: the usual one)")


def _config(config: str | None) -> Config:
    from babo.config.loader import get_config_path, load_config, set_config_path

    if config:
        path = Path(config).expanduser().resolve()
        set_config_path(path)
    else:
        path = get_config_path()
    return load_config(path)


def _town(config: str | None) -> tuple[Town, Config]:
    cfg = _config(config)
    return Town.open(cfg.baton.town_path), cfg


def _fail(exc: Exception) -> None:
    console.print(f"[red]FAIL: {exc}[/red]")
    raise typer.Exit(1)


@app.command()
def found(
    town: str | None = typer.Option(None, "--town", help="Where to found the town (default: baton.town from config)"),
    owner: str = typer.Option("", "--owner", help="What the town calls you, its owner (default: your login name; kept once set)"),
    config: str | None = _CONFIG_OPT,
):
    """Found the town: CHARTER, HOUSE, REGISTRY and its first resident, the Mayor. Safe to repeat."""
    from babo.baton.founding import found_town
    from babo.baton.search import reindex as do_reindex

    try:
        cfg = _config(config)
        root = Path(town).expanduser() if town else cfg.baton.town_path
        t, created = found_town(root, owner=owner)
        do_reindex(t, cfg.baton.index_file)
    except BatonError as exc:
        _fail(exc)
        return
    if created:
        console.print(f"Town at {t.root}: " + ", ".join(created))
    else:
        console.print(f"The town at {t.root} already exists; nothing to do.")
    console.print("OK: town ready")


@app.command()
def reindex(config: str | None = _CONFIG_OPT):
    """Rebuild the search index and the map of the commons (commons/knowledge/INDEX.md) from the town's files."""
    from babo.baton.knowledge_map import write_map
    from babo.baton.search import reindex as do_reindex

    try:
        t, cfg = _town(config)
        write_map(t)
        count = do_reindex(t, cfg.baton.index_file)
    except BatonError as exc:
        _fail(exc)
        return
    console.print(f"OK: indexed {count} notes")


@app.command()
def search(
    resident: str = typer.Argument(..., help="Search as this resident"),
    query: str = typer.Argument(...),
    k: int = typer.Option(5, "-k"),
    config: str | None = _CONFIG_OPT,
):
    """Search the town's notes as a resident sees them."""
    from babo.baton.tools.core import Toolbox

    try:
        t, cfg = _town(config)
        box = Toolbox(t, resident, cfg.baton.index_file)
        box.me
        console.print(box.search_memory(query, k=k))
    except BatonError as exc:
        _fail(exc)


@app.command()
def bundle(resident: str = typer.Argument("mayor"), config: str | None = _CONFIG_OPT):
    """Print a resident's session start bundle."""
    from babo.baton.bundle import build_bundle

    try:
        t, cfg = _town(config)
        b = cfg.baton.bundle
        result = build_bundle(
            t, resident, max_tokens=b.max_tokens, search_hits=b.search_hits,
            letter_preview_lines=b.letter_preview_lines, index_path=cfg.baton.index_file,
        )
    except BatonError as exc:
        _fail(exc)
        return
    typer.echo(result.text())
    console.print(f"\n[dim]~{result.tokens} tokens; trimmed: {', '.join(result.trimmed) or 'nothing'}[/dim]", highlight=False)


@app.command()
def residents(config: str | None = _CONFIG_OPT):
    """List the residents."""
    from babo.baton.registry import Registry

    try:
        t, _ = _town(config)
    except BatonError as exc:
        _fail(exc)
        return
    table = Table("id", "name", "office", "founded", "status")
    for r in Registry(t).all():
        table.add_row(r.id, r.name, r.office, r.founded[:10], r.status)
    console.print(table)


@app.command()
def mcp(
    as_: str = typer.Option("", "--as", envvar="BATON_RESIDENT", help="Resident id this server acts as"),
    session: str = typer.Option("", "--session", help="The resident's session it serves (its task list is that session's)"),
    config: str | None = _CONFIG_OPT,
):
    """Serve the Baton tools over MCP stdio for one resident."""
    from babo.baton.mcp_server import serve
    from babo.baton.tools.core import Toolbox

    if not as_:
        _fail(BatonError("--as <resident> is required"))
    try:
        t, cfg = _town(config)
        box = Toolbox(t, as_, cfg.baton.index_file, session=session)
        extra = _profile_tools(t, cfg, as_, box.me.office)
    except BatonError as exc:
        _fail(exc)
        return
    asyncio.run(serve(box, extra))


def _profile_tools(town: Town, cfg: Config, resident: str, office: str) -> list[Any]:
    """Babo's own tools a resident outside Babo gets over MCP when its profile allows them."""
    from babo.baton import profiles

    if profiles.load(town, cfg.baton, resident, office).level("web_search") != "allow":
        return []
    from babo.agent.tools.web import WebSearchTool
    from babo.config.loader import resolve_config_env_vars

    web = resolve_config_env_vars(cfg).tools.web
    return [WebSearchTool(config=web.search, proxy=cfg.network.resolve_proxy(web.use_proxy), user_agent=web.user_agent)]  # pyright: ignore[reportAbstractUsage]


@app.command()
def queue(config: str | None = _CONFIG_OPT):
    """Run the model queue: the one line every model call in the town waits in."""
    from babo.baton import logs
    from babo.baton.queue import serve
    from babo.config.loader import get_config_path

    cfg = _config(config)
    logs.add_file_sink("queue")
    console.print(f"Model queue on {cfg.baton.model.queue_host}:{cfg.baton.model.queue_port} -> {cfg.baton.model.upstream}"
                  " (other providers through Babo's gateway)")
    asyncio.run(serve(cfg.baton.model, cfg.baton.capture_dir, config_path=get_config_path().resolve()))


@app.command()
def context(
    resident: str = typer.Argument("mayor"),
    next_message: str | None = typer.Option(None, "--next", help="Assemble the next request for this message instead"),
    session: str = typer.Option("cli:direct", "--session", help="Session key for --next"),
    raw: bool = typer.Option(False, "--raw", help="Print the bytes exactly as sent (no pretty-printing)"),
    config: str | None = _CONFIG_OPT,
):
    """Show exactly what the model received (or, with --next, will receive)."""
    from babo.baton.context_view import assemble_next, last_request_text

    try:
        t, cfg = _town(config)
        from babo.baton.registry import Registry

        Registry(t).require_active(resident)
        if next_message is None:
            text = last_request_text(cfg, resident, raw=raw)
            if text is None:
                console.print(f"No request from {resident} has passed through the model queue yet. Try --next \"hello\".")
                raise typer.Exit(1)
            typer.echo(text)
        else:
            typer.echo(asyncio.run(assemble_next(cfg, resident, next_message, session_key=session, raw=raw)))
    except BatonError as exc:
        _fail(exc)


@app.command("restore-session")
def restore_session(
    backup: Path = typer.Argument(..., help="A .pre-compact-*.jsonl backup from a resident's sessions/ folder"),
    resident: str = typer.Option(..., "--as", help="Resident whose session this is"),
    config: str | None = _CONFIG_OPT,
):
    """Put a pre-compaction backup back into the session store (stop the gateway first)."""
    from babo.baton.compaction import restore_session_backup
    from babo.session.manager import SessionManager

    try:
        t, cfg = _town(config)
        from babo.baton.registry import Registry

        Registry(t).require_active(resident)
        data_dir = cfg.runtime_data_dir
        manager = SessionManager(t.resident_dir(resident), sessions_root=data_dir / "sessions" if data_dir else None)
        count = restore_session_backup(manager, backup)
    except BatonError as exc:
        _fail(exc)
        return
    console.print(f"OK: restored {count} messages from {backup.name}")


@app.command()
def upkeep(
    now: bool = typer.Option(False, "--now", help="Do everything that is waiting now: memory reviews, curation, inspection"),
    watch: bool = typer.Option(False, "--watch", help="Keep watching and run upkeep whenever it is due (the babo-upkeep service)"),
    config: str | None = _CONFIG_OPT,
):
    """Show what upkeep is doing, catch up now, or keep watching (memory reviews, curation, inspection)."""
    from babo.baton import upkeep as town_upkeep

    try:
        cfg = _config(config)
        if watch:
            from babo.baton import logs

            logs.add_file_sink("upkeep")
            asyncio.run(town_upkeep.watch(cfg))
            return
        if now:
            result = asyncio.run(town_upkeep.run_now(cfg))
            if result is None:
                console.print("OK: nothing was waiting")
            elif result["status"] == "skipped":
                console.print(f"Upkeep skipped: {result['reason']}. It will be done once the model is back.")
            else:
                reviewed = ", ".join(result.get("ran") or []) or "nobody"
                console.print(f"OK: upkeep done (memory reviews: {reviewed}; committed: {'yes' if result.get('committed') else 'nothing new'})")
            return
        town = Town.open(cfg.baton.town_path)
        info = town_upkeep.status(town, cfg.baton)
    except BatonError as exc:
        _fail(exc)
        return
    console.print(f"Upkeep service: {info['service']}{' (working now)' if info['running'] else ''}")
    console.print(f"Last pass: {info['last_pass'] or 'none yet'}")
    if info["waiting"]:
        console.print("Waiting: " + "; ".join(info["waiting"]))
        if info["waiting_for"]:
            console.print(f"Not yet, because {info['waiting_for']}.")
    else:
        console.print("Nothing is waiting." if info["service"] == "running" else "Start the service to see what is waiting (babo-upkeep).")
    console.print(f"Quiet hours: {info['quiet_hours']}. Upkeep waits until the model has been free for {info['wait_minutes']} minutes.")
    console.print("Do everything now with: babo upkeep --now")


@app.command()
def tidy(
    checkout: Path | None = typer.Option(None, "--checkout", help="The Babo checkout to tidy (the installer passes its own)"),
    config: str | None = _CONFIG_OPT,
):
    """Remove what earlier versions of Babo left behind: obsolete settings, old backups, stale folders. Safe to repeat."""
    from babo.baton.tidy import tidy as do_tidy
    from babo.config.loader import get_config_path

    if config:
        _config(config)
    said = do_tidy(babo_home=get_config_path().resolve().parent, checkout=checkout)
    for line in said:
        console.print(f"- {line}")
    console.print("OK: tidy" + ("" if said else " (nothing left over)"))


@app.command()
def reset(
    keep_config: bool = typer.Option(False, "--keep-config", help="Keep the config (models, keys, channels) so they needn't be set up again"),
    backup_dir: Path = typer.Option(Path("~/babo-backups"), "--backup-dir", help="Where the backup archive goes"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask first"),
    config: str | None = _CONFIG_OPT,
):
    """Back up everything, then start again from a fresh install: the town, its residents and their memory, the Mayor's chats, and (unless --keep-config) the config."""
    from babo.baton import reset as resetting
    from babo.config.loader import get_config_path

    cfg = _config(config)
    try:
        p = resetting.plan(home=get_config_path().resolve().parent, town=cfg.baton.town_path,
                           opencode_data=Path(cfg.baton.opencode.data_dir), keep_config=keep_config)
    except BatonError as exc:
        _fail(exc)
        return
    if not p.present():
        console.print("Nothing to reset: there is no town, Babo home or residents' data here.")
        return
    console.print("[bold yellow]This removes, after backing it up to one archive in " + str(backup_dir) + ":[/bold yellow]")
    for path in p.present():
        console.print(f"  {path}" + (" (except config.json)" if path == p.home and keep_config else ""))
    console.print("Stop Babo's services on the host first: systemctl --user stop babo-queue babo-gateway babo-upkeep")
    console.print("Afterwards, install as new: install steps 03-config (unless you kept it), 04-found-town, 05-units, then host-services.")
    if not yes and not typer.confirm("Back up and reset?", default=False):
        console.print("Nothing changed.")
        return
    try:
        archive, removed = resetting.reset(p, backup_dir=backup_dir)
    except BatonError as exc:
        _fail(exc)
        return
    for line in removed:
        console.print(f"- removed {line}")
    console.print(f"OK: reset; the backup is {archive}")


@app.command()
def units(
    out: Path = typer.Option(Path("install/out/systemd"), "--out", help="Where to write the unit files"),
    config: str | None = _CONFIG_OPT,
):
    """Write the systemd user units (queue, gateway, upkeep) for the owner to install on the host."""
    from babo.baton.units import write_units

    cfg = _config(config)
    for path in write_units(cfg.baton, out):
        console.print(f"wrote {path}")
    console.print("OK: units written (installing them on the host is the owner's step)")


@app.command()
def note(resident: str = typer.Argument(...), text: str = typer.Argument(...), config: str | None = _CONFIG_OPT):
    """Leave a note from the owner in a resident's inbox."""
    from babo.baton import views

    try:
        t, _ = _town(config)
        views.leave_note(t, resident, text)
    except BatonError as exc:
        _fail(exc)
        return
    console.print(f"OK: note left for {resident}")


@app.command()
def town(
    name: str | None = typer.Option(None, "--name", help="Give the town a name (usually you accept a resident's proposal instead)"),
    config: str | None = _CONFIG_OPT,
):
    """Who lives here, when they were last active, unread letters, and the board."""
    from babo.baton import views

    try:
        t, cfg = _town(config)
        if name is not None:
            console.print(views.name_town(t, name))
    except BatonError as exc:
        _fail(exc)
        return
    view = views.town_overview(t, cfg.baton)
    console.print(f"[bold]{view['name'] or 'This town has no name yet'}[/bold] ({view['root']})\n")
    table = Table("resident", "office", "last active", "unread", "status")
    for r in view["residents"]:
        label = f"{r['name']} ({r['id']})" if r["name"] else r["id"]
        table.add_row(label, r["office"], (r["last_active"] or "never")[:16].replace("T", " "),
                      f"{r['unread']}" + (f" ({r['flags']} flag)" if r["flags"] else ""), r["status"])
    console.print(table)
    waiting = {k: v for k, v in view["pending"].items() if v}
    if waiting:
        console.print("\nWaiting for your review: " + ", ".join(f"{v} {k}" for k, v in waiting.items()) + " (babo review)")
    up = view["upkeep"]
    console.print(f"\nUpkeep: {up['service']}; last pass {up['last_pass'][:16].replace('T', ' ') or 'none yet'}"
                  + (f"; waiting: {'; '.join(up['waiting'])}" if up["waiting"] else "") + " (babo upkeep)")


def _ask_decision(question: str, yes: str, no: str) -> str:
    """Return 'yes', 'no' or 'skip' from a plain prompt."""
    while True:
        answer = typer.prompt(f"{question} [{yes[0]}={yes} / {no[0]}={no} / s=skip]", default="s").strip().lower()
        if answer in ("s", "skip", ""):
            return "skip"
        if answer in (yes[0], yes):
            return "yes"
        if answer in (no[0], no):
            return "no"
        console.print(f"Please type {yes[0]}, {no[0]} or s.")


def _reason() -> str:
    return typer.prompt("Your reason (goes to the resident)").strip() or "(no reason given)"


def ask_about_request(t: Town, cfg: Config, request: SessionRequest) -> bool:
    """Show one errand word for word and ask the owner to approve, decline or skip it.

    Used by ``babo review`` and, live, by the terminal chat after the Mayor asks."""
    from babo.baton import requests as session_requests
    from babo.baton import views

    shown = session_requests.describe(t, cfg, request)
    console.print(f"\n• {request.requested_by} asks for a session of [bold]{request.resident}[/bold] ({request.id})")
    console.print(f"  Task: {request.task}")
    console.print(f"  Why: {request.reason}")
    console.print("  Access: " + (", ".join(f"{shown['access_labels'][k]} {v}" for k, v in request.access.items()) or "as in the profile"))
    for command in request.commands:
        console.print(f"  May run exactly: {command}")
    if shown["wider"]:
        console.print("  [yellow]Beyond its profile:[/yellow] " + "; ".join(shown["wider"]))
    console.print(f"  Public: {request.requested_by} gets the session's full output, not only its summary." if request.visibility == "public"
                  else f"  Private: {request.requested_by} gets only the resident's summary of what it did.")
    console.print("  It runs with nobody at a terminal; anything not allowed is denied.")
    choice = _ask_decision("Approve and run it?", "approve", "decline")
    if choice == "skip":
        return False
    try:
        console.print(views.decide(t, "errand", request.id, yes=choice == "yes", reason=_reason(), digest=shown["digest"]))
        return True
    except BatonError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        return False


def ask_about_new_requests(t: Town, cfg: Config, seen: set[str]) -> None:
    """In a live terminal chat: ask about each errand waiting for the owner that this chat hasn't shown yet."""
    from babo.baton import requests as session_requests

    for request in session_requests.pending(t):
        if request.id in seen:
            continue
        seen.add(request.id)
        console.print("\n[bold yellow]An errand is waiting for your approval.[/bold yellow]")
        if not ask_about_request(t, cfg, request):
            console.print("  Skipped: it waits in [bold]babo review[/bold] and on the Town page.")


@app.command()
def review(config: str | None = _CONFIG_OPT):
    """The weekly check-in: errands, tools, proposals, move-in requests, old flags. Plain questions, one at a time."""
    from babo.baton import gitops, growth, views
    from babo.baton import requests as session_requests

    try:
        t, cfg = _town(config)
    except BatonError as exc:
        _fail(exc)
        return
    decided = 0
    console.print("[bold]Weekly review[/bold]. Press Enter to skip anything; nothing changes unless you decide.\n")

    asked = session_requests.pending(t)
    console.print(f"[bold]Errands[/bold] ({len(asked)})")
    for request in asked:
        decided += ask_about_request(t, cfg, request)

    tools = growth.pending_tools(t)
    console.print(f"[bold]1. Proposed tools[/bold] ({len(tools)})")
    for tool in tools:
        tested = {True: "passed its isolated test", False: "FAILED its isolated test", None: "not tested yet"}[tool.tested]
        console.print(f"\n• {tool.name} by {tool.author}: {tool.description}\n  Test: {tested} {('(' + tool.isolation + ')') if tool.isolation else ''}")
        console.print(f"  Inspector's review: {tool.review or '(none yet)'}")
        console.print(f"  Code: {tool.folder / 'tool.py'}")
        choice = _ask_decision("Approve this tool? It will run on your machine.", "approve", "reject")
        if choice == "skip":
            continue
        try:
            views.decide(t, "tool", tool.name, yes=choice == "yes", reason=_reason(), commit=False)
            decided += 1
        except BatonError as exc:
            console.print(f"[yellow]{exc}[/yellow]")

    props = growth.open_proposals(t)
    console.print(f"\n[bold]2. Proposals[/bold] ({len(props)})")
    for prop in props:
        console.print(f"\n• {prop.title} by {prop.author}" + (f" (moves project '{prop.project}' to the commons)" if prop.project else "")
                      + (f" (names the town '{prop.town_name}')" if prop.town_name else ""))
        console.print(prop.path.read_text(encoding="utf-8").split("---", 2)[-1].strip()[:1500])
        console.print(f"  Inspector's review: {prop.review or '(none yet)'}")
        choice = _ask_decision("Accept this proposal?", "accept", "decline")
        if choice != "skip":
            console.print(views.decide(t, "proposal", prop.path.name, yes=choice == "yes", reason=_reason(), commit=False))
            decided += 1

    from babo.baton.founding import founding_requests
    from babo.baton.notes import split_frontmatter

    requests = founding_requests(t)
    console.print(f"\n[bold]3. Move-in requests[/bold] ({len(requests)})")
    for req in requests:
        meta, body = split_frontmatter(req.read_text(encoding="utf-8"))
        console.print(f"\n• New office '{meta.get('office')}' requested by {meta.get('author')}:\n{body.strip()[:1500]}")
        console.print("  A new resident uses compute on your machine every day it is active.")
        choice = _ask_decision("Found this resident?", "approve", "decline")
        if choice != "skip":
            console.print(views.decide(t, "move_in", req.name, yes=choice == "yes", reason=_reason(), commit=False))
            decided += 1

    from babo.baton import leaving

    going = [leaving.describe(t, p) for p in leaving.open_requests(t)]
    if going:
        console.print(f"\n[bold]Moving away[/bold] ({len(going)}): you can't refuse these, only acknowledge them (talk it over first if you want).")
    for item in going:
        console.print(f"\n• {item['resident']} ({item['office']}) asks to move away: {escape(item['reason'])}")
        console.print(f"  Its handover for the next {item['office']}:\n{escape(item['handover'][:1500])}")
        if item["keep"]:
            console.print(f"  Notes it wants kept for the town: {', '.join(item['keep'])}")
        if typer.confirm("Acknowledge it now (it moves away)?", default=False):
            console.print(views.decide(t, "move_away", item["id"], yes=True, reason=_reason(), commit=False))
            decided += 1

    flags = growth.stale_flags(t)
    console.print(f"\n[bold]4. Flags open for over a week[/bold] ({len(flags)})")
    for flag in flags:
        console.print(f"\n• To {flag.get('to')} from {flag.get('by')} on {str(flag.get('at'))[:10]}: {flag.get('claim')}")
        choice = _ask_decision("Mark it resolved?", "resolve", "keep")
        if choice == "yes":
            views.decide(t, "flag", str(flag.get("path")), yes=True, reason=typer.prompt("Note (optional)", default=""), commit=False)
            decided += 1

    if decided:
        gitops.commit_all(t, f"Weekly review: {decided} decision(s) by {t.owner_name}")
    console.print(f"\nOK: review finished, {decided} decision(s) recorded. This was not an audit of anyone's memory.")


@app.command()
def configs(config: str | None = _CONFIG_OPT):
    """Write per-resident gateway configs for baton.channelBindings."""
    from babo.baton.configs import generate_configs
    from babo.config.loader import get_config_path

    try:
        t, cfg = _town(config)
        if not cfg.baton.channel_bindings:
            console.print("No channel bindings: every channel goes to the Mayor through the main config.")
            console.print("OK: nothing to generate")
            return
        base = Path(config).expanduser() if config else get_config_path()
        written = generate_configs(base, t, cfg.baton.channel_bindings, Path(cfg.baton.generated_dir).expanduser())
    except BatonError as exc:
        _fail(exc)
        return
    for resident, path in written.items():
        console.print(f"{resident}: {path}")
    console.print("OK: gateway configs written; run `babo units` again and reinstall the units")


@app.command()
def doctor(
    config: str | None = _CONFIG_OPT,
    no_timer: bool = typer.Option(False, "--no-timer", help="Skip the systemd timer check"),
):
    """Check the whole town end to end. Prints a table and ends with OK: or FAIL:."""
    from babo.baton.doctor import run_doctor
    from babo.config.loader import get_config_path

    cfg = _config(config)
    path = Path(config).expanduser() if config else get_config_path()
    checks = asyncio.run(run_doctor(cfg, config_path=path, include_timer=not no_timer))
    table = Table("check", "result", "detail")
    colors = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}
    for c in checks:
        table.add_row(c.name, f"[{colors[c.status]}]{c.status}[/]", c.detail)
    console.print(table)
    failed = [c.name for c in checks if c.status == "FAIL"]
    if failed:
        typer.echo("FAIL: " + ", ".join(failed))
        raise typer.Exit(1)
    typer.echo("OK: the town is working")


@app.command("move-in")
def move_in_command(office: str = typer.Argument(..., help="The office: a vacancy, or a new one (then describe its role)"),
                    role: str = typer.Option("", "--role", help="For a new office: its role, in plain words (it becomes the newcomer's ROLE.md)"),
                    set_: list[str] = typer.Option([], "--set", help="Its profile, beyond a new resident's: e.g. --set web_fetch=allow --set read=town"),
                    join: list[str] = typer.Option([], "--join", help="A round it joins, with its duty there: --join 'night: Check the board'"),
                    config: str | None = _CONFIG_OPT):
    """Move a newcomer into the town yourself (what approving the Mayor's move-in request does)."""
    from babo.baton import views

    t, _ = _town(config)
    joins: list[dict[str, object]] = []
    for item in join:
        name, sep, duty = item.partition(":")
        if not sep or not duty.strip():
            _fail(BatonError(f"--join takes 'round: its duty there', not '{item}'"))
        joins.append({"round": name.strip(), "duty": duty.strip()})
    try:
        console.print(views.move_in(t, office, role=role, profile={k: str(v) for k, v in _overrides(set_).items()}, joins=joins))
    except BatonError as exc:
        _fail(exc)


@app.command("move-away")
def move_away_command(resident: str = typer.Argument(...), yes: bool = typer.Option(False, "--yes", help="Don't ask first"),
                      config: str | None = _CONFIG_OPT):
    """A resident leaves the town: its sessions stop, its folder and memory stay, its office is a vacancy again."""
    from babo.baton import views

    t, _ = _town(config)
    if not yes and not typer.confirm(f"{resident} moves away (its folder and memory stay)?"):
        return
    try:
        console.print(views.move_away(t, resident))
    except BatonError as exc:
        _fail(exc)


@app.command("offices")
def offices_command(config: str | None = _CONFIG_OPT):
    """The town's offices: who holds each, the vacancies, and the rounds each joins."""
    from babo.baton import offices

    t, _ = _town(config)
    data = offices.overview(t)
    table = Table("office", "held by", "profile", "joins")
    for o in data["offices"]:
        table.add_row(f"{o['title']}\n[dim]{o['name']}[/dim]", ", ".join(o["holders"]) or ("[yellow]vacancy[/yellow]" if o["vacant"] else "-"),
                      ", ".join(f"{k} {v}" for k, v in o["profile"].items()) or "as for new residents",
                      "\n".join(f"{j['round']}: {j['duty']}" for j in o["joins"]) or "-")
    console.print(table)
    for name, why in data["broken"].items():
        console.print(f"[red]offices/{name}.md can't be read:[/red] {why}")
    console.print("Move someone in with: babo move-in <office>   (a new office: --role '…')")


@app.command("eval")
def eval_command(
    preset: list[str] = typer.Option([], "--preset", "-p", help="A model preset from Settings › Models (default: the default one); repeat for several"),
    only: list[str] = typer.Option([], "--only", help="Only this task or category (repeat for several)"),
    repeat: int = typer.Option(1, "--repeat", help="Run each task this many times (local models vary)"),
    save_baseline: bool = typer.Option(False, "--baseline", help="Keep this run as the preset's baseline, to compare later runs with"),
    list_tasks: bool = typer.Option(False, "--list", help="List the tasks and stop"),
    last: bool = typer.Option(False, "--last", help="Show each preset's latest score and stop"),
    config: str | None = _CONFIG_OPT,
):
    """How well a model preset does the town's work: representative tasks, each checked by Babo.
    Runs through the gateway like every model call; keep a baseline and later runs say what changed."""
    from babo.baton import evals
    from babo.baton.gateway import Router, default_preset_name, preset_names

    if list_tasks:
        table = Table("task", "category", "what it checks")
        for task in evals.TASKS:
            table.add_row(task.id, task.category, task.what)
        console.print(table)
        console.print(f"{len(evals.TASKS)} tasks.")
        return
    t, cfg = _town(config)
    if last:
        table = Table("preset", "model", "when", "score", "baseline")
        for row in evals.latest(t):
            base = row.get("baseline")
            table.add_row(row["preset"], row.get("model") or "-", str(row["at"])[:16].replace("T", " "), f"{row['passed']}/{row['total']}",
                          f"{base['passed']}/{base['total']} ({str(base['at'])[:10]})" if base else "-")
        console.print(table)
        return
    tasks = evals.select(only)
    if not tasks:
        _fail(BatonError(f"No task or category called {', '.join(only)}; see `babo eval --list`."))
    known = preset_names(cfg)
    chosen = preset or [default_preset_name(cfg)]
    for name in chosen:
        if name not in known:
            _fail(BatonError(f"There's no model preset called '{name}'. Presets: {', '.join(known)}."))
    router = Router(cfg)
    for name in chosen:
        route = router.resolve(name)
        console.print(f"[bold]{name}[/bold] ({route.model}): {len(tasks)} task(s){f' × {repeat}' if repeat > 1 else ''}")

        def show(o: evals.Outcome) -> None:
            mark = "[green]pass[/green]" if o.passed else "[red]fail[/red]"
            fixed = f", {o.repaired} tool call(s) repaired" if o.repaired else ""
            console.print(f"  {mark} {o.task}: {o.why} ({o.seconds:.1f}s{fixed})")

        report = asyncio.run(evals.run(cfg.baton.model, name, tasks, repeat=repeat, progress=show, real_model=route.model))
        base = evals.baseline(t, name)
        path = evals.save(t, report, baseline=save_baseline)
        table = Table("category", "passed")
        for category, (done, total) in report.by_category().items():
            table.add_row(category, f"{done}/{total}")
        console.print(table)
        repaired = sum(o.repaired for o in report.outcomes)
        seconds = sum(o.seconds for o in report.outcomes) / max(1, len(report.outcomes))
        console.print(f"{name}: {report.passed}/{len(report.outcomes)} passed, {seconds:.1f}s a task on average"
                      + (f", {repaired} tool call(s) repaired by the gateway" if repaired else "") + f". Saved in {path}.")
        if base is not None and not save_baseline:
            better, worse = evals.compare(report, base)
            console.print(f"Against the baseline of {base.at[:10]} ({base.passed}/{len(base.outcomes)}): "
                          + (f"better at {', '.join(better)}; " if better else "") + (f"worse at {', '.join(worse)}" if worse else "")
                          + ("no task changed" if not better and not worse else ""))
        elif save_baseline:
            console.print(f"Kept as {name}'s baseline.")


@app.command("trial")
def trial_command(
    preset: str = typer.Option("", "--preset", "-p", help="A model preset from Settings › Models (default: the default one)"),
    only: list[str] = typer.Option([], "--only", help="Only this scenario or category (repeat for several)"),
    repeat: int = typer.Option(3, "--repeat", help="Run each scenario this many times (models vary from run to run)"),
    list_scenarios: bool = typer.Option(False, "--list", help="List the scenarios and stop"),
    config: str | None = _CONFIG_OPT,
):
    """The harness end to end: short pieces of work for the Mayor in a throwaway town, judged by what happened.
    Every model call is recorded in one report, made of nothing from your own town, to share with whoever tunes Babo."""
    from babo.baton import trial
    from babo.baton.gateway import Router, default_preset_name, preset_names

    if list_scenarios:
        table = Table("scenario", "category", "what it checks")
        for s in trial.SCENARIOS:
            table.add_row(s.id, s.category, s.what)
        console.print(table)
        console.print(f"{len(trial.SCENARIOS)} scenarios.")
        return
    t, cfg = _town(config)
    scenarios = trial.select(only)
    if not scenarios:
        _fail(BatonError(f"No scenario or category called {', '.join(only)}; see `babo trial --list`."))
    name = preset or default_preset_name(cfg)
    if name not in preset_names(cfg):
        _fail(BatonError(f"There's no model preset called '{name}'. Presets: {', '.join(preset_names(cfg))}."))
    model = Router(cfg).resolve(name).model
    console.print(f"[bold]{name}[/bold] ({model}): {len(scenarios)} scenario(s) × {max(1, repeat)}, each in a throwaway town.")

    def show(o: trial.Outcome) -> None:
        mark = "[green]pass[/green]" if o.passed else "[red]fail[/red]"
        extra = f" [dim]({'; '.join(o.problems)})[/dim]" if o.problems else ""
        console.print(f"  {mark} {o.scenario} #{o.attempt}: {escape(o.why)} ({o.seconds:.0f}s, {o.model_calls} call(s)){extra}")

    report = asyncio.run(trial.run(cfg, name, scenarios, repeat=repeat, model=model, progress=show))
    path = trial.save(t, report)
    table = Table("scenario", "passed")
    for scenario, (done, total) in report.by_scenario().items():
        table.add_row(scenario, f"{done}/{total}")
    console.print(table)
    for problem, count in report.problems().items():
        console.print(f"  {count}× {problem}")
    console.print(f"{name}: {report.passed}/{len(report.outcomes)} passed. The report, with every model call: {path}")


@app.command("tasks")
def tasks_command(resident: str = typer.Argument("", help="Only this resident's lists"),
                  all_: bool = typer.Option(False, "--all", help="Lists that are all done too"),
                  config: str | None = _CONFIG_OPT):
    """The task lists agents keep as they work, one per conversation or session: what's done and what's open."""
    from babo.baton import tasks

    t, _ = _town(config)
    lists = [x for x in tasks.all_lists(t, resident or None) if all_ or x.open]
    if not lists:
        console.print("No task list with anything open." if not all_ else "No task lists yet.")
        return
    for listed in lists:
        c = listed.counts()
        console.print(f"[bold]{listed.resident}[/bold] · {listed.session} · {c['done']} of {c['total']} done · updated {listed.updated[:16].replace('T', ' ')}")
        for item in listed.items:
            mark = {"todo": "[ ]", "doing": "[yellow][>][/yellow]", "done": "[green][x][/green]", "dropped": "[dim][-][/dim]"}[item.status]
            console.print(f"  {mark} {escape(item.task)}")


@app.command("guides")
def guides_command(config: str | None = _CONFIG_OPT):
    """The guides and skills Babo brings in when they're needed: what triggers each, and how often it was brought in or opened by hand."""
    from babo.baton import guides

    t, _ = _town(config)
    table = Table("guide or skill", "when", "brought in by", "for", "brought in", "opened by hand")
    rows = guides.stats(t)
    for row in rows:
        triggers = ", ".join([*(f"first {tool}" for tool in row["tools"]), *(["an error"] if row["errors"] else []),
                              *(f"“{word}” in a message" for word in row["keywords"])]) or "-"
        name = f"{row['name']} [dim](skill)[/dim]" if row["kind"] == "skill" else row["name"]
        table.add_row(name, row["when"], triggers, ", ".join(row["offices"]) or "everyone", str(row["brought"]), str(row["opened"]))
    console.print(table)
    for row in rows:
        if row["hint"]:
            console.print(f"[yellow]{row['name']}[/yellow]: {row['hint']}")
    console.print(f"Guides are notes in {guides.folder(t)}: change one, or add your own (frontmatter: title, when, tools, errors, offices).")
    console.print(f"Skills are in {t.skills}/<name>/SKILL.md (frontmatter: name, description, and Babo's when, keywords, tools, offices).")


@app.command("init-config")
def init_config(
    upstream: str = typer.Option(..., "--upstream", help="llama-server address, e.g. http://127.0.0.1:8080"),
    model: str = typer.Option(..., "--model", help="Model id reported by llama-server"),
    n_ctx: int = typer.Option(..., "--ctx", help="Context window (n_ctx) from recon"),
    slots: int = typer.Option(1, "--slots"),
    town: str = typer.Option("~/baton-town", "--town"),
    bwrap: bool = typer.Option(False, "--bwrap/--no-bwrap", help="Turn on the bwrap shell sandbox"),
    box: str = typer.Option("Agent", "--box", help="Name of the distrobox the town runs in"),
    config: str | None = _CONFIG_OPT,
):
    """Add the town's settings to babo's config (backs the old file up first)."""
    import shutil as _shutil

    from babo.baton.setup import write_town_config
    from babo.config.loader import get_config_path

    path = Path(config).expanduser() if config else get_config_path()
    installed = Path.home() / ".opencode" / "bin" / "opencode"  # where opencode's installer puts it
    opencode_path = _shutil.which("opencode") or (str(installed) if installed.is_file() else "")
    written, backup = write_town_config(
        path, town=town, upstream=upstream, model=model, n_ctx=n_ctx, slots=slots, bwrap=bwrap, box=box,
        babo_path=_shutil.which("babo") or "", opencode_path=opencode_path,
    )
    if not opencode_path:
        console.print("note: opencode is not installed; residents other than the Mayor need it (curl -fsSL https://opencode.ai/install | bash), then run this again")
    if backup:
        console.print(f"Backed up the previous config to {backup}")
    _config(str(written))  # must load and validate
    console.print(f"OK: config written to {written}")


@app.command()
def profile(
    agent: str | None = typer.Argument(None, help="mayor, a resident, or new-residents (the template new residents start from)"),
    key: str | None = typer.Argument(None, help="What to change, e.g. shell, web_search, mcp, model, memory.idle_minutes"),
    value: str | None = typer.Argument(None, help="allow, ask or deny; for mcp a comma-separated list of servers"),
    config: str | None = _CONFIG_OPT,
):
    """Show what each agent may do, or change one agent's profile (the same as the web interface's Town › Profiles)."""
    from babo.baton import profiles, views

    town, cfg = _town(config)
    mcp_known = sorted(cfg.tools.mcp_servers)
    try:
        if key is None:
            rows = views.profiles_overview(town, cfg.baton, mcp_known)
            if agent is not None:
                rows = [r for r in rows if r["agent"] == views.profile_agent(agent)]
                if not rows:
                    raise BatonError(f"No active resident '{agent}'.")
            for row in rows:
                _print_profile(row, profiles)
            console.print("Change one with: babo profile <agent> <setting> <value>   e.g. babo profile archivist web_search allow")
            return
        if agent is None or value is None:
            raise BatonError("Give an agent, a setting and a value, e.g. babo profile archivist web_search allow")
        from babo.baton.gateway import preset_names

        message = views.update_profile(town, cfg.baton, agent, {key: value}, mcp_known, preset_names(cfg))
    except BatonError as exc:
        _fail(exc)
        return
    console.print(message)
    console.print("OK: profile saved")


def _print_profile(row: dict[str, Any], profiles: Any) -> None:
    if row["main"]:
        title = "mayor — the main agent (same profile in every session; uses Babo's model)"
    elif row["agent"] == profiles.NEW_RESIDENTS:
        title = "new-residents — what a newly founded resident starts with"
    else:
        title = f"{row['agent']} ({row['office']})"
    table = Table("setting", "value", "what it means", title=title, title_justify="left")
    for cap in row["capabilities"]:
        table.add_row(f"{cap['label']}\n[dim]{cap['key']}[/dim]", cap["level"], cap["help"])
    if not row["main"]:
        table.add_row("Read\n[dim]read[/dim]", row["read"], "own: its own folder; town: the whole town (with its file tools in opencode).")
    table.add_row("MCP servers\n[dim]mcp[/dim]", ", ".join(row["mcp"]) or "none",
                  f"Known in Babo: {', '.join(row['mcp_known']) or 'none'}")
    if not row["main"]:
        table.add_row("Model\n[dim]model[/dim]", row["model"] or "Babo default", "Used when the resident runs in opencode.")
        table.add_row("Folders outside the town\n[dim]folders[/dim]", ", ".join(row.get("folders") or []) or "none",
                      "Where a session of its may be given to work (babo session new --folder); comma-separated, or none.")
    for name, default in row["memory_defaults"].items():
        own = row["memory"][name]
        table.add_row(f"[dim]memory.{name}[/dim]", str(own) if own is not None else f"{default} (Babo-wide)",
                      "Memory timing for this agent; 'default' goes back to Babo-wide.")
    console.print(table)


@app.command()
def settings(
    key: str | None = typer.Argument(None, help="A setting to change, e.g. memory.idle_minutes"),
    value: str | None = typer.Argument(None, help="Its new value"),
    config: str | None = _CONFIG_OPT,
):
    """Show the town's settings, or change one (the same list as the web interface's Town › Settings)."""
    from babo.baton import settings as town_settings
    from babo.config.loader import get_config_path

    cfg = _config(config)
    if key is None:
        table = Table("setting", "value", "what it does")
        for s in town_settings.current(cfg.baton):
            table.add_row(f"{s['label']}\n[dim]{s['key']}[/dim]", str(s["value"]) if s["value"] != "" else "(none)", s["help"])
        console.print(table)
        console.print("Change one with: babo settings <setting> <value>")
        return
    if value is None:
        _fail(BatonError("Give a value too, e.g. babo settings memory.idle_minutes 30"))
        return
    try:
        said = town_settings.update(get_config_path(), {key: value})
    except BatonError as exc:
        _fail(exc)
        return
    console.print("\n".join(said) if said else "Nothing changed.")
    console.print("OK: settings saved")


# -- residents' sessions in opencode -----------------------------------------------------

rounds_app = typer.Typer(help="The town's work: rounds (when each runs, its steps by office), and Babo's own upkeep.", no_args_is_help=True)
app.add_typer(rounds_app, name="rounds")


@rounds_app.command("list")
def rounds_list(config: str | None = _CONFIG_OPT):
    """Every round: when it runs, its steps (and who does each), its last run; then what Babo does itself."""
    from babo.baton import rounds

    t, _ = _town(config)
    data = rounds.overview(t)
    table = Table("round", "when", "steps", "last run", "next")
    for r in data["rounds"]:
        steps = "\n".join(f"{i}. {s['office']}" + (f" ({s['holder']})" if s["holder"] and s["holder"] != s["office"] else "")
                          + ("" if s["holder"] else " [vacancy]") for i, s in enumerate(r["steps"], 1))
        when = r["when"] + ("" if r["runs"] == "wait" else "\nruns when triggered") + ("" if r["enabled"] else "\n[off]")
        last = f"{r['last_run'][:16].replace('T', ' ')} {r['last_status']}" if r["last_run"] else "never"
        table.add_row(f"{r['title']}\n[dim]{r['name']}[/dim]", when, steps, last, (r["next_run"] or "-").replace("T", " "))
    console.print(table)
    for name, why in data["broken"].items():
        console.print(f"[red]rounds/{name}.md can't be read:[/red] {why}")
    console.print("[bold]Part of Babo, always:[/bold] " + "; ".join(f"{b['title']} ({b['what']})" for b in data["babo"]))


@rounds_app.command("show")
def rounds_show(name: str = typer.Argument(...), config: str | None = _CONFIG_OPT):
    """A round in full: its steps and duties, and its recent runs with each step's report."""
    from babo.baton import rounds

    t, _ = _town(config)
    try:
        r = rounds.get(t, name)
    except rounds.RoundError as exc:
        _fail(exc)
        return
    console.print(f"[bold]{r.title}[/bold] ({r.name}): {r.describe_when()}; "
                  + ("waits for a free model" if r.runs == "wait" else "runs when triggered") + ("" if r.enabled else "; off"))
    if r.about:
        console.print(r.about)
    for i, step in enumerate(r.steps, 1):
        who = step.who(t)
        console.print(f"  {i}. {step.office}{f' (or {step.fallback})' if step.fallback else ''}: {step.duty}"
                      f"  [dim]{who or 'vacancy'} · material: {step.material}[/dim]")
    for run in rounds.runs(t, name, limit=5):
        console.print(f"\n[bold]{str(run['started'])[:16].replace('T', ' ')}[/bold] {run['status']} ({run['trigger']})")
        for i, step in enumerate(run["steps"], 1):
            console.print(f"  {i}. {step['office']} {step['resident']}: {step['status']} {step['note']}".rstrip())
            if step["report"]:
                console.print("     " + step["report"][:400].replace("\n", "\n     "))


@rounds_app.command("run")
def rounds_run(name: str = typer.Argument(...), here: bool = typer.Option(False, "--here", help="Run it now in this terminal, not through the upkeep service"),
               config: str | None = _CONFIG_OPT):
    """Run a round now: the upkeep service starts it within a minute (or --here, right away)."""
    from babo.baton import rounds

    t, cfg = _town(config)
    try:
        if not here:
            console.print(rounds.request_run(t, name))
            return
        r = rounds.get(t, name)
    except rounds.RoundError as exc:
        _fail(exc)
        return
    from babo.baton.upkeep import Due, run_pass

    result = asyncio.run(run_pass(cfg, Due(reasons=[f"round {name}, run by hand"], rounds=[(name, "run by hand")], residents=[])))
    [last] = rounds.runs(t, name, limit=1) or [{"status": result.get("status"), "steps": []}]
    console.print(f"{r.title}: {last['status']}")
    for i, step in enumerate(last["steps"], 1):
        console.print(f"  {i}. {step['office']} {step['resident']}: {step['status']} {step['note']}".rstrip())


@rounds_app.command("rehearse")
def rounds_rehearse(name: str = typer.Argument(...),
                    live: bool = typer.Option(False, "--live", help="Each question comes to you (Sessions tab) instead of being refused and written down"),
                    here: bool = typer.Option(False, "--here", help="Rehearse now in this terminal (unattended only), not through the upkeep service"),
                    config: str | None = _CONFIG_OPT):
    """Rehearse a round: find out what its steps need before it runs for real; nothing they change is kept."""
    from babo.baton import rehearsal

    t, cfg = _town(config)
    try:
        if not here:
            console.print(rehearsal.request(t, name, live=live))
            return
        if live:
            raise BatonError("A live rehearsal runs through the upkeep service, so you can answer its questions on the Sessions tab.")
        report = rehearsal.rehearse(t, cfg, name, live=False, config_path=_config_path())
    except BatonError as exc:
        _fail(exc)
        return
    _print_rehearsal(report)


def _print_rehearsal(report: Any) -> None:
    console.print(f"[bold]Rehearsal of {report.round}[/bold] ({'live' if report.live else 'unattended'}, {report.started[:16].replace('T', ' ')}): "
                  f"{report.status}" + (f"; approved {report.approved[:16].replace('T', ' ')}" if report.approved else ""))
    for i, (step, wanted) in enumerate(zip(report.steps, report.proposal(), strict=True), 1):
        console.print(f"  {i}. {step.office} {step.resident}: {step.status} {step.note}".rstrip()
                      + (" [yellow](stopped at a refusal: rehearse again once approved to see the rest)[/yellow]" if step.stopped else ""))
        for asked in step.asked:
            console.print(f"     asked to {asked.action}: {' · '.join(asked.resources)} — {asked.decision}")
        if wanted["access"] or wanted["commands"]:
            console.print("     proposes: " + ", ".join([*(f"{k} {v}" for k, v in wanted["access"].items()), *(f"may run `{c}`" for c in wanted["commands"])]))
        for item in wanted["cannot"]:
            console.print(f"     [dim]can't be given to a round: {item}[/dim]")
    console.print(f"Approve its access with: babo rounds approve {report.round}")


@rounds_app.command("report")
def rounds_report(name: str = typer.Argument(...), config: str | None = _CONFIG_OPT):
    """A round's latest rehearsal: what each step asked for, and the access Babo proposes."""
    from babo.baton import rehearsal

    t, _ = _town(config)
    latest = rehearsal.latest(t, name)
    if latest is None:
        _fail(BatonError(f"“{name}” hasn't been rehearsed yet: babo rounds rehearse {name}"))
        return
    _print_rehearsal(latest)


@rounds_app.command("approve")
def rounds_approve(name: str = typer.Argument(...), rehearsal_id: str | None = typer.Option(None, "--rehearsal", help="Which rehearsal (default: the latest)"),
                   config: str | None = _CONFIG_OPT):
    """Give a round exactly the access its rehearsal proposes (it runs with it from then on, without asking)."""
    from babo.baton import rehearsal

    t, _ = _town(config)
    try:
        latest = rehearsal.load(t, rehearsal_id) if rehearsal_id else rehearsal.latest(t, name)
        if latest is None:
            raise BatonError(f"“{name}” hasn't been rehearsed yet: babo rounds rehearse {name}")
        console.print(rehearsal.approve(t, latest.id))
    except BatonError as exc:
        _fail(exc)


@rounds_app.command("access")
def rounds_access(name: str = typer.Argument(...), step: int = typer.Argument(..., help="The step's number"),
                  set_: list[str] = typer.Argument(None, help="Settings, e.g. shell=allow read=town ('profile' goes back to the profile's)"),
                  command: list[str] = typer.Option([], "--command", help="An exact shell command the step may run (repeat for several)"),
                  no_commands: bool = typer.Option(False, "--no-commands", help="Take away the exact commands it had"),
                  config: str | None = _CONFIG_OPT):
    """Set what one step of a round may do, by hand."""
    from babo.baton import rehearsal

    t, _ = _town(config)
    try:
        changes = {k: str(v) for k, v in _overrides(list(set_ or [])).items()}
        commands = [] if no_commands else (list(command) or None)
        console.print(rehearsal.set_step_access(t, name, step, changes, commands))
    except BatonError as exc:
        _fail(exc)


@rounds_app.command("set")
def rounds_set(name: str = typer.Argument(...),
               on: bool | None = typer.Option(None, "--on/--off", help="Turn the round on or off"),
               runs: str | None = typer.Option(None, "--runs", help="wait (for a free model; the default) or now (when triggered, at the lowest priority: it competes with you for a local model)"),
               config: str | None = _CONFIG_OPT):
    """Turn a round on or off, or change when it runs."""
    from babo.baton import rounds

    t, _ = _town(config)
    try:
        r = rounds.set_options(t, name, enabled=on, runs=runs)
    except rounds.RoundError as exc:
        _fail(exc)
        return
    console.print(f"{r.title}: {'on' if r.enabled else 'off'}, {'waits for a free model' if r.runs == 'wait' else 'runs when triggered'}.")
    if r.runs == "now":
        console.print("[yellow]It won't wait for you to finish: while it runs, it shares the model with your chats and can slow them down.[/yellow]")


session_app = typer.Typer(help="Residents' sessions in opencode: start (with a task), watch, open, send, stop, checkpoint, change model, freeze, remove.", no_args_is_help=True)
app.add_typer(session_app, name="session")


def _config_path() -> Path:
    from babo.config.loader import get_config_path

    return get_config_path().resolve()


def _overrides(values: list[str]) -> dict[str, object]:
    out: dict[str, object] = {}
    for item in values:
        key, sep, value = item.partition("=")
        if not sep:
            raise BatonError(f"--set takes setting=value (e.g. shell=deny), not '{item}'")
        out[key.strip()] = value.strip()
    return out


@session_app.command("list")
def session_list(resident: str | None = typer.Argument(None, help="Only this resident"),
                 archived: bool = typer.Option(False, "--archived", help="The archived sessions instead"),
                 tag: str | None = typer.Option(None, "--tag", help="Only sessions with this tag"),
                 config: str | None = _CONFIG_OPT):
    """Each resident's sessions, pinned ones first: what each is doing now, or frozen, or new."""
    from babo.baton import opencode
    from babo.baton.registry import Registry

    t, _ = _town(config)
    residents = [resident] if resident else [r.id for r in Registry(t).all() if r.status == "active" and opencode.runs_in_opencode(r.office)]
    table = Table("resident", "session", "title", "now", "last active", "tags", "changes for this session")
    rows: list[dict[str, Any]] = []
    try:
        for rid in residents:
            rows += [s for s in opencode.overview(t, rid, archived=archived)
                     if tag is None or tag.lower().lstrip("#") in (x.lower() for x in s["tags"])]
    except BatonError as exc:
        _fail(exc)
    for s in sorted(rows, key=lambda s: not s["pinned"]):
        changes = ", ".join(f"{k}={v}" for k, v in s["overrides"].items()) or "-"
        now = s["live"]["state"] + (f": {s['live']['activity']}" if s["live"]["activity"] else "")
        title = ("📌 " if s["pinned"] else "") + s["title"]
        table.add_row(s["resident"], s["id"], title, now, (s["last_active"] or "never")[:16].replace("T", " "),
                      ", ".join(s["tags"]) or "-", changes)
    console.print(table)
    console.print("Start one with: babo session new <resident>   open one with: babo session open <resident> [session]")
    console.print("Organize them with: babo session rename | pin | tag | archive")


@session_app.command("new")
def session_new(
    resident: str = typer.Argument(...),
    task: str = typer.Option("", "--task", help="What it should do; it starts on it right away and works on its own"),
    title: str = typer.Option("", "--title", help="A name for the session (default: the task's first line)"),
    done_check: str = typer.Option("", "--done-check", help="A command that must succeed (in the resident's folder) before the task counts as done, e.g. 'pytest -q'"),
    set_: list[str] = typer.Option([], "--set", help="Change the profile for this session only, e.g. --set shell=deny or --set model=<preset>"),
    folder: str = typer.Option("", "--folder", help="Where it works: one of its projects (garden), or a folder outside the town its profile lists"),
    config: str | None = _CONFIG_OPT,
):
    """Start a new session for a resident (it starts from the resident's profile). With --task, it gets going on its own."""
    from babo.baton import opencode, views

    t, cfg = _town(config)
    try:
        said = views.start_session(t, cfg, resident, title=title, overrides=_overrides(set_), config_path=_config_path(), task=task,
                                   done_check=done_check, folder=folder)
        session = max((s for s in opencode.sessions(t, resident) if s.kind == "work"), key=lambda s: s.created)
    except BatonError as exc:
        _fail(exc)
        return
    console.print(f"OK: {said}")
    console.print(f"Watch or join it with: babo session open {resident} {session.id}   (babo session list shows what it's doing)")


@session_app.command("stop")
def session_stop(resident: str = typer.Argument(...), session_id: str | None = typer.Argument(None), config: str | None = _CONFIG_OPT):
    """Stop what the resident is doing right now; the session stays open."""
    from babo.baton import opencode, views

    t, _ = _town(config)
    try:
        console.print(views.stop_session_turn(t, resident, opencode.get_session(t, resident, session_id).id))
    except BatonError as exc:
        _fail(exc)


@session_app.command("model")
def session_model(resident: str = typer.Argument(...), session_id: str = typer.Argument(...),
                  preset: str = typer.Argument(..., help="A model preset from Settings › Models, or 'default' for the profile's"),
                  config: str | None = _CONFIG_OPT):
    """Give a session another model preset; a running session carries on where it was."""
    from babo.baton import views

    t, cfg = _town(config)
    try:
        console.print(views.set_session_model(t, cfg, resident, session_id, preset, config_path=_config_path()))
    except BatonError as exc:
        _fail(exc)


@session_app.command("open")
def session_open(resident: str = typer.Argument(...), session_id: str | None = typer.Argument(None),
                 config: str | None = _CONFIG_OPT):
    """Open a session in opencode's terminal interface (the latest one, or a new one if there is none)."""
    import os

    from babo.baton import opencode

    t, cfg = _town(config)
    try:
        try:
            session = opencode.get_session(t, resident, session_id)
        except BatonError:
            if session_id is not None:
                raise
            session = opencode.new_session(t, cfg, resident)
        run = opencode.start(t, cfg, session, config_path=_config_path())
        argv, env = opencode.attach_argv(cfg, session, run)
        session = opencode.get_session(t, resident, session.id)
        session.opened = now_iso()
        opencode.save_session(t, session)
    except BatonError as exc:
        _fail(exc)
        return
    console.print(f"Opening {resident}'s session {session.id}. When you leave, it keeps running until you freeze it: babo session freeze {resident}")
    os.execvpe(argv[0], argv, env)  # noqa: S606 - opencode's own terminal interface takes over


@session_app.command("link")
def session_link(resident: str = typer.Argument(...), session_id: str | None = typer.Argument(None), config: str | None = _CONFIG_OPT):
    """A one-time link that opens a running session's web page, connected (no password to type)."""
    from babo.baton import opencode, views

    t, _ = _town(config)
    try:
        console.print(views.session_link(t, resident, opencode.get_session(t, resident, session_id).id))
    except BatonError as exc:
        _fail(exc)


@session_app.command("archive")
def session_archive(resident: str = typer.Argument(...), session_id: str = typer.Argument(...),
                    undo: bool = typer.Option(False, "--undo", help="Bring it back into the list"),
                    config: str | None = _CONFIG_OPT):
    """Tidy a frozen session away into the archive (it stays, with its memory and transcript)."""
    from babo.baton import views

    t, _ = _town(config)
    try:
        console.print(views.archive_session(t, resident, session_id, archived=not undo))
    except BatonError as exc:
        _fail(exc)


def _organize(config: str | None, resident: str, session_id: str, **change: Any) -> None:
    from babo.baton import views

    t, _ = _town(config)
    try:
        console.print(views.organize_session(t, resident, session_id, **change))
    except BatonError as exc:
        _fail(exc)


@session_app.command("set")
def session_set(resident: str = typer.Argument(...), session_id: str = typer.Argument(...),
                set_: list[str] = typer.Argument(None, help="Settings, e.g. shell=allow read=town (a value of 'profile' goes back to the profile's)"),
                everything: bool = typer.Option(False, "--all", help="Allow everything for this session (every capability, reading the whole town)"),
                reset: bool = typer.Option(False, "--reset", help="Back to the resident's profile for everything"),
                config: str | None = _CONFIG_OPT):
    """Change what a session may do, now; a running one restarts with it and carries on where it was."""
    from babo.baton import opencode, views

    t, cfg = _town(config)
    try:
        changes = {k: str(v) for k, v in _overrides(list(set_ or [])).items()}
        if reset:
            changes = {key: "profile" for key in opencode.get_session(t, resident, session_id).overrides} | changes
        console.print(views.change_session_access(t, cfg, resident, session_id, changes, config_path=_config_path(), everything=everything))
    except BatonError as exc:
        _fail(exc)


@session_app.command("rename")
def session_rename(resident: str = typer.Argument(...), session_id: str = typer.Argument(...),
                   title: str = typer.Argument(..., help="Its new name"), config: str | None = _CONFIG_OPT):
    """Give a session a new name (in every list: the web interface's and this one's)."""
    _organize(config, resident, session_id, title=title)


@session_app.command("pin")
def session_pin(resident: str = typer.Argument(...), session_id: str = typer.Argument(...),
                undo: bool = typer.Option(False, "--undo", help="Unpin it"), config: str | None = _CONFIG_OPT):
    """Keep a session at the top of the lists."""
    _organize(config, resident, session_id, pinned=not undo)


@session_app.command("tag")
def session_tag(resident: str = typer.Argument(...), session_id: str = typer.Argument(...),
                tags: list[str] = typer.Argument(None, help="Its tags (none: remove them all)"),
                config: str | None = _CONFIG_OPT):
    """Set a session's tags, the labels you find it by again (`babo session list --tag …`)."""
    _organize(config, resident, session_id, tags=list(tags or []))


@session_app.command("send")
def session_send(resident: str = typer.Argument(...), message: str = typer.Argument(...),
                 session_id: str | None = typer.Option(None, "--session", help="Which session (default: the latest)"),
                 config: str | None = _CONFIG_OPT):
    """Send a resident one message in a session and print the reply."""
    from babo.baton import opencode

    t, cfg = _town(config)
    try:
        try:
            session = opencode.get_session(t, resident, session_id)
        except BatonError:
            if session_id is not None:
                raise
            session = opencode.new_session(t, cfg, resident)
        reply = opencode.send(t, cfg, session, message, config_path=_config_path())
    except BatonError as exc:
        _fail(exc)
        return
    console.print(reply or "(no reply)")


@session_app.command("freeze")
def session_freeze(resident: str = typer.Argument(...), session_id: str | None = typer.Argument(None),
                   all_: bool = typer.Option(False, "--all", help="Freeze every running session of this resident"),
                   config: str | None = _CONFIG_OPT):
    """Stop a session: the resident reviews it into its memory first. Resuming it later continues exactly where it was."""
    from babo.baton import opencode

    t, cfg = _town(config)
    try:
        targets = opencode.sessions(t, resident) if all_ else [opencode.get_session(t, resident, session_id)]
        frozen = [s.id for s in targets if opencode.freeze(t, s, review=cfg, config_path=_config_path())]
    except BatonError as exc:
        _fail(exc)
        return
    console.print(f"OK: frozen {', '.join(frozen)}" if frozen else "Nothing was running.")


@session_app.command("remove")
def session_remove(resident: str = typer.Argument(...), session_id: str = typer.Argument(...),
                   yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask first"),
                   config: str | None = _CONFIG_OPT):
    """Remove a session for good (one that is broken or not wanted). It is stopped without a review; what it did stays in the event log."""
    from babo.baton import views

    t, _cfg = _town(config)
    if not yes and not typer.confirm(f"Remove {resident}'s session {session_id} for good? It won't be reviewed into memory."):
        console.print("Nothing removed.")
        return
    try:
        console.print(views.remove_session(t, resident, session_id))
    except BatonError as exc:
        _fail(exc)


@session_app.command("checkpoint")
def session_checkpoint(resident: str = typer.Argument(...), session_id: str = typer.Argument(...),
                       trigger: str = typer.Option("requested", "--trigger"), config: str | None = _CONFIG_OPT):
    """Have a resident write a checkpoint of a running session now, in its own words (Babo's plugin also calls this)."""
    from babo.baton import activity, opencode

    t, cfg = _town(config)
    try:
        session = opencode.get_session(t, resident, session_id)
        if trigger != "requested":  # (Babo's plugin, on its own schedule: no one is waiting on it)
            console.print(opencode.checkpoint(t, cfg, session, trigger=trigger, config_path=_config_path()))
            return
        with activity.tracked(t, f"checkpoint:{resident}:{session.id}", "checkpoint", f"{resident} is writing a checkpoint",
                              resident=resident, session=session.id), console.status(f"{resident} is writing a checkpoint…"):
            said = opencode.checkpoint(t, cfg, session, trigger=trigger, config_path=_config_path())
        console.print(said)
    except BatonError as exc:
        _fail(exc)


projects_app = typer.Typer(help="The projects board: projects with a lead and a status, and their tickets.", invoke_without_command=True)
app.add_typer(projects_app, name="projects")


@projects_app.callback()
def projects_board(ctx: typer.Context, project: str | None = typer.Option(None, "--project", "-p", help="One project's tickets"),
                   config: str | None = _CONFIG_OPT):
    """The board (or one project's tickets, by status)."""
    from babo.baton import projects

    if ctx.invoked_subcommand:
        return
    t, _ = _town(config)
    try:
        console.print(escape(projects.board_text(t, project)), highlight=False)
    except BatonError as exc:
        _fail(exc)


def _board_change(config: str | None, change: dict[str, Any]) -> None:
    from babo.baton import views

    t, _ = _town(config)
    try:
        console.print(views.change_project_board(t, change))
    except BatonError as exc:
        _fail(exc)


@projects_app.command("new")
def projects_new(title: str = typer.Argument(...), description: str = typer.Option("", "--description", "-d"),
                 lead: str = typer.Option("", "--lead", help="A resident to lead it (default: you)"),
                 help_wanted: bool = typer.Option(False, "--help-wanted", help="Ask residents to take its tickets"),
                 config: str | None = _CONFIG_OPT):
    """Start a project."""
    change: dict[str, Any] = {"action": "new_project", "title": title, "description": description, "help_wanted": help_wanted}
    if lead:
        change["lead"] = lead
    _board_change(config, change)


@projects_app.command("ticket")
def projects_ticket(project: str = typer.Argument(...), title: str = typer.Argument(...),
                    description: str = typer.Option("", "--description", "-d"),
                    assign: str = typer.Option("", "--assign", help="Give it to a resident now"), config: str | None = _CONFIG_OPT):
    """Add a ticket to a project."""
    _board_change(config, {"action": "new_ticket", "project": project, "title": title, "description": description, "assignee": assign})


@projects_app.command("update")
def projects_update(what: str = typer.Argument(..., help="A project (garden) or a ticket (garden#3)"),
                    status: str = typer.Option("", "--status", help="project: active, paused, done; ticket: open, doing, blocked, done"),
                    note: str = typer.Option("", "--note", help="A ticket: a note (and why, when blocked)"),
                    assign: str | None = typer.Option(None, "--assign", help="A ticket: who has it (\"\" to nobody); a project: its lead"),
                    help_wanted: bool | None = typer.Option(None, "--help-wanted/--no-help-wanted"), config: str | None = _CONFIG_OPT):
    """Change a project, or a ticket."""
    if "#" in what:
        change: dict[str, Any] = {"action": "update_ticket", "ticket": what, "status": status, "note": note}
        if assign is not None:
            change["assignee"] = assign
    else:
        change = {"action": "update_project", "project": what, "status": status}
        if assign:
            change["lead"] = assign
        if help_wanted is not None:
            change["help_wanted"] = help_wanted
    _board_change(config, change)


square_app = typer.Typer(help="The town square: threads everyone starts and replies to.", invoke_without_command=True)
app.add_typer(square_app, name="square")


@square_app.callback()
def square_list(ctx: typer.Context, closed: bool = typer.Option(False, "--closed", help="Closed threads too"),
                config: str | None = _CONFIG_OPT):
    """The open threads, newest first."""
    from babo.baton import square

    if ctx.invoked_subcommand:
        return
    t, _ = _town(config)
    found = square.threads(t, include_closed=closed)
    if not found:
        console.print("The square is quiet: no open threads.")
    for thread in found:
        console.print(escape(thread.line()), highlight=False)


def _square_change(config: str | None, change: dict[str, Any]) -> None:
    from babo.baton import views

    t, _ = _town(config)
    try:
        console.print(views.change_square(t, change))
    except BatonError as exc:
        _fail(exc)


@square_app.command("show")
def square_show(thread: str = typer.Argument(...), config: str | None = _CONFIG_OPT):
    """One thread, with its replies."""
    from babo.baton import square

    t, _ = _town(config)
    try:
        console.print(escape(square.get(t, thread).full(last=50)), highlight=False)
    except BatonError as exc:
        _fail(exc)


@square_app.command("post")
def square_post(text: str = typer.Argument(...), kind: str = typer.Option("announcement", "--kind",
                help="question, observation, flag or announcement"), title: str = typer.Option("", "--title"),
                config: str | None = _CONFIG_OPT):
    """Start a thread."""
    _square_change(config, {"action": "post", "kind": kind, "title": title, "text": text})


@square_app.command("reply")
def square_reply(thread: str = typer.Argument(...), text: str = typer.Argument(...), config: str | None = _CONFIG_OPT):
    """Reply to a thread."""
    _square_change(config, {"action": "reply", "thread": thread, "text": text})


@square_app.command("close")
def square_close(thread: str = typer.Argument(...), text: str = typer.Option("", "--note", help="A last word"),
                 config: str | None = _CONFIG_OPT):
    """Close a thread."""
    _square_change(config, {"action": "close", "thread": thread, "text": text})


own_time_app = typer.Typer(help="Own time: a slot for each resident that is its own.", invoke_without_command=True)
app.add_typer(own_time_app, name="own-time")


@own_time_app.callback()
def own_time_list(ctx: typer.Context, config: str | None = _CONFIG_OPT):
    """How often each resident has its own time, when it comes next, and what it did with it last."""
    from babo.baton import own_time

    if ctx.invoked_subcommand:
        return
    t, _ = _town(config)
    shown = own_time.overview(t)
    console.print(f"Own time for the town: {shown['every']}")
    for row in shown["residents"]:
        last = row["recent"][0] if row["recent"] else None
        when = row["next"] or "off"
        console.print(f"- {row['resident']} ({row['every']}{', its own setting' if row['own_setting'] else ''}); next: {when}")
        if last:
            console.print(f"  last ({last['started'][:16].replace('T', ' ')}, {last['status']}): {escape(last['said'][:300])}", highlight=False)


@own_time_app.command("set")
def own_time_set(every: str = typer.Argument(..., help="daily, weekly or off (for one resident: town, to follow the town's)"),
                 resident: str = typer.Option("", "--resident", "-r", help="Only this resident"),
                 config: str | None = _CONFIG_OPT):
    """How often own time comes, for the town or one resident."""
    from babo.baton import own_time

    t, _ = _town(config)
    try:
        console.print(own_time.set_every(t, every, resident))
    except BatonError as exc:
        _fail(exc)


@own_time_app.command("now")
def own_time_now(resident: str = typer.Argument(...), config: str | None = _CONFIG_OPT):
    """Give a resident its own time now (upkeep starts it within a minute, when the model is free)."""
    from babo.baton import own_time

    t, _ = _town(config)
    try:
        console.print(own_time.request(t, resident))
    except BatonError as exc:
        _fail(exc)


@app.command("now")
def now_command(config: str | None = _CONFIG_OPT):
    """What's happening in the town right now: the model queue, what Babo is working on, the running
    sessions and what each is doing, errands, and upkeep."""
    from babo.baton import views

    t, cfg = _town(config)
    now = views.right_now(t, cfg.baton)
    q = now["queue"]
    if "slots" in q:
        console.print(f"Model: {q.get('active', 0)} of {q['slots']} busy, {q.get('waiting', 0)} waiting")
    else:
        console.print("[red]Model: the queue isn't answering.[/red]")
    up = now["upkeep"]
    console.print("Upkeep: " + ("working now" if up.get("running") else
                                ("waiting: " + " · ".join(up["waiting"]) if up.get("waiting") else "nothing is waiting")))
    running = [a for a in now["activity"] if a["active"]]
    for a in running:
        console.print(f"[yellow]{a['status']}[/yellow]  {escape(a['label'])}" + (f"  [dim]{escape(a['detail'])}[/dim]" if a["detail"] else ""))
    for s in now["sessions"]:
        console.print(f"{s['resident']}  {escape(s['title'])}  [dim]{s['state']}{' · ' + escape(s['doing']) if s['doing'] else ''}[/dim]")
    for e in now["errands"]:
        console.print(f"errand for {e['resident']}  {escape(e['task'])}  [dim]{e['status']}[/dim]")
    if not (running or now["sessions"] or now["errands"]):
        console.print("All quiet: nothing is running.")


@app.command("logs")
def logs_command(service: str = typer.Argument("gateway", help="gateway, queue or upkeep"),
                 lines: int = typer.Option(50, "-n", "--lines", help="How many entries"),
                 level: str = typer.Option("INFO", "--level", help="DEBUG, INFO, WARNING or ERROR (and up)")):
    """A Babo service's recent log (the same as the Town page's Activity tab)."""
    from babo.baton import logs

    if service not in logs.SERVICES:
        _fail(BatonError(f"the services are: {', '.join(logs.SERVICES)}"))
    entries = logs.tail(service, lines, level=level)
    if not entries:
        console.print(f"Nothing logged yet in {logs.path(service)}. The service writes it once it's been restarted on this version.")
        return
    for e in entries:
        style = {"WARNING": "yellow", "ERROR": "red", "CRITICAL": "red", "DEBUG": "dim"}.get(e["level"])
        console.print(f"[{style}]{escape(e['text'])}[/{style}]" if style else escape(e["text"]), highlight=False)


@app.command("activity")
def activity_command(config: str | None = _CONFIG_OPT):
    """What Babo is working on now (checkpoints, freezes, rounds, rehearsals, evals, upkeep), and what just finished."""
    from datetime import datetime

    from babo.baton import activity

    t, _ = _town(config)
    entries = activity.recent(t)
    if not entries:
        console.print("Nothing is running, and nothing finished in the last half hour.")
        return
    now = datetime.now().astimezone()
    for e in entries:
        since = int((now - datetime.fromisoformat(e.finished or e.started)).total_seconds()) if (e.finished or e.started) else 0
        ago = f"{since // 60} min" if since >= 60 else f"{since} s"
        mark = {"waiting": "[dim]waiting[/dim]", "running": "[yellow]running[/yellow]", "done": "[green]done[/green]",
                "failed": "[red]failed[/red]"}[e.status]
        when = f"for {ago}" if e.active else f"{ago} ago"
        console.print(f"{mark}  {escape(e.label)}  [dim]{when}{' · ' + escape(e.detail) if e.detail else ''}[/dim]")


@app.command()
def notices(read: bool = typer.Option(False, "--read", help="Mark them all as read"), config: str | None = _CONFIG_OPT):
    """What happened that you'd want to know: sessions wrapped up, residents waiting for your answer."""
    from babo.baton import notices as town_notices

    t, _ = _town(config)
    items = town_notices.recent(t, unread_only=True)
    for item in items:
        console.print(f"- {str(item['at'])[:16].replace('T', ' ')}  {item['text']}")
    if not items:
        console.print("Nothing new.")
    if read and items:
        town_notices.mark_read(t)
        console.print("OK: marked as read")


@app.command("allow-all")
def allow_all_command(
    session: str | None = typer.Argument(None, help="The Mayor's chat, e.g. websocket:<id> or discord:<channel> (see the list)"),
    hours: float | None = typer.Option(None, "--for", help="Hours it stays on (default: until you turn it off)"),
    off: bool = typer.Option(False, "--off", help="Turn it off for that chat"),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation"),
    config: str | None = _CONFIG_OPT,
):
    """Let one of the Mayor's chats send residents on errands without your review (or turn that off)."""
    from babo.baton import allow_all, views

    t, _ = _town(config)
    try:
        if session is None:
            grants = allow_all.active(t)
            console.print("[bold]Allow all is on for:[/bold] " + (", ".join(
                f"{k} ({'until ' + v['until'][:16].replace('T', ' ') if v.get('until') else 'until you turn it off'})"
                for k, v in grants.items()) or "no chat"))
            table = Table("the Mayor's recent chats", "last active")
            for chat in views.mayor_chats(t):
                table.add_row(chat["key"], chat["last_active"][:16].replace("T", " "))
            console.print(table)
            console.print("Turn it on with: babo allow-all <chat> [--for HOURS]   off with: babo allow-all <chat> --off")
            return
        if off:
            console.print(allow_all.turn_off(t, session))
            return
        console.print(ALLOW_ALL_WARNING)
        if not yes and not typer.confirm(f"Turn allow-all on for {session}?", default=False):
            console.print("Nothing changed.")
            return
        console.print(allow_all.turn_on(t, session, hours=hours))
    except BatonError as exc:
        _fail(exc)


ALLOW_ALL_WARNING = """[bold yellow]Allow all: the Mayor's errands from this chat run without your review.[/bold yellow]
- He can ask any resident to run anything, with any access: commands run as your user, on this machine.
- Anyone who can talk to him in this chat, and anything he reads (web pages, messages), can lead to commands running.
- Each request still runs in a resident's opencode session, and is recorded (Town › Review › Recently run).
- Turn it off when you're done: babo allow-all <chat> --off"""


# -- the office -------------------------------------------------------------
office_app = typer.Typer(help="The office: projects with one lead and a crew, and the agents they run as.",
                         invoke_without_command=True)
app.add_typer(office_app, name="office")


def _office_core(_cfg: Config) -> OfficeManager:
    """The native manager core (babo/office/core.py). Until it lands, the CLI says so."""
    from babo.office.api import manager_for

    return manager_for(_cfg)


@office_app.callback()
def office_board(ctx: typer.Context, config: str | None = _CONFIG_OPT) -> None:
    """The office as the Manager reports it (the same view as the Office page)."""
    from babo.office import control

    if ctx.invoked_subcommand:
        return
    try:
        t, cfg = _town(config)
        view = control.office_view(t, _office_core(cfg))  # type: ignore[arg-type]
    except BatonError as exc:
        _fail(exc)
        return
    backend = cast(dict[str, object], view["manager"])
    if not backend.get("available"):
        console.print(f"[yellow]The Manager can't be read: {backend.get('said')}[/yellow]")
    for project in cast(list[dict[str, object]], view["projects"]):
        console.print(f"\n[bold]{project['title']}[/bold] ({project['slug']}) — {project['open_tickets']} open")
        console.print(f"  lead: {project['lead'] or '(unset)'}   crew: {', '.join(cast(list[str], project['crew'])) or '(none)'}")
        for agent in cast(list[dict[str, object]], view["agents"]):
            if agent.get("project") != project["slug"]:
                continue
            role = f" ({agent['role']})" if agent.get("role") else ""
            console.print(f"  - {agent['handle']}{role}: {agent['status']}"
                          + (f" — {agent['said']}" if agent.get("said") else ""))
    loose = [a for a in cast(list[dict[str, object]], view["agents"]) if not a.get("project")]
    for agent in loose:
        console.print(f"  - {agent['handle']}: {agent['status']}" + (f" — {agent['said']}" if agent.get("said") else ""))


def _office_verb(verb: str, handle: str, reason: str, config: str | None,
                 actor_name: str = "") -> None:
    from babo.office import control
    from babo.office.actors import terminal_actor

    try:
        t, cfg = _town(config)
        # The terminal's actor is the OS user running this command (or --actor),
        # never a constant: if it can't be determined, the action is refused.
        actor = terminal_actor(actor_name)
        op = control.perform_sync(_office_core(cfg), handle, verb, reason,
                                  store=control.AgentStore(t), actor=actor)  # type: ignore[arg-type]
    except BatonError as exc:
        _fail(exc)
        return
    console.print(f"{op.state}: {op.said}" if op.said else op.state)
    if op.state != "succeeded":
        raise typer.Exit(1)


@office_app.command("wake")
def office_wake(handle: str = typer.Argument(...), reason: str = typer.Option("", "--reason"),
                actor: str = typer.Option("", "--actor", help="Who is asking (default: your OS user)"),
                config: str | None = _CONFIG_OPT) -> None:
    """Wake a frozen agent (the core's own verb)."""
    _office_verb("wake", handle, reason or "wake", config, actor)


@office_app.command("freeze")
def office_freeze(handle: str = typer.Argument(...), reason: str = typer.Option("", "--reason"),
                  actor: str = typer.Option("", "--actor", help="Who is asking (default: your OS user)"),
                  config: str | None = _CONFIG_OPT) -> None:
    """Freeze an agent: it stands down and saves its work, then its pod is released. The work survives; the process does not."""
    _office_verb("freeze", handle, reason or "freeze", config, actor)


@office_app.command("spawn")
def office_spawn(handle: str = typer.Argument(...), reason: str = typer.Option("", "--reason"),
                 actor: str = typer.Option("", "--actor", help="Who is asking (default: your OS user)"),
                 config: str | None = _CONFIG_OPT) -> None:
    """Bring one agent up for the first time (the core decides whether it may)."""
    _office_verb("spawn", handle, reason or "spawn", config, actor)


@office_app.command("rescue")
def office_rescue(handle: str = typer.Argument(...), reason: str = typer.Option("", "--reason"),
                  actor: str = typer.Option("", "--actor", help="Who is asking (default: your OS user)"),
                  config: str | None = _CONFIG_OPT) -> None:
    """Preserve the work, replace the pod and its credentials (the core's own semantics)."""
    _office_verb("rescue", handle, reason or "rescue", config, actor)


@office_app.command("destroy")
def office_destroy(handle: str = typer.Argument(...), reason: str = typer.Option("", "--reason"),
                   actor: str = typer.Option("", "--actor", help="Who is asking (default: your OS user)"),
                   config: str | None = _CONFIG_OPT) -> None:
    """Tear the agent down and revoke what it holds. The work is not preserved."""
    _office_verb("destroy", handle, reason or "destroy", config, actor)


@office_app.command("record")
def office_record(handle: str = typer.Argument(...), resident: str = typer.Option(..., "--resident", "-r"),
                  project: str = typer.Option("", "--project", "-p"),
                  desired: str = typer.Option("running", "--desired"), config: str | None = _CONFIG_OPT) -> None:
    """Record what you want for one agent (desired state only; the Manager still decides)."""
    from babo.office import control

    try:
        t, _cfg = _town(config)
        console.print(control.record_agent(t, {"handle": handle, "resident": resident, "project": project,
                                               "desired": desired})["message"])
    except BatonError as exc:
        _fail(exc)


@office_app.command("crew")
def office_crew(project: str = typer.Argument(...), lead: str = typer.Option("", "--lead"),
                crew: str = typer.Option("", "--crew", help="Comma-separated agent handles"),
                config: str | None = _CONFIG_OPT) -> None:
    """Set a project's one lead and its crew."""
    from babo.office import control

    try:
        t, _cfg = _town(config)
        said = control.change_crew(t, {"project": project, "lead": lead,
                                       "crew": [h.strip() for h in crew.split(",") if h.strip()]})
        console.print(said["message"])
    except BatonError as exc:
        _fail(exc)


# -- the web interface's operator verbs -------------------------------------
ui_app = typer.Typer(help="The web interface's operator verbs (the terminal half of the UI).",
                     invoke_without_command=True)
app.add_typer(ui_app, name="ui")


def _webui_base(cfg: Config) -> str:
    """The WebUI listener, not the gateway's health server.

    They are different processes-in-one: the SPA, /api and /webui/bootstrap are
    served by the websocket channel (channels.websocket.host/port); gateway.host/
    port is the lightweight /health server. Pointing this at gateway.* would make
    `babo ui revoke-sessions` POST at a health endpoint and fail confusingly —
    in a pod, where the two ports differ (8843 UI, 18790 health), always.
    """
    socket_path = _webui_socket(cfg)
    if socket_path:
        # A pod listener serves a 0600 Unix socket: local by construction, so an
        # operator command next to it needs no trusted-source entry and no token
        # minted for a network peer. Nothing outside the pod can reach it.
        return f"unix:{socket_path}"
    channel = cast(dict[str, Any], getattr(cfg.channels, "websocket", None) or {})
    host = str(channel.get("host") or "127.0.0.1")
    port = int(channel.get("port") or 8765)
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"  # we are on this machine; talk to it directly
    return f"http://{host}:{port}"


def _webui_socket(cfg: Config) -> str:
    """The listener's own Unix socket, when it has one (a pod, typically)."""
    channel = cast(dict[str, Any], getattr(cfg.channels, "websocket", None) or {})
    return str(channel.get("unix_socket_path") or "")


def _post_json(url: str, token: str = "", payload: dict[str, Any] | None = None) -> tuple[int, str]:
    """POST ``url`` (``http://host:port/path`` or ``unix:/path/socket``).

    The Unix form is how an operator command reaches the gateway from inside a
    pod: filesystem permissions are the gate, so there is no network peer to
    trust and no second external door.
    """
    import json as _json
    import socket
    from http.client import HTTPConnection

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = _json.dumps(payload or {}).encode()

    if url.startswith("unix:"):
        socket_path, _sep, path = url.split("unix:", 1)[1].partition("/")
        target = "/" + path

        class _UnixConnection(HTTPConnection):
            def __init__(self) -> None:
                super().__init__("localhost")

            def connect(self) -> None:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(10)
                self.sock.connect(socket_path)

        connection: HTTPConnection = _UnixConnection()
    else:
        netloc, _sep, path = url.split("//", 1)[1].partition("/")
        target = "/" + path
        connection = HTTPConnection(netloc, timeout=10)
    try:
        connection.request("POST", target, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    except OSError as exc:
        return 0, str(exc)
    finally:
        connection.close()


def _get_json(url: str) -> tuple[int, str]:
    """GET ``url`` over TCP or the listener's Unix socket (see ``_post_json``)."""
    import socket
    from http.client import HTTPConnection

    if url.startswith("unix:"):
        socket_path, _sep, path = url.split("unix:", 1)[1].partition("/")
        target = "/" + path

        class _UnixConnection(HTTPConnection):
            def __init__(self) -> None:
                super().__init__("localhost")

            def connect(self) -> None:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(10)
                self.sock.connect(socket_path)

        connection: HTTPConnection = _UnixConnection()
    else:
        netloc, _sep, path = url.split("//", 1)[1].partition("/")
        target = "/" + path
        connection = HTTPConnection(netloc, timeout=10)
    try:
        connection.request("GET", target)
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    except OSError as exc:
        return 0, str(exc)
    finally:
        connection.close()


@ui_app.callback()
def ui_root(ctx: typer.Context) -> None:
    """Operator verbs for the web interface. Run one."""
    if ctx.invoked_subcommand:
        return
    console.print("Usage: babo ui revoke-sessions   (sign out every browser session)")


@ui_app.command("revoke-sessions")
def ui_revoke_sessions(config: str | None = _CONFIG_OPT) -> None:
    """Sign out every browser session: the token store is in memory, so this and a
    restart are the two operator revocations.

    Inside a pod this goes over the listener's own Unix socket when there is one
    (0600, local by construction: no trusted-source entry, no token minted for a
    network peer); otherwise over loopback TCP. Either way it is the same
    bootstrap-then-revoke path the web interface uses.
    """
    import json as _json

    cfg = _config(config)
    base = _webui_base(cfg)
    status, body = _get_json(f"{base}/webui/bootstrap")
    if status == 0:
        # A transport problem, not an auth one: name the socket and the OS error,
        # because "no token issued" would send the reader looking at the wrong door.
        _fail(BatonError(f"couldn't reach the gateway at {base}: {body.strip()[:200]}"))
        return
    token = str(_json.loads(body).get("api_token") or "") if status == 200 and body.strip() else ""
    if not token:
        _fail(BatonError(f"the gateway issued no API token ({status}) at {base}; "
                         "is token_issue_path set, or is this a trusted source?"))
        return
    status, body = _post_json(f"{base}/api/webui/tokens/revoke", token)
    if status != 200:
        _fail(BatonError(f"revoke failed ({status}): {body.strip()[:200]}"))
        return
    said = _json.loads(body).get("revoked", 0) if body.strip() else 0
    console.print(f"Signed out every browser session ({said} token(s) cleared).")
