from __future__ import annotations

import os
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import lamindb as ln

from laminagent._setup import get_task, normalize_task_name


def _current_commit_hash16() -> str | None:
    try:
        commit_hash = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return commit_hash[:16] if commit_hash else None


def _current_runner_env() -> str | None:
    if (
        os.getenv("GITHUB_ACTIONS") == "true"
        and os.getenv("RUNNER_ENVIRONMENT") == "github-hosted"
    ):
        return "github_hosted"
    return None


def record_usage_task_name(generated_path: str | None) -> str:
    pytest_current_test = os.getenv("PYTEST_CURRENT_TEST", "")
    if pytest_current_test:
        node_id = pytest_current_test.split(" ", 1)[0]
        test_path = node_id.split("::", 1)[0].replace("\\", "/")
        if "/tests/tasks/test_" in f"/{test_path}":
            return normalize_task_name(Path(test_path).name)

    if generated_path:
        return normalize_task_name(Path(generated_path).name)
    return "lag_authoring"


def current_package_version() -> str:
    try:
        return version("laminagent")
    except PackageNotFoundError:
        return "0.0.0"


def log_usage_record(
    usage: dict[str, int | None],
    *,
    run: ln.Run,
    duration_in_sec: float,
    task_name: str,
) -> None:
    if usage["n_call_count"] == 0:
        return
    task = get_task(task_name=task_name)
    if task is None or task.schema_id is None:
        return
    record = ln.Record(
        features={
            "package_version": current_package_version(),
            "duration_in_sec": duration_in_sec,
            "commit_hash16": _current_commit_hash16(),
            "runner_env": _current_runner_env(),
            "n_call_count": usage["n_call_count"],
            "n_prompt_tokens": usage["n_prompt_tokens"],
            "n_output_tokens": usage["n_output_tokens"],
            "n_total_tokens": usage["n_total_tokens"],
        },
        type=task,
    )
    record.run = run
    record.save()


def normalize_claude_usage(result: dict) -> dict[str, int | None]:
    """Use final CLI totals; cached input contributes to the full prompt size."""
    usage = result["usage"]
    prompt_tokens = (
        usage["input_tokens"]
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
    )
    output_tokens = usage["output_tokens"]
    return {
        # num_turns is not a count of all API requests (e.g. subagent calls).
        "n_call_count": None,
        "n_prompt_tokens": prompt_tokens,
        "n_output_tokens": output_tokens,
        "n_total_tokens": prompt_tokens + output_tokens,
    }
