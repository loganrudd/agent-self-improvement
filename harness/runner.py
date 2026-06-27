"""Drive the loop's source: for each feed item, run the agent, eval, emit TelemetryRecord.

TODO(Rohan):
- for question in feed.stream(...):
    sql, tokens, latency = agent.generate_sql(question, schema, config)
    acc, valid, gen_cx, req_cx = evaluator.eval(sql, gold, db)
    rec = TelemetryRecord(...)
    append_event(rec)
- read the latest AgentConfig (with few_shot_examples) so corrections take effect.
"""
from contracts.schemas import TelemetryRecord  # noqa: F401
from contracts.eventlog import append_event    # noqa: F401

if __name__ == "__main__":
    raise SystemExit("TODO(Rohan): implement the harness runner")
