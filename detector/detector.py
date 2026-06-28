"""Windowed drift detection -> DriftEvent.

Consumes a stream of TelemetryRecord one at a time via update().
Phases:
  WARMUP   — buffering the first baseline_len real records; no firing
  NORMAL   — rolling window live; fires when sustained breach detected
  DRIFTING — latched after first fire; never fires again this run
"""
from __future__ import annotations

import time
from enum import Enum, auto

from contracts.schemas import Difficulty, DriftEvent, FailureMode, TelemetryRecord

from detector.baseline import fit_baseline
from detector.config import DetectorConfig
from detector.rolling import RollingStats

# Soft coupling to the harness's API-error sentinel. Centralised here so there's
# one place to update if the harness changes the prefix. (Open Q from Plan 002.)
OUTAGE_SQL_PREFIX = "-- error:"


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

        if self._state is _State.WARMUP:
            return self._handle_warmup(record)

        if self._state is _State.NORMAL:
            return self._handle_normal(record)

        # DRIFTING: latched — never fires again
        return None

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
            return DriftEvent(
                detected_at=record.timestamp,
                channel="execution_accuracy",
                severity=max(0.0, baseline_mean - window_mean),
                window_mean=window_mean,
                baseline_mean=baseline_mean,
                failure_mode=FailureMode.NONE,
                failing_run_ids=[],
            )

        return None
