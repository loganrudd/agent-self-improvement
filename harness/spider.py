"""Load a Spider subset: schemas, difficulty-pooled questions, gold SQL, SQLite DBs.

Run fixtures/prepare_spider.py first to build spider_subset.json and copy SQLite DBs.
Exposes: questions_by_difficulty(), get_db_path(), schema_text().
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

SUBSET_JSON = Path("fixtures/spider_subset.json")
DATABASES_DIR = Path("fixtures/databases")


def load_questions() -> list[dict]:
    with open(SUBSET_JSON) as f:
        return json.load(f)


def questions_by_difficulty(difficulties: list[str], questions: list[dict] | None = None) -> list[dict]:
    if questions is None:
        questions = load_questions()
    return [q for q in questions if q.get("difficulty") in difficulties]


def get_db_path(db_id: str) -> str:
    return str(DATABASES_DIR / f"{db_id}.sqlite")


def schema_text(db_path: str) -> str:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cur.fetchall()]
    parts = []
    for table in tables:
        cur.execute(f"PRAGMA table_info('{table}')")
        cols = cur.fetchall()
        col_defs = ", ".join(f"{c[1]} {c[2]}" for c in cols)
        parts.append(f"Table {table}({col_defs})")
    con.close()
    return "\n".join(parts)
