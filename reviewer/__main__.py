"""CLI entrypoint pro multi-agent code reviewer.

Spuštění:

    python -m reviewer <soubor>           # review konkrétního souboru
    python -m reviewer --diff             # review aktuálních neuložených změn
    python -m reviewer - < soubor         # review ze stdin
    python -m reviewer file.py --output report.md
    python -m reviewer file.py --json     # strojově čitelný výstup

Pokud máš nainstalovaný Claude Code (`claude` na PATH), SDK použije tvoji
přihlašovací kontextu — žádný API klíč nepotřebuješ.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .orchestrator import FinalReview, review_code


def _read_input(args: argparse.Namespace) -> tuple[str, str]:
    """Vrátí (kód, popisek) podle CLI argumentů."""
    if args.diff:
        try:
            output = subprocess.run(
                ["git", "diff", "--no-color"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            sys.exit(f"Chyba: nepodařilo se spustit `git diff`: {exc}")
        if not output.stdout.strip():
            sys.exit("V pracovním adresáři nejsou žádné neuložené změny.")
        return output.stdout, "git diff (HEAD)"

    if args.path == "-":
        return sys.stdin.read(), "<stdin>"

    path = Path(args.path)
    if not path.exists():
        sys.exit(f"Chyba: soubor '{path}' neexistuje.")
    if not path.is_file():
        sys.exit(f"Chyba: '{path}' není soubor.")
    try:
        return path.read_text(encoding="utf-8"), str(path)
    except UnicodeDecodeError:
        sys.exit(f"Chyba: '{path}' není UTF-8 textový soubor.")


def _render_progress(console: Console, file_label: str) -> None:
    """Vypíše pretty hlavičku do terminálu."""
    console.print(
        Panel.fit(
            f"[bold]Cíl review:[/bold] [cyan]{file_label}[/cyan]\n"
            "[dim]3 specialisté běží paralelně → supervisor je sloučí.[/dim]",
            title="Multi-agent code reviewer",
            border_style="cyan",
        )
    )


def _render_stats(console: Console, review: FinalReview) -> None:
    """Vypíše tabulku se statistikami všech čtyř agentů."""
    table = Table(title="Statistika běhu", show_header=True, header_style="bold")
    table.add_column("Agent", style="cyan")
    table.add_column("Délka", justify="right")
    table.add_column("Cena USD", justify="right")

    for r in review.specialist_reports:
        table.add_row(r.label, f"{r.duration_s:.1f} s", f"${r.cost_usd:.4f}")

    table.add_row(
        "Supervisor",
        f"{review.supervisor_duration_s:.1f} s",
        f"${review.supervisor_cost_usd:.4f}",
    )
    table.add_section()
    table.add_row(
        "[bold]Celkem (kritická cesta)[/bold]",
        f"[bold]{review.total_duration_s:.1f} s[/bold]",
        f"[bold]${review.total_cost_usd:.4f}[/bold]",
    )
    console.print(table)


def _to_json(review: FinalReview, file_label: str) -> str:
    payload = {
        "file": file_label,
        "final_report_markdown": review.final_report,
        "specialists": [
            {
                "name": r.name,
                "label": r.label,
                "duration_s": round(r.duration_s, 3),
                "cost_usd": round(r.cost_usd, 6),
                "report": r.content,
            }
            for r in review.specialist_reports
        ],
        "supervisor": {
            "duration_s": round(review.supervisor_duration_s, 3),
            "cost_usd": round(review.supervisor_cost_usd, 6),
        },
        "totals": {
            "duration_s": round(review.total_duration_s, 3),
            "cost_usd": round(review.total_cost_usd, 6),
        },
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reviewer",
        description="Multi-agent code reviewer (Supervisor + Parallel) postavený "
        "na Claude Agent SDK. Tři specialisté + supervisor.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="Cesta k souboru ke review, nebo '-' pro stdin. "
        "Vynech, pokud používáš --diff.",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Místo souboru reviewuj neuložené změny v aktuálním git repu.",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        help="Zapiš finální markdown report do souboru (kromě výpisu do "
        "terminálu).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Strojově čitelný výstup (JSON) místo pretty markdownu.",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    console = Console()

    if not args.diff and not args.path:
        console.print(
            "[red]Chyba:[/red] musíš zadat buď cestu k souboru, nebo --diff.\n"
            "Spusť `python -m reviewer --help` pro detail."
        )
        return 2

    code, file_label = _read_input(args)
    _render_progress(console, file_label)

    with console.status("[cyan]Probíhá review…[/cyan]", spinner="dots"):
        review = await review_code(code, file_label=file_label)

    if args.json:
        print(_to_json(review, file_label))
    else:
        console.print()
        console.print(Markdown(review.final_report))
        console.print()
        _render_stats(console, review)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(review.final_report, encoding="utf-8")
        console.print(f"[green]Report uložen do[/green] [cyan]{args.output}[/cyan]")

    return 0


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        exit_code = asyncio.run(_run(args))
    except KeyboardInterrupt:
        sys.exit("\nPřerušeno uživatelem.")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
