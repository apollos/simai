"""Local sealed-inbox ingress over a Unix domain socket (section 15.3).

The OpenClaw plugin connects to this socket and submits one JSON object
per line:

    {"binding_id": "...", "channel": "...", "account_id": "...",
     "sender_key": "...", "conversation_id": "...", "is_group": false,
     "capture_mode": "passive", "message_id": "...",
     "session_key": "...", "body": "..."}

The service validates that the binding exists and is enabled in config
(the plugin has already performed channel/account/sender whitelisting),
seals the envelope with the inbox PUBLIC key and stores it atomically.
This works while the vault is locked - no secret key is involved.

No network port is ever opened for ingestion; file permissions on the
socket restrict access to the local user.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat

from ..config import Config
from ..crypto import keyring, sealed_inbox

log = logging.getLogger("simai.ingress")

INGRESS_FIELDS = sealed_inbox.ENVELOPE_FIELDS - {"schema_version", "captured_at"}


class IngressServer:
    def __init__(self, config: Config):
        self.config = config
        self._server: asyncio.AbstractServer | None = None
        self._queue_lock = asyncio.Lock()

    async def start(self) -> None:
        if os.name == "nt":
            log.warning("unix-socket ingress unavailable on Windows; skipped")
            return
        if self._server is not None:
            return
        if not self.config.key_header_path.is_file():
            log.warning("vault not initialized; ingress not started")
            return
        socket_path = self.config.inbox_socket_path
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_path.parent.chmod(0o700)
        _remove_existing_socket(socket_path)
        max_bytes = _body_limit(self.config)
        line_limit = min(
            sealed_inbox.MAX_PLAINTEXT_BYTES,
            max_bytes * 6 + 32 * 1024,
        )
        self._server = await asyncio.start_unix_server(
            self._handle,
            path=str(socket_path),
            limit=line_limit,
        )
        os.chmod(socket_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        log.info("sealed-inbox ingress listening on %s", socket_path)

    @property
    def listening(self) -> bool:
        return self._server is not None

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        enabled_bindings = {b.id: b for b in self.config.source_bindings() if b.enabled}
        max_bytes = _body_limit(self.config)
        line_limit = min(
            sealed_inbox.MAX_PLAINTEXT_BYTES,
            max_bytes * 6 + 32 * 1024,
        )
        try:
            public_key = keyring.inbox_public_key(self.config.key_header_path)
            while True:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    writer.write(b'{"ok": false, "error": "payload_too_large"}\n')
                    await writer.drain()
                    break
                if not line:
                    break
                if len(line) > line_limit:
                    writer.write(b'{"ok": false, "error": "payload_too_large"}\n')
                    await writer.drain()
                    break
                try:
                    payload = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_object)
                    if type(payload) is not dict or set(payload) != INGRESS_FIELDS:
                        raise ValueError("unexpected ingress fields")
                    binding_id = payload["binding_id"]
                    body = payload["body"]
                    capture_mode = payload["capture_mode"]
                    sealed_inbox.validate_item_fields(
                        binding_id=binding_id,
                        body=body,
                        channel=payload["channel"],
                        account_id=payload["account_id"],
                        sender_key=payload["sender_key"],
                        conversation_id=payload["conversation_id"],
                        is_group=payload["is_group"],
                        message_id=payload["message_id"],
                        session_key=payload["session_key"],
                        capture_mode=capture_mode,
                        max_body_bytes=max_bytes,
                    )
                except (UnicodeDecodeError, ValueError, sealed_inbox.InboxError):
                    writer.write(b'{"ok": false, "error": "bad_payload"}\n')
                    await writer.drain()
                    continue
                binding = enabled_bindings.get(binding_id)
                identity_matches = bool(
                    binding
                    and payload["channel"] == binding.channel
                    and payload["account_id"] == binding.account_id
                    and payload["sender_key"] == binding.sender_key
                    and payload["conversation_id"] == binding.conversation_id
                    and (not payload["is_group"] or binding.allow_group)
                    and (capture_mode != "passive" or binding.passive_capture)
                )
                # Validate the full immutable identity tuple against the core
                # YAML.  A stale or misconfigured Plugin binding id alone is
                # never sufficient authority.
                if not identity_matches:
                    writer.write(b'{"ok": false, "error": "binding_refused"}\n')
                    await writer.drain()
                    log.warning("ingress refused binding=%s", binding_id)
                    continue
                pending_bytes = sealed_inbox.estimate_sealed_item_size(
                    binding_id=binding_id,
                    body=body,
                    channel=payload["channel"],
                    account_id=payload["account_id"],
                    sender_key=payload["sender_key"],
                    conversation_id=payload["conversation_id"],
                    is_group=payload["is_group"],
                    message_id=payload["message_id"],
                    session_key=payload["session_key"],
                    capture_mode=capture_mode,
                    max_body_bytes=max_bytes,
                )
                async with self._queue_lock:
                    queued = sealed_inbox.list_items(self.config.inbox_dir)
                    max_items = _nonnegative_config_int(
                        self.config,
                        "max_queue_items",
                        10000,
                    )
                    max_queue_bytes = _nonnegative_config_int(
                        self.config,
                        "max_queue_bytes",
                        512 * 1024 * 1024,
                    )
                    queued_bytes = 0
                    for item in queued:
                        try:
                            queued_bytes += item.lstat().st_size
                        except FileNotFoundError:
                            continue
                    if len(queued) + 1 > max_items or queued_bytes + pending_bytes > max_queue_bytes:
                        writer.write(b'{"ok": false, "error": "queue_full"}\n')
                        await writer.drain()
                        log.error("ingress queue limit reached; item retained by sender for retry")
                        continue
                    sealed_inbox.seal_item(
                        self.config.inbox_dir,
                        public_key,
                        binding_id=binding_id,
                        body=body,
                        channel=payload["channel"],
                        account_id=payload["account_id"],
                        sender_key=payload["sender_key"],
                        conversation_id=payload["conversation_id"],
                        is_group=payload["is_group"],
                        message_id=payload["message_id"],
                        session_key=payload["session_key"],
                        capture_mode=capture_mode,
                        max_body_bytes=max_bytes,
                    )
                writer.write(b'{"ok": true}\n')
                await writer.drain()
                log.info("ingress sealed item binding=%s", binding_id)  # no content logged
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass


def _body_limit(config: Config) -> int:
    configured = _positive_config_int(config, "max_message_bytes", sealed_inbox.MAX_BODY_BYTES)
    return min(configured, sealed_inbox.MAX_BODY_BYTES)


def _positive_config_int(config: Config, key: str, default: int) -> int:
    raw = config.section("sealed_inbox").get(key, default)
    if type(raw) is not int or raw <= 0:
        raise ValueError(f"sealed_inbox.{key} must be a positive integer")
    return raw


def _nonnegative_config_int(config: Config, key: str, default: int) -> int:
    raw = config.section("sealed_inbox").get(key, default)
    if type(raw) is not int or raw < 0:
        raise ValueError(f"sealed_inbox.{key} must be a non-negative integer")
    return raw


def _remove_existing_socket(socket_path) -> None:
    try:
        socket_stat = socket_path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(socket_stat.st_mode) or not stat.S_ISSOCK(socket_stat.st_mode):
        raise RuntimeError(f"refusing to replace non-socket ingress path: {socket_path}")
    socket_path.unlink()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate ingress field: {key}")
        result[key] = value
    return result
