"""LLM-assisted reorganisation of one node's direct children (Web button).

The model only PROPOSES, it never mutates the tree:
- merge suggestions become pending candidates (proposed_action="merge") in
  the normal confirmation inbox;
- semantic relations between siblings are written in the ai_generated state,
  reviewable on the node detail page and in the relation view.

Which model runs this analysis is configured separately: set
models.task_agents.reorganize (and optionally models.task_models.reorganize
to force a specific, stronger backend model through the same agent).
"""

from __future__ import annotations

import logging

from ..db.engine import now_iso
from ..llm.client import ModelError, OpenClawClient
from ..llm.schemas import ReorganizeResult
from . import candidates, relations, tree

log = logging.getLogger("simai.reorganize")

# meta-table watermark per analyzed scope ("<top>" or a node id): the deep
# scan re-analyzes a scope only when some descendant changed afterwards.
MARK_PREFIX = "reorganize_mark:"

REORGANIZE_SYSTEM = """You reorganise ONE level of the owner's private thought tree.
You receive a parent topic and its direct children (id, type, title, body
excerpt, number of grandchildren). Propose, conservatively:

- merges: pairs of children that clearly express substantially the SAME
  thought; the source's content would be appended into the target after the
  owner confirms. Do not merge merely related topics.
- relations: semantic links between two children using ONLY these types:
  related_to, supports, contradicts, refines, qualifies, depends_on,
  applies_to, inspired_by. Never propose supersedes.

Rules: use only node ids from the list; never invent content; a node may
appear in at most one merge; when nothing is clearly warranted return empty
lists. Write every rationale in Chinese, quoting the ideas involved.
"""

MAX_CHILD_EXCERPT_CHARS = 400
REORGANIZE_SOURCE = "web_reorganize"


def reorganize_children(
    conn,
    client: OpenClawClient,
    audit_key: bytes,
    excerpt_key: bytes,
    node_id: str | None,
) -> dict:
    """Analyze the children of `node_id` (None = top level) and record proposals."""
    parent_title = "（顶层主题）"
    if node_id:
        parent_title = tree.get_node(conn, node_id)["title"]
    children = [dict(child) for child in tree.list_children(conn, node_id)]
    if len(children) < 2:
        return {
            "children": len(children),
            "merge_candidates": 0,
            "relation_proposals": 0,
            "skipped": "该节点下少于两个子节点，无需整理",
        }

    titles: dict[str, str] = {}
    blocks: list[str] = []
    for child in children:
        revision = tree.get_current_revision(conn, child["id"])
        body = (revision["body"] if revision else "").strip().replace("\n", " ")
        grandchildren = len(tree.list_children(conn, child["id"]))
        titles[child["id"]] = child["title"]
        blocks.append(
            f"NODE {child['id']} | type={child['node_type']} | children={grandchildren}\n"
            f"TITLE: {child['title']}\n"
            f"BODY: {body[:MAX_CHILD_EXCERPT_CHARS]}"
        )
    prompt = f"PARENT: {parent_title}\n\nCHILDREN:\n\n" + "\n\n".join(blocks)
    result = client.structured("reorganize", REORGANIZE_SYSTEM, prompt, ReorganizeResult)

    child_ids = set(titles)
    merge_count = 0
    skipped_invalid = 0
    merged_endpoints: set[str] = set()
    for merge in result.merges:
        if (
            merge.source_node_id not in child_ids
            or merge.target_node_id not in child_ids
            or merge.source_node_id == merge.target_node_id
            or merge.source_node_id in merged_endpoints
            or merge.target_node_id in merged_endpoints
        ):
            skipped_invalid += 1
            continue
        source_title = titles[merge.source_node_id]
        target_title = titles[merge.target_node_id]
        description = (
            f"AI 整理提案：将「{source_title}」({merge.source_node_id}) "
            f"合并入「{target_title}」({merge.target_node_id})。\n"
            f"理由：{merge.rationale}"
        )
        candidates.create_candidate(
            conn,
            excerpt_key,
            candidate_type="insight",
            source_excerpt=description,
            normalized_content=merge.rationale,
            title=f"合并建议：{source_title} → {target_title}"[:120],
            proposed_action="merge",
            proposed_parent_ids=[merge.source_node_id, merge.target_node_id],
            confidence=merge.confidence,
            source_binding_id=REORGANIZE_SOURCE,
        )
        merged_endpoints.update({merge.source_node_id, merge.target_node_id})
        merge_count += 1

    relation_count = 0
    for proposal in result.relations:
        if (
            proposal.from_node_id not in child_ids
            or proposal.to_node_id not in child_ids
            or proposal.from_node_id == proposal.to_node_id
        ):
            skipped_invalid += 1
            continue
        try:
            relations.add_relation(
                conn,
                audit_key,
                proposal.from_node_id,
                proposal.to_node_id,
                proposal.relation_type,
                origin="ai",
                rationale=proposal.rationale,
                confidence=proposal.confidence,
                model_profile="reorganize",
            )
        except relations.RelationError:
            # duplicate, archived endpoint, or a type reserved for the user
            skipped_invalid += 1
            continue
        relation_count += 1

    _set_mark(conn, node_id)
    log.info(
        "reorganize done node=%s children=%d merges=%d relations=%d skipped=%d",
        node_id or "<top>",
        len(children),
        merge_count,
        relation_count,
        skipped_invalid,
    )
    return {
        "children": len(children),
        "merge_candidates": merge_count,
        "relation_proposals": relation_count,
        "skipped_invalid": skipped_invalid,
    }


