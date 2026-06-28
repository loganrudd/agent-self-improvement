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
MEDIUM_Q = ["How many concerts has each stadium hosted? Show the stadium name and the count.",
            "List each country and the number of singers from it, highest first.",
            "What is the average GPA of students for each advisor?"]
HARD_Q = ["For each country with >3 singers, avg age of singers older than the youngest, ordered desc.",
          "Students enrolled in every course their advisor teaches, with semester GPA above dept median.",
          "Top 3 countries by ratio of concert attendance to stadium capacity across all years."]

# Realistic SQL per question, bound to a schema. Three variants:
#   correct -> teacher/gold form (verdict "correct")
#   wrong   -> the base agent's under-reaching form: parses + runs, wrong result (verdict "valid_but_wrong")
#   broken  -> won't parse/execute (verdict "invalid")
# The viewer derives the verdict from accuracy+validity; we emit the matching SQL so the
# example panel is coherent. This is demo data only — the contract/schema is untouched.
SQL = {
    "How many singers are there?": {
        "db": "concert_singer",
        "correct": "SELECT COUNT(*) FROM singer;",
        "wrong":   "SELECT COUNT(*) FROM singer WHERE Is_male = 'T';",
        "broken":  "SELECT COUNT(*) FROM singers;",
    },
    "List all country names.": {
        "db": "world_1",
        "correct": "SELECT Name FROM country;",
        "wrong":   "SELECT DISTINCT Continent FROM country;",
        "broken":  "SELECT Name FROM contry;",
    },
    "Count students.": {
        "db": "student_network",
        "correct": "SELECT COUNT(*) FROM student;",
        "wrong":   "SELECT COUNT(DISTINCT advisor_id) FROM student;",
        "broken":  "SELECT COUNT(*) FROM student WHERE;",
    },
    MEDIUM_Q[0]: {
        "db": "concert_singer",
        "correct": "SELECT T2.Name, COUNT(*) AS concerts\n"
                   "FROM concert AS T1\n"
                   "JOIN stadium AS T2 ON T1.Stadium_ID = T2.Stadium_ID\n"
                   "GROUP BY T2.Name;",
        "wrong":   "SELECT Name, COUNT(*) AS concerts\n"
                   "FROM stadium\n"
                   "GROUP BY Name;",
        "broken":  "SELECT T2.Name, COUNT(*)\n"
                   "FROM concert AS T1\n"
                   "JOIN stadium AS T2\n"
                   "GROUP BY T2.Name;",
    },
    MEDIUM_Q[1]: {
        "db": "concert_singer",
        "correct": "SELECT Country, COUNT(*) AS singers\n"
                   "FROM singer\n"
                   "GROUP BY Country\n"
                   "ORDER BY singers DESC;",
        "wrong":   "SELECT Country, COUNT(*) AS singers\n"
                   "FROM singer\n"
                   "ORDER BY singers DESC;",
        "broken":  "SELECT Country, COUNT(*) AS singers\n"
                   "FROM singer\n"
                   "GROUP Country;",
    },
    MEDIUM_Q[2]: {
        "db": "student_network",
        "correct": "SELECT a.name, AVG(s.gpa) AS avg_gpa\n"
                   "FROM advisor a\n"
                   "JOIN student s ON s.advisor_id = a.id\n"
                   "GROUP BY a.name;",
        "wrong":   "SELECT advisor_id, AVG(gpa) AS avg_gpa\n"
                   "FROM student\n"
                   "GROUP BY advisor_id;",
        "broken":  "SELECT a.name, AVG(s.gpa)\n"
                   "FROM advisor a, student s\n"
                   "GROUP BY a.name;",
    },
    HARD_Q[0]: {
        "db": "concert_singer",
        "correct": "SELECT Country, AVG(Age) AS avg_age\n"
                   "FROM singer\n"
                   "WHERE Age > (SELECT MIN(Age) FROM singer)\n"
                   "GROUP BY Country\n"
                   "HAVING COUNT(*) > 3\n"
                   "ORDER BY avg_age DESC;",
        "wrong":   "SELECT Country, AVG(Age)\n"
                   "FROM singer\n"
                   "GROUP BY Country\n"
                   "ORDER BY AVG(Age) DESC;",
        "broken":  "SELECT Country, AVG(Age)\n"
                   "FROM singer\n"
                   "WHERE Age > MIN(Age)\n"
                   "GROUP BY Country;",
    },
    HARD_Q[1]: {
        "db": "student_network",
        "correct": "SELECT s.name\n"
                   "FROM student s\n"
                   "JOIN advisor a ON a.id = s.advisor_id\n"
                   "WHERE s.gpa > (SELECT AVG(gpa) FROM student WHERE dept_id = s.dept_id)\n"
                   "  AND NOT EXISTS (\n"
                   "    SELECT 1 FROM teaches t\n"
                   "    WHERE t.instructor_id = a.id\n"
                   "      AND t.course_id NOT IN (\n"
                   "        SELECT e.course_id FROM enrollment e WHERE e.student_id = s.id));",
        "wrong":   "SELECT s.name\n"
                   "FROM student s\n"
                   "JOIN enrollment e ON e.student_id = s.id\n"
                   "WHERE s.gpa > 3.0;",
        "broken":  "SELECT name\n"
                   "FROM student\n"
                   "JOIN enrollment ON student.id = student_id\n"
                   "WHERE gpa > AVG(gpa);",
    },
    HARD_Q[2]: {
        "db": "concert_singer",
        "correct": "SELECT s.Country,\n"
                   "       SUM(c.Attendance) * 1.0 / SUM(s.Capacity) AS ratio\n"
                   "FROM stadium s\n"
                   "JOIN concert c ON c.Stadium_ID = s.Stadium_ID\n"
                   "GROUP BY s.Country\n"
                   "ORDER BY ratio DESC\n"
                   "LIMIT 3;",
        "wrong":   "SELECT Country\n"
                   "FROM stadium\n"
                   "ORDER BY Capacity DESC\n"
                   "LIMIT 3;",
        "broken":  "SELECT Country, SUM(Attendance) / SUM(Capacity)\n"
                   "FROM stadium\n"
                   "JOIN concert\n"
                   "ORDER BY ratio DESC LIMIT 3;",
    },
}

def _bern(p: float) -> float:
    return 1.0 if random.random() < p else 0.0

def _record(i: int, difficulty: Difficulty, acc_p: float, valid_p: float,
            req_cx: int, gen_cx: int) -> TelemetryRecord:
    if difficulty in (Difficulty.HARD, Difficulty.EXTRA):
        pool = HARD_Q
    elif difficulty == Difficulty.MEDIUM:
        pool = MEDIUM_Q
    else:
        pool = EASY_Q
    q = random.choice(pool)  # pools are all length 3 -> RNG sequence (accuracy etc.) unchanged
    v = SQL[q]
    valid = _bern(valid_p) == 1.0
    acc = _bern(acc_p) if valid else 0.0   # invalid SQL is never correct
    # emit the SQL variant that matches the derived verdict, so the example panel is coherent
    sql = v["broken"] if not valid else (v["correct"] if acc == 1.0 else v["wrong"])
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
        generated_sql=sql,
        db_id=v["db"],
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
            FewShotExample(question=q, correct_sql=SQL[q]["correct"], db_id=SQL[q]["db"])
            for q in HARD_Q
        ],
        rationale="Learned 8 hard-query failures via teacher; distilled into 3 few-shot examples "
                  "(GROUP BY/HAVING, relational division, ratio aggregation).",
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
