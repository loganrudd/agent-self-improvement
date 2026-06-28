"""Tests for orchestrator.py: _build_feed phase structure and dry_run_heldout aggregation.

All tests are hermetic — no API keys, no SQLite, no events.jsonl writes.
run_item and load_questions are patched at the orchestrator module level.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from contracts.schemas import AgentConfig, Difficulty, TelemetryRecord
from harness.feed import FeedItem
from orchestrator import _build_feed, dry_run_heldout


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_questions(n_easy: int = 20, n_hard: int = 20) -> list[dict]:
    easy = [
        {"id": f"e{i}", "question": "q", "expected_sql": "SELECT 1", "db_id": "db", "difficulty": "easy"}
        for i in range(n_easy)
    ]
    hard = [
        {"id": f"h{i}", "question": "q", "expected_sql": "SELECT 1", "db_id": "db", "difficulty": "hard"}
        for i in range(n_hard)
    ]
    return easy + hard


def _make_item(phase: str, difficulty: str = "hard", idx: int = 0) -> FeedItem:
    return FeedItem(
        question_id=f"q_{phase}_{idx}",
        question=f"question {idx}",
        gold_sql="SELECT COUNT(*) FROM t",
        db_id="db",
        difficulty=difficulty,
        phase=phase,
    )


def _make_record(accuracy: float, difficulty: str = "hard") -> TelemetryRecord:
    return TelemetryRecord(
        run_id=f"run_{int(accuracy * 100)}",
        timestamp=time.time(),
        difficulty=Difficulty(difficulty),
        execution_accuracy=accuracy,
        query_valid=accuracy > 0.0,
    )


def _make_mixed_items(n_baseline: int = 2, n_recovery: int = 4) -> list[FeedItem]:
    items = [_make_item("baseline", "easy", i) for i in range(n_baseline)]
    for i in range(n_recovery):
        diff = "hard" if i % 2 == 0 else "extra"
        items.append(_make_item("recovery", diff, 100 + i))
    return items


# ---------------------------------------------------------------------------
# _build_feed
# ---------------------------------------------------------------------------

class TestBuildFeed:
    def test_phases_present(self):
        with patch("orchestrator.load_questions", return_value=_make_questions()):
            items = _build_feed(n=5, full=False)
        assert {i.phase for i in items} == {"baseline", "degraded", "recovery"}

    def test_n_per_phase(self):
        with patch("orchestrator.load_questions", return_value=_make_questions()):
            items = _build_feed(n=7, full=False)
        for phase in ("baseline", "degraded", "recovery"):
            assert len([i for i in items if i.phase == phase]) == 7

    def test_full_uses_80(self):
        with patch("orchestrator.load_questions", return_value=_make_questions()):
            items = _build_feed(n=5, full=True)
        assert len([i for i in items if i.phase == "baseline"]) == 80

    def test_full_overrides_n(self):
        with patch("orchestrator.load_questions", return_value=_make_questions()):
            items = _build_feed(n=10, full=True)
        # full=True always wins regardless of --n
        assert len([i for i in items if i.phase == "recovery"]) == 80

    def test_degraded_and_recovery_disjoint(self):
        """Core benchmark-credibility invariant: no leakage from learn into held-out."""
        with patch("orchestrator.load_questions", return_value=_make_questions()):
            items = _build_feed(n=10, full=False)
        deg_ids = {i.question_id for i in items if i.phase == "degraded"}
        rec_ids = {i.question_id for i in items if i.phase == "recovery"}
        assert deg_ids.isdisjoint(rec_ids), (
            "LEAKAGE: recovery pool overlaps learn pool — recovery accuracy "
            "would be inflated by memorisation, not learning."
        )

    def test_deterministic_across_calls(self):
        qs = _make_questions()
        with patch("orchestrator.load_questions", return_value=qs):
            items1 = _build_feed(n=5, full=False)
        with patch("orchestrator.load_questions", return_value=qs):
            items2 = _build_feed(n=5, full=False)
        assert [(i.question_id, i.phase) for i in items1] == [(i.question_id, i.phase) for i in items2]

    def test_baseline_difficulty(self):
        with patch("orchestrator.load_questions", return_value=_make_questions()):
            items = _build_feed(n=10, full=False)
        baseline = [i for i in items if i.phase == "baseline"]
        assert all(i.difficulty in ("easy", "medium") for i in baseline)

    def test_recovery_difficulty(self):
        with patch("orchestrator.load_questions", return_value=_make_questions()):
            items = _build_feed(n=10, full=False)
        recovery = [i for i in items if i.phase == "recovery"]
        assert all(i.difficulty in ("hard", "extra") for i in recovery)


# ---------------------------------------------------------------------------
# dry_run_heldout
# ---------------------------------------------------------------------------

class TestDryRunHeldout:
    def test_only_recovery_items_run(self):
        items = _make_mixed_items(n_baseline=3, n_recovery=4)
        called_phases = []

        def fake_run(item, config):
            called_phases.append(item.phase)
            return _make_record(0.5)

        with patch("orchestrator.run_item", side_effect=fake_run):
            dry_run_heldout(items)

        assert all(p == "recovery" for p in called_phases)
        assert len(called_phases) == 4

    def test_uses_empty_few_shot_examples(self):
        items = _make_mixed_items(n_recovery=2)
        seen_configs: list[AgentConfig] = []

        def fake_run(item, config):
            seen_configs.append(config)
            return _make_record(1.0)

        with patch("orchestrator.run_item", side_effect=fake_run):
            dry_run_heldout(items)

        assert all(len(c.few_shot_examples) == 0 for c in seen_configs), (
            "dry_run_heldout must not inject any few-shot examples — "
            "it measures base-config performance only."
        )

    def test_overall_accuracy_is_mean(self):
        items = _make_mixed_items(n_recovery=4)
        records = [_make_record(1.0), _make_record(0.0), _make_record(1.0), _make_record(0.0)]

        with patch("orchestrator.run_item", side_effect=records):
            acc = dry_run_heldout(items)

        assert acc == pytest.approx(0.5)

    def test_none_records_excluded_from_accuracy(self):
        items = _make_mixed_items(n_recovery=3)
        # gold-SQL failure (None), then one correct, one wrong
        records = [None, _make_record(1.0), _make_record(0.0)]

        with patch("orchestrator.run_item", side_effect=records):
            acc = dry_run_heldout(items)

        # only 2 scored: (1.0 + 0.0) / 2 = 0.5
        assert acc == pytest.approx(0.5)

    def test_all_skipped_returns_zero(self):
        items = _make_mixed_items(n_recovery=3)
        with patch("orchestrator.run_item", return_value=None):
            acc = dry_run_heldout(items)
        assert acc == 0.0

    def test_explicit_config_forwarded(self):
        items = _make_mixed_items(n_recovery=1)
        custom = AgentConfig(config_id="custom-test", model="test-model", few_shot_examples=[])
        seen: list[AgentConfig] = []

        def fake_run(item, config):
            seen.append(config)
            return _make_record(1.0)

        with patch("orchestrator.run_item", side_effect=fake_run):
            dry_run_heldout(items, config=custom)

        assert len(seen) == 1
        assert seen[0].config_id == "custom-test"

    def test_default_config_uses_base_model(self):
        items = _make_mixed_items(n_recovery=1)
        seen: list[AgentConfig] = []

        def fake_run(item, config):
            seen.append(config)
            return _make_record(0.0)

        with patch("orchestrator.run_item", side_effect=fake_run):
            dry_run_heldout(items)

        assert seen[0].model == "MiniMax-M2.7-highspeed"

    def test_returns_overall_accuracy(self):
        items = _make_mixed_items(n_recovery=2)
        records = [_make_record(0.8), _make_record(0.4)]

        with patch("orchestrator.run_item", side_effect=records):
            acc = dry_run_heldout(items)

        assert acc == pytest.approx(0.6)

    def test_no_recovery_items_returns_zero(self):
        items = [_make_item("baseline", "easy", i) for i in range(3)]
        with patch("orchestrator.run_item") as mock_run:
            acc = dry_run_heldout(items)
        mock_run.assert_not_called()
        assert acc == 0.0
