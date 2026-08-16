"""Main thought tree: single-parent nodes with append-only revisions.

Invariants enforced here (sections 10 and 12.4):
- every formal node has exactly one parent_id (NULL only for roots);
- a node can never become its own ancestor (recursive-CTE check);
- content changes never overwrite history: each change appends a revision
  and moves nodes.current_revision_id forward;
- node snapshot, revision, FTS row and audit event commit in ONE
  transaction (the caller owns the transaction; helpers never commit).
"""

from __future__ import annotations

import hashlib

from ..db.engine import now_iso
from . import audit, ids
from .textseg import segment_for_index

NODE_TYPES = {
    "idea",
    "opinion",
    "decision",
    "question",
    "principle",
    "hypothesis",
    "insight",
    "risk",
    "method",
    "topic",
}


class TreeError(Exception):
    pass


def _content_hash(title: str, body: str) -> str:
    return hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()


def _touch_fts(conn, node_id: str, title: str, body: str) -> None:
    conn.execute("DELETE FROM node_fts WHERE node_id = ?", (node_id,))
    conn.execute(
        "INSERT INTO node_fts(node_id, title, body) VALUES (?,?,?)",
        (node_id, segment_for_index(title), segment_for_index(body)),
    )


def get_node(conn, node_id: str):
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if row is None:
        raise TreeError(f"Node not found: {node_id}")
    return row


def _get_active_node(conn, node_id: str, role: str = "Node"):
    row = get_node(conn, node_id)
    if row["state"] != "active":
        raise TreeError(f"{role} must be active: {node_id} (state={row['state']})")
    return row


def get_current_revision(conn, node_id: str):
    return conn.execute(
        """SELECT r.* FROM node_revisions r
           JOIN nodes n ON n.current_revision_id = r.id
           WHERE n.id = ?""",
        (node_id,),
    ).fetchone()


def node_path(conn, node_id: str) -> list[dict]:
    """Root-to-node path using a recursive CTE."""
    rows = conn.execute(
        """WITH RECURSIVE up(id, parent_id, title, depth) AS (
               SELECT id, parent_id, title, 0 FROM nodes WHERE id = ?
               UNION ALL
               SELECT n.id, n.parent_id, n.title, up.depth + 1
               FROM nodes n JOIN up ON n.id = up.parent_id
           )
           SELECT id, title FROM up ORDER BY depth DESC""",
        (node_id,),
    ).fetchall()
    return [{"id": r["id"], "title": r["title"]} for r in rows]


def is_descendant(conn, ancestor_id: str, maybe_descendant_id: str) -> bool:
    row = conn.execute(
        """WITH RECURSIVE down(id) AS (
               SELECT id FROM nodes WHERE id = ?
               UNION ALL
               SELECT n.id FROM nodes n JOIN down d ON n.parent_id = d.id
           )
           SELECT 1 FROM down WHERE id = ? LIMIT 1""",
        (ancestor_id, maybe_descendant_id),
    ).fetchone()
    return row is not None


