"""``nanobot baton …`` commands."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table

from nanobot.baton.town import BatonError, Town

if TYPE_CHECKING:
    from nanobot.config.schema import Config

app = typer.Typer(help="The Baton town: residents, memory, night cycle, review.", no_args_is_help=True)
console = Console()

_CONFIG_OPT = typer.Option(None, "--config", "-c", help="Path to the nanobot config (default: the usual one)")


def _config(config: str | None) -> Config:
    from nanobot.config.loader import get_config_path, load_config, set_config_path

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
    from nanobot.baton.founding import found_town
    from nanobot.baton.search import reindex as do_reindex

    try:
        cfg = _config(config)
        root = Path(town).expanduser() if town else cfg.baton.town_path
        t, created = found_town(root)
        do_reindex(t, cfg.baton.index_file)
    except BatonError as exc:
        _fail(exc)
        return
    if created:
        console.print(f"Founded at {t.root}: " + ", ".join(created))
    else:
        console.print(f"The town at {t.root} already exists; nothing to do.")
    console.print("OK: town ready")


@app.command()
def reindex(config: str | None = _CONFIG_OPT):
    """Rebuild the search index from the town's files."""
    from nanobot.baton.search import reindex as do_reindex

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
    from nanobot.baton.tools.core import Toolbox

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
    from nanobot.baton.bundle import build_bundle

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
    from nanobot.baton.registry import Registry

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
    from nanobot.baton.mcp_server import serve
    from nanobot.baton.tools.core import Toolbox

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
    from nanobot.baton.queue import serve

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
    from nanobot.baton.context_view import assemble_next, last_request_text

    try:
        t, cfg = _town(config)
        from nanobot.baton.registry import Registry

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
    from nanobot.baton.compaction import restore_session_backup
    from nanobot.session.manager import SessionManager

    try:
        t, cfg = _town(config)
        from nanobot.baton.registry import Registry

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
    from nanobot.baton.night import run_night

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
    from nanobot.baton.units import write_units

    cfg = _config(config)
    for path in write_units(cfg.baton, out):
        console.print(f"wrote {path}")
    console.print("OK: units written (installing them on the host is a Squid step)")
