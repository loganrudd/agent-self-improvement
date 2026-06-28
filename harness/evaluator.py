"""Execution-based eval + complexity features.

- execute(sql, db_path) -> rows | None  (None = invalid SQL)
- execution_accuracy: compare generated rows vs gold rows, order-aware when gold uses ORDER BY.
  Matches Spider's official Test-Suite EX metric semantics:
    * No ORDER BY in gold → compare as frozensets (row-order and duplicate insensitive).
    * ORDER BY in gold    → compare as lists (order matters for top-k queries).
    * Gold SQL fails       → skip (return None) so the record is excluded rather than inflated.
- query_valid: did generated SQL execute without error
- complexity(sql) -> int  joins + nesting count
"""
from __future__ import annotations

import re
import sqlite3


def execute(sql: str, db_path: str) -> list[tuple] | None:
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        con.close()
        return rows
    except Exception:
        return None


def _has_order_by(sql: str) -> bool:
    return bool(re.search(r"\bORDER\s+BY\b", sql, re.IGNORECASE))


def _normalize_row(row: tuple) -> tuple:
    return tuple(str(v).strip().lower() if v is not None else "" for v in row)


def execution_accuracy(generated_sql: str, gold_sql: str, db_path: str) -> float | None:
    """Score one generated query against gold using Spider EX semantics.

    Returns None when gold SQL itself fails — callers should exclude these records
    from aggregate accuracy rather than counting them (avoids inflation).
    """
    gen_rows = execute(generated_sql, db_path)
    gold_rows = execute(gold_sql, db_path)

    if gold_rows is None:
        return None  # gold is broken; exclude from aggregate — do not score 1.0

    if gen_rows is None:
        return 0.0

    norm_gen = [_normalize_row(r) for r in gen_rows]
    norm_gold = [_normalize_row(r) for r in gold_rows]

    if _has_order_by(gold_sql):
        # Order matters: top-k queries, ranked results
        return 1.0 if norm_gen == norm_gold else 0.0
    else:
        # Order-insensitive: match as multisets
        return 1.0 if sorted(norm_gen) == sorted(norm_gold) else 0.0


def query_valid(sql: str, db_path: str) -> bool:
    if sql.startswith("-- error:") or not sql.strip():
        return False
    return execute(sql, db_path) is not None


def complexity(sql: str) -> int:
    upper = sql.upper()
    joins = len(re.findall(r"\bJOIN\b", upper))
    nested = max(0, len(re.findall(r"\bSELECT\b", upper)) - 1)
    having = 1 if re.search(r"\bHAVING\b", upper) else 0
    group_by = 1 if re.search(r"\bGROUP BY\b", upper) else 0
    return joins + nested + having + group_by
