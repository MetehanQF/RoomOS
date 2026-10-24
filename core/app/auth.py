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
from .panel_preferences import PanelLayout

logger = logging.getLogger("roomos.security")

SESSION_COOKIE = "roomos_session"
CSRF_COOKIE = "roomos_csrf"
DEFAULT_DEVICE_SCOPES = ("lights:read", "lights:write", "system:read")
NATIVE_READONLY_SCOPES = (
    "lights:read",
    "system:read",
    "camera:read",
    "activity:read",
    "routines:read",
)
PAIRING_PROFILES = frozenset({"web", "android-readonly"})
# Every scope the Core checks. telemetry:write:self (7D.3) lets a bound app write its own
# device telemetry only; it is never granted by pairing defaults.
KNOWN_SCOPES = frozenset({
    "lights:read", "lights:write", "system:read", "routines:read", "routines:write",
    "camera:read", "activity:read", "devices:read", "telemetry:write:self",
    "alerts:ack",
    # 7G: manage this device's own push preferences/subscription. Non-default, never paired.
    "notifications:self",
    # 7I: snooze one alert's repeat notifications / select a usage profile. Non-default, never paired;
    # neither can acknowledge, resolve or hide a critical escalation.
    "alerts:snooze",
    "alerts:profile",
})


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
    expires_at: int


class AuthRejected(Exception):
    pass


