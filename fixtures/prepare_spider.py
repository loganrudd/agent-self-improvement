"""
Pull a Spider subset for the demo. Run once from repo root:

    python fixtures/prepare_spider.py

Prereq: git clone https://github.com/taoyds/spider.git /tmp/spider_data

Outputs:
    fixtures/spider_subset.json         — 100 questions (50 easy/med + 50 hard/extra)
    fixtures/databases/<db_id>.sqlite   — SQLite DB files for selected schemas
"""
from __future__ import annotations

import json
import os
import re
import random
import shutil
import sys
from pathlib import Path

SPIDER_DATA = os.environ.get("SPIDER_DATA", "/tmp/spider_data")
OUTPUT_JSON = Path("fixtures/spider_subset.json")
OUTPUT_DBS = Path("fixtures/databases")
TARGET_EASY_MED = 50
TARGET_HARD_EXTRA = 50
SEED = 42


def classify_difficulty(sql: str) -> str:
    upper = sql.upper()
    joins = len(re.findall(r"\bJOIN\b", upper))
    nested = max(0, len(re.findall(r"\bSELECT\b", upper)) - 1)
    having = 1 if re.search(r"\bHAVING\b", upper) else 0
    group_by = 1 if re.search(r"\bGROUP BY\b", upper) else 0
    set_ops = 1 if re.search(r"\b(INTERSECT|EXCEPT|UNION)\b", upper) else 0
    score = joins + nested * 2 + having + group_by + set_ops * 2
    if score == 0:
        return "easy"
    elif score <= 2:
        return "medium"
    elif score <= 5:
        return "hard"
    else:
        return "extra"


def count_complexity(sql: str) -> int:
    upper = sql.upper()
    joins = len(re.findall(r"\bJOIN\b", upper))
    nested = max(0, len(re.findall(r"\bSELECT\b", upper)) - 1)
    having = 1 if re.search(r"\bHAVING\b", upper) else 0
    group_by = 1 if re.search(r"\bGROUP BY\b", upper) else 0
    return joins + nested + having + group_by


def main() -> None:
    train_path = os.path.join(SPIDER_DATA, "train_spider.json")
    if not os.path.exists(train_path):
        print(f"Spider data not found at {train_path}")
        print("Run: git clone https://github.com/taoyds/spider.git /tmp/spider_data")
        sys.exit(1)

    db_base = os.path.join(SPIDER_DATA, "database")
    available_dbs = set()
    if os.path.exists(db_base):
        for db_id in os.listdir(db_base):
            sqlite_path = os.path.join(db_base, db_id, f"{db_id}.sqlite")
            if os.path.exists(sqlite_path):
                available_dbs.add(db_id)
    print(f"Found {len(available_dbs)} databases with SQLite files")

    with open(train_path) as f:
        data = json.load(f)

    classified = []
    for item in data:
        db_id = item["db_id"]
        if db_id not in available_dbs:
            continue
        sql = item.get("query", "")
        classified.append({
            "db_id": db_id,
            "question": item["question"],
            "expected_sql": sql,
            "difficulty": classify_difficulty(sql),
            "required_complexity": count_complexity(sql),
        })

    easy_med = [q for q in classified if q["difficulty"] in ("easy", "medium")]
    hard_extra = [q for q in classified if q["difficulty"] in ("hard", "extra")]
    print(f"Classified: {len(easy_med)} easy/med, {len(hard_extra)} hard/extra")

    rng = random.Random(SEED)
    rng.shuffle(easy_med)
    rng.shuffle(hard_extra)
    selected = easy_med[:TARGET_EASY_MED] + hard_extra[:TARGET_HARD_EXTRA]
    for i, item in enumerate(selected):
        item["id"] = f"spider_{i + 1:03d}"

    OUTPUT_DBS.mkdir(parents=True, exist_ok=True)
    copied_dbs: set[str] = set()
    for item in selected:
        db_id = item["db_id"]
        if db_id not in copied_dbs:
            src = os.path.join(db_base, db_id, f"{db_id}.sqlite")
            dst = OUTPUT_DBS / f"{db_id}.sqlite"
            shutil.copy(src, str(dst))
            copied_dbs.add(db_id)

    with open(OUTPUT_JSON, "w") as f:
        json.dump(selected, f, indent=2)

    print(f"Wrote {len(selected)} questions → {OUTPUT_JSON}")
    print(f"Copied {len(copied_dbs)} databases → {OUTPUT_DBS}/")
    print("\nFirst 3 entries:")
    for item in selected[:3]:
        print(f"  [{item['id']}] [{item['difficulty']}] {item['question'][:70]}")


if __name__ == "__main__":
    main()
