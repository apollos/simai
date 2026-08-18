"""Semantic relations between arbitrary nodes (design doc section 11).

Separate from the main-tree parent/child structure.  Every relation is
bound to the revisions of both endpoints; a content change on either end
marks the relation stale (handled in tree.update_node).
"""

from __future__ import annotations

from ..db.engine import now_iso
from . import audit, ids
from .tree import get_current_revision, get_node

RELATION_TYPES = {
    "related_to": False,  # value = is_directed
    "supports": True,
    "contradicts": True,
    "refines": True,
    "qualifies": True,
    "depends_on": True,
    "applies_to": True,
    "inspired_by": True,
    "supersedes": True,
}

# These change the effective validity of an older opinion and therefore
# can never be recorded silently by AI (sections 11.4 / 19).
REQUIRE_USER_CONFIRMATION = {"supersedes"}


class RelationError(Exception):
    pass


def add_relation(
    conn,
    audit_key: bytes,
    from_node_id: str,
    to_node_id: str,
    relation_type: str,
    origin: str,  # 'ai' | 'user'
    rationale: str | None = None,
    confidence: float | None = None,
    model_profile: str | None = None,
    label: str | None = None,
) -> str:
    if relation_type not in RELATION_TYPES:
        raise RelationError(f"Unknown relation_type: {relation_type}")
    if from_node_id == to_node_id:
        raise RelationError("A relation cannot connect a node to itself")
    if origin == "ai" and relation_type in REQUIRE_USER_CONFIRMATION:
        raise RelationError(f"{relation_type} requires explicit user confirmation")

    is_directed = RELATION_TYPES[relation_type]
    if not is_directed and from_node_id > to_node_id:
        # normalise undirected endpoints so the unique index works
        from_node_id, to_node_id = to_node_id, from_node_id

    from_node = get_node(conn, from_node_id)
    to_node = get_node(conn, to_node_id)
    if from_node["state"] != "active" or to_node["state"] != "active":
        raise RelationError("Both relation endpoints must be active")
    from_rev = get_current_revision(conn, from_node_id)
    to_rev = get_current_revision(conn, to_node_id)
    if from_rev is None or to_rev is None:
        raise RelationError("Both endpoints must have a current revision")

    duplicate = conn.execute(
        """SELECT 1 FROM relations
           WHERE from_node_id = ? AND to_node_id = ? AND relation_type = ?
             AND state IN ('ai_generated','confirmed')""",
        (from_node_id, to_node_id, relation_type),
    ).fetchone()
    if duplicate:
        raise RelationError("An active relation of this type already exists between these nodes")

    rel_id = ids.relation_id()
    state = "confirmed" if origin == "user" else "ai_generated"
    conn.execute(
        """INSERT INTO relations
           (id, from_node_id, to_node_id, relation_type, is_directed, label, rationale,
            from_revision_id, to_revision_id, confidence, origin, model_profile,
            state, valid_from)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            rel_id,
            from_node_id,
            to_node_id,
            relation_type,
            int(is_directed),
            label,
            rationale,
            from_rev["id"],
            to_rev["id"],
            confidence,
            origin,
            model_profile,
            state,
            now_iso(),
        ),
    )
    audit.record_event(
        conn,
        audit_key,
        "relation_add",
        "relation",
        rel_id,
        after={"from": from_node_id, "to": to_node_id, "type": relation_type, "origin": origin},
    )
    return rel_id


def set_relation_state(conn, audit_key: bytes, relation_id: str, new_state: str) -> str:
    if new_state not in ("confirmed", "rejected", "stale"):
        raise RelationError(f"Invalid relation state: {new_state}")
    row = conn.execute("SELECT * FROM relations WHERE id = ?", (relation_id,)).fetchone()
    if row is None:
        raise RelationError(f"Relation not found: {relation_id}")
    if new_state == "confirmed":
        from_node = get_node(conn, row["from_node_id"])
        to_node = get_node(conn, row["to_node_id"])
        if from_node["state"] != "active" or to_node["state"] != "active":
            raise RelationError("Both relation endpoints must be active")
    from_rev = get_current_revision(conn, row["from_node_id"])
    to_rev = get_current_revision(conn, row["to_node_id"])
    revisions_changed = (
        from_rev is None
        or to_rev is None
        or row["from_revision_id"] != from_rev["id"]
        or row["to_revision_id"] != to_rev["id"]
    )
    if new_state == "confirmed" and (row["state"] == "stale" or revisions_changed):
        if from_rev is None or to_rev is None:
            raise RelationError("Both endpoints must have a current revision")
        duplicate = conn.execute(
            """SELECT id FROM relations
               WHERE from_node_id = ? AND to_node_id = ? AND relation_type = ?
                 AND state IN ('ai_generated','confirmed')""",
            (row["from_node_id"], row["to_node_id"], row["relation_type"]),
        ).fetchone()
        if duplicate:
            raise RelationError("A current relation of this type already exists")
        new_id = ids.relation_id()
        now = now_iso()
        conn.execute(
            """INSERT INTO relations
               (id, from_node_id, to_node_id, relation_type, is_directed, label,
                rationale, from_revision_id, to_revision_id, confidence, origin,
                model_profile, state, supersedes_relation_id, valid_from)
               VALUES (?,?,?,?,?,?,?,?,?,?,? ,?,'confirmed',?,?)""",
            (
                new_id,
                row["from_node_id"],
                row["to_node_id"],
                row["relation_type"],
                row["is_directed"],
                row["label"],
                row["rationale"],
                from_rev["id"],
                to_rev["id"],
                row["confidence"],
                "user",
                row["model_profile"],
                relation_id,
                now,
            ),
        )
        if row["state"] != "stale":
            conn.execute(
                "UPDATE relations SET state = 'stale', valid_to = ? WHERE id = ?",
                (now, relation_id),
            )
        audit.record_event(
            conn,
            audit_key,
            "relation_reconfirm",
            "relation",
            new_id,
            before={"supersedes_relation_id": relation_id},
            after={"state": "confirmed", "from_revision_id": from_rev["id"], "to_revision_id": to_rev["id"]},
            confirmed_at=now,
        )
        return new_id
    valid_to = now_iso() if new_state in ("rejected", "stale") else None
    conn.execute(
        "UPDATE relations SET state = ?, valid_to = ? WHERE id = ?",
        (new_state, valid_to, relation_id),
    )
    audit.record_event(
        conn,
        audit_key,
        "relation_state",
        "relation",
        relation_id,
        before={"state": row["state"]},
        after={"state": new_state},
        confirmed_at=now_iso() if new_state == "confirmed" else None,
    )
    return relation_id


def pending_ai(conn, limit: int = 200) -> list[dict]:
    """All ai_generated relations awaiting the owner's confirm/reject,
    newest first. Shown in the confirmation inbox next to candidates."""
    rows = conn.execute(
        """SELECT r.id, r.from_node_id, r.to_node_id, r.relation_type, r.rationale,
                  r.confidence, r.model_profile, r.valid_from,
                  nf.title AS from_title, nt.title AS to_title
           FROM relations r
           JOIN nodes nf ON nf.id = r.from_node_id
           JOIN nodes nt ON nt.id = r.to_node_id
           WHERE r.state = 'ai_generated'
           ORDER BY r.valid_from DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def relations_of(conn, node_id: str, include_stale: bool = False) -> list[dict]:
    states = ("ai_generated", "confirmed", "stale") if include_stale else ("ai_generated", "confirmed")
    marks = ",".join("?" * len(states))
    rows = conn.execute(
        f"""SELECT r.*, nf.title AS from_title, nt.title AS to_title
            FROM relations r
            JOIN nodes nf ON nf.id = r.from_node_id
            JOIN nodes nt ON nt.id = r.to_node_id
            WHERE (r.from_node_id = ? OR r.to_node_id = ?) AND r.state IN ({marks})
            ORDER BY r.valid_from DESC""",
        (node_id, node_id, *states),
    ).fetchall()
    return [dict(r) for r in rows]


def local_graph(conn, node_id: str, depth: int = 1) -> dict:
    """Local relation neighbourhood for the web relation view (section 14.4).
    Never returns the whole graph by default."""
    if node_id:
        get_node(conn, node_id)
    seen = {node_id}
    frontier = {node_id}
    edges: list[dict] = []
    for _ in range(max(1, depth)):
        next_frontier: set[str] = set()
        for nid in frontier:
            for rel in relations_of(conn, nid, include_stale=True):
                if rel not in edges:
                    edges.append(rel)
                for other in (rel["from_node_id"], rel["to_node_id"]):
                    if other not in seen:
                        seen.add(other)
                        next_frontier.add(other)
        frontier = next_frontier
    nodes = [
        dict(conn.execute("SELECT id, title, node_type, state FROM nodes WHERE id = ?", (nid,)).fetchone())
        for nid in seen
        if conn.execute("SELECT 1 FROM nodes WHERE id = ?", (nid,)).fetchone()
    ]
    # de-duplicate edges by id
    unique = {e["id"]: e for e in edges}
    return {"nodes": nodes, "relations": list(unique.values())}


def count_auto_relations_for_revision(conn, revision_id: str) -> int:
    return conn.execute(
        """SELECT COUNT(*) FROM relations
           WHERE origin = 'ai' AND (from_revision_id = ? OR to_revision_id = ?)""",
        (revision_id, revision_id),
    ).fetchone()[0]
