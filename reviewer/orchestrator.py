"""Orchestrace multi-agent review.

Vzor: **Supervisor + Parallel**.

Tři specialisté (security / performance / style) běží paralelně přes
``asyncio.gather``. Supervisor pak jejich tři dílčí reporty sloučí do jednoho
strukturovaného reportu.

Architektura:

    user_code ──► [Supervisor.kickoff] ──► fan-out ─► [Sec]   ─┐
                                                  ├─► [Perf]  ├─► gather ─► [Supervisor.consolidate] ─► final report
                                                  └─► [Style] ─┘

Pro každého specialistu se používá rychlý/levný model (Haiku 4.5). Supervisor
volá silnější Sonnet, protože syntéza vyžaduje lepší reasoning.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from .prompts import (
    PERFORMANCE_PROMPT,
    SECURITY_PROMPT,
    STYLE_PROMPT,
    SUPERVISOR_PROMPT,
)

# Modely
SPECIALIST_MODEL = "claude-haiku-4-5-20251001"   # rychlý a levný
SUPERVISOR_MODEL = "claude-sonnet-4-5"           # silnější syntéza


@dataclass
class SpecialistReport:
    """Výstup jednoho specialisty."""

    name: str          # 'security' | 'performance' | 'style'
    label: str         # lidsky čitelný popisek pro výpis
    content: str       # markdown report
    duration_s: float  # jak dlouho běh trval
    cost_usd: float    # API náklad podle ResultMessage


@dataclass
class FinalReview:
    """Výsledek celého reviewu — finální report + dílčí reporty + statistiky."""

    final_report: str
    specialist_reports: list[SpecialistReport]
    supervisor_duration_s: float
    supervisor_cost_usd: float

    @property
    def total_cost_usd(self) -> float:
        return self.supervisor_cost_usd + sum(r.cost_usd for r in self.specialist_reports)

    @property
    def total_duration_s(self) -> float:
        # Specialisté běží paralelně, takže jejich součet není reálný čas;
        # bereme maximum (kritická cesta).
        parallel_time = max((r.duration_s for r in self.specialist_reports), default=0.0)
        return parallel_time + self.supervisor_duration_s


async def _run_single_query(
    *,
    system_prompt: str,
    user_prompt: str,
    model: str,
) -> tuple[str, float]:
    """Spustí jeden ``query()`` a vrátí (text, cena).

    Specialisté ani supervisor nepotřebují žádné nástroje — všechno je čistá
    analýza textu. Proto ``allowed_tools=[]`` a ``max_turns=1``.
    """
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        max_turns=1,
        allowed_tools=[],
        # SDK by jinak hledal Claude Code projektovou konfiguraci v cwd —
        # tady nás zajímá jen čistý dotaz na model.
        setting_sources=[],
    )

    parts: list[str] = []
    cost: float = 0.0
    async for message in query(prompt=user_prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
        elif isinstance(message, ResultMessage):
            cost = float(message.total_cost_usd or 0.0)

    return "\n".join(parts).strip(), cost


async def _run_specialist(
    name: str,
    label: str,
    system_prompt: str,
    code: str,
    file_label: str,
) -> SpecialistReport:
    user_prompt = (
        f"Zde je kód k review (`{file_label}`):\n\n"
        "```\n"
        f"{code}\n"
        "```\n\n"
        "Postupuj podle pokynů v tvém systémovém promptu a vrať pouze "
        "strukturovaný markdown report podle šablony."
    )

    started = time.perf_counter()
    text, cost = await _run_single_query(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=SPECIALIST_MODEL,
    )
    duration = time.perf_counter() - started

    return SpecialistReport(
        name=name,
        label=label,
        content=text,
        duration_s=duration,
        cost_usd=cost,
    )


async def review_code(code: str, file_label: str = "<input>") -> FinalReview:
    """Hlavní vstupní bod orchestrace.

    Args:
        code: Plný text kódu nebo diffu, který má být zreviewován.
        file_label: Lidsky čitelný popisek (cesta nebo "git diff"), který se
            propíše do user promptu i finálního reportu.

    Returns:
        ``FinalReview`` s finálním markdown reportem a metadaty.
    """
    if not code.strip():
        raise ValueError("Kód k review je prázdný — není co reviewovat.")

    # ---- Krok 1: paralelní specialisté ----
    specialists = await asyncio.gather(
        _run_specialist("security", "Bezpečnostní reviewer", SECURITY_PROMPT, code, file_label),
        _run_specialist("performance", "Výkonnostní reviewer", PERFORMANCE_PROMPT, code, file_label),
        _run_specialist("style", "Stylový reviewer", STYLE_PROMPT, code, file_label),
    )

    # ---- Krok 2: supervisor sloučí ----
    combined = "\n\n---\n\n".join(
        f"## Report: {r.label}\n\n{r.content}" for r in specialists
    )
    supervisor_user_prompt = (
        f"Soubor k review: `{file_label}`\n\n"
        "Tady jsou tři dílčí reporty od specialistů. Slož je do jednoho "
        "finálního reportu podle šablony v tvém systémovém promptu:\n\n"
        f"{combined}"
    )

    started = time.perf_counter()
    final_text, supervisor_cost = await _run_single_query(
        system_prompt=SUPERVISOR_PROMPT,
        user_prompt=supervisor_user_prompt,
        model=SUPERVISOR_MODEL,
    )
    supervisor_duration = time.perf_counter() - started

    return FinalReview(
        final_report=final_text,
        specialist_reports=list(specialists),
        supervisor_duration_s=supervisor_duration,
        supervisor_cost_usd=supervisor_cost,
    )
