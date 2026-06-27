"""Execution-based eval + complexity features.

TODO(Rohan):
- execute(sql, db_path) -> rows | error
- execution_accuracy: compare generated rows vs gold rows AS SETS (normalize order/dupes);
  reuse Spider's eval to avoid false mismatches.
- query_valid: did generated SQL execute without error.
- complexity(sql) -> int (count joins + nesting) for generated_complexity & required_complexity.
"""
