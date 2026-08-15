"""Natural-language Q&A over the thought tree (section 13.2).

The answer must separate: (1) user-confirmed thoughts, (2) unconfirmed
AI-generated relations, (3) inferences the model makes in THIS answer.
Citations include node id, full path and revision number.
"""

from __future__ import annotations

import logging

from ..llm.client import OpenClawClient
from ..llm.schemas import QueryAnswer
from . import relations, search, tree

log = logging.getLogger("simai.qa")

SYSTEM = """You answer questions about the OWNER's personal thought tree.
You are given retrieved nodes (the owner's CONFIRMED thoughts) and
relations (marked confirmed or ai_generated).

Rules:
1. Base the answer on the provided nodes only.
2. Clearly distinguish three kinds of content:
   - the owner's confirmed thoughts (cite them);
   - AI-generated relations that the owner has NOT confirmed (say so);
   - inferences you make right now (list them in new_inferences, never
     present them as the owner's historical opinion).
3. Every citation must reference a provided node: node_id, its revision_no
   and its full path exactly as given.
4. If the tree contains nothing relevant, say so instead of inventing.
Answer in the same language as the question.
"""


def answer_question(conn, client: OpenClawClient, question: str, limit: int = 8) -> dict:
    hits = search.combined_search(conn, client, question, limit)
    if not hits:
        return {
            "answer": "思维树中没有找到与该问题相关的已确认思想。",
            "citations": [],
            "new_inferences": [],
        }

    blocks = []
    citation_truth: dict[str, dict] = {}
    for hit in hits:
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

    result = client.structured(
        "query", SYSTEM, f"QUESTION: {question}\n\nRETRIEVED:\n\n" + "\n\n".join(blocks), QueryAnswer
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
