"""Build & anchor FewShotExamples from failures (continual-learning / memory).

TODO(Mihir):
- make_examples(failing_cases, teacher) -> list[FewShotExample]
- anchor: retain good easy-query examples so hard-query injection doesn't regress easy bucket
  (the DER++/anti-forgetting angle). Minimal-but-real first.
"""
from contracts.schemas import FewShotExample  # noqa: F401
