"""Windowed drift detection -> DriftEvent.

Consumes a stream of TelemetryRecord one at a time via update().
Phases:
  WARMUP   — buffering the first baseline_len real records; no firing
  NORMAL   — rolling window live; fires when sustained breach detected
  DRIFTING — latched after first fire; never fires again this run
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter, deque
from enum import Enum, auto
from pathlib import Path
from typing import Iterator

from contracts.schemas import Difficulty, DriftEvent, FailureMode, TelemetryRecord

from detector.baseline import fit_baseline
from detector.config import DetectorConfig
from detector.rolling import RollingStats

# Soft coupling to the harness's API-error sentinel. Centralised here so there's
# one place to update if the harness changes the prefix. (Open Q from Plan 002.)
OUTAGE_SQL_PREFIX = "-- error:"


# ---------------------------------------------------------------------------
# Input loader (Step 1)
# ---------------------------------------------------------------------------

def _looks_enveloped(line: str) -> bool:
    """True when the line is an eventlog typed envelope, not a raw TelemetryRecord."""
    obj = json.loads(line)
    return isinstance(obj, dict) and "type" in obj and "data" in obj


def load_telemetry(
    path: Path | str,
    fmt: str = "auto",
) -> Iterator[TelemetryRecord]:
    """Yield TelemetryRecord from a JSONL file.

    fmt: "raw" (bare TelemetryRecord lines), "events" (typed envelopes,
    telemetry-only), or "auto" (sniff the first non-blank line to decide).
    Malformed lines are skipped with a stderr warning; non-telemetry envelopes
    are skipped silently.
    """
    p = Path(path)
    lines = p.read_text().splitlines()
    non_blank = [l for l in lines if l.strip()]

    resolved = fmt
    if fmt == "auto" and non_blank:
        try:
            resolved = "events" if _looks_enveloped(non_blank[0]) else "raw"
        except (json.JSONDecodeError, KeyError):
            resolved = "raw"

    bad = 0
    for lineno, raw in enumerate(lines, 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            if resolved == "events":
                obj = json.loads(raw)
                if obj.get("type") != "telemetry":
                    continue
                yield TelemetryRecord.model_validate(obj["data"])
            else:
                yield TelemetryRecord.model_validate_json(raw)
        except Exception as exc:
            bad += 1
            print(f"[detector] warning: skipping line {lineno}: {exc}", file=sys.stderr)

    if bad:
        print(f"[detector] warning: skipped {bad} malformed line(s) in {p}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Summary printer (Step 2) — milestone lines only, no per-record trickle
# ---------------------------------------------------------------------------

def print_summary(
    *,
    n_records: int,
    baseline: object | None,
    event: DriftEvent | None,
    fire_index: int | None,
    strat: dict,
) -> None:
    """Print a human-readable summary of one detector run to stdout.

    baseline: the Baseline object (may be None if warmup never completed).
    event: the DriftEvent fired, or None.
    strat: dict from Detector.stratified_means() snapshotted at fire time.
    """
    print(f"\n{'=' * 60}")
    print(f"  Detector run — {n_records} records processed")
    print(f"{'=' * 60}")

    if baseline is None:
        print("  [baseline] not established (too few records)")
    else:
        acc  = baseline.execution_accuracy
        val  = baseline.query_valid
        gap  = baseline.complexity_gap
        print(f"  [baseline]  n={acc.n}  acc={acc.mean:.3f}  "
              f"valid={val.mean:.3f}  gap={gap.mean:.3f}")

    print()

    if event is None:
        print("  [result]  no drift detected")
    else:
        print(f"  [DRIFT DETECTED at record #{fire_index}]")
        print(f"    channel      : {event.channel}")
        print(f"    window_mean  : {event.window_mean:.3f}")
        print(f"    baseline_mean: {event.baseline_mean:.3f}")
        print(f"    severity     : {event.severity:.3f}")
        print(f"    failure_mode : {event.failure_mode.value}")
        print(f"    failing_runs : {len(event.failing_run_ids)} id(s) collected")
        if strat:
            print()
            print("  [stratified accuracy at fire]")
            for diff, mean in sorted(strat.items(), key=lambda x: x[0].value):
                print(f"    {diff.value:<12} {mean:.3f}")

    print(f"{'=' * 60}\n")


class _State(Enum):
    WARMUP = auto()
    NORMAL = auto()
    DRIFTING = auto()


def _is_outage_record(record: TelemetryRecord) -> bool:
    """True when the record is a harness API-error, not a model failure.

    Distinguishable because the harness emits generated_sql="-- error: ..." on
    network/API exceptions, while real invalid SQL is the model's actual broken
    query. Outage records are excluded from windows and warmup buffers entirely
    so transient outages cannot trigger false drift events.
    """
    return not record.query_valid and record.generated_sql.startswith(OUTAGE_SQL_PREFIX)


def _classify_failure(record: TelemetryRecord) -> FailureMode:
    """Classify one run by failure kind.

    Failure = strict execution_accuracy == 0 (binary mock; Decision 6).
    - acc != 0          -> NONE (not a failure)
    - acc == 0, invalid -> INVALID_SQL  (SQL didn't parse/run)
    - acc == 0, valid   -> VALID_BUT_WRONG (ran, returned wrong result set)
    """
    if record.execution_accuracy != 0.0:
        return FailureMode.NONE
    return FailureMode.INVALID_SQL if not record.query_valid else FailureMode.VALID_BUT_WRONG


class Detector:
    """Stateful single-pass drift detector over a TelemetryRecord stream.

    Usage::
        det = Detector(DetectorConfig())
        for record in stream:
            event = det.update(record)
            if event:
                handle_drift(event)
    """

    def __init__(self, cfg: DetectorConfig | None = None) -> None:
        self._cfg = cfg or DetectorConfig()
        self._state = _State.WARMUP
        self._warmup_buf: list[TelemetryRecord] = []
        self._baseline = None  # set after warmup; type: Baseline
        self._acc_window = RollingStats(maxlen=self._cfg.window)
        self._strat_windows: dict[Difficulty, RollingStats] = {
            d: RollingStats(maxlen=self._cfg.window) for d in Difficulty
        }
        self._breach_streak: int = 0
        self._record_window: deque[TelemetryRecord] = deque(maxlen=self._cfg.window)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, record: TelemetryRecord) -> DriftEvent | None:
        """Ingest one record. Returns a DriftEvent the first time drift is
        confirmed; None otherwise."""
        if _is_outage_record(record):
            return None  # outage: don't buffer, don't advance streak, can't fire

        # Always push accuracy into the rolling window (even during warmup so
        # the window is warm by the time the baseline freezes).
        self._acc_window.push(record.execution_accuracy)
        self._strat_windows[record.difficulty].push(record.execution_accuracy)
        self._record_window.append(record)

        if self._state is _State.WARMUP:
            return self._handle_warmup(record)

        if self._state is _State.NORMAL:
            return self._handle_normal(record)

        # DRIFTING: latched — never fires again
        return None

    def _diagnose_failures(self) -> tuple[FailureMode, list[str]]:
        """Majority failure mode + prioritized, capped run ids from the current window.

        Returns (NONE, []) when the window holds no strict failures — defensive,
        possible with partial-accuracy streams; shouldn't happen on the binary mock.
        Dominant-mode ids come first, then other-mode ids, capped at failing_ids_cap.
        Tie-break: VALID_BUT_WRONG wins (the learnable logic case; explicit over Counter
        insertion-order).
        """
        failures = [
            (r.run_id, _classify_failure(r))
            for r in self._record_window
            if r.execution_accuracy == 0.0
        ]
        if not failures:
            return FailureMode.NONE, []

        counts: Counter[FailureMode] = Counter(mode for _, mode in failures)
        dominant = max(
            counts,
            key=lambda m: (counts[m], m == FailureMode.VALID_BUT_WRONG),
        )
        dom_ids = [rid for rid, mode in failures if mode == dominant]
        other_ids = [rid for rid, mode in failures if mode != dominant]
        failing_ids = (dom_ids + other_ids)[: self._cfg.failing_ids_cap]
        return dominant, failing_ids

    def stratified_means(self) -> dict[Difficulty, float]:
        """Current windowed execution-accuracy per difficulty.

        Returns only buckets with at least one record in the current window;
        an empty bucket is omitted rather than reported as a misleading 0.0.
        Call right after update() returns a DriftEvent to snapshot the fire moment.
        """
        return {d: w.mean for d, w in self._strat_windows.items() if w.n > 0}

    # ------------------------------------------------------------------
    # Internal state handlers
    # ------------------------------------------------------------------

    def _handle_warmup(self, record: TelemetryRecord) -> DriftEvent | None:
        self._warmup_buf.append(record)
        if len(self._warmup_buf) >= self._cfg.baseline_len:
            self._baseline = fit_baseline(self._warmup_buf, self._cfg)
            self._warmup_buf = []  # free memory; no longer needed
            self._state = _State.NORMAL
        return None

    def _handle_normal(self, record: TelemetryRecord) -> DriftEvent | None:
        # Guard: only evaluate once the window is full (defensive against
        # baseline_len < window misconfig — see Plan 002 Step 2).
        if self._acc_window.n < self._cfg.window:
            return None

        baseline_mean = self._baseline.execution_accuracy.mean
        window_mean = self._acc_window.mean
        breached = window_mean <= baseline_mean - self._cfg.drop_threshold

        if breached:
            self._breach_streak += 1
        else:
            self._breach_streak = 0

        if self._breach_streak >= self._cfg.min_sustained:
            self._state = _State.DRIFTING
            failure_mode, failing_run_ids = self._diagnose_failures()
            return DriftEvent(
                detected_at=record.timestamp,
                channel="execution_accuracy",
                severity=max(0.0, baseline_mean - window_mean),
                window_mean=window_mean,
                baseline_mean=baseline_mean,
                failure_mode=failure_mode,
                failing_run_ids=failing_run_ids,
            )

        return None


# ---------------------------------------------------------------------------
# run() orchestration (Step 3)
# ---------------------------------------------------------------------------

def run(args: object) -> int:
    """Drive the detector end-to-end from parsed CLI args.  Returns exit code."""
    from contracts.eventlog import append_event

    input_path = Path(args.input)

    # --- edge case: missing or empty input ---
    if not input_path.exists():
        print(f"[detector] error: input file not found: {input_path}", file=sys.stderr)
        return 2
    if input_path.stat().st_size == 0:
        print(f"[detector] error: input file is empty: {input_path}", file=sys.stderr)
        return 2

    cfg = DetectorConfig(
        window=args.window,
        baseline_len=args.baseline,
        drop_threshold=args.drop_threshold,
        failing_ids_cap=args.cap,
    )

    det = Detector(cfg)
    event: DriftEvent | None = None
    fire_index: int | None = None
    strat: dict = {}
    n_records = 0

    for record in load_telemetry(input_path, fmt=args.format):
        n_records += 1
        ev = det.update(record)
        if ev is not None and event is None:
            event = ev
            fire_index = n_records - 1   # 0-based index of the firing record
            strat = det.stratified_means()

    # --- edge case: too few records to establish a baseline ---
    if det._state is _State.WARMUP:
        print(
            f"[detector] error: need ≥ {cfg.baseline_len} records to establish a "
            f"baseline, got {n_records}",
            file=sys.stderr,
        )
        return 2

    print_summary(
        n_records=n_records,
        baseline=det._baseline,
        event=event,
        fire_index=fire_index,
        strat=strat,
    )

    if event is not None:
        append_event(event, path=args.output)
        print(f"[detector] DriftEvent appended to {args.output}")

    return 0


# ---------------------------------------------------------------------------
# argparse + main() (Step 4)
# ---------------------------------------------------------------------------

def build_arg_parser() -> "argparse.ArgumentParser":
    import argparse
    from contracts.eventlog import DEFAULT_LOG

    _d = DetectorConfig()
    p = argparse.ArgumentParser(
        prog="python -m detector.detector",
        description="Windowed drift detector. Reads TelemetryRecord JSONL, "
                    "emits a DriftEvent to events.jsonl on first sustained drop. "
                    "Output is append-only — re-running stacks events.",
    )
    p.add_argument("--input", required=True, help="Path to JSONL input file.")
    p.add_argument(
        "--output", default=str(DEFAULT_LOG),
        help=f"Path to event-log output (default: {DEFAULT_LOG}). Append-only.",
    )
    p.add_argument(
        "--format", default="auto", choices=["auto", "raw", "events"],
        help="Input format: 'raw' (bare TelemetryRecord), 'events' (typed envelope), "
             "or 'auto' (sniff first line). Default: auto.",
    )
    p.add_argument("--window", type=int, default=_d.window,
                   help=f"Rolling window size. Default: {_d.window}.")
    p.add_argument("--baseline", type=int, default=_d.baseline_len,
                   help=f"Warmup / baseline length. Default: {_d.baseline_len}.")
    p.add_argument("--drop-threshold", type=float, default=_d.drop_threshold,
                   help=f"Absolute accuracy drop to signal breach. Default: {_d.drop_threshold}.")
    p.add_argument("--cap", type=int, default=_d.failing_ids_cap,
                   help=f"Max failing run IDs to collect. Default: {_d.failing_ids_cap}.")
    return p


def main(argv=None) -> int:
    import argparse
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
