"""Tests for detector.detector (Phase 2).

Three tiers:
  Tier A — regression guard: replays the real mock, asserts invariants.
           Survives threshold / window re-tuning; documents the core contract
           ("detector distinguishes drift from baseline noise").
  Tier B — behavioral unit tests on tiny hand-built streams.
  Tier C — mock-pinned numbers (seed=7 specific; clearly marked).
"""
from __future__ import annotations

import json

import pytest

from contracts.schemas import Difficulty, DriftEvent, FailureMode, TelemetryRecord
from detector.config import DetectorConfig
from detector.detector import Detector, OUTAGE_SQL_PREFIX, _is_outage_record


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_mock() -> list[TelemetryRecord]:
    return [TelemetryRecord(**json.loads(l)) for l in open("fixtures/mock_telemetry.jsonl")]


def _make_rec(
    i: int,
    acc: float = 1.0,
    valid: bool = True,
    sql: str = "SELECT 1",
    difficulty: Difficulty = Difficulty.EASY,
) -> TelemetryRecord:
    return TelemetryRecord(
        run_id=f"r{i}",
        timestamp=float(i),
        difficulty=difficulty,
        execution_accuracy=acc,
        query_valid=valid,
        generated_sql=sql,
    )


def _outage_rec(i: int) -> TelemetryRecord:
    return _make_rec(i, acc=0.0, valid=False, sql=f"{OUTAGE_SQL_PREFIX} connection timeout")


def _run_stream(
    recs: list[TelemetryRecord],
    cfg: DetectorConfig | None = None,
) -> tuple[list[DriftEvent], list[int]]:
    """Run all records through a fresh Detector; return (events, fire_indices)."""
    det = Detector(cfg or DetectorConfig())
    events, indices = [], []
    for i, r in enumerate(recs):
        ev = det.update(r)
        if ev is not None:
            events.append(ev)
            indices.append(i)
    return events, indices


def _baseline_stream(n: int, acc: float = 1.0) -> list[TelemetryRecord]:
    return [_make_rec(i, acc=acc) for i in range(n)]


def _degraded_stream(n: int, acc: float = 0.0, start: int = 0) -> list[TelemetryRecord]:
    return [_make_rec(start + i, acc=acc) for i in range(n)]


# ---------------------------------------------------------------------------
# Tier A — regression guard (invariant form; survives threshold re-tuning)
# ---------------------------------------------------------------------------

class TestMockReplayInvariants:
    """The core regression guard. If these break, the detector's fundamental
    behaviour has changed — check firing logic before adjusting thresholds."""

    def test_fires_exactly_once(self):
        _, indices = _run_stream(_load_mock())
        assert len(indices) == 1

    def test_fires_in_degraded_window_not_baseline(self):
        """Key invariant: fire index must be in [80, 160), never in baseline."""
        _, indices = _run_stream(_load_mock())
        assert len(indices) == 1
        assert 80 <= indices[0] < 160

    def test_zero_events_in_baseline_phase(self):
        recs = _load_mock()
        _, indices = _run_stream(recs[:80])
        assert len(indices) == 0

    def test_zero_events_in_recovery_phase(self):
        """Feed baseline+recovery only (no degraded). Should stay silent."""
        recs = _load_mock()
        _, indices = _run_stream(recs[:80] + recs[160:])
        assert len(indices) == 0

    def test_event_fields_are_genuine_drop(self):
        cfg = DetectorConfig()
        recs = _load_mock()
        events, _ = _run_stream(recs, cfg)
        ev = events[0]
        assert ev.channel == "execution_accuracy"
        assert ev.window_mean < ev.baseline_mean - cfg.drop_threshold  # genuine breach
        assert ev.severity > 0
        assert ev.severity == pytest.approx(ev.baseline_mean - ev.window_mean)

    def test_phase4_fields_are_defaults(self):
        """Phase 4 not built yet — these must be defaults, not garbage."""
        events, _ = _run_stream(_load_mock())
        ev = events[0]
        assert ev.failure_mode == FailureMode.NONE
        assert ev.failing_run_ids == []


