# agent.py - Log analysis agent built with LangGraph
#
# The graph:
#
#   START ─▶ fetch_log ──(not found?)──▶ END
#                 │
#                 ▼
#            classify_agent (Gemini) — owns its retry loop internally
#                 │
#                 ├── critical/warning ─▶ find_similar (pgvector) ─┐
#                 └── info / unknown ──────────────────────────────┤
#                                                                  ▼
#                                                    human_approval ⏸ INTERRUPT
#                                                          │
#                                                     orchestrator
#                                                          ├── approved & critical/warning ─▶ write_report ─▶ END
#                                                          ├── approved & info/unknown ─────▶ skip_report ──▶ END
#                                                          └── rejected ────────────────────▶ END

import os
import sys
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, List, Dict, Any

import aiosqlite
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import interrupt, Command
from dotenv import load_dotenv
from langfuse import observe, propagate_attributes
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

# All data access goes through the MCP server — this file never touches
# Postgres directly, so importing it opens no DB connection.
from llm import tracked_generate
from classifier import (MAX_ATTEMPTS, build_classify_prompt,
                        needs_retry, parse_classification)

load_dotenv()

MODEL = os.getenv("CLASSIFICATION_MODEL", "models/gemini-3.5-flash")


# ============================================
# 0. MCP — this process is the HOST
# ============================================
# The agent spawns mcp_server.py as a subprocess and speaks JSON-RPC to it
# over stdio: the graph decides WHAT to fetch, the server owns HOW.

SERVER = StdioServerParameters(
    command=sys.executable,
    args=["mcp_server.py"],
    cwd=str(Path(__file__).parent),  # so the server subprocess finds its .env
)

# The live connection for the current graph run; mcp_session() owns its
# lifecycle. One session per run — each open costs a subprocess spawn +
# initialize handshake, so per-call would be wasteful.
_mcp: Optional[Client] = None


@asynccontextmanager
async def mcp_session():
    """Open the MCP connection for one graph run and publish it to the nodes."""
    global _mcp
    async with Client(SERVER) as client:
        _mcp = client
        try:
            yield
        finally:
            _mcp = None  # teardown kills the subprocess


async def call_mcp(tool: str, args: dict) -> Any:
    """tools/call + unwrap.

    A CallToolResult carries the payload twice: `content` (text blocks for
    an LLM) and `structured_content` (the original JSON for code) — we take
    the structured one. is_error=True is a protocol-level failure, distinct
    from a tool that RAN and returned {"error": ...} as data.
    """
    result = await _mcp.call_tool(tool, args)
    if result.is_error:
        raise RuntimeError(f"MCP tool '{tool}' failed: {result.content}")
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


# ============================================
# 1. STATE
# ============================================
# One dict that flows through the graph; each node fills in more keys.

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

@observe()
async def fetch_log(state: AgentState) -> dict:
    """Load the log row via the MCP server's get_log tool."""
    log = await call_mcp("get_log", {"log_id": state["log_id"]})
    if "error" in log:
        return {"error": log["error"]}
    return {"log": log}


# ---- SUB-AGENT: classify ----
# Standalone: classify_agent(log) -> {classification, attempts}. Owns the
# escalating-retry loop internally, so the orchestrator, the eval, or a REPL
# can all call it the same way. Built from classifier.py primitives so it
# can never drift from eval/run_eval.py.
def classify_agent(log: dict) -> dict:
    classification, attempt = {}, 0
    while attempt < MAX_ATTEMPTS:
        attempt += 1
        prompt = build_classify_prompt(log, attempt)
        try:
            text = tracked_generate(MODEL, prompt, purpose="classify",
                                    log_id=log.get("id"), attempt=attempt)
        except Exception:
            text = ""
        # Malformed / empty output normalizes to "unknown" → retried.
        classification = parse_classification(text)
        print(f"   🔁 classify attempt {attempt}: severity={classification.get('severity')}, "
              f"category={classification.get('category')}")
        if not needs_retry(classification):
            break
    return {"classification": classification, "attempts": attempt}


