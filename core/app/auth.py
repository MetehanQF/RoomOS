import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings

logger = logging.getLogger("roomos.security")

SESSION_COOKIE = "roomos_session"
CSRF_COOKIE = "roomos_csrf"
DEFAULT_DEVICE_SCOPES = ("lights:read", "lights:write", "system:read")


def security_audit(event: str, *, device_id: str | None = None, result: str | None = None) -> None:
    fields = [f"timestamp={datetime.now(UTC).isoformat()}", f"event={event}"]
    if device_id:
        fields.append(f"device_id={device_id}")
    if result:
        fields.append(f"result={result}")
    logger.info("RoomOS security %s", " ".join(fields))


@dataclass(frozen=True)
class AuthContext:
    device_id: str
    friendly_name: str
    scopes: frozenset[str]
    session_id: str


@dataclass(frozen=True)
class IssuedSession:
    context: AuthContext
    token: str
    csrf_token: str


class AuthRejected(Exception):
    pass


class PairingRejected(Exception):
    pass


class PairingRateLimited(Exception):
    pass


class AuthStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.database_path = Path(settings.auth_database_path)
        self.secret_path = Path(settings.auth_secret_path)
        self._secret: bytes | None = None
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def initialize(self) -> None:
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.database_path.parent, 0o700)
        except PermissionError:
            # The directory can be an externally managed mount owned by another
            # user. Tightening it is defence in depth; what actually protects
            # the store is that the secret and database below are created 0600
            # and owned by this process. Warn rather than refusing to boot.
            logger.warning(
                "Could not tighten permissions on %s; it is owned by another user. "
                "The auth secret and database are still created with mode 0600.",
                self.database_path.parent,
            )
        if not self.secret_path.exists():
            fd = os.open(self.secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, secrets.token_bytes(32))
            finally:
                os.close(fd)
        os.chmod(self.secret_path, 0o600)
        self._secret = self.secret_path.read_bytes()
        if len(self._secret) < 32:
            raise RuntimeError("RoomOS auth secret is invalid")
        if not self.database_path.exists():
            fd = os.open(self.database_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY,
                    friendly_name TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_seen_at INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    revoked_at INTEGER,
                    scopes TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                    token_digest TEXT NOT NULL,
                    csrf_digest TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    sliding_expires_at INTEGER NOT NULL,
                    absolute_expires_at INTEGER NOT NULL,
                    revoked_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_device ON sessions(device_id);
                CREATE TABLE IF NOT EXISTS pairing_windows (
                    id TEXT PRIMARY KEY,
                    code_digest TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    used_at INTEGER
                );
                """
            )
        os.chmod(self.database_path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=DELETE")
        return db

    def _digest(self, purpose: str, value: str) -> str:
        if self._secret is None:
            raise RuntimeError("AuthStore is not initialized")
        return hmac.new(self._secret, f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _normalize_code(value: str) -> str:
        return value.strip().replace("-", "").replace(" ", "").upper()

    @staticmethod
    def _format_code(value: str) -> str:
        return "-".join(value[index:index + 4] for index in range(0, len(value), 4))

    def create_pairing_window(self, ttl_seconds: int | None = None) -> tuple[str, int]:
        now = int(time.time())
        ttl = ttl_seconds or self.settings.auth_pairing_ttl_seconds
        ttl = max(60, min(ttl, 1800))
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        compact = "".join(secrets.choice(alphabet) for _ in range(12))
        with self._connect() as db:
            db.execute("UPDATE pairing_windows SET used_at=? WHERE used_at IS NULL", (now,))
            db.execute(
                "INSERT INTO pairing_windows(id,code_digest,created_at,expires_at) VALUES(?,?,?,?)",
                (str(uuid.uuid4()), self._digest("pairing", compact), now, now + ttl),
            )
        security_audit("pairing_window_opened", result="success")
        return self._format_code(compact), now + ttl

    def _check_rate_limit(self, key: str) -> None:
        now = time.monotonic()
        window = self.settings.auth_pairing_window_seconds
        attempts = self._attempts[key]
        while attempts and attempts[0] <= now - window:
            attempts.popleft()
        if len(attempts) >= self.settings.auth_pairing_attempts:
            security_audit("pairing_failed", result="rate_limited")
            raise PairingRateLimited
        attempts.append(now)

    def pair(self, code: str, friendly_name: str, rate_key: str) -> IssuedSession:
        self._check_rate_limit(rate_key)
        normalized = self._normalize_code(code)
        digest = self._digest("pairing", normalized)
        now = int(time.time())
        with self._connect() as db:
            candidates = db.execute(
                "SELECT id,code_digest FROM pairing_windows WHERE used_at IS NULL AND expires_at>?",
                (now,),
            ).fetchall()
            match = next(
                (row for row in candidates if hmac.compare_digest(row["code_digest"], digest)),
                None,
            )
            if match is None:
                security_audit("pairing_failed", result="invalid_or_expired")
                raise PairingRejected
            updated = db.execute(
                "UPDATE pairing_windows SET used_at=? WHERE id=? AND used_at IS NULL",
                (now, match["id"]),
            )
            if updated.rowcount != 1:
                security_audit("pairing_failed", result="already_used")
                raise PairingRejected
            device_id = str(uuid.uuid4())
            scopes = tuple(DEFAULT_DEVICE_SCOPES)
            db.execute(
                "INSERT INTO devices(id,friendly_name,created_at,last_seen_at,enabled,scopes) VALUES(?,?,?,?,1,?)",
                (device_id, friendly_name, now, now, json.dumps(scopes)),
            )
            issued = self._create_session(db, device_id, friendly_name, scopes, now)
        self._attempts.pop(rate_key, None)
        security_audit("pairing_succeeded", device_id=device_id, result="success")
        return issued

    def _create_session(
        self,
        db: sqlite3.Connection,
        device_id: str,
        friendly_name: str,
        scopes: tuple[str, ...],
        now: int,
    ) -> IssuedSession:
        session_id = str(uuid.uuid4())
        secret = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        sliding = now + self.settings.auth_session_sliding_days * 86400
        absolute = now + self.settings.auth_session_absolute_days * 86400
        db.execute(
            """INSERT INTO sessions(
                id,device_id,token_digest,csrf_digest,created_at,last_seen_at,
                sliding_expires_at,absolute_expires_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                session_id,
                device_id,
                self._digest("session", secret),
                self._digest("csrf", csrf),
                now,
                now,
                min(sliding, absolute),
                absolute,
            ),
        )
        return IssuedSession(
            context=AuthContext(device_id, friendly_name, frozenset(scopes), session_id),
            token=f"{session_id}.{secret}",
            csrf_token=csrf,
        )

    def authenticate(self, token: str | None) -> AuthContext:
        if not token or "." not in token:
            raise AuthRejected
        session_id, secret = token.split(".", 1)
        now = int(time.time())
        with self._connect() as db:
            row = db.execute(
                """SELECT s.*,d.friendly_name,d.scopes,d.enabled,d.revoked_at AS device_revoked_at
                   FROM sessions s JOIN devices d ON d.id=s.device_id WHERE s.id=?""",
                (session_id,),
            ).fetchone()
            if row is None or not hmac.compare_digest(
                row["token_digest"], self._digest("session", secret)
            ):
                raise AuthRejected
            if (
                row["revoked_at"] is not None
                or row["device_revoked_at"] is not None
                or not row["enabled"]
                or row["sliding_expires_at"] <= now
                or row["absolute_expires_at"] <= now
            ):
                raise AuthRejected
            if now - row["last_seen_at"] >= 300:
                next_expiry = min(
                    now + self.settings.auth_session_sliding_days * 86400,
                    row["absolute_expires_at"],
                )
                db.execute(
                    "UPDATE sessions SET last_seen_at=?,sliding_expires_at=? WHERE id=?",
                    (now, next_expiry, session_id),
                )
                db.execute("UPDATE devices SET last_seen_at=? WHERE id=?", (now, row["device_id"]))
        return AuthContext(
            row["device_id"],
            row["friendly_name"],
            frozenset(json.loads(row["scopes"])),
            session_id,
        )

    def validate_csrf(self, context: AuthContext, token: str | None) -> bool:
        if not token:
            return False
        with self._connect() as db:
            row = db.execute("SELECT csrf_digest FROM sessions WHERE id=?", (context.session_id,)).fetchone()
        return bool(row and hmac.compare_digest(row["csrf_digest"], self._digest("csrf", token)))

    def list_devices(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,friendly_name,created_at,last_seen_at,enabled,revoked_at,scopes FROM devices ORDER BY created_at"
            ).fetchall()
        return [dict(row) | {"scopes": json.loads(row["scopes"])} for row in rows]

    def revoke_sessions(self, device_id: str) -> bool:
        now = int(time.time())
        with self._connect() as db:
            exists = db.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone()
            if not exists:
                return False
            db.execute(
                "UPDATE sessions SET revoked_at=? WHERE device_id=? AND revoked_at IS NULL",
                (now, device_id),
            )
        security_audit("device_sessions_revoked", device_id=device_id, result="success")
        return True

    def disable_device(self, device_id: str) -> bool:
        now = int(time.time())
        with self._connect() as db:
            updated = db.execute("UPDATE devices SET enabled=0 WHERE id=?", (device_id,))
            if updated.rowcount != 1:
                return False
            db.execute(
                "UPDATE sessions SET revoked_at=? WHERE device_id=? AND revoked_at IS NULL",
                (now, device_id),
            )
        security_audit("device_disabled", device_id=device_id, result="success")
        return True

    def revoke_device(self, device_id: str) -> bool:
        now = int(time.time())
        with self._connect() as db:
            updated = db.execute(
                "UPDATE devices SET enabled=0,revoked_at=? WHERE id=? AND revoked_at IS NULL",
                (now, device_id),
            )
            if updated.rowcount != 1:
                return False
            db.execute(
                "UPDATE sessions SET revoked_at=? WHERE device_id=? AND revoked_at IS NULL",
                (now, device_id),
            )
        security_audit("device_revoked", device_id=device_id, result="success")
        return True

    def close(self) -> None:
        self._attempts.clear()
