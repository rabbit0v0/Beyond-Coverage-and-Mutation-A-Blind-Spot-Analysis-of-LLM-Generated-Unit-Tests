#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

RETURN_RULE_STRATA = [
    "opaque_object_degenerate_null",
    "domain_object_default_constructor",
    "domain_object_concrete_constructor_defaults",
    "return_null_type_compatible_replacement",
    "boolean_return",
    "numeric_return",
    "string_return",
]
EXCEPTION_STRATA = ["exception_behavior", "exception_behavior_broad_original"]
OUTCOME_ORDER = ["survived", "killed_by_oracle"]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_rows(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        if path.is_dir():
            for child in sorted(path.glob("*.jsonl")):
                rows.extend(read_jsonl(child))
        else:
            rows.extend(read_jsonl(path))
    return rows


def return_subcategory(operator_rule: str | None) -> str | None:
    rule = str(operator_rule or "")
    if rule == "boolean_negate":
        return "boolean"
    if rule in {
        "numeric_zero_or_one_literal_only",
        "floating_zero_or_one_literal_only",
        "integral_expression_plus_one_noop_safe",
        "integral_expression_increment_noop_safe",
        "boxed_integral_literal_only",
    }:
        return "numeric"
    if rule in {"string_empty_or_mutated_literal_only", "string_expression_concatenated_noop_safe"}:
        return "string"
    return None


def source_path(row: dict[str, Any]) -> Path | None:
    workdir = Path(str(row.get("workdir") or ""))
    if not workdir:
        return None
    if not workdir.is_absolute():
        workdir = ROOT / workdir
    path = workdir / "src/main/java/benchmark/Subject.java"
    return path if path.exists() else None


def source_text(row: dict[str, Any]) -> str:
    path = source_path(row)
    if not path:
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def test_text(row: dict[str, Any]) -> str:
    for key in ["post_repair_test_code", "extracted_test_code"]:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    test_file = row.get("test_file")
    if isinstance(test_file, dict):
        preview = test_file.get("preview")
        if isinstance(preview, str):
            return preview
        path = test_file.get("path")
        if path and Path(path).exists():
            return Path(path).read_text(encoding="utf-8", errors="replace")
    return ""


def line_window(text: str, line: int | None, radius: int = 12) -> str:
    if not text or not isinstance(line, int) or line <= 0:
        return text[:5000]
    lines = text.splitlines()
    start = max(1, line - radius)
    end = min(len(lines), line + radius)
    return "\n".join(
        f"{number:04d}{' >>> ' if number == line else '     '}{lines[number - 1]}"
        for number in range(start, end + 1)
    )


def full_source_with_line_numbers(text: str, highlight_line: int | None) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(
        f"{number:04d}{' >>> ' if number == highlight_line else '     '}{lines[number - 1]}"
        for number in range(1, len(lines) + 1)
    )


def test_window(test: str, item: dict[str, Any], mutant: dict[str, Any], radius: int = 18) -> str:
    snippets = [
        str(item.get("snippet") or "").strip(),
        str(mutant.get("mutant_text") or "").strip(),
        str(item.get("evidence_snippet") or "").strip(),
    ]
    compact_lines = test.splitlines()
    for snippet in snippets:
        if not snippet:
            continue
        needle = re.sub(r"\s+", " ", snippet)
        for idx, line in enumerate(compact_lines):
            if needle and needle in re.sub(r"\s+", " ", line):
                start = max(0, idx - radius)
                end = min(len(compact_lines), idx + radius + 1)
                return "\n".join(
                    f"{number + 1:04d}{' >>> ' if number == idx else '     '}{compact_lines[number]}"
                    for number in range(start, end)
                )
    return test[:8000]


def execute_log_path(item: dict[str, Any], mutant: dict[str, Any]) -> str:
    variant = str(mutant.get("variant") or mutant.get("sub_variant") or "")
    if variant:
        value = item.get(f"{variant}_mutant_execute_log")
        if value:
            return str(value)
    return str(mutant.get("execute_log") or "")


def execute_log_excerpt(path_value: str) -> str:
    if not path_value:
        return ""
    path = Path(path_value)
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[:20])[:3000]


