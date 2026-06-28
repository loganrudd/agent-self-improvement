import math
import statistics

import pytest

from detector.rolling import RollingStats


def test_mean_and_std_match_stdlib():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    rs = RollingStats()
    rs.extend(vals)
    assert rs.n == 5
    assert math.isclose(rs.mean, statistics.mean(vals))
    assert math.isclose(rs.std, statistics.stdev(vals))


def test_single_value_std_zero():
    rs = RollingStats()
    rs.push(42.0)
    assert rs.n == 1
    assert rs.mean == 42.0
    assert rs.std == 0.0


def test_empty_mean_zero():
    rs = RollingStats()
    assert rs.n == 0
    assert rs.mean == 0.0
    assert rs.std == 0.0


def test_all_identical_std_zero():
    rs = RollingStats()
    rs.extend([3.14] * 40)
    assert rs.std == 0.0
    assert math.isclose(rs.mean, 3.14)


def test_maxlen_eviction_keeps_last_k():
    rs = RollingStats(maxlen=3)
    rs.extend([1.0, 2.0, 3.0, 4.0])
    # window is [2, 3, 4]
    assert rs.n == 3
    assert math.isclose(rs.mean, statistics.mean([2.0, 3.0, 4.0]))
    assert math.isclose(rs.std, statistics.stdev([2.0, 3.0, 4.0]))


def test_maxlen_eviction_std_stays_correct_over_time():
    # slide the window through [1..6], maxlen=3
    rs = RollingStats(maxlen=3)
    vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    for i, v in enumerate(vals):
        rs.push(v)
        if i >= 2:
            window = vals[max(0, i - 2) : i + 1]
            assert math.isclose(rs.mean, statistics.mean(window)), f"at i={i}"
            if len(window) >= 2:
                assert math.isclose(rs.std, statistics.stdev(window)), f"at i={i}"


def test_two_values_std():
    rs = RollingStats()
    rs.extend([0.0, 1.0])
    assert math.isclose(rs.std, statistics.stdev([0.0, 1.0]))


def test_large_values_no_overflow():
    rs = RollingStats()
    rs.extend([1e8, 1e8 + 1.0, 1e8 + 2.0])
    assert math.isclose(rs.mean, 1e8 + 1.0)
    assert math.isclose(rs.std, 1.0)
