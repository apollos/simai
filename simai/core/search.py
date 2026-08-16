"""Search (section 13.1): FTS5 keyword search plus in-memory cosine
similarity over embeddings stored inside the SQLCipher database.
No plaintext external index is ever created.
"""

from __future__ import annotations

import array
import logging

from ..db.engine import now_iso
from ..llm.client import ModelError, OpenClawClient
from . import tree
from .textseg import build_match_query

log = logging.getLogger("simai.search")


def _pack(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def _unpack(blob: bytes) -> list[float]:
    a = array.array("f")
    a.frombytes(blob)
    return list(a)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def upsert_embedding(conn, client: OpenClawClient, node_id: str) -> bool:
    """Compute and store the embedding for a node's current revision.
    Returns False (without writing) when the embedding model is down;
    the caller decides whether that is fatal for its task."""
    rev = tree.get_current_revision(conn, node_id)
    if rev is None:
        return False
    try:
        [vector] = client.embed([f"{rev['title']}\n{rev['body']}"], kind="document")
    except ModelError as exc:
        log.warning("embedding skipped node=%s reason=%s", node_id, exc)
        return False
    conn.execute(
        """INSERT INTO embeddings (node_id, revision_id, model_id, dimensions, vector_blob, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(node_id, model_id) DO UPDATE SET
             revision_id = excluded.revision_id,
             dimensions = excluded.dimensions,
             vector_blob = excluded.vector_blob,
             updated_at = excluded.updated_at""",
        (node_id, rev["id"], client.embedding_model, len(vector), _pack(vector), now_iso()),
    )
    return True


def keyword_search(conn, query: str, limit: int = 20) -> list[dict]:
    match_expr = build_match_query(query)
    if not match_expr:
        return []
    rows = conn.execute(
        """SELECT f.node_id, n.title, n.node_type, n.updated_at,
                  snippet(node_fts, 2, '[', ']', '…', 12) AS snippet,
                  bm25(node_fts) AS score
           FROM node_fts f JOIN nodes n ON n.id = f.node_id
           WHERE node_fts MATCH ? AND n.state = 'active'
           ORDER BY score LIMIT ?""",
        (match_expr, limit),
    ).fetchall()
    return [dict(r) | {"match": "keyword"} for r in rows]


def semantic_search(
    conn, client: OpenClawClient, query: str, limit: int = 10, min_score: float = 0.0
) -> list[dict]:
    """Unlocked-memory cosine ranking; explicit ModelError if embeddings
    are unavailable (callers may fall back to keyword search).

    Scores at or below min_score are dropped. The default 0.0 only
    removes noise (zero/negative cosine), not a relevance policy.
    """
    [qvec] = client.embed([query], kind="query")
    rows = conn.execute(
        """SELECT e.node_id, e.vector_blob, n.title, n.node_type, n.updated_at
           FROM embeddings e JOIN nodes n ON n.id = e.node_id
           WHERE n.state = 'active' AND e.model_id = ?
             AND e.revision_id = n.current_revision_id""",
        (client.embedding_model,),
    ).fetchall()
    scored = [dict(r, score=_cosine(qvec, _unpack(r["vector_blob"])), match="semantic") for r in rows]
    scored = [item for item in scored if item["score"] > min_score]
    scored.sort(key=lambda d: d["score"], reverse=True)
    for item in scored:
        item.pop("vector_blob", None)
    return scored[:limit]


def combined_search(
    conn,
    client: OpenClawClient | None,
    query: str,
    limit: int = 10,
    semantic_min_score: float = 0.0,
) -> list[dict]:
    """Keyword + semantic union used by the web UI and simai_search tool."""
    results: dict[str, dict] = {}
    try:
        for item in keyword_search(conn, query, limit):
            results[item["node_id"]] = item
    except Exception:  # malformed FTS query syntax should not kill search
        log.info("keyword search failed for query, continuing with semantic only")
    if client is not None:
        try:
            for item in semantic_search(conn, client, query, limit, min_score=semantic_min_score):
                results.setdefault(item["node_id"], item)
        except ModelError as exc:
            log.warning("semantic search unavailable: %s", exc)
    out = list(results.values())
    for item in out:
        item["path"] = " / ".join(p["title"] for p in tree.node_path(conn, item["node_id"]))
    return out[:limit]


SMALL_TREE_MAX = 20
RECALL_LIMIT = 20


def count_active_nodes(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM nodes WHERE state = 'active'").fetchone()[0])


def list_active_hits(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT id AS node_id, title, node_type, updated_at
           FROM nodes WHERE state = 'active' ORDER BY updated_at DESC"""
    ).fetchall()
    out = [dict(r) | {"match": "all"} for r in rows]
    for item in out:
        item["path"] = " / ".join(p["title"] for p in tree.node_path(conn, item["node_id"]))
    return out


def recall_for_qa(
    conn,
    client: OpenClawClient | None,
    query: str,
    small_tree_max: int = SMALL_TREE_MAX,
    recall_limit: int = RECALL_LIMIT,
) -> list[dict]:
    """Candidate generation for Q&A. Small trees skip ranking and return
    every active node; larger trees use keyword+semantic recall."""
    if count_active_nodes(conn) <= small_tree_max:
        return list_active_hits(conn)
    return combined_search(conn, client, query, limit=recall_limit, semantic_min_score=0.0)


def reindex_all(conn, client: OpenClawClient) -> dict:
    """Recompute embeddings for every active node. Used after the
    embedding backend becomes available for the first time."""
    rows = conn.execute("SELECT id FROM nodes WHERE state = 'active'").fetchall()
    written = 0
    skipped = 0
    for row in rows:
        if upsert_embedding(conn, client, row["id"]):
            written += 1
        else:
            skipped += 1
    return {"nodes": len(rows), "written": written, "skipped": skipped}


def similar_nodes(conn, client: OpenClawClient, text: str, limit: int = 5) -> list[dict]:
    """Used by placement: nearest existing nodes for a new piece of content."""
    try:
        return semantic_search(conn, client, text, limit)
    except ModelError:
        # degrade to keyword search over the raw text
        return keyword_search(conn, text[:200], limit)
