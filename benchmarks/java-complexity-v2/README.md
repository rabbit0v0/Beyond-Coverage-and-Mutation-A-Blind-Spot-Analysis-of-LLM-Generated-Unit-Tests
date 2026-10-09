# ObligBench / Java V2

The supported mixed benchmark contains 1,595 unique tasks: 895 treatment and
700 easy control functions. Current run manifests are `java_v2_mixed.jsonl`,
`java_v2_mixed_treatment.jsonl`, and `java_v2_mixed_control.jsonl` in `manifests/`.
The combined manifest is exactly the union of the treatment and control
manifests, not another version. Only these three current manifests and this
README are included in Git; older local benchmarks and summaries are excluded.

Manifests record provenance, source licenses, signatures, split membership,
complexity, and relative task directories. Tasks are distributed separately;
see DATA_AVAILABILITY.md in the repository root for release status. Restore
projects under `benchmarks/java-complexity-v2/tasks/` before generation.

The public ZIP includes 1,585 source projects (890 treatment and 695 control).
To respect upstream licensing conditions, source for five tasks in each group
is excluded from distribution. All 1,595 task IDs and manifest records are
retained, as are the historical results; this does not change the study's task
population or scores. See [source-exclusions.json](source-exclusions.json) for
task-specific reasons and [source distribution](../../SOURCE_DISTRIBUTION.md)
for details. Metadata for withheld tasks does not constitute a runnable project.

Java v1 and TypeScript exploration and construction scripts requiring those old
inputs are excluded from the public workflow. Use the provided treatment and
control manifests for the current cohorts. The benchmark packager checks task
metadata, source-exclusion policy and required upstream notices when preparing
an archive.

Upstream licenses and attribution must accompany a task-bundle release. The
project's MIT license does not replace individual source licenses. See
[third-party notices](../../THIRD_PARTY_NOTICES.md) for the licensing scope.
