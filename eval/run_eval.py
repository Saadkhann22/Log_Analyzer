# eval/run_eval.py - Score the classifier against the golden dataset.
#
# The rule this file lives by: MEASURE THE SYSTEM YOU ACTUALLY RUN.
# It imports the prompts + parsing from classifier.py (the same code
# agent.py uses) and reproduces the graph's retry loop exactly:
# attempt 1 -> retry with a better prompt while severity is "unknown",
# up to MAX_ATTEMPTS. (Category "unknown" is a valid abstention, not a
# retry trigger.) No database needed.
#
# Usage (from the LogClassifier directory):
#   python eval/run_eval.py
#
# Outputs:
#   - console summary (accuracy, confusion, attempts, cost)
#   - eval/results.csv        one row per golden log, for inspection
#   - eval/eval_trace.jsonl   every Gemini call (same JSONL tracing as the app)

import csv
import os
import sys
import time
from collections import Counter

# Windows consoles often default to cp1252, which can't print the emoji
# in our progress lines (or llm.py's). Force UTF-8, degrade gracefully.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# Keep eval calls out of the app's trace file — but use the SAME tracer.
# Must be set before importing llm (it reads the env var at import time).
os.environ.setdefault("LLM_TRACE_FILE", os.path.join(HERE, "eval_trace.jsonl"))

from llm import tracked_generate                      # noqa: E402
from classifier import (                              # noqa: E402
    MAX_ATTEMPTS, build_classify_prompt, needs_retry, parse_classification,
)

MODEL = os.getenv("CLASSIFICATION_MODEL", "models/gemini-3.5-flash")

GOLDEN_CSV = os.path.join(HERE, "golden_dataset.csv")
RESULTS_CSV = os.path.join(HERE, "results.csv")

# The golden CSV is a spreadsheet export with padding columns.
# The real data sits at these fixed positions:
COL_ID, COL_LEVEL, COL_MESSAGE, COL_NOTE, COL_SEVERITY, COL_CATEGORY = 0, 1, 5, 8, 14, 16

VALID_SEVERITIES = {"critical", "warning", "info", "unknown"}
VALID_CATEGORIES = {"auth", "network", "database", "deployment", "federation", "unknown"}


def load_golden():
    """Read the golden CSV. Returns (usable_rows, skipped) — and we always
    REPORT what was skipped: silently dropping rows is how evals lie."""
    rows, skipped = [], []
    with open(GOLDEN_CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader)  # header
        for raw in reader:
            raw += [""] * (17 - len(raw))  # pad short rows
            row = {
                "id": raw[COL_ID].strip(),
                "level": raw[COL_LEVEL].strip(),
                "message": raw[COL_MESSAGE].strip(),
                "note": raw[COL_NOTE].strip(),
                "severity": raw[COL_SEVERITY].strip().lower(),
                "category": raw[COL_CATEGORY].strip().lower(),
            }
            if not row["message"]:
                skipped.append((row["id"], "no message"))
            elif not row["severity"] or not row["category"]:
                skipped.append((row["id"], "no golden labels"))
            elif row["severity"] not in VALID_SEVERITIES:
                skipped.append((row["id"], f"bad severity label {row['severity']!r}"))
            elif row["category"] not in VALID_CATEGORIES:
                skipped.append((row["id"], f"bad category label {row['category']!r}"))
            else:
                rows.append(row)
    return rows, skipped


def generate_with_backoff(prompt: str, **meta):
    """One Gemini call with exponential backoff (10 -> 20 -> 40 -> 80s).

    503s come in bursts lasting minutes, so an immediate retry is wasted —
    each wait doubles. Returns the text, or None if every retry failed.
    None means "the API was down", which is NOT the same as "" (the model
    answered garbage) — the caller must keep those two cases apart.
    """
    for wait in (10, 20, 40, 80, None):
        try:
            return tracked_generate(MODEL, prompt, purpose="eval_classify", **meta)
        except Exception as e:
            if wait is None:
                print(f"      ⚠ giving up after all retries ({type(e).__name__})")
                return None
            print(f"      ⚠ call failed ({type(e).__name__}), retrying in {wait}s...")
            time.sleep(wait)


def classify_with_retries(row: dict) -> tuple[dict, int, bool]:
    """Mirror of the graph's classify loop (route_after_classify):
    retry while severity is unknown, up to MAX_ATTEMPTS.

    Returns (classification, attempts, errored). errored=True means the
    API failed us, not the model — no point burning better prompts on a
    dead server, so we stop the row immediately.
    """
    log = {  # the shape build_classify_prompt expects; stub what the CSV lacks
        "message": row["message"],
        "level": row["level"],
        "user_name": "eval_harness",
        "timestamp": "2026-08-18T00:00:00",
    }
    classification, attempt = {}, 0
    while attempt < MAX_ATTEMPTS:
        attempt += 1
        text = generate_with_backoff(build_classify_prompt(log, attempt),
                                     golden_id=row["id"], attempt=attempt)
        if text is None:
            return {"severity": "unknown", "category": "unknown"}, attempt, True
        classification = parse_classification(text)
        if not needs_retry(classification):
            break
    return classification, attempt, False


