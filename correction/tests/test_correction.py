"""Tests for correction/correction.py — handle() contract shape and gating."""
from __future__ import annotations

import time

import pytest

from contracts.schemas import CorrectionAction, DriftEvent, FailureMode, FewShotExample
from correction.correction import _MIN_SEVERITY, handle
from correction.learner import FailingCase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _drift_event(severity: float = 0.40) -> DriftEvent:
    return DriftEvent(
        detected_at=time.time(),
        channel="execution_accuracy",
        severity=severity,
        window_mean=0.50,
        baseline_mean=0.90,
        failure_mode=FailureMode.VALID_BUT_WRONG,
        failing_run_ids=["run_0080", "run_0081"],
    )


def _case(idx: int = 0, difficulty: str = "hard") -> FailingCase:
    return FailingCase(
        run_id=f"run_{idx:04d}",
        question=f"question {idx}",
        db_id="test_db",
        broken_sql="SELECT bad",
        gold_sql=f"SELECT gold_{idx} FROM t",
        difficulty=difficulty,
    )


def _fake_make_examples(failing_cases, anchor_cases=(), **kwargs):
    """Stub that returns one FewShotExample per case without touching any model or DB."""
    examples = []
    for c in failing_cases:
        examples.append(FewShotExample(
            question=c.question, correct_sql=c.gold_sql, db_id=c.db_id, source="gold"
        ))
    for c in anchor_cases:
        examples.append(FewShotExample(
            question=c.question, correct_sql=c.gold_sql, db_id=c.db_id, source="anchor"
        ))
    return examples


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHandle:
    def test_returns_correction_action(self, monkeypatch):
        monkeypatch.setattr("correction.correction.make_examples", _fake_make_examples)
        result = handle(_drift_event(), [_case(0)])
        assert isinstance(result, CorrectionAction)

    def test_triggered_by_matches_channel(self, monkeypatch):
        monkeypatch.setattr("correction.correction.make_examples", _fake_make_examples)
        event = _drift_event()
        result = handle(event, [_case(0)])
        assert result.triggered_by == event.channel

    def test_examples_populated_from_failing_cases(self, monkeypatch):
        monkeypatch.setattr("correction.correction.make_examples", _fake_make_examples)
        cases = [_case(i) for i in range(3)]
        result = handle(_drift_event(), cases)
        assert len(result.new_few_shot_examples) == 3

    def test_rationale_is_non_empty_on_real_drift(self, monkeypatch):
        monkeypatch.setattr("correction.correction.make_examples", _fake_make_examples)
        result = handle(_drift_event(), [_case(0)])
        assert len(result.rationale) > 0

    def test_rationale_mentions_window_and_baseline(self, monkeypatch):
        monkeypatch.setattr("correction.correction.make_examples", _fake_make_examples)
        result = handle(_drift_event(severity=0.35), [_case(0)])
        assert "window" in result.rationale
        assert "baseline" in result.rationale

    def test_below_threshold_severity_returns_empty_examples(self, monkeypatch):
        monkeypatch.setattr("correction.correction.make_examples", _fake_make_examples)
        low_event = _drift_event(severity=_MIN_SEVERITY - 0.001)
        result = handle(low_event, [_case(0)])
        assert result.new_few_shot_examples == []

    def test_below_threshold_rationale_says_too_small(self, monkeypatch):
        monkeypatch.setattr("correction.correction.make_examples", _fake_make_examples)
        low_event = _drift_event(severity=0.0)
        result = handle(low_event, [_case(0)])
        assert "threshold" in result.rationale

    def test_anchor_cases_included_in_examples(self, monkeypatch):
        monkeypatch.setattr("correction.correction.make_examples", _fake_make_examples)
        failing = [_case(0)]
        anchors = [_case(99, difficulty="easy")]
        result = handle(_drift_event(), failing, anchor_cases=anchors)
        sources = {e.source for e in result.new_few_shot_examples}
        assert "anchor" in sources

    def test_empty_failing_cases_still_returns_action(self, monkeypatch):
        monkeypatch.setattr("correction.correction.make_examples", _fake_make_examples)
        result = handle(_drift_event(), [])
        assert isinstance(result, CorrectionAction)
        assert result.new_few_shot_examples == []

    def test_at_threshold_severity_corrects(self, monkeypatch):
        monkeypatch.setattr("correction.correction.make_examples", _fake_make_examples)
        # exactly at threshold should still trigger
        at_threshold = _drift_event(severity=_MIN_SEVERITY)
        result = handle(at_threshold, [_case(0)])
        assert len(result.new_few_shot_examples) == 1
