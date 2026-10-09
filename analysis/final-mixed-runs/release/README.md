# Final Study Results

Only the compact results from the `pit-mutation-v11c-object-observability-broad`
analysis lineage are included in Git. Generated summaries are not included.

| Group | Models | Strategies per model | Tasks per configuration | Rows |
| --- | ---: | ---: | ---: | ---: |
| Treatment | 4 | 4 | 895 | 14,320 |
| Control | 2 | 4 | 700 | 5,600 |

Treatment models are GPT-5.4, Gemma-4-31B, Gemini-3.6-flash, and Qwen3-Coder-30B.
Control models are GPT-5.4 and Gemma-4-31B. Strategies are zero-shot,
intention-planning, mutation-feedback, and Panta. The Gemini control run,
duplicate model-only exports, older analysis versions, and previous summaries
are excluded.

`treatment/blindspot_profiles.jsonl.gz` and `control/blindspot_profiles.jsonl.gz`
contain one compact analyzed row per task/configuration. They retain recorded
execution/failure classifications, BVA categories and counts, CFA construct
counts, OS construct/subcategory counts, and primary scores. They omit source
snippets, test code, machine-local paths, and experimental side-effect fields.
These are derived numeric results, not raw evaluation evidence.

The analyzer's `--saved-profiles` mode regenerates the report tables without
source workdirs, model calls, Maven, or mutation execution. Follow
[the reproduction guide](../../../docs/reproduction.md) from the repository root.
Rechecking the original classifications and mutations requires the separately
archived source, tests, logs, and mutant evidence. The accompanying artifact distribution
provides benchmark source, evaluated tests and a separate aligned-zero
assertion-mutation rerun. These compact profiles retain the historical scores;
they have not been replaced with aligned-rerun results. Historical PIT and
extreme-condition/CFA evidence are not in those mutation ZIPs. See
[data availability](../../../DATA_AVAILABILITY.md) for reproduction limits.

`inventory.json` records configurations, row counts, source-profile hashes, and
compressed release-profile hashes. Recreate the compact release from the full
final profiles with:

```bash
python scripts/package_study_results.py --out-dir /tmp/final-study-release
```

The exporter validates that every configuration contains every task exactly
once. It copies recorded scores and counts; it does not recompute them.
