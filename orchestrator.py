"""Wires the full live loop. BUILT TOGETHER AT THE INTEGRATION CHECKPOINT (hr 5-6),
not by one person in isolation (see .claude/rules/04-workflow.md).

Intended flow once stages exist:
    config = AgentConfig(config_id="c0", model=<weak tier>)        # few_shot_examples empty
    detector = Detector(baseline_from=phase1)
    for rec in harness.run_stream(config):       # harness emits TelemetryRecord
        append_event(rec)
        ev = detector.update(rec)                 # -> DriftEvent or None
        if ev:
            append_event(ev)
            action = correction.handle(ev)        # -> CorrectionAction (learned examples)
            append_event(action)
            config.few_shot_examples += action.new_few_shot_examples   # THE FEEDBACK SPINE
    # viewer tails events.jsonl throughout
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from contracts.eventlog import DEFAULT_LOG
from contracts.schemas import AgentConfig
from harness.feed import FeedItem, build_stream
from harness.runner import run_item
from harness.spider import load_questions

_BASE_MODEL = "MiniMax-M2.7-highspeed"
_SEED = 42  # fixed so dry-run-heldout and the live run measure the exact same pools


# ---------------------------------------------------------------------------
# Feed construction
# ---------------------------------------------------------------------------

def _build_feed(n: int, full: bool) -> list[FeedItem]:
    """Load Spider questions and build the change-point stream.

    Seed is fixed so --dry-run-heldout and the full live run always operate on
    the identical LEARN / HELD-OUT split — the measurement and the demo are
    the same experiment.
    """
    per_phase = 80 if full else n
    questions = load_questions()
    return build_stream(
        questions,
        n_baseline=per_phase,
        n_degraded=per_phase,
        n_recovery=per_phase,
        seed=_SEED,
    )


# ---------------------------------------------------------------------------
# Step 0.5: headroom gate (plan 006)
# ---------------------------------------------------------------------------

def dry_run_heldout(
    items: list[FeedItem],
    config: Optional[AgentConfig] = None,
) -> float:
    """Run held-out (recovery) items at base config, no corrections injected.

    Contamination-free by construction:
    - Fresh AgentConfig with empty few_shot_examples (no _active_config call).
    - run_item is called directly — it does not touch events.jsonl.

    Returns overall accuracy so callers can assert the headroom gate.
    """
    if config is None:
        config = AgentConfig(
            config_id="v0-base-dryrun",
            model=_BASE_MODEL,
            few_shot_examples=[],
        )

    held_out = [it for it in items if it.phase == "recovery"]
    total = len(held_out)
    accs_by_diff: dict[str, list[float]] = {}
    n_scored = 0
    n_skipped = 0

    print(
        f"[dry-run-heldout] {total} held-out items, base config (no corrections). "
        f"All hard/extra on a reasoning model — expect a few seconds each.",
        flush=True,
    )

    for i, item in enumerate(held_out, 1):
        rec = run_item(item, config)
        if rec is None:
            n_skipped += 1
            print(f"  [{i:>3}/{total}] [{item.difficulty:<6}] SKIP (gold SQL failed)  "
                  f"{item.question[:55]}", flush=True)
            continue
        n_scored += 1
        accs_by_diff.setdefault(item.difficulty, []).append(rec.execution_accuracy)
        mark = "✓" if rec.execution_accuracy == 1.0 else "✗"
        print(f"  [{i:>3}/{total}] [{item.difficulty:<6}] {mark}  "
              f"{item.question[:55]}", flush=True)

    all_accs = [a for accs in accs_by_diff.values() for a in accs]
    overall = sum(all_accs) / len(all_accs) if all_accs else 0.0

    print(f"\n  overall : {overall:.3f}  ({n_scored} scored, {n_skipped} skipped — gold SQL failed)")
    for diff in ("easy", "medium", "hard", "extra"):
        if diff in accs_by_diff:
            accs = accs_by_diff[diff]
            print(f"  {diff:<8}: {sum(accs)/len(accs):.3f}  ({len(accs)} runs)")

    if overall >= 0.7:
        print(
            "\n  WARNING: base accuracy on held-out is high (>=0.7). "
            "The recovery 'V' may reflect pool difficulty, not learning. "
            "See plan 006 Step 0.5 — consider re-seeding the split."
        )
    else:
        print(
            f"\n  Headroom confirmed: base struggles on held-out ({overall:.3f}). "
            "Recovery improvement will be attributable to learned examples."
        )

    return overall


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python orchestrator.py",
        description="Agent self-improvement orchestrator (integration checkpoint).",
    )
    p.add_argument(
        "--n", type=int, default=40,
        help="Questions per phase (default 40; must be >=40 for the detector to warm up).",
    )
    p.add_argument(
        "--full", action="store_true",
        help="80 questions per phase — full demo stream.",
    )
    p.add_argument(
        "--dry-run-heldout", action="store_true",
        help=(
            "Run only the held-out pool at base config. No detector, no correction, "
            "no event-log writes. Validates that recovery has headroom (plan 006 Step 0.5)."
        ),
    )
    p.add_argument(
        "--fresh", action="store_true",
        help=(
            "Truncate events.jsonl before a real run so stale correction events "
            "cannot contaminate _active_config."
        ),
    )
    return p


if __name__ == "__main__":
    args = _build_parser().parse_args()

    from harness.agent import require_api_key
    require_api_key()

    items = _build_feed(args.n, args.full)

    if args.dry_run_heldout:
        dry_run_heldout(items)
        sys.exit(0)

    if args.fresh:
        log = Path(DEFAULT_LOG)
        if log.exists():
            log.unlink()
            print(f"[orchestrator] {DEFAULT_LOG} cleared (--fresh).")

    raise SystemExit("TODO(team): full two-pass loop — see plan 006 Step 5")
