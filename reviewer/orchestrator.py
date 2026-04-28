"""Orchestrace multi-agent review.

Vzory implementované v této pipeline:

1. **Conditional** — ``preflight()`` se zeptá triage agenta, kteří specialisté
   mají dostat dotyčný kód.
2. **Parallel** — vybraní specialisté běží paralelně přes ``asyncio.gather``.
3. **Sequential** — specialisté → supervisor je sekvenční fáze.
4. **Supervisor** — supervisor slévá výstupy a hodnotí kvalitu skóre 1–10.
5. **Loop** — pokud supervisor vrátí skóre pod ``score_threshold``, spustí se
   druhé kolo specialistů s ``REFINEMENT_INSTRUCTION`` a finální report
   pochází z posledního kola. Cap: ``max_iterations``.

Architektura:

    user_code
        │
        ▼
   [Preflight]                       ◄── Conditional
        │ rozhodne kteří specialisté
        ▼
    ┌───iteration loop─────────────────────────────────┐ ◄── Loop
    │  fan-out (asyncio.gather)        ◄── Parallel    │
    │  ┌──[Sec]─────┐  ┌──[Perf]─────┐  ┌──[Style]──┐  │
    │  └────────────┘  └─────────────┘  └───────────┘  │
    │            │                                      │
    │            ▼                                      │
    │       [Supervisor]              ◄── Supervisor    │
    │            │                                      │
    │            ▼                                      │
    │     skóre ≥ threshold? ── ano ──► konec           │
    │            │ ne                                   │
    │            └──► další iterace s REFINEMENT        │
    └──────────────────────────────────────────────────┘

Modely:

- Preflighter + specialisté: ``claude-haiku-4-5`` (rychlé, levné, úzce focusované)
- Supervisor: ``claude-sonnet-4-5`` (syntéza vyžaduje silnější reasoning)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from .prompts import (
    PERFORMANCE_PROMPT,
    PREFLIGHTER_PROMPT,
    REFINEMENT_INSTRUCTION,
    SECURITY_PROMPT,
    STYLE_PROMPT,
    SUPERVISOR_PROMPT,
)

log = logging.getLogger(__name__)

# Modely
PREFLIGHT_MODEL = "claude-haiku-4-5-20251001"
SPECIALIST_MODEL = "claude-haiku-4-5-20251001"
SUPERVISOR_MODEL = "claude-sonnet-4-5"

# Loop konfigurace
DEFAULT_MAX_ITERATIONS = 2
DEFAULT_SCORE_THRESHOLD = 5.0  # supervisor vrací X/10; pod 5 = další iterace

# Mapování názvů specialistů na konfiguraci
_SPECIALIST_REGISTRY: dict[str, tuple[str, str]] = {
    # name -> (lidský label, system prompt)
    "security": ("Bezpečnostní reviewer", SECURITY_PROMPT),
    "performance": ("Výkonnostní reviewer", PERFORMANCE_PROMPT),
    "style": ("Stylový reviewer", STYLE_PROMPT),
}


# ============================================================================
# Datové třídy
# ============================================================================


@dataclass
class PreflightDecision:
    """Výstup *Conditional* preflighteru."""

    language: str
    file_type: str
    rationale: str
    specialists: list[str]
    duration_s: float
    cost_usd: float


@dataclass
class SpecialistReport:
    """Výstup jednoho specialisty v jedné iteraci."""

    name: str          # 'security' | 'performance' | 'style'
    label: str         # lidsky čitelný popisek
    content: str       # markdown report
    duration_s: float
    cost_usd: float


@dataclass
class IterationResult:
    """Jedna iterace *Loop* patternu — paralelní specialisté + supervisor."""

    iteration: int               # 1, 2, …
    is_refinement: bool          # True pro 2. a další iteraci
    specialist_reports: list[SpecialistReport]
    supervisor_report: str       # markdown report od supervisora
    score: float | None          # 1–10 (None pokud se nepodařilo parsovat)
    supervisor_duration_s: float
    supervisor_cost_usd: float

    @property
    def parallel_duration_s(self) -> float:
        """Kritická cesta paralelních specialistů (max, ne součet)."""
        return max((r.duration_s for r in self.specialist_reports), default=0.0)

    @property
    def total_duration_s(self) -> float:
        return self.parallel_duration_s + self.supervisor_duration_s

    @property
    def total_cost_usd(self) -> float:
        return self.supervisor_cost_usd + sum(r.cost_usd for r in self.specialist_reports)


@dataclass
class FinalReview:
    """Výsledek celého review pipelinu."""

    preflight: PreflightDecision | None
    iterations: list[IterationResult] = field(default_factory=list)

    @property
    def final_report(self) -> str:
        if not self.iterations:
            return ""
        return self.iterations[-1].supervisor_report

    @property
    def final_score(self) -> float | None:
        if not self.iterations:
            return None
        return self.iterations[-1].score

    @property
    def total_cost_usd(self) -> float:
        cost = sum(i.total_cost_usd for i in self.iterations)
        if self.preflight:
            cost += self.preflight.cost_usd
        return cost

    @property
    def total_duration_s(self) -> float:
        duration = sum(i.total_duration_s for i in self.iterations)
        if self.preflight:
            duration += self.preflight.duration_s
        return duration

    # Zpětně kompatibilní property pro CLI (jen poslední iterace).
    @property
    def specialist_reports(self) -> list[SpecialistReport]:
        return self.iterations[-1].specialist_reports if self.iterations else []

    @property
    def supervisor_duration_s(self) -> float:
        return self.iterations[-1].supervisor_duration_s if self.iterations else 0.0

    @property
    def supervisor_cost_usd(self) -> float:
        return self.iterations[-1].supervisor_cost_usd if self.iterations else 0.0


# ============================================================================
# Pomocné funkce
# ============================================================================


async def _run_single_query(
    *,
    system_prompt: str,
    user_prompt: str,
    model: str,
) -> tuple[str, float]:
    """Spustí jeden ``query()`` a vrátí (text, cena).

    Žádný agent v pipeline nepotřebuje nástroje — všechno je čistá analýza
    textu, výstup je text. Proto ``allowed_tools=[]`` a ``max_turns=1``.
    """
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        max_turns=1,
        allowed_tools=[],
        # Bez tohohle by SDK hledal Claude Code projektovou konfiguraci v cwd.
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


_SCORE_RE = re.compile(r"\*\*Sk[oó]re:?\*\*\s*(\d+(?:[.,]\d+)?)\s*/\s*10", re.IGNORECASE)


def _parse_score(report: str) -> float | None:
    """Vytáhne skóre X/10 z markdown reportu supervisora."""
    match = _SCORE_RE.search(report)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


# ============================================================================
# Conditional pattern — preflight
# ============================================================================


async def preflight(code: str, file_label: str) -> PreflightDecision:
    """Zeptá se triage agenta, kteří specialisté mají běžet."""
    user_prompt = (
        f"Soubor: `{file_label}`\n\n"
        "Prvních ~500 znaků obsahu (pro orientaci):\n\n"
        "```\n"
        f"{code[:500]}\n"
        "```\n\n"
        "Vrať JSON podle pravidel v systémovém promptu."
    )

    started = time.perf_counter()
    raw, cost = await _run_single_query(
        system_prompt=PREFLIGHTER_PROMPT,
        user_prompt=user_prompt,
        model=PREFLIGHT_MODEL,
    )
    duration = time.perf_counter() - started

    # Model občas obalí JSON do code fence — odlepit.
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("Preflighter vrátil nevalidní JSON, padám zpět na všechny specialisty: %r", raw[:200])
        # Bezpečný fallback — když preflight selže, spusť všechno.
        return PreflightDecision(
            language="unknown",
            file_type="unknown",
            rationale="Preflighter selhal, spuštěn fallback na všechny specialisty.",
            specialists=list(_SPECIALIST_REGISTRY.keys()),
            duration_s=duration,
            cost_usd=cost,
        )

    raw_specialists = data.get("specialists") or []
    valid = [s for s in raw_specialists if s in _SPECIALIST_REGISTRY]

    return PreflightDecision(
        language=str(data.get("language", "unknown")),
        file_type=str(data.get("file_type", "unknown")),
        rationale=str(data.get("rationale", "")),
        specialists=valid,
        duration_s=duration,
        cost_usd=cost,
    )


# ============================================================================
# Specialisté — Parallel pattern
# ============================================================================


async def _run_specialist(
    name: str,
    code: str,
    file_label: str,
    *,
    is_refinement: bool = False,
    previous_score: float | None = None,
    previous_findings: str = "",
) -> SpecialistReport:
    label, system_prompt = _SPECIALIST_REGISTRY[name]

    user_prompt = (
        f"Zde je kód k review (`{file_label}`):\n\n"
        "```\n"
        f"{code}\n"
        "```\n\n"
        "Postupuj podle pokynů v tvém systémovém promptu a vrať pouze "
        "strukturovaný markdown report podle šablony."
    )

    if is_refinement:
        user_prompt += REFINEMENT_INSTRUCTION.format(
            previous_score=f"{previous_score:.1f}" if previous_score is not None else "?",
            previous_findings=previous_findings or "(žádné)",
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


async def _run_supervisor(
    specialist_reports: list[SpecialistReport],
    file_label: str,
    iteration: int,
) -> tuple[str, float, float]:
    """Vrátí (markdown report, duration, cost)."""
    combined = "\n\n---\n\n".join(
        f"## Report: {r.label}\n\n{r.content}" for r in specialist_reports
    )

    iteration_note = (
        f"Toto je iterace #{iteration}. " if iteration > 1 else ""
    )
    supervisor_user_prompt = (
        f"{iteration_note}Soubor k review: `{file_label}`\n\n"
        "Tady jsou dílčí reporty od specialistů. Slož je do jednoho finálního "
        "reportu podle šablony v tvém systémovém promptu:\n\n"
        f"{combined}"
    )

    started = time.perf_counter()
    text, cost = await _run_single_query(
        system_prompt=SUPERVISOR_PROMPT,
        user_prompt=supervisor_user_prompt,
        model=SUPERVISOR_MODEL,
    )
    duration = time.perf_counter() - started
    return text, duration, cost


# ============================================================================
# Hlavní vstupní bod
# ============================================================================


async def review_code(
    code: str,
    file_label: str = "<input>",
    *,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    score_threshold: float = DEFAULT_SCORE_THRESHOLD,
    skip_preflight: bool = False,
) -> FinalReview:
    """Spustí kompletní orchestraci nad kódem.

    Args:
        code: Plný text kódu nebo diffu.
        file_label: Lidsky čitelný popisek (cesta nebo "git diff").
        max_iterations: Loop cap. Default 2 — když 1. kolo nezíská skóre
            ≥ ``score_threshold``, spustí se 2. kolo se zaměřením na
            edge cases.
        score_threshold: Pod tímto skóre se spouští refinement loop. Default 5.0.
        skip_preflight: Když True, vynechá Conditional preflight a spustí
            všechny tři specialisty. Užitečné pro testy nebo když víš, že
            soubor je standardní source code.

    Returns:
        ``FinalReview`` se všemi mezivýsledky a finálním reportem.
    """
    if not code.strip():
        raise ValueError("Kód k review je prázdný — není co reviewovat.")

    # ---- Conditional pattern: preflight ----
    if skip_preflight:
        decision = None
        active = list(_SPECIALIST_REGISTRY.keys())
    else:
        decision = await preflight(code, file_label)
        active = decision.specialists or list(_SPECIALIST_REGISTRY.keys())

    if not active:
        raise ValueError(
            "Preflighter rozhodl, že nemá smysl reviewovat (např. binární "
            "soubor). Pokud si myslíš opak, spusť s `skip_preflight=True`."
        )

    review = FinalReview(preflight=decision)

    # ---- Loop pattern: maximálně N iterací ----
    previous_score: float | None = None
    previous_findings_text = ""

    for iteration in range(1, max_iterations + 1):
        is_refinement = iteration > 1

        # ---- Parallel pattern: specialisté ----
        specialists = await asyncio.gather(
            *[
                _run_specialist(
                    name,
                    code,
                    file_label,
                    is_refinement=is_refinement,
                    previous_score=previous_score,
                    previous_findings=previous_findings_text,
                )
                for name in active
            ]
        )

        # ---- Supervisor ----
        supervisor_report, supervisor_duration, supervisor_cost = await _run_supervisor(
            list(specialists), file_label, iteration
        )

        score = _parse_score(supervisor_report)

        review.iterations.append(
            IterationResult(
                iteration=iteration,
                is_refinement=is_refinement,
                specialist_reports=list(specialists),
                supervisor_report=supervisor_report,
                score=score,
                supervisor_duration_s=supervisor_duration,
                supervisor_cost_usd=supervisor_cost,
            )
        )

        # Když máme dostatečné skóre, končíme.
        if score is not None and score >= score_threshold:
            break

        # Příprava kontextu pro další iteraci
        previous_score = score
        previous_findings_text = "\n\n".join(r.content for r in specialists)

    return review
