# classifier.py - The classification "brain", separated from the graph.
#
# agent.py (the LangGraph app) and eval/run_eval.py (the evaluation
# harness) must use the SAME prompts and the SAME output parsing —
# otherwise the eval measures a different system than the one running.
# Pulling these out of agent.py also means the eval can import them
# without triggering agent.py's Postgres connection.

import json
import re

MAX_ATTEMPTS = 3

# THE CONTRACT: one written definition of severity that BOTH sides of the
# eval obey — the human labeling golden_dataset.csv and the model reading
# this prompt. Change it here and re-audit the golden labels together;
# they must never drift apart, or accuracy numbers stop meaning anything.
SEVERITY_RUBRIC = """Severity definitions — apply these EXACTLY:
- critical: the system stopped working or users are affected; a request failed
  in a way that breaks the pipeline behind it; what was applied could not be
  carried out; a failure needing immediate action.
- warning: the system is at risk or degraded but still working; an operation
  was carried out only partially or the response has inconsistencies;
  a transgression on the system's end; needs attention, but nothing is down.
- info: routine computational noise containing information about the system;
  no action needed; ignoring it has no consequences.

Judge severity from the OUTCOME the log describes (stopped / failed /
partial / at risk / routine), not from how technical or alarming the
vocabulary sounds. Unfamiliar vocabulary is never a reason to escalate;
when genuinely torn between two severities, pick the lower one.

Category rule: choose auth/network/database/deployment/federation ONLY when
the log's vocabulary clearly belongs to that domain. If the log is about
something outside these domains, return category "unknown" — an honest
"unknown" is the CORRECT answer; a confident wrong guess is a failure."""

JSON_SHAPE = """{
    "severity": "critical|warning|info|unknown",
    "likely_cause": "brief cause",
    "suggested_action": "action to fix",
    "category": "auth|network|database|deployment|federation|unknown"
}"""


def build_classify_prompt(log: dict, attempt: int) -> str:
    """Each retry uses a BETTER prompt — that's why looping is worth it.

    Attempt 1: message only (cheap, works for obvious logs).
    Attempt 2: add metadata (level, user, time) and keyword hints.
    Attempt 3: add few-shot examples to anchor the categories.
    """
    if attempt == 1:
        return f"""Classify this log. Return ONLY JSON:
{JSON_SHAPE}
{SEVERITY_RUBRIC}
Use "unknown" ONLY if you genuinely cannot tell.

Log: {log['message']}"""

    if attempt == 2:
        return f"""Classify this log. Return ONLY JSON:
{JSON_SHAPE}
{SEVERITY_RUBRIC}
Use "unknown" ONLY if you genuinely cannot tell.

Use ALL the context below. Hints: auth = logins/tokens/OIDC/MAS, network = timeouts/DNS/SSL,
database = Postgres/queries/connections, deployment = disk/memory/docker/infra,
federation = Synapse/matrix server-to-server traffic.

Level: {log['level']}
Reported by: {log['user_name']}
Time: {log['timestamp']}
Message: {log['message']}"""

    # attempt 3 — few-shot examples
    return f"""Classify this log. Return ONLY JSON:
{JSON_SHAPE}
{SEVERITY_RUBRIC}
Severity: never "unknown" — the rubric always decides one.
Category: pick the closest matching domain, but if the log is genuinely
outside all five domains, "unknown" is the correct answer. Examples:

"OIDC token validation failed" -> {{"severity": "critical", "category": "auth"}}
"SSL certificate expired" -> {{"severity": "warning", "category": "network"}}
"Slow query took 12s" -> {{"severity": "warning", "category": "database"}}
"Disk space at 95%" -> {{"severity": "critical", "category": "deployment"}}
"Failed to send event to matrix.org" -> {{"severity": "warning", "category": "federation"}}
"3D printer nozzle temperature fluctuating beyond tolerance" -> {{"severity": "warning", "category": "unknown"}}

Level: {log['level']}
Reported by: {log['user_name']}
Message: {log['message']}"""


def needs_retry(classification: dict) -> bool:
    """THE retry contract, in one place — agent router, eval, and replay
    all call this, so the policy can never drift between them.

    Severity "unknown" means something went wrong: the rubric always
    decides a severity, and parse failures set both fields to unknown.
    Category "unknown" is a legitimate abstention — never retried.
    """
    return classification.get("severity") == "unknown"


def parse_classification(text: str) -> dict:
    """Turn raw model output into a classification dict.

    Malformed / empty output is normalized to "unknown", so the caller's
    retry logic treats it the same as a genuine "I can't tell".
    """
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        classification = json.loads(match.group()) if match else {}
    except Exception:
        classification = {}

    if classification.get("severity") not in ("critical", "warning", "info"):
        classification["severity"] = "unknown"
    if not classification.get("category"):
        classification["category"] = "unknown"
    return classification