# ---------------------------------------------------------------------------
# Tier B — behavioral unit tests on hand-built streams
# ---------------------------------------------------------------------------

class TestSingleSpike:
    """One bad record in a baseline stream must not fire (anomaly ≠ drift)."""

    def test_single_zero_in_baseline_no_fire(self):
        # window=10 so one zero gives mean=9/10=0.9 > threshold (1.0-0.2=0.8) → no breach.
        # With window=5 one zero would give exactly 0.8 = threshold (breached by <=).
        cfg = DetectorConfig(baseline_len=10, window=10, min_sustained=3, drop_threshold=0.2)
        recs = _baseline_stream(10)          # warmup: baseline mean = 1.0
        recs += _baseline_stream(4, acc=1.0) # more good records
        recs.append(_make_rec(14, acc=0.0))  # one spike: mean=9/10=0.9 → no breach
        recs += _baseline_stream(10, acc=1.0) # recovers
        _, indices = _run_stream(recs, cfg)
        assert len(indices) == 0

    def test_breach_shorter_than_min_sustained_no_fire(self):
        # With window=3 and drop_threshold=0.4 (threshold=0.6):
        #   - 2 bad records: peak breach streak = 2 (< min_sustained=4) → no fire.
        #   - Trace: [1,0,0]=0.33 breach(1); [0,0,1]=0.33 breach(2); [0,1,1]=0.67 reset.
        # A window=5 config with 3 bad records would sustain breach for 5 records
        # (the bad records linger in the window through recovery), exceeding min_sustained.
        cfg = DetectorConfig(baseline_len=3, window=3, min_sustained=4, drop_threshold=0.4)
        recs = _baseline_stream(3)           # warmup: baseline_mean=1.0, threshold=0.6
        recs += _baseline_stream(2, acc=1.0) # fill window to 3
        recs += _degraded_stream(2, acc=0.0, start=5)   # 2 bad: max streak=2 < min_sustained=4
        recs += _baseline_stream(5, acc=1.0, )
        _, indices = _run_stream(recs, cfg)
        assert len(indices) == 0


class TestSustainedDrop:
    """Sustained drop fires exactly once; further degraded records don't re-fire."""

    def test_sustained_drop_fires_once(self):
        cfg = DetectorConfig(baseline_len=5, window=5, min_sustained=3, drop_threshold=0.2)
        recs = _baseline_stream(5)           # warmup: mean = 1.0
        recs += _baseline_stream(5, acc=1.0) # fill window
        recs += _degraded_stream(10, acc=0.0, start=10)  # > min_sustained
        _, indices = _run_stream(recs, cfg)
        assert len(indices) == 1

    def test_continued_degradation_no_second_fire(self):
        cfg = DetectorConfig(baseline_len=5, window=5, min_sustained=3, drop_threshold=0.2)
        recs = _baseline_stream(5)
        recs += _baseline_stream(5, acc=1.0)
        recs += _degraded_stream(30, acc=0.0, start=10)  # long degradation
        _, indices = _run_stream(recs, cfg)
        assert len(indices) == 1  # latched after first fire

    def test_recovery_climb_no_refire(self):
        """After firing, recovery to good accuracy must not re-trigger."""
        cfg = DetectorConfig(baseline_len=5, window=5, min_sustained=3, drop_threshold=0.2)
        recs = _baseline_stream(5)
        recs += _baseline_stream(5, acc=1.0)
        recs += _degraded_stream(10, acc=0.0, start=10)  # fires
        recs += _baseline_stream(20, acc=1.0)             # recovery
        recs += _degraded_stream(10, acc=0.0, start=45)  # second drop (no re-fire)
        _, indices = _run_stream(recs, cfg)
        assert len(indices) == 1


