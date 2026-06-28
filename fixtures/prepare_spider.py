"""
Pull a Spider subset for the demo. Run once from repo root:

    python fixtures/prepare_spider.py

Prereq: the FULL Spider dataset (questions + SQLite DBs), not just the GitHub code repo.
  - Official zip: https://yale-lily.github.io/spider  → unzip to /tmp/spider_data
  - Or set SPIDER_DATA to wherever you extracted it.

The taoyds/spider git clone alone is NOT enough — it has no database/ folder.

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

# Top 8 Spider dev DBs by hard+extra question count (car_1=40, world_1=36, ...).
# Used by --dev-concentrated to guarantee deep same-DB clusters for few-shot learning.
_CONCENTRATED_DBS = [
    "car_1", "world_1", "dog_kennels", "network_1",
    "student_transcripts_tracking", "flight_2", "cre_Doc_Template_Mgt", "pets_1",
]


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


def _train_candidates(root: str) -> list[str]:
    return [
        os.path.join(root, "train_spider.json"),
        os.path.join(root, "evaluation_examples", "examples", "train_spider.json"),
    ]


def _db_base_candidates(root: str) -> list[str]:
    return [
        os.path.join(root, "database"),
        os.path.join(root, "spider_data", "database"),
    ]


def resolve_train_path(root: str) -> str | None:
    for path in _train_candidates(root):
        if os.path.exists(path):
            return path
    return None


def resolve_db_base(root: str) -> str | None:
    for path in _db_base_candidates(root):
        if not os.path.isdir(path):
            continue
        for db_id in os.listdir(path):
            if os.path.exists(os.path.join(path, db_id, f"{db_id}.sqlite")):
                return path
    return None


def list_available_dbs(db_base: str) -> set[str]:
    available: set[str] = set()
    for db_id in os.listdir(db_base):
        if os.path.exists(os.path.join(db_base, db_id, f"{db_id}.sqlite")):
            available.add(db_id)
    return available


def _die_missing_data(root: str) -> None:
    print(f"Spider dataset not ready under SPIDER_DATA={root!r}\n")
    train_path = resolve_train_path(root)
    db_base = resolve_db_base(root)

    if train_path:
        print(f"  Found questions: {train_path}")
    else:
        print("  Missing: train_spider.json")
        print("    Checked:")
        for path in _train_candidates(root):
            print(f"      - {path}")

    if db_base:
        print(f"  Found databases: {db_base} ({len(list_available_dbs(db_base))} schemas)")
    else:
        print("  Missing: database/<db_id>/<db_id>.sqlite files")
        print("    The GitHub code repo does NOT include SQLite databases.")
        print("    Download the full dataset zip and unzip it:\n")
        print("      1. https://yale-lily.github.io/spider  (official Spider Dataset link)")
        print("      2. Unzip so you have:")
        print(f"           {root}/train_spider.json")
        print(f"           {root}/database/concert_singer/concert_singer.sqlite")
        print("         Or set SPIDER_DATA to your extract path.\n")
        print("      Alt (HF re-host with zip + DBs):")
        print("        pip install huggingface_hub")
        print("        huggingface-cli download HAL-9001/spider-databases spider_data.zip --local-dir /tmp/spider_hf")
        print("        unzip /tmp/spider_hf/spider_data.zip -d /tmp/")
        print("        SPIDER_DATA=/tmp/spider_data python fixtures/prepare_spider.py")

    sys.exit(1)


def main() -> None:
    root = SPIDER_DATA
    train_path = resolve_train_path(root)
    db_base = resolve_db_base(root)

    if not train_path or not db_base:
        _die_missing_data(root)

    available_dbs = list_available_dbs(db_base)
    print(f"Using train data: {train_path}")
    print(f"Using databases:  {db_base}")
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

    if not classified:
        print("No questions matched available databases — check SPIDER_DATA layout.")
        sys.exit(1)

    easy_med = [q for q in classified if q["difficulty"] in ("easy", "medium")]
    hard_extra = [q for q in classified if q["difficulty"] in ("hard", "extra")]
    print(f"Classified: {len(easy_med)} easy/med, {len(hard_extra)} hard/extra")

    if len(easy_med) < TARGET_EASY_MED or len(hard_extra) < TARGET_HARD_EXTRA:
        print(
            f"Warning: wanted {TARGET_EASY_MED}+{TARGET_HARD_EXTRA} questions, "
            f"have {len(easy_med)}+{len(hard_extra)} — using all available."
        )

    rng = random.Random(SEED)
    rng.shuffle(easy_med)
    rng.shuffle(hard_extra)
    selected = easy_med[:TARGET_EASY_MED] + hard_extra[:TARGET_HARD_EXTRA]
    if not selected:
        print("No questions selected — cannot build subset.")
        sys.exit(1)
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


def main_dev_concentrated() -> None:
    """Rebuild spider_subset.json from dev.json, restricted to the 8 highest hard/extra-density
    databases. This gives 200+ hard/extra questions in deep same-DB clusters (10-30 siblings each),
    so the same-DB leave-one-out split can always find a relevant few-shot example.
    """
    dev_path = Path("spider_data/dev.json")
    db_base = Path("spider_data/database")

    if not dev_path.exists():
        print(f"ERROR: {dev_path} not found — run from repo root with spider_data/ present.")
        sys.exit(1)

    with open(dev_path) as f:
        data = json.load(f)

    focus = set(_CONCENTRATED_DBS)
    classified = []
    for item in data:
        db_id = item["db_id"]
        if db_id not in focus:
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
    print(f"Concentrated subset: {len(easy_med)} easy/med, {len(hard_extra)} hard/extra "
          f"across {len(focus)} DBs")

    rng = random.Random(SEED)
    rng.shuffle(easy_med)
    rng.shuffle(hard_extra)
    selected = easy_med + hard_extra  # take all — deep clusters are the point

    for i, item in enumerate(selected):
        item["id"] = f"spider_{i + 1:03d}"

    OUTPUT_DBS.mkdir(parents=True, exist_ok=True)
    copied_dbs: set[str] = set()
    for item in selected:
        db_id = item["db_id"]
        if db_id not in copied_dbs:
            src = db_base / db_id / f"{db_id}.sqlite"
            if not src.exists():
                print(f"  SKIP {db_id}: SQLite not found at {src}")
                continue
            shutil.copy(str(src), str(OUTPUT_DBS / f"{db_id}.sqlite"))
            copied_dbs.add(db_id)

    with open(OUTPUT_JSON, "w") as f:
        json.dump(selected, f, indent=2)

    print(f"Wrote {len(selected)} questions → {OUTPUT_JSON}")
    print(f"Copied {len(copied_dbs)} databases → {OUTPUT_DBS}/")
    from collections import Counter
    by_db = Counter(q["db_id"] for q in hard_extra)
    print("\nhard/extra per DB:")
    for db, c in sorted(by_db.items(), key=lambda kv: -kv[1]):
        print(f"  {c:>4}  {db}")


if __name__ == "__main__":
    if "--dev-concentrated" in sys.argv:
        main_dev_concentrated()
    else:
        main()
