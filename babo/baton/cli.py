"""``babo …`` commands."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table

from babo.baton.town import BatonError, Town

if TYPE_CHECKING:
    from babo.config.schema import Config

app = typer.Typer(help="The Baton town: residents, memory, night cycle, review.", no_args_is_help=True)
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
    config: str | None = _CONFIG_OPT,
):
    """Found the town: CHARTER, HOUSE, REGISTRY and the three founding residents. Safe to repeat."""
    from babo.baton.founding import found_town
    from babo.baton.search import reindex as do_reindex

    try:
        cfg = _config(config)
        root = Path(town).expanduser() if town else cfg.baton.town_path
        t, created = found_town(root)
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
    """Rebuild the search index from the town's files."""
    from babo.baton.search import reindex as do_reindex

    try:
        t, cfg = _town(config)
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
        box.me
    except BatonError as exc:
        _fail(exc)
        return
    asyncio.run(serve(box))


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
def night(
    config: str | None = _CONFIG_OPT,
    date: str | None = typer.Option(None, "--date", help="Night to run or resume (default: today)"),
):
    """Run (or resume) the night cycle. Exits 0 even when the model is unreachable."""
    from babo.baton.night import run_night

    try:
        cfg = _config(config)
        result = run_night(cfg, night=date)
    except BatonError as exc:
        _fail(exc)
        return
    if result["status"] == "skipped":
        console.print(f"Night skipped: {result['reason']}. It will resume next time.")
        return
    console.print(f"OK: night {result['night']} finished ({len(result['steps'])} steps)")


@app.command()
def units(
    out: Path = typer.Option(Path("install/out/systemd"), "--out", help="Where to write the unit files"),
    config: str | None = _CONFIG_OPT,
):
    """Write the systemd user units (queue, gateway, night timer) for Squid to install on the host."""
    from babo.baton.units import write_units

    cfg = _config(config)
    for path in write_units(cfg.baton, out):
        console.print(f"wrote {path}")
    console.print("OK: units written (installing them on the host is a Squid step)")


@app.command()
def note(resident: str = typer.Argument(...), text: str = typer.Argument(...), config: str | None = _CONFIG_OPT):
    """Leave a note from Squid in a resident's inbox."""
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
        t, _ = _town(config)
        if name is not None:
            console.print(views.name_town(t, name))
    except BatonError as exc:
        _fail(exc)
        return
    view = views.town_overview(t)
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


@app.command()
def review(config: str | None = _CONFIG_OPT):
    """The weekly check-in: tools, proposals, founding requests, old flags. Plain questions, one at a time."""
    from babo.baton import gitops, growth, views

    try:
        t, _ = _town(config)
    except BatonError as exc:
        _fail(exc)
        return
    decided = 0
    console.print("[bold]Weekly review[/bold]. Press Enter to skip anything; nothing changes unless you decide.\n")

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
        gitops.commit_all(t, f"Weekly review: {decided} decision(s) by Squid")
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
    written, backup = write_town_config(
        path, town=town, upstream=upstream, model=model, n_ctx=n_ctx, slots=slots, bwrap=bwrap, box=box,
        babo_path=_shutil.which("babo") or "",
    )
    if backup:
        console.print(f"Backed up the previous config to {backup}")
    _config(str(written))  # must load and validate
    console.print(f"OK: config written to {written}")
