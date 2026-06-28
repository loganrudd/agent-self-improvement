"""Tests for feed.py (train/held-out split) and evaluator.py (Spider EX semantics)."""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from harness.feed import FeedItem, build_stream, _split_hard, _split_hard_by_db
from harness.evaluator import execution_accuracy, _has_order_by


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_q(id: str, difficulty: str) -> dict:
    return {
        "id": id,
        "question": f"q_{id}",
        "expected_sql": "SELECT 1",
        "db_id": "db",
        "difficulty": difficulty,
    }


def _make_db(tmp: str, rows: list[tuple]) -> str:
    """Write a tiny SQLite DB with a single table t(a, b) and return its path."""
    path = os.path.join(tmp, "test.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (a TEXT, b INTEGER)")
    con.executemany("INSERT INTO t VALUES (?, ?)", rows)
    con.commit()
    con.close()
    return path


# ---------------------------------------------------------------------------
# feed.py: _split_hard
# ---------------------------------------------------------------------------

class TestSplitHard:
    def _pool(self, n: int) -> list[dict]:
        return [_make_q(str(i), "hard") for i in range(n)]

    def test_disjoint(self):
        import random
        pool = self._pool(10)
        learn, held = _split_hard(pool, 0.5, random.Random(0))
        learn_ids = {q["id"] for q in learn}
        held_ids = {q["id"] for q in held}
        assert learn_ids.isdisjoint(held_ids)

    def test_union_is_full_pool(self):
        import random
        pool = self._pool(10)
        learn, held = _split_hard(pool, 0.5, random.Random(0))
        assert len(learn) + len(held) == len(pool)

    def test_learn_frac_respected(self):
        import random
        pool = self._pool(20)
        learn, held = _split_hard(pool, 0.6, random.Random(0))
        assert len(learn) == 12
        assert len(held) == 8

    def test_min_one_in_learn(self):
        import random
        pool = self._pool(3)
        learn, _ = _split_hard(pool, 0.0, random.Random(0))
        assert len(learn) >= 1

    def test_deterministic_with_same_seed(self):
        import random
        pool = self._pool(10)
        l1, h1 = _split_hard(pool, 0.5, random.Random(42))
        l2, h2 = _split_hard(pool, 0.5, random.Random(42))
        assert [q["id"] for q in l1] == [q["id"] for q in l2]
        assert [q["id"] for q in h1] == [q["id"] for q in h2]


# ---------------------------------------------------------------------------
# feed.py: _split_hard_by_db
# ---------------------------------------------------------------------------

