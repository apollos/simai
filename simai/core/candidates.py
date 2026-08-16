"""Candidate lifecycle (sections 8.4, 9, 12.3).

State machine:  pending -> confirmed | rejected | snoozed(-> pending)

Rules enforced here:
- the raw user excerpt is stored only as ciphertext and is wiped in the
  SAME transaction that records the decision;
- confirming a candidate applies the confirmed tree action atomically
  (node + revision + audit event + candidate update in one commit);
- candidates never auto-commit, never auto-merge.
"""

from __future__ import annotations

import json
import secrets

from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
)

from ..db.engine import now_iso
from . import audit, ids, tree

ACTIONS = {"create_root", "create_child", "append", "revise", "merge"}


class CandidateError(Exception):
    pass


def _encrypt_excerpt(excerpt_key: bytes, text: str) -> bytes:
    nonce = secrets.token_bytes(24)
    ct = crypto_aead_xchacha20poly1305_ietf_encrypt(
        text.encode("utf-8"), b"simai-excerpt", nonce, excerpt_key
    )
    return nonce + ct


def _decrypt_excerpt(excerpt_key: bytes, blob: bytes) -> str:
    nonce, ct = blob[:24], blob[24:]
    return crypto_aead_xchacha20poly1305_ietf_decrypt(ct, b"simai-excerpt", nonce, excerpt_key).decode(
        "utf-8"
    )


def create_candidate(
    conn,
    excerpt_key: bytes,
    *,
    candidate_type: str,
    source_excerpt: str,
    normalized_content: str,
    title: str,
    proposed_action: str,
    proposed_parent_ids: list[str] | None = None,
    confidence: float | None = None,
    needs_clarification: bool = False,
    source_binding_id: str | None = None,
    message_hmac: str | None = None,
    batch_date: str | None = None,
) -> str:
    if proposed_action not in ACTIONS:
        raise CandidateError(f"Unknown proposed_action: {proposed_action}")
    parent_ids = proposed_parent_ids or []
    # fail-safe: proposed parents must exist, otherwise do not write
    for pid in parent_ids:
        tree.get_node(conn, pid)

    cand_id = ids.candidate_id()
    conn.execute(
        """INSERT INTO candidates
           (id, source_binding_id, candidate_type, source_excerpt_ciphertext,
            normalized_content, title, proposed_action, proposed_parent_ids,
            confidence, needs_clarification, status, batch_date, message_hmac, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,?,?)""",
        (
            cand_id,
            source_binding_id,
            candidate_type,
            _encrypt_excerpt(excerpt_key, source_excerpt),
            normalized_content,
            title,
            proposed_action,
            json.dumps(parent_ids),
            confidence,
            int(needs_clarification),
            batch_date,
            message_hmac,
            now_iso(),
        ),
    )
    return cand_id


def get_candidate(conn, candidate_id: str, excerpt_key: bytes | None = None) -> dict:
    row = conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    if row is None:
        raise CandidateError(f"Candidate not found: {candidate_id}")
    out = dict(row)
    blob = out.pop("source_excerpt_ciphertext", None)
    out["source_excerpt"] = None
    if blob and excerpt_key:
        out["source_excerpt"] = _decrypt_excerpt(excerpt_key, blob)
    out["proposed_parent_ids"] = json.loads(out.get("proposed_parent_ids") or "[]")
    return out


