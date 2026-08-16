"""Natural-language Q&A over the thought tree (section 13.2).

Recall narrows the tree; a separate model call decides which of those
candidates actually answer the question; the answer call sees only the
judged nodes. Citations include node id, full path and revision number.
"""

from __future__ import annotations

import logging

from ..llm.client import OpenClawClient
from ..llm.schemas import QueryAnswer, QueryRelevance
from . import relations, search, tree

log = logging.getLogger("simai.qa")

EMPTY = {
    "answer": "思维树中没有找到与该问题相关的已确认思想。",
    "citations": [],
    "new_inferences": [],
}

ANSWER_LIMIT = 8

JUDGE_SYSTEM = """You filter the OWNER's confirmed thought-tree nodes for a question.

You are given CANDIDATE nodes (id, title, short body). Decide which of
them actually help answer the question.

Rules:
1. Return only node_ids from the candidate list.
2. Keep a node only if it is about the same matter as the question.
   Near-topic, tangentially related, or "might be useful later" is not enough.
3. If none qualify, return an empty list. Do not stretch to fill it.
4. Prefer fewer, clearer matches over a long maybe-list.
"""

SYSTEM = """You answer questions about the OWNER's personal thought tree.
You are given retrieved nodes (the owner's CONFIRMED thoughts) and
relations (marked confirmed or ai_generated). These nodes have already
been judged relevant; do not mention any other node.

Rules:
1. Base the answer on the provided nodes only.
2. Do not mention, allude to, or contrast with nodes that are not listed.
3. Clearly distinguish three kinds of content:
   - the owner's confirmed thoughts (cite them);
   - AI-generated relations that the owner has NOT confirmed (say so);
   - inferences you make right now (list them in new_inferences, never
     present them as the owner's historical opinion).
4. Every citation must reference a provided node: node_id, its revision_no
   and its full path exactly as given.
5. If the provided nodes do not answer the question, say so instead of inventing.
Answer in the same language as the question.
"""


def answer_question(conn, client: OpenClawClient, question: str, limit: int = ANSWER_LIMIT) -> dict:
    hits = search.recall_for_qa(conn, client, question)
    judged = _judge_relevant(conn, client, question, hits)
    if not judged:
        return dict(EMPTY)

    selected = judged[: max(1, min(limit, ANSWER_LIMIT))]
    blocks = []
    citation_truth: dict[str, dict] = {}
    for hit in selected:
        rev = tree.get_current_revision(conn, hit["node_id"])
        if rev is None:
            continue
        path = " / ".join(p["title"] for p in tree.node_path(conn, hit["node_id"]))
        citation_truth[hit["node_id"]] = {
            "node_id": hit["node_id"],
            "revision_no": rev["revision_no"],
            "path": path,
        }
        rels = relations.relations_of(conn, hit["node_id"])
        rel_lines = [
            f"  - [{r['state']}] {r['from_title']} --{r['relation_type']}--> {r['to_title']}"
            f" ({'user-confirmed' if r['state'] == 'confirmed' else 'AI, unconfirmed'})"
            for r in rels[:6]
        ]
        blocks.append(
            f"NODE {hit['node_id']} | revision_no={rev['revision_no']} | path={path}\n"
            f"TITLE: {rev['title']}\nBODY: {rev['body']}\n"
            + ("RELATIONS:\n" + "\n".join(rel_lines) if rel_lines else "RELATIONS: none")
        )
    if not blocks:
        return dict(EMPTY)

    result = client.structured(
        _task(client, "query"),
        SYSTEM,
        f"QUESTION: {question}\n\nRETRIEVED:\n\n" + "\n\n".join(blocks),
        QueryAnswer,
    )
    # Never trust model-generated path/version metadata. Keep its node
    # selection, but resolve canonical citation fields from the database.
    citations = []
    seen = set()
    for citation in result.citations:
        if citation.node_id in citation_truth and citation.node_id not in seen:
            citations.append(citation_truth[citation.node_id])
            seen.add(citation.node_id)
    return {"answer": result.answer, "citations": citations, "new_inferences": result.new_inferences}


def _judge_relevant(conn, client: OpenClawClient, question: str, hits: list[dict]) -> list[dict]:
    allowed: dict[str, dict] = {}
    lines: list[str] = []
    for hit in hits:
        rev = tree.get_current_revision(conn, hit["node_id"])
        if rev is None:
            continue
        allowed[hit["node_id"]] = hit
        body = rev["body"]
        if len(body) > 240:
            body = body[:240] + "…"
        lines.append(f"NODE {hit['node_id']}\nTITLE: {rev['title']}\nBODY: {body}")
    if not lines:
        return []
    result = client.structured(
        _task(client, "query_relevance"),
        JUDGE_SYSTEM,
        f"QUESTION: {question}\n\nCANDIDATES:\n\n" + "\n\n".join(lines),
        QueryRelevance,
    )
    ordered: list[dict] = []
    seen: set[str] = set()
    for node_id in result.node_ids:
        if node_id in allowed and node_id not in seen:
            ordered.append(allowed[node_id])
            seen.add(node_id)
    log.info("qa judge kept=%s of %s", len(ordered), len(allowed))
    return ordered


def _task(client: OpenClawClient, name: str) -> str:
    agents = getattr(client, "task_agents", None)
    if isinstance(agents, dict) and name in agents:
        return name
    return "query"
