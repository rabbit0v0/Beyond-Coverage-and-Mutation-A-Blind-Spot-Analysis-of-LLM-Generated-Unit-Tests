# Source Distribution

ObligBench distributes production source for all 1,595 study tasks: 895 treatment
and 700 control. Accompanying packages provide evaluated tests and aligned
assertion-mutation evidence. See [data availability](DATA_AVAILABILITY.md) for
package contents and reproduction requirements.

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
Use the mapping to locate the notices applicable to each task. The notices
directory contains collected upstream license texts and attribution, including
source-specific conditions. Keep these materials with redistributed source.
Third-party content in evaluated tests and mutation evidence also retains its
upstream terms; MIT applies only to our original material.