class TestSplitHardByDb:
    def _pool(self, db_counts: dict[str, int]) -> list[dict]:
        """Build a pool where each key is a db_id and value is number of questions."""
        qs = []
        for db_id, n in db_counts.items():
            for i in range(n):
                qs.append({"id": f"{db_id}_{i}", "db_id": db_id, "difficulty": "hard"})
        return qs

    def test_multi_db_contributes_one_to_heldout(self):
        import random
        pool = self._pool({"db_a": 3, "db_b": 2})
        learn, held = _split_hard_by_db(pool, random.Random(0))
        held_dbs = [q["db_id"] for q in held]
        assert held_dbs.count("db_a") == 1
        assert held_dbs.count("db_b") == 1

    def test_single_db_goes_entirely_to_learn(self):
        import random
        pool = self._pool({"db_a": 3, "db_single": 1})
        learn, held = _split_hard_by_db(pool, random.Random(0))
        held_dbs = {q["db_id"] for q in held}
        assert "db_single" not in held_dbs
        learn_dbs = [q["db_id"] for q in learn]
        assert learn_dbs.count("db_single") == 1

    def test_all_single_question_dbs_gives_empty_heldout(self):
        import random
        pool = self._pool({"db_x": 1, "db_y": 1, "db_z": 1})
        learn, held = _split_hard_by_db(pool, random.Random(0))
        assert held == []
        assert len(learn) == 3

    def test_heldout_and_learn_are_disjoint(self):
        import random
        pool = self._pool({"db_a": 4, "db_b": 3, "db_c": 1})
        learn, held = _split_hard_by_db(pool, random.Random(42))
        learn_ids = {q["id"] for q in learn}
        held_ids = {q["id"] for q in held}
        assert learn_ids.isdisjoint(held_ids)

    def test_union_is_full_pool(self):
        import random
        pool = self._pool({"db_a": 3, "db_b": 2, "db_c": 1})
        learn, held = _split_hard_by_db(pool, random.Random(7))
        assert len(learn) + len(held) == len(pool)

    def test_deterministic_with_same_seed(self):
        import random
        pool = self._pool({"db_a": 3, "db_b": 4})
        l1, h1 = _split_hard_by_db(pool, random.Random(99))
        l2, h2 = _split_hard_by_db(pool, random.Random(99))
        assert [q["id"] for q in l1] == [q["id"] for q in l2]
        assert [q["id"] for q in h1] == [q["id"] for q in h2]

    def test_every_heldout_has_same_db_in_learn(self):
        """The core invariant: each held-out question has ≥1 same-DB question in LEARN."""
        import random
        pool = self._pool({"db_a": 3, "db_b": 2, "db_c": 2, "db_solo": 1})
        learn, held = _split_hard_by_db(pool, random.Random(0))
        learn_dbs = {q["db_id"] for q in learn}
        for q in held:
            assert q["db_id"] in learn_dbs, (
                f"held-out question on {q['db_id']} has no matching LEARN example"
            )

    def test_fractional_heldout_with_large_pool(self):
        """With a large per-DB pool, ~40% go to held-out and the rest to LEARN."""
        import random
        pool = self._pool({"db_big": 20})   # 20 Qs → int(20*0.4)=8 held-out, 12 learn
        learn, held = _split_hard_by_db(pool, random.Random(0), heldout_frac=0.4)
        assert len(held) == 8
        assert len(learn) == 12

    def test_every_heldout_has_same_db_in_learn_fractional(self):
        """Core invariant holds with fractional split: every held-out Q has a same-DB LEARN sibling."""
        import random
        pool = self._pool({"db_a": 10, "db_b": 8, "db_c": 6, "db_solo": 1})
        learn, held = _split_hard_by_db(pool, random.Random(0), heldout_frac=0.4)
        learn_dbs = {q["db_id"] for q in learn}
        for q in held:
            assert q["db_id"] in learn_dbs, (
                f"held-out question on {q['db_id']} has no matching LEARN example"
            )
        assert "db_solo" not in {q["db_id"] for q in held}

    def test_same_db_split_flag_in_build_stream(self):
        """build_stream with same_db_split=True produces valid disjoint phases."""
        easy = [{"id": f"e{i}", "question": "q", "expected_sql": "SELECT 1",
                 "db_id": "db_easy", "difficulty": "easy"} for i in range(5)]
        hard = [{"id": f"h{i}", "question": "q", "expected_sql": "SELECT 1",
                 "db_id": f"db_{i//2}", "difficulty": "hard"} for i in range(10)]
        items = build_stream(easy + hard, n_baseline=3, n_degraded=3, n_recovery=3,
                             seed=0, same_db_split=True)
        deg_ids = {it.question_id for it in items if it.phase == "degraded"}
        rec_ids = {it.question_id for it in items if it.phase == "recovery"}
        assert deg_ids.isdisjoint(rec_ids)


# ---------------------------------------------------------------------------
# feed.py: build_stream
# ---------------------------------------------------------------------------

