"""Generate mock data so all four stages can build in parallel WITHOUT the real loop.

Produces (in repo root / fixtures):
  fixtures/mock_telemetry.jsonl  -> for the DETECTOR (baseline -> degraded -> recovery stream)
  fixtures/mock_drift_events.jsonl -> for CORRECTION (a couple of drift events + failing cases)
  fixtures/mock_events.jsonl     -> for the VIEWER (full typed event stream: telemetry+drift+correction)

Shape matches rules/02: change-point on a stratified stream. Per-query accuracy is noisy;
the windowed average is smooth. Run once from repo root:  python fixtures/generate_mocks.py
"""
from __future__ import annotations

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # repo root on path

import json
import random
import time
from pathlib import Path

from contracts.schemas import (
    TelemetryRecord, Difficulty, DriftEvent, CorrectionAction,
    FewShotExample, FailureMode,
)
from contracts.eventlog import Event

random.seed(7)
HERE = Path(__file__).resolve().parent
DBS = ["concert_singer", "world_1", "student_network"]
EASY_Q = ["How many singers are there?", "List all country names.", "Count students."]
HARD_Q = ["For each country with >3 singers, avg age of singers older than the youngest, ordered desc.",
          "Students enrolled in every course their advisor teaches, with semester GPA above dept median.",
          "Top 3 countries by ratio of concert attendance to stadium capacity across all years."]

def _bern(p: float) -> float:
    return 1.0 if random.random() < p else 0.0

def _record(i: int, difficulty: Difficulty, acc_p: float, valid_p: float,
            req_cx: int, gen_cx: int) -> TelemetryRecord:
    q = random.choice(HARD_Q if difficulty in (Difficulty.HARD, Difficulty.EXTRA) else EASY_Q)
    valid = _bern(valid_p) == 1.0
    acc = _bern(acc_p) if valid else 0.0   # invalid SQL is never correct
    return TelemetryRecord(
        run_id=f"run_{i:04d}",
        timestamp=time.time() + i,
        difficulty=difficulty,
        execution_accuracy=acc,
        query_valid=valid,
        generated_complexity=gen_cx + random.randint(0, 1),
        required_complexity=req_cx,
        latency_ms=random.uniform(300, 1200),
        tokens=random.randint(80, 400),
        question=q,
        generated_sql="SELECT ...",
        db_id=random.choice(DBS),
        config_id="c0",
    )

def build_stream() -> list[TelemetryRecord]:
    recs: list[TelemetryRecord] = []
    i = 0
    # Phase 1 baseline: easy/medium, high accuracy, valid
    for _ in range(80):
        d = random.choice([Difficulty.EASY, Difficulty.MEDIUM])
        recs.append(_record(i, d, acc_p=0.92, valid_p=0.99, req_cx=1, gen_cx=1)); i += 1
    # Change-point -> Phase 2 degraded: hard/extra, accuracy collapses, some invalid SQL
    for _ in range(80):
        d = random.choice([Difficulty.HARD, Difficulty.EXTRA])
        recs.append(_record(i, d, acc_p=0.38, valid_p=0.80, req_cx=4, gen_cx=1)); i += 1
    # Phase 3 recovery: SAME hard/extra, accuracy climbs (agent learned), validity back up
    for _ in range(80):
        d = random.choice([Difficulty.HARD, Difficulty.EXTRA])
        recs.append(_record(i, d, acc_p=0.82, valid_p=0.97, req_cx=4, gen_cx=4)); i += 1
    return recs

def main() -> None:
    recs = build_stream()

    # 1) telemetry mock (detector)
    with open(HERE / "mock_telemetry.jsonl", "w") as f:
        for r in recs:
            f.write(r.model_dump_json() + "\n")

    # degraded-window run ids (for the drift event's failing_run_ids)
    degraded = [r.run_id for r in recs[80:160] if r.execution_accuracy == 0.0][:8]

    # 2) drift events mock (correction)
    drift = DriftEvent(
        detected_at=time.time() + 165,
        channel="execution_accuracy",
        severity=0.45, window_mean=0.40, baseline_mean=0.90,
        failure_mode=FailureMode.VALID_BUT_WRONG,
        failing_run_ids=degraded,
    )
    with open(HERE / "mock_drift_events.jsonl", "w") as f:
        f.write(drift.model_dump_json() + "\n")

    # 3) full event stream mock (viewer): telemetry up to drift, drift, correction, recovery telemetry
    correction = CorrectionAction(
        triggered_by="execution_accuracy",
        new_few_shot_examples=[
            FewShotExample(question=HARD_Q[0], correct_sql="SELECT ... GROUP BY ... HAVING ...", db_id=DBS[0]),
            FewShotExample(question=HARD_Q[1], correct_sql="SELECT ... JOIN ... JOIN ...", db_id=DBS[2]),
        ],
        rationale="Learned 8 hard-query failures via teacher; injected as few-shot examples.",
    )
    def env(rec) -> str:
        t = {"TelemetryRecord": "telemetry", "DriftEvent": "drift", "CorrectionAction": "correction"}[type(rec).__name__]
        return Event(type=t, ts=getattr(rec, "timestamp", getattr(rec, "detected_at", time.time())),
                     data=rec.model_dump(mode="json")).model_dump_json()
    with open(HERE / "mock_events.jsonl", "w") as f:
        for r in recs[:160]:
            f.write(env(r) + "\n")
        f.write(env(drift) + "\n")
        f.write(env(correction) + "\n")
        for r in recs[160:]:
            f.write(env(r) + "\n")

    # quick sanity summary
    def avg(xs): return round(sum(xs) / len(xs), 3)
    print("Wrote mocks to fixtures/:")
    print("  mock_telemetry.jsonl   ", len(recs), "records")
    print("  mock_drift_events.jsonl 1 drift event,", len(degraded), "failing run ids")
    print("  mock_events.jsonl       ", len(recs) + 2, "events")
    print("windowed accuracy by phase (sanity):")
    print("  baseline:", avg([r.execution_accuracy for r in recs[:80]]),
          " degraded:", avg([r.execution_accuracy for r in recs[80:160]]),
          " recovery:", avg([r.execution_accuracy for r in recs[160:]]))

if __name__ == "__main__":
    main()
