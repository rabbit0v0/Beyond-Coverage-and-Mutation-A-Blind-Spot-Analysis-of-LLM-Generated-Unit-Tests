# Third-Party Material

The MIT license in `LICENSE` applies to original material contributed to this
project. It does not relicense third-party material or override the upstream
terms for extracted or adapted benchmark code, including production sources
bundled with evaluated tests. Any third-party content incorporated into test
sources also retains its applicable upstream terms.

Benchmark manifests and per-task metadata record the source dataset, repository,
revision, source path, and license labels supplied with the dataset. Release
bundles also include `THIRD_PARTY_SOURCES.jsonl`, keyed by benchmark task ID.
Evaluated-test bundles include only the provenance for their own cohort.

The Stack v2 labels are the starting point for identifying upstream terms.
Where they conflict with the original file or applicable module license, the
original source terms take precedence. File-specific terms also take precedence
over a repository's general license. A list of dataset labels is not interpreted
as a choice of licenses for each extracted method.

To respect upstream licensing conditions, production source for ten tasks is
excluded from public packages under the release's source-distribution policy.
Their task IDs and historical results remain in the study.
See [source distribution](SOURCE_DISTRIBUTION.md) and the benchmark's
`source-exclusions.json` for the task-specific decisions.

Release bundles contain a consolidated `third-party-notices/` directory.
Its `task-sources.jsonl` links task IDs to source notices, license documents,
and the recorded source-specific terms. Documents are stored once by hash.
When regenerating a benchmark ZIP, supply this consolidated directory through
`scripts/package_benchmark_release.py --notices-dir`; the packager checks task
provenance and hashes and includes only referenced notice files.

These records preserve provenance; they are not substitutes for upstream
copyright notices, license texts, or other required attribution. Dataset license
labels may cover multiple parts of a repository or be unrecognized, and must not
be interpreted as a definitive license assignment for an extracted method.

Retain applicable upstream license texts and copyright notices when
redistributing third-party source. Publication of the source bundles requires
review of those upstream terms and inclusion of the required notices.
