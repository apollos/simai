"""Daily extraction worker (section 6.3).

Invoked by OpenClaw command-cron at 22:30 Asia/Shanghai (or manually via
`simai daily run`).  Behaviour:

- processes ALL unprocessed sealed-inbox items older than the cutoff
  (default: keep the most recent 30 minutes for the next batch);
- skips messages already handled explicitly (source_receipts.handled_explicitly);
- deduplicates by HMAC(binding_id | message_id) so re-runs are idempotent;
- candidates + receipts + cursor advance commit in ONE transaction;
- sealed ciphertexts are deleted only AFTER that commit; on any failure
  the cursor is not advanced and items stay for the next run;
- when the vault is locked: no processing, no cursor advance, the caller
  gets {"locked": true} and may send a content-free reminder.

Returned summary never contains thought bodies (log policy, section 23).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from ..crypto import sealed_inbox
from ..db.engine import now_iso
from ..llm.client import ModelError, OpenClawClient
from ..llm.schemas import DailyExtractResult
from . import capture, ids
from .state import AppState

log = logging.getLogger("simai.daily")

EXTRACT_SYSTEM = """You extract long-term personal thoughts from the OWNER's chat
messages of one day. Extract ONLY: personal opinions/judgements, thinking
about organisations/products/markets/technology, decisions and their
grounds, hypotheses and open questions, risk judgements, working methods
and principles, retrospective conclusions, inspirations and improvement
ideas, long-term concerns, personal takeaways from reading/meetings.

EXCLUDE: greetings and small talk; commands to the assistant; one-off
lookups (weather/price/time); assistant answers or inferences; pasted
material without personal processing; passwords/codes/keys/credentials;
short replies meaningless out of context; repetitions with no new info.

