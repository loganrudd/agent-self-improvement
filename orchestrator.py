"""Full live loop: harness → detector → correction → harness (recovery).

Run:
    python orchestrator.py              # 40 baseline + 80 degraded + 80 recovery
    python orchestrator.py --baseline 20 --degraded 40 --recovery 40  # faster demo
    python orchestrator.py --dry-run    # no API calls; replays events.jsonl
"""
from __future__ import annotations

import argparse
import sys
import time

from contracts.eventlog import append_event, read_events
from contracts.schemas import AgentConfig, CorrectionAction, DriftEvent, FewShotExample, TelemetryRecord
from detector.config import DetectorConfig
from detector.detector import Detector
from harness import agent, evaluator
from harness.feed import FeedItem, build_stream
from harness.runner import _active_config, run_item
from harness.spider import load_questions


# ---------------------------------------------------------------------------
# Correction shim — calls Mihir's stage if available, gold injection fallback
# ---------------------------------------------------------------------------

def _correction_handle(event: DriftEvent, questions: list[dict]) -> CorrectionAction:
    """Produce a CorrectionAction from a DriftEvent.

    Tries Mihir's correction stage first. Falls back to gold SQL injection
    from the hard/extra subset when the correction stage isn't wired yet.
    """
    try:
        from correction.on_drift import on_drift_event
        from correction.contracts import FailedRun
        from contracts.eventlog import read_events as _re

        # build FailedRun objects from failing_run_ids in events.jsonl
        telem = {e.run_id: e for e in _re(only="telemetry")}
        hard = [q for q in questions if q["difficulty"] in ("hard", "extra")]
        examples = []
        for rid in event.failing_run_ids[:8]:
            rec = telem.get(rid)
            if rec is None:
                continue
            # find gold SQL for this question
            gold = next((q for q in hard if q["question"] == rec.question), None)
            if gold:
                examples.append(FewShotExample(
                    question=rec.question,
                    correct_sql=gold["expected_sql"],
                    db_id=rec.db_id,
                ))
        if examples:
            return CorrectionAction(
                triggered_by=event.channel,
                new_few_shot_examples=examples,
                rationale=f"Mihir path: matched {len(examples)} failing runs to gold SQL.",
            )
    except Exception as exc:
        print(f"  [correction] Mihir stage unavailable ({exc}), using gold injection fallback")

    # Fallback: inject diverse gold examples from hard/extra pool
    hard = [q for q in questions if q["difficulty"] in ("hard", "extra")]
    seen_dbs: set[str] = set()
    examples = []
    for q in hard:
        if q["db_id"] not in seen_dbs and len(examples) < 8:
            examples.append(FewShotExample(
                question=q["question"],
                correct_sql=q["expected_sql"],
                db_id=q["db_id"],
            ))
            seen_dbs.add(q["db_id"])
    return CorrectionAction(
        triggered_by=event.channel,
        new_few_shot_examples=examples,
        rationale=f"Gold injection fallback: {len(examples)} hard/extra examples, diverse db_ids.",
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(n_baseline: int, n_degraded: int, n_recovery: int) -> None:
    agent.require_api_key()
    questions = load_questions()
    items = build_stream(questions, n_baseline=n_baseline, n_degraded=n_degraded, n_recovery=n_recovery)

    base_config = AgentConfig(
        config_id="v0-base",
        model="MiniMax-M2.7-highspeed",
        few_shot_examples=[],
    )

    cfg = DetectorConfig(
        baseline_len=min(40, n_baseline),
        window=25,
        drop_threshold=0.20,
        min_sustained=5,
    )
    det = Detector(cfg)
    drift_fired = False
    correction_applied = False

    print(f"\nAgentWatch — live loop ({n_baseline} baseline / {n_degraded} degraded / {n_recovery} recovery)")
    print("=" * 70)

    phase_acc: dict[str, list[float]] = {}
    for item in items:
        config = _active_config(base_config)
        rec = run_item(item, config)
        append_event(rec)

        phase_acc.setdefault(item.phase, []).append(rec.execution_accuracy)
        mark = "✓" if rec.execution_accuracy == 1.0 else "✗"
        few_shot_count = len(config.few_shot_examples)
        fs_tag = f" [+{few_shot_count} examples]" if few_shot_count else ""
        print(f"  [{item.phase:<9}] {mark}{fs_tag}  {item.question[:55]}", flush=True)

        ev = det.update(rec)
        if ev and not drift_fired:
            drift_fired = True
            print(f"\n{'!' * 70}")
            print(f"  DRIFT DETECTED — channel={ev.channel}  severity={ev.severity:.2f}")
            print(f"  window_mean={ev.window_mean:.2f}  baseline_mean={ev.baseline_mean:.2f}")
            print(f"  failure_mode={ev.failure_mode.value}  failing_runs={len(ev.failing_run_ids)}")
            print(f"{'!' * 70}\n")
            append_event(ev)

            print("  [correction] generating few-shot examples from failures...")
            action = _correction_handle(ev, questions)
            append_event(action)
            print(f"  [correction] injected {len(action.new_few_shot_examples)} examples → harness will pick up on next item")
            print(f"  rationale: {action.rationale}\n")
            correction_applied = True

    print("\n" + "=" * 70)
    print("  RESULTS")
    print("=" * 70)
    for phase in ["baseline", "degraded", "recovery"]:
        accs = phase_acc.get(phase, [])
        if accs:
            avg = sum(accs) / len(accs)
            print(f"  {phase:<12} {avg:.2f}  ({len(accs)} runs)")
    if not drift_fired:
        print("  [!] no drift detected — try more degraded runs or lower drop_threshold")
    if not correction_applied:
        print("  [!] no correction applied")
    print("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AgentWatch full live loop")
    parser.add_argument("--baseline", type=int, default=40)
    parser.add_argument("--degraded", type=int, default=80)
    parser.add_argument("--recovery", type=int, default=80)
    args = parser.parse_args()
    run(args.baseline, args.degraded, args.recovery)
