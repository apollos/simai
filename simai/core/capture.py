"""Capture pipeline (sections 6.2, 8, 10.3).

    user text -> light normalization (model) -> placement proposal
              -> pending candidate (encrypted excerpt)

Nothing is written to the formal tree here; that only happens in
candidates.confirm_candidate after the user decides.
"""

from __future__ import annotations

import logging

from ..llm.client import OpenClawClient
from ..llm.schemas import CaptureBatchResult, PlacementResult
from . import candidates, search, tree

log = logging.getLogger("simai.capture")

NORMALIZE_SYSTEM = """You are Simai's capture assistant. The user text below is a personal
thought spoken or typed by the OWNER. Perform LIGHT normalization only:

ALLOWED: remove filler words and repetitions; fix typos, punctuation and
obvious grammar; turn speech into concise written form; complete clear
references from the given context.

FORBIDDEN: adding facts the user did not express; changing numbers,
times, subjects or proper nouns; dropping negations, conditions, scope
or uncertainty; turning a question into a conclusion; presenting
assistant suggestions as the user's opinion.

Personal dictionary (correct ASR errors toward these terms): {dictionary}

Existing candidate parent nodes (id | path | title):
{parent_context}

Decide:
- Split the utterance into separate items when it contains multiple
  independent thoughts. Return {{"items": [...]}} with one item per thought.
- candidate_type: idea/opinion/decision/question/principle/hypothesis/insight/risk/method
- proposed_action: create_root if no existing topic fits; create_child to
  attach under an existing node; append/revise when the content extends or
  corrects one of the listed nodes.
- proposed_parent_ids: up to 3 node ids FROM THE LIST ABOVE ONLY (empty if create_root).
- needs_clarification: true if the text cannot be understood without more context.
"""

PLACEMENT_SYSTEM = """Choose where a normalized candidate belongs in the owner's
thought tree. Only use node ids from the supplied candidate list.
Choose create_root when none fits; create_child for a new thought under an
existing topic; append when it adds information to an existing node; revise
when it corrects or changes an existing view. Never merge or move automatically.
Return at most three proposed parent/target ids."""


def _dictionary_terms(conn) -> str:
    rows = conn.execute("SELECT term FROM personal_dictionary ORDER BY term LIMIT 200").fetchall()
    return ", ".join(r["term"] for r in rows) or "(empty)"


def _placement_context(conn, client: OpenClawClient | None, text: str, k: int = 3) -> tuple[str, list[str]]:
    """Section 10.3: semantic top-k plus their parents/siblings/ancestors."""
    if client is None:
        return "(no context available)", []
    hits = search.similar_nodes(conn, client, text, k)
    seen: dict[str, str] = {}
    for hit in hits:
        nid = hit["node_id"]
        path = tree.node_path(conn, nid)
        for entry in path:  # ancestors
            seen.setdefault(entry["id"], " / ".join(p["title"] for p in tree.node_path(conn, entry["id"])))
        node = tree.get_node(conn, nid)
        for sib in tree.list_children(conn, node["parent_id"]):
            seen.setdefault(sib["id"], " / ".join(p["title"] for p in tree.node_path(conn, sib["id"])))
    lines = [f"{nid} | {path}" for nid, path in list(seen.items())[:30]]
    return ("\n".join(lines) or "(tree is empty)"), list(seen.keys())


def run_capture(
    conn,
    client: OpenClawClient,
    excerpt_key: bytes,
    text: str,
    *,
    source_binding_id: str | None = None,
    message_hmac: str | None = None,
    batch_date: str | None = None,
) -> list[dict]:
    """Returns confirmation-card dicts for each created candidate."""
    text = text.strip()
    if not text:
        return []
    existing = _cards_for_message(conn, message_hmac, excerpt_key)
    if existing:
        if source_binding_id and message_hmac:
            candidates.mark_explicit_receipt(conn, source_binding_id, message_hmac)
        return existing

    parent_context, allowed_ids = _placement_context(conn, client, text)
    system = NORMALIZE_SYSTEM.format(dictionary=_dictionary_terms(conn), parent_context=parent_context)
    result = client.structured("capture", system, text, CaptureBatchResult)
    cards = []
    for item in result.items:
        action, valid_parents = _validated_placement(conn, item, allowed_ids)
        cand_id = candidates.create_candidate(
            conn,
            excerpt_key,
            candidate_type=item.candidate_type,
            source_excerpt=text,
            normalized_content=item.normalized_content,
            title=item.title,
            proposed_action=action,
            proposed_parent_ids=valid_parents,
            confidence=item.confidence,
            needs_clarification=item.needs_clarification,
            source_binding_id=source_binding_id,
            message_hmac=message_hmac,
            batch_date=batch_date,
        )
        cards.append(confirmation_card(conn, cand_id, excerpt_key))
    if source_binding_id and message_hmac:
        candidates.mark_explicit_receipt(conn, source_binding_id, message_hmac)
    log.info("candidates created count=%d", len(cards))  # never log content
    return cards


