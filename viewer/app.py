"""Thin live viewer: recovery curve + channels + SQL example panel.

NOT the product, NOT Streamlit (see rules/03-compliance.md). Keep minimal.

TODO(Yiwen):
- FastAPI app; an endpoint that reads events.jsonl (read_events / tail_events) and returns
  the series the chart needs (windowed accuracy overall + per difficulty, drift/correction marks,
  latest example: question/generated_sql/result).
- one HTML page with Chart.js polling that endpoint.
- build against fixtures/mock_events.jsonl.
"""
from contracts.eventlog import read_events, tail_events  # noqa: F401

# from fastapi import FastAPI
# app = FastAPI()
