"""Dictation session closure registry.

The plugin is the only party that sees the session end ("结束记录"); sealed
envelopes carry a dictation_id but no open/closed state. On session close the
plugin notifies the core, which records (binding_id, dictation_id, closed_at)
here. The registry stores random identifiers only - never message content -
so it lives as an owner-only plaintext file next to the sealed inbox, works
while the vault is locked, and survives restarts.

Daily processing treats a CLOSED session as complete: its items bypass the
cutoff quiet window and are merged immediately. Sessions that were never
closed (plugin restart, lost message) fall back to the quiet-window rule.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger("simai.dictation")

REGISTRY_NAME = "dictation_sessions.json"
MAX_ENTRIES = 500
MAX_AGE_DAYS = 14
MAX_ID_CHARS = 128


def _registry_path(inbox_dir: Path) -> Path:
    # Lives inside the inbox directory on purpose: both sides ignore files
    # without the .sealed suffix, and inbox_dir already has 0700 semantics.
    return inbox_dir / REGISTRY_NAME


def mark_closed(inbox_dir: Path, binding_id: str, dictation_id: str) -> None:
    """Record one closed session (idempotent)."""
    if not (
        isinstance(binding_id, str)
        and 0 < len(binding_id) <= MAX_ID_CHARS
        and isinstance(dictation_id, str)
        and 0 < len(dictation_id) <= MAX_ID_CHARS
    ):
        raise ValueError("invalid dictation closure identifiers")
    entries = _load(inbox_dir)
    key = (binding_id, dictation_id)
    if key not in entries:
        entries[key] = datetime.now(UTC).isoformat(timespec="seconds")
        _store(inbox_dir, entries)
        log.info("dictation session closed binding=%s session=%s", binding_id, dictation_id)


def closed_keys(inbox_dir: Path) -> set[tuple[str, str]]:
    """All (binding_id, dictation_id) pairs currently marked closed."""
    return set(_load(inbox_dir))


def discard(inbox_dir: Path, keys: set[tuple[str, str]]) -> None:
    """Forget sessions whose items have been fully processed."""
    if not keys:
        return
    entries = _load(inbox_dir)
    remaining = {k: v for k, v in entries.items() if k not in keys}
    if len(remaining) != len(entries):
        _store(inbox_dir, remaining)


def _load(inbox_dir: Path) -> dict[tuple[str, str], str]:
    path = _registry_path(inbox_dir)
    try:
        raw = json.loads(path.read_text("utf-8"))
        entries: dict[tuple[str, str], str] = {}
        for row in raw:
            binding_id, dictation_id, closed_at = row
            if isinstance(binding_id, str) and isinstance(dictation_id, str) and isinstance(closed_at, str):
                entries[(binding_id, dictation_id)] = closed_at
        return _pruned(entries)
    except FileNotFoundError:
        return {}
    except (ValueError, TypeError, OSError):
        # A corrupt registry only costs the fast path; quiet-window fallback
        # still processes every session. Start over.
        log.warning("dictation registry unreadable; resetting")
        return {}


def _pruned(entries: dict[tuple[str, str], str]) -> dict[tuple[str, str], str]:
    horizon = datetime.now(UTC) - timedelta(days=MAX_AGE_DAYS)
    kept = {}
    for key, closed_at in entries.items():
        try:
            if datetime.fromisoformat(closed_at) > horizon:
                kept[key] = closed_at
        except ValueError:
            continue
    if len(kept) > MAX_ENTRIES:
        newest = sorted(kept.items(), key=lambda kv: kv[1], reverse=True)[:MAX_ENTRIES]
        kept = dict(newest)
    return kept


def _store(inbox_dir: Path, entries: dict[tuple[str, str], str]) -> None:
    inbox_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _registry_path(inbox_dir)
    payload = json.dumps(
        [[binding_id, dictation_id, closed_at] for (binding_id, dictation_id), closed_at in sorted(entries.items())],
        ensure_ascii=False,
    )
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
