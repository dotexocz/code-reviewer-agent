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

from .orchestrator import (
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_SCORE_THRESHOLD,
    FinalReview,
    review_code,
)


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
            "[dim]Preflight (Conditional) → specialisté (Parallel) → "
            "supervisor → loop pokud skóre nízké.[/dim]",
            title="Multi-agent code reviewer",
            border_style="cyan",
        )
    )


def _render_preflight(console: Console, review: FinalReview) -> None:
    """Vypíše rozhodnutí Conditional preflighteru, pokud běžel."""
    if review.preflight is None:
        return
    p = review.preflight
    specialists_str = ", ".join(p.specialists) if p.specialists else "(žádný)"
    console.print(
        Panel.fit(
            f"[bold]Jazyk:[/bold] {p.language}    "
            f"[bold]Typ:[/bold] {p.file_type}\n"
            f"[bold]Aktivováno:[/bold] [cyan]{specialists_str}[/cyan]\n"
            f"[dim]{p.rationale}[/dim]",
            title="Preflight (Conditional)",
            border_style="magenta",
        )
    )


def _render_stats(console: Console, review: FinalReview) -> None:
    """Vypíše tabulku se statistikami všech agentů napříč iteracemi."""
    table = Table(title="Statistika běhu", show_header=True, header_style="bold")
    table.add_column("Fáze", style="cyan")
    table.add_column("Délka", justify="right")
    table.add_column("Cena USD", justify="right")

    if review.preflight:
        table.add_row(
            "Preflight",
            f"{review.preflight.duration_s:.1f} s",
            f"${review.preflight.cost_usd:.4f}",
        )

    for it in review.iterations:
        marker = f"Iterace #{it.iteration}"
        if it.is_refinement:
            marker += " (refinement)"
        if it.score is not None:
            marker += f" — skóre {it.score:.1f}/10"
        table.add_section()
        table.add_row(f"[bold]{marker}[/bold]", "", "")
        for r in it.specialist_reports:
            table.add_row(f"  {r.label}", f"{r.duration_s:.1f} s", f"${r.cost_usd:.4f}")
        table.add_row(
            "  Supervisor",
            f"{it.supervisor_duration_s:.1f} s",
            f"${it.supervisor_cost_usd:.4f}",
        )

    table.add_section()
    table.add_row(
        "[bold]Celkem (sečtené iterace)[/bold]",
        f"[bold]{review.total_duration_s:.1f} s[/bold]",
        f"[bold]${review.total_cost_usd:.4f}[/bold]",
    )
    console.print(table)


def _to_json(review: FinalReview, file_label: str) -> str:
    payload = {
        "file": file_label,
        "final_report_markdown": review.final_report,
        "final_score": review.final_score,
        "preflight": (
            {
                "language": review.preflight.language,
                "file_type": review.preflight.file_type,
                "rationale": review.preflight.rationale,
                "specialists": review.preflight.specialists,
                "duration_s": round(review.preflight.duration_s, 3),
                "cost_usd": round(review.preflight.cost_usd, 6),
            }
            if review.preflight
            else None
        ),
        "iterations": [
            {
                "iteration": it.iteration,
                "is_refinement": it.is_refinement,
                "score": it.score,
                "supervisor": {
                    "duration_s": round(it.supervisor_duration_s, 3),
                    "cost_usd": round(it.supervisor_cost_usd, 6),
                    "report": it.supervisor_report,
                },
                "specialists": [
                    {
                        "name": r.name,
                        "label": r.label,
                        "duration_s": round(r.duration_s, 3),
                        "cost_usd": round(r.cost_usd, 6),
                        "report": r.content,
                    }
                    for r in it.specialist_reports
                ],
            }
            for it in review.iterations
        ],
        "totals": {
            "duration_s": round(review.total_duration_s, 3),
            "cost_usd": round(review.total_cost_usd, 6),
            "iteration_count": len(review.iterations),
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
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=DEFAULT_MAX_ITERATIONS,
        metavar="N",
        help=f"Loop pattern: maximální počet iterací (default {DEFAULT_MAX_ITERATIONS}). "
        "1 = žádný loop, jen jedno kolo.",
    )
    parser.add_argument(
        "--score-threshold",
        type=float,
        default=DEFAULT_SCORE_THRESHOLD,
        metavar="X",
        help=f"Loop pattern: pod tímto skóre 1-10 se spouští refinement "
        f"(default {DEFAULT_SCORE_THRESHOLD}).",
    )
    parser.add_argument(
        "--no-preflight",
        action="store_true",
        help="Conditional pattern: vynech preflight a spusť všechny tři "
        "specialisty (rychlejší o 1 LLM call, ale dražší o 1-2 specialisty "
        "u nestandardních souborů).",
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
        review = await review_code(
            code,
            file_label=file_label,
            max_iterations=args.max_iterations,
            score_threshold=args.score_threshold,
            skip_preflight=args.no_preflight,
        )

    if args.json:
        print(_to_json(review, file_label))
    else:
        console.print()
        _render_preflight(console, review)
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
