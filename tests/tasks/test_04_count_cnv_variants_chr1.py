"""Count distinct CNV variants on chromosome 1 from local raw parquet files.

Mirrors a real Claude Code session against the 1000 Genomes CNV parquet files,
reduced to a small synthetic fixture so the expected answer is deterministic and
computed here (not hardcoded). The fixture reproduces the real
`data/dragen-3.7.6/hg38-graph-based/<sample>/<sample>.cnv.parquet` layout and the
17-column DRAGEN CNV schema, and plants decoys the agent must filter out:

* chr1 REF / no-call rows (``INFO_SVTYPE`` is null) — not variants
* chr2 CNV rows — wrong chromosome
* chr10 / chr11 CNV rows — a ``"chr1"``-substring trap (exact match, not contains)

The same variant ``ID`` is shared across samples so the count also exercises
de-duplication.
"""

import json
import re
import shutil
from pathlib import Path

import pandas as pd
from testutils import TESTDB1_DEV_DIR, run_claudecode

PROMPT = (
    "Report the number of distinct CNV variants on chromosome 1. "
    "Return the final answer as a single integer. Use the locally stored raw files. "
    "You can only create or put files under raw_files_test and you cannot access "
    "files in lamindb_test. Name the files starting with raw_files_ followed by "
    "whatever you want to put after that."
)

RUN_DIR = Path(f"{TESTDB1_DEV_DIR}/test_04")

# 17-column DRAGEN CNV schema, matching the real *.cnv.parquet files.
_COLUMNS = [
    "CHROM",
    "POS",
    "ID",
    "REF",
    "ALT",
    "QUAL",
    "FILTER",
    "FORMAT",
    "INFO_REFLEN",
    "SAMPLE_GT",
    "SAMPLE_SM",
    "SAMPLE_CN",
    "SAMPLE_BC",
    "SAMPLE_PE",
    "SAMPLE_NAME",
    "INFO_SVLEN",
    "INFO_SVTYPE",
]


def _cnv_row(
    chrom: str, pos: int, variant_id: str, sample: str, *, svtype: str | None, alt: str
) -> dict:
    """One parquet row; REF/no-call rows pass svtype=None (they are not variants)."""
    is_variant = svtype is not None
    return {
        "CHROM": chrom,
        "POS": pos,
        "ID": variant_id,
        "REF": "N",
        "ALT": alt,
        "QUAL": 50.0,
        "FILTER": "cnvLength" if is_variant else "PASS",
        "FORMAT": "GT:SM:CN:BC:PE",
        "INFO_REFLEN": 1000,
        "SAMPLE_GT": "0/1" if is_variant else "./.",
        "SAMPLE_SM": 0.5 if is_variant else 1.0,
        "SAMPLE_CN": 1 if is_variant else 2,
        "SAMPLE_BC": 3,
        "SAMPLE_PE": "9/24",
        "SAMPLE_NAME": sample,
        "INFO_SVLEN": -1000.0 if is_variant else float("nan"),
        "INFO_SVTYPE": svtype,
    }


def _write_fixture(data_root: Path) -> int:
    """Write synthetic per-sample CNV parquet files; return the true distinct count."""
    # ground-truth distinct CNV variants on chr1 (identity = ID; POS derived from
    # ID so any reasonable "distinct" definition yields the same count)
    chr1_variants = [
        (f"DRAGEN:LOSS:chr1:{i * 1000}-{i * 1000 + 500}", i * 1000)
        for i in range(1, 13)
    ]  # 12 distinct

    # deal them to 3 samples with deliberate cross-sample overlap
    per_sample = {
        "HG00096": chr1_variants[0:6],
        "HG00097": chr1_variants[4:10],  # overlaps HG00096 at indices 4,5
        "HG00099": chr1_variants[8:12] + chr1_variants[0:2],  # overlaps both
    }
    expected = len({vid for variants in per_sample.values() for vid, _ in variants})

    for sample, variants in per_sample.items():
        rows: list[dict] = []
        for j, (vid, pos) in enumerate(variants):
            # genuine chr1 CNV variant
            rows.append(_cnv_row("chr1", pos, vid, sample, svtype="CNV", alt="<DEL>"))
            # chr1 REF / no-call row — not a variant, must be excluded
            rows.append(
                _cnv_row(
                    "chr1",
                    pos + 100,
                    f"DRAGEN:REF:chr1:{j}",
                    sample,
                    svtype=None,
                    alt=".",
                )
            )
            # chr2 CNV — wrong chromosome, must be excluded
            rows.append(
                _cnv_row(
                    "chr2",
                    pos,
                    f"DRAGEN:LOSS:chr2:{j}",
                    sample,
                    svtype="CNV",
                    alt="<DEL>",
                )
            )
            # chr10 / chr11 CNV — "chr1"-substring trap, must be excluded
            rows.append(
                _cnv_row(
                    "chr10",
                    pos,
                    f"DRAGEN:LOSS:chr10:{j}",
                    sample,
                    svtype="CNV",
                    alt="<DEL>",
                )
            )
            rows.append(
                _cnv_row(
                    "chr11",
                    pos,
                    f"DRAGEN:GAIN:chr11:{j}",
                    sample,
                    svtype="CNV",
                    alt="<DUP>",
                )
            )
        sample_dir = data_root / sample
        sample_dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows, columns=_COLUMNS).to_parquet(
            sample_dir / f"{sample}.cnv.parquet"
        )
    return expected


def _extract_final_integer(stdout: str) -> int:
    """Pull the agent's final integer answer out of `claude --output-format json`."""
    payload = json.loads(stdout)
    text = payload["result"] if isinstance(payload, dict) else str(payload)
    ints = re.findall(r"-?\d+", text.replace(",", ""))
    assert ints, f"no integer in final answer: {text!r}"
    return int(ints[-1])  # the answer comes last


def test_count_cnv_variants_chr1() -> None:
    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR)
    RUN_DIR.mkdir(parents=True)

    data_root = RUN_DIR / "data" / "dragen-3.7.6" / "hg38-graph-based"
    expected = _write_fixture(data_root)
    (RUN_DIR / "raw_files_test").mkdir()
    (RUN_DIR / "lamindb_test").mkdir()

    result = run_claudecode(RUN_DIR, PROMPT)
    print(f"\n--- claude stdout ---\n{result.stdout}")
    assert result.returncode == 0, (
        f"claude failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    # the answer: distinct CNV variants on chr1, decoys excluded and IDs de-duped
    answer = _extract_final_integer(result.stdout)
    assert answer == expected, (
        f"agent answered {answer}, expected {expected} distinct chr1 CNV variants"
    )

    # constraint 1: the agent must not have written into lamindb_test
    assert not any((RUN_DIR / "lamindb_test").iterdir()), (
        "agent created files under lamindb_test despite the constraint"
    )

    # constraint 2: any output it produced lives under raw_files_test with the
    # required raw_files_ prefix
    raw_outputs = list((RUN_DIR / "raw_files_test").glob("raw_files_*"))
    assert raw_outputs, (
        "agent wrote no raw_files_-prefixed outputs under raw_files_test"
    )
