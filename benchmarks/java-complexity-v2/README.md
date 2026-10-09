# ObligBench / Java V2

The supported mixed benchmark contains 1,595 unique tasks: 895 treatment and
700 easy control functions. Current run manifests are `java_v2_mixed.jsonl`,
`java_v2_mixed_treatment.jsonl`, and `java_v2_mixed_control.jsonl` in `manifests/`.
The combined manifest is exactly the union of the treatment and control
manifests. The repository provides the manifests and documentation; source
projects are distributed in `ObligBench.zip`.

Manifests record provenance, source licenses, signatures, split membership,
complexity, and relative task directories. Tasks are distributed separately;
see [data availability](../../DATA_AVAILABILITY.md) for package contents. Restore
projects under `benchmarks/java-complexity-v2/tasks/` before generation.

The public ZIP includes all 1,595 source projects (895 treatment and 700 control),
with task metadata, provenance and upstream notices. Please respect the applicable
original licenses when using or redistributing source. See
[source distribution](../../SOURCE_DISTRIBUTION.md) for Stack v2 licensing references.
