import ast
import subprocess
import sys
from pathlib import Path

import requests
from testutils import TESTDB1_DEV_DIR, run_codex

_MOESM7_URL = (
    "https://static-content.springer.com/esm/art%3A10.1038%2F"
    "s41467-022-28120-2/MediaObjects/41467_2022_28120_MOESM7_ESM.xlsx"
)

SKILL_UID = "XarPyl1XCgZwSQmY0000"
SKILL_INSTANCE = "laminlabs/lamin-skills"

PROMPT = (
    "Write a Python script that performs DESeq2 bulk RNA-seq analysis "
    "following the skill. Write the script to the current directory. "
    "Do NOT run or execute the script — only write it."
)

RUN_DIR = Path(f"{TESTDB1_DEV_DIR}/test_03")


def test_deseq2_bulk_rnaseq() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    xlsx_path = RUN_DIR / "moesm7.xlsx"
    if not xlsx_path.exists():
        response = requests.get(_MOESM7_URL, timeout=120)
        response.raise_for_status()
        xlsx_path.write_bytes(response.content)

    result = run_codex(
        RUN_DIR,
        PROMPT,
        skill_uid=SKILL_UID,
        skill_instance=SKILL_INSTANCE,
    )
    print(f"\n--- codex stdout ---\n{result.stdout}")
    assert result.returncode == 0, (
        f"codex failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    py_files = list(RUN_DIR.rglob("*.py"))
    assert len(py_files) == 1, f"expected 1 .py file, got {len(py_files)}"
    script = py_files[0]
    ast.parse(script.read_text())

    # step 2: execute the script
    subprocess.run(
        [sys.executable, script.name],
        cwd=RUN_DIR,
        check=True,
    )

    # step 3: check output files were produced
    for fname in ("deseq2_results.csv", "volcano_plot.png", "heatmap_top50.png"):
        fpath = RUN_DIR / fname
        assert fpath.exists(), f"Expected output file missing: {fname}"
        assert fpath.stat().st_size > 0, f"Output file is empty: {fname}"
