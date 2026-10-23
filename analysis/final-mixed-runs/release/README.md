# Final Study Results

These compact study analysis profiles contain the recorded numeric results used
to regenerate the study's reports and comparisons.

| Group | Models | Strategies per model | Tasks per configuration | Rows |
| --- | ---: | ---: | ---: | ---: |
| Treatment | 4 | 4 | 895 | 14,320 |
| Control | 2 | 4 | 700 | 5,600 |

Treatment models are GPT-5.4, Gemma-4-31B, Gemini-3.6-flash, and Qwen3-Coder-30B.
Control models are GPT-5.4 and Gemma-4-31B. Strategies are zero-shot,
intention-planning, mutation-feedback, and Panta.

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
provides benchmark source, evaluated tests and matching assertion-mutation
evidence. Incidental kills are scored zero in the profiles.
Recognized assertion and verification failures earn OS credit,
while uncaught non-assertion exceptions and crashes do not. BVA, CFA, PIT,
execution outcomes and failure classifications retain their recorded values.
PIT and extreme-condition/CFA mutation logs are not in these ZIPs. See
[data availability](../../../DATA_AVAILABILITY.md) for reproduction limits.

`inventory.json` records configurations, row counts, collector settings, evidence
bundle names, and compressed profile hashes. Matching compact profiles are also
included in each cohort's mutation-evidence ZIP. Each configuration contains
every task exactly once.