def _append_revision(
    conn,
    node_id: str,
    parent_id: str | None,
    node_type: str,
    title: str,
    body: str,
    change_type: str,
    source_candidate_id: str | None,
) -> str:
    last = conn.execute(
        "SELECT COALESCE(MAX(revision_no), 0) FROM node_revisions WHERE node_id = ?", (node_id,)
    ).fetchone()[0]
    rev_id = ids.revision_id()
    conn.execute(
        """INSERT INTO node_revisions
           (id, node_id, revision_no, parent_id, node_type, title, body,
            change_type, source_candidate_id, content_hash, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            rev_id,
            node_id,
            last + 1,
            parent_id,
            node_type,
            title,
            body,
            change_type,
            source_candidate_id,
            _content_hash(title, body),
            now_iso(),
        ),
    )
    return rev_id


def create_node(
    conn,
    audit_key: bytes,
    title: str,
    body: str,
    node_type: str = "idea",
    parent_id: str | None = None,
    source_candidate_id: str | None = None,
) -> dict:
    """create_root (parent_id None) or create_child."""
    if node_type not in NODE_TYPES:
        raise TreeError(f"Unknown node_type: {node_type}")
    if parent_id is not None:
        _get_active_node(conn, parent_id, "Parent node")

    node_id = ids.node_id()
    now = now_iso()
    conn.execute(
        """INSERT INTO nodes (id, parent_id, node_type, title, sort_order, state, created_at, updated_at)
           VALUES (?,?,?,?,
                   (SELECT COALESCE(MAX(sort_order),0)+1 FROM nodes WHERE parent_id IS ?),
                   'active', ?, ?)""",
        (node_id, parent_id, node_type, title, parent_id, now, now),
    )
    change = "create_root" if parent_id is None else "create_child"
    rev_id = _append_revision(conn, node_id, parent_id, node_type, title, body, change, source_candidate_id)
    conn.execute("UPDATE nodes SET current_revision_id = ? WHERE id = ?", (rev_id, node_id))
    _touch_fts(conn, node_id, title, body)
    audit.record_event(
        conn,
        audit_key,
        change,
        "node",
        node_id,
        after={"title": title, "parent_id": parent_id, "node_type": node_type},
        candidate_id=source_candidate_id,
        confirmed_at=now,
    )
    return {"node_id": node_id, "revision_id": rev_id}


def update_node(
    conn,
    audit_key: bytes,
    node_id: str,
    change_type: str,  # 'append' or 'revise'
    title: str | None = None,
    body: str | None = None,
    node_type: str | None = None,
    source_candidate_id: str | None = None,
) -> dict:
    if change_type not in ("append", "revise"):
        raise TreeError(f"Unsupported update change_type: {change_type}")
    node = _get_active_node(conn, node_id)
    current = get_current_revision(conn, node_id)
    new_title = title if title is not None else node["title"]
    new_type = node_type if node_type is not None else node["node_type"]
    if new_type not in NODE_TYPES:
        raise TreeError(f"Unknown node_type: {new_type}")
    if change_type == "append":
        new_body = (current["body"] + "\n\n" + (body or "")).strip()
    else:
        new_body = body if body is not None else current["body"]

    rev_id = _append_revision(
        conn, node_id, node["parent_id"], new_type, new_title, new_body, change_type, source_candidate_id
    )
    conn.execute(
        "UPDATE nodes SET title = ?, node_type = ?, current_revision_id = ?, updated_at = ? WHERE id = ?",
        (new_title, new_type, rev_id, now_iso(), node_id),
    )
    _touch_fts(conn, node_id, new_title, new_body)
    mark_relations_stale(conn, audit_key, node_id)
    audit.record_event(
        conn,
        audit_key,
        change_type,
        "node",
        node_id,
        before={"title": node["title"], "revision_id": node["current_revision_id"]},
        after={"title": new_title, "revision_id": rev_id},
        candidate_id=source_candidate_id,
        confirmed_at=now_iso(),
    )
    return {"node_id": node_id, "revision_id": rev_id}


def move_node(conn, audit_key: bytes, node_id: str, new_parent_id: str | None) -> dict:
    """User-confirmed only. Refuses cycles via recursive descendant check."""
    node = _get_active_node(conn, node_id)
    if new_parent_id is not None:
        _get_active_node(conn, new_parent_id, "Parent node")
        if new_parent_id == node_id or is_descendant(conn, node_id, new_parent_id):
            raise TreeError("Target parent is the node itself or one of its descendants")
    current = get_current_revision(conn, node_id)
    rev_id = _append_revision(
        conn, node_id, new_parent_id, node["node_type"], node["title"], current["body"], "move", None
    )
    conn.execute(
        """UPDATE nodes SET parent_id = ?, current_revision_id = ?,
           sort_order = (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM nodes
                         WHERE parent_id IS ? AND id <> ?),
           updated_at = ? WHERE id = ?""",
        (new_parent_id, rev_id, new_parent_id, node_id, now_iso(), node_id),
    )
    mark_relations_stale(conn, audit_key, node_id)
    audit.record_event(
        conn,
        audit_key,
        "move",
        "node",
        node_id,
        before={"parent_id": node["parent_id"]},
        after={"parent_id": new_parent_id},
        confirmed_at=now_iso(),
    )
    return {"node_id": node_id, "revision_id": rev_id}


def merge_nodes(conn, audit_key: bytes, source_id: str, target_id: str) -> dict:
    """User-confirmed only. Source content is appended into target; source
    node is kept (state='merged') so history and references stay valid."""
    if source_id == target_id:
        raise TreeError("Cannot merge a node into itself")
    source = _get_active_node(conn, source_id, "Merge source")
    _get_active_node(conn, target_id, "Merge target")
    if is_descendant(conn, source_id, target_id):
        raise TreeError("Cannot merge a node into one of its descendants")
    src_rev = get_current_revision(conn, source_id)
    result = update_node(
        conn,
        audit_key,
        target_id,
        "append",
        body=f"[merged from {source_id}: {source['title']}]\n{src_rev['body']}",
    )
    # re-parent children of the merged node to the target
    for child in conn.execute(
        "SELECT id FROM nodes WHERE parent_id = ? AND state = 'active'", (source_id,)
    ).fetchall():
        move_node(conn, audit_key, child["id"], target_id)
    conn.execute("UPDATE nodes SET state = 'merged', updated_at = ? WHERE id = ?", (now_iso(), source_id))
    conn.execute("DELETE FROM node_fts WHERE node_id = ?", (source_id,))
    mark_relations_stale(conn, audit_key, source_id)
    audit.record_event(
        conn,
        audit_key,
        "merge",
        "node",
        source_id,
        before={"state": source["state"]},
        after={"state": "merged", "merged_into": target_id},
        confirmed_at=now_iso(),
    )
    return result


def archive_node(conn, audit_key: bytes, node_id: str) -> None:
    _get_active_node(conn, node_id)
    rows = conn.execute(
        """WITH RECURSIVE down(id) AS (
               SELECT id FROM nodes WHERE id = ?
               UNION ALL
               SELECT n.id FROM nodes n JOIN down d ON n.parent_id = d.id
           ) SELECT n.id, n.state FROM nodes n JOIN down ON down.id = n.id""",
        (node_id,),
    ).fetchall()
    for node in rows:
        if node["state"] != "active":
            continue
        conn.execute(
            "UPDATE nodes SET state = 'archived', updated_at = ? WHERE id = ?",
            (now_iso(), node["id"]),
        )
        conn.execute("DELETE FROM node_fts WHERE node_id = ?", (node["id"],))
        mark_relations_stale(conn, audit_key, node["id"])
        audit.record_event(
            conn,
            audit_key,
            "archive",
            "node",
            node["id"],
            before={"state": node["state"]},
            after={"state": "archived"},
            confirmed_at=now_iso(),
        )


def restore_revision(conn, audit_key: bytes, node_id: str, revision_no: int) -> dict:
    # Revision restore changes the current snapshot; it is not an unarchive
    # operation.  Refuse hidden archived/merged nodes just like update().
    node = _get_active_node(conn, node_id)
    old = conn.execute(
        "SELECT * FROM node_revisions WHERE node_id = ? AND revision_no = ?", (node_id, revision_no)
    ).fetchone()
    if old is None:
        raise TreeError(f"Revision {revision_no} of {node_id} not found")
    parent_id = old["parent_id"]
    if parent_id is not None:
        _get_active_node(conn, parent_id, "Historical parent node")
        if parent_id == node_id or is_descendant(conn, node_id, parent_id):
            raise TreeError("Historical parent would create a cycle in the current tree")
    if old["node_type"] not in NODE_TYPES:
        raise TreeError(f"Historical node type is invalid: {old['node_type']}")
    rev_id = _append_revision(
        conn,
        node_id,
        parent_id,
        old["node_type"],
        old["title"],
        old["body"],
        "restore",
        old["source_candidate_id"],
    )
    conn.execute(
        """UPDATE nodes SET parent_id = ?, node_type = ?, title = ?,
           current_revision_id = ?, updated_at = ? WHERE id = ?""",
        (parent_id, old["node_type"], old["title"], rev_id, now_iso(), node_id),
    )
    _touch_fts(conn, node_id, old["title"], old["body"])
    mark_relations_stale(conn, audit_key, node_id)
    audit.record_event(
        conn,
        audit_key,
        "restore",
        "node",
        node_id,
        before={
            "revision_id": node["current_revision_id"],
            "parent_id": node["parent_id"],
            "node_type": node["node_type"],
        },
        after={
            "revision_id": rev_id,
            "restored_revision_no": revision_no,
            "parent_id": parent_id,
            "node_type": old["node_type"],
        },
        confirmed_at=now_iso(),
    )
    return {"node_id": node_id, "revision_id": rev_id}


def mark_relations_stale(conn, audit_key: bytes, node_id: str) -> None:
    """Section 11.5: content change invalidates relations bound to older
    revisions of this node; they must be re-evaluated, not silently kept."""
    rows = conn.execute(
        """SELECT id FROM relations
           WHERE state IN ('ai_generated','confirmed')
             AND (from_node_id = ? OR to_node_id = ?)""",
        (node_id, node_id),
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE relations SET state = 'stale', valid_to = ? WHERE id = ?", (now_iso(), row["id"])
        )
        audit.record_event(conn, audit_key, "relation_stale", "relation", row["id"])


def list_children(conn, parent_id: str | None):
    if parent_id is None:
        return conn.execute(
            "SELECT * FROM nodes WHERE parent_id IS NULL AND state = 'active' ORDER BY sort_order"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM nodes WHERE parent_id = ? AND state = 'active' ORDER BY sort_order", (parent_id,)
    ).fetchall()


def subtree(conn, root_id: str | None = None, include_archived: bool = False) -> list[dict]:
    """Flattened subtree with depth, ordered depth-first."""
    state_filter = "" if include_archived else "AND n.state = 'active'"
    if root_id is None:
        seed = f"SELECT id, parent_id, title, node_type, state, 0 AS depth FROM nodes n WHERE parent_id IS NULL {state_filter}"
        params: tuple = ()
    else:
        seed = f"SELECT id, parent_id, title, node_type, state, 0 AS depth FROM nodes n WHERE id = ? {state_filter}"
        params = (root_id,)
    rows = conn.execute(
        f"""WITH RECURSIVE walk(id, parent_id, title, node_type, state, depth) AS (
               {seed}
               UNION ALL
               SELECT n.id, n.parent_id, n.title, n.node_type, n.state, walk.depth + 1
               FROM nodes n JOIN walk ON n.parent_id = walk.id
               WHERE 1=1 {state_filter}
           )
           SELECT w.*, nd.updated_at, nd.current_revision_id
           FROM walk w JOIN nodes nd ON nd.id = w.id
           ORDER BY w.depth, nd.sort_order""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def revision_timeline(conn, node_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT revision_no, title, body, change_type, source_candidate_id, content_hash, created_at
           FROM node_revisions WHERE node_id = ? ORDER BY revision_no""",
        (node_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def export_snapshot(conn, root_id: str | None = None, include_history: bool = False) -> dict:
    """Full-fidelity dict used by exporters and backups."""
    nodes = subtree(conn, root_id, include_archived=False)
    node_ids = {n["id"] for n in nodes}
    out_nodes = []
    for n in nodes:
        rev = get_current_revision(conn, n["id"])
        entry = {
            **n,
            "body": rev["body"] if rev else "",
            "revision_no": rev["revision_no"] if rev else 0,
        }
        if include_history:
            entry["history"] = revision_timeline(conn, n["id"])
        out_nodes.append(entry)
    relations = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM relations WHERE state IN ('ai_generated','confirmed')"
        ).fetchall()
        if r["from_node_id"] in node_ids and r["to_node_id"] in node_ids
    ]
    return {"nodes": out_nodes, "relations": relations, "exported_at": now_iso()}
