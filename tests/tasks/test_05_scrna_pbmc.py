from pathlib import Path

import anndata as ad
import lamindb as ln
import scanpy as sc
from testutils import TESTDB1_DEV_DIR, run_claudecode

# Known stable HVGs in PBMC3k under seurat_v3 / n_top_genes=2000.
# These span all major cell types and should always rank as highly variable.
_EXPECTED_HVGS = {
    "LYZ",  # monocyte
    "S100A9",  # monocyte
    "IL32",  # T cell
    "CD79A",  # B cell
    "NKG7",  # NK cell
    "GNLY",  # NK cell
    "PPBP",  # megakaryocyte
    "MS4A1",  # B cell
    "CST3",  # monocyte / DC
    "GZMB",  # cytotoxic
}


# Top marker genes per expected PBMC cell type.
# CI checks that each annotated cell type in obs["cell_type"] has at least
# one of its canonical markers in rank_genes_groups top-50 for that cluster.
# Robust PBMC lineage/cell-type marker sets.
# Positive markers only are included here.
# Negative markers from the source dictionary are intentionally excluded.

_CELL_TYPE_MARKERS = {
    "cd4": {"CD3D", "CD3E", "CD4", "IL7R", "CCR7", "LTB", "IL32"},
    "cd8": {"CD3D", "CD3E", "CD8A", "CD8B", "GZMK", "GZMA", "CCL5"},
    "nk": {"NKG7", "GNLY", "GZMB", "PRF1", "CD247", "TYROBP", "FCER1G", "KLRG1"},
    "b": {"CD79A", "CD79B", "MS4A1", "CD74", "HLA-DRA", "IGHM", "CD37"},
    "monocyte": {"LYZ", "S100A9", "FCN1", "CST3", "CTSS", "LYN", "FCGR3A", "LILRB1"},
    "dendritic": {"FCER1A", "CST3", "CD1C", "CLEC10A", "HLA-DRA", "HLA-DQA1"},
    "megakaryocyte": {"PPBP", "PF4", "SDPR", "ITGA2B", "RGS18"},
}
PROMPT = (
    "Don't ask for confirmations, track the session. "
    "Write a Python script called scrna_pbmc3k.py that runs a full single-cell "
    "RNA-seq analysis on the PBMC3k dataset already available at pbmc3k_raw.h5ad "
    "in the current directory. The analysis must cover:\n\n"
    "1. QC: filter low-quality cells and genes, annotate mitochondrial genes, "
    "compute QC metrics, and filter on gene count and mitochondrial content. "
    "Preserve the raw counts in a dedicated layer.\n\n"
    "2. Normalization and log-transformation.\n\n"
    "3. Highly variable gene selection (~2000 genes, using the counts layer). "
    "Subset the object to only the highly variable genes before any further steps.\n\n"
    "4. Regress out technical confounders and scale, storing the result in a separate layer.\n\n"
    "5. Dimensionality reduction, graph construction, Leiden clustering, and UMAP "
    "initialized from PAGA. Ensure PCA coordinates, UMAP coordinates, and Leiden "
    "labels are all stored on the object.\n\n"
    "6. Marker gene analysis per cluster (Wilcoxon test). "
    "run it and ensure the results are stored on the AnnData object before saving. "
    "Print the top genes per cluster.\n\n"
    "7. Cell type annotation: inspect the marker genes and assign a biologically "
    "meaningful cell type label to each cluster, storing it in obs['cell_type']. "
    "Expected PBMC populations include CD4 T, CD8 T, NK, B cells, CD14+ Monocytes, "
    "FCGR3A+ Monocytes, Dendritic Cells, and Megakaryocytes — use these as a guide "
    "based on the actual markers, not as a hard-coded map.\n\n"
    "Save a UMAP coloured by cell_type as umap_cell_type.png. "
    "Save the final AnnData as pbmc3k_processed.h5ad and register it as a LaminDB "
    "artifact with the key 'scrna/pbmc3k_processed.h5ad'. "
)

RUN_DIR = Path(f"{TESTDB1_DEV_DIR}/test_05")


# ---------------------------------------------------------------------------
# Biological validation
# ---------------------------------------------------------------------------


