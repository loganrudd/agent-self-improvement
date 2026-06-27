"""Teacher model generates corrected SQL for failing questions (stronger Gemini tier).

TODO(Mihir):
- correct(question, schema_text, failure_mode) -> correct_sql
- this is a TEACHER (used to produce training examples), NOT a permanent model swap.
"""
