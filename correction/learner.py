"""Build verified FewShotExamples from failing cases (anti-forgetting / anchoring).

The learner calls the teacher, verifies its output by running it against the gold SQL,
and falls back to gold on a miss. Anchor cases (easy baseline successes) are appended
as-is to prevent regression on the easy bucket after hard-query injection.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from contracts.schemas import FewShotExample


@dataclass
class FailingCase:
    """One agent run that got execution_accuracy == 0.

    Produced by the orchestrator from a (TelemetryRecord, FeedItem) pair.
    Passed to the learner so correction never has to re-read events.jsonl.
    """
    run_id: str
    question: str
    db_id: str
    broken_sql: str
    gold_sql: str
    difficulty: str


def make_examples(
    failing_cases: list[FailingCase],
    anchor_cases: list[FailingCase] = (),
    teacher: Callable[[str, str], str] | None = None,
    _get_db_path: Callable[[str], str] | None = None,
    _schema_text: Callable[[str], str] | None = None,
    _execution_accuracy: Callable[[str, str, str], float | None] | None = None,
) -> list[FewShotExample]:
    """Build verified few-shot examples from failing cases, with easy anchors.

    For each failing case the flow is:
      1. Build schema string from db_id (via harness.spider).
      2. Call teacher to generate corrected SQL.
      3. Verify by executing teacher SQL vs gold SQL (Spider EX metric).
      4. teacher match → source="teacher"; mismatch or error → gold, source="gold".

    Anchor cases (baseline easy successes) are appended as source="anchor" so
    injecting hard examples does not regress the easy bucket.

    The *_get_db_path*, *_schema_text*, and *_execution_accuracy* kwargs exist
    solely for unit-test injection — callers in production never pass them.
    """
    if teacher is None:
        from correction.teacher import generate_sql as teacher  # type: ignore[assignment]

    if _get_db_path is None:
        from harness.spider import get_db_path as _get_db_path  # type: ignore[assignment]
    if _schema_text is None:
        from harness.spider import schema_text as _schema_text  # type: ignore[assignment]
    if _execution_accuracy is None:
        from harness.evaluator import execution_accuracy as _execution_accuracy  # type: ignore[assignment]

    examples: list[FewShotExample] = []

    for case in failing_cases:
        db_path = _get_db_path(case.db_id)
        schema = _schema_text(db_path)
        try:
            teacher_sql = teacher(case.question, schema)
            acc = _execution_accuracy(teacher_sql, case.gold_sql, db_path)
            if acc == 1.0:
                examples.append(FewShotExample(
                    question=case.question,
                    correct_sql=teacher_sql,
                    db_id=case.db_id,
                    source="teacher",
                ))
            else:
                # teacher missed (wrong result or non-None accuracy < 1) — use gold
                examples.append(FewShotExample(
                    question=case.question,
                    correct_sql=case.gold_sql,
                    db_id=case.db_id,
                    source="gold",
                ))
        except Exception:
            # teacher call failed entirely — fall back to gold
            examples.append(FewShotExample(
                question=case.question,
                correct_sql=case.gold_sql,
                db_id=case.db_id,
                source="gold",
            ))

    for case in anchor_cases:
        examples.append(FewShotExample(
            question=case.question,
            correct_sql=case.gold_sql,
            db_id=case.db_id,
            source="anchor",
        ))

    return examples
