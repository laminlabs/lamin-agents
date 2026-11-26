from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import lamindb as ln
import pytest
from click.testing import CliRunner
from laminagent._eval import log_usage_record, normalize_claude_usage
from laminagent._lag import (
    _extract_runnable_keys_from_prompt,
    _format_progress_message_for_log,
    _parse_generated_paths,
    _print_generated_tool_contents,
    _redact_payload,
    _resolve_prompt_runnable_paths,
    _set_current_project_env,
    _warn_if_missing_project,
    lag,
    run_agent_authoring,
)


@pytest.fixture(autouse=True)
def _clear_lamin_current_project(monkeypatch) -> None:
    monkeypatch.delenv("LAMIN_CURRENT_PROJECT", raising=False)


@pytest.fixture(autouse=True)
def _stub_setup(monkeypatch) -> None:
    monkeypatch.setattr("laminagent._lag.setup", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def _bypass_prompt_flow_wrapper(monkeypatch) -> None:
    callback = lag.commands["prompt"].callback
    unwrapped_callback = callback
    while hasattr(unwrapped_callback, "__wrapped__"):
        unwrapped_callback = unwrapped_callback.__wrapped__

    def _callback_without_flow(*args, **kwargs):
        return unwrapped_callback(*args, **kwargs)

    monkeypatch.setattr(lag.commands["prompt"], "callback", _callback_without_flow)

    from laminagent import _lag as lag_module

    flow_callback = lag_module.lamin_executable_prompt
    while hasattr(flow_callback, "__wrapped__"):
        flow_callback = flow_callback.__wrapped__
    monkeypatch.setattr(lag_module, "lamin_executable_prompt", flow_callback)


def test_parse_generated_paths_filters_empty_entries(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    paths = _parse_generated_paths(f"{a},,{b},")
    assert paths == [a.resolve(), b.resolve()]


def test_print_generated_tool_contents_prints_each_file_once(
    tmp_path: Path, capsys
) -> None:
    a = tmp_path / "a.py"
    a.write_text("print('a')\n", encoding="utf-8")
    b = tmp_path / "b.py"
    b.write_text("print('b')\n", encoding="utf-8")

    _print_generated_tool_contents([a, b, a])
    output = capsys.readouterr().out

    assert output.count("[Generated Tool ") == 2
    assert "print('a')" in output
    assert "print('b')" in output


def test_set_current_project_env_sets_current_project(monkeypatch) -> None:
    monkeypatch.delenv("LAMIN_CURRENT_PROJECT", raising=False)
    project = _set_current_project_env("demo-project")
    assert project == "demo-project"
    assert os.environ["LAMIN_CURRENT_PROJECT"] == "demo-project"


def test_warn_if_missing_project_logs_warning(monkeypatch) -> None:
    calls: list[str] = []

    def _fake_warning(message: str) -> None:
        calls.append(message)

    monkeypatch.setattr("laminagent._lag.logger.warning", _fake_warning)
    _warn_if_missing_project(None)
    assert len(calls) == 1


def test_extract_runnable_keys_from_prompt_deduplicates() -> None:
    prompt = "rerun test-lag/create_fasta.py and test-lag/create_fasta.py plus x.py"
    keys = _extract_runnable_keys_from_prompt(prompt)
    assert keys == ["test-lag/create_fasta.py", "x.py"]


def test_resolve_prompt_runnable_paths_returns_empty_without_keys() -> None:
    assert _resolve_prompt_runnable_paths("please rerun the tool") == []


def test_lag_setup_routes_to_setup_handler(monkeypatch) -> None:
    called: dict[str, Path | None] = {}

    def _fake_setup(script: Path | None) -> None:
        called["script"] = script

    monkeypatch.setattr("laminagent._lag.setup", _fake_setup)
    runner = CliRunner()
    result = runner.invoke(lag, ["setup"])

    assert result.exit_code == 0
    assert called["script"] is None


def test_lag_setup_accepts_script_argument(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "tests" / "tasks" / "test_01.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('ok')\n", encoding="utf-8")
    called: dict[str, Path | None] = {}

    def _fake_setup(*, script: Path | None = None, **_kwargs) -> None:
        called["script"] = script

    monkeypatch.setattr("laminagent._lag.setup", _fake_setup)
    runner = CliRunner()
    result = runner.invoke(lag, ["setup", str(script)])

    assert result.exit_code == 0
    assert called["script"] == script


def test_lag_prompt_requires_prompt_argument() -> None:
    runner = CliRunner()
    result = runner.invoke(lag, ["prompt"])
    assert result.exit_code != 0
    assert "Missing argument 'PROMPT'" in result.output


@pytest.mark.parametrize(
    "keys",
    [
        ["test-lag/create_fasta.py"],
        ["missing.py"],
        ["test-lag/create_fasta.py", "missing.py"],
    ],
)
def test_lag_default_mode_executes_only_local_prompt_paths(
    monkeypatch, tmp_path: Path, keys: list[str]
) -> None:
    from laminagent import _lag as lag_module

    monkeypatch.chdir(tmp_path)
    local = tmp_path / "test-lag/create_fasta.py"
    local.parent.mkdir()
    local.write_text("print('local')\n")
    before = set(tmp_path.rglob("*"))
    remote_lookup = Mock(side_effect=AssertionError("Must not fetch remote code"))
    for registry in ("Transform", "Artifact"):
        monkeypatch.setattr(
            lag_module.ln, registry, SimpleNamespace(filter=remote_lookup)
        )
    monkeypatch.setattr(lag_module.ln, "context", SimpleNamespace(run=None))
    execute = Mock(return_value={"final_text": "done"})
    monkeypatch.setattr(lag_module, "execute_runnable_paths", execute)
    prompt = "run " + " and ".join(keys)
    runner = CliRunner()
    result = runner.invoke(lag, ["prompt", prompt])

    remote_lookup.assert_not_called()
    assert set(tmp_path.rglob("*")) == before
    if "missing.py" in keys:
        assert result.exit_code == 1
        assert "Download and review it before running" in result.output
        execute.assert_not_called()
    else:
        assert result.exit_code == 0, result.output
        execute.assert_called_once()
        assert execute.call_args.kwargs["runnable_paths"] == [local.resolve()]
        assert execute.call_args.kwargs["prompt"] == prompt
        clean_output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "[Notes]" in clean_output
        assert "done" in clean_output


def test_lag_auto_authoring_runs_without_verbose_option(monkeypatch) -> None:
    def _fake_run_agent_authoring(**kwargs):
        return {
            "run_uid": "run-1",
            "generated_path": None,
            "generated_paths": "",
            "final_text": "ok",
            "llm_usage": {
                "n_call_count": 1,
                "n_prompt_tokens": 1,
                "n_output_tokens": 1,
                "n_total_tokens": 2,
            },
            "duration_in_sec": 0.1,
            "trace_events": [],
        }

    monkeypatch.setattr("laminagent._lag.find_tool_file", lambda: None)
    monkeypatch.setattr(
        "laminagent._lag.run_agent_authoring", _fake_run_agent_authoring
    )
    monkeypatch.setattr(
        "laminagent._lag._log_gemini_usage_to_run_features", lambda *_: None
    )
    monkeypatch.setattr("laminagent._lag.log_usage_record", lambda *_, **__: None)
    runner = CliRunner()
    result = runner.invoke(lag, ["prompt", "build tool"])

    assert result.exit_code == 0


def test_run_agent_authoring_uses_tmp_cwd_on_lambda(monkeypatch) -> None:
    original_cwd = Path.cwd()
    tmp_realpath = Path(tempfile.gettempdir()).resolve()
    call_cwd: dict[str, str] = {}

    def _fake_run_agent(**_kwargs):
        call_cwd["value"] = str(Path.cwd().resolve())
        return {
            "generated_file": "generated.py",
            "generated_files": ["generated.py"],
            "resolved_runnable_path": "",
            "final_text": "ok",
            "llm_usage": {
                "n_call_count": 0,
                "n_prompt_tokens": 0,
                "n_output_tokens": 0,
                "n_total_tokens": 0,
            },
            "trace_events": [],
        }

    class _FakeTransform:
        saved_paths: list[str] = []

        def __init__(self, path: str) -> None:
            self.path = path

        def save(self):
            _FakeTransform.saved_paths.append(self.path)
            return self

    monkeypatch.setattr("laminagent._lag.load_dotenv", lambda **_kwargs: None)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "laminagent-test")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("laminagent._lag.run_agent", _fake_run_agent)
    monkeypatch.setattr(
        "laminagent._lag.ln.context",
        SimpleNamespace(run=None),
    )
    monkeypatch.setattr(
        "laminagent._lag.ln.Transform",
        SimpleNamespace(from_path=lambda path: _FakeTransform(path)),
    )

    outcome = run_agent_authoring(prompt="build tool", model="gemini-flash-latest")

    assert call_cwd["value"] == str(tmp_realpath)
    assert Path.cwd() == original_cwd
    assert isinstance(outcome["generated_path"], str)
    assert str(Path(str(outcome["generated_path"])).resolve()) == str(
        Path(tmp_realpath) / "generated.py"
    )
    assert _FakeTransform.saved_paths == [str(outcome["generated_path"])]


def test_lag_rejects_less_verbose_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(lag, ["prompt", "--less-verbose", "build tool"])

    assert result.exit_code != 0
    assert "No such option" in result.output


def test_lag_auto_executes_discovered_tool_file(monkeypatch, tmp_path: Path) -> None:
    tool_file = tmp_path / "tool.md"
    tool_file.write_text("- run `a.py`\n", encoding="utf-8")
    called: dict[str, str] = {}

    monkeypatch.setattr("laminagent._lag.find_tool_file", lambda: tool_file)

    def _fake_execute_the_tool(prompt: str, tool_file: Path) -> dict[str, str | list]:
        called["prompt"] = prompt
        called["tool"] = str(tool_file)
        return {
            "run_uid": "run-1",
            "tool_path": str(tool_file),
            "final_text": "done",
            "trace_events": [],
        }

    monkeypatch.setattr("laminagent._lag.execute_the_tool", _fake_execute_the_tool)
    runner = CliRunner()
    result = runner.invoke(lag, ["prompt", "please run latest tool"])

    assert result.exit_code == 0
    assert called["prompt"] == "please run latest tool"
    assert called["tool"] == str(tool_file)


def test_trace_is_logged_with_redaction(monkeypatch) -> None:
    logged: list[str] = []

    monkeypatch.setattr(
        "laminagent._lag.logger.info", lambda message: logged.append(message)
    )

    def _fake_execute(prompt: str) -> dict[str, object]:
        assert prompt == "run test-lag/create_fasta.py"
        return {
            "run_uid": "run-1",
            "resolved_paths": "",
            "final_text": "done",
            "trace_events": [
                {
                    "step": 1,
                    "event": "tool_call",
                    "tool": "x",
                    "tool_args": {"api_key": "super-secret"},
                }
            ],
        }

    monkeypatch.setattr("laminagent._lag.execute_existing_from_prompt", _fake_execute)
    runner = CliRunner()
    result = runner.invoke(lag, ["prompt", "run test-lag/create_fasta.py"])

    assert result.exit_code == 0
    assert logged
    assert "lag_trace_summary=" in logged[-1]
    assert "super-secret" not in "".join(logged)


def test_redact_payload_masks_known_secret_keys() -> None:
    payload = {"api_key": "abc", "nested": {"authorization": "Bearer token", "ok": 1}}
    redacted = _redact_payload(payload)
    assert isinstance(redacted, dict)
    assert redacted["api_key"] == "***REDACTED***"
    assert isinstance(redacted["nested"], dict)
    assert redacted["nested"]["authorization"] == "***REDACTED***"


def test_format_progress_message_for_log_summarizes_tool_payload() -> None:
    message = (
        'step 1: tool result payload={"message":"Found 5 LaminDB matches for '
        '\\"artifact\\".","results":[{"type":"artifact","key":"a"},{"type":"artifact","key":"b"}]}'
    )
    formatted = _format_progress_message_for_log(message)
    assert "step 1: tool result payload:" in formatted
    assert 'Found 5 LaminDB matches for "artifact".' in formatted
    assert "results: 2" in formatted


def test_format_progress_message_for_log_summarizes_tool_call_args() -> None:
    message = (
        "step 2: tool call -> read_skill_from_lamindb_instance "
        'args={"uid":"u5muNUOPnWPBuZ8z","instance_slug":"laminlabs/biomed-skills"}'
    )
    formatted = _format_progress_message_for_log(message)
    assert (
        formatted == "step 2: tool call -> read_skill_from_lamindb_instance "
        "(uid='u5muNUOPnWPBuZ8z', instance_slug='laminlabs/biomed-skills')"
    )


@pytest.mark.parametrize(
    "model_count,cache_read,cache_creation,zero",
    [
        (None, 0, 0, False),  # Gemini
        (1, 0, 0, True),  # Claude: reported zero usage
        (1, 0, 0, False),  # Uncached input and output
        (1, 7, 0, False),  # Cache reads without cache creation
        (2, 0, 11, False),  # Multiple models, cache creation without cache reads
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


@pytest.mark.parametrize(
    "current_test,expected",
    [
        (None, "save_protein"),
        (
            "tests/tasks/test_01_create_fasta_for_favorite_protein.py::test_create_favorite_protein_sequence (call)",
            "01_create_fasta_for_favorite_protein",
        ),
    ],
)
def test_record_usage_task_name(monkeypatch, current_test, expected) -> None:
    from laminagent._eval import record_usage_task_name

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    if current_test is not None:
        monkeypatch.setenv("PYTEST_CURRENT_TEST", current_test)
    assert record_usage_task_name("save_protein.py") == expected


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
    cli = Mock(return_value=result)
    monkeypatch.setattr(testutils, "_run_cli", cli)
    schema = {"type": "object"} if nested else None

    assert (
        testutils.run_claudecode(
            run_dir, "review", install_skill=install_skill, json_schema=schema
        )
        is result
    )
    command = cli.call_args.args[0]
    if schema is not None:
        assert json.loads(command[command.index("--json-schema") + 1]) == schema
    else:
        assert "--json-schema" not in command
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


def test_trace_markdown_preserves_blocks():
    from tests.tasks.test_06_review_agent_trace import _trace_markdown

    text = 'α\n```\n## Entry 999: user\n"quoted"\n\n' + "long " * 1000
    blocks = [
        {"type": "text", "text": text},
        {"type": "thinking", "thinking": text},
        {"type": "tool_use", "id": "call-1", "name": "exec", "input": {"cmd": text}},
        {"type": "tool_result", "tool_use_id": "call-1", "content": text},
    ]
    markdown = _trace_markdown(
        [
            {"role": "assistant", "content": blocks[:3]},
            {"role": "user", "content": blocks[3:]},
        ]
    )
    assert [line for line in markdown.splitlines() if line.startswith("## Entry")] == [
        "## Entry 1: assistant",
        "## Entry 2: user",
    ]
    # Decode the representation independently: metadata plus indented block body.
    recovered = []
    for section in markdown.split("### ")[1:]:
        header, body = section.split("\n\n", 1)
        block = json.loads(header)
        lines = body.splitlines()
        end = next(
            (i for i, line in enumerate(lines) if not line.startswith("    ")),
            len(lines),
        )
        value = "\n".join(line[4:] for line in lines[:end])
        field = {
            "text": "text",
            "thinking": "thinking",
            "tool_use": "input",
            "tool_result": "content",
        }[block["type"]]
        block[field] = json.loads(value) if block["type"] == "tool_use" else value
        recovered.append(block)
    assert recovered == blocks


def test_trace_review_checks_require_correct_answers_and_evidence():
    from tests.tasks.test_06_review_agent_trace import _review_checks

    request = "Cells with exactly 20% mitochondrial content should also be retained."
    validation = "PASS: added=['S02', 'S03'], removed=[]; 5 retained; 8/8 decisions"
    environment = "could not track Python environment (no pixi.lock or uv.lock found)"
    entries = [{"content": []} for _ in range(43)]
    entries[26]["content"] = [{"type": "text", "text": request}]
    entries[39]["content"] = [
        {"type": "tool_result", "content": validation + "\n" + environment}
    ]
    review = {
        "old_mito_operator": "<",
        "new_mito_operator": "<=",
        "baseline_cells": ["S01", "S06", "S07"],
        "updated_cells": ["S01", "S02", "S03", "S06", "S07"],
        "gene_cutoff_changed": False,
        "validation_cases_passed": 8,
        "environment_captured": False,
        "session_closed_after_revision": False,
        "evidence": [
            {"purpose": "request", "entry": 27, "quote": request},
            {"purpose": "validation", "entry": 40, "quote": validation},
            {"purpose": "environment", "entry": 40, "quote": environment},
        ],
    }
    assert all(_review_checks(review, entries).values())
    # Vary each answer independently; accepting one wrong field must fail.
    for field, value in (
        ("old_mito_operator", "<="),
        ("new_mito_operator", "<"),
        ("baseline_cells", ["S01"]),
        ("updated_cells", ["S01", "S06", "S07"]),
        ("gene_cutoff_changed", True),
        ("validation_cases_passed", 7),
        ("environment_captured", True),
        ("session_closed_after_revision", True),
    ):
        assert not all(_review_checks({**review, field: value}, entries).values()), (
            field
        )
    for citation in review["evidence"]:
        original = citation.copy()
        for field, value in (
            ("quote", original["quote"] + " invented"),
            ("entry", 999),
        ):
            citation[field] = value
            assert not all(_review_checks(review, entries).values())
            citation.update(original)