def classify(state: AgentState) -> dict:
    """Graph node: thin wrapper handing the log to the classify sub-agent."""
    return classify_agent(state["log"])


@observe()
async def find_similar(state: AgentState) -> dict:
    """Hybrid similarity search via the server's find_similar_logs tool.

    Only runs when the router decided the log is serious.
    """
    result = await call_mcp("find_similar_logs",
                            {"log_id": state["log_id"], "limit": 3})
    if "error" in result:
        return {"similar_logs": []}  # no embedding stored yet — skip gracefully

    similar = result["similar"]
    for s in similar:
        # the server formats similarity as a string for display;
        # write_report needs a float for its :.2f formatting
        s["similarity"] = float(s["similarity"])
    return {"similar_logs": similar}


def human_approval(state: AgentState) -> dict:
    """Human-in-the-loop node.

    interrupt() PAUSES the whole graph here; the payload is handed back to
    the caller so the human sees what they're approving. The state waits in
    the checkpointer until someone resumes with Command(resume=<value>) —
    then this node runs again from the top and interrupt() returns that
    value instead of pausing.
    """
    decision = interrupt({
        "question": "Generate an incident report for this log?",
        "log": state["log"]["message"],
        "classification": state["classification"],
        "similar_logs_found": len(state["similar_logs"]),
        "attempts": state["attempts"],
    })
    if not decision:
        return {"approved": False,
                "report": "(rejected by operator — no report generated)"}
    return {"approved": True}


# ---- SUB-AGENT: report ----
# Standalone: report_agent(log, classification, similar_logs) -> report
# string. Takes exactly what it needs as explicit arguments, so it's
# independently callable and testable.
def report_agent(log: dict, classification: dict, similar_logs: list) -> str:
    similar_text = "\n".join(
        f"- [{s['level']}] {s['message']} (similarity {s['similarity']:.2f})"
        for s in similar_logs
    ) or "(no similar-log data available for this log)"

    prompt = f"""You are an SRE assistant. Write a short incident report (max 5 sentences).

Log: {log['message']}
Classification: {json.dumps(classification)}
Similar past logs:
{similar_text}

Mention whether similar past incidents exist and what to do next."""
    try:
        text = tracked_generate(MODEL, prompt, purpose="write_report",
                                log_id=log.get("id"))
        return text.strip()
    except Exception as e:
        return f"Report generation failed: {e}"


def write_report(state: AgentState) -> dict:
    """Graph node: thin wrapper handing state slices to the report sub-agent."""
    return {"report": report_agent(state["log"], state["classification"],
                                   state.get("similar_logs", []))}


@observe()
def skip_report(state: AgentState) -> dict:
    """Terminal node when no report is warranted (info/unknown severity).
    Sets a clean `report` note and gives the skipped path its own span."""
    sev = state["classification"].get("severity")
    return {"report": f"(no report generated — severity '{sev}' does not warrant one)"}


# ============================================
# 3. ROUTERS — pick the next node, change nothing
# ============================================

def route_after_fetch(state: AgentState) -> str:
    if state.get("error"):
        return END          # log didn't exist → stop the whole graph
    return "classify"


def route_after_classify(state: AgentState) -> str:
    if state["classification"].get("severity") in ("critical", "warning"):
        return "find_similar"     # serious → dig up similar past incidents
    return "human_approval"       # info / unknown → straight to approval


def orchestrator(state: AgentState) -> str:
    """Deterministic orchestrator: the route is fully knowable from state,
    so an `if` beats an LLM here. Gates on TWO things:
      1. did the operator approve?              (rejected → stop)
      2. does severity warrant a report at all? (info/unknown → skip)
    """
    if not state["approved"]:
        return END                # report holds the rejection note
    if state["classification"].get("severity") in ("critical", "warning"):
        return "write_report"
    return "skip_report"


# ============================================
# 4. BUILD THE GRAPH
# ============================================

builder = StateGraph(AgentState)

