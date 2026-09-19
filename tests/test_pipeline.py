"""
tests.test_pipeline — Verify pipeline execution engine

Planned tests:
- test_run_single_block         — run pipeline with one block, check Job status
- test_run_multi_block          — chain multiple blocks, verify context threading
- test_failing_block_marks_job  — a broken block results in FAILED job
- test_step_ordering            — steps are recorded in correct order
"""

# TODO: implement once DB test fixtures are in place