def likely_test_highlight_terms(item: dict[str, Any], mutant: dict[str, Any], log_excerpt: str) -> list[str]:
    terms: list[str] = []
    for key in ["source_snippet", "snippet"]:
        value = str(item.get(key) or "").strip()
        if value:
            terms.append(value)
    mutant_text = str(mutant.get("mutant_text") or "").strip()
    if mutant_text:
        terms.append(mutant_text)
    for marker in ["Java class. ", "AssertionError:"]:
        if marker in log_excerpt:
            message = log_excerpt.split(marker, 1)[1].split(" -> [Help", 1)[0].splitlines()[0].strip()
            if message:
                terms.append(message)
                terms.extend(part.strip() for part in re.split(r"\bexpected=|\bactual=|\bwrong exception=", message) if len(part.strip()) >= 12)
    return list(dict.fromkeys(terms))


def sample_fingerprint(record: dict[str, Any]) -> str:
    payload = {
        "task_id": record.get("task_id"),
        "prompt_template": record.get("prompt_template"),
        "model_id": record.get("model_id"),
        "construct": record.get("construct"),
        "source_line": record.get("source_line"),
        "source_snippet": record.get("source_snippet"),
        "mutant_text": record.get("mutant_text"),
        "operator_rule": record.get("operator_rule"),
        "outcome": record.get("analyzer_outcome"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def stratum_for(item: dict[str, Any], mutant: dict[str, Any]) -> str | None:
    construct = str(item.get("construct") or mutant.get("operator") or "")
    rule = str(mutant.get("operator_rule") or "")
    if construct == "exception_behavior":
        if mutant.get("subsumed_by_broad_original"):
            return "exception_behavior_broad_original"
        return "exception_behavior"
    if construct != "return_behavior":
        return None
    if rule in {
        "opaque_object_degenerate_null",
        "domain_object_default_constructor",
        "domain_object_concrete_constructor_defaults",
        "return_null_type_compatible_replacement",
    }:
        return rule
    subcategory = str(mutant.get("return_subcategory") or return_subcategory(rule) or "")
    if subcategory in {"boolean", "numeric", "string"}:
        return f"{subcategory}_return"
    return None


def collect_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if not row.get("execution_passed") or row.get("target_is_private"):
            continue
        for item in (row.get("assertion_mutation") or {}).get("items") or []:
            for mutant in item.get("mutants") or []:
                outcome = str(mutant.get("outcome") or "")
                if outcome not in OUTCOME_ORDER:
                    continue
                stratum = stratum_for(item, mutant)
                if not stratum:
                    continue
                source_line = mutant.get("source_line") or item.get("line")
                record = {
                    "stratum": stratum,
                    "analyzer_outcome": outcome,
                    "construct": item.get("construct"),
                    "operator_rule": mutant.get("operator_rule"),
                    "return_subcategory": mutant.get("return_subcategory"),
                    "subsumed_by_broad_original": bool(mutant.get("subsumed_by_broad_original")),
                    "degenerate_null": bool(mutant.get("degenerate_null")),
                    "observability_signal": mutant.get("observability_signal"),
                    "task_id": row.get("task_id"),
                    "model_id": row.get("model_id"),
                    "prompt_template": row.get("prompt_template"),
                    "workdir": row.get("workdir"),
                    "signature": (row.get("task") or {}).get("signature") or row.get("signature"),
                    "source_line": source_line,
                    "source_snippet": item.get("snippet"),
                    "mutant_text": mutant.get("mutant_text"),
                    "failure_type": mutant.get("failure_type"),
                    "execute_log": execute_log_path(item, mutant),
                    "coverage_imprecise": bool(mutant.get("coverage_imprecise")),
                    "_row": row,
                    "_item": item,
                    "_mutant": mutant,
                }
                record["sample_fingerprint"] = sample_fingerprint(record)
                candidates.append(record)
    return candidates


def attach_review_context(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source_cache: dict[str, str] = {}
    test_cache: dict[str, str] = {}
    enriched: list[dict[str, Any]] = []
    for row in rows:
        source_row = row.pop("_row")
        item = row.pop("_item")
        mutant = row.pop("_mutant")
        key = str(source_row.get("workdir") or "")
        if key not in source_cache:
            source_cache[key] = source_text(source_row)
            test_cache[key] = test_text(source_row)
        log_excerpt = execute_log_excerpt(str(row.get("execute_log") or ""))
        row["source_window"] = full_source_with_line_numbers(source_cache[key], row.get("source_line"))
        row["source_is_full"] = True
        row["test_window"] = test_window(test_cache[key], item, mutant)
        row["execute_log_excerpt"] = log_excerpt
        row["test_highlight_terms"] = likely_test_highlight_terms(item, mutant, log_excerpt)
        enriched.append(row)
    return enriched


def allocate_counts(strata: list[str], total: int) -> dict[str, int]:
    base = total // len(strata)
    extra = total % len(strata)
    return {stratum: base + (1 if index < extra else 0) for index, stratum in enumerate(strata)}


def pick_balanced(candidates: list[dict[str, Any]], total: int, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    strata = RETURN_RULE_STRATA + EXCEPTION_STRATA
    by_bucket: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        by_bucket[(candidate["stratum"], candidate["analyzer_outcome"])].append(candidate)
    for bucket in by_bucket.values():
        rng.shuffle(bucket)

    targets = allocate_counts(strata, total)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for stratum in strata:
        need = targets[stratum]
        per_outcome = allocate_counts(OUTCOME_ORDER, need)
        for outcome in OUTCOME_ORDER:
            for candidate in by_bucket.get((stratum, outcome), [])[: per_outcome[outcome]]:
                if candidate["sample_fingerprint"] not in seen:
                    selected.append(candidate)
                    seen.add(candidate["sample_fingerprint"])
        while sum(1 for item in selected if item["stratum"] == stratum) < need:
            pool = [
                candidate
                for outcome in OUTCOME_ORDER
                for candidate in by_bucket.get((stratum, outcome), [])
                if candidate["sample_fingerprint"] not in seen
            ]
            if not pool:
                break
            candidate = pool[0]
            selected.append(candidate)
            seen.add(candidate["sample_fingerprint"])

    if len(selected) < total:
        leftovers = [candidate for candidate in candidates if candidate["sample_fingerprint"] not in seen]
        rng.shuffle(leftovers)
        selected.extend(leftovers[: total - len(selected)])

    selected = selected[:total]
    selected.sort(key=lambda row: (row["stratum"], row["analyzer_outcome"], row["task_id"] or ""))
    for index, row in enumerate(selected, start=1):
        row["sample_id"] = f"AMV-{index:03d}"
    return selected


def write_csv(path: Path, rows: list[dict[str, Any]], rater: str | None = None) -> None:
    fieldnames = [
        "sample_id",
        "sample_fingerprint",
        "stratum",
        "analyzer_outcome",
        "construct",
        "operator_rule",
        "return_subcategory",
        "subsumed_by_broad_original",
        "task_id",
        "model_id",
        "prompt_template",
        "signature",
        "source_line",
        "source_snippet",
        "mutant_text",
        "failure_type",
        "execute_log",
        "rater_id",
        "mutant_validity_label",
        "oracle_label_verification",
        "notes",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            out = {key: row.get(key, "") for key in fieldnames}
            out["rater_id"] = rater or ""
            out["mutant_validity_label"] = ""
            out["oracle_label_verification"] = ""
            out["notes"] = ""
            writer.writerow(out)


def write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps({"samples": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_codebook(path: Path, rows: list[dict[str, Any]], candidate_counts: Counter[tuple[str, str]]) -> None:
    lines = [
        "# Assertion Mutation Manual Validation",
        "",
        f"This pack samples {len(rows)} v11c assertion-strength mutants, stratified by mutation rule and analyzer outcome.",
        "",
        "Each rater should label independently in `review.html`, then download/export their completed CSV.",
        "",
        "## Labels",
        "",
        "`mutant_validity_label`: choose one of `genuine`, `behaviorally_equivalent`, `out_of_domain`, or `unsure`.",
        "",
        "- Use `genuine` when the mutant is a fair, in-domain behavioral change for the reached path.",
        "- Use `behaviorally_equivalent` when the mutant does not meaningfully change behavior for the reached path.",
        "- Use `out_of_domain` when the mutant creates behavior outside the source contract or benchmark intent.",
        "- Use `unsure` when you cannot confidently judge mutant validity.",
        "",
        "`oracle_label_verification`: this checks the analyzer's outcome interpretation, not whether the program run occurred. Choose one of `analyzer_correct`, `should_be_oracle_kill`, `should_be_incidental_kill`, `should_be_survivor`, `should_be_not_scorable`, or `unsure`.",
        "",
        "The raw mutant outcome comes from a program run, but the oracle-versus-incidental label is inferred from logs and failure types, so this field verifies that interpretation. For killed rows, inspect whether the test failed through an assertion/expected-exception oracle or by an unrelated crash. For survived rows, `analyzer_correct` is usually sufficient unless the item should be not-scorable.",
        "",
        "## Strata In Sample",
        "",
        "| Stratum | Survived | Oracle Killed | Candidate Survived | Candidate Oracle Killed |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    selected_counts = Counter((row["stratum"], row["analyzer_outcome"]) for row in rows)
    for stratum in RETURN_RULE_STRATA + EXCEPTION_STRATA:
        lines.append(
            f"| {stratum} | {selected_counts[(stratum, 'survived')]} | {selected_counts[(stratum, 'killed_by_oracle')]} | "
            f"{candidate_counts[(stratum, 'survived')]} | {candidate_counts[(stratum, 'killed_by_oracle')]} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_kappa_script(path: Path) -> None:
    script = r'''#!/usr/bin/env python3
import csv
import sys
from collections import Counter


def read_labels(path, column):
    rows = {}
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            label = (row.get(column) or "").strip()
            if label:
                rows[row["sample_id"]] = label
    return rows


def kappa(a, b):
    common = sorted(set(a) & set(b))
    if not common:
        return None, 0, {}
    labels = sorted(set(a[i] for i in common) | set(b[i] for i in common))
    agree = sum(1 for i in common if a[i] == b[i])
    po = agree / len(common)
    ca = Counter(a[i] for i in common)
    cb = Counter(b[i] for i in common)
    pe = sum((ca[label] / len(common)) * (cb[label] / len(common)) for label in labels)
    value = (po - pe) / (1 - pe) if pe != 1 else (1.0 if po == 1 else None)
    return value, len(common), {"observed_agreement": po, "expected_agreement": pe, "labels": labels}


def main():
    if len(sys.argv) != 3:
        print("usage: compute_kappa.py rater1_labels.csv rater2_labels.csv")
        return 2
    for column in ["mutant_validity_label", "oracle_label_verification"]:
        a = read_labels(sys.argv[1], column)
        b = read_labels(sys.argv[2], column)
        value, n, meta = kappa(a, b)
        print(f"{column}: n={n} kappa={value}")
        print(meta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def write_html(path: Path) -> None:
    html_text = """<!doctype html>
<meta charset="utf-8">
<title>Assertion Mutation Manual Validation</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:0;background:#f7f7f5;color:#1f2328}
header{position:sticky;top:0;background:#fff;border-bottom:1px solid #ddd;padding:10px 18px;z-index:2;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
main{display:grid;grid-template-columns:320px 1fr;gap:0;min-height:calc(100vh - 55px)}
#list{border-right:1px solid #ddd;background:#fff;overflow:auto;max-height:calc(100vh - 55px)}
.item{padding:10px 12px;border-bottom:1px solid #eee;cursor:pointer}
.item.active{background:#e9f2ff}
.item.done b::after{content:" done";font-size:11px;color:#0969da;font-weight:500}
.item b{display:block}
.meta{color:#57606a;font-size:12px}
#detail{padding:18px;overflow:auto}
pre{background:#fff;border:1px solid #ddd;border-radius:6px;padding:12px;overflow:auto;line-height:1.35}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.pill{display:inline-block;border:1px solid #bbb;border-radius:999px;padding:2px 8px;margin:2px;background:#fff;font-size:12px}
mark{background:#fff4a3;padding:0 2px}
.controls{background:#fff;border:1px solid #ddd;border-radius:6px;padding:12px;margin:12px 0;display:grid;gap:10px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.row strong{min-width:155px}
button{border:1px solid #b7bdc5;background:#fff;border-radius:6px;padding:6px 10px;cursor:pointer}
button.selected{background:#0969da;color:#fff;border-color:#0969da}
button.primary{background:#1f883d;color:#fff;border-color:#1f883d}
textarea{width:100%;min-height:68px;border:1px solid #bbb;border-radius:6px;padding:8px;font:inherit}
.hint{font-size:12px;color:#57606a}
</style>
<header><b>Assertion Mutation Manual Validation</b> <span id="count"></span><button onclick="loadAdjacentLabels(true)">Load labels.csv</button><button class="primary" onclick="downloadCsv()">Download labels.csv</button><span id="labelStatus" class="hint">Labels are saved in this browser and exported with the button.</span></header>
<main><aside id="list"></aside><section id="detail"></section></main>
<script>
let samples=[];
let currentIndex=0;
const labelStoreKey=`assertion-mutation-labels:${location.pathname}`;
let labels=JSON.parse(localStorage.getItem(labelStoreKey)||"{}");
const validityLabels=["genuine","behaviorally_equivalent","out_of_domain","unsure"];
const oracleLabels=["analyzer_correct","should_be_oracle_kill","should_be_incidental_kill","should_be_survivor","should_be_not_scorable","unsure"];
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"}[c]));
function inferredRater(){ return location.pathname.includes("reviewer2") ? "rater2" : (location.pathname.includes("reviewer1") ? "rater1" : ""); }
function currentLabel(s){ return labels[s.sample_id] || {sample_fingerprint:s.sample_fingerprint,rater_id:inferredRater(),mutant_validity_label:"",oracle_label_verification:"",notes:""}; }
function saveLabel(sampleId, patch){
  const s=samples.find(x=>x.sample_id===sampleId);
  labels[sampleId]={...currentLabel(s),...patch,sample_fingerprint:s.sample_fingerprint};
  localStorage.setItem(labelStoreKey,JSON.stringify(labels));
  renderList();
  show(currentIndex);
}
function saveNotes(sampleId, value){
  const s=samples.find(x=>x.sample_id===sampleId);
  labels[sampleId]={...currentLabel(s),notes:value,sample_fingerprint:s.sample_fingerprint};
  localStorage.setItem(labelStoreKey,JSON.stringify(labels));
  renderList();
}
function buttonGroup(s, field, options){
  const label=currentLabel(s);
  return options.map(value=>`<button class="${label[field]===value?'selected':''}" onclick="saveLabel('${esc(s.sample_id)}',{${field}: '${esc(value)}'})">${esc(value)}</button>`).join("");
}
function normalizeText(s){return String(s||"").toLowerCase().replace(/[^a-z0-9]+/g," ").trim();}
function shouldHighlight(line,s){
  if(line.includes(">>>")) return true;
  const normalizedLine=normalizeText(line);
  if(!normalizedLine) return false;
  for(const term of (s.test_highlight_terms||[])){
    const normalizedTerm=normalizeText(term);
    if(normalizedTerm.length>=12 && (normalizedLine.includes(normalizedTerm) || normalizedTerm.includes(normalizedLine))) return true;
  }
  if(s.analyzer_outcome==="killed_by_oracle" && /assert|check|throw new assertionerror|subject\\./i.test(line)) return true;
  return false;
}
function renderPre(text,s,kind){
  return String(text||"").split("\\n").map(line=>{
    const escaped=esc(line);
    if(kind==="source" && line.includes(">>>")) return `<mark>${escaped}</mark>`;
    if(kind==="test" && shouldHighlight(line,s)) return `<mark>${escaped}</mark>`;
    return escaped;
  }).join("\\n");
}
function renderList(){
  const list=document.getElementById("list");
  const done=samples.filter(s=>currentLabel(s).mutant_validity_label && currentLabel(s).oracle_label_verification).length;
  document.getElementById("count").textContent=`${done}/${samples.length} labeled`;
  list.innerHTML=samples.map((s,i)=>{
    const label=currentLabel(s);
    const doneClass=label.mutant_validity_label && label.oracle_label_verification ? "done" : "";
    return `<div class="item ${i===currentIndex?'active':''} ${doneClass}" onclick="show(${i})"><b>${esc(s.sample_id)} ${esc(s.stratum)}</b><span class="meta">${esc(s.analyzer_outcome)} · ${esc(s.task_id)}</span></div>`;
  }).join("");
}
function show(i){
  currentIndex=i;
  document.querySelectorAll(".item").forEach((el,j)=>el.classList.toggle("active",i===j));
  const s=samples[i];
  const label=currentLabel(s);
  document.getElementById("detail").innerHTML=`
    <h2>${esc(s.sample_id)} ${esc(s.stratum)}</h2>
    <div><span class="pill">${esc(s.analyzer_outcome)}</span><span class="pill">${esc(s.operator_rule)}</span><span class="pill">${esc(s.prompt_template)}</span></div>
    <p><b>Task:</b> ${esc(s.task_id)}<br><b>Signature:</b> ${esc(s.signature)}<br><b>Mutant:</b> ${esc(s.mutant_text)}</p>
    <div class="controls">
      <div class="row"><strong>Mutant validity</strong>${buttonGroup(s,"mutant_validity_label",validityLabels)}</div>
      <div class="row"><strong>Outcome check</strong>${buttonGroup(s,"oracle_label_verification",oracleLabels)}</div>
      <label><strong>Notes</strong><textarea oninput="saveNotes('${esc(s.sample_id)}',this.value)">${esc(label.notes||"")}</textarea></label>
      <div class="row"><button onclick="show(Math.max(0,currentIndex-1))">Previous</button><button onclick="show(Math.min(samples.length-1,currentIndex+1))">Next</button></div>
    </div>
    <div class="grid"><div><h3>Full Source</h3><pre>${renderPre(s.source_window,s,"source")}</pre></div><div><h3>Generated Test</h3><pre>${renderPre(s.test_window,s,"test")}</pre></div></div>
    ${s.execute_log_excerpt ? `<h3>Mutant Execute Log</h3><pre>${esc(s.execute_log_excerpt)}</pre>` : ""}
  `;
}
function csvEscape(value){
  const s=String(value??"");
  return /[",\\n]/.test(s) ? `"${s.replace(/"/g,'""')}"` : s;
}
function parseCsv(text){
  const rows=[];
  let row=[];
  let field="";
  let inQuotes=false;
  for(let i=0;i<text.length;i++){
    const ch=text[i];
    if(inQuotes){
      if(ch==='"' && text[i+1]==='"'){field+='"';i++;}
      else if(ch==='"'){inQuotes=false;}
      else{field+=ch;}
    }else if(ch==='"'){
      inQuotes=true;
    }else if(ch===","){
      row.push(field);field="";
    }else if(ch==="\\n"){
      row.push(field);field="";
      rows.push(row);row=[];
    }else if(ch==="\\r"){
      continue;
    }else{
      field+=ch;
    }
  }
  if(field || row.length){row.push(field);rows.push(row);}
  if(!rows.length) return [];
  const headers=rows.shift().map(h=>h.trim());
  return rows.filter(r=>r.some(v=>String(v).trim())).map(r=>{
    const obj={};
    headers.forEach((h,i)=>{obj[h]=r[i]??"";});
    return obj;
  });
}
function importLabelRows(rows, overwrite){
  const sampleById=Object.fromEntries(samples.map(s=>[s.sample_id,s]));
  let imported=0;
  let skipped=0;
  for(const row of rows){
    const sample=sampleById[row.sample_id];
    if(!sample){skipped++;continue;}
    if(row.sample_fingerprint && row.sample_fingerprint!==sample.sample_fingerprint){skipped++;continue;}
    const existing=currentLabel(sample);
    const hasExisting=existing.mutant_validity_label || existing.oracle_label_verification || existing.notes;
    if(hasExisting && !overwrite){skipped++;continue;}
    labels[row.sample_id]={
      sample_fingerprint:sample.sample_fingerprint,
      rater_id:row.rater_id || inferredRater(),
      mutant_validity_label:row.mutant_validity_label || "",
      oracle_label_verification:row.oracle_label_verification || "",
      notes:row.notes || "",
    };
    imported++;
  }
  localStorage.setItem(labelStoreKey,JSON.stringify(labels));
  renderList();
  show(currentIndex);
  document.getElementById("labelStatus").textContent=`Loaded ${imported} labels from labels.csv${skipped ? `; skipped ${skipped}` : ""}.`;
}
async function loadAdjacentLabels(overwrite){
  try{
    const response=await fetch("labels.csv",{cache:"no-store"});
    if(!response.ok) throw new Error(`HTTP ${response.status}`);
    importLabelRows(parseCsv(await response.text()), overwrite);
  }catch(error){
    document.getElementById("labelStatus").textContent=`Could not load labels.csv: ${error.message}`;
  }
}
function downloadCsv(){
  const cols=["sample_id","sample_fingerprint","stratum","analyzer_outcome","construct","operator_rule","return_subcategory","subsumed_by_broad_original","task_id","model_id","prompt_template","signature","source_line","source_snippet","mutant_text","failure_type","execute_log","rater_id","mutant_validity_label","oracle_label_verification","notes"];
  const lines=[cols.join(",")];
  for(const s of samples){
    const label=currentLabel(s);
    const row={...s,...label,rater_id:label.rater_id||inferredRater()};
    lines.push(cols.map(c=>csvEscape(row[c])).join(","));
  }
  const blob=new Blob([lines.join("\\n")+"\\n"],{type:"text/csv"});
  const a=document.createElement("a");
  a.href=URL.createObjectURL(blob);
  a.download="labels.csv";
  a.click();
  URL.revokeObjectURL(a.href);
}
fetch("samples.json").then(r=>r.json()).then(data=>{
  samples=data.samples;
  renderList();
  show(0);
  if(!Object.values(labels).some(label=>label.mutant_validity_label || label.oracle_label_verification || label.notes)){
    loadAdjacentLabels(false);
  }
});
</script>
"""
    path.write_text(html_text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a stratified manual validation sample for assertion mutation.")
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "analysis/manual-validation/assertion-mutation-v11c")
    parser.add_argument("--total", type=int, default=50)
    parser.add_argument("--seed", type=int, default=20260909)
    args = parser.parse_args()

    rows = load_rows(args.inputs)
    candidates = collect_candidates(rows)
    candidate_counts = Counter((row["stratum"], row["analyzer_outcome"]) for row in candidates)
    sample = attach_review_context(pick_balanced(candidates, args.total, args.seed))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_json(args.out_dir / "samples.json", sample)
    write_csv(args.out_dir / "annotation_sheet.csv", sample)
    write_csv(args.out_dir / "rater1_labels.csv", sample, "rater1")
    write_csv(args.out_dir / "rater2_labels.csv", sample, "rater2")
    write_codebook(args.out_dir / "codebook.md", sample, candidate_counts)
    write_kappa_script(args.out_dir / "compute_kappa.py")
    write_html(args.out_dir / "review.html")
    print(f"candidates: {len(candidates)}")
    print(f"samples: {len(sample)}")
    print(f"wrote {args.out_dir}")
    for key, value in sorted(Counter((row["stratum"], row["analyzer_outcome"]) for row in sample).items()):
        print(key, value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
