# Copy-number variation

Agentic variant analysis of the [1000 Genomes Project](https://www.internationalgenome.org/) — VCF → Parquet curation under a schema contract, then Polars / DuckDB / PyArrow queries and agent-tracked genomic-band analyses.

Write-up: [Agentic variant analysis of the 1000 Genomes Project](https://blog.lamin.ai/1000genomes)

**Instance:** [laminlabs/1000genomes](https://lamin.ai/laminlabs/1000genomes) · **Projects:** [Lakehouse benchmarks](https://lamin.ai/laminlabs/1000genomes/project/huN5V4kDfDQT) · [Agentic workflows](https://lamin.ai/laminlabs/1000genomes/project/xSomr73bTHWK)

## Agentic analyses

Example task: count variants in a genomic band (chr1:150–200 Mb) — raw Parquet files vs. collection schema contract ([blog Fig. 1](https://blog.lamin.ai/1000genomes)).

| Approach       | Script                                                                                      | Agent run                                                                                 |
| -------------- | ------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Via collection | [count_variants_band.py](https://lamin.ai/laminlabs/1000genomes/transform/X4viXPeYaUL90000) | [run](https://lamin.ai/laminlabs/1000genomes/transform/SnfuhjObaAKR0000/ydgt2ApOZ41szGwv) |
| Via raw files  | [count_chr1_band.py](https://lamin.ai/laminlabs/1000genomes/transform/4hanQjMg7hpk0000)     | [run](https://lamin.ai/laminlabs/1000genomes/transform/SnfuhjObaAKR0000/JglGIr0IRE4BFM4y) |