def main():
    golden, skipped = load_golden()
    print(f"Golden dataset: {len(golden)} usable rows, {len(skipped)} skipped")
    for row_id, reason in skipped:
        print(f"   ⏭ skipped id={row_id}: {reason}")

    trace_file = os.environ["LLM_TRACE_FILE"]
    trace_lines_before = 0
    if os.path.exists(trace_file):
        with open(trace_file, encoding="utf-8") as f:
            trace_lines_before = sum(1 for _ in f)

    results, consecutive_errors = [], 0
    for i, row in enumerate(golden, 1):
        print(f"[{i}/{len(golden)}] id={row['id']} {row['message'][:60]}...")
        pred, attempts, errored = classify_with_retries(row)
        results.append({
            "id": row["id"],
            "message": row["message"],
            "note": row["note"],
            "golden_severity": row["severity"],
            "pred_severity": pred["severity"],
            "severity_ok": pred["severity"] == row["severity"],
            "golden_category": row["category"],
            "pred_category": pred["category"],
            "category_ok": pred["category"] == row["category"],
            "attempts": attempts,
            "error": errored,
        })
        # Circuit breaker: if the API is down, 3 rows of proof is plenty —
        # a run that limps on produces numbers no one should trust.
        consecutive_errors = consecutive_errors + 1 if errored else 0
        if consecutive_errors >= 3:
            print("\n🛑 3 rows errored in a row — API looks down, aborting run.")
            print("   Wait a few minutes and rerun. Partial results NOT saved.")
            return

    write_and_report(results)

    # What did this eval run cost? Read it back from the trace we just wrote.
    calls, cost, tokens = 0, 0.0, 0
    with open(trace_file, encoding="utf-8") as f:
        import json
        for line_no, line in enumerate(f):
            if line_no < trace_lines_before:
                continue
            rec = json.loads(line)
            calls += 1
            cost += rec.get("cost_usd") or 0
            tokens += rec.get("total_tokens") or 0
    print(f"\n  eval cost: {calls} Gemini calls, {tokens} tokens, ${cost:.4f}")


def write_and_report(results):
    """Write results.csv and print the scoreboard. Shared with
    replay_from_trace.py so a live run and an offline replay report
    their numbers in exactly the same format."""
    with open(RESULTS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)

    # ---- summary ----
    # Errored rows measure Google's uptime, not our prompts — keep them
    # OUT of the accuracy denominator and report them separately.
    errored = [r for r in results if r["error"]]
    scored = [r for r in results if not r["error"]]
    n = len(scored)
    if n == 0:
        print("\nNo rows scored (all errored) — nothing to report.")
        return
    sev_ok = sum(r["severity_ok"] for r in scored)
    cat_ok = sum(r["category_ok"] for r in scored)
    both_ok = sum(r["severity_ok"] and r["category_ok"] for r in scored)

    print("\n" + "=" * 60)
    print(f"RESULTS: {n} logs scored, {len(errored)} errored  (model: {MODEL})")
    if errored:
        print(f"  ⚠ errored (excluded): ids {', '.join(r['id'] for r in errored)}")
    print(f"  severity accuracy : {sev_ok}/{n}  ({sev_ok / n:.0%})")
    print(f"  category accuracy : {cat_ok}/{n}  ({cat_ok / n:.0%})")
    print(f"  exact match (both): {both_ok}/{n}  ({both_ok / n:.0%})")
    print(f"  avg attempts/log  : {sum(r['attempts'] for r in scored) / n:.2f}")

    # Per-class accuracy: which golden classes does the model struggle with?
    for field in ("severity", "category"):
        per_class = {}
        for r in scored:
            g = r[f"golden_{field}"]
            hit, total = per_class.get(g, (0, 0))
            per_class[g] = (hit + r[f"{field}_ok"], total + 1)
        print(f"\n  {field} accuracy by golden label:")
        for label, (hit, total) in sorted(per_class.items()):
            print(f"    {label:<12} {hit}/{total}")

    # Confusion: the actual (golden -> predicted) mistakes, most common first.
    print("\n  top confusions (golden -> predicted):")
    confusions = Counter(
        (field, r[f"golden_{field}"], r[f"pred_{field}"])
        for r in scored for field in ("severity", "category")
        if not r[f"{field}_ok"]
    )
    for (field, g, p), count in confusions.most_common(10):
        print(f"    {field}: {g} -> {p}  x{count}")

    print(f"\nPer-row detail written to {RESULTS_CSV}")


if __name__ == "__main__":
    main()