def reorganize_tree(
    conn,
    client: OpenClawClient,
    audit_key: bytes,
    excerpt_key: bytes,
    max_parents: int = 8,
) -> dict:
    """Deep scan: analyze every parent (top level included) with >=2 active
    children whose subtree changed since that parent's last reorganize pass.

    At most `max_parents` model calls per invocation; remaining eligible
    scopes are reported as deferred so the owner can simply run it again.
    A model failure on one scope never aborts the others.
    """
    rows = conn.execute(
        "SELECT id, parent_id, updated_at FROM nodes WHERE state = 'active'"
    ).fetchall()
    children_map: dict[str | None, list[str]] = {}
    updated: dict[str, str] = {}
    for row in rows:
        children_map.setdefault(row["parent_id"], []).append(row["id"])
        updated[row["id"]] = row["updated_at"]

    subtree_latest: dict[str, str] = {}

    def latest(node_id: str) -> str:
        cached = subtree_latest.get(node_id)
        if cached is None:
            cached = max(
                [updated[node_id]] + [latest(child) for child in children_map.get(node_id, [])]
            )
            subtree_latest[node_id] = cached
        return cached

    eligible: list[tuple[str, str | None]] = []
    skipped_unchanged = 0
    scopes_total = 0
    for parent, kids in children_map.items():
        if len(kids) < 2:
            continue
        scopes_total += 1
        watermark = max(latest(kid) for kid in kids)
        mark = _get_mark(conn, parent)
        if mark and watermark <= mark:
            skipped_unchanged += 1
            continue
        eligible.append((watermark, parent))

    # Freshest changes first: the scopes the owner just touched get analyzed
    # before older backlog when the per-run budget bites.
    eligible.sort(key=lambda pair: pair[0], reverse=True)
    deferred = max(0, len(eligible) - max_parents)
    merge_candidates = 0
    relation_proposals = 0
    scopes_run = 0
    failed = 0
    for _watermark, scope in eligible[:max_parents]:
        try:
            summary = reorganize_children(conn, client, audit_key, excerpt_key, scope)
        except ModelError:
            failed += 1
            log.warning("deep reorganize: model failed for scope %s", scope or "<top>")
            continue
        merge_candidates += summary.get("merge_candidates", 0)
        relation_proposals += summary.get("relation_proposals", 0)
        scopes_run += 1
    log.info(
        "deep reorganize done scopes=%d run=%d unchanged=%d deferred=%d failed=%d",
        scopes_total,
        scopes_run,
        skipped_unchanged,
        deferred,
        failed,
    )
    return {
        "scopes_total": scopes_total,
        "scopes_run": scopes_run,
        "skipped_unchanged": skipped_unchanged,
        "deferred": deferred,
        "failed": failed,
        "merge_candidates": merge_candidates,
        "relation_proposals": relation_proposals,
    }


def _mark_key(node_id: str | None) -> str:
    return MARK_PREFIX + (node_id or "<top>")


def _get_mark(conn, node_id: str | None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (_mark_key(node_id),)).fetchone()
    return row["value"] if row else None


def _set_mark(conn, node_id: str | None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (_mark_key(node_id), now_iso()),
    )
