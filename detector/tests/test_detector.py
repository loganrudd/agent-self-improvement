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
from detector.detector import (
    Detector,
    OUTAGE_SQL_PREFIX,
    _classify_failure,
    _is_outage_record,
)


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

    def test_phase4_fields_populated(self):
        """Phase 4: fire event carries real failure_mode + failing_run_ids."""
        recs = _load_mock()
        events, _ = _run_stream(recs)
        ev = events[0]
        assert ev.failure_mode == FailureMode.VALID_BUT_WRONG
        assert len(ev.failing_run_ids) > 0
        assert len(ev.failing_run_ids) <= DetectorConfig().failing_ids_cap
        zeros = {r.run_id for r in recs if r.execution_accuracy == 0.0}
        assert all(rid in zeros for rid in ev.failing_run_ids)


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


# ---------------------------------------------------------------------------
# Phase 3 — Per-difficulty stratification
# ---------------------------------------------------------------------------

class TestStratification:
    """Tests for Detector.stratified_means().

    Tiers match test_detector.py convention:
      Tier A — mock-replay invariants (survive threshold re-tuning)
      Tier B — behavioral unit tests on hand-built streams
      Tier C — mock-pinned numbers (seed=7; update if mock regenerated)
    """

    # ------------------------------------------------------------------
    # Tier A — mock-replay invariants
    # ------------------------------------------------------------------

    def test_fire_time_hard_extra_below_easy_medium(self):
        """At fire, hard/extra must be strictly below easy/medium — degraded signal visible."""
        recs = _load_mock()
        det = Detector(DetectorConfig())
        fire_snap = None
        for r in recs:
            ev = det.update(r)
            if ev is not None and fire_snap is None:
                fire_snap = det.stratified_means()
        assert fire_snap is not None
        assert fire_snap[Difficulty.HARD] < fire_snap[Difficulty.EASY]
        assert fire_snap[Difficulty.EXTRA] < fire_snap[Difficulty.MEDIUM]

    def test_end_hard_extra_climbed_from_fire(self):
        """Replaying to end: hard/extra must be strictly above their fire-time values."""
        recs = _load_mock()
        det = Detector(DetectorConfig())
        fire_snap = None
        for r in recs:
            ev = det.update(r)
            if ev is not None and fire_snap is None:
                fire_snap = det.stratified_means()
        end_snap = det.stratified_means()
        assert end_snap[Difficulty.HARD] > fire_snap[Difficulty.HARD]
        assert end_snap[Difficulty.EXTRA] > fire_snap[Difficulty.EXTRA]

    def test_easy_medium_stable_baseline_to_end(self):
        """easy/medium means at end must be within 0.05 of fire-time (no new easy/medium records)."""
        recs = _load_mock()
        det = Detector(DetectorConfig())
        fire_snap = None
        for r in recs:
            ev = det.update(r)
            if ev is not None and fire_snap is None:
                fire_snap = det.stratified_means()
        end_snap = det.stratified_means()
        assert abs(end_snap[Difficulty.EASY] - fire_snap[Difficulty.EASY]) < 0.05
        assert abs(end_snap[Difficulty.MEDIUM] - fire_snap[Difficulty.MEDIUM]) < 0.05

    def test_baseline_only_hard_extra_omitted(self):
        """Baseline-only replay: hard/extra never appear (no such records in P1)."""
        recs = _load_mock()
        det = Detector(DetectorConfig())
        for r in recs[:80]:
            det.update(r)
        means = det.stratified_means()
        assert Difficulty.HARD not in means
        assert Difficulty.EXTRA not in means
        assert Difficulty.EASY in means
        assert Difficulty.MEDIUM in means

    # ------------------------------------------------------------------
    # Tier B — behavioral unit tests on hand-built streams
    # ------------------------------------------------------------------

    def test_per_bucket_mean_independent(self):
        """Each bucket's mean is computed independently from other buckets."""
        cfg = DetectorConfig(baseline_len=4, window=4, min_sustained=3, drop_threshold=0.2)
        det = Detector(cfg)
        # warmup with EASY=1.0
        for i in range(4):
            det.update(_make_rec(i, acc=1.0, difficulty=Difficulty.EASY))
        # push known values: two HARD, two EXTRA
        det.update(_make_rec(10, acc=0.0, difficulty=Difficulty.HARD))
        det.update(_make_rec(11, acc=1.0, difficulty=Difficulty.HARD))
        det.update(_make_rec(12, acc=0.25, difficulty=Difficulty.EXTRA))
        det.update(_make_rec(13, acc=0.75, difficulty=Difficulty.EXTRA))
        means = det.stratified_means()
        assert means[Difficulty.HARD] == pytest.approx(0.5)
        assert means[Difficulty.EXTRA] == pytest.approx(0.5)
        assert means[Difficulty.EASY] == pytest.approx(1.0)
        assert Difficulty.MEDIUM not in means

    def test_empty_bucket_omitted_not_zero(self):
        """Buckets with no records in the window are absent, not reported as 0.0."""
        cfg = DetectorConfig(baseline_len=3, window=5, min_sustained=3, drop_threshold=0.2)
        det = Detector(cfg)
        for i in range(5):
            det.update(_make_rec(i, acc=1.0, difficulty=Difficulty.EASY))
        means = det.stratified_means()
        assert Difficulty.HARD not in means
        assert Difficulty.EXTRA not in means
        assert Difficulty.MEDIUM not in means
        assert Difficulty.EASY in means

    def test_outage_excluded_from_strat_window(self):
        """Outage records (-- error: prefix) must not enter per-difficulty windows."""
        cfg = DetectorConfig(baseline_len=3, window=5, min_sustained=3, drop_threshold=0.2)
        det = Detector(cfg)
        # warmup with real EASY records
        for i in range(3):
            det.update(_make_rec(i, acc=1.0, difficulty=Difficulty.EASY))
        # scatter outages — they carry difficulty=EASY but must not count
        for i in range(5):
            det.update(_outage_rec(100 + i))
        # add two real EASY=1.0 to confirm window not poisoned
        det.update(_make_rec(200, acc=1.0, difficulty=Difficulty.EASY))
        det.update(_make_rec(201, acc=1.0, difficulty=Difficulty.EASY))
        means = det.stratified_means()
        assert means[Difficulty.EASY] == pytest.approx(1.0)

    def test_per_bucket_maxlen_eviction(self):
        """Each per-difficulty window honours maxlen; old values are evicted."""
        cfg = DetectorConfig(baseline_len=2, window=3, min_sustained=5, drop_threshold=0.2)
        det = Detector(cfg)
        # warmup
        for i in range(2):
            det.update(_make_rec(i, acc=1.0, difficulty=Difficulty.EASY))
        # push window+2 HARD records (5 total) — only last 3 should count
        accs = [1.0, 1.0, 0.0, 0.0, 0.0]
        for i, a in enumerate(accs):
            det.update(_make_rec(10 + i, acc=a, difficulty=Difficulty.HARD))
        means = det.stratified_means()
        # last 3 HARD = [0, 0, 0] → mean 0.0
        assert means[Difficulty.HARD] == pytest.approx(0.0)
        assert det._strat_windows[Difficulty.HARD].n == 3  # maxlen respected

    # ------------------------------------------------------------------
    # Tier C — mock-pinned numbers (seed=7; update if mock regenerated)
    # ------------------------------------------------------------------

    def setup_method(self):
        recs = _load_mock()
        det = Detector(DetectorConfig())
        self._fire_snap = None
        for r in recs:
            ev = det.update(r)
            if ev is not None and self._fire_snap is None:
                self._fire_snap = det.stratified_means()
        self._end_snap = det.stratified_means()

    def test_fire_hard_range(self):
        assert 0.0 <= self._fire_snap[Difficulty.HARD] <= 0.2

    def test_fire_extra_range(self):
        assert 0.1 <= self._fire_snap[Difficulty.EXTRA] <= 0.4

    def test_fire_easy_medium_high(self):
        assert self._fire_snap[Difficulty.EASY] >= 0.9
        assert self._fire_snap[Difficulty.MEDIUM] >= 0.9

    def test_end_hard_climbed(self):
        assert 0.8 <= self._end_snap[Difficulty.HARD] <= 1.0

    def test_end_extra_climbed(self):
        assert 0.6 <= self._end_snap[Difficulty.EXTRA] <= 0.85


