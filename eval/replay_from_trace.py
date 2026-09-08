# eval/replay_from_trace.py - Score the eval OFFLINE from the trace file.
#
# When a run dies mid-way (rate limits, 503s, Ctrl-C), the completed
# Gemini responses aren't lost: tracked_generate logged every prompt AND
# response to eval_trace.jsonl. This script replays those responses
# through the same parse_classification + retry logic — zero API calls,
# zero cost — and reports whatever rows the trace can reconstruct.
#
# This is observability paying for itself: a trace is not just for
# debugging, it's a durable record of work you already paid for.
#
# Usage (from the LogClassifier directory):
#   python eval/replay_from_trace.py

import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from run_eval import load_golden, write_and_report  # noqa: E402
from classifier import MAX_ATTEMPTS, needs_retry, parse_classification  # noqa: E402

TRACE_FILE = os.path.join(HERE, "eval_trace.jsonl")


def load_traced_responses():
    """Latest successful response per (golden_id, attempt).

    The trace may hold several runs of the same row (an aborted run, then
    a retry) — "latest wins" because later lines were appended later.
    Failed calls (error set, no response) are skipped: they carry no answer.
    """
    latest = {}
    with open(TRACE_FILE, encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if rec.get("purpose") != "eval_classify":
                continue
            if rec.get("error") or not rec.get("response"):
                continue
            latest[(str(rec.get("golden_id")), rec.get("attempt"))] = rec["response"]
    return latest


def main():
    golden, _skipped = load_golden()
    responses = load_traced_responses()

    results, unrecovered = [], []
    for row in golden:
        # Replay the graph's retry loop, but reading answers from the
        # trace instead of the API. The loop stops exactly where the
        # live run stopped: on a confident answer, or when the trace
        # has no record of the next attempt.
        pred, attempts = None, 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            text = responses.get((row["id"], attempt))
            if text is None:
                break
            pred, attempts = parse_classification(text), attempt
            if not needs_retry(pred):
                break
        if pred is None:
            unrecovered.append(row["id"])
            continue
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
            "error": False,
        })

    print(f"Replayed from trace: {len(results)} rows recovered, "
          f"{len(unrecovered)} not in trace (never ran or all calls failed)")
    if not results:
        print("Nothing to score — trace holds no successful eval_classify calls.")
        return
    write_and_report(results)
    print("\n  eval cost: $0.0000 (replayed from trace — no API calls)")
    print("  ⚠ PARTIAL RESULT: rows not recovered:",
          ", ".join(unrecovered) if unrecovered else "none")


if __name__ == "__main__":
    main()