def propose_placement(conn, client: OpenClawClient, content: str) -> tuple[str, list[str]]:
    """Route an already-normalized daily candidate against the current tree."""
    parent_context, allowed_ids = _placement_context(conn, client, content)
    if not allowed_ids:
        return "create_root", []
    result = client.structured(
        "graph_routing",
        PLACEMENT_SYSTEM,
        f"CANDIDATE:\n{content}\n\nALLOWED TREE NODES:\n{parent_context}",
        PlacementResult,
    )
    return _validated_placement(conn, result, allowed_ids)


def _validated_placement(conn, result, allowed_ids: list[str]) -> tuple[str, list[str]]:
    valid_parents = [
        pid
        for pid in result.proposed_parent_ids
        if pid in allowed_ids
        and conn.execute("SELECT 1 FROM nodes WHERE id = ? AND state = 'active'", (pid,)).fetchone()
    ][:3]
    action = result.proposed_action
    if action in ("create_child", "append", "revise") and not valid_parents:
        action = "create_root"
    return action, valid_parents


def _cards_for_message(conn, message_hmac: str | None, excerpt_key: bytes) -> list[dict]:
    if not message_hmac:
        return []
    rows = conn.execute(
        "SELECT id FROM candidates WHERE message_hmac = ? ORDER BY created_at, id",
        (message_hmac,),
    ).fetchall()
    return [confirmation_card(conn, row["id"], excerpt_key) for row in rows]


def create_raw_candidate(
    conn,
    excerpt_key: bytes,
    text: str,
    title: str | None = None,
    source_binding_id: str | None = None,
    message_hmac: str | None = None,
) -> dict:
    """Offline/manual path (no model): user text becomes the candidate
    verbatim. Still requires confirmation before entering the tree."""
    existing = _cards_for_message(conn, message_hmac, excerpt_key)
    if existing:
        if source_binding_id and message_hmac:
            candidates.mark_explicit_receipt(conn, source_binding_id, message_hmac)
        return existing[0]
    cand_id = candidates.create_candidate(
        conn,
        excerpt_key,
        candidate_type="idea",
        source_excerpt=text,
        normalized_content=text.strip(),
        title=(title or text.strip().splitlines()[0][:60]),
        proposed_action="create_root",
        source_binding_id=source_binding_id,
        message_hmac=message_hmac,
    )
    if source_binding_id and message_hmac:
        candidates.mark_explicit_receipt(conn, source_binding_id, message_hmac)
    return confirmation_card(conn, cand_id, excerpt_key)


def confirmation_card(conn, candidate_id: str, excerpt_key: bytes) -> dict:
    """Section 9: everything the user needs to decide."""
    cand = candidates.get_candidate(conn, candidate_id, excerpt_key)
    proposals = []
    for pid in cand["proposed_parent_ids"]:
        try:
            proposals.append(
                {
                    "node_id": pid,
                    "path": " / ".join(p["title"] for p in tree.node_path(conn, pid)),
                }
            )
        except tree.TreeError:
            continue
    return {
        "candidate_id": cand["id"],
        "candidate_type": cand["candidate_type"],
        "source_excerpt": cand["source_excerpt"],
        "normalized_content": cand["normalized_content"],
        "title": cand["title"],
        "proposed_action": cand["proposed_action"],
        "proposed_parents": proposals,
        "confidence": cand["confidence"],
        "needs_clarification": bool(cand["needs_clarification"]),
        "status": cand["status"],
        "created_at": cand["created_at"],
    }
