"""SQLCipher connection management with fail-safe capability checks.

Section 12.4: before a formal database is created, cipher_version,
cipher integrity, foreign keys and FTS5 must all be verified; if any is
missing, refuse to proceed.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("simai.db")

try:  # sqlcipher3 exposes the sqlite3 DB-API over SQLCipher
    import sqlcipher3 as sqlcipher  # type: ignore
except ImportError:  # pragma: no cover
    sqlcipher = None


class DatabaseError(Exception):
    pass


class DatabaseLocked(DatabaseError):
    """Raised when an operation requires the unlocked database."""


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _require_driver() -> None:
    if sqlcipher is None:
        raise DatabaseError(
            "sqlcipher3 driver not installed. Install `sqlcipher3-wheels` "
            "(Linux/WSL). Simai refuses to fall back to plain SQLite."
        )


def capability_report() -> dict:
    """Used by `simai doctor` and by init-time fail-safe checks."""
    report = {
        "driver": sqlcipher is not None,
        "cipher_version": None,
        "fts5": False,
        "foreign_keys": False,
        "memory_temp_store": False,
        "cipher_status": False,
    }
    if sqlcipher is None:
        return report
    conn = sqlcipher.connect(":memory:")
    try:
        row = conn.execute("PRAGMA cipher_version").fetchone()
        report["cipher_version"] = row[0] if row else None
        conn.execute("PRAGMA key = \"x'0000000000000000000000000000000000000000000000000000000000000000'\"")
        status = conn.execute("PRAGMA cipher_status").fetchone()
        report["cipher_status"] = bool(status and str(status[0]) == "1")
        conn.execute("PRAGMA foreign_keys = ON")
        report["foreign_keys"] = conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        conn.execute("PRAGMA temp_store = MEMORY")
        report["memory_temp_store"] = conn.execute("PRAGMA temp_store").fetchone()[0] == 2
        try:
            conn.execute("CREATE VIRTUAL TABLE _fts_probe USING fts5(x)")
            report["fts5"] = True
        except Exception:
            report["fts5"] = False
    finally:
        conn.close()
    return report


def verify_capabilities() -> None:
    report = capability_report()
    missing = [k for k, v in report.items() if not v]
    if missing:
        raise DatabaseError(f"Required database capabilities missing: {', '.join(missing)}")


def open_database(db_path: Path, sqlcipher_key_hex: str, create: bool = False):
    """Open (and optionally create) the encrypted database.

    Raises WrongKey-style DatabaseError if the key cannot decrypt the file.
    """
    _require_driver()
    if not create and not db_path.exists():
        raise DatabaseError(f"Database not found: {db_path}")
    if create:
        verify_capabilities()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.parent.chmod(0o700)

    conn = sqlcipher.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlcipher.Row
    # autocommit mode: transactions are controlled explicitly (BEGIN IMMEDIATE)
    conn.isolation_level = None
    # Raw hex key avoids passphrase-KDF ambiguity across SQLCipher versions.
    conn.execute(f"PRAGMA key = \"x'{sqlcipher_key_hex}'\"")
    if sys.platform != "win32":
        # optional hardening; crashes SQLCipher on Windows (VirtualLock quota)
        conn.execute("PRAGMA cipher_memory_security = ON")
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except Exception as exc:
        conn.close()
        raise DatabaseError("Cannot open database: wrong key or corrupt file") from exc

    conn.execute("PRAGMA foreign_keys = ON")
    # SQLCipher encrypts database pages, not arbitrary SQLite temporary
    # files.  Keep all transient query data in memory.
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA secure_delete = ON")
    # DELETE journal avoids relying on WAL-reset behaviour across the range of
    # SQLCipher versions shipped by third-party Python wheels.  It trades some
    # write concurrency for a simpler encrypted-file lifecycle.
    conn.execute("PRAGMA journal_mode = DELETE")

    if create:
        from .schema import DDL, SCHEMA_VERSION

        conn.executescript(DDL)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at', ?)", (now_iso(),))
        conn.commit()

    from .schema import EXTRA_DDL

    conn.executescript(EXTRA_DDL)
    conn.commit()

    try:
        _verify_open_state(conn)
        if db_path.exists():
            db_path.chmod(0o600)
    except Exception:
        conn.close()
        raise
    return conn


def _verify_open_state(conn) -> None:
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise DatabaseError("foreign_keys pragma is not enabled")
    if conn.execute("PRAGMA temp_store").fetchone()[0] != 2:
        raise DatabaseError("temp_store must be MEMORY to avoid plaintext temporary files")
    cipher_status = conn.execute("PRAGMA cipher_status").fetchone()
    if not cipher_status or str(cipher_status[0]) != "1":
        raise DatabaseError("SQLCipher connection is not keyed (cipher_status != 1)")
    if conn.execute("PRAGMA secure_delete").fetchone()[0] != 1:
        raise DatabaseError("secure_delete pragma is not enabled")
    status = conn.execute("PRAGMA cipher_integrity_check").fetchall()
    if status:  # non-empty result means page-level HMAC failures
        raise DatabaseError("cipher_integrity_check reported corrupt pages")


def integrity_check(conn) -> bool:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return bool(row and row[0] == "ok")
