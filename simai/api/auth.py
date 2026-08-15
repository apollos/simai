"""Web session management (section 15.1 / 15.4).

The web session is independent from the vault lock: a session expiring
does NOT lock the database.  Sessions are random in-memory tokens set as
HttpOnly cookies; the service binds to 127.0.0.1 only, remote access goes
through an SSH tunnel.
"""

from __future__ import annotations

import os
import secrets
import stat
import threading
import time
from pathlib import Path

from fastapi import HTTPException, Request

SESSION_COOKIE = "simai_session"


class SessionStore:
    def __init__(self, idle_minutes: int = 30):
        self.idle_seconds = idle_minutes * 60
        self._sessions: dict[str, float] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = time.time()
        return token

    def validate(self, token: str | None) -> bool:
        with self._lock:
            if not token or token not in self._sessions:
                return False
            if time.time() - self._sessions[token] > self.idle_seconds:
                del self._sessions[token]
                return False
            self._sessions[token] = time.time()
            return True

    def drop(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(token, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


def require_session(request: Request) -> None:
    store: SessionStore = request.app.state.sessions
    if not store.validate(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(status_code=401, detail="Web session required (login via unlock page)")


def ensure_plugin_token(path: Path) -> str:
    """Create/read the loopback plugin credential with owner-only permissions."""
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeError("Simai plugin token must be a regular non-symlink file")
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise RuntimeError("Simai plugin token must be owned by the service user")
        if info.st_mode & 0o077:
            raise RuntimeError("Simai plugin token must have mode 0600")
        token = path.read_text(encoding="ascii").strip()
        if len(token) < 32:
            raise RuntimeError("Invalid Simai plugin token file")
        return token
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    token = secrets.token_urlsafe(48)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(token)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if tmp.exists():
            tmp.unlink()
    return token