def list_candidates(
    conn,
    status: str = "pending",
    excerpt_key: bytes | None = None,
    source_binding_id: str | None = None,
) -> list[dict]:
    if source_binding_id is None:
        rows = conn.execute(
            "SELECT id FROM candidates WHERE status = ? ORDER BY created_at DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id FROM candidates
               WHERE status = ? AND source_binding_id = ?
               ORDER BY created_at DESC""",
            (status, source_binding_id),
        ).fetchall()
    return [get_candidate(conn, r["id"], excerpt_key) for r in rows]


def _finish(conn, audit_key: bytes, cand: dict, status: str) -> None:
    """Record decision and wipe the encrypted excerpt in the same statement."""
    if status == "rejected":
        conn.execute(
            """UPDATE candidates SET status = ?, decided_at = ?,
               source_excerpt_ciphertext = NULL, normalized_content = '', title = ''
               WHERE id = ?""",
            (status, now_iso(), cand["id"]),
        )
    else:
        conn.execute(
            """UPDATE candidates
               SET status = ?, decided_at = ?, source_excerpt_ciphertext = NULL
               WHERE id = ?""",
            (status, now_iso(), cand["id"]),
        )
    audit.record_event(
        conn,
        audit_key,
        f"candidate_{status}",
        "candidate",
        cand["id"],
        before={"status": cand["status"]},
        after={"status": status},
        candidate_id=cand["id"],
        confirmed_at=now_iso(),
    )


def confirm_candidate(
    conn,
    audit_key: bytes,
    candidate_id: str,
    *,
    action: str | None = None,
    parent_id: str | None = None,
    target_node_id: str | None = None,
    edited_title: str | None = None,
    edited_content: str | None = None,
    node_type: str | None = None,
) -> dict:
    """Apply the user's confirmed decision. The caller wraps this in a
    transaction; everything commits or nothing does."""
    cand = get_candidate(conn, candidate_id)
    if cand["status"] != "pending":
        raise CandidateError(f"Candidate {candidate_id} is not pending")

    final_action = action or cand["proposed_action"]
    if final_action not in ACTIONS:
        raise CandidateError(f"Unknown action: {final_action}")
    title = cand["title"] if edited_title is None else edited_title
    content = cand["normalized_content"] if edited_content is None else edited_content
    ntype = cand["candidate_type"] if node_type is None else node_type
    if ntype not in tree.NODE_TYPES:
        ntype = "idea"

    if final_action == "create_root":
        result = tree.create_node(conn, audit_key, title, content, ntype, None, candidate_id)
    elif final_action == "create_child":
        chosen_parent = parent_id or (cand["proposed_parent_ids"][0] if cand["proposed_parent_ids"] else None)
        if not chosen_parent:
            raise CandidateError("create_child requires a parent node")
        result = tree.create_node(conn, audit_key, title, content, ntype, chosen_parent, candidate_id)
    elif final_action in ("append", "revise"):
        target = target_node_id or (cand["proposed_parent_ids"][0] if cand["proposed_parent_ids"] else None)
        if not target:
            raise CandidateError(f"{final_action} requires a target node")
        result = tree.update_node(
            conn,
            audit_key,
            target,
            final_action,
            title=title,
            body=content,
            node_type=ntype,
            source_candidate_id=candidate_id,
        )
    elif final_action == "merge":
        if not (target_node_id and parent_id):
            raise CandidateError("merge requires target_node_id (source) and parent_id (target)")
        result = tree.merge_nodes(conn, audit_key, target_node_id, parent_id)
    else:  # pragma: no cover
        raise CandidateError(final_action)

    _finish(conn, audit_key, cand, "confirmed")
    _mark_receipt_processed(conn, cand)
    return result


def reject_candidate(conn, audit_key: bytes, candidate_id: str) -> None:
    cand = get_candidate(conn, candidate_id)
    if cand["status"] != "pending":
        raise CandidateError(f"Candidate {candidate_id} is not pending")
    _finish(conn, audit_key, cand, "rejected")
    _mark_receipt_processed(conn, cand)


def snooze_candidate(conn, audit_key: bytes, candidate_id: str) -> None:
    cand = get_candidate(conn, candidate_id)
    if cand["status"] != "pending":
        raise CandidateError(f"Candidate {candidate_id} is not pending")
    conn.execute(
        "UPDATE candidates SET status = 'snoozed', decided_at = ? WHERE id = ?",
        (now_iso(), candidate_id),
    )
    audit.record_event(conn, audit_key, "candidate_snoozed", "candidate", candidate_id)


def wake_snoozed(conn) -> int:
    cur = conn.execute("UPDATE candidates SET status = 'pending' WHERE status = 'snoozed'")
    return cur.rowcount


def mark_explicit_receipt(conn, source_binding_id: str, message_hmac: str) -> None:
    """Section 6.3: an explicit capture must skip the same message in daily extract."""
    conn.execute(
        """INSERT INTO source_receipts
           (source_binding_id, message_hmac, capture_mode, handled_explicitly, captured_at)
           VALUES (?,?, 'explicit', 1, ?)
           ON CONFLICT(source_binding_id, message_hmac) DO UPDATE SET handled_explicitly = 1""",
        (source_binding_id, message_hmac, now_iso()),
    )


def _mark_receipt_processed(conn, cand: dict) -> None:
    if cand.get("source_binding_id") and cand.get("message_hmac"):
        conn.execute(
            """UPDATE source_receipts SET processed_at = ?
               WHERE source_binding_id = ? AND message_hmac = ?""",
            (now_iso(), cand["source_binding_id"], cand["message_hmac"]),
        )
