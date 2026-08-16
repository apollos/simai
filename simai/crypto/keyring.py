"""Vault key management (design doc section 15.2).

Layout:

    passphrase --Argon2id(salt, versioned params)--> KEK
    KEK --XChaCha20-Poly1305--> unwrap(Vault Root Key)
    Vault Root Key --HKDF--> SQLCipher Key
                          --> Inbox Private-Key Wrap Key
                          --> Audit HMAC Key

The public key header file is stored next to the database and may be
copied freely: it contains no secret material in usable form.  The
passphrase never touches disk, argv, environment or logs.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from nacl import pwhash
from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
)
from nacl.public import PrivateKey, PublicKey

FORMAT_VERSION = 1
VAULT_ROOT_KEY_BYTES = 32
XCHACHA_NONCE_BYTES = 24

# Versioned Argon2id parameters (moderate profile of libsodium).
KDF_PARAMS_V1 = {
    "ops_limit": pwhash.argon2id.OPSLIMIT_MODERATE,
    "mem_limit": pwhash.argon2id.MEMLIMIT_MODERATE,
}


class VaultError(Exception):
    pass


class WrongPassphrase(VaultError):
    pass


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(data: str) -> bytes:
    try:
        return base64.b64decode(data.encode("ascii"), validate=True)
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise VaultError("Invalid base64 in vault metadata") from exc


def hkdf_sha256(key: bytes, info: bytes, length: int = 32, salt: bytes = b"") -> bytes:
    """RFC 5869 HKDF-Extract + Expand with SHA-256 (stdlib only)."""
    prk = hmac.new(salt or b"\x00" * 32, key, hashlib.sha256).digest()
    okm = b""
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


@dataclass
class UnlockedKeys:
    """Derived keys; live only in process memory while unlocked."""

    vault_root_key: bytes
    sqlcipher_key: bytes
    inbox_wrap_key: bytes
    audit_hmac_key: bytes
    excerpt_key: bytes
    inbox_private_key: bytes
    inbox_public_key: bytes

    def sqlcipher_hex(self) -> str:
        return self.sqlcipher_key.hex()

    def wipe(self) -> None:
        # Python cannot securely zero immutable bytes; drop references so the
        # objects become collectable as soon as possible.
        self.vault_root_key = b""
        self.sqlcipher_key = b""
        self.inbox_wrap_key = b""
        self.audit_hmac_key = b""
        self.excerpt_key = b""
        self.inbox_private_key = b""


def _derive_kek(passphrase: str, salt: bytes, params: dict) -> bytes:
    # Header fields are unauthenticated until the KEK is derived.  Refuse
    # attacker-controlled resource parameters instead of allowing a copied or
    # modified header to force an excessive Argon2 allocation.
    expected = {k: int(v) for k, v in KDF_PARAMS_V1.items()}
    if not isinstance(params, dict):
        raise VaultError("Missing Argon2id parameters")
    try:
        actual = {k: int(params.get(k, -1)) for k in expected}
    except (TypeError, ValueError) as exc:
        raise VaultError("Invalid Argon2id parameters") from exc
    if actual != expected or len(salt) != pwhash.argon2id.SALTBYTES:
        raise VaultError("Unsupported or unsafe Argon2id parameters")
    return pwhash.argon2id.kdf(
        32,
        passphrase.encode("utf-8"),
        salt,
        opslimit=int(params["ops_limit"]),
        memlimit=int(params["mem_limit"]),
    )


def _derive_subkeys(vault_root_key: bytes, header: dict) -> UnlockedKeys:
    sqlcipher_key = hkdf_sha256(vault_root_key, b"simai/sqlcipher-key/v1")
    inbox_wrap_key = hkdf_sha256(vault_root_key, b"simai/inbox-wrap-key/v1")
    audit_hmac_key = hkdf_sha256(vault_root_key, b"simai/audit-hmac-key/v1")
    excerpt_key = hkdf_sha256(vault_root_key, b"simai/excerpt-key/v1")

    wrapped = _b64d(header["wrapped_sealed_inbox_private_key"])
    nonce = _b64d(header["sealed_inbox_private_key_nonce"])
    try:
        inbox_private = crypto_aead_xchacha20poly1305_ietf_decrypt(
            wrapped, b"simai-inbox-private", nonce, inbox_wrap_key
        )
    except Exception as exc:  # cryptographic failure => treat as corrupt vault
        raise VaultError("Failed to unwrap sealed-inbox private key") from exc

    return UnlockedKeys(
        vault_root_key=vault_root_key,
        sqlcipher_key=sqlcipher_key,
        inbox_wrap_key=inbox_wrap_key,
        audit_hmac_key=audit_hmac_key,
        excerpt_key=excerpt_key,
        inbox_private_key=inbox_private,
        inbox_public_key=_b64d(header["sealed_inbox_public_key"]),
    )


def create_vault(header_path: Path, passphrase: str) -> tuple[UnlockedKeys, dict]:
    """Initialise a brand-new vault. Returns unlocked keys and a one-time
    offline recovery pack (never persisted server-side)."""
    if header_path.exists():
        raise VaultError(f"Vault header already exists: {header_path}")

    salt = secrets.token_bytes(pwhash.argon2id.SALTBYTES)
    kek = _derive_kek(passphrase, salt, KDF_PARAMS_V1)
    vault_root_key = secrets.token_bytes(VAULT_ROOT_KEY_BYTES)

    nonce = secrets.token_bytes(XCHACHA_NONCE_BYTES)
    wrapped_vrk = crypto_aead_xchacha20poly1305_ietf_encrypt(vault_root_key, b"simai-vault-root", nonce, kek)

    inbox_key = PrivateKey.generate()
    inbox_wrap_key = hkdf_sha256(vault_root_key, b"simai/inbox-wrap-key/v1")
    priv_nonce = secrets.token_bytes(XCHACHA_NONCE_BYTES)
    wrapped_priv = crypto_aead_xchacha20poly1305_ietf_encrypt(
        bytes(inbox_key), b"simai-inbox-private", priv_nonce, inbox_wrap_key
    )

    header = {
        "format_version": FORMAT_VERSION,
        "vault_id": hashlib.sha256(bytes(inbox_key.public_key)).hexdigest(),
        "kdf_type": "argon2id",
        "kdf_parameters": {k: int(v) for k, v in KDF_PARAMS_V1.items()},
        "salt": _b64e(salt),
        "wrap_algorithm": "xchacha20_poly1305",
        "wrap_nonce": _b64e(nonce),
        "wrapped_vault_root_key": _b64e(wrapped_vrk),
        "sealed_inbox_public_key": _b64e(bytes(inbox_key.public_key)),
        "sealed_inbox_private_key_nonce": _b64e(priv_nonce),
        "wrapped_sealed_inbox_private_key": _b64e(wrapped_priv),
    }
    _atomic_owner_write(header_path, json.dumps(header, indent=2).encode("utf-8"))

    recovery_pack = {
        "type": "simai-offline-recovery-pack",
        "format_version": FORMAT_VERSION,
        "vault_id": header["vault_id"],
        "vault_root_key": _b64e(vault_root_key),
        "warning": "Keep offline. Anyone holding this key can derive the database key.",
    }
    return _derive_subkeys(vault_root_key, header), recovery_pack


def load_header(header_path: Path) -> dict:
    if not header_path.is_file():
        raise VaultError(f"Vault header not found: {header_path}")
    if header_path.stat().st_size > 64 * 1024:
        raise VaultError("Vault header is unexpectedly large")
    try:
        header = json.loads(header_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VaultError("Vault header is unreadable or corrupt") from exc
    if header.get("format_version") != FORMAT_VERSION:
        raise VaultError("Unsupported vault header format_version")
    if header.get("kdf_type") != "argon2id":
        raise VaultError("Unsupported vault KDF")
    return header


def unlock_vault(header_path: Path, passphrase: str) -> UnlockedKeys:
    header = load_header(header_path)
    salt = _b64d(header["salt"])
    kek = _derive_kek(passphrase, salt, header["kdf_parameters"])
    try:
        vault_root_key = crypto_aead_xchacha20poly1305_ietf_decrypt(
            _b64d(header["wrapped_vault_root_key"]),
            b"simai-vault-root",
            _b64d(header["wrap_nonce"]),
            kek,
        )
    except Exception as exc:
        raise WrongPassphrase("Passphrase incorrect or header corrupt") from exc
    return _derive_subkeys(vault_root_key, header)


def unlock_with_recovery_pack(header_path: Path, pack: dict) -> UnlockedKeys:
    header = load_header(header_path)
    if pack.get("type") != "simai-offline-recovery-pack":
        raise VaultError("Not a simai recovery pack")
    if header.get("vault_id") and pack.get("vault_id") != header["vault_id"]:
        raise VaultError("Recovery pack belongs to a different vault")
    return _derive_subkeys(_b64d(pack["vault_root_key"]), header)


def change_passphrase(header_path: Path, old_passphrase: str, new_passphrase: str) -> None:
    """Re-wrap the Vault Root Key only; the database is not re-encrypted."""
    keys = unlock_vault(header_path, old_passphrase)
    try:
        header = load_header(header_path)
        salt = secrets.token_bytes(pwhash.argon2id.SALTBYTES)
        kek = _derive_kek(new_passphrase, salt, KDF_PARAMS_V1)
        nonce = secrets.token_bytes(XCHACHA_NONCE_BYTES)
        header["salt"] = _b64e(salt)
        header["kdf_parameters"] = {k: int(v) for k, v in KDF_PARAMS_V1.items()}
        header["wrap_nonce"] = _b64e(nonce)
        header["wrapped_vault_root_key"] = _b64e(
            crypto_aead_xchacha20poly1305_ietf_encrypt(keys.vault_root_key, b"simai-vault-root", nonce, kek)
        )
        _atomic_owner_write(header_path, json.dumps(header, indent=2).encode("utf-8"))
    finally:
        keys.wipe()


def _atomic_owner_write(path: Path, payload: bytes) -> None:
    """Atomically replace a sensitive file with mode 0600 and durable metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        path.chmod(0o600)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def inbox_public_key(header_path: Path) -> PublicKey:
    """Public half of the sealed-inbox keypair; safe to hand to the Plugin."""
    header = load_header(header_path)
    return PublicKey(_b64d(header["sealed_inbox_public_key"]))
