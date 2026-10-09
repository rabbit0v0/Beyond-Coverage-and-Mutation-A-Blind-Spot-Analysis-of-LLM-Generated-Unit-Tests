"""
Compute eligibility-funnel counts for the mutation-based metrics section
(Table tab:eligibility-funnel in documentation/metrics.tex).

Source: analysis/final-mixed-runs/release/treatment/blindspot_profiles.jsonl.gz

Funnel stages (row-level → obligation-level):
  1. Generated-test rows in set
  2.   executed successfully         (execution_passed=True)
  3.     and public target method    (target_is_private=False)
  4. Obligations found               (sum assertion_strength_total_items, over rows passing stage 3)
  5.   reached by the original test  (sum assertion_strength_reached_items)
  6.     with a generated, executed mutant (sum assertion_strength_represented_items)
  7.       checked                   (sum assertion_strength_checked_items)

Run from the project root:
  python3 scripts/compute_funnel_numbers.py
"""

import argparse
import json
import gzip
from pathlib import Path
from collections import Counter

PROFILES = Path(
    "analysis/final-mixed-runs/release/treatment/blindspot_profiles.jsonl.gz"
)

# gemma_panta and gemma_panta_fixenabled both land as model/prompt "panta" in the
# profiles.  Only gemma_panta_fixenabled is canonical; exclude the other run.
EXCLUDED_RUN_IDS = {"java-v2-mixed-gemma-panta-control-full"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", type=Path, default=PROFILES)
    args = parser.parse_args()
    if not args.profiles.exists():
        raise FileNotFoundError(f"Not found: {args.profiles}\nRestore the final profile artifact first.")

    rows = []
    opener = gzip.open if args.profiles.suffix == ".gz" else open
    with opener(args.profiles, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                r = json.loads(line)
                if r.get("run_id") not in EXCLUDED_RUN_IDS:
                    rows.append(r)

    # --- row-level funnel ---
    n_total = len(rows)
    n_executed = sum(1 for r in rows if r.get("execution_passed"))
    n_exec_public = sum(
        1 for r in rows
        if r.get("execution_passed") and not r.get("target_is_private")
    )

    # --- obligation-level funnel (only over executed+public rows) ---
    eligible = [
        r for r in rows
        if r.get("execution_passed") and not r.get("target_is_private")
    ]

    def _s(field):
        return sum(r.get(field) or 0 for r in eligible)

    n_obligations     = _s("assertion_strength_total_items")
    n_reached         = _s("assertion_strength_reached_items")
    n_represented     = _s("assertion_strength_represented_items")
    n_checked         = _s("assertion_strength_checked_items")

    # --- breakdown by model/prompt (for verification) ---
    combos = Counter()
    for r in rows:
        combos[(r.get("model_id", "?"), r.get("prompt_template", "?"))] += 1

    print("=" * 60)
    print("Model × prompt breakdown")
    print("=" * 60)
    for (m, p), n in sorted(combos.items()):
        print(f"  {n:>5}  {m}  /  {p}")
    print(f"  -----")
    print(f"  {n_total:>5}  total")

    print()
    print("=" * 60)
    print("Eligibility funnel (paste into metrics.tex)")
    print("=" * 60)
    print(f"Generated-test rows in set                      & {n_total:,} \\\\")
    print(f"\\quad executed successfully                     & {n_executed:,} \\\\")
    print(f"\\quad\\quad and public target method             & {n_exec_public:,} \\\\")
    print(f"Obligations found                               & {n_obligations:,} \\\\")
    print(f"\\quad reached by the original test              & {n_reached:,} \\\\")
    print(f"\\quad\\quad with a generated, executed mutant    & {n_represented:,} \\\\")
    print(f"\\quad\\quad\\quad checked                         & {n_checked:,} \\\\")

    print()
    print("=" * 60)
    print("Derived rates (sanity check)")
    print("=" * 60)
    def pct(a, b):
        return f"{100*a/b:.1f}%" if b else "n/a"

    print(f"Execution rate:       {pct(n_executed, n_total)} of rows")
    print(f"Public target rate:   {pct(n_exec_public, n_executed)} of executed rows")
    print(f"Reach rate:           {pct(n_reached, n_obligations)} of obligations")
    print(f"Representability:     {pct(n_represented, n_reached)} of reached obligations")
    print(f"Check rate:           {pct(n_checked, n_represented)} of represented obligations")

    # --- note missing configs ---
    expected_configs = {
        ("gpt-5.4", "zero-shot"), ("gpt-5.4", "p3-intention-planning"),
        ("gpt-5.4", "p5-mutation-feedback"), ("gpt-5.4", "panta"),
    }
    found_configs = set(combos.keys())
    missing = [
        f"{m}/{p}" for m, p in sorted(expected_configs - found_configs)
    ]
    gemini_zs = ("gemini-3.6-flash", "zero-shot")
    if gemini_zs not in found_configs:
        print()
        print("NOTE: gemini-3.6-flash / zero-shot not in blindspot_profiles — run not yet analyzed.")

    if missing:
        print("NOTE: other missing expected configs:", missing)


if __name__ == "__main__":
    main()