class TestDriftEventFields:
    def test_severity_formula(self):
        cfg = DetectorConfig(baseline_len=5, window=5, min_sustained=2, drop_threshold=0.1)
        recs = _baseline_stream(5)                # all 1.0 → baseline_mean = 1.0
        recs += _baseline_stream(5, acc=1.0)      # fill window
        recs += _degraded_stream(5, acc=0.0, start=10)
        events, _ = _run_stream(recs, cfg)
        assert len(events) == 1
        ev = events[0]
        assert ev.severity == pytest.approx(ev.baseline_mean - ev.window_mean)
        assert ev.severity > 0

    def test_detected_at_matches_firing_record_timestamp(self):
        cfg = DetectorConfig(baseline_len=5, window=5, min_sustained=2, drop_threshold=0.1)
        recs = _baseline_stream(5)
        recs += _baseline_stream(5, acc=1.0)
        degraded = _degraded_stream(5, acc=0.0, start=10)
        recs += degraded
        det = Detector(cfg)
        events = []
        for r in recs:
            ev = det.update(r)
            if ev:
                events.append((ev, r))
        assert len(events) == 1
        ev, firing_rec = events[0]
        assert ev.detected_at == firing_rec.timestamp


class TestWindowFullGuard:
    """Firing must not happen before the window is full (baseline_len < window misconfig)."""

    def test_no_fire_before_window_full(self):
        # baseline_len=3 < window=10: window won't be full at baseline freeze
        cfg = DetectorConfig(baseline_len=3, window=10, min_sustained=1, drop_threshold=0.1)
        recs = _baseline_stream(3)                # warmup (baseline freezes here)
        recs += _degraded_stream(5, acc=0.0, start=3)  # only 5 real records post-baseline
        # window needs 10 records to be full; we have 3+5=8 real → no fire
        _, indices = _run_stream(recs, cfg)
        assert len(indices) == 0

    def test_fires_once_window_is_full(self):
        cfg = DetectorConfig(baseline_len=3, window=5, min_sustained=2, drop_threshold=0.1)
        recs = _baseline_stream(3)                # warmup
        recs += _baseline_stream(5, acc=1.0)      # fill window to 5 (3 warmup + 2 new)
        recs += _degraded_stream(5, acc=0.0, start=8)
        _, indices = _run_stream(recs, cfg)
        assert len(indices) == 1


