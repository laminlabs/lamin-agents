# Agentic usescases with LaminDB

This repo hosts documentation and tests for agentic usecases in the `docs/` and `tests/` folders.

The tests for end-to-end tasks are in `tests/tasks/` and also run as benchmarks for token efficiency.

The repo also contains a custom harness `laminagent` in a separate folder.

Tracked Claude task runs also save token totals and duration in `LaminAgentEvals`, linked to the session Run. Totals include parent and subagent work across models. Prompt totals include cache reads and cache creation; the raw usage breakdown is kept in `Run.extra_data`. Claude’s API-call count is left unset because its turn count is not an equivalent metric.
