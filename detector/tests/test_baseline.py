import json
import math

import pytest

from contracts.schemas import TelemetryRecord
from detector.baseline import Baseline, ChannelStats, fit_baseline
from detector.config import DetectorConfig


def _load_mock() -> list[TelemetryRecord]:
    return [TelemetryRecord(**json.loads(l)) for l in open("fixtures/mock_telemetry.jsonl")]


def _make_records(
    n: int,
    *,
    accuracy: float = 1.0,
    valid: bool = True,
    gen_complexity: int = 1,
    req_complexity: int = 1,
) -> list[TelemetryRecord]:
    from contracts.schemas import Difficulty
    return [
        TelemetryRecord(
            run_id=f"r{i}",
            timestamp=float(i),
            difficulty=Difficulty.EASY,
            execution_accuracy=accuracy,
            query_valid=valid,
            generated_complexity=gen_complexity,
            required_complexity=req_complexity,
        )
        for i in range(n)
    ]


class TestFitBaselineOnMock:
    def setup_method(self):
        self.recs = _load_mock()
        self.cfg = DetectorConfig()
        self.b = fit_baseline(self.recs, self.cfg)

    def test_accuracy_mean_in_range(self):
        assert 0.94 <= self.b.execution_accuracy.mean <= 0.96

    def test_validity_mean_is_one(self):
        assert self.b.query_valid.mean == 1.0

    def test_gap_mean_in_range(self):
        assert -0.50 <= self.b.complexity_gap.mean <= -0.40

    def test_validity_std_is_floored_not_zero(self):
        # query_valid is 1.0 for all 40 baseline records → raw std 0.0
        # must come back as std_floor, not 0.0
        assert self.b.query_valid.std == self.cfg.std_floor
        assert self.b.query_valid.std > 0.0

    def test_n_equals_baseline_len(self):
        assert self.b.execution_accuracy.n == self.cfg.baseline_len
        assert self.b.query_valid.n == self.cfg.baseline_len
        assert self.b.complexity_gap.n == self.cfg.baseline_len

    def test_baseline_is_frozen(self):
        with pytest.raises((AttributeError, TypeError)):
            self.b.execution_accuracy = ChannelStats(0.0, 0.0, 0)  # type: ignore[misc]

    def test_get_helper_returns_same_as_attribute(self):
        assert self.b.get("execution_accuracy") is self.b.execution_accuracy
        assert self.b.get("query_valid") is self.b.query_valid
        assert self.b.get("complexity_gap") is self.b.complexity_gap

    def test_get_unknown_channel_raises_key_error(self):
        with pytest.raises(KeyError, match="nonexistent"):
            self.b.get("nonexistent")


class TestFitBaselineKnownValues:
    def test_known_accuracy(self):
        cfg = DetectorConfig(baseline_len=4)
        recs = _make_records(4, accuracy=0.75)
        b = fit_baseline(recs, cfg)
        assert b.execution_accuracy.mean == 0.75
        assert b.execution_accuracy.std == cfg.std_floor  # all identical → 0 → floored

    def test_mixed_accuracy_mean_and_std(self):
        import statistics as s
        cfg = DetectorConfig(baseline_len=4)
        vals = [1.0, 0.0, 1.0, 0.0]
        recs = _make_records(4)
        for i, r in enumerate(recs):
            object.__setattr__(r, "execution_accuracy", vals[i])
        b = fit_baseline(recs, cfg)
        assert math.isclose(b.execution_accuracy.mean, s.mean(vals))
        assert math.isclose(b.execution_accuracy.std, s.stdev(vals))

    def test_complexity_gap_computed_correctly(self):
        # generated=1, required=3 → gap = +2 for each record
        cfg = DetectorConfig(baseline_len=3)
        recs = _make_records(3, gen_complexity=1, req_complexity=3)
        b = fit_baseline(recs, cfg)
        assert b.complexity_gap.mean == 2.0

    def test_std_not_floored_when_variance_exists(self):
        cfg = DetectorConfig(baseline_len=2)
        recs = _make_records(2)
        object.__setattr__(recs[0], "execution_accuracy", 0.0)
        object.__setattr__(recs[1], "execution_accuracy", 1.0)
        b = fit_baseline(recs, cfg)
        assert b.execution_accuracy.std > cfg.std_floor


class TestFitBaselineErrorPaths:
    def test_empty_list_raises(self):
        with pytest.raises(ValueError, match="at least 40"):
            fit_baseline([], DetectorConfig())

    def test_fewer_than_baseline_len_raises(self):
        recs = _make_records(5)
        with pytest.raises(ValueError, match="at least 40"):
            fit_baseline(recs, DetectorConfig())

    def test_exactly_baseline_len_succeeds(self):
        cfg = DetectorConfig(baseline_len=5)
        recs = _make_records(5)
        b = fit_baseline(recs, cfg)
        assert b.execution_accuracy.n == 5

    def test_more_than_baseline_len_uses_only_first_n(self):
        cfg = DetectorConfig(baseline_len=5)
        recs = _make_records(10, accuracy=1.0)
        # override records 5-9 with accuracy 0.0 — should not affect baseline
        for r in recs[5:]:
            object.__setattr__(r, "execution_accuracy", 0.0)
        b = fit_baseline(recs, cfg)
        assert b.execution_accuracy.mean == 1.0
        assert b.execution_accuracy.n == 5
