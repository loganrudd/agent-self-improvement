from __future__ import annotations

from dataclasses import dataclass

from contracts.schemas import TelemetryRecord
from detector.config import DetectorConfig
from detector.rolling import RollingStats


@dataclass(frozen=True)
class ChannelStats:
    mean: float
    std: float   # floored at cfg.std_floor; never raw 0 from a zero-variance baseline
    n: int


@dataclass(frozen=True)
class Baseline:
    execution_accuracy: ChannelStats
    query_valid: ChannelStats
    complexity_gap: ChannelStats

    def get(self, channel: str) -> ChannelStats:
        try:
            return getattr(self, channel)
        except AttributeError:
            raise KeyError(f"Unknown baseline channel: {channel!r}")


def fit_baseline(records: list[TelemetryRecord], cfg: DetectorConfig) -> Baseline:
    """Compute per-channel (mean, floored-std, n) over the first baseline_len records."""
    if len(records) < cfg.baseline_len:
        raise ValueError(
            f"Need at least {cfg.baseline_len} records to fit baseline, "
            f"got {len(records)}"
        )

    window = records[: cfg.baseline_len]

    acc = RollingStats()
    valid = RollingStats()
    gap = RollingStats()

    for r in window:
        acc.push(r.execution_accuracy)
        valid.push(1.0 if r.query_valid else 0.0)
        gap.push(float(r.complexity_gap))

    def _stats(rs: RollingStats) -> ChannelStats:
        return ChannelStats(
            mean=rs.mean,
            std=max(rs.std, cfg.std_floor),
            n=rs.n,
        )

    return Baseline(
        execution_accuracy=_stats(acc),
        query_valid=_stats(valid),
        complexity_gap=_stats(gap),
    )
