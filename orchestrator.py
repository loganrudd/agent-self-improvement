"""Wires the full live loop. BUILT TOGETHER AT THE INTEGRATION CHECKPOINT (hr 5-6).

Flow:
    Pass 1 (baseline + degraded): run agent item-by-item, feed records to the
      detector; on DriftEvent, call correction to build few-shot examples and
      append CorrectionAction to events.jsonl.
    Pass 2 (recovery): run the held-out items; _active_config in the harness
      re-reads the CorrectionAction each item so the agent has the learned examples.
    Comparison: print held-out hard-bucket accuracy with vs without examples,
      side-by-side — the unambiguous self-improvement signal.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Optional

from contracts.eventlog import DEFAULT_LOG, append_event
from contracts.schemas import AgentConfig, DriftEvent, FewShotExample
from correction.correction import handle as correction_handle
from correction.learner import FailingCase
from detector.config import DetectorConfig
from detector.detector import Detector
from harness.feed import FeedItem, build_stream
from harness.runner import run_item, run_stream
from harness.spider import load_questions

_BASE_MODEL = "MiniMax-M2.7-highspeed"
_SEED = 42  # fixed so dry-run-heldout and the live run measure the exact same pools

# Number of easy baseline successes to inject as anti-forgetting anchors.
_N_ANCHORS = 2


# ---------------------------------------------------------------------------
# Feed construction
# ---------------------------------------------------------------------------

def _build_feed(n: int, full: bool) -> list[FeedItem]:
    """Load Spider questions and build the change-point stream.

    Seed is fixed so --dry-run-heldout and the full live run always operate on
    the identical LEARN / HELD-OUT split — the measurement and the demo are
    the same experiment.

    same_db_split=True: every HELD-OUT question has at least one same-DB question in
    LEARN, so injected examples are schema-relevant rather than cross-schema noise.

    baseline_easy_only=True: the weak base fails ~50% of "medium" questions on these
    complex schemas, so an easy+medium baseline is noisy enough to false-trigger drift
    before the change-point. Easy-only gives a stable-high baseline (~0.77).
    """
    per_phase = 80 if full else n
    questions = load_questions()
    return build_stream(
        questions,
        n_baseline=per_phase,
        n_degraded=per_phase,
        n_recovery=per_phase,
        seed=_SEED,
        same_db_split=True,
        baseline_easy_only=True,
    )


# ---------------------------------------------------------------------------
# Accuracy helpers
# ---------------------------------------------------------------------------

def _unique_acc(
    question_acc_pairs: list[tuple[str, float]],
) -> tuple[float, int]:
    """Mean accuracy over unique questions.

    When the same question appears multiple times (stream samples with replacement),
    take the first occurrence — at temperature=0.0 the model is deterministic so all
    occurrences have the same accuracy. Returns (mean, n_unique).
    """
    by_q: dict[str, float] = {}
    for q, acc in question_acc_pairs:
        if q not in by_q:
            by_q[q] = acc
    if not by_q:
        return 0.0, 0
    return sum(by_q.values()) / len(by_q), len(by_q)


# ---------------------------------------------------------------------------
# Step 0.5: headroom gate (plan 006)
# ---------------------------------------------------------------------------

def dry_run_heldout(
    items: list[FeedItem],
    config: Optional[AgentConfig] = None,
) -> dict[str, float]:
    """Run held-out (recovery) items at base config, no corrections injected.

    Contamination-free by construction:
    - Fresh AgentConfig with empty few_shot_examples (no _active_config call).
    - run_item is called directly — it does not touch events.jsonl.

    Returns a dict with "overall" and per-difficulty unique-question accuracy.
    Unique-question accuracy deduplicates repeated samples (stream uses rng.choices with
    replacement, so the same question can appear multiple times; at temperature=0.0 the
    model is deterministic, so the first occurrence is canonical).
    """
    if config is None:
        config = AgentConfig(
            config_id="v0-base-dryrun",
            model=_BASE_MODEL,
            few_shot_examples=[],
        )

    held_out = [it for it in items if it.phase == "recovery"]
    total = len(held_out)
    # (question, accuracy) pairs per difficulty, for unique-question dedup
    pairs_by_diff: dict[str, list[tuple[str, float]]] = {}
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
        pairs_by_diff.setdefault(item.difficulty, []).append(
            (item.question, rec.execution_accuracy)
        )
        mark = "✓" if rec.execution_accuracy == 1.0 else "✗"
        print(f"  [{i:>3}/{total}] [{item.difficulty:<6}] {mark}  "
              f"{item.question[:55]}", flush=True)

    all_pairs = [p for ps in pairs_by_diff.values() for p in ps]
    overall, n_unique = _unique_acc(all_pairs)
    result: dict[str, float] = {"overall": overall}
    for diff in ("easy", "medium", "hard", "extra"):
        if diff in pairs_by_diff:
            acc, n_u = _unique_acc(pairs_by_diff[diff])
            result[diff] = acc

    print(
        f"\n  overall : {overall:.3f}  "
        f"({n_unique} unique q, {n_scored} runs, {n_skipped} skipped — gold SQL failed)"
    )
    for diff in ("easy", "medium", "hard", "extra"):
        if diff in result and diff in pairs_by_diff:
            acc_u, n_u = _unique_acc(pairs_by_diff[diff])
            print(f"  {diff:<8}: {acc_u:.3f}  ({n_u} unique q, {len(pairs_by_diff[diff])} runs)")

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

    return result


# ---------------------------------------------------------------------------
# Step 5: two-pass live loop
# ---------------------------------------------------------------------------

def _make_base_config(run_suffix: str = "v0") -> AgentConfig:
    return AgentConfig(
        config_id=f"v0-base-{run_suffix}",
        model=_BASE_MODEL,
        few_shot_examples=[],
    )


def _build_failing_cases(
    event: DriftEvent,
    run_id_to_record_and_item: dict,
) -> list[FailingCase]:
    """Map event.failing_run_ids back to FailingCase bundles.

    The orchestrator holds the emitted TelemetryRecord AND the FeedItem (which
    carries gold_sql), so this requires no events.jsonl re-reading.
    """
    cases = []
    for run_id in event.failing_run_ids:
        entry = run_id_to_record_and_item.get(run_id)
        if entry is None:
            continue
        rec, item = entry
        cases.append(FailingCase(
            run_id=run_id,
            question=rec.question or item.question,
            db_id=rec.db_id or item.db_id,
            broken_sql=rec.generated_sql,
            gold_sql=item.gold_sql,
            difficulty=item.difficulty,
        ))
    return cases


def _pick_anchors(
    baseline_items: list[FeedItem],
    baseline_records: list,
    n: int = _N_ANCHORS,
) -> list[FailingCase]:
    """Pick n easy baseline successes as anti-forgetting anchors."""
    anchors = []
    for item, rec in zip(baseline_items, baseline_records):
        if len(anchors) >= n:
            break
        if item.difficulty in ("easy", "medium") and rec.execution_accuracy == 1.0:
            anchors.append(FailingCase(
                run_id=rec.run_id,
                question=rec.question or item.question,
                db_id=rec.db_id or item.db_id,
                broken_sql="",
                gold_sql=item.gold_sql,
                difficulty=item.difficulty,
            ))
    return anchors


def _pass1(
    items: list[FeedItem],
    base_config: AgentConfig,
    detector: Detector,
    run_id_map: dict,
    baseline_items_out: list,
    baseline_recs_out: list,
) -> DriftEvent | None:
    """Run baseline + degraded items, feed detector, return DriftEvent when fired."""
    drift_event: DriftEvent | None = None
    pass1_items = [it for it in items if it.phase in ("baseline", "degraded")]
    total = len(pass1_items)

    print(f"\n[pass 1] {total} items (baseline + degraded) ...", flush=True)

    for i, item in enumerate(pass1_items, 1):
        rec = run_item(item, base_config)
        if rec is None:
            print(f"  [{i:>3}/{total}] [{item.phase:<9}] [{item.difficulty:<6}] SKIP", flush=True)
            continue

        append_event(rec)
        run_id_map[rec.run_id] = (rec, item)

        if item.phase == "baseline":
            baseline_items_out.append(item)
            baseline_recs_out.append(rec)

        ev = detector.update(rec)
        mark = "✓" if rec.execution_accuracy == 1.0 else "✗"
        detector_tag = " 🔔DRIFT" if ev else ""
        print(
            f"  [{i:>3}/{total}] [{item.phase:<9}] [{item.difficulty:<6}] {mark}"
            f"  {item.question[:50]}{detector_tag}",
            flush=True,
        )

        if ev and drift_event is None:
            drift_event = ev
            append_event(ev)
            print(
                f"\n  [detector] Drift detected! channel={ev.channel}, "
                f"severity={ev.severity:.3f}, "
                f"failing_run_ids={ev.failing_run_ids}\n",
                flush=True,
            )

    return drift_event


def _pass2(
    items: list[FeedItem],
    base_config: AgentConfig,
) -> list:
    """Run recovery items. _active_config in the harness reads the CorrectionAction."""
    recovery_items = [it for it in items if it.phase == "recovery"]
    total = len(recovery_items)
    print(f"\n[pass 2] {total} held-out items (recovery, with learned examples) ...", flush=True)
    records = run_stream(recovery_items, base_config)
    return records


def _print_comparison(
    recovery_records: list,
    base_accs: dict[str, float],
    diff: str = "hard",
) -> None:
    """Print side-by-side hard-bucket accuracy with vs without examples (unique-question)."""
    diff_recs = [r for r in recovery_records if r.difficulty.value == diff]
    if not diff_recs:
        print(f"\n[result] No {diff} recovery records to compare.")
        return
    with_acc, n_unique = _unique_acc([(r.question, r.execution_accuracy) for r in diff_recs])
    without_acc = base_accs.get(diff, base_accs.get("overall", 0.0))
    print(f"\n{'='*60}")
    print(f"  Self-improvement result ({diff} bucket, {n_unique} unique held-out questions):")
    print(f"    WITHOUT examples (base)  : {without_acc:.3f}")
    print(f"    WITH examples (recovered): {with_acc:.3f}")
    delta = with_acc - without_acc
    sign = "+" if delta >= 0 else ""
    print(f"    Delta                    : {sign}{delta:.3f}")
    if delta > 0:
        print(f"  ✓ Agent improved on {diff} queries after learning from its own failures.")
    else:
        print(f"  ✗ No improvement detected on {diff} queries.")
    print("=" * 60, flush=True)


def probe_relevance(items: list[FeedItem]) -> None:
    """Cheap with/without probe: runs unique held-out questions twice (no events.jsonl writes).

    Uses gold SQL from same-DB LEARN questions as examples (zero teacher API calls).
    This isolates "does schema-relevant injection help?" from teacher quality.
    Typically ~22 agent calls (2 × 11 unique held-out Qs) vs 240 for a full run.
    """
    base_config = _make_base_config("probe-base")

    # Unique held-out questions (deduplicate by question_id since stream uses choices())
    seen_ids: set[str] = set()
    unique_heldout: list[FeedItem] = []
    for it in items:
        if it.phase == "recovery" and it.question_id not in seen_ids:
            unique_heldout.append(it)
            seen_ids.add(it.question_id)

    # Build same-DB LEARN examples (gold SQL) per db_id — unique questions only
    heldout_ids = {it.question_id for it in unique_heldout}
    learn_by_db: dict[str, list[FeedItem]] = {}
    seen_learn: dict[str, set[str]] = {}
    for it in items:
        if it.phase == "degraded" and it.question_id not in heldout_ids:
            db = it.db_id
            if db not in seen_learn:
                seen_learn[db] = set()
            if it.question_id not in seen_learn[db]:
                learn_by_db.setdefault(db, []).append(it)
                seen_learn[db].add(it.question_id)

    total = len(unique_heldout)
    print(
        f"\n[probe] {total} unique held-out questions × 2 passes "
        f"({total * 2} agent calls, no events.jsonl writes)",
        flush=True,
    )

    without_accs: dict[str, float] = {}
    with_accs: dict[str, float] = {}

    for i, it in enumerate(unique_heldout, 1):
        # WITHOUT — base config, no examples
        rec_wo = run_item(it, base_config)
        if rec_wo is not None:
            without_accs[it.question_id] = rec_wo.execution_accuracy
            mark_wo = "✓" if rec_wo.execution_accuracy == 1.0 else "✗"
        else:
            mark_wo = "SKIP"

        # WITH — same-DB gold examples only (zero teacher calls)
        same_db = learn_by_db.get(it.db_id, [])
        examples = [
            FewShotExample(
                question=l.question, correct_sql=l.gold_sql, db_id=l.db_id, source="gold"
            )
            for l in same_db
        ]
        cfg_with = AgentConfig(
            config_id="probe-with", model=_BASE_MODEL, few_shot_examples=examples
        )
        rec_w = run_item(it, cfg_with)
        if rec_w is not None:
            with_accs[it.question_id] = rec_w.execution_accuracy
            mark_w = "✓" if rec_w.execution_accuracy == 1.0 else "✗"
        else:
            mark_w = "SKIP"

        n_ex = len(examples)
        print(
            f"  [{i:>2}/{total}] [{it.db_id:<32}] [{it.difficulty:<6}] "
            f"NO-EX:{mark_wo}  +{n_ex}ex:{mark_w}  {it.question[:40]}",
            flush=True,
        )

    # Comparison
    common = [q for q in without_accs if q in with_accs]
    if not common:
        print("[probe] No scorable questions.")
        return
    wo_mean = sum(without_accs[q] for q in common) / len(common)
    w_mean = sum(with_accs[q] for q in common) / len(common)
    delta = w_mean - wo_mean
    print(f"\n{'='*60}")
    print(f"  Probe: same-DB gold examples, {len(common)} unique held-out Qs")
    print(f"    WITHOUT examples : {wo_mean:.3f}")
    print(f"    WITH    examples : {w_mean:.3f}")
    print(f"    Delta            : {delta:+.3f}")
    if delta >= 0.05:
        print("  ✓ Schema-relevant examples help — proceed to --full run.")
    elif delta <= -0.02:
        print("  ✗ Examples hurting accuracy — check db_id filter or example quality.")
    else:
        print("  ~ Marginal delta — base may be near ceiling; consider weaker model.")
    print("=" * 60, flush=True)


def run_full_loop(items: list[FeedItem], log_path: Path) -> None:
    """Execute the full two-pass self-improvement loop."""
    base_config = _make_base_config()
    detector = Detector(DetectorConfig())

    # Map run_id -> (TelemetryRecord, FeedItem) so correction can build FailingCase bundles.
    run_id_map: dict = {}
    baseline_items: list = []
    baseline_recs: list = []

    # -------------------------------------------------------------------------
    # Pass 1: baseline + degraded — detect drift, fire correction
    # -------------------------------------------------------------------------
    drift_event = _pass1(
        items, base_config, detector, run_id_map, baseline_items, baseline_recs
    )

    if drift_event is None:
        print(
            "\n[orchestrator] WARNING: no drift detected after pass 1. "
            "Try --full to run more records (detector needs baseline_len=40 + window=25).",
            file=sys.stderr,
        )
        # Still run pass 2 so the log has recovery telemetry; just no correction.
    else:
        # -----------------------------------------------------------------
        # Correction: build examples from failing cases + easy anchors
        # -----------------------------------------------------------------
        print("[correction] Building few-shot examples from failing cases ...", flush=True)
        failing_cases = _build_failing_cases(drift_event, run_id_map)
        anchor_cases = _pick_anchors(baseline_items, baseline_recs)
        print(
            f"  {len(failing_cases)} failing cases, {len(anchor_cases)} anchors",
            flush=True,
        )
        action = correction_handle(drift_event, failing_cases, anchor_cases)
        append_event(action)
        print(
            f"  CorrectionAction: {len(action.new_few_shot_examples)} examples — "
            f"{sum(1 for e in action.new_few_shot_examples if e.source == 'teacher')} teacher, "
            f"{sum(1 for e in action.new_few_shot_examples if e.source == 'gold')} gold, "
            f"{sum(1 for e in action.new_few_shot_examples if e.source == 'anchor')} anchor",
            flush=True,
        )
        print(f"  rationale: {action.rationale}", flush=True)

    # -------------------------------------------------------------------------
    # Measure base accuracy on held-out WITHOUT examples (contamination-free)
    # -------------------------------------------------------------------------
    print(
        "\n[orchestrator] Measuring base (no-correction) accuracy on held-out pool ...",
        flush=True,
    )
    base_accs = dry_run_heldout(items)

    # -------------------------------------------------------------------------
    # Pass 2: recovery — agent reads CorrectionAction via _active_config
    # -------------------------------------------------------------------------
    recovery_records = _pass2(items, base_config)

    # -------------------------------------------------------------------------
    # Print the improvement claim (hard bucket is the benchmark — see plan 006)
    # -------------------------------------------------------------------------
    _print_comparison(recovery_records, base_accs=base_accs, diff="hard")

    print(f"\n[orchestrator] events.jsonl written to {log_path}", flush=True)


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
        "--dry-run-degraded", action="store_true",
        help=(
            "Run only the degraded (LEARN) pool at base config; print accuracy. "
            "Confirms the detector will fire before committing to a full --full run."
        ),
    )
    p.add_argument(
        "--probe", action="store_true",
        help=(
            "Cheap with/without test: run each unique held-out question twice — "
            "once WITHOUT examples, once WITH same-DB gold examples. "
            "Zero teacher API calls. ~22 agent calls instead of 240. "
            "Use this to validate that schema-relevant examples help before --full."
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

    if args.probe:
        probe_relevance(items)
        sys.exit(0)

    if args.dry_run_degraded:
        base_cfg = _make_base_config("dry-degraded")
        degraded = [it for it in items if it.phase == "degraded"]
        total = len(degraded)
        accs: list[float] = []
        print(f"[dry-run-degraded] {total} degraded items, base config ...", flush=True)
        for i, item in enumerate(degraded, 1):
            rec = run_item(item, base_cfg)
            if rec is None:
                print(f"  [{i:>3}/{total}] SKIP (gold failed)", flush=True)
                continue
            accs.append(rec.execution_accuracy)
            mark = "✓" if rec.execution_accuracy == 1.0 else "✗"
            print(f"  [{i:>3}/{total}] [{item.difficulty:<6}] {mark}  {item.question[:55]}", flush=True)
        if accs:
            avg = sum(accs) / len(accs)
            print(f"\n  degraded accuracy: {avg:.3f} ({len(accs)} runs)")
            if avg <= 0.70:
                print("  ✓ Degraded accuracy low — detector should fire during a full run.")
            else:
                print("  ✗ Degraded accuracy high — drift may not fire. Check feed configuration.")
        sys.exit(0)

    log_path = Path(DEFAULT_LOG)
    if args.fresh:
        if log_path.exists():
            log_path.unlink()
            print(f"[orchestrator] {DEFAULT_LOG} cleared (--fresh).")

    run_full_loop(items, log_path)