builder.add_node("fetch_log", fetch_log)
builder.add_node("classify", classify)
builder.add_node("find_similar", find_similar)
builder.add_node("human_approval", human_approval)
builder.add_node("write_report", write_report)
builder.add_node("skip_report", skip_report)

builder.add_edge(START, "fetch_log")
builder.add_conditional_edges("fetch_log", route_after_fetch)
builder.add_conditional_edges("classify", route_after_classify)
builder.add_edge("find_similar", "human_approval")
builder.add_conditional_edges("human_approval", orchestrator)
builder.add_edge("write_report", END)
builder.add_edge("skip_report", END)

# THE CHECKPOINTER: saves a state snapshot after every node — what makes
# interrupt() possible, since paused runs live on disk, not in the process.
# In Docker, point CHECKPOINT_DB at a mounted volume (e.g.
# /data/checkpoints.db) or paused runs die with the container.
CHECKPOINT_DB = os.getenv("CHECKPOINT_DB", "checkpoints.db")

# AsyncSqliteSaver binds to the running event loop at construction, and at
# import time there is no loop yet — so the graph compiles lazily, on first
# use inside the loop.
graph = None


def _ensure_graph():
    """Compile the graph on first use. Call only from async code."""
    global graph
    if graph is None:
        checkpointer = AsyncSqliteSaver(aiosqlite.connect(CHECKPOINT_DB))
        graph = builder.compile(checkpointer=checkpointer)
    return graph


# ============================================
# 5. RUN IT
# ============================================
# With a checkpointer, every run belongs to a THREAD. The thread_id is the
# key the checkpointer files snapshots under — how a resume call finds the
# exact paused run to continue.

@observe(name="analyze_log")
async def start_analysis(log_id: int, thread_id: str) -> dict:
    """Run the graph until it finishes OR hits the interrupt.

    If it paused, the returned dict contains a "__interrupt__" key
    holding the payload that human_approval passed to interrupt().
    """
    # session_id = thread_id: the checkpointer's key for resuming state
    # doubles as Langfuse's key for grouping this trace and the /approve
    # one into one session.
    config = {"configurable": {"thread_id": thread_id}}
    g = _ensure_graph()
    with propagate_attributes(session_id=thread_id):
        async with mcp_session():   # server subprocess lives for this run only
            return await g.ainvoke({
                "log_id": log_id,
                "log": None,
                "classification": None,
                "attempts": 0,
                "similar_logs": [],
                "approved": None,
                "report": "",
                "error": None,
            }, config)


@observe(name="resume_analysis")
async def resume_analysis(thread_id: str, approved: bool) -> dict:
    """Wake the paused graph and hand it the human's decision.

    Command(resume=X) makes interrupt() inside human_approval return X.
    This opens a FRESH MCP session — the pause survives in the
    checkpointer, not in the connection. Sessions are per-run; state is
    per-thread.
    """
    config = {"configurable": {"thread_id": thread_id}}
    g = _ensure_graph()
    with propagate_attributes(session_id=thread_id):
        async with mcp_session():
            return await g.ainvoke(Command(resume=approved), config)


if __name__ == "__main__":
    # Interactive test:  python agent.py 56
    # Runs until the interrupt, asks YOU in the terminal, then resumes.
    import asyncio

    async def main():
        log_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
        thread_id = f"cli-log-{log_id}"

        result = await start_analysis(log_id, thread_id)

        if "__interrupt__" in result:
            payload = result["__interrupt__"][0].value
            print("\n⏸  GRAPH PAUSED — waiting for human approval")
            print(json.dumps(payload, indent=2, default=str))
            answer = input("\nApprove report generation? [y/n] ")
            result = await resume_analysis(thread_id,
                                           answer.strip().lower().startswith("y"))

        print("\n=== FINAL STATE ===")
        print(json.dumps(result, indent=2, default=str))

        # Flush the Langfuse queue before exiting, or the resume trace dies
        # in the in-memory batch.
        from llm import langfuse
        langfuse.shutdown()

    asyncio.run(main())
