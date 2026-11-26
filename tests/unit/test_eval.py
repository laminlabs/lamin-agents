import itertools
import json
import subprocess

import lamindb as ln
import pytest
from laminagent._eval import log_usage_record, normalize_claude_usage


@pytest.mark.parametrize(
    "model_count,cache_read,cache_creation,zero",
    [(None, 0, 0, False), (1, 0, 0, True)]
    + [
        (count, read, create, False)
        for count, read, create in itertools.product([1, 2], [0, 7], [0, 11])
    ],
)
def test_log_usage_record_writes_record(
    monkeypatch, model_count, cache_read, cache_creation, zero
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

    expected_prompt, expected_output = 2, 3
    if model_count is not None:
        models = {
            f"model-{i}": {
                "inputTokens": 0 if zero else 2 * i,
                "outputTokens": 0 if zero else 3 * i,
                "cacheReadInputTokens": cache_read * i,
                "cacheCreationInputTokens": cache_creation * i,
            }
            for i in range(1, model_count + 1)
        }
        factor = 1 if model_count == 1 else 3
        expected_prompt = 0 if zero else (2 + cache_read + cache_creation) * factor
        expected_output = 0 if zero else 3 * factor
        usage = normalize_claude_usage(
            {
                "modelUsage": models,
                "usage": {"input_tokens": 2, "output_tokens": 3},
                "num_turns": 99,
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
        == expected_prompt + expected_output
    )
    assert FakeRecord.payload["features"]["n_output_tokens"] == expected_output
    assert FakeRecord.payload["features"]["n_call_count"] == (
        None if model_count is not None else 1
    )


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


@pytest.mark.parametrize("result", [{}, {"modelUsage": {"model": {}}}])
def test_claude_usage_requires_reported_token_counts(result):
    with pytest.raises(KeyError):
        normalize_claude_usage(result)


@pytest.mark.parametrize(
    "install_skill,nested", [(False, True), (True, False), (True, True)]
)
def test_claude_runner_records_usage_for_its_session(
    setup_lamindb, tmp_path, monkeypatch, install_skill, nested
):
    from lamin_cli.agents._common import persistent_run_uid_file
    from lamin_cli.agents.claude import _run_uid_file
    from laminagent._setup import ensure_task

    from tasks import testutils

    task = ensure_task("claude_eval")
    transform = ln.Transform(key="claude-eval-test", kind="function").save()
    run = ln.Run(transform)
    run.branch = ln.Branch(name=f"claude-eval-{install_skill}-{nested}").save()
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
        "modelUsage": {
            "parent-model": {
                "inputTokens": 2,
                "outputTokens": 3,
                "cacheReadInputTokens": 7,
                "cacheCreationInputTokens": 11,
            },
            "child-model": {
                "inputTokens": 5,
                "outputTokens": 13,
                "cacheReadInputTokens": 17,
                "cacheCreationInputTokens": 19,
            },
        },
    }
    run_dir = tmp_path / "task" if nested else tmp_path
    run_dir.mkdir(exist_ok=True)
    ln.setup.settings.dev_dir = tmp_path
    # Create state through the CLI owner in the agent's actual working directory.
    with monkeypatch.context() as agent_context:
        agent_context.chdir(run_dir)
        marker = _run_uid_file(payload["session_id"])
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
        testutils.run_claudecode(run_dir, "review", install_skill=install_skill)
        is result
    )
    evaluation = ln.Record.filter(type=task, run=run, branch=None).one_or_none()
    if install_skill:
        assert evaluation is not None
        values = evaluation.features.get_values()
        assert values["duration_in_sec"] == 1.25
        assert values["n_prompt_tokens"] == 61
        assert values["n_output_tokens"] == 16
        assert values["n_total_tokens"] == 77
        assert values.get("n_call_count") is None
        run.refresh_from_db()
        assert run.extra_data["existing"] == "preserved"
        assert run.extra_data["claude_usage"] == payload["usage"]
        assert run.extra_data["claude_model_usage"] == payload["modelUsage"]
        assert run.features.get_values()["n_total_tokens"] == 77
    else:
        assert evaluation is None
        run.refresh_from_db()
        assert run.extra_data == {"existing": "preserved"}
        assert not run.features.get_values()
    unrelated_run.refresh_from_db()
    assert not unrelated_run.extra_data
    assert not unrelated_run.features.get_values()
    assert not ln.Record.filter(type=task, run=unrelated_run).exists()