class TestBuildStream:
    def _questions(self) -> list[dict]:
        easy = [_make_q(f"e{i}", "easy") for i in range(10)]
        med = [_make_q(f"m{i}", "medium") for i in range(10)]
        hard = [_make_q(f"h{i}", "hard") for i in range(20)]
        extra = [_make_q(f"x{i}", "extra") for i in range(10)]
        return easy + med + hard + extra

    def test_phases_present(self):
        items = build_stream(self._questions(), n_baseline=5, n_degraded=5, n_recovery=5)
        phases = {i.phase for i in items}
        assert phases == {"baseline", "degraded", "recovery"}

    def test_phase_lengths(self):
        items = build_stream(self._questions(), n_baseline=5, n_degraded=7, n_recovery=9)
        by_phase = {p: [i for i in items if i.phase == p] for p in ("baseline", "degraded", "recovery")}
        assert len(by_phase["baseline"]) == 5
        assert len(by_phase["degraded"]) == 7
        assert len(by_phase["recovery"]) == 9

    def test_baseline_is_easy_med(self):
        items = build_stream(self._questions(), n_baseline=20, n_degraded=5, n_recovery=5)
        baseline = [i for i in items if i.phase == "baseline"]
        assert all(i.difficulty in ("easy", "medium") for i in baseline)

    def test_degraded_is_hard_extra(self):
        items = build_stream(self._questions(), n_baseline=5, n_degraded=20, n_recovery=5)
        degraded = [i for i in items if i.phase == "degraded"]
        assert all(i.difficulty in ("hard", "extra") for i in degraded)

    def test_recovery_is_hard_extra(self):
        items = build_stream(self._questions(), n_baseline=5, n_degraded=5, n_recovery=20)
        recovery = [i for i in items if i.phase == "recovery"]
        assert all(i.difficulty in ("hard", "extra") for i in recovery)

    def test_baseline_easy_only_excludes_medium(self):
        """baseline_easy_only=True draws the baseline phase from easy questions only."""
        items = build_stream(self._questions(), n_baseline=20, n_degraded=5, n_recovery=5,
                             baseline_easy_only=True)
        baseline = [i for i in items if i.phase == "baseline"]
        assert baseline, "baseline phase should be non-empty"
        assert all(i.difficulty == "easy" for i in baseline), (
            "easy-only baseline must not contain medium questions"
        )

    def test_baseline_easy_only_raises_if_no_easy(self):
        """With no easy questions, easy-only baseline fails fast rather than firing on medium."""
        questions = ([_make_q(f"m{i}", "medium") for i in range(5)]
                     + [_make_q(f"h{i}", "hard") for i in range(5)])
        with pytest.raises(ValueError, match="baseline questions"):
            build_stream(questions, n_baseline=2, n_degraded=2, n_recovery=2,
                         baseline_easy_only=True)

    def test_degraded_and_recovery_are_disjoint(self):
        """Core benchmark-credibility invariant: no question_id from the learn pool
        appears in the held-out recovery pool."""
        items = build_stream(self._questions(), n_baseline=5, n_degraded=50, n_recovery=50, seed=0)
        degraded_ids = {i.question_id for i in items if i.phase == "degraded"}
        recovery_ids = {i.question_id for i in items if i.phase == "recovery"}
        assert degraded_ids.isdisjoint(recovery_ids), (
            "LEAKAGE: recovery phase contains questions from the learn/degraded pool. "
            "Recovery accuracy would be inflated by memorisation."
        )

    def test_empty_hard_pool_raises(self):
        questions = [_make_q(f"e{i}", "easy") for i in range(10)]
        with pytest.raises(ValueError, match="hard/extra"):
            build_stream(questions, n_baseline=5, n_degraded=5, n_recovery=5)

    def test_empty_easy_pool_raises(self):
        questions = [_make_q(f"h{i}", "hard") for i in range(10)]
        with pytest.raises(ValueError, match="easy/medium"):
            build_stream(questions, n_baseline=5, n_degraded=5, n_recovery=5)

    def test_small_hard_pool_raises_if_heldout_empty(self):
        questions = [_make_q("e0", "easy")] + [_make_q("h0", "hard")]
        with pytest.raises(ValueError, match="Held-out pool is empty"):
            build_stream(questions, n_baseline=1, n_degraded=1, n_recovery=1, learn_frac=1.0)

    def test_deterministic(self):
        q = self._questions()
        s1 = build_stream(q, n_baseline=5, n_degraded=5, n_recovery=5, seed=7)
        s2 = build_stream(q, n_baseline=5, n_degraded=5, n_recovery=5, seed=7)
        assert [(i.question_id, i.phase) for i in s1] == [(i.question_id, i.phase) for i in s2]

    def test_feeditem_fields(self):
        items = build_stream(self._questions(), n_baseline=1, n_degraded=1, n_recovery=1)
        for item in items:
            assert isinstance(item, FeedItem)
            assert item.question_id
            assert item.question
            assert item.gold_sql
            assert item.db_id
            assert item.difficulty in ("easy", "medium", "hard", "extra")
            assert item.phase in ("baseline", "degraded", "recovery")


