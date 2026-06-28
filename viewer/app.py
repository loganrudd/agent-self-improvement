"""Thin live viewer: recovery curve + channels + SQL example panel.

NOT the product, NOT Streamlit (see rules/03-compliance.md). Keep minimal.

Consumes events.jsonl ONLY, via contracts.eventlog. Builds against
fixtures/mock_events.jsonl. Server-side windowing: this module precomputes the
per-run windowed series so the front-end stays thin and the math stays testable.

Run from repo root:
    .venv/bin/uvicorn viewer.app:app --reload
    -> http://127.0.0.1:8000          (placeholder page, real UI lands in Phase B)
    -> http://127.0.0.1:8000/api/state (the precomputed series)

Point at a different log with the VIEWER_LOG env var (e.g. events.jsonl at integration).
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from contracts.eventlog import read_events
from contracts.schemas import TelemetryRecord, DriftEvent, CorrectionAction, Difficulty

# --- config -----------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = REPO_ROOT / "fixtures" / "mock_events.jsonl"
STATIC_DIR = Path(__file__).resolve().parent / "static"
WINDOW = 20  # rolling window size (rules/02: ~20-30); the curve is the windowed aggregate

HARD = (Difficulty.HARD, Difficulty.EXTRA)


def log_path() -> Path:
    """Which event log to read. Defaults to the mock; override with VIEWER_LOG."""
    env = os.environ.get("VIEWER_LOG")
    return Path(env) if env else DEFAULT_LOG


# --- windowing --------------------------------------------------------------
def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _verdict(rec: TelemetryRecord) -> str:
    """Derived from execution_accuracy + query_valid (no result-set on the contract)."""
    if not rec.query_valid:
        return "invalid"
    return "correct" if rec.execution_accuracy >= 1.0 else "valid_but_wrong"


def build_state(path: Path | str = None, window: int = WINDOW) -> dict:
    """Read the log and precompute the full per-run windowed series + drift/correction marks.

    The front-end replays this cursor-by-cursor: each `runs[k]` is the snapshot of the
    system *as of* run k (windowed accuracy overall + per stratum, channel values, example).
    """
    path = path or log_path()
    events = read_events(path)  # parsed records, in stream order

    runs: list[dict] = []
    drift: dict | None = None
    correction: dict | None = None

    # accumulators (whole history; we slice the trailing `window` for each snapshot)
    acc_all: list[float] = []
    acc_hard: list[float] = []
    acc_easy: list[float] = []
    valids: list[float] = []
    gaps: list[float] = []
    lats: list[float] = []
    hist_is_hard: list[bool] = []  # difficulty per run, for stratum "is it active in the window"

    seen_runs = 0  # telemetry records seen so far (x-position for drift/correction marks)

    for ev in events:
        if isinstance(ev, TelemetryRecord):
            is_hard = ev.difficulty in HARD
            acc_all.append(ev.execution_accuracy)
            valids.append(1.0 if ev.query_valid else 0.0)
            gaps.append(float(ev.required_complexity - ev.generated_complexity))
            lats.append(ev.latency_ms)
            hist_is_hard.append(is_hard)
            (acc_hard if is_hard else acc_easy).append(ev.execution_accuracy)

            # a stratum's line renders only while that difficulty appears in the recent
            # window — keeps the hard value smooth (windowed over hard runs) but stops the
            # easy/medium line once easy runs slide out (it's baseline-only by design).
            recent = hist_is_hard[-window:]
            hard_active = any(recent)
            easy_active = not all(recent)

            # how many same-DB correction examples are currently active for this run's schema
            # (populated after correction fires; used to show the learning mechanism in the UI)
            same_db_active = 0
            if correction:
                same_db_active = sum(
                    1 for e in correction["examples"] if e.get("db_id") == ev.db_id
                )

            runs.append({
                "run_index": seen_runs,
                "run_id": ev.run_id,
                "difficulty": ev.difficulty.value,
                "is_hard": is_hard,
                # raw (per-query, noisy) — front-end may dot these faintly
                "accuracy_raw": ev.execution_accuracy,
                "valid": ev.query_valid,
                # windowed aggregates (the smooth curve)
                "acc_overall": _mean(acc_all[-window:]),
                "acc_hard": _mean(acc_hard[-window:]) if hard_active else None,
                "acc_easy": _mean(acc_easy[-window:]) if easy_active else None,
                # channel panel values (windowed)
                "validity_rate": _mean(valids[-window:]),
                "complexity_gap": _mean(gaps[-window:]),
                "latency_ms": _mean(lats[-window:]),
                # example panel
                "question": ev.question,
                "generated_sql": ev.generated_sql,
                "db_id": ev.db_id,
                "verdict": _verdict(ev),
                "same_db_examples_active": same_db_active,
            })
            seen_runs += 1

        elif isinstance(ev, DriftEvent):
            drift = {
                "at": seen_runs,  # fired after this many runs
                "channel": ev.channel,
                "severity": ev.severity,
                "window_mean": ev.window_mean,
                "baseline_mean": ev.baseline_mean,
                "failure_mode": ev.failure_mode.value,
                "failing_run_ids": ev.failing_run_ids,
            }

        elif isinstance(ev, CorrectionAction):
            correction = {
                "at": seen_runs,
                "triggered_by": ev.triggered_by,
                "rationale": ev.rationale,
                # matched by question text in the example panel (Phase D)
                "examples": [e.model_dump(mode="json") for e in ev.new_few_shot_examples],
            }

    return {
        "window": window,
        "n_runs": seen_runs,
        "log": str(path),
        "runs": runs,
        "drift": drift,
        "correction": correction,
    }


# --- app --------------------------------------------------------------------
app = FastAPI(title="Agent Self-Improvement — Viewer")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/state")
def api_state() -> dict:
    """The precomputed per-run windowed series + drift/correction marks."""
    return build_state()


@app.get("/")
def index() -> FileResponse:
    """The single narrative page (curve hero; later phases add channels + SQL panel)."""
    return FileResponse(STATIC_DIR / "index.html")
