import itertools
import json
import subprocess

import lamindb as ln
import pytest
from laminagent._eval import log_usage_record, normalize_claude_usage


@pytest.mark.parametrize(
    "claude,cache_read,cache_creation,zero",
    [(False, False, False, False)]
    + [
        (True, read, create, zero)
        for read, create, zero in itertools.product([False, True], repeat=3)
    ],
)
def test_log_usage_record_writes_record(
    monkeypatch, claude, cache_read, cache_creation, zero
) -> None:
    run = object()

    class FakeTask:
        schema_id = 1

    class FakeRecord:
        payload = None

        def __init__(self, *, features, type):
            self.features = features
            self.type = type

        def save(self):
            FakeRecord.payload = {
                "features": self.features,
                "type": self.type,
                "run": self.run,
            }
            return self

    monkeypatch.setattr("laminagent._eval.get_task", lambda **_kwargs: FakeTask())
    monkeypatch.setattr("laminagent._eval.ln.Record", FakeRecord)
    monkeypatch.setattr("laminagent._eval.current_package_version", lambda: "0.1.0")
    monkeypatch.setattr("laminagent._eval._current_commit_hash16", lambda: "abc123")
    monkeypatch.setattr("laminagent._eval._current_runner_env", lambda: "github_hosted")

    input_tokens, output_tokens = (0, 0) if zero else (2, 3)
    expected_prompt = input_tokens
    if claude:
        raw_usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
        if cache_read:
            raw_usage["cache_read_input_tokens"] = 0 if zero else 7
            expected_prompt += 0 if zero else 7
        if cache_creation:
            raw_usage["cache_creation_input_tokens"] = 0 if zero else 11
            expected_prompt += 0 if zero else 11
        usage = normalize_claude_usage(
            {
                "usage": raw_usage,
                "num_turns": 99,
                "modelUsage": {"another-model": {"inputTokens": 999}},
            }
        )
    else:
        usage = {
            "n_call_count": 1,
            "n_prompt_tokens": 2,
            "n_output_tokens": 3,
            "n_total_tokens": 5,
        }
    log_usage_record(
        usage,
        run=run,
        duration_in_sec=0.2,
        task_name="tool.py",
    )

    assert FakeRecord.payload is not None
    assert FakeRecord.payload["type"].schema_id == 1
    assert FakeRecord.payload["features"]["package_version"] == "0.1.0"
    assert FakeRecord.payload["run"] is run
    assert FakeRecord.payload["features"]["n_prompt_tokens"] == expected_prompt
    assert (
        FakeRecord.payload["features"]["n_total_tokens"]
        == expected_prompt + output_tokens
    )
    assert FakeRecord.payload["features"]["n_output_tokens"] == output_tokens
    assert FakeRecord.payload["features"]["n_call_count"] == (None if claude else 1)


def test_log_usage_record_skips_when_task_not_configured(monkeypatch) -> None:
    monkeypatch.setattr("laminagent._eval.get_task", lambda **_kwargs: None)

    run = object()
    log_usage_record(
        {
            "n_call_count": 1,
            "n_prompt_tokens": 2,
            "n_output_tokens": 3,
            "n_total_tokens": 5,
        },
        run=run,
        duration_in_sec=0.2,
        task_name="tool",
    )


def test_record_usage_task_name_prefers_pytest_task_context(monkeypatch) -> None:
    from laminagent._eval import record_usage_task_name

    monkeypatch.setenv(
        "PYTEST_CURRENT_TEST",
        "tests/tasks/test_01_create_fasta_for_favorite_protein.py::test_create_favorite_protein_sequence (call)",
    )

    task_name = record_usage_task_name("save_protein.py")
    assert task_name == "01_create_fasta_for_favorite_protein"


def test_record_usage_task_name_falls_back_to_generated_script(monkeypatch) -> None:
    from laminagent._eval import record_usage_task_name

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    task_name = record_usage_task_name("save_protein.py")
    assert task_name == "save_protein"


@pytest.mark.parametrize("usage", [{}, {"input_tokens": 2}])
def test_claude_usage_requires_reported_token_counts(usage):
    with pytest.raises(KeyError):
        normalize_claude_usage({"usage": usage})


@pytest.mark.parametrize("install_skill", [False, True])
def test_claude_runner_records_usage_for_its_session(
    setup_lamindb, tmp_path, monkeypatch, install_skill
):
    from lamin_cli.agents._common import persistent_run_uid_file
    from laminagent._setup import ensure_task

    from tasks import testutils

    task = ensure_task("claude_eval")
    transform = ln.Transform(key="claude-eval-test", kind="function").save()
    run = ln.Run(transform)
    run.branch = ln.Branch(name=f"claude-eval-{install_skill}").save()
    run.extra_data = {"existing": "preserved"}
    run.save()
    unrelated_run = ln.Run(transform).save()
    payload = {
        "session_id": "test-session",
        "duration_ms": 1250,
        "num_turns": 2,
        "usage": {
            "input_tokens": 2,
            "output_tokens": 3,
            "cache_read_input_tokens": 7,
            "cache_creation_input_tokens": 11,
        },
        "modelUsage": {"test-model": {"inputTokens": 2}},
    }
    marker = tmp_path / ".claude" / ".lamindb_run_uid_test-session"
    marker.parent.mkdir()
    persistent_run_uid_file(marker, ln).write_text(run.uid)
    result = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv(
        "PYTEST_CURRENT_TEST", "tests/tasks/test_claude_eval.py::test_review (call)"
    )
    monkeypatch.setattr(testutils, "load_dotenv", lambda **_: None)
    monkeypatch.setattr(testutils, "_install_lamindb_skill", lambda *_: None)
    monkeypatch.setattr(testutils, "_run_cli", lambda *_, **__: result)

    assert (
        testutils.run_claudecode(tmp_path, "review", install_skill=install_skill)
        is result
    )
    evaluation = ln.Record.filter(type=task, run=run, branch=None).one_or_none()
    if install_skill:
        assert evaluation is not None
        values = evaluation.features.get_values()
        assert values["duration_in_sec"] == 1.25
        assert values["n_prompt_tokens"] == 20
        assert values["n_output_tokens"] == 3
        assert values["n_total_tokens"] == 23
        assert values.get("n_call_count") is None
        run.refresh_from_db()
        assert run.extra_data["existing"] == "preserved"
        assert run.extra_data["claude_usage"] == payload["usage"]
        assert run.extra_data["claude_model_usage"] == payload["modelUsage"]
        assert run.features.get_values()["n_total_tokens"] == 23
    else:
        assert evaluation is None
        run.refresh_from_db()
        assert run.extra_data == {"existing": "preserved"}
        assert not run.features.get_values()
    unrelated_run.refresh_from_db()
    assert not unrelated_run.extra_data
    assert not unrelated_run.features.get_values()
    assert not ln.Record.filter(type=task, run=unrelated_run).exists()
