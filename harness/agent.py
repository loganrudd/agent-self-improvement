"""Text-to-SQL agent (MiniMax, weaker tier as base).

KEY: the prompt MUST include config.few_shot_examples — that growing list is how the
agent recovers after correction feeds it learned examples.

- generate_sql(question, schema_text, config) -> (sql, tokens, latency_ms)

Models:
  base agent  -> MiniMax-M2.7-highspeed  (fast; genuinely struggles on hard SQL)
  teacher     -> MiniMax-M3              (Mihir's correction stage uses this)
"""
from __future__ import annotations

import os
import re
import time

from openai import OpenAI

from contracts.schemas import AgentConfig, FewShotExample

_MINIMAX_BASE_URL = "https://api.minimax.io/v1"

_client: OpenAI | None = None


class MissingCredentialsError(RuntimeError):
    """Raised when MINIMAX_API_KEY is not set — fail fast instead of emitting
    error-SQL telemetry that silently pollutes the drift stream."""


def require_api_key() -> None:
    """Call at startup so a missing key stops the run loudly, before any
    telemetry is written. Without this, every run becomes a fake '-- error'
    record that the evaluator can score as valid/correct by accident."""
    if not os.environ.get("MINIMAX_API_KEY"):
        raise MissingCredentialsError(
            "MINIMAX_API_KEY is not set. Export it in THIS shell before running:\n"
            "    export MINIMAX_API_KEY=sk-...\n"
            "then re-run. (A key set in another terminal does not carry over.)"
        )


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        require_api_key()
        _client = OpenAI(
            api_key=os.environ["MINIMAX_API_KEY"],
            base_url=_MINIMAX_BASE_URL,
        )
    return _client


_SYSTEM = (
    "You are a SQL expert. Given a database schema and a question, "
    "write a single valid SQL SELECT statement. "
    "Return ONLY the SQL, no markdown fences, no explanation."
)


def _build_prompt(question: str, schema: str, examples: list[FewShotExample]) -> str:
    parts = [f"Schema:\n{schema}"]
    if examples:
        shots = "\n\n".join(f"Q: {e.question}\nSQL: {e.correct_sql}" for e in examples[:16])
        parts.append(f"Few-shot examples:\n{shots}")
    parts.append(f"Question: {question}\nSQL:")
    return "\n\n".join(parts)


def _strip_think(text: str) -> str:
    """Remove <think>...</think> reasoning blocks (MiniMax M-series reasoning models).
    Also handles unclosed <think> (model cut off mid-reasoning — no valid SQL follows).
    """
    # closed block: strip the think section, keep what follows
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # unclosed block: model was cut off mid-think — strip from <think> to end
    text = re.sub(r"<think>.*", "", text, flags=re.DOTALL).strip()
    return text


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else text


def _clean_response(text: str) -> str:
    return _strip_fences(_strip_think(text))


def _extract_think(text: str) -> str:
    """Return the raw content of the first <think>...</think> block, or empty string."""
    m = re.search(r"<think>(.*?)</think>", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def generate_sql(
    question: str,
    schema: str,
    config: AgentConfig,
) -> tuple[str, int, float, str]:
    """Returns (sql, tokens, latency_ms, reasoning). sql may be an error comment on failure."""
    t0 = time.time()
    try:
        client = _get_client()
        prompt = _build_prompt(question, schema, config.few_shot_examples)
        response = client.chat.completions.create(
            model=config.model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
        )
        raw = response.choices[0].message.content or ""
        reasoning = _extract_think(raw)
        sql = _clean_response(raw)
        tokens = response.usage.total_tokens if response.usage else 0
    except Exception as e:
        sql = f"-- error: {e}"
        reasoning = ""
        tokens = 0
    latency_ms = (time.time() - t0) * 1000
    return sql, tokens, latency_ms, reasoning
