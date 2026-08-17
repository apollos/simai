"""Sealed inbox (design doc sections 6.3 and 15.3).

A short-lived, write-only queue that survives service restarts and
database lock.  Each item is a NaCl sealed box (anonymous public-key
encryption) over a JSON envelope:

    {
      "schema_version": 2,
      "binding_id":   "...",
      "channel":      "openclaw-weixin",
      "account_id":   "...",
      "sender_key":   "...",
      "conversation_id": "...",
      "is_group":     false,
      "capture_mode": "passive|explicit",
      "message_id":   "...",        # may be null only when session_key is present
      "session_key":  "...",        # may be null only when message_id is present
      "dictation_id": "...",        # nullable: groups one dictation session
      "speaker":      "owner",      # owner | assistant (dictation context)
      "captured_at":  "ISO-8601",
      "body":         "final user text"
    }

Files are written atomically (tmp file + fsync + rename) so a crash never
leaves half an item.  Items are deleted only after the daily transaction
that consumed them commits successfully.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nacl.public import PrivateKey, PublicKey, SealedBox

SCHEMA_VERSION = 2
ITEM_SUFFIX = ".sealed"
CAPTURE_MODES = frozenset({"passive", "explicit"})
SPEAKERS = frozenset({"owner", "assistant"})

# These are protocol limits, not merely Web/API validation hints.  They are
# enforced both before encryption and after decryption so a copied or locally
# planted queue file cannot make the worker allocate unbounded memory.
MAX_BODY_BYTES = 256 * 1024
MAX_PLAINTEXT_BYTES = 2 * 1024 * 1024
SEALED_BOX_OVERHEAD_BYTES = 48  # libsodium crypto_box_SEALBYTES
MAX_SEALED_FILE_BYTES = MAX_PLAINTEXT_BYTES + SEALED_BOX_OVERHEAD_BYTES

MAX_BINDING_ID_BYTES = 128
MAX_CHANNEL_BYTES = 128
MAX_ACCOUNT_ID_BYTES = 512
MAX_SENDER_KEY_BYTES = 512
MAX_CONVERSATION_ID_BYTES = 2048
MAX_MESSAGE_ID_BYTES = 1024
MAX_SESSION_KEY_BYTES = 2048
MAX_DICTATION_ID_BYTES = 128
MAX_CAPTURED_AT_BYTES = 64

ENVELOPE_FIELDS = frozenset(
    {
        "schema_version",
        "binding_id",
        "channel",
        "account_id",
        "sender_key",
        "conversation_id",
        "is_group",
        "capture_mode",
        "message_id",
        "session_key",
        "dictation_id",
        "speaker",
        "captured_at",
        "body",
    }
)

# dictation_id and speaker were added within schema v2; envelopes sealed by an
# older plugin build simply omit them and are read with their defaults
# (dictation_id=None, speaker="owner").
_OPTIONAL_ENVELOPE_FIELDS = frozenset({"dictation_id", "speaker"})


class InboxError(Exception):
    pass


@dataclass
class InboxItem:
    path: Path
    binding_id: str
    message_id: str | None
    session_key: str | None
    captured_at: str
    body: str
    capture_mode: str = "passive"
    channel: str = ""
    account_id: str = ""
    sender_key: str = ""
    conversation_id: str | None = None
    is_group: bool = False
    dictation_id: str | None = None
    speaker: str = "owner"

    def message_fingerprint(self, hmac_key: bytes) -> str:
        """Stable dedupe key: HMAC(binding_id | message_id-or-fallback)."""
        if self.message_id:
            material = f"{self.binding_id}|mid:{self.message_id}"
        else:
            digest = hashlib.sha256(self.body.encode("utf-8")).hexdigest()
            material = f"{self.binding_id}|sk:{self.session_key}|body:{digest}"
        return hmac.new(hmac_key, material.encode("utf-8"), hashlib.sha256).hexdigest()


def seal_item(
    inbox_dir: Path,
    public_key: PublicKey,
    binding_id: str,
    body: str,
    *,
    channel: str,
    account_id: str,
    sender_key: str,
    conversation_id: str | None,
    is_group: bool,
    message_id: str | None = None,
    session_key: str | None = None,
    capture_mode: str = "passive",
    dictation_id: str | None = None,
    speaker: str = "owner",
    max_body_bytes: int = MAX_BODY_BYTES,
) -> Path:
    """Encrypt and atomically persist one user message. Usable while locked."""
    plaintext = _encode_item(
        binding_id=binding_id,
        body=body,
        channel=channel,
        account_id=account_id,
        sender_key=sender_key,
        conversation_id=conversation_id,
        is_group=is_group,
        message_id=message_id,
        session_key=session_key,
        capture_mode=capture_mode,
        dictation_id=dictation_id,
        speaker=speaker,
        max_body_bytes=max_body_bytes,
    )
    ciphertext = SealedBox(public_key).encrypt(plaintext)
    if len(ciphertext) > MAX_SEALED_FILE_BYTES:
        raise InboxError("sealed inbox item exceeds protocol file-size limit")

    _ensure_inbox_dir(inbox_dir)
    name = f"{time.time_ns():020d}-{secrets.token_hex(6)}{ITEM_SUFFIX}"
    final = inbox_dir / name
    tmp = inbox_dir / (name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(ciphertext)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    final.chmod(0o600)
    dir_fd = os.open(
        inbox_dir,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return final


def estimate_sealed_item_size(
    *,
    binding_id: str,
    body: str,
    channel: str,
    account_id: str,
    sender_key: str,
    conversation_id: str | None,
    is_group: bool,
    message_id: str | None,
    session_key: str | None,
    capture_mode: str,
    dictation_id: str | None = None,
    speaker: str = "owner",
    max_body_bytes: int = MAX_BODY_BYTES,
) -> int:
    """Return the exact ciphertext byte count for a prospective item."""
    plaintext = _encode_item(
        binding_id=binding_id,
        body=body,
        channel=channel,
        account_id=account_id,
        sender_key=sender_key,
        conversation_id=conversation_id,
        is_group=is_group,
        message_id=message_id,
        session_key=session_key,
        capture_mode=capture_mode,
        dictation_id=dictation_id,
        speaker=speaker,
        max_body_bytes=max_body_bytes,
    )
    return len(plaintext) + SEALED_BOX_OVERHEAD_BYTES


def list_items(inbox_dir: Path) -> list[Path]:
    try:
        inbox_stat = inbox_dir.lstat()
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(inbox_stat.st_mode) or stat.S_ISLNK(inbox_stat.st_mode):
        raise InboxError("sealed inbox path must be a real directory")
    items: list[Path] = []
    for path in inbox_dir.iterdir():
        if path.suffix != ITEM_SUFFIX:
            continue
        try:
            path_stat = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(path_stat.st_mode):
            raise InboxError(f"sealed inbox must not contain symlinks: {path.name}")
        if stat.S_ISREG(path_stat.st_mode):
            items.append(path)
    return sorted(items)


def open_item(path: Path, private_key_bytes: bytes) -> InboxItem:
    ciphertext = _read_ciphertext(path)
    try:
        plaintext = SealedBox(PrivateKey(private_key_bytes)).decrypt(ciphertext)
        if len(plaintext) > MAX_PLAINTEXT_BYTES:
            raise InboxError(f"Inbox plaintext is too large in {path.name}")
        envelope = json.loads(plaintext.decode("utf-8"), object_pairs_hook=_unique_object)
    except InboxError:
        raise
    except Exception as exc:
        raise InboxError(f"Cannot decrypt or decode inbox item {path.name}") from exc
    if (
        type(envelope) is not dict
        or set(envelope) - ENVELOPE_FIELDS
        or not (ENVELOPE_FIELDS - set(envelope)) <= _OPTIONAL_ENVELOPE_FIELDS
    ):
        raise InboxError(f"Invalid inbox envelope fields in {path.name}")
    if type(envelope["schema_version"]) is not int or envelope["schema_version"] != SCHEMA_VERSION:
        raise InboxError(f"Unsupported inbox schema in {path.name}")
    try:
        validate_item_fields(
            binding_id=envelope["binding_id"],
            body=envelope["body"],
            channel=envelope["channel"],
            account_id=envelope["account_id"],
            sender_key=envelope["sender_key"],
            conversation_id=envelope["conversation_id"],
            is_group=envelope["is_group"],
            message_id=envelope["message_id"],
            session_key=envelope["session_key"],
            capture_mode=envelope["capture_mode"],
            dictation_id=envelope.get("dictation_id"),
            speaker=envelope.get("speaker", "owner"),
        )
        _validate_text(
            envelope["captured_at"],
            "captured_at",
            MAX_CAPTURED_AT_BYTES,
        )
        parsed = datetime.fromisoformat(envelope["captured_at"])
    except (InboxError, ValueError) as exc:
        raise InboxError(f"Invalid inbox envelope in {path.name}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InboxError(f"Naive captured_at in {path.name}")
    return InboxItem(
        path=path,
        binding_id=envelope["binding_id"],
        message_id=envelope["message_id"],
        session_key=envelope["session_key"],
        captured_at=envelope["captured_at"],
        body=envelope["body"],
        capture_mode=envelope["capture_mode"],
        channel=envelope["channel"],
        account_id=envelope["account_id"],
        sender_key=envelope["sender_key"],
        conversation_id=envelope["conversation_id"],
        is_group=envelope["is_group"],
        dictation_id=envelope.get("dictation_id"),
        speaker=envelope.get("speaker", "owner"),
    )


def validate_item_fields(
    *,
    binding_id: object,
    body: object,
    channel: object,
    account_id: object,
    sender_key: object,
    conversation_id: object,
    is_group: object,
    message_id: object,
    session_key: object,
    capture_mode: object,
    dictation_id: object = None,
    speaker: object = "owner",
    max_body_bytes: int = MAX_BODY_BYTES,
) -> None:
    """Validate the typed v2 fields shared by ingress, seal and open."""
    if type(max_body_bytes) is not int or not 1 <= max_body_bytes <= MAX_BODY_BYTES:
        raise InboxError("max_body_bytes is outside the protocol limit")
    _validate_text(binding_id, "binding_id", MAX_BINDING_ID_BYTES)
    _validate_text(channel, "channel", MAX_CHANNEL_BYTES)
    _validate_text(account_id, "account_id", MAX_ACCOUNT_ID_BYTES)
    _validate_text(sender_key, "sender_key", MAX_SENDER_KEY_BYTES)
    _validate_text(conversation_id, "conversation_id", MAX_CONVERSATION_ID_BYTES, nullable=True)
    _validate_text(message_id, "message_id", MAX_MESSAGE_ID_BYTES, nullable=True)
    _validate_text(session_key, "session_key", MAX_SESSION_KEY_BYTES, nullable=True)
    _validate_text(dictation_id, "dictation_id", MAX_DICTATION_ID_BYTES, nullable=True)
    if type(speaker) is not str or speaker not in SPEAKERS:
        raise InboxError("speaker must be owner or assistant")
    if message_id is None and session_key is None:
        raise InboxError("message_id and session_key cannot both be null")
    if type(is_group) is not bool:
        raise InboxError("is_group must be boolean")
    if type(capture_mode) is not str or capture_mode not in CAPTURE_MODES:
        raise InboxError("capture_mode must be passive or explicit")
    if type(body) is not str or not body.strip():
        raise InboxError("body must be a non-empty string")
    if "\x00" in body:
        raise InboxError("body must not contain NUL")
    if len(body.encode("utf-8")) > max_body_bytes:
        raise InboxError("body exceeds the configured byte limit")


def _validate_text(value: object, field: str, max_bytes: int, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if type(value) is not str or not value or not value.strip():
        suffix = " or null" if nullable else ""
        raise InboxError(f"{field} must be a non-empty string{suffix}")
    if len(value.encode("utf-8")) > max_bytes:
        raise InboxError(f"{field} exceeds its byte limit")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise InboxError(f"{field} contains control characters")


def _encode_item(
    *,
    binding_id: object,
    body: object,
    channel: object,
    account_id: object,
    sender_key: object,
    conversation_id: object,
    is_group: object,
    message_id: object,
    session_key: object,
    capture_mode: object,
    dictation_id: object,
    speaker: object,
    max_body_bytes: int,
) -> bytes:
    validate_item_fields(
        binding_id=binding_id,
        body=body,
        channel=channel,
        account_id=account_id,
        sender_key=sender_key,
        conversation_id=conversation_id,
        is_group=is_group,
        message_id=message_id,
        session_key=session_key,
        capture_mode=capture_mode,
        dictation_id=dictation_id,
        speaker=speaker,
        max_body_bytes=max_body_bytes,
    )
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "binding_id": binding_id,
        "message_id": message_id,
        "session_key": session_key,
        "dictation_id": dictation_id,
        "speaker": speaker,
        "captured_at": datetime.now(UTC).isoformat(timespec="microseconds"),
        "body": body,
        "capture_mode": capture_mode,
        "channel": channel,
        "account_id": account_id,
        "sender_key": sender_key,
        "conversation_id": conversation_id,
        "is_group": is_group,
    }
    plaintext = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(plaintext) > MAX_PLAINTEXT_BYTES:
        raise InboxError("inbox plaintext exceeds protocol limit")
    return plaintext


def _ensure_inbox_dir(inbox_dir: Path) -> None:
    try:
        inbox_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        inbox_stat = inbox_dir.lstat()
    except OSError as exc:
        raise InboxError("cannot create sealed inbox directory") from exc
    if not stat.S_ISDIR(inbox_stat.st_mode) or stat.S_ISLNK(inbox_stat.st_mode):
        raise InboxError("sealed inbox path must be a real directory")
    inbox_dir.chmod(0o700)


def _read_ciphertext(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as fh:
            file_stat = os.fstat(fh.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise InboxError(f"Inbox item is not a regular file: {path.name}")
            if not SEALED_BOX_OVERHEAD_BYTES < file_stat.st_size <= MAX_SEALED_FILE_BYTES:
                raise InboxError(f"Inbox item has invalid size: {path.name}")
            ciphertext = fh.read(MAX_SEALED_FILE_BYTES + 1)
    except InboxError:
        raise
    except OSError as exc:
        raise InboxError(f"Cannot read inbox item {path.name}") from exc
    if len(ciphertext) != file_stat.st_size or len(ciphertext) > MAX_SEALED_FILE_BYTES:
        raise InboxError(f"Inbox item changed while reading: {path.name}")
    return ciphertext


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InboxError(f"Duplicate envelope field: {key}")
        result[key] = value
    return result


def delete_item(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def oldest_item_age_days(inbox_dir: Path) -> float | None:
    items = list_items(inbox_dir)
    if not items:
        return None
    oldest = min(p.stat().st_mtime for p in items)
    return (time.time() - oldest) / 86400.0
