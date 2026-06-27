"""Drive the loop: for each feed item, run the agent, eval, emit TelemetryRecord.

Usage:
    python -m harness.runner                  # smoke test: 5 per phase
    python -m harness.runner --full           # full demo stream (80 per phase)
    python -m harness.runner --n 10           # n per phase
"""
from __future__ import annotations

import argparse
import uuid
import time

from contracts.eventlog import append_event, read_events
from contracts.schemas import AgentConfig, CorrectionAction, Difficulty, TelemetryRecord
from harness import agent, evaluator
from harness.feed import FeedItem, build_stream, stream
from harness.spider import get_db_path, load_questions, questions_by_difficulty, schema_text


def _active_config(base: AgentConfig) -> AgentConfig:
    """Return base config, optionally updated with latest correction's few-shot examples."""
    corrections = read_events(only="correction")
    if not corrections:
        return base
    latest: CorrectionAction = corrections[-1]
    return base.model_copy(update={"few_shot_examples": latest.new_few_shot_examples})


def run_item(item: FeedItem, config: AgentConfig) -> TelemetryRecord:
    db_path = get_db_path(item.db_id)
    schema = schema_text(db_path)
    sql, tokens, latency_ms = agent.generate_sql(item.question, schema, config)
    acc = evaluator.execution_accuracy(sql, item.gold_sql, db_path)
    valid = evaluator.query_valid(sql, db_path)
    gen_cx = evaluator.complexity(sql)
    req_cx = evaluator.complexity(item.gold_sql)
    return TelemetryRecord(
        run_id=f"{item.question_id}_{uuid.uuid4().hex[:8]}",
        timestamp=time.time(),
        difficulty=Difficulty(item.difficulty),
        execution_accuracy=acc,
        query_valid=valid,
        generated_complexity=gen_cx,
        required_complexity=req_cx,
        latency_ms=latency_ms,
        tokens=tokens,
        question=item.question,
        generated_sql=sql,
        db_id=item.db_id,
        config_id=config.config_id,
    )


def run_stream(items: list[FeedItem], base_config: AgentConfig) -> list[TelemetryRecord]:
    records = []
    for item in stream(items):
        # re-read config each item so corrections take effect mid-stream
        config = _active_config(base_config)
        rec = run_item(item, config)
        append_event(rec)
        records.append(rec)
        phase_label = f"[{item.phase:<9}]"
        acc_label = "✓" if rec.execution_accuracy == 1.0 else "✗"
        print(f"  {phase_label} {acc_label}  {item.question[:60]}")
    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="Full demo stream (80 per phase)")
    parser.add_argument("--n", type=int, default=5, help="Questions per phase (default 5)")
    args = parser.parse_args()

    n = 80 if args.full else args.n
    questions = load_questions()
    items = build_stream(questions, n_baseline=n, n_degraded=n, n_recovery=n)

    base_config = AgentConfig(
        config_id="v0-base",
        model="gemini-2.0-flash",
        few_shot_examples=[],
    )
    print(f"Running {len(items)} questions ({n} per phase)...")
    records = run_stream(items, base_config)
    passed = sum(r.execution_accuracy == 1.0 for r in records)
    print(f"\nDone. Passed: {passed}/{len(records)}")
    by_phase: dict[str, list] = {}
    for r, item in zip(records, items):
        by_phase.setdefault(item.phase, []).append(r.execution_accuracy)
    for phase, accs in by_phase.items():
        avg = sum(accs) / len(accs)
        print(f"  {phase}: {avg:.2f} avg accuracy ({len(accs)} runs)")
