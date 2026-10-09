# Source Distribution

ObligBench distributes production source for all 1,595 study tasks: 895 treatment
and 700 control. The accompanying evaluated-test and mutation-evidence packages
preserve the available source, tests and evidence used in the study. No task
source is withheld under the current distribution policy.

## Upstream Licenses

Please respect each source's applicable upstream license when using, modifying,
or redistributing these artifacts. Retain copyright notices, license texts and
required attribution, and follow any source-specific conditions. Our MIT license
covers our original contributions only; it does not relicense upstream source
or grant additional rights to it. Publication for research does not remove
upstream conditions or establish permission for every downstream use.

The benchmark source was extracted from The Stack v2. Its licensing materials
provide context for the recorded dataset labels:

- [Terms of use and licensing information](https://huggingface.co/datasets/bigcode/the-stack-v2#licensing-information)
- [License detection process](https://huggingface.co/datasets/bigcode/the-stack-v2#license-detection)
- [License list and statistics](https://huggingface.co/datasets/bigcode/the-stack-v2/blob/main/license_stats.csv)

Stack v2 requires users to follow the original source licenses, including
attribution where applicable. Its license list is not a single license for the
benchmark. Dataset labels are used where no conflicting source evidence is found;
original file or module terms take precedence where they differ.

Release packages include `THIRD_PARTY_SOURCES.jsonl` and a consolidated
`third-party-notices/` directory. The task mapping records repositories, revisions,
source paths, dataset labels and collected source-specific license evidence.
Preserved notices include custom conditions or ambiguous scope where found;
their inclusion does not convert them into unrestricted permission. See
[third-party notices](THIRD_PARTY_NOTICES.md) for the licensing scope and mapping.

## Reproduction

The benchmark contains all task projects. Evaluated-test packages preserve
historical fixtures rather than replacing them with the current benchmark code.
Files absent from historical runs and failed generations remain identified in
the evaluation records. These historical gaps are not licensing exclusions.

The mutation packages retain production snippets, mutant text and expression
fields from the aligned assertion-mutation rerun. They do not contain historical
PIT or extreme-condition/CFA evidence. Restoring source availability does not
change recorded outcomes, scores or the study population. See
[data availability](DATA_AVAILABILITY.md) for package contents and reproduction
requirements.
