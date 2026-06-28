"""DriftEvent + failing cases -> CorrectionAction (learned few-shot examples).

The handle() function is the main entry point. It gates on severity, calls the
learner to build execution-verified examples, and returns a CorrectionAction whose
new_few_shot_examples will be appended to events.jsonl and read by the harness on
the next run — closing the feedback spine.

CLI usage (standalone, reads from fixture files):
    python -m correction.correction \\
        --drift fixtures/mock_drift_events.jsonl \\
        --cases correction/tests/fixtures/failing_cases.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from contracts.eventlog import append_event
from contracts.schemas import CorrectionAction, DriftEvent, FewShotExample
from correction.learner import FailingCase, make_examples

# Gate: only correct if the drift is real (severity below this → no-op).
# Kept lower than the detector's drop_threshold (0.20) so we correct any genuine fire.
_MIN_SEVERITY = 0.10


def handle(
    event: DriftEvent,
    failing_cases: Sequence[FailingCase],
    anchor_cases: Sequence[FailingCase] = (),
) -> CorrectionAction:
    """Turn a DriftEvent into a CorrectionAction with learned few-shot examples.

    Args:
        event:         The DriftEvent emitted by the detector.
        failing_cases: Curated cases to learn from (orchestrator builds these from
                       event.failing_run_ids + the held FeedItem gold_sql).
        anchor_cases:  Easy baseline successes to retain easy-bucket skill (anti-forgetting).

    Returns:
        CorrectionAction whose new_few_shot_examples is the complete curated set
        (hard examples + anchors). _active_config replaces, not appends, so this
        must be the full set intended for injection.
    """
    if event.severity < _MIN_SEVERITY:
        return CorrectionAction(
            triggered_by=event.channel,
            new_few_shot_examples=[],
            rationale=(
                f"Severity {event.severity:.3f} below threshold {_MIN_SEVERITY} — "
                "drift too small to correct."
            ),
        )

    examples = make_examples(list(failing_cases), list(anchor_cases))

    n_teacher = sum(1 for e in examples if e.source == "teacher")
    n_gold = sum(1 for e in examples if e.source == "gold")
    n_anchor = sum(1 for e in examples if e.source == "anchor")

    rationale = (
        f"Drift on {event.channel}: window={event.window_mean:.3f}, "
        f"baseline={event.baseline_mean:.3f}, severity={event.severity:.3f}. "
        f"failure_mode={event.failure_mode.value}. "
        f"Injecting {len(examples)} examples "
        f"({n_teacher} teacher-verified, {n_gold} gold-fallback, {n_anchor} anchor)."
    )
    return CorrectionAction(
        triggered_by=event.channel,
        new_few_shot_examples=examples,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# CLI (standalone smoke-test / fixture-based run)
# ---------------------------------------------------------------------------

def _load_drift_event(path: str) -> DriftEvent:
    """Load the last drift event from a JSONL file (mock or real events.jsonl)."""
    event = None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            # Support both bare DriftEvent lines and typed event-log envelopes.
            if "type" in obj and obj["type"] == "drift":
                obj = obj["data"]
            if "channel" in obj and "severity" in obj:
                event = DriftEvent.model_validate(obj)
    if event is None:
        raise ValueError(f"No drift event found in {path}")
    return event


def _load_failing_cases(path: str) -> list[FailingCase]:
    cases = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            cases.append(FailingCase(**obj))
    return cases


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m correction.correction",
        description="Standalone correction: drift event + failing cases -> CorrectionAction.",
    )
    p.add_argument(
        "--drift", default="fixtures/mock_drift_events.jsonl",
        help="Path to JSONL file containing drift events (last one is used).",
    )
    p.add_argument(
        "--cases", default="correction/tests/fixtures/failing_cases.jsonl",
        help="Path to JSONL file of FailingCase records.",
    )
    p.add_argument(
        "--append-log", default=None,
        help="If set, append the resulting CorrectionAction to this events.jsonl.",
    )
    return p


if __name__ == "__main__":
    args = _build_parser().parse_args()

    # Require API key early so errors are loud.
    import os
    if not os.environ.get("MINIMAX_API_KEY"):
        print(
            "error: MINIMAX_API_KEY not set — teacher model calls will fail.\n"
            "  export MINIMAX_API_KEY=sk-... then re-run.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[correction] Loading drift event from {args.drift} ...", flush=True)
    event = _load_drift_event(args.drift)
    print(f"  channel={event.channel}, severity={event.severity:.3f}, "
          f"failure_mode={event.failure_mode.value}")

    print(f"[correction] Loading failing cases from {args.cases} ...", flush=True)
    cases = _load_failing_cases(args.cases)
    print(f"  {len(cases)} cases loaded")

    print("[correction] Calling teacher and building examples ...", flush=True)
    action = handle(event, cases)

    print(f"\n[correction] CorrectionAction:")
    print(f"  triggered_by      : {action.triggered_by}")
    print(f"  n_examples        : {len(action.new_few_shot_examples)}")
    for i, ex in enumerate(action.new_few_shot_examples):
        print(f"  [{i}] [{ex.source:<8}] {ex.question[:55]}")
        print(f"        SQL: {ex.correct_sql[:80]}")
    print(f"  rationale         : {action.rationale}")

    if args.append_log:
        append_event(action, path=args.append_log)
        print(f"\n[correction] Appended CorrectionAction to {args.append_log}")
