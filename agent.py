# agent.py - Log analysis agent built with LangGraph
#
# The graph:
#
#   START ─▶ fetch_log ──(not found?)──▶ END
#                 │
#                 ▼
#          ┌─▶ classify (Gemini)
#          │      │
#          │      ├── "unknown" and attempts < 3 ─┐    ← THE LOOP: a cycle back to
#          └──────┴───────────────────────────────┘      the SAME node, but each
#                 │                                      attempt uses a better prompt
#                 ├── critical/warning ─▶ find_similar (pgvector) ─┐
#                 └── info / still unknown ────────────────────────┤
#                                                                  ▼
#                                                    human_approval ⏸ INTERRUPT
#                                                    (graph pauses here and waits)
#                                                          │
#                                                          ├── approved ─▶ write_report ─▶ END
#                                                          └── rejected ─▶ END

import os
import re
import json
from typing import Optional, List, Dict, Any

from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt, Command
from google import genai
from dotenv import load_dotenv

from db import db

load_dotenv()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = os.getenv("CLASSIFICATION_MODEL", "models/gemini-3.5-flash")


# ============================================
# 1. STATE — the shared notebook
# ============================================
# One dict that flows through the graph. Starts with only log_id
# filled in; each node fills in more.

class AgentState(TypedDict):
    log_id: int                               # input
    log: Optional[Dict[str, Any]]             # filled by fetch_log
    classification: Optional[Dict[str, Any]]  # filled by classify
    attempts: int                             # how many times classify has run
    similar_logs: List[Dict[str, Any]]        # filled by find_similar
    approved: Optional[bool]                  # filled by human_approval
    report: str                               # filled by write_report
    error: Optional[str]                      # set by fetch_log on failure


# ============================================
# 2. NODES — plain functions: state in, partial update out
# ============================================
# Each node returns ONLY the keys it changed; LangGraph merges them.

def fetch_log(state: AgentState) -> dict:
    """Load the log row from Postgres."""
    rows = db.execute(
        "SELECT id, timestamp, level, message, user_name FROM logs WHERE id = %s",
        [state["log_id"]],
    )
    if not rows:
        return {"error": f"Log {state['log_id']} not found"}
    log = dict(rows[0])
    log["timestamp"] = str(log["timestamp"])  # JSON-friendly
    return {"log": log}


MAX_ATTEMPTS = 3

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
Use "unknown" ONLY if you genuinely cannot tell.

Log: {log['message']}"""

    if attempt == 2:
        return f"""Classify this log. Return ONLY JSON:
{JSON_SHAPE}
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
Avoid "unknown" — pick the closest match. Examples:

"OIDC token validation failed" -> {{"severity": "critical", "category": "auth"}}
"SSL certificate expired" -> {{"severity": "warning", "category": "network"}}
"Slow query took 12s" -> {{"severity": "warning", "category": "database"}}
"Disk space at 95%" -> {{"severity": "critical", "category": "deployment"}}
"Failed to send event to matrix.org" -> {{"severity": "warning", "category": "federation"}}

Level: {log['level']}
Reported by: {log['user_name']}
Message: {log['message']}"""


def classify(state: AgentState) -> dict:
    """Ask Gemini to classify the log.

    This node can run up to MAX_ATTEMPTS times: the router sends the
    state back here whenever the result is "unknown". Because `attempts`
    lives in the state, the node knows which prompt to use this round.
    """
    attempt = state.get("attempts", 0) + 1
    prompt = build_classify_prompt(state["log"], attempt)
    try:
        response = client.models.generate_content(model=MODEL, contents=prompt)
        match = re.search(r"\{.*\}", response.text, re.DOTALL)
        classification = json.loads(match.group()) if match else {}
    except Exception:
        classification = {}

    # Malformed / empty output is treated the same as "unknown",
    # so it also gets retried with a better prompt.
    if classification.get("severity") not in ("critical", "warning", "info"):
        classification.setdefault("severity", "unknown")
    if not classification.get("category"):
        classification["category"] = "unknown"

    print(f"   🔁 classify attempt {attempt}: severity={classification.get('severity')}, "
          f"category={classification.get('category')}")
    return {"classification": classification, "attempts": attempt}


def find_similar(state: AgentState) -> dict:
    """pgvector cosine search for the 3 most similar past logs.

    Only runs when the router decided the log is serious.
    """
    emb = db.execute(
        "SELECT embedding FROM log_embeddings WHERE log_id = %s", [state["log_id"]]
    )
    if not emb:
        return {"similar_logs": []}  # no embedding stored yet — skip gracefully

    target = emb[0]["embedding"]
    rows = db.execute(
        """
        SELECT l.id, l.level, l.message,
               1 - (le.embedding <=> %s) AS similarity
        FROM logs l
        JOIN log_embeddings le ON l.id = le.log_id
        WHERE l.id != %s
        ORDER BY le.embedding <=> %s
        LIMIT 3
        """,
        [target, state["log_id"], target],
    )
    return {"similar_logs": [dict(r) for r in rows]}


def human_approval(state: AgentState) -> dict:
    """Human-in-the-loop node.

    interrupt() PAUSES the whole graph right here. The payload we pass
    is handed back to the caller (API / CLI) so the human can see what
    they're approving. The graph then sits frozen — state safely stored
    in the checkpointer — until someone resumes it with
    Command(resume=<value>).

    When resumed, this node runs AGAIN from the top, but this time
    interrupt() doesn't pause: it returns the resume value.
    """
    decision = interrupt({
        "question": "Generate an incident report for this log?",
        "log": state["log"]["message"],
        "classification": state["classification"],
        "similar_logs_found": len(state["similar_logs"]),
        "attempts": state["attempts"],  # how many classify rounds it took
    })
    if not decision:
        return {"approved": False,
                "report": "(rejected by operator — no report generated)"}
    return {"approved": True}


def write_report(state: AgentState) -> dict:
    """Final node: it can see log + classification + similar_logs because
    every previous node wrote into the SAME shared state."""
    similar_text = "\n".join(
        f"- [{s['level']}] {s['message']} (similarity {s['similarity']:.2f})"
        for s in state.get("similar_logs", [])
    ) or "(no similar-log data available for this log)"

    prompt = f"""You are an SRE assistant. Write a short incident report (max 5 sentences).

