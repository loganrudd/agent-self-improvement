"""The change-point stratified feed (rules/02-tech-decisions.md).

Phase 1 baseline: sample easy/medium.   --change-point-->
Phase 2 degraded: sample hard/extra.
Phase 3 recovery: keep sampling the SAME hard/extra pool (acc recovers via learned few-shots).

Support a fast REPLAY mode for the demo (pre-computed records) + a live change-point trigger.

TODO(Rohan): stream(n_per_phase, change_point_trigger) -> yields (question, difficulty, gold).
"""
