# search.py - Hybrid similarity search (pgvector + full-text + RRF).
# Shared by mcp_server.py and main.py so the two callers can't drift.

from db import db


def reciprocal_rank_fusion(vector_results: list, keyword_results: list,
                           k: int = 60) -> list:
    """Merge two ranked lists by RRF: each list contributes 1/(k+rank) per
    item, so an id ranked well in BOTH lists beats one ranked well in one.

    k=60 is the textbook default, kept ON PURPOSE after measuring
    alternatives (k=1/5/10/60 on this corpus, 2026-08-28): the order was
    essentially identical — at this corpus size ranking is dominated by
    both-lists membership, not by k. Scores are ordinal (only sorted, never
    thresholded). Revisit if the corpus grows or scores get thresholded.
    """
    scores: dict = {}
    for rank, log in enumerate(vector_results, start=1):
        scores[log["id"]] = scores.get(log["id"], 0) + 1 / (k + rank)
    for rank, log in enumerate(keyword_results, start=1):
        scores[log["id"]] = scores.get(log["id"], 0) + 1 / (k + rank)

    # Field-union merge: an id present in both lists keeps its vector
    # `similarity` AND its keyword `rank`.
    merged: dict = {}
    for log in vector_results + keyword_results:
        merged[log["id"]] = {**merged.get(log["id"], {}), **log}

    ranked_ids = sorted(scores, key=scores.get, reverse=True)
    return [{**merged[i], "rrf_score": round(scores[i], 5)} for i in ranked_ids]


def hybrid_similar(log_id: int, limit: int = 5) -> dict:
    """Hybrid search: pgvector cosine (meaning) + Postgres full-text
    (exact words), merged with Reciprocal Rank Fusion.

    Returns {"log_id", "similar", "vector_ids", "keyword_ids"} or
    {"error": ...} if the log is missing or has no embedding.
    """
    emb = db.execute(
        "SELECT embedding FROM log_embeddings WHERE log_id = %s", [log_id]
    )
    if not emb:
        return {"error": f"log {log_id} has no embedding (run db_seed.py?)"}

    target = emb[0]["embedding"]
    src = db.execute("SELECT message FROM logs WHERE id = %s", [log_id])
    if not src:
        return {"error": f"Log {log_id} not found"}

    # Both branches fetch MORE than `limit` so fusion has something to
    # reorder — fusing two top-3 lists can only shuffle 3 rows.
    pool = max(limit, 10)

    vector_rows = db.execute(
        """SELECT l.id, l.level, l.message,
                  ROUND((1 - (le.embedding <=> %s))::numeric, 4) AS similarity
           FROM logs l
           JOIN log_embeddings le ON l.id = le.log_id
           WHERE l.id != %s
           ORDER BY le.embedding <=> %s
           LIMIT %s""",
        [target, log_id, target, pool],
    )

    # plainto_tsquery ANDs every lexeme — our query is a whole log message,
    # so that would match ~nothing. websearch_to_tsquery with explicit ORs
    # matches on ANY shared term, ranked by how many. The embedding join
    # lets keyword hits report their true cosine similarity too.
    query_text = " OR ".join(src[0]["message"].split())
    keyword_rows = db.execute(
        """SELECT l.id, l.level, l.message,
                  ROUND(ts_rank(to_tsvector('english', l.message),
                                websearch_to_tsquery('english', %s))::numeric,
                        4) AS rank,
                  ROUND((1 - (le.embedding <=> %s))::numeric, 4) AS similarity
           FROM logs l
           JOIN log_embeddings le ON l.id = le.log_id
           WHERE to_tsvector('english', l.message)
                 @@ websearch_to_tsquery('english', %s)
             AND l.id != %s
           ORDER BY rank DESC
           LIMIT %s""",
        [query_text, target, query_text, log_id, pool],
    )

    fused = reciprocal_rank_fusion(
        [dict(r) for r in vector_rows], [dict(r) for r in keyword_rows]
    )
    return {
        "log_id": log_id,
        "similar": fused[:limit],
        # provenance for debugging: what each branch saw before fusion
        "vector_ids": [r["id"] for r in vector_rows],
        "keyword_ids": [r["id"] for r in keyword_rows],
    }
