# mcp_server.py - MCP server exposing the log database to Claude Code.
#
# Transport is stdio: the MCP host launches this script as a subprocess and
# speaks JSON-RPC over stdin/stdout. NOTHING may print to stdout except the
# protocol itself — a stray print() corrupts the stream and the host drops
# the connection. db.py prints on import, so stdout is redirected to stderr
# (which the host shows as server logs) while importing it.

import sys
import contextlib

# mcp 2.x renamed FastMCP -> MCPServer (same decorator API).
from mcp.server.mcpserver import MCPServer

with contextlib.redirect_stdout(sys.stderr):
    from db import db
    from search import hybrid_similar

mcp = MCPServer("log-classifier")


@mcp.tool()
def get_log(log_id: int) -> dict:
    """Fetch a single log entry by its id.

    Returns id, timestamp, level, message, and user for the log,
    or {"error": ...} if no log with that id exists.
    """
    rows = db.execute(
        """SELECT id, timestamp, level, message, user_name AS "user"
           FROM logs WHERE id = %s""",
        [log_id],
    )
    if not rows:
        return {"error": f"Log {log_id} not found"}
    return {**rows[0], "timestamp": str(rows[0]["timestamp"])}


@mcp.tool()
def get_recent_logs(limit: int = 20) -> list[dict]:
    """Fetch the most recent log entries from the log analyzer database.

    Returns id, timestamp, level, message, and user for each log,
    newest first.
    """
    rows = db.execute(
        """SELECT id, timestamp, level, message, user_name AS "user"
           FROM logs ORDER BY id DESC LIMIT %s""",
        [limit],
    )
    return [{**r, "timestamp": str(r["timestamp"])} for r in rows]


@mcp.tool()
def search_logs(query: str, level: str | None = None, limit: int = 20) -> list[dict]:
    """Search log messages by keyword (case-insensitive substring match).

    Optionally filter by level (e.g. ERROR, WARNING, INFO).
    Use this for exact-word searches; use find_similar_logs for
    meaning-based search.
    """
    sql = """SELECT id, timestamp, level, message, user_name AS "user"
             FROM logs WHERE message ILIKE %s"""
    params: list = [f"%{query}%"]
    if level:
        sql += " AND UPPER(level) = UPPER(%s)"
        params.append(level)
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(limit)
    rows = db.execute(sql, params)
    return [{**r, "timestamp": str(r["timestamp"])} for r in rows]


@mcp.tool()
def find_similar_logs(log_id: int, limit: int = 5) -> dict:
    """Find logs similar to a given log, using HYBRID search: pgvector
    cosine similarity (meaning) + Postgres full-text search (exact words),
    merged with Reciprocal Rank Fusion. Each result carries `similarity`
    (cosine, 0..1), `rrf_score` (fused rank score), and `rank` (text-search
    score, only if it keyword-matched). Returns an error message if the
    log has no embedding.
    """
    return hybrid_similar(log_id, limit)


@mcp.tool()
def log_stats() -> dict:
    """Summarize the log database: total logs, counts per level, and how
    many logs have embeddings.
    """
    total = db.execute("SELECT COUNT(*) AS n FROM logs")[0]["n"]
    by_level = db.execute(
        "SELECT level, COUNT(*) AS n FROM logs GROUP BY level ORDER BY n DESC"
    )
    embedded = db.execute("SELECT COUNT(*) AS n FROM log_embeddings")[0]["n"]
    return {
        "total_logs": total,
        "by_level": {r["level"]: r["n"] for r in by_level},
        "logs_with_embeddings": embedded,
    }


if __name__ == "__main__":
    # Blocks forever serving JSON-RPC over stdio — run directly it just
    # sits there silently, waiting for a client. That's correct.
    mcp.run(transport="stdio")