class AuthorizationChanged(AuthRejected):
    """The session is still valid but its scopes changed; the client must reconnect."""


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
        os.chmod(self.database_path.parent, 0o700)
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
                CREATE TABLE IF NOT EXISTS panel_preferences (
                    device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    layout TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            session_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(sessions)")
            }
            if "kind" not in session_columns:
                db.execute(
                    "ALTER TABLE sessions ADD COLUMN kind TEXT NOT NULL DEFAULT 'web'"
                )
            pairing_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(pairing_windows)")
            }
            if "profile" not in pairing_columns:
                db.execute(
                    "ALTER TABLE pairing_windows ADD COLUMN profile TEXT NOT NULL DEFAULT 'web'"
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

    def create_pairing_window(
        self, ttl_seconds: int | None = None, *, profile: str = "web"
    ) -> tuple[str, int]:
        if profile not in PAIRING_PROFILES:
            raise ValueError("Unsupported pairing profile")
        now = int(time.time())
        ttl = ttl_seconds or self.settings.auth_pairing_ttl_seconds
        ttl = max(60, min(ttl, 1800))
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        compact = "".join(secrets.choice(alphabet) for _ in range(12))
        with self._connect() as db:
            db.execute("UPDATE pairing_windows SET used_at=? WHERE used_at IS NULL", (now,))
            db.execute(
                "INSERT INTO pairing_windows(id,code_digest,created_at,expires_at,profile) VALUES(?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    self._digest("pairing", compact),
                    now,
                    now + ttl,
                    profile,
                ),
            )
        security_audit(f"pairing_window_opened profile={profile}", result="success")
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

    def pair(
        self,
        code: str,
        friendly_name: str,
        rate_key: str,
        *,
        profile: str = "web",
        scopes: tuple[str, ...] | None = None,
        session_kind: str = "web",
    ) -> IssuedSession:
        if profile not in PAIRING_PROFILES:
            raise PairingRejected
        self._check_rate_limit(rate_key)
        normalized = self._normalize_code(code)
        digest = self._digest("pairing", normalized)
        now = int(time.time())
        with self._connect() as db:
            candidates = db.execute(
                "SELECT id,code_digest FROM pairing_windows WHERE used_at IS NULL AND expires_at>? AND profile=?",
                (now, profile),
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
            granted_scopes = scopes or tuple(DEFAULT_DEVICE_SCOPES)
            db.execute(
                "INSERT INTO devices(id,friendly_name,created_at,last_seen_at,enabled,scopes) VALUES(?,?,?,?,1,?)",
                (device_id, friendly_name, now, now, json.dumps(granted_scopes)),
            )
            issued = self._create_session(
                db,
                device_id,
                friendly_name,
                granted_scopes,
                now,
                session_kind=session_kind,
            )
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
        *,
        session_kind: str = "web",
    ) -> IssuedSession:
        session_id = str(uuid.uuid4())
        secret = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        sliding = now + self.settings.auth_session_sliding_days * 86400
        absolute = now + self.settings.auth_session_absolute_days * 86400
        db.execute(
            """INSERT INTO sessions(
                id,device_id,token_digest,csrf_digest,created_at,last_seen_at,
                sliding_expires_at,absolute_expires_at,kind
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                session_id,
                device_id,
                self._digest("session", secret),
                self._digest("csrf", csrf),
                now,
                now,
                min(sliding, absolute),
                absolute,
                session_kind,
            ),
        )
        return IssuedSession(
            context=AuthContext(device_id, friendly_name, frozenset(scopes), session_id),
            token=f"{session_id}.{secret}",
            csrf_token=csrf,
            expires_at=absolute,
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

    def session_kind(self, session_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT kind FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        return str(row["kind"]) if row is not None else None

    def delivery_access(self, device_id: str) -> tuple[frozenset[str], frozenset[str]] | None:
        """(scopes, active session kinds) for an enabled, unrevoked device with at least one
        live session; None otherwise. Push delivery re-checks this at send time, so revoking a
        device or logging out its last session stops delivery without touching subscriptions."""
        now = int(time.time())
        with self._connect() as db:
            device = db.execute(
                "SELECT scopes FROM devices WHERE id=? AND enabled=1 AND revoked_at IS NULL",
                (device_id,),
            ).fetchone()
            if device is None:
                return None
            kinds = {
                str(row["kind"])
                for row in db.execute(
                    """SELECT kind FROM sessions WHERE device_id=? AND revoked_at IS NULL
                       AND sliding_expires_at>? AND absolute_expires_at>?""",
                    (device_id, now, now),
                )
            }
        if not kinds:
            return None
        return frozenset(json.loads(device["scopes"])), frozenset(kinds)

    def panel_layout(self, device_id: str) -> tuple[PanelLayout, int] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT layout,updated_at FROM panel_preferences WHERE device_id=?",
                (device_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return PanelLayout.model_validate_json(row["layout"]), int(row["updated_at"])
        except (ValueError, TypeError):
            logger.warning("Invalid panel layout ignored device_id=%s", device_id)
            return None

    def save_panel_layout(self, device_id: str, layout: PanelLayout) -> int:
        now = int(time.time())
        encoded = layout.model_dump_json()
        with self._connect() as db:
            db.execute(
                """INSERT INTO panel_preferences(device_id,version,layout,updated_at)
                   VALUES(?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET
                   version=excluded.version,layout=excluded.layout,updated_at=excluded.updated_at""",
                (device_id, layout.version, encoded, now),
            )
        security_audit("panel_layout_saved", device_id=device_id, result="success")
        return now

    def reset_panel_layout(self, device_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM panel_preferences WHERE device_id=?", (device_id,))
        security_audit("panel_layout_reset", device_id=device_id, result="success")

    def list_devices(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,friendly_name,created_at,last_seen_at,enabled,revoked_at,scopes FROM devices ORDER BY created_at"
            ).fetchall()
        return [dict(row) | {"scopes": json.loads(row["scopes"])} for row in rows]

    def list_clients(self, current_device_id: str | None = None) -> list[dict]:
        """Active paired RoomOS clients for the Device Center (7D.4).

        Only enabled, unrevoked devices. No scopes, token digests, session IDs or raw device
        IDs leave this method; ``last_seen_at`` advances at most every 300 s (authenticate).
        The client kind comes from the newest live session (``web`` or ``android``).
        """
        now = int(time.time())
        with self._connect() as db:
            rows = db.execute(
                """SELECT d.id,d.friendly_name,d.created_at,d.last_seen_at,
                          (SELECT s.kind FROM sessions s WHERE s.device_id=d.id AND s.revoked_at IS NULL
                             AND s.sliding_expires_at>? AND s.absolute_expires_at>?
                           ORDER BY s.created_at DESC LIMIT 1) AS kind
                   FROM devices d WHERE d.enabled=1 AND d.revoked_at IS NULL
                   ORDER BY d.last_seen_at DESC, d.created_at""",
                (now, now),
            ).fetchall()
        return [
            {
                # Keyed digest: stable for the UI, but not the device ID used by the CLI/audit.
                "ref": self._digest("client-ref", row["id"])[:16],
                "name": row["friendly_name"],
                "kind": row["kind"],
                "paired_at": row["created_at"],
                "last_seen_at": row["last_seen_at"],
                "current": row["id"] == current_device_id,
            }
            for row in rows
        ]

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
            db.execute("DELETE FROM panel_preferences WHERE device_id=?", (device_id,))
        security_audit("device_revoked", device_id=device_id, result="success")
        return True

    def change_scope(self, device_id: str, scope: str, *, add: bool) -> tuple[list[str], list[str]] | None:
        """Add or remove one scope on an active device in one transaction (7D.3 operator tool).

        Only known scopes are accepted. Sessions stay valid; open sockets see the change on the
        next delivery (4409) and clients re-read their scopes without pairing again.
        Returns (before, after), or None when the device is unknown, disabled or revoked."""
        if scope not in KNOWN_SCOPES:
            raise ValueError(f"unknown scope: {scope}")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT scopes FROM devices WHERE id=? AND enabled=1 AND revoked_at IS NULL", (device_id,)
            ).fetchone()
            if row is None:
                return None
            before = list(json.loads(row["scopes"]))
            if add:
                after = before if scope in before else before + [scope]
            else:
                after = [s for s in before if s != scope]
            if after != before:
                db.execute("UPDATE devices SET scopes=? WHERE id=? AND scopes=?", (json.dumps(after), device_id, row["scopes"]))
        security_audit(f"device_scope_{'added' if add else 'removed'} scope={scope}", device_id=device_id,
                       result="success" if after != before else "unchanged")
        return before, after

    def close(self) -> None:
        self._attempts.clear()
