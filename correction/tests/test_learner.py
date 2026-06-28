"""Tests for correction/learner.py.

All tests are hermetic — no API calls, no SQLite. teacher, get_db_path,
schema_text, and execution_accuracy are all injected as fakes.
"""
from __future__ import annotations

import pytest

from correction.learner import FailingCase, make_examples


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _case(idx: int = 0, difficulty: str = "hard") -> FailingCase:
    return FailingCase(
        run_id=f"run_{idx:04d}",
        question=f"question {idx}",
        db_id="test_db",
        broken_sql="SELECT bad FROM table",
        gold_sql=f"SELECT gold_{idx} FROM table",
        difficulty=difficulty,
    )


def _noop_infra(accuracy: float | None = 1.0):
    """Return injectable fakes for the DB/eval layer."""
    def fake_get_db_path(db_id: str) -> str:
        return f"/fake/{db_id}.sqlite"

    def fake_schema_text(db_path: str) -> str:
        return "Table test(id INT, val TEXT)"

    def fake_execution_accuracy(sql: str, gold: str, db_path: str) -> float | None:
        return accuracy

    return fake_get_db_path, fake_schema_text, fake_execution_accuracy


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMakeExamples:
    def test_teacher_match_gives_teacher_source(self):
        db, schema, acc = _noop_infra(accuracy=1.0)
        teacher_sql = "SELECT correct FROM table"
        examples = make_examples(
            [_case(0)],
            teacher=lambda q, s: teacher_sql,
            _get_db_path=db, _schema_text=schema, _execution_accuracy=acc,
        )
        assert len(examples) == 1
        assert examples[0].source == "teacher"
        assert examples[0].correct_sql == teacher_sql

    def test_teacher_miss_falls_back_to_gold(self):
        db, schema, acc = _noop_infra(accuracy=0.0)
        case = _case(0)
        examples = make_examples(
            [case],
            teacher=lambda q, s: "SELECT wrong FROM table",
            _get_db_path=db, _schema_text=schema, _execution_accuracy=acc,
        )
        assert len(examples) == 1
        assert examples[0].source == "gold"
        assert examples[0].correct_sql == case.gold_sql

    def test_teacher_none_accuracy_falls_back_to_gold(self):
        # execution_accuracy returning None means gold SQL failed; treat as mismatch
        db, schema, acc = _noop_infra(accuracy=None)
        case = _case(0)
        examples = make_examples(
            [case],
            teacher=lambda q, s: "SELECT something FROM table",
            _get_db_path=db, _schema_text=schema, _execution_accuracy=acc,
        )
        assert examples[0].source == "gold"

    def test_teacher_exception_falls_back_to_gold(self):
        db, schema, _ = _noop_infra()
        case = _case(0)

        def bad_teacher(q, s):
            raise RuntimeError("API timeout")

        examples = make_examples(
            [case],
            teacher=bad_teacher,
            _get_db_path=db, _schema_text=schema, _execution_accuracy=lambda *a: 1.0,
        )
        assert len(examples) == 1
        assert examples[0].source == "gold"
        assert examples[0].correct_sql == case.gold_sql

    def test_anchor_cases_appended_as_anchor_source(self):
        db, schema, acc = _noop_infra(accuracy=1.0)
        failing = [_case(0)]
        anchor = [_case(99, difficulty="easy")]
        examples = make_examples(
            failing,
            anchor_cases=anchor,
            teacher=lambda q, s: "SELECT x FROM t",
            _get_db_path=db, _schema_text=schema, _execution_accuracy=acc,
        )
        assert len(examples) == 2
        assert examples[-1].source == "anchor"
        assert examples[-1].question == anchor[0].question

    def test_anchors_use_gold_sql(self):
        db, schema, acc = _noop_infra(accuracy=1.0)
        anchor = _case(5, difficulty="easy")
        examples = make_examples(
            [],
            anchor_cases=[anchor],
            teacher=lambda q, s: "SELECT x FROM t",
            _get_db_path=db, _schema_text=schema, _execution_accuracy=acc,
        )
        assert examples[0].correct_sql == anchor.gold_sql

    def test_empty_inputs_returns_empty_list(self):
        db, schema, acc = _noop_infra()
        examples = make_examples(
            [],
            teacher=lambda q, s: "SELECT 1",
            _get_db_path=db, _schema_text=schema, _execution_accuracy=acc,
        )
        assert examples == []

    def test_multiple_failing_cases_all_processed(self):
        db, schema, acc = _noop_infra(accuracy=1.0)
        cases = [_case(i) for i in range(5)]
        examples = make_examples(
            cases,
            teacher=lambda q, s: "SELECT 1",
            _get_db_path=db, _schema_text=schema, _execution_accuracy=acc,
        )
        assert len(examples) == 5

    def test_db_id_propagated_to_example(self):
        db, schema, acc = _noop_infra(accuracy=1.0)
        case = FailingCase(
            run_id="r0", question="q", db_id="my_database",
            broken_sql="SELECT bad", gold_sql="SELECT good", difficulty="hard"
        )
        examples = make_examples(
            [case],
            teacher=lambda q, s: "SELECT good",
            _get_db_path=db, _schema_text=schema, _execution_accuracy=acc,
        )
        assert examples[0].db_id == "my_database"

    def test_question_propagated_to_example(self):
        db, schema, acc = _noop_infra(accuracy=1.0)
        case = _case(0)
        examples = make_examples(
            [case],
            teacher=lambda q, s: "SELECT 1",
            _get_db_path=db, _schema_text=schema, _execution_accuracy=acc,
        )
        assert examples[0].question == case.question
