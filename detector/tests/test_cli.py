"""Tests for Phase 5 — CLI, input loader, and end-to-end event-log round-trip.

Three tiers:
  Tier A — loader invariants: both input formats, malformed lines, autodetect.
  Tier B — run() / edge cases: missing/empty/too-short input, no-drift, re-run append,
            --cap flag wired end-to-end.
  Tier C — e2e round-trip: full mock through main(), read_events() returns the
            right event (mock-pinned).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from contracts.eventlog import read_events
from contracts.schemas import DriftEvent, FailureMode, TelemetryRecord
from detector.config import DetectorConfig
from detector.detector import _looks_enveloped, load_telemetry, main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MOCK_RAW = Path("fixtures/mock_telemetry.jsonl")
MOCK_ENV = Path("fixtures/mock_events.jsonl")


def _write_raw(path: Path, records: list[TelemetryRecord]) -> None:
    """Write a list of TelemetryRecord as raw JSONL."""
    with open(path, "w") as f:
        for r in records:
            f.write(r.model_dump_json() + "\n")


def _make_rec(i: int, acc: float = 1.0, valid: bool = True) -> TelemetryRecord:
    from contracts.schemas import Difficulty
    return TelemetryRecord(
        run_id=f"r{i}",
        timestamp=float(i),
        difficulty=Difficulty.EASY,
        execution_accuracy=acc,
        query_valid=valid,
        generated_sql="SELECT 1",
    )


def _run_main(*args: str) -> int:
    return main(list(args))


# ---------------------------------------------------------------------------
# Tier A — loader invariants
# ---------------------------------------------------------------------------

class TestLoader:
    def test_raw_count(self):
        recs = list(load_telemetry(MOCK_RAW))
        assert len(recs) == 240

    def test_enveloped_count(self):
        recs = list(load_telemetry(MOCK_ENV))
        assert len(recs) == 240

    def test_raw_and_enveloped_same_ids(self):
        raw = [r.run_id for r in load_telemetry(MOCK_RAW)]
        env = [r.run_id for r in load_telemetry(MOCK_ENV)]
        assert raw == env

    def test_autodetect_raw(self):
        """auto mode should resolve to 'raw' for the bare-record fixture."""
        first_line = MOCK_RAW.read_text().splitlines()[0]
        assert not _looks_enveloped(first_line)

    def test_autodetect_enveloped(self):
        """auto mode should resolve to 'events' for the envelope fixture."""
        first_line = MOCK_ENV.read_text().splitlines()[0]
        assert _looks_enveloped(first_line)

    def test_explicit_raw_format(self):
        recs = list(load_telemetry(MOCK_RAW, fmt="raw"))
        assert len(recs) == 240

    def test_explicit_events_format(self):
        recs = list(load_telemetry(MOCK_ENV, fmt="events"))
        assert len(recs) == 240

    def test_malformed_line_skipped_with_warning(self, tmp_path, capsys):
        good = _make_rec(0)
        bad_line = "{NOT VALID JSON{{{"
        good2 = _make_rec(1)
        f = tmp_path / "mixed.jsonl"
        f.write_text(good.model_dump_json() + "\n" + bad_line + "\n" + good2.model_dump_json() + "\n")
        recs = list(load_telemetry(f, fmt="raw"))
        assert len(recs) == 2
        assert recs[0].run_id == "r0"
        assert recs[1].run_id == "r1"
        err = capsys.readouterr().err
        assert "warning" in err.lower()

    def test_non_telemetry_envelopes_skipped(self, tmp_path):
        """Drift/correction envelope lines are silently skipped in events mode."""
        from contracts.schemas import DriftEvent, FailureMode
        import time as _time
        # write one telemetry + one drift envelope
        tel = _make_rec(0)
        drift_ev = DriftEvent(
            detected_at=1.0, channel="execution_accuracy",
            severity=0.3, window_mean=0.6, baseline_mean=0.95,
            failure_mode=FailureMode.VALID_BUT_WRONG, failing_run_ids=[],
        )
        from contracts.eventlog import Event
        f = tmp_path / "mixed_envelopes.jsonl"
        with open(f, "w") as fh:
            fh.write(Event(type="telemetry", ts=_time.time(), data=tel.model_dump(mode="json")).model_dump_json() + "\n")
            fh.write(Event(type="drift", ts=_time.time(), data=drift_ev.model_dump(mode="json")).model_dump_json() + "\n")
        recs = list(load_telemetry(f, fmt="events"))
        assert len(recs) == 1
        assert recs[0].run_id == "r0"

    def test_empty_file_yields_nothing(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("")
        recs = list(load_telemetry(f, fmt="raw"))
        assert recs == []


# ---------------------------------------------------------------------------
# Tier B — run() / edge cases
# ---------------------------------------------------------------------------

class TestRunEdgeCases:
    def test_missing_file_exit2(self, tmp_path, capsys):
        rc = _run_main("--input", str(tmp_path / "does_not_exist.jsonl"),
                       "--output", str(tmp_path / "out.jsonl"))
        assert rc == 2
        assert "not found" in capsys.readouterr().err.lower()

    def test_empty_file_exit2(self, tmp_path, capsys):
        f = tmp_path / "empty.jsonl"
        f.write_text("")
        rc = _run_main("--input", str(f), "--output", str(tmp_path / "out.jsonl"))
        assert rc == 2
        assert "empty" in capsys.readouterr().err.lower()

    def test_too_few_records_exit2(self, tmp_path, capsys):
        """Fewer than baseline_len records → WARMUP never completes → exit 2."""
        f = tmp_path / "short.jsonl"
        _write_raw(f, [_make_rec(i) for i in range(5)])  # baseline_len default=40
        rc = _run_main("--input", str(f), "--output", str(tmp_path / "out.jsonl"))
        assert rc == 2
        err = capsys.readouterr().err
        assert "baseline" in err.lower() or "need" in err.lower()

    def test_no_drift_exit0_no_event_appended(self, tmp_path, capsys):
        """All-good stream → exit 0, no event written to output."""
        cfg = DetectorConfig()
        f = tmp_path / "good.jsonl"
        _write_raw(f, [_make_rec(i, acc=1.0) for i in range(cfg.baseline_len + cfg.window + 10)])
        out = tmp_path / "out.jsonl"
        rc = _run_main("--input", str(f), "--output", str(out))
        assert rc == 0
        assert "no drift" in capsys.readouterr().out.lower()
        assert not out.exists() or read_events(out, only="drift") == []

    def test_rerun_appends_second_event(self, tmp_path):
        """Running twice on the same output appends two drift events."""
        out = tmp_path / "ev.jsonl"
        for _ in range(2):
            rc = _run_main(
                "--input", str(MOCK_RAW),
                "--output", str(out),
            )
            assert rc == 0
        evs = read_events(out, only="drift")
        assert len(evs) == 2

    def test_cap_flag_limits_failing_run_ids(self, tmp_path):
        """--cap N flows through to the persisted DriftEvent."""
        out = tmp_path / "ev.jsonl"
        rc = _run_main("--input", str(MOCK_RAW), "--output", str(out), "--cap", "3")
        assert rc == 0
        ev = read_events(out, only="drift")[0]
        assert len(ev.failing_run_ids) <= 3

    def test_enveloped_input_path_succeeds(self, tmp_path):
        """--input pointing at an enveloped fixture works via autodetect."""
        out = tmp_path / "ev.jsonl"
        rc = _run_main("--input", str(MOCK_ENV), "--output", str(out))
        assert rc == 0
        assert len(read_events(out, only="drift")) == 1

    def test_explicit_format_events_flag(self, tmp_path):
        """--format events works explicitly on the enveloped fixture."""
        out = tmp_path / "ev.jsonl"
        rc = _run_main("--input", str(MOCK_ENV), "--output", str(out), "--format", "events")
        assert rc == 0
        assert len(read_events(out, only="drift")) == 1


# ---------------------------------------------------------------------------
# Tier C — e2e round-trip (mock-pinned; update if mock regenerated)
# ---------------------------------------------------------------------------

class TestEndToEnd:
    """Integration proof: full mock through main() -> read_events() -> correct event."""

    def setup_method(self):
        pass  # each test uses tmp_path — no shared state

    def test_exactly_one_drift_event(self, tmp_path):
        out = tmp_path / "ev.jsonl"
        rc = _run_main("--input", str(MOCK_RAW), "--output", str(out))
        assert rc == 0
        evs = read_events(out, only="drift")
        assert len(evs) == 1

    def test_event_channel(self, tmp_path):
        out = tmp_path / "ev.jsonl"
        _run_main("--input", str(MOCK_RAW), "--output", str(out))
        ev = read_events(out, only="drift")[0]
        assert ev.channel == "execution_accuracy"

    def test_event_failure_mode(self, tmp_path):
        out = tmp_path / "ev.jsonl"
        _run_main("--input", str(MOCK_RAW), "--output", str(out))
        ev = read_events(out, only="drift")[0]
        assert ev.failure_mode == FailureMode.VALID_BUT_WRONG

    def test_event_failing_run_ids_non_empty_within_cap(self, tmp_path):
        out = tmp_path / "ev.jsonl"
        _run_main("--input", str(MOCK_RAW), "--output", str(out))
        ev = read_events(out, only="drift")[0]
        assert len(ev.failing_run_ids) > 0
        assert len(ev.failing_run_ids) <= DetectorConfig().failing_ids_cap

    def test_event_ids_are_real_failures(self, tmp_path):
        """Every persisted run_id must be a genuine acc==0 run in the mock."""
        out = tmp_path / "ev.jsonl"
        _run_main("--input", str(MOCK_RAW), "--output", str(out))
        ev = read_events(out, only="drift")[0]
        recs = list(load_telemetry(MOCK_RAW))
        zeros = {r.run_id for r in recs if r.execution_accuracy == 0.0}
        assert all(rid in zeros for rid in ev.failing_run_ids)

    def test_event_severity_positive(self, tmp_path):
        out = tmp_path / "ev.jsonl"
        _run_main("--input", str(MOCK_RAW), "--output", str(out))
        ev = read_events(out, only="drift")[0]
        assert ev.severity > 0

    def test_cap3_end_to_end(self, tmp_path):
        """--cap 3 is honored in the persisted event, not just in memory."""
        out = tmp_path / "ev.jsonl"
        rc = _run_main("--input", str(MOCK_RAW), "--output", str(out), "--cap", "3")
        assert rc == 0
        ev = read_events(out, only="drift")[0]
        assert len(ev.failing_run_ids) <= 3
