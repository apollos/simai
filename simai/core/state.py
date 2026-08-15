"""Process-wide runtime state: Locked vs Unlocked (design doc section 15.1).

- Service starts Locked; only a web unlock (or CLI prompt without argv
  passphrase) opens the vault.
- Keys live exclusively in this process's memory.
- Web session expiry does NOT re-lock the database; only `lock()` or
  process exit does.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from datetime import UTC, datetime

from ..config import Config
from ..crypto import keyring
from ..db import engine

log = logging.getLogger("simai.state")


class AppState:
    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.RLock()
        # A daily run performs model calls outside the SQL transaction.  A
        # separate job lock prevents Web, cron and post-unlock backlog runs
        # from extracting the same sealed items concurrently.
        self.daily_lock = threading.Lock()
        self._keys: keyring.UnlockedKeys | None = None
        self._conn = None
        self.unlocked_at: str | None = None

    # -- lifecycle --------------------------------------------------------
    @property
    def is_unlocked(self) -> bool:
        return self._keys is not None and self._conn is not None

    def initialize_vault(self, passphrase: str) -> dict:
        """First-time setup: create vault header + encrypted database.
        Returns the one-time offline recovery pack (caller shows/saves it;
        it is never persisted server-side)."""
        with self._lock:
            engine.verify_capabilities()
            if self.config.key_header_path.exists() or self.config.db_path.exists():
                raise keyring.VaultError("Vault header or database already exists")
            keys, recovery_pack = keyring.create_vault(self.config.key_header_path, passphrase)
            try:
                conn = engine.open_database(self.config.db_path, keys.sqlcipher_hex(), create=True)
            except BaseException:
                keys.wipe()
                for path in (
                    self.config.key_header_path,
                    self.config.db_path,
                    self.config.db_path.with_name(self.config.db_path.name + "-wal"),
                    self.config.db_path.with_name(self.config.db_path.name + "-shm"),
                ):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                raise
            self._keys, self._conn = keys, conn
            self._sync_source_bindings(conn)
            conn.commit()
            self.unlocked_at = datetime.now(UTC).isoformat()
            return recovery_pack

    def unlock(self, passphrase: str) -> None:
        with self._lock:
            if self.is_unlocked:
                # Unlock is also the Web authentication boundary.  Never
                # accept an arbitrary passphrase merely because this process
                # already has the vault open (for example after another Web
                # session expired).
                probe = keyring.unlock_vault(self.config.key_header_path, passphrase)
                probe.wipe()
                return
            keys = keyring.unlock_vault(self.config.key_header_path, passphrase)
            conn = engine.open_database(self.config.db_path, keys.sqlcipher_hex())
            self._keys, self._conn = keys, conn
            self._sync_source_bindings(conn)
            conn.commit()
            self.unlocked_at = datetime.now(UTC).isoformat()
            log.info("vault unlocked")

    def lock(self) -> None:
        with self.daily_lock, self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None
            if self._keys is not None:
                self._keys.wipe()
                self._keys = None
            self.unlocked_at = None
            log.info("vault locked")

    # -- accessors (fail closed) -------------------------------------------
    @property
    def conn(self):
        with self._lock:
            if self._conn is None:
                raise engine.DatabaseLocked("Simai is locked. Unlock via the web console first.")
            return self._conn

    @property
    def keys(self) -> keyring.UnlockedKeys:
        with self._lock:
            if self._keys is None:
                raise engine.DatabaseLocked("Simai is locked. Unlock via the web console first.")
            return self._keys

    def transaction(self):
        """Context manager: BEGIN IMMEDIATE ... COMMIT/ROLLBACK."""
        return _Transaction(self)

    @contextmanager
    def reading(self):
        """Hold the state lock for a complete read operation."""
        self._lock.acquire()
        try:
            if self._conn is None:
                raise engine.DatabaseLocked("Simai is locked. Unlock via the web console first.")
            yield self._conn
        finally:
            self._lock.release()

    def status(self) -> dict:
        with self._lock:
            out = {
                "locked": not self.is_unlocked,
                "unlocked_at": self.unlocked_at,
                "profile": self.config.profile,
            }
            if self.is_unlocked:
                conn = self._conn
                out["pending_candidates"] = conn.execute(
                    "SELECT COUNT(*) FROM candidates WHERE status = 'pending'"
                ).fetchone()[0]
                out["nodes"] = conn.execute("SELECT COUNT(*) FROM nodes WHERE state = 'active'").fetchone()[0]
                out["relations"] = conn.execute(
                    "SELECT COUNT(*) FROM relations WHERE state IN ('ai_generated','confirmed')"
                ).fetchone()[0]
                last_job = conn.execute(
                    "SELECT job_type, status, started_at, finished_at FROM job_runs ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                out["last_job"] = dict(last_job) if last_job else None
        from ..crypto.sealed_inbox import list_items, oldest_item_age_days

        out["sealed_inbox_backlog"] = len(list_items(self.config.inbox_dir))
        age = oldest_item_age_days(self.config.inbox_dir)
        warning_days = float(self.config.section("sealed_inbox").get("warning_after_days", 7))
        out["sealed_inbox_oldest_days"] = age
        out["sealed_inbox_overdue"] = bool(age is not None and age > warning_days)
        return out

    # -- helpers -----------------------------------------------------------
    def _sync_source_bindings(self, conn) -> None:
        """Mirror YAML source bindings into the database (binding_key = HMAC
        of channel|account|sender|conversation, section 7.3)."""
        import hashlib
        import hmac as hmac_mod

        assert self._keys is not None, "_sync_source_bindings requires unlocked keys"
        for b in self.config.source_bindings():
            material = "|".join([b.channel, b.account_id, b.sender_key, b.conversation_id or ""])
            binding_key = hmac_mod.new(
                self._keys.audit_hmac_key,
                material.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            conn.execute(
                """INSERT INTO source_bindings
                   (id, binding_key, channel, account_id, sender_key, conversation_id,
                    enabled, passive_capture, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     binding_key = excluded.binding_key,
                     channel = excluded.channel,
                     account_id = excluded.account_id,
                     sender_key = excluded.sender_key,
                     conversation_id = excluded.conversation_id,
                     enabled = excluded.enabled,
                     passive_capture = excluded.passive_capture""",
                (
                    b.id,
                    binding_key,
                    b.channel,
                    b.account_id,
                    b.sender_key,
                    b.conversation_id,
                    int(b.enabled),
                    int(b.passive_capture),
                    engine.now_iso(),
                ),
            )


class _Transaction:
    def __init__(self, state: AppState):
        self.state = state
        self.conn = None
        self.lock = state._lock

    def __enter__(self):
        self.lock.acquire()
        if self.state._conn is None:
            self.lock.release()
            raise engine.DatabaseLocked("Simai is locked. Unlock via the web console first.")
        self.conn = self.state._conn
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            self.lock.release()
            raise
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.lock.release()
        return False
