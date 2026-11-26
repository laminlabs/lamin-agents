from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner
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
