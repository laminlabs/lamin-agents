import subprocess
import sys
from pathlib import Path

import lamindb as ln
import pytest
from testutils import (
    TESTDB1_DEV_DIR,
    assert_single_valid_script,
    assert_valid_fasta_outputs,
    run_claudecode,
    run_copilot,
    run_laminagent,
)

EXAMPLE_UID = "RL6ZsKnHZvlhDg1d"

LAMINAGENT_PROMPT = (
    "Write a Python script that writes your favorite protein sequence to a file called protein.fasta "
    "and saves it as a LaminDB artifact."
)
CLAUDECODE_PROMPT = (
    "For this automated test, I explicitly consent to LaminDB tracking. "
    "Don't ask for confirmations; track the session and continue. "
    "Write a Python script called save_protein.py in the current directory that "
    "writes your favorite protein sequence to a file called protein.fasta and "
    "saves it as a LaminDB artifact. Then run the script."
)
COPILOT_PROMPT = CLAUDECODE_PROMPT


def _run_and_verify_laminagent(run_dir: Path) -> None:
    # step 1: write the script
    result = run_laminagent(str(run_dir), "prompt", LAMINAGENT_PROMPT)
    assert result.returncode == 0
    run = ln.Run.filter().order_by("-created_at").first()
    assert run is not None, "No run found after laminagent invocation"
    example_record = ln.Record.filter(uid=EXAMPLE_UID).one_or_none()
    if example_record is None:
        example_record = ln.Record(name="example_fasta_run")
        example_record.uid = EXAMPLE_UID
        example_record.save()
    run.records.add(example_record)
    feature_values = run.features.get_values()
    for key in ("n_call_count", "n_prompt_tokens", "n_output_tokens", "n_total_tokens"):
        assert key in feature_values, f"Missing usage feature: {key}"

    script = assert_single_valid_script(run_dir)

    # step 2: execute the script directly
    script_result = subprocess.run(
        [sys.executable, script.name],
        cwd=run_dir,
        capture_output=True,
        text=True,
    )
    assert script_result.returncode == 0, (
        f"{script.name} failed\nSTDOUT:\n{script_result.stdout}\nSTDERR:\n{script_result.stderr}"
    )


def _verify_agent_tracking(run_dir: Path, transform_key: str) -> None:
    """Verify the end-to-end tracking contract shared by coding-agent skills."""
    transform = ln.Transform.filter(key=transform_key).one_or_none()
    assert transform is not None, f"skill never created the {transform_key} transform"
    run = ln.Run.filter(transform=transform).order_by("-created_at").first()
    assert run is not None, f"no Run found for the {transform_key} transform"
    assert run.report is not None, "skill did not save a run.report"
    assert run.finished_at is not None, "skill never closed the run (Step 3 skipped)"

    assert_single_valid_script(run_dir)

    # Find the script's own Run via its initiated_by_run lineage, not by guessing
    # its self-assigned Transform key: the key is path-qualified (e.g.
    # "copilot/save_protein.py") in a way we can't reliably predict, and other
    # harnesses' scripts can share the same bare filename, which would silently
    # match the wrong Transform if we filtered by key alone. initiated_by_run is
    # collision-proof by construction.
    script_run = ln.Run.filter(initiated_by_run=run).order_by("-created_at").first()
    assert script_run is not None, (
        f"no Run is linked back to the {transform_key} agent run via initiated_by_run "
        "— the generated script was not self-tracked with LAMIN_INITIATED_BY_RUN_UID "
        "set, per the skill's 'Self-tracking scripts' section (or it was saved as a "
        "plain Artifact instead of a Transform, the same class of bug caught earlier)"
    )
    assert script_run.transform.run is not None, (
        "lamin finish did not expose the generated script Transform as an output "
        f"of the {transform_key} agent run"
    )
    assert script_run.transform.run.uid == run.uid

    fasta_artifact = (
        ln.Artifact.filter(run=script_run, suffix=".fasta")
        .order_by("-created_at")
        .first()
    )
    assert fasta_artifact is not None, (
        "protein.fasta was written to disk but never saved as a LaminDB Artifact "
        "attached to the script's own Run — the prompt explicitly asked for "
        "'saves it as a LaminDB artifact'"
    )


def _run_and_verify_claudecode(run_dir: Path) -> None:
    previous_dev_dir = ln.setup.settings.dev_dir
    ln.setup.settings.dev_dir = run_dir
    try:
        result = run_claudecode(run_dir, CLAUDECODE_PROMPT, install_skill=True)
    finally:
        ln.setup.settings.dev_dir = previous_dev_dir
    print(f"\n--- claude stdout ---\n{result.stdout}")
    _verify_agent_tracking(run_dir, "__claudecode__")


def _run_and_verify_copilot(run_dir: Path) -> None:
    previous_dev_dir = ln.setup.settings.dev_dir
    ln.setup.settings.dev_dir = run_dir
    try:
        result = run_copilot(run_dir, COPILOT_PROMPT, install_skill=True)
    finally:
        ln.setup.settings.dev_dir = previous_dev_dir
    print(f"\n--- copilot stdout ---\n{result.stdout}")
    _verify_agent_tracking(run_dir, "__copilot__")


_HARNESS_RUNNERS = {
    "laminagent": _run_and_verify_laminagent,
    "claudecode": _run_and_verify_claudecode,
    "copilot": _run_and_verify_copilot,
}


@pytest.mark.parametrize("harness", ["laminagent", "claudecode", "copilot"])
def test_create_favorite_protein_sequence(harness: str) -> None:
    run_dir = Path(TESTDB1_DEV_DIR) / harness
    run_dir.mkdir(parents=True, exist_ok=True)

    _HARNESS_RUNNERS[harness](run_dir)

    # step 3: check .fasta was produced and is valid — shared across harnesses
    assert_valid_fasta_outputs(run_dir)
