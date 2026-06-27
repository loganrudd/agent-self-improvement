"""Text-to-SQL agent (Gemini, weaker tier as base).

KEY: the prompt MUST include config.few_shot_examples — that growing list is how the
agent recovers after correction feeds it learned examples.

TODO(Rohan):
- generate_sql(question, schema_text, config) -> (sql, tokens, latency_ms)
- inject config.few_shot_examples into the prompt.
"""
from contracts.schemas import AgentConfig  # noqa: F401