class TestOutageExclusion:
    """API-error records (-- error: prefix + query_valid=False) are excluded from
    windows and warmup buffers. Outages must never trigger drift."""

    def test_is_outage_record_detects_sentinel(self):
        outage = _outage_rec(0)
        real_invalid = _make_rec(1, acc=0.0, valid=False, sql="SELECT bad syntax (((")
        assert _is_outage_record(outage) is True
        assert _is_outage_record(real_invalid) is False
        assert _is_outage_record(_make_rec(2)) is False

    def test_all_outage_window_cannot_fire(self):
        cfg = DetectorConfig(baseline_len=5, window=5, min_sustained=2, drop_threshold=0.2)
        recs = _baseline_stream(5)               # warmup
        recs += _baseline_stream(5, acc=1.0)     # fill window
        recs += [_outage_rec(100 + i) for i in range(20)]
        _, indices = _run_stream(recs, cfg)
        assert len(indices) == 0

    def test_outage_records_excluded_from_window_mean(self):
        """Scatter errors into a good stream; window mean should still reflect
        only the real records (all 1.0)."""
        cfg = DetectorConfig(baseline_len=5, window=5, min_sustained=3, drop_threshold=0.2)
        recs = _baseline_stream(5)               # warmup
        interleaved = []
        for i in range(10):
            interleaved.append(_make_rec(10 + i * 2, acc=1.0))
            interleaved.append(_outage_rec(11 + i * 2))
        recs += interleaved
        _, indices = _run_stream(recs, cfg)
        assert len(indices) == 0  # real records are all 1.0 → no drop

    def test_outage_records_dont_count_toward_warmup(self):
        """Outages during warmup must not advance the warmup buffer count."""
        cfg = DetectorConfig(baseline_len=4, window=4, min_sustained=2, drop_threshold=0.1)
        det = Detector(cfg)
        for i in range(2):
            det.update(_make_rec(i, acc=1.0))
        from detector.detector import _State
        assert det._state is _State.WARMUP
        for i in range(10):
            det.update(_outage_rec(100 + i))
        assert det._state is _State.WARMUP  # still warmup; only 2 real records seen
        for i in range(2, 4):
            det.update(_make_rec(i, acc=1.0))
        assert det._state is _State.NORMAL  # now 4 real records → baseline frozen

    def test_sustained_outage_straddling_changepoint_no_false_fire(self):
        """Long outage that spans the boundary between baseline and degraded
        phases must not masquerade as drift."""
        cfg = DetectorConfig(baseline_len=10, window=8, min_sustained=3, drop_threshold=0.2)
        recs = _baseline_stream(10)              # warmup: baseline mean = 1.0
        recs += _baseline_stream(8, acc=1.0)     # fill window
        recs += [_outage_rec(100 + i) for i in range(25)]  # long outage
        recs += _degraded_stream(3, acc=0.0, start=200)    # just 3 real bad records
        # window won't accumulate 3 consecutive real-bad records (only 3 total post-outage)
        # — below min_sustained=3 because the window is warming back up
        _, indices = _run_stream(recs, cfg)
        # The 3 degraded records after the outage may or may not fire depending on
        # whether the window is full. With window=8 and only 3 new real records after
        # the long outage (and 8 good ones before it), the window holds 8 good + 3 bad
        # → mean = 3/11 ≈ 0.27... wait, window is maxlen=8. After outage the window
        # holds the 8 pre-outage good records. Then 3 bad records slide in:
        # window = [1,1,1,1,1,0,0,0] → mean = 5/8 = 0.625 > threshold (1.0 - 0.2 = 0.8)
        # → no breach. Correct: outage didn't reset the window, so stale-good guards us.
        assert len(indices) == 0

    def test_real_invalid_sql_still_counts_as_failure(self):
        """genuine invalid SQL (not outage) must still enter the window and can trigger drift."""
        cfg = DetectorConfig(baseline_len=5, window=5, min_sustained=3, drop_threshold=0.2)
        recs = _baseline_stream(5)
        recs += _baseline_stream(5, acc=1.0)
        # real invalid SQL: query_valid=False but NOT the -- error: prefix
        recs += [_make_rec(10 + i, acc=0.0, valid=False, sql="SELECT bad ;;;") for i in range(10)]
        _, indices = _run_stream(recs, cfg)
        assert len(indices) == 1  # genuine failure → drift fires


# ---------------------------------------------------------------------------
# Tier C — mock-pinned (seed=7 specific; adjust if mock is regenerated)
# ---------------------------------------------------------------------------

class TestMockPinnedNumbers:
    """Pinned to fixtures/mock_telemetry.jsonl seed=7. If these fail after a
    mock regeneration or threshold re-tune, update the expected ranges here."""

    def setup_method(self):
        recs = _load_mock()
        self.events, self.indices = _run_stream(recs)

    def test_fire_index_range(self):
        assert 85 <= self.indices[0] <= 95  # first breach=85; fire after 5 sustained

    def test_window_mean_range(self):
        ev = self.events[0]
        assert 0.50 <= ev.window_mean <= 0.70  # ~0.60 at fire (half-baseline window)

    def test_severity_range(self):
        ev = self.events[0]
        assert 0.25 <= ev.severity <= 0.45  # ~0.35

    def test_baseline_mean_range(self):
        ev = self.events[0]
        assert 0.94 <= ev.baseline_mean <= 0.96  # ~0.95