# ---------------------------------------------------------------------------
# evaluator.py: _has_order_by
# ---------------------------------------------------------------------------

class TestHasOrderBy:
    def test_detects_order_by(self):
        assert _has_order_by("SELECT a FROM t ORDER BY a")

    def test_case_insensitive(self):
        assert _has_order_by("select a from t order by a desc")

    def test_no_order_by(self):
        assert not _has_order_by("SELECT COUNT(*) FROM t GROUP BY b")

    def test_order_in_subquery_counts(self):
        sql = "SELECT * FROM (SELECT a FROM t ORDER BY a) sub"
        assert _has_order_by(sql)


# ---------------------------------------------------------------------------
# evaluator.py: execution_accuracy (Spider EX semantics)
# ---------------------------------------------------------------------------

class TestExecutionAccuracy:
    @pytest.fixture()
    def db(self, tmp_path):
        path = str(tmp_path / "test.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE t (a TEXT, b INTEGER)")
        con.executemany("INSERT INTO t VALUES (?, ?)", [("x", 1), ("y", 2), ("z", 3)])
        con.commit()
        con.close()
        return path

    # --- gold SQL fails → None (exclude, not 1.0) ---

    def test_gold_failure_returns_none(self, db):
        result = execution_accuracy("SELECT 1", "SELECT * FROM nonexistent", db)
        assert result is None

    def test_gold_failure_not_one(self, db):
        result = execution_accuracy("SELECT a FROM t", "SELECT * FROM missing_table", db)
        assert result != 1.0

    # --- generated SQL fails → 0.0 ---

    def test_generated_invalid_returns_zero(self, db):
        result = execution_accuracy("SELECT * FROM missing", "SELECT a FROM t", db)
        assert result == 0.0

    # --- order-insensitive (no ORDER BY in gold) ---

    def test_correct_different_order(self, db):
        gen = "SELECT a FROM t ORDER BY a DESC"
        gold = "SELECT a FROM t"
        assert execution_accuracy(gen, gold, db) == 1.0

    def test_wrong_result_no_order(self, db):
        gen = "SELECT a FROM t WHERE b = 1"
        gold = "SELECT a FROM t"
        assert execution_accuracy(gen, gold, db) == 0.0

    def test_extra_rows_wrong(self, db):
        gen = "SELECT a FROM t"
        gold = "SELECT a FROM t WHERE b = 1"
        assert execution_accuracy(gen, gold, db) == 0.0

    # --- order-sensitive (ORDER BY in gold) ---

    def test_order_matters_correct(self, db):
        gen = "SELECT a FROM t ORDER BY b ASC"
        gold = "SELECT a FROM t ORDER BY b ASC"
        assert execution_accuracy(gen, gold, db) == 1.0

    def test_order_matters_wrong_order(self, db):
        gen = "SELECT a FROM t ORDER BY b DESC"
        gold = "SELECT a FROM t ORDER BY b ASC"
        assert execution_accuracy(gen, gold, db) == 0.0

    # --- value normalisation ---

    def test_case_insensitive_values(self, db):
        con = sqlite3.connect(db)
        con.execute("INSERT INTO t VALUES ('X', 4)")
        con.commit()
        con.close()
        gen = "SELECT LOWER(a) FROM t WHERE b = 4"
        gold = "SELECT a FROM t WHERE b = 4"
        assert execution_accuracy(gen, gold, db) == 1.0

    def test_whitespace_normalised(self, db):
        con = sqlite3.connect(db)
        con.execute("INSERT INTO t VALUES ('  x  ', 5)")
        con.commit()
        con.close()
        gen = "SELECT TRIM(a) FROM t WHERE b = 5"
        gold = "SELECT a FROM t WHERE b = 5"
        assert execution_accuracy(gen, gold, db) == 1.0

    # --- aggregate queries ---

    def test_count_correct(self, db):
        gen = "SELECT COUNT(*) FROM t"
        gold = "SELECT COUNT(*) FROM t"
        assert execution_accuracy(gen, gold, db) == 1.0

    def test_count_wrong(self, db):
        gen = "SELECT COUNT(*) FROM t WHERE b > 1"
        gold = "SELECT COUNT(*) FROM t"
        assert execution_accuracy(gen, gold, db) == 0.0
