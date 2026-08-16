"""Human-scannable unique IDs: N- nodes, R- revisions, C- candidates,
L- relations, A- audit events, B- batches, E- exports, J- jobs."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime


def _new(prefix: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    return f"{prefix}-{stamp}-{secrets.token_hex(4)}"


def node_id() -> str:
    return _new("N")


def revision_id() -> str:
    return _new("R")


def candidate_id() -> str:
    return _new("C")


def relation_id() -> str:
    return _new("L")


def audit_id() -> str:
    return _new("A")


def batch_id() -> str:
    return _new("B")


def export_id() -> str:
    return _new("E")


def job_id() -> str:
    return _new("J")
