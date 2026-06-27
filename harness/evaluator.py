"""Execution-based eval + complexity features.

- execute(sql, db_path) -> rows | None  (None = invalid SQL)
- execution_accuracy: compare generated rows vs gold rows AS SETS (order/dupe insensitive)
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


def _normalize(rows: list[tuple]) -> frozenset:
    return frozenset(
        tuple(str(v).strip().lower() if v is not None else "" for v in row)
        for row in rows
    )


def execution_accuracy(generated_sql: str, gold_sql: str, db_path: str) -> float:
    gen_rows = execute(generated_sql, db_path)
    gold_rows = execute(gold_sql, db_path)
    if gen_rows is None:
        return 0.0
    if gold_rows is None:
        return 1.0  # gold failed — can't penalize
    return 1.0 if _normalize(gen_rows) == _normalize(gold_rows) else 0.0


def query_valid(sql: str, db_path: str) -> bool:
    return execute(sql, db_path) is not None


def complexity(sql: str) -> int:
    upper = sql.upper()
    joins = len(re.findall(r"\bJOIN\b", upper))
    nested = max(0, len(re.findall(r"\bSELECT\b", upper)) - 1)
    having = 1 if re.search(r"\bHAVING\b", upper) else 0
    group_by = 1 if re.search(r"\bGROUP BY\b", upper) else 0
    return joins + nested + having + group_by
