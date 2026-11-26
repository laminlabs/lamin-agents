# Agentic usescases with LaminDB

This repo hosts documentation and tests for agentic usecases in the `docs/` and `tests/` folders.

The tests for end-to-end tasks are in `tests/tasks/` and also run as benchmarks for token efficiency.

The repo also contains a custom harness `laminagent` in a separate folder.

Tracked Claude task runs also save token totals and duration in `LaminAgentEvals`, linked to the session Run. Totals include parent and subagent work across models. Prompt totals include cache reads and cache creation; the raw usage breakdown is kept in `Run.extra_data`. Claude’s API-call count is left unset because its turn count is not an equivalent metric.

The trace-review benchmark (`tests/tasks/test_06_review_agent_trace.py`) runs two independent Claude sessions against the same frozen Codex trace and code diff: JSONL versus lossless Markdown. Both use the same prompt, output schema, and CLI model configuration. It checks the cutoff change, retained cells, executed validation, and gaps visible in the trace, with exact evidence quotations. Each evaluation links the source inputs, reviewer report, structured answer, and correctness results.

The source artifacts are pinned by UID and SHA-256; the Markdown preserves every normalized block, including tool inputs and results. Token totals measure the whole reviewer session, including tracking and answering, rather than just file reading. One pair is a smoke benchmark, not evidence of a statistically reliable winner. Existing CI runs it automatically: pull requests use a temporary instance; pushes to main persist the results in `laminlabs/lamindata`.
