"""``babo …`` commands."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.table import Table

from babo.baton.town import BatonError, Town

if TYPE_CHECKING:
    from babo.baton.requests import SessionRequest
    from babo.config.schema import Config

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
    config: str | None = _CONFIG_OPT,
):
    """Serve the Baton tools over MCP stdio for one resident."""
    from babo.baton.mcp_server import serve
    from babo.baton.tools.core import Toolbox

    if not as_:
        _fail(BatonError("--as <resident> is required"))
    try:
        t, cfg = _town(config)
        box = Toolbox(t, as_, cfg.baton.index_file)
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
    from babo.baton.queue import serve

    cfg = _config(config)
    console.print(f"Model queue on {cfg.baton.model.queue_host}:{cfg.baton.model.queue_port} -> {cfg.baton.model.upstream}")
    asyncio.run(serve(cfg.baton.model, cfg.baton.capture_dir))


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
    said = do_tidy(babo_home=get_config_path().resolve().parent, user_home=Path.home(), checkout=checkout)
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
    posts = [f"- [{b['kind']}] {b['text'][:90]} ({b['author']})" for b in view["board"]]
    console.print("\nBoard:\n" + ("\n".join(posts) if posts else "- (nothing open)"))
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
    """Show one session request word for word and ask the owner to approve, decline or skip it.

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
    console.print("  It runs with nobody at a terminal; anything not allowed is denied.")
    choice = _ask_decision("Approve and run it?", "approve", "decline")
    if choice == "skip":
        return False
    try:
        console.print(views.decide(t, "session", request.id, yes=choice == "yes", reason=_reason(), digest=shown["digest"]))
        return True
    except BatonError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        return False


def ask_about_new_requests(t: Town, cfg: Config, seen: set[str]) -> None:
    """In a live terminal chat: ask about each session request waiting for the owner that this chat hasn't shown yet."""
    from babo.baton import requests as session_requests

    for request in session_requests.pending(t):
        if request.id in seen:
            continue
        seen.add(request.id)
        console.print("\n[bold yellow]A session request is waiting for your approval.[/bold yellow]")
        if not ask_about_request(t, cfg, request):
            console.print("  Skipped: it waits in [bold]babo review[/bold] and on the Town page.")


@app.command()
def review(config: str | None = _CONFIG_OPT):
    """The weekly check-in: session requests, tools, proposals, founding requests, old flags. Plain questions, one at a time."""
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
    console.print(f"[bold]Session requests[/bold] ({len(asked)})")
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
    console.print(f"\n[bold]3. Founding requests[/bold] ({len(requests)})")
    for req in requests:
        meta, body = split_frontmatter(req.read_text(encoding="utf-8"))
        console.print(f"\n• New office '{meta.get('office')}' requested by {meta.get('author')}:\n{body.strip()[:1500]}")
        console.print("  A new resident uses compute on your machine every day it is active.")
        choice = _ask_decision("Found this resident?", "approve", "decline")
        if choice != "skip":
            console.print(views.decide(t, "founding", req.name, yes=choice == "yes", reason=_reason(), commit=False))
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
        message = views.update_profile(town, cfg.baton, agent, {key: value}, mcp_known)
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
    table.add_row("MCP servers\n[dim]mcp[/dim]", ", ".join(row["mcp"]) or "none",
                  f"Known in Babo: {', '.join(row['mcp_known']) or 'none'}")
    if not row["main"]:
        table.add_row("Model\n[dim]model[/dim]", row["model"] or "Babo default", "Used when the resident runs in opencode.")
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

session_app = typer.Typer(help="Residents' sessions in opencode: start, open, send, freeze, remove.", no_args_is_help=True)
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
def session_list(resident: str | None = typer.Argument(None, help="Only this resident"), config: str | None = _CONFIG_OPT):
    """Each resident's sessions: running, frozen or new."""
    from babo.baton import opencode
    from babo.baton.registry import Registry

    t, _ = _town(config)
    residents = [resident] if resident else [r.id for r in Registry(t).all() if r.status == "active" and opencode.runs_in_opencode(r.office)]
    table = Table("resident", "session", "title", "status", "last active", "changes for this session")
    try:
        for rid in residents:
            for s in opencode.overview(t, rid):
                changes = ", ".join(f"{k}={v}" for k, v in s["overrides"].items()) or "-"
                table.add_row(rid, s["id"], s["title"], s["status"], (s["last_active"] or "never")[:16].replace("T", " "), changes)
    except BatonError as exc:
        _fail(exc)
    console.print(table)
    console.print("Start one with: babo session new <resident>   open one with: babo session open <resident> [session]")


@session_app.command("new")
def session_new(
    resident: str = typer.Argument(...),
    title: str = typer.Option("", "--title", help="What the session is for"),
    set_: list[str] = typer.Option([], "--set", help="Change the profile for this session only, e.g. --set shell=deny"),
    config: str | None = _CONFIG_OPT,
):
    """Start a new session for a resident (it starts from the resident's profile)."""
    from babo.baton import opencode

    t, cfg = _town(config)
    try:
        session = opencode.new_session(t, cfg, resident, title=title, overrides=_overrides(set_))
        run = opencode.start(t, cfg, session, config_path=_config_path())
    except BatonError as exc:
        _fail(exc)
        return
    console.print(f"OK: session {session.id} for {resident} is running ({run.url}, user opencode, password {run.password})")
    console.print(f"Open it in the terminal with: babo session open {resident} {session.id}")


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
    except BatonError as exc:
        _fail(exc)
        return
    console.print(f"Opening {resident}'s session {session.id}. When you leave, it keeps running until you freeze it: babo session freeze {resident}")
    os.execvpe(argv[0], argv, env)  # noqa: S606 - opencode's own terminal interface takes over


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


@session_app.command("checkpoint", hidden=True)
def session_checkpoint(resident: str = typer.Argument(...), session_id: str = typer.Argument(...),
                       trigger: str = typer.Option("requested", "--trigger"), config: str | None = _CONFIG_OPT):
    """Ask a resident in opencode for a checkpoint (Babo's plugin calls this at the right moments)."""
    from babo.baton import opencode

    t, cfg = _town(config)
    try:
        console.print(opencode.checkpoint(t, cfg, opencode.get_session(t, resident, session_id), trigger=trigger, config_path=_config_path()))
    except BatonError as exc:
        _fail(exc)


@app.command("allow-all")
def allow_all_command(
    session: str | None = typer.Argument(None, help="The Mayor's chat, e.g. websocket:<id> or discord:<channel> (see the list)"),
    hours: float | None = typer.Option(None, "--for", help="Hours it stays on (default: until you turn it off)"),
    off: bool = typer.Option(False, "--off", help="Turn it off for that chat"),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation"),
    config: str | None = _CONFIG_OPT,
):
    """Let one of the Mayor's chats run session requests without your review (or turn that off)."""
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


ALLOW_ALL_WARNING = """[bold yellow]Allow all: the Mayor's session requests from this chat run without your review.[/bold yellow]
- He can ask any resident to run anything, with any access: commands run as your user, on this machine.
- Anyone who can talk to him in this chat, and anything he reads (web pages, messages), can lead to commands running.
- Each request still runs in a resident's opencode session, and is recorded (Town › Review › Recently run).
- Turn it off when you're done: babo allow-all <chat> --off"""
