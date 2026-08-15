"""Application-append-only audit trail. Every tree- or relation-changing action is
recorded inside the same transaction as the change itself (section 12.4).

Each event carries a per-row HMAC over its canonical content. Database
triggers reject UPDATE/DELETE through the application connection. This is
useful integrity evidence, but is not a deletion-complete hash chain against
an attacker who possesses the unlocked database; that stronger guarantee is
listed as a production follow-up in the review report.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from ..db.engine import now_iso
from . import ids


def record_event(
    conn,
    audit_hmac_key: bytes,
    event_type: str,
    entity_type: str,
    entity_id: str,
    before: dict | None = None,
    after: dict | None = None,
    candidate_id: str | None = None,
    confirmed_at: str | None = None,
) -> str:
    event_id = ids.audit_id()
    created_at = now_iso()
    before_json = json.dumps(before, ensure_ascii=False, sort_keys=True) if before else None
    after_json = json.dumps(after, ensure_ascii=False, sort_keys=True) if after else None
    material = "|".join(
        [event_id, event_type, entity_type, entity_id, before_json or "", after_json or "", created_at]
    )
    event_hmac = hmac.new(audit_hmac_key, material.encode("utf-8"), hashlib.sha256).hexdigest()
    conn.execute(
        """INSERT INTO audit_events
           (id, event_type, entity_type, entity_id, before_json, after_json,
            candidate_id, event_hmac, confirmed_at, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            event_type,
            entity_type,
            entity_id,
            before_json,
            after_json,
            candidate_id,
            event_hmac,
            confirmed_at,
            created_at,
        ),
    )
    return event_id
