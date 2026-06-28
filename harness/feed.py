"""The change-point stratified feed (rules/02-tech-decisions.md).

Phase 1 baseline: sample easy/medium.   --change-point-->
Phase 2 degraded: sample hard/extra.
Phase 3 recovery: keep sampling the SAME hard/extra pool (acc recovers via learned few-shots).

Fast REPLAY mode: pre-compute the full stream once, replay instantly on stage.
Live change-point trigger: caller can swap phase by setting change_point_at.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterator


@dataclass
class FeedItem:
    question_id: str
    question: str
    gold_sql: str
    db_id: str
    difficulty: str
    phase: str   # "baseline" | "degraded" | "recovery"


def build_stream(
    questions: list[dict],
    n_baseline: int = 80,
    n_degraded: int = 80,
    n_recovery: int = 80,
    seed: int = 42,
) -> list[FeedItem]:
    """Pre-compute the full demo stream. Call once; replay fast."""
    rng = random.Random(seed)
    easy_med = [q for q in questions if q["difficulty"] in ("easy", "medium")]
    hard_extra = [q for q in questions if q["difficulty"] in ("hard", "extra")]

    if not easy_med:
        raise ValueError("No easy/medium questions — run fixtures/prepare_spider.py first")
    if not hard_extra:
        raise ValueError("No hard/extra questions — run fixtures/prepare_spider.py first")

    def _pick(pool: list[dict], n: int, phase: str) -> list[FeedItem]:
        return [
            FeedItem(
                question_id=q["id"],
                question=q["question"],
                gold_sql=q["expected_sql"],
                db_id=q["db_id"],
                difficulty=q["difficulty"],
                phase=phase,
            )
            for q in rng.choices(pool, k=n)
        ]

    return (
        _pick(easy_med, n_baseline, "baseline")
        + _pick(hard_extra, n_degraded, "degraded")
        + _pick(hard_extra, n_recovery, "recovery")
    )


def stream(items: list[FeedItem]) -> Iterator[FeedItem]:
    yield from items
