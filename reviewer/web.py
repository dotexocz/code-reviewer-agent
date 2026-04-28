"""Web UI pro multi-agent code reviewer.

Spuštění:

    python -m reviewer.web                              # http://127.0.0.1:8000
    uvicorn reviewer.web:app --host 0.0.0.0 --port 8000

Endpointy:
    GET  /                  HTML stránka s formulářem
    POST /api/review        JSON API: { code, file_label } -> review JSON
    GET  /healthz           healthcheck (pro testy)
"""

from __future__ import annotations

import logging
from pathlib import Path

import markdown as md_lib
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .orchestrator import review_code

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="Multi-agent Code Reviewer",
    description="Web UI pro multi-agent code reviewer (Supervisor + Parallel).",
    version="0.1.0",
)


class ReviewRequest(BaseModel):
    code: str = Field(..., description="Kód k review (libovolný jazyk)")
    file_label: str = Field("<web>", description="Popisek souboru pro report")
    max_iterations: int = Field(2, ge=1, le=3, description="Loop cap (1-3)")
    score_threshold: float = Field(5.0, ge=0.0, le=10.0, description="Pod tímto skóre se spouští refinement")
    skip_preflight: bool = Field(False, description="Vynech Conditional preflight")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/review")
async def api_review(req: ReviewRequest) -> JSONResponse:
    if not req.code.strip():
        raise HTTPException(status_code=400, detail="Kód k review je prázdný.")

    try:
        result = await review_code(
            req.code,
            file_label=req.file_label,
            max_iterations=req.max_iterations,
            score_threshold=req.score_threshold,
            skip_preflight=req.skip_preflight,
        )
    except Exception as exc:
        log.exception("Review failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    final_html = md_lib.markdown(
        result.final_report,
        extensions=["fenced_code", "tables"],
    )

    return JSONResponse(
        {
            "final_report_markdown": result.final_report,
            "final_report_html": final_html,
            "final_score": result.final_score,
            "preflight": (
                {
                    "language": result.preflight.language,
                    "file_type": result.preflight.file_type,
                    "rationale": result.preflight.rationale,
                    "specialists": result.preflight.specialists,
                    "duration_s": round(result.preflight.duration_s, 2),
                    "cost_usd": round(result.preflight.cost_usd, 4),
                }
                if result.preflight
                else None
            ),
            "iterations": [
                {
                    "iteration": it.iteration,
                    "is_refinement": it.is_refinement,
                    "score": it.score,
                    "specialists": [
                        {
                            "name": r.name,
                            "label": r.label,
                            "duration_s": round(r.duration_s, 2),
                            "cost_usd": round(r.cost_usd, 4),
                        }
                        for r in it.specialist_reports
                    ],
                    "supervisor": {
                        "duration_s": round(it.supervisor_duration_s, 2),
                        "cost_usd": round(it.supervisor_cost_usd, 4),
                    },
                }
                for it in result.iterations
            ],
            "totals": {
                "duration_s": round(result.total_duration_s, 2),
                "cost_usd": round(result.total_cost_usd, 4),
                "iteration_count": len(result.iterations),
            },
        }
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


def main() -> None:
    """CLI entrypoint: ``python -m reviewer.web``."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("Starting web UI on http://127.0.0.1:8000")
    uvicorn.run(
        "reviewer.web:app",
        host="127.0.0.1",
        port=8000,
        log_level="info",
    )


if __name__ == "__main__":
    main()