# ---------------------------------------------------------------------------
# Phase 4 — Failure-mode diagnosis + failing_run_ids
# ---------------------------------------------------------------------------

class TestFailureMode:
    """Tests for _classify_failure, Detector._diagnose_failures, and the
    Phase 4 DriftEvent fields.

    Tiers:
      Tier A — mock-replay invariants (survive threshold re-tuning)
      Tier B — behavioral unit tests on _classify_failure and _diagnose_failures
      Tier C — mock-pinned numbers (seed=7; update if mock regenerated)
    """

    # ------------------------------------------------------------------
    # Tier A — mock-replay invariants
    # ------------------------------------------------------------------

    def test_fire_event_failure_mode_is_valid_but_wrong(self):
        """Mock's dominant failure kind is VALID_BUT_WRONG (7 > 3 in fire window)."""
        events, _ = _run_stream(_load_mock())
        assert events[0].failure_mode == FailureMode.VALID_BUT_WRONG

    def test_fire_event_failing_run_ids_non_empty(self):
        events, _ = _run_stream(_load_mock())
        assert len(events[0].failing_run_ids) > 0

    def test_fire_event_failing_run_ids_within_cap(self):
        cfg = DetectorConfig()
        events, _ = _run_stream(_load_mock(), cfg)
        assert len(events[0].failing_run_ids) <= cfg.failing_ids_cap

    def test_fire_event_ids_are_real_failures(self):
        """Every id in failing_run_ids must be a genuine acc==0 run."""
        recs = _load_mock()
        events, _ = _run_stream(recs)
        zeros = {r.run_id for r in recs if r.execution_accuracy == 0.0}
        assert all(rid in zeros for rid in events[0].failing_run_ids)

    def test_fire_event_ids_within_fire_window(self):
        """All ids must come from records that were in the detector's window at fire."""
        recs = _load_mock()
        cfg = DetectorConfig()
        det = Detector(cfg)
        fire_window_ids: set[str] = set()
        for r in recs:
            ev = det.update(r)
            if ev is not None:
                fire_window_ids = {rec.run_id for rec in det._record_window}
                assert all(rid in fire_window_ids for rid in ev.failing_run_ids)
                break

    # ------------------------------------------------------------------
    # Tier B — _classify_failure unit tests
    # ------------------------------------------------------------------

    def test_classify_correct_run_is_none(self):
        assert _classify_failure(_make_rec(0, acc=1.0, valid=True)) == FailureMode.NONE

    def test_classify_partial_acc_is_none(self):
        """Strict acc==0 rule: partial accuracy (0 < acc < 1) is not a failure."""
        assert _classify_failure(_make_rec(0, acc=0.5, valid=True)) == FailureMode.NONE

    def test_classify_invalid_sql_zero_acc(self):
        assert _classify_failure(_make_rec(0, acc=0.0, valid=False)) == FailureMode.INVALID_SQL

    def test_classify_valid_but_wrong(self):
        assert _classify_failure(_make_rec(0, acc=0.0, valid=True)) == FailureMode.VALID_BUT_WRONG

    def test_classify_valid_wrong_not_misread_as_invalid(self):
        r = _make_rec(0, acc=0.0, valid=True, sql="SELECT count(*) FROM singers")
        assert _classify_failure(r) == FailureMode.VALID_BUT_WRONG

    # ------------------------------------------------------------------
    # Tier B — _diagnose_failures behavioral tests
    # ------------------------------------------------------------------

    def _det_with_window(self, records: list) -> Detector:
        """Build a Detector whose _record_window contains exactly `records`."""
        cfg = DetectorConfig(
            baseline_len=len(records),
            window=len(records),
            min_sustained=999,  # never fires in these helpers
        )
        det = Detector(cfg)
        for r in records:
            det.update(r)
        return det

    def test_diagnose_all_invalid(self):
        recs = [_make_rec(i, acc=0.0, valid=False) for i in range(5)]
        det = self._det_with_window(recs)
        mode, ids = det._diagnose_failures()
        assert mode == FailureMode.INVALID_SQL
        assert set(ids) == {r.run_id for r in recs}

    def test_diagnose_all_valid_wrong(self):
        recs = [_make_rec(i, acc=0.0, valid=True) for i in range(5)]
        det = self._det_with_window(recs)
        mode, ids = det._diagnose_failures()
        assert mode == FailureMode.VALID_BUT_WRONG
        assert set(ids) == {r.run_id for r in recs}

    def test_diagnose_mixed_majority_invalid(self):
        """3 INVALID_SQL vs 2 VALID_BUT_WRONG → INVALID_SQL wins."""
        recs = (
            [_make_rec(i, acc=0.0, valid=False) for i in range(3)]
            + [_make_rec(10 + i, acc=0.0, valid=True) for i in range(2)]
        )
        det = self._det_with_window(recs)
        mode, ids = det._diagnose_failures()
        assert mode == FailureMode.INVALID_SQL
        dom_ids = [r.run_id for r in recs[:3]]
        assert ids[:3] == dom_ids  # dominant ids come first

    def test_diagnose_tie_breaks_to_valid_but_wrong(self):
        """Tie (2 each) → VALID_BUT_WRONG wins (explicit tie-break)."""
        recs = (
            [_make_rec(i, acc=0.0, valid=False) for i in range(2)]
            + [_make_rec(10 + i, acc=0.0, valid=True) for i in range(2)]
        )
        det = self._det_with_window(recs)
        mode, _ = det._diagnose_failures()
        assert mode == FailureMode.VALID_BUT_WRONG

    def test_diagnose_cap_limits_ids(self):
        """When failures exceed cap, exactly cap ids are returned."""
        recs = [_make_rec(i, acc=0.0, valid=True) for i in range(8)]
        det = Detector(DetectorConfig(
            baseline_len=len(recs),
            window=len(recs),
            min_sustained=999,
            failing_ids_cap=3,
        ))
        for r in recs:
            det.update(r)
        _, ids = det._diagnose_failures()
        assert len(ids) == 3

    def test_diagnose_dominant_ids_prioritized_over_cap(self):
        """Cap-limited result leads with dominant-mode ids before others."""
        cfg_kw = dict(baseline_len=6, window=6, min_sustained=999, failing_ids_cap=4)
        det = Detector(DetectorConfig(**cfg_kw))
        # 4 VALID_BUT_WRONG, 2 INVALID_SQL — cap=4 should give all 4 dominant
        vbw = [_make_rec(i, acc=0.0, valid=True) for i in range(4)]
        inv = [_make_rec(10 + i, acc=0.0, valid=False) for i in range(2)]
        for r in vbw + inv:
            det.update(r)
        mode, ids = det._diagnose_failures()
        assert mode == FailureMode.VALID_BUT_WRONG
        dom_run_ids = {r.run_id for r in vbw}
        assert all(rid in dom_run_ids for rid in ids)  # all 4 slots go to dominant

    def test_diagnose_no_failures_returns_none(self):
        """If the window holds no acc==0 runs, return (NONE, [])."""
        recs = [_make_rec(i, acc=1.0) for i in range(5)]
        det = self._det_with_window(recs)
        mode, ids = det._diagnose_failures()
        assert mode == FailureMode.NONE
        assert ids == []

    def test_diagnose_outages_not_in_ids(self):
        """Outage records are excluded from _record_window upstream; they must
        never appear in failing_run_ids even if they look like failures."""
        cfg = DetectorConfig(baseline_len=3, window=5, min_sustained=999)
        det = Detector(cfg)
        # warmup with 3 real good records
        for i in range(3):
            det.update(_make_rec(i, acc=1.0))
        # scatter outages — should not enter _record_window
        for i in range(5):
            det.update(_outage_rec(100 + i))
        # add 2 real failures
        real_fails = [_make_rec(200 + i, acc=0.0, valid=True) for i in range(2)]
        for r in real_fails:
            det.update(r)
        outage_ids = {f"r{100 + i}" for i in range(5)}
        _, ids = det._diagnose_failures()
        assert not any(rid in outage_ids for rid in ids)

    # ------------------------------------------------------------------
    # Tier C — mock-pinned numbers (seed=7; update if mock regenerated)
    # ------------------------------------------------------------------

    def setup_method(self):
        recs = _load_mock()
        cfg = DetectorConfig()
        det = Detector(cfg)
        self._event = None
        self._fire_window_ids: set[str] = set()
        self._recs = recs
        for r in recs:
            ev = det.update(r)
            if ev is not None and self._event is None:
                self._event = ev
                self._fire_window_ids = {rec.run_id for rec in det._record_window}

    def test_pinned_failure_mode(self):
        assert self._event.failure_mode == FailureMode.VALID_BUT_WRONG

    def test_pinned_id_count(self):
        """Fire window has 10 failures; cap=8 → exactly 8 ids."""
        assert len(self._event.failing_run_ids) == 8

    def test_pinned_majority_split(self):
        """7 VALID_BUT_WRONG vs 3 INVALID_SQL in fire window — verify the split."""
        zeros = [r for r in self._recs if r.run_id in self._fire_window_ids and r.execution_accuracy == 0.0]
        vbw = sum(1 for r in zeros if r.query_valid)
        inv = sum(1 for r in zeros if not r.query_valid)
        assert vbw == 7
        assert inv == 3
