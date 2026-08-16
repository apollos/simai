"""Automatic semantic-relation generation after a node/revision is
confirmed (section 11.4).

Limits: at most `max_per_revision` relations, minimum confidence
threshold, `supersedes` never auto-recorded, everything marked
origin='ai' / state='ai_generated' (dashed in the UI).
"""

from __future__ import annotations

import logging

from ..llm.client import ModelError, OpenClawClient
from ..llm.schemas import RelationProposals
from . import relations, search, tree

log = logging.getLogger("simai.autorel")

SYSTEM = """You analyse a personal thought tree. Given a NEW node and a list of
EXISTING nodes, propose only genuinely meaningful semantic relations between
the new node and existing ones. For every directed relation, set direction to
`new_to_existing` or `existing_to_new` according to its actual meaning.

Allowed relation_type values: related_to, supports, contradicts, refines,
qualifies, depends_on, applies_to, inspired_by.
NEVER propose 'supersedes' (that requires explicit user confirmation).
Only use to_node_id values from the provided list. Provide a short,
checkable rationale (<=300 chars) per relation. Return an empty list when
nothing is truly meaningful. Quality over quantity.
"""


def generate_for_node(
    conn,
    client: OpenClawClient,
    audit_key: bytes,
    node_id: str,
    *,
    max_per_revision: int = 3,
    minimum_confidence: float = 0.75,
) -> list[str]:
    """Best-effort: model failure means no relations, never a crash of the
    confirmation flow that already committed."""
    rev = tree.get_current_revision(conn, node_id)
    if rev is None:
        return []

    # context: parents, siblings, ancestors + semantic top-k over the tree
    context_ids: dict[str, dict] = {}
    node = tree.get_node(conn, node_id)
    for entry in tree.node_path(conn, node_id)[:-1]:
        context_ids[entry["id"]] = entry
    for sib in tree.list_children(conn, node["parent_id"]):
        if sib["id"] != node_id:
            context_ids[sib["id"]] = {"id": sib["id"], "title": sib["title"]}
    try:
        for hit in search.semantic_search(conn, client, f"{rev['title']}\n{rev['body']}", 10):
            if hit["node_id"] != node_id:
                context_ids[hit["node_id"]] = {"id": hit["node_id"], "title": hit["title"]}
    except ModelError:
        pass
    if not context_ids:
        return []

    lines = []
    for cid in list(context_ids)[:25]:
        crev = tree.get_current_revision(conn, cid)
        body_head = crev["body"][:160] if crev else ""
        lines.append(f"{cid} | {context_ids[cid].get('title', '')} | {body_head}")

    user = (
        f"NEW NODE {node_id} (revision {rev['revision_no']}):\n"
        f"{rev['title']}\n{rev['body']}\n\nEXISTING NODES:\n" + "\n".join(lines)
    )
    try:
        proposals = client.structured("graph_routing", SYSTEM, user, RelationProposals)
    except ModelError as exc:
        log.warning("auto-relations skipped node=%s reason=%s", node_id, exc)
        return []

    created: list[str] = []
    for prop in proposals.relations:
        if len(created) >= max_per_revision:
            break
        if prop.confidence < minimum_confidence:
            continue
        if prop.to_node_id not in context_ids:
            continue  # model may only pick from what it was shown
        if prop.relation_type == "supersedes":
            continue
        try:
            from_id, to_id = (
                (prop.to_node_id, node_id)
                if prop.direction == "existing_to_new"
                else (node_id, prop.to_node_id)
            )
            rel_id = relations.add_relation(
                conn,
                audit_key,
                from_id,
                to_id,
                prop.relation_type,
                origin="ai",
                rationale=prop.rationale,
                confidence=prop.confidence,
                model_profile=client.model_for("graph_routing"),
            )
            created.append(rel_id)
        except relations.RelationError:
            continue
    log.info("auto-relations node=%s created=%d", node_id, len(created))
    return created
