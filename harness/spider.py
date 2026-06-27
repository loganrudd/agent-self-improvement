"""Load a Spider subset: schemas, difficulty-pooled questions, gold SQL, SQLite DBs.

TODO(Rohan):
- Download/point to Spider; pick a handful of db_ids (schemas).
- Build question pools: ~30-50 easy/medium, ~30-50 hard/extra (use Spider's difficulty labels).
- Expose: questions_by_difficulty(), gold_sql(question_id), db_path(db_id), schema_text(db_id).
"""