For each extracted item, `source_excerpt` must be a verbatim substring of
one input message and `source_message_no` must be that message's displayed
number. Be conservative: when unsure, do not extract.
Messages are numbered; treat each independently unless they clearly
continue one thought.
"""


def run_daily(state: AppState, client: OpenClawClient) -> dict:
    """Run at most one extractor per process.

    The lock spans model calls as well as database writes; otherwise cron,
    Web and post-unlock backlog processing can all observe the same sealed
    files before any receipt exists and create duplicate candidates.
    """
    if not state.daily_lock.acquire(blocking=False):
        return {
            "locked": not state.is_unlocked,
            "already_running": True,
            "processed": 0,
            "candidates": 0,
            "notify": False,
        }
    try:
        return _run_daily_locked(state, client)
    finally:
        state.daily_lock.release()


def _run_daily_locked(state: AppState, client: OpenClawClient) -> dict:
    cfg = state.config.section("daily_capture")
    if not bool(cfg.get("enabled", True)):
        return {
            "locked": not state.is_unlocked,
            "disabled": True,
            "processed": 0,
            "candidates": 0,
            "notify": False,
        }
    cutoff_minutes = int(cfg.get("cutoff_delay_minutes", 30))
    review_batch_size = int(cfg.get("review_batch_size", 5))
    max_messages = max(1, int(cfg.get("max_messages_per_run", 100)))
    max_prompt_chars = max(1000, int(cfg.get("max_prompt_chars_per_run", 60000)))

    if not state.is_unlocked:
        log.warning("daily run skipped: vault locked")
        return {
            "locked": True,
            "processed": 0,
            "candidates": 0,
            "notify": bool(cfg.get("notify_when_locked", True)),
        }

    keys = state.keys
    job = ids.job_id()
    batch = ids.batch_id()
    started = now_iso()
    with state.transaction() as tx:
        tx.execute(
            "INSERT INTO job_runs (id, job_type, started_at, status) VALUES (?, 'daily_extract', ?, 'running')",
            (job, started),
        )

    cutoff = datetime.now(UTC) - timedelta(minutes=cutoff_minutes)
    items: list[sealed_inbox.InboxItem] = []
    undecryptable = 0
    refused_binding = 0
    bindings = {b.id: b for b in state.config.source_bindings() if b.enabled}
    for path in sealed_inbox.list_items(state.config.inbox_dir):
        try:
            item = sealed_inbox.open_item(path, keys.inbox_private_key)
            captured = datetime.fromisoformat(item.captured_at)
        except Exception:
            undecryptable += 1
            continue
        binding = bindings.get(item.binding_id)
        identity_matches = bool(
            binding
            and item.channel == binding.channel
            and item.account_id == binding.account_id
            and item.sender_key == binding.sender_key
            and item.conversation_id == binding.conversation_id
            and (not item.is_group or binding.allow_group)
        )
        if not identity_matches or (item.capture_mode == "passive" and not binding.passive_capture):
            # Configuration may have been tightened after an item was queued.
            # Retain its ciphertext for explicit operator review; never silently
            # import it under a now-disabled source.
            refused_binding += 1
            continue
        if captured <= cutoff:
            items.append(item)

    # Deduplicate: drop items already receipted (explicitly handled or
    # processed by a previous batch).  Their ciphertext can be removed.
    pending_by_fingerprint: dict[str, tuple[sealed_inbox.InboxItem, str]] = {}
    already_done: list[sealed_inbox.InboxItem] = []
    with state.reading() as read_conn:
        for item in items:
            fingerprint = item.message_fingerprint(keys.audit_hmac_key)
            receipt = read_conn.execute(
                """SELECT handled_explicitly, processed_at FROM source_receipts
                   WHERE source_binding_id = ? AND message_hmac = ?""",
                (item.binding_id, fingerprint),
            ).fetchone()
            if receipt and (receipt["handled_explicitly"] or receipt["processed_at"]):
                already_done.append(item)
            else:
                existing = pending_by_fingerprint.get(fingerprint)
                if existing is None:
                    pending_by_fingerprint[fingerprint] = (item, fingerprint)
                elif item.capture_mode == "explicit" and existing[0].capture_mode == "passive":
                    # One OpenClaw turn can reach both passive observation and the
                    # explicit simai_capture tool.  Prefer the explicit intent even
                    # when its encrypted file arrived second.
                    already_done.append(existing[0])
                    pending_by_fingerprint[fingerprint] = (item, fingerprint)
                else:
                    already_done.append(item)
    todo = list(pending_by_fingerprint.values())
    backlog_total = len(todo)
    selected: list[tuple[sealed_inbox.InboxItem, str]] = []
    selected_chars = 0
    oversized = 0
    for pair in todo:
        item_chars = len(pair[0].body)
        is_passive = pair[0].capture_mode == "passive"
        if is_passive and item_chars > max_prompt_chars:
            oversized += 1
            continue
        if len(selected) >= max_messages:
            break
        if is_passive and selected_chars + item_chars > max_prompt_chars:
            continue
        selected.append(pair)
        if is_passive:
            selected_chars += item_chars
    todo = selected

    candidates_created = 0
    rechecked_done = 0
    try:
        if todo:
            passive_todo = [pair for pair in todo if pair[0].capture_mode == "passive"]
            extraction = DailyExtractResult(items=[])
            if passive_todo:
                numbered = "\n".join(f"[{i + 1}] {item.body}" for i, (item, _) in enumerate(passive_todo))
                extraction = client.structured("daily_extract", EXTRACT_SYSTEM, numbered, DailyExtractResult)
            planned = []
            for entry in extraction.items:
                owner = _owning_item(passive_todo, entry.source_message_no, entry.source_excerpt)
                if owner is None:
                    continue  # excerpt is not verbatim from input: refuse
                with state.reading() as read_conn:
                    placement = capture.propose_placement(
                        read_conn,
                        client,
                        entry.capture.normalized_content,
                    )
                planned.append((entry, owner, placement))

            with state.transaction() as tx:
                # An explicit Tool capture can finish while the daily model is
                # running.  Recheck receipts after BEGIN IMMEDIATE so that the
                # explicit and daily paths cannot both create a candidate for
                # the same OpenClaw message.
                live_todo: list[tuple[sealed_inbox.InboxItem, str]] = []
                for item, fingerprint in todo:
                    receipt = tx.execute(
                        """SELECT handled_explicitly, processed_at FROM source_receipts
                           WHERE source_binding_id = ? AND message_hmac = ?""",
                        (item.binding_id, fingerprint),
                    ).fetchone()
                    if receipt and (receipt["handled_explicitly"] or receipt["processed_at"]):
                        already_done.append(item)
                        rechecked_done += 1
                    else:
                        live_todo.append((item, fingerprint))
                live_fingerprints = {fingerprint for _item, fingerprint in live_todo}
                planned = [entry for entry in planned if entry[1][1] in live_fingerprints]
                todo = live_todo

                # Explicit/driving captures express an unambiguous request to
                # remember.  They must never be discarded by the conservative
                # passive-chat filter.  Preserve them as editable raw cards.
                for item, _fingerprint in todo:
                    if item.capture_mode == "explicit":
                        capture.create_raw_candidate(
                            tx,
                            keys.excerpt_key,
                            item.body,
                            source_binding_id=item.binding_id,
                        )
                        candidates_created += 1
                for entry, owner, placement in planned:
                    item, fingerprint = owner
                    cards = _write_candidate(tx, keys, entry, item, fingerprint, batch, placement)
                    candidates_created += cards
                for item, fingerprint in todo:
                    tx.execute(
                        """INSERT INTO source_receipts
                           (source_binding_id, message_hmac, capture_mode, handled_explicitly,
                            captured_at, processed_at, batch_id)
                           VALUES (?,?,?,0,?,?,?)
                           ON CONFLICT(source_binding_id, message_hmac) DO UPDATE SET
                             processed_at = excluded.processed_at,
                             batch_id = excluded.batch_id""",
                        (
                            item.binding_id,
                            fingerprint,
                            "explicit" if item.capture_mode == "explicit" else "daily",
                            item.captured_at,
                            now_iso(),
                            batch,
                        ),
                    )
                _advance_cursors(tx, todo)
                tx.execute(
                    """UPDATE job_runs SET finished_at = ?, status = 'ok',
                       messages_in = ?, candidates_out = ? WHERE id = ?""",
                    (now_iso(), len(todo), candidates_created, job),
                )
        else:
            with state.transaction() as tx:
                tx.execute(
                    "UPDATE job_runs SET finished_at = ?, status = 'ok', messages_in = 0 WHERE id = ?",
                    (now_iso(), job),
                )
    except Exception as exc:
        # fail-safe: nothing committed, cursor untouched, ciphertexts kept
        with state.transaction() as tx:
            tx.execute(
                "UPDATE job_runs SET finished_at = ?, status = 'failed', error_kind = ? WHERE id = ?",
                (now_iso(), type(exc).__name__, job),
            )
        log.error("daily run failed: %s", type(exc).__name__)
        if not isinstance(exc, ModelError):
            raise  # unexpected bug: bookkeeping done, let the caller see it
        return {"locked": False, "processed": 0, "candidates": 0, "failed": True, "notify": False}

    # transaction committed: now (and only now) delete consumed ciphertexts
    for item, _ in todo:
        sealed_inbox.delete_item(item.path)
    for item in already_done:
        sealed_inbox.delete_item(item.path)

    # snoozed -> pending: the daily reminder gathers everything left to review
    # (sections 4.2 / 12.3)
    from . import candidates as cand_mod

    with state.transaction() as tx:
        woken = cand_mod.wake_snoozed(tx)

    with state.reading() as read_conn:
        pending_total = read_conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE status = 'pending'"
        ).fetchone()[0]
    summary = {
        "locked": False,
        "processed": len(todo),
        "backlog_remaining": max(0, backlog_total - len(todo) - rechecked_done),
        "skipped_already_handled": len(already_done),
        "undecryptable": undecryptable,
        "refused_binding": refused_binding,
        "oversized": oversized,
        "candidates": candidates_created,
        "woken_snoozed": woken,
        "pending_total": pending_total,
        "review_batch_size": review_batch_size,
        # notify only when there is something to review (section 6.3)
        "notify": pending_total > 0,
    }
    log.info(
        "daily run ok processed=%d candidates=%d pending=%d",
        len(todo),
        candidates_created,
        pending_total,
    )
    return summary


def _owning_item(todo, source_message_no: int, excerpt: str):
    index = source_message_no - 1
    if 0 <= index < len(todo):
        item, fingerprint = todo[index]
        if excerpt and excerpt in item.body:
            return item, fingerprint
    return None


def _write_candidate(tx, keys, entry, item, fingerprint, batch, placement) -> int:
    from . import candidates as cand_mod

    cap = entry.capture
    action, parent_ids = placement
    cand_mod.create_candidate(
        tx,
        keys.excerpt_key,
        candidate_type=cap.candidate_type,
        source_excerpt=entry.source_excerpt,
        normalized_content=cap.normalized_content,
        title=cap.title,
        proposed_action=action,
        proposed_parent_ids=parent_ids,
        confidence=cap.confidence,
        needs_clarification=cap.needs_clarification,
        source_binding_id=item.binding_id,
        message_hmac=fingerprint,
        batch_date=batch,
    )
    return 1


def _advance_cursors(tx, todo) -> None:
    latest: dict[str, tuple[str, str]] = {}
    for item, fingerprint in todo:
        current = latest.get(item.binding_id)
        if current is None or item.captured_at > current[0]:
            latest[item.binding_id] = (item.captured_at, fingerprint)
    for binding_id, (captured_at, fingerprint) in latest.items():
        tx.execute(
            """INSERT INTO source_cursors
               (source_binding_id, last_successful_time, last_message_hmac, last_job_status, updated_at)
               VALUES (?,?,?,'ok',?)
               ON CONFLICT(source_binding_id) DO UPDATE SET
                 last_successful_time = excluded.last_successful_time,
                 last_message_hmac = excluded.last_message_hmac,
                 last_job_status = 'ok',
                 updated_at = excluded.updated_at""",
            (binding_id, captured_at, fingerprint, now_iso()),
        )
