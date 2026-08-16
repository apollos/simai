"""Encrypted backup & restore (section 22).

- Uses the SQLite Online Backup API through a second SQLCipher connection
  whose key is set BEFORE the backup runs, so the target file is
  encrypted by construction (never a plain file copy during writes).
- After writing, verifies: opens with the correct key, integrity check,
  cipher settings present, and that a WRONG key fails to open.
- The sealed inbox is copied as ciphertext; the vault header rides along
  so a backup set is restorable on a fresh machine (given the passphrase).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
from pathlib import Path

from ..db import engine
from ..db.engine import now_iso


class BackupError(Exception):
    pass


def create_backup(
    conn,
    sqlcipher_key_hex: str,
    backup_dir: Path,
    key_header_path: Path,
    inbox_dir: Path,
) -> dict:
    if engine.sqlcipher is None:
        raise BackupError("sqlcipher3 driver not available")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    stamp = now_iso().replace(":", "").replace("+", "Z")[:17]
    set_dir = backup_dir / f"backup-{stamp}-{secrets.token_hex(4)}"
    set_dir.mkdir(mode=0o700)

    db_target = set_dir / "simai.db"
    target = engine.sqlcipher.connect(str(db_target))
    try:
        target.execute(f"PRAGMA key = \"x'{sqlcipher_key_hex}'\"")
        conn.backup(target)  # SQLite Online Backup API, consistent snapshot
        target.commit()
    finally:
        target.close()

    _verify_encrypted(db_target, sqlcipher_key_hex)
    db_target.chmod(0o600)

    shutil.copy2(key_header_path, set_dir / key_header_path.name)
    (set_dir / key_header_path.name).chmod(0o600)
    inbox_target = set_dir / "inbox"
    inbox_target.mkdir(mode=0o700)
    inbox_count = 0
    if inbox_dir.is_dir():
        for item in inbox_dir.iterdir():
            if item.suffix == ".sealed":
                shutil.copy2(item, inbox_target / item.name)
                (inbox_target / item.name).chmod(0o600)
                inbox_count += 1
    file_hashes = {
        "simai.db": _sha256(db_target),
        key_header_path.name: _sha256(set_dir / key_header_path.name),
    }
    for item in inbox_target.iterdir():
        if item.suffix == ".sealed":
            file_hashes[f"inbox/{item.name}"] = _sha256(item)

    manifest = {
        "created_at": now_iso(),
        "schema_version": _meta(conn, "schema_version"),
        "counts": {
            "nodes": conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
            "revisions": conn.execute("SELECT COUNT(*) FROM node_revisions").fetchone()[0],
            "relations": conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0],
            "candidates": conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0],
            "sealed_inbox_items": inbox_count,
        },
        "files": file_hashes,
    }
    manifest["manifest_hmac"] = _manifest_hmac(manifest, sqlcipher_key_hex)
    manifest_path = set_dir / "manifest.json"
    fd = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    return {"backup_dir": str(set_dir), **manifest}


def _verify_encrypted(db_path: Path, key_hex: str) -> None:
    # 1. correct key opens and passes integrity check
    good = engine.sqlcipher.connect(str(db_path))
    try:
        good.execute(f"PRAGMA key = \"x'{key_hex}'\"")
        if good.execute("PRAGMA cipher_version").fetchone() is None:
            raise BackupError("Backup target is not a SQLCipher database")
        cipher_status = good.execute("PRAGMA cipher_status").fetchone()
        if not cipher_status or str(cipher_status[0]) != "1":
            raise BackupError("Backup target SQLCipher key is not active")
        good.execute("PRAGMA temp_store = MEMORY")
        if good.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("Backup failed integrity check")
    finally:
        good.close()
    # 2. wrong key must fail
    wrong = engine.sqlcipher.connect(str(db_path))
    try:
        wrong.execute(f"PRAGMA key = \"x'{secrets.token_hex(32)}'\"")
        try:
            wrong.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except Exception:
            return  # expected: undecryptable with wrong key
        raise BackupError("SECURITY: backup opened with a wrong key - aborting")
    finally:
        wrong.close()


def verify_restore(set_dir: Path, sqlcipher_key_hex: str) -> dict:
    """Restore drill (section 22.2). Read-only verification of a backup set."""
    manifest = json.loads((set_dir / "manifest.json").read_text(encoding="utf-8"))
    supplied_hmac = manifest.pop("manifest_hmac", None)
    expected_hmac = _manifest_hmac(manifest, sqlcipher_key_hex)
    if not isinstance(supplied_hmac, str) or not hmac.compare_digest(supplied_hmac, expected_hmac):
        raise BackupError("Backup manifest authentication failed")
    db_path = set_dir / "simai.db"
    root = set_dir.resolve()
    if any(path.is_symlink() for path in set_dir.rglob("*")):
        raise BackupError("Symlinks are not allowed in a backup set")
    expected_files = {"manifest.json", *manifest["files"].keys()}
    actual_files = {
        str(path.relative_to(set_dir))
        for path in set_dir.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if actual_files != expected_files:
        raise BackupError("Backup file set differs from manifest")
    for relative, expected_hash in manifest["files"].items():
        raw_path = set_dir / relative
        if raw_path.is_symlink():
            raise BackupError(f"Symlink refused in backup: {relative}")
        path = raw_path.resolve()
        if path != root and root not in path.parents:
            raise BackupError(f"Unsafe path in backup manifest: {relative}")
        if not path.is_file() or _sha256(path) != expected_hash:
            raise BackupError(f"Backup file hash mismatch: {relative}")
    _verify_encrypted(db_path, sqlcipher_key_hex)

    conn = engine.sqlcipher.connect(str(db_path))
    try:
        conn.execute(f"PRAGMA key = \"x'{sqlcipher_key_hex}'\"")
        counts = {
            "nodes": conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
            "revisions": conn.execute("SELECT COUNT(*) FROM node_revisions").fetchone()[0],
            "relations": conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0],
        }
        fts_ok = conn.execute("SELECT COUNT(*) FROM node_fts").fetchone() is not None
        emb_ok = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone() is not None
    finally:
        conn.close()

    mismatches = {k: (counts[k], manifest["counts"][k]) for k in counts if counts[k] != manifest["counts"][k]}
    if mismatches:
        raise BackupError(f"Object counts differ from manifest: {mismatches}")
    inbox_files = list((set_dir / "inbox").glob("*.sealed"))
    if len(inbox_files) != manifest["counts"].get("sealed_inbox_items", 0):
        raise BackupError("Sealed inbox count differs from manifest")
    return {"ok": True, "counts": counts, "fts_readable": fts_ok, "embeddings_readable": emb_ok}


def restore_backup(set_dir: Path, data_dir: Path, sqlcipher_key_hex: str) -> dict:
    """Restore a verified backup set into the data directory. Refuses to
    overwrite an existing live database."""
    result = verify_restore(set_dir, sqlcipher_key_hex)
    live_db = data_dir / "simai.db"
    if live_db.exists():
        raise BackupError(f"A live database already exists at {live_db}; move it away before restoring")
    header = next(set_dir.glob("*.header.json"), None)
    if header is None:
        raise BackupError("Backup set has no vault header")
    header_target = data_dir / header.name
    inbox_target = data_dir / "inbox"
    if header_target.exists() or inbox_target.exists():
        raise BackupError("Restore target already contains vault header or inbox")

    data_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = data_dir.parent / f".simai-restore-{secrets.token_hex(8)}"
    stage.mkdir(mode=0o700)
    installed: list[Path] = []
    try:
        staged_db = stage / "simai.db"
        staged_header = stage / header.name
        staged_inbox = stage / "inbox"
        shutil.copy2(set_dir / "simai.db", staged_db)
        staged_db.chmod(0o600)
        shutil.copy2(header, staged_header)
        staged_header.chmod(0o600)
        inbox_src = set_dir / "inbox"
        if inbox_src.is_dir():
            shutil.copytree(inbox_src, staged_inbox)
        else:
            staged_inbox.mkdir(mode=0o700)
        staged_inbox.chmod(0o700)
        for item in staged_inbox.rglob("*"):
            item.chmod(0o700 if item.is_dir() else 0o600)
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        data_dir.chmod(0o700)
        for staged, target in (
            (staged_db, live_db),
            (staged_header, header_target),
            (staged_inbox, inbox_target),
        ):
            if staged.exists():
                staged.replace(target)
                installed.append(target)
        live_db.chmod(0o600)
        header_target.chmod(0o600)
        inbox_target.chmod(0o700)
        for item in inbox_target.rglob("*"):
            item.chmod(0o700 if item.is_dir() else 0o600)
    except Exception as exc:
        for target in reversed(installed):
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
        raise BackupError("Restore installation failed; partial files were rolled back") from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return result


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest_hmac(manifest: dict, sqlcipher_key_hex: str) -> str:
    root = bytes.fromhex(sqlcipher_key_hex)
    auth_key = hmac.new(root, b"simai/backup-manifest-auth/v1", hashlib.sha256).digest()
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(auth_key, payload, hashlib.sha256).hexdigest()


def _meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None