Log: {state['log']['message']}
Classification: {json.dumps(state['classification'])}
Similar past logs:
{similar_text}

Mention whether similar past incidents exist and what to do next."""
    try:
        response = client.models.generate_content(model=MODEL, contents=prompt)
        report = response.text.strip()
    except Exception as e:
        report = f"Report generation failed: {e}"
    return {"report": report}


# ============================================
# 3. ROUTERS — pick the next node, change nothing
# ============================================
# A router returns the NAME of the next node (a string), not a result.

def route_after_fetch(state: AgentState) -> str:
    if state.get("error"):
        return END          # log didn't exist → stop the whole graph
    return "classify"


def route_after_classify(state: AgentState) -> str:
    c = state["classification"]
    is_unknown = c.get("severity") == "unknown" or c.get("category") == "unknown"

    # THE LOOP: returning "classify" from classify's own router = a cycle.
    # The attempts counter in state is what stops it running forever.
    if is_unknown and state["attempts"] < MAX_ATTEMPTS:
        return "classify"

    if c.get("severity") in ("critical", "warning"):
        return "find_similar"     # serious → dig up similar past incidents
    return "human_approval"       # info / still unknown → straight to approval


def route_after_approval(state: AgentState) -> str:
    if state["approved"]:
        return "write_report"
    return END                    # rejected → stop, report stays as the rejection note


# ============================================
# 4. BUILD THE GRAPH
# ============================================

builder = StateGraph(AgentState)

# Register the workers (name → function)
builder.add_node("fetch_log", fetch_log)
builder.add_node("classify", classify)
builder.add_node("find_similar", find_similar)
builder.add_node("human_approval", human_approval)
builder.add_node("write_report", write_report)

# Wiring
builder.add_edge(START, "fetch_log")                          # entry
builder.add_conditional_edges("fetch_log", route_after_fetch)  # decision
builder.add_conditional_edges("classify", route_after_classify)  # decision
builder.add_edge("find_similar", "human_approval")            # plain edge
builder.add_conditional_edges("human_approval", route_after_approval)  # decision
builder.add_edge("write_report", END)                         # exit

# THE CHECKPOINTER: saves a snapshot of the state after every node.
# This is what makes interrupt() possible — the graph can pause and be
# resumed later because its state is stored outside the running process.
#
# Was:  checkpointer = MemorySaver()   ← RAM: paused runs die with the process
# Now:  SqliteSaver on a local file    ← disk: paused runs survive restarts
# (kill_test.py proves the difference; a Postgres checkpointer would be
# the same one-line idea for production)
import sqlite3
# In Docker, point CHECKPOINT_DB at a mounted volume (e.g. /data/checkpoints.db)
# — otherwise paused runs die with the container's writable layer.
CHECKPOINT_DB = os.getenv("CHECKPOINT_DB", "checkpoints.db")
sqlite_conn = sqlite3.connect(CHECKPOINT_DB, check_same_thread=False)
checkpointer = SqliteSaver(sqlite_conn)
graph = builder.compile(checkpointer=checkpointer)


# ============================================
# 5. RUN IT
# ============================================

# With a checkpointer, every run belongs to a THREAD. The thread_id is
# the key the checkpointer files the state snapshots under — it's how a
# resume call finds the exact paused run it should continue.

def start_analysis(log_id: int, thread_id: str) -> dict:
    """Run the graph until it finishes OR hits the interrupt.

    If it paused, the returned dict contains a "__interrupt__" key
    holding the payload that human_approval passed to interrupt().
    """
    config = {"configurable": {"thread_id": thread_id}}
    return graph.invoke({
        "log_id": log_id,
        "log": None,
        "classification": None,
        "attempts": 0,
        "similar_logs": [],
        "approved": None,
        "report": "",
        "error": None,
    }, config)


def resume_analysis(thread_id: str, approved: bool) -> dict:
    """Wake the paused graph up and hand it the human's decision.

    Command(resume=X) makes interrupt() inside human_approval return X.
    The graph then continues from that node to the end.
    """
    config = {"configurable": {"thread_id": thread_id}}
    return graph.invoke(Command(resume=approved), config)


if __name__ == "__main__":
    # Interactive test:  python agent.py 56
    # Runs until the interrupt, asks YOU in the terminal, then resumes.
    import sys
    log_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    thread_id = f"cli-log-{log_id}"

    result = start_analysis(log_id, thread_id)

    if "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        print("\n⏸  GRAPH PAUSED — waiting for human approval")
        print(json.dumps(payload, indent=2, default=str))
        answer = input("\nApprove report generation? [y/n] ")
        result = resume_analysis(thread_id, answer.strip().lower().startswith("y"))

    print("\n=== FINAL STATE ===")
    print(json.dumps(result, indent=2, default=str))
