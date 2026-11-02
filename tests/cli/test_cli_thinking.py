"""The terminal shows a model's thinking labelled and set apart, never read as markup."""

from __future__ import annotations

from rich.console import Console

from babo.cli import terminal


def test_thinking_is_labelled_and_brackets_in_it_are_kept(monkeypatch) -> None:
    out = Console(record=True, width=120, force_terminal=False)
    monkeypatch.setattr(terminal, "console", out)
    terminal._print_cli_reasoning("Check [the board] first.", None)  # pyright: ignore[reportPrivateUsage]
    assert out.export_text() == "[thinking]\nCheck [the board] first.\n[/thinking]\n"
