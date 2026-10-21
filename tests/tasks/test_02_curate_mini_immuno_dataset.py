import ast
import shutil
import subprocess
import sys
from pathlib import Path

import lamindb as ln
from lamindb.models.feature import parse_dtype
from testutils import TESTDB1_DEV_DIR, run_laminagent

PROMPT = (
    "Track this session in Lamin without permission. "
    "Create a script that curates artifact MdvQpu992LjdLxNz0000 from the laminlabs/lamindata LaminDB instance using "
    "schema pnQvQVcQ417bfmVq and skill u5muNUOPnWPBuZ8z from instance laminlabs/biomed-skills."
)

RUN_DIR = Path(f"{TESTDB1_DEV_DIR}/test_02")


def test_curate_mini_immuno() -> None:
    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR)
    RUN_DIR.mkdir(parents=True)

    # Schema transfer copies the subtype, but not its valid labels. Import the
    # reference values so validation rejects only the intentional IFNJ typo.
    db = ln.DB("laminlabs/lamindata")
    schema = db.Schema.get("pnQvQVcQ417bfmVq").save()
    dtype = schema.members.get(name="perturbation").dtype_as_str
    perturbation_type = db.Record.get(parse_dtype(dtype)[0]["type_uid"])
    for label in db.Record.filter(type=perturbation_type, name__in=["DMSO", "IFNG"]):
        label.save()

    # step 1: write the curation script
    result = run_laminagent(RUN_DIR, "prompt", PROMPT)
    print(f"\n--- agent stdout ---\n{result.stdout}")
    assert result.returncode == 0, (
        f"lag failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    runnable_files = list(RUN_DIR.rglob("*.py"))
    assert runnable_files, (
        f"agent wrote no files\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert len(runnable_files) == 1, "agent should write exactly one .py file"

    script = runnable_files[0]
    code = script.read_text()
    ast.parse(code)
    print(code)
    assert "ln.Artifact.from_dataframe(" in code, (
        "ln.Artifact.from_dataframe( not in code"
    )

    # step 2: execute the script - it should fail!
    script_result = subprocess.run(
        [sys.executable, script.name],
        cwd=RUN_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert script_result.returncode == 1, (
        f"{script.name} failed\nSTDOUT:\n{script_result.stdout}\nSTDERR:\n{script_result.stderr}"
    )
    stderr = script_result.stderr
    assert "IFNJ" in stderr, stderr
    assert (
        "not validated in feature 'perturbation'" in stderr
        or "valid label for subtype 'Perturbations'" in stderr
    ), stderr