def _validate_anndata(h5ad_path: Path) -> None:
    """Load the processed AnnData and assert structural + biological correctness."""
    adata = ad.read_h5ad(h5ad_path)

    # --- structural checks ---
    assert "counts" in adata.layers, (
        "adata.layers['counts'] missing — raw counts were not preserved"
    )
    assert "scaled" in adata.layers, (
        "adata.layers['scaled'] missing — scaled expression not stored"
    )
    assert "X_pca" in adata.obsm, "adata.obsm['X_pca'] missing"
    assert "X_umap" in adata.obsm, "adata.obsm['X_umap'] missing"
    assert "leiden" in adata.obs.columns, "adata.obs['leiden'] missing"
    assert "cell_type" in adata.obs.columns, "adata.obs['cell_type'] missing"
    assert "rank_genes_groups" in adata.uns, (
        "adata.uns['rank_genes_groups'] missing — marker gene analysis was not run"
    )
    assert "highly_variable" in adata.var.columns, (
        "adata.var['highly_variable'] missing — HVG selection was not performed"
    )

    # --- HVG overlap with known PBMC markers ---
    hvgs = set(adata.var_names[adata.var["highly_variable"]])
    overlap = _EXPECTED_HVGS & hvgs
    assert len(overlap) == len(_EXPECTED_HVGS), (
        f"HVG list overlaps only {len(overlap)} known PBMC markers "
        f"(Missing: {_EXPECTED_HVGS - overlap}"
    )

    # --- cell type diversity ---
    cell_types = adata.obs["cell_type"].unique().tolist()
    assert len(cell_types) >= 7, (
        f"Only {len(cell_types)} distinct cell types assigned): {cell_types}"
    )

    # --- marker gene sanity: at least one canonical marker in top-50 per cell type keyword ---

    rgg = adata.uns["rank_genes_groups"]
    groups = list(rgg["names"].dtype.names)
    # flatten top-50 markers across all groups into a single set for a lightweight check
    top_markers: set[str] = set()
    for grp in groups:
        top_markers.update(rgg["names"][grp][:50].tolist())

    for label, canonical in _CELL_TYPE_MARKERS.items():
        if any(label.lower() in ct.lower() for ct in cell_types):
            hit = canonical & top_markers
            assert hit, (
                f"No canonical marker for '{label}' found in rank_genes_groups top-50. "
                f"Expected one of {canonical}."
            )

    # --- basic cell count sanity ---
    n_cells = adata.n_obs
    assert 1500 <= n_cells <= 2700, (
        f"Unexpected cell count after QC: {n_cells} (expected 1500–2700 for PBMC3k)"
    )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_scrna_pbmc3k() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    raw_path = RUN_DIR / "pbmc3k_raw.h5ad"
    if not raw_path.exists():
        adata = sc.datasets.pbmc3k()
        adata.write_h5ad(raw_path)

    result = run_claudecode(RUN_DIR, PROMPT, install_skill=True)
    print(f"\n--- claude stdout ---\n{result.stdout}")

    # --- LaminDB lineage checks ---
    transform = ln.Transform.filter(key="__claudecode__").one_or_none()
    assert transform is not None, "skill never created the __claudecode__ transform"
    run = ln.Run.filter(transform=transform).order_by("-created_at").first()
    assert run is not None, "no Run found for the __claudecode__ transform"
    assert run.finished_at is not None, (
        "skill did not close the run (ln.finish() was not called)"
    )

    script_run = ln.Run.filter(initiated_by_run=run).order_by("-created_at").first()
    assert script_run is not None, (
        "no Run is linked back to the __claudecode__ agent run via initiated_by_run — "
        "the generated script was not self-tracked with LAMIN_INITIATED_BY_RUN_UID set"
    )

    h5ad_artifact = (
        ln.Artifact.filter(run=script_run, key="scrna/pbmc3k_processed.h5ad")
        .order_by("-created_at")
        .first()
    )
    assert h5ad_artifact is not None, (
        "pbmc3k_processed.h5ad was not saved as a LaminDB artifact attached to the script's Run"
    )

    # --- file existence checks ---
    h5ad_path = RUN_DIR / "pbmc3k_processed.h5ad"
    assert h5ad_path.exists(), "pbmc3k_processed.h5ad not found on disk"
    assert h5ad_path.stat().st_size > 0, "pbmc3k_processed.h5ad is empty"

    png_path = RUN_DIR / "umap_cell_type.png"
    assert png_path.exists(), "umap_cell_type.png not found on disk"
    assert png_path.stat().st_size > 0, "umap_cell_type.png is empty"

    # --- biological content validation ---
    _validate_anndata(h5ad_path)
