"""Windowed drift detection -> DriftEvent.

TODO(Logan):
- rolling window (~20-30 records) per channel.
- fire when windowed mean deviates past threshold (z-score / EWMA) for a SUSTAINED window.
- on fire: classify failure_mode (validity vs accuracy), collect failing_run_ids from the window.
- stratify accuracy by difficulty (improvement signal).
- NO training. Single-query spikes must NOT fire (that's anomaly, not drift).
"""
from contracts.schemas import TelemetryRecord, DriftEvent, FailureMode  # noqa: F401
from contracts.eventlog import append_event, read_events                # noqa: F401

if __name__ == "__main__":
    raise SystemExit("TODO(Logan): implement the windowed detector")
