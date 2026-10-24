"""Phase 7G push providers. Neither logs tokens, endpoints or payload text."""

import base64
import json
import logging
import os
import stat
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hmac import HMAC

from .fcm import TEMPORARY_FCM_ERRORS, AccessTokenSource, FcmNotificationProvider
from .push import PushDevice, PushMessage, ProviderResult

logger = logging.getLogger(__name__)

# Push services Android Chrome / Firefox / Edge / Safari actually use. Anything else is refused
# at subscription time so a client can never make the Core POST to an arbitrary host (SSRF).
WEBPUSH_HOSTS = frozenset({
    "fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com",
})
WEBPUSH_HOST_SUFFIXES = (".notify.windows.com", ".push.services.mozilla.com")
INVALID_FCM_ERRORS = {"UNREGISTERED", "NOT_FOUND", "SENDER_ID_MISMATCH"}


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def allowed_endpoint(endpoint: str, extra_hosts: frozenset[str] = frozenset()) -> str | None:
    """Return the host when ``endpoint`` is an https URL on an allowed push service."""
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username or parts.password or len(endpoint) > 2048:
        return None
    if parts.port not in (None, 443) and host not in extra_hosts:
        return None
    if host in WEBPUSH_HOSTS or host.endswith(WEBPUSH_HOST_SUFFIXES) or host in extra_hosts:
        return host
    return None


def valid_subscription_keys(p256dh: str, auth: str) -> bool:
    try:
        point = b64url_decode(p256dh)
        secret = b64url_decode(auth)
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    except (ValueError, TypeError):
        return False
    return len(point) == 65 and len(secret) == 16


def _hmac(key: bytes, data: bytes) -> bytes:
    mac = HMAC(key, hashes.SHA256())
    mac.update(data)
    return mac.finalize()


def webpush_encrypt(
    payload: bytes, ua_public: bytes, auth_secret: bytes, *,
    as_private: ec.EllipticCurvePrivateKey | None = None, salt: bytes | None = None,
) -> bytes:
    """RFC 8291 message encryption with the RFC 8188 aes128gcm content coding (one record).

    Implemented on the locked ``cryptography`` package; verified against RFC 8291 Appendix A.
    """
    if len(payload) > 3993:  # 4096 record - 16 tag - 1 delimiter - 86 header
        raise ValueError("push payload too large")
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    as_public = as_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    ecdh_secret = as_private.exchange(ec.ECDH(), ua_key)
    # RFC 8291 §3.4: IKM = HKDF(auth_secret, ecdh_secret, "WebPush: info" 0x00 ua_public as_public, 32)
    prk_key = _hmac(auth_secret, ecdh_secret)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")[:32]
    # RFC 8188 §2.2/2.3: CEK and NONCE from PRK = HMAC(salt, IKM)
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    record = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + (4096).to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + record


def client_payload(message: PushMessage, *, details: bool) -> dict:
    """The only fields that leave RoomOS. No tokens, hosts, addresses or HA/MQTT data."""
    title, body = (message.title, message.body) if details else (message.public_title, message.public_body)
    return {
        "v": "1", "id": message.id, "severity": message.severity, "transition": message.transition,
        "title": title, "body": body, "public_title": message.public_title,
        "public_body": message.public_body, "path": message.path,
        "tag": message.alert_id or message.id,
    }


class VapidKey:
    def __init__(self, private_key: ec.EllipticCurvePrivateKey) -> None:
        if not isinstance(private_key.curve, ec.SECP256R1):
            raise ValueError("VAPID key must be P-256")
        self._key = private_key
        self.public_key = b64url(private_key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))

    @classmethod
    def load(cls, path: str) -> "VapidKey":
        info = Path(path).stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("VAPID key file permissions are unsafe")
        key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError("VAPID key must be an EC private key")
        return cls(key)

    def authorization(self, endpoint: str, subject: str, *, now: float | None = None) -> str:
        parts = urlsplit(endpoint)
        audience = f"{parts.scheme}://{parts.netloc}"
        header = b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        claims = b64url(json.dumps({"aud": audience, "exp": int((now or time.time()) + 12 * 3600),
                                    "sub": subject}, separators=(",", ":")).encode())
        signing_input = f"{header}.{claims}".encode("ascii")
        r, s = decode_dss_signature(self._key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
        token = f"{header}.{claims}.{b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
        return f"vapid t={token}, k={self.public_key}"


class WebPushProvider:
    kind = "webpush"

    def __init__(self, vapid: VapidKey, subject: str, *, client: httpx.AsyncClient | None = None,
                 timeout_seconds: float = 10.0) -> None:
        self.vapid = vapid
        self.subject = subject
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds), follow_redirects=False)

    def encrypt(self, payload: bytes, p256dh: str, auth: str) -> bytes:
        return webpush_encrypt(payload, b64url_decode(p256dh), b64url_decode(auth))

    async def send(self, message: PushMessage, device: PushDevice, ttl_seconds: int) -> tuple[ProviderResult, str | None]:
        channel = device.webpush
        if channel is None or not channel.endpoint or not channel.p256dh or not channel.auth:
            return "invalid_target", "no_subscription"
        details = device.preferences.lock_screen_details
        body = self.encrypt(json.dumps(client_payload(message, details=details), separators=(",", ":")).encode(),
                            channel.p256dh, channel.auth)
        headers = {
            "Authorization": self.vapid.authorization(channel.endpoint, self.subject),
            "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
            "TTL": str(ttl_seconds), "Urgency": "high" if message.severity == "critical" else "normal",
            # Collapse a lifecycle's notifications on the push service (≤32 url-safe chars).
            "Topic": b64url(bytes.fromhex(message.dedupe_key[:32]))[:32],
        }
        try:
            response = await self._client.post(channel.endpoint, content=body, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError):
            return "temporary_failure", "network"
        code = f"http_{response.status_code}"
        if response.status_code in (200, 201, 202):
            return "accepted", None
        if response.status_code in (404, 410):
            return "invalid_target", code
        if response.status_code == 429 or response.status_code >= 500:
            return "temporary_failure", code
        return "permanent_failure", code

    async def close(self) -> None:
        await self._client.aclose()


class FcmPushProvider:
    """Data-only FCM v1 messages; the token is resolved live per send so rotation needs no restart."""

    kind = "fcm"

    def __init__(self, token_source: AccessTokenSource, project_id: str, token_lookup: Callable[[str], str | None],
                 *, endpoint_base: str = "https://fcm.googleapis.com", client: httpx.AsyncClient | None = None,
                 timeout_seconds: float = 10.0) -> None:
        self._token_source = token_source
        self._lookup = token_lookup
        self.endpoint = f"{endpoint_base.rstrip('/')}/v1/projects/{project_id}/messages:send"
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds), follow_redirects=False)

    @staticmethod
    def payload(message: PushMessage, device: PushDevice, token: str, ttl_seconds: int,
                high_priority: bool = False) -> dict:
        data = client_payload(message, details=True)  # Android hides it on the lock screen itself
        data["lock_screen_details"] = "1" if device.preferences.lock_screen_details else "0"
        data["channel"] = {"critical": "roomos_critical", "warning": "roomos_warning"}.get(message.severity, "roomos_info")
        urgent = high_priority or (message.severity == "critical" and message.transition != "resolved")
        return {"message": {
            "token": token,
            "data": {key: str(value) for key, value in data.items()},
            "android": {"priority": "HIGH" if urgent else "NORMAL", "ttl": f"{ttl_seconds}s",
                        "collapse_key": (message.alert_id or message.id)[:60]},
        }}

    async def send(self, message: PushMessage, device: PushDevice, ttl_seconds: int,
                   *, high_priority: bool = False) -> tuple[ProviderResult, str | None]:
        """``high_priority`` is used only by the operator canary so a locked phone shows it promptly."""
        if device.fcm is None:
            return "invalid_target", "no_registration"
        token = self._lookup(device.fcm.target_ref)
        if not token:
            return "invalid_target", "token_missing"
        try:
            access = await self._token_source.token()
            response = await self._client.post(
                self.endpoint, json=self.payload(message, device, token, ttl_seconds, high_priority),
                headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json; UTF-8"})
        except (httpx.TimeoutException, httpx.NetworkError):
            return "temporary_failure", "network"
        if response.status_code == 200:
            try:
                ok = isinstance(response.json().get("name"), str)
            except (ValueError, AttributeError):
                ok = False
            return ("accepted", None) if ok else ("temporary_failure", "bad_response")
        error = FcmNotificationProvider._error_code(response) or f"http_{response.status_code}"
        if response.status_code == 401 or error == "UNAUTHENTICATED":
            self._token_source.invalidate()
            return "temporary_failure", "UNAUTHENTICATED"
        if error in INVALID_FCM_ERRORS or response.status_code == 404:
            return "invalid_target", error
        if error in TEMPORARY_FCM_ERRORS or response.status_code in {429, 500, 502, 503, 504}:
            return "temporary_failure", error
        return "permanent_failure", error

    async def close(self) -> None:
        await self._client.aclose()


def generate_vapid_private_key(path: str) -> str:
    """Operator helper: create a new P-256 VAPID key (0600) and return its public key."""
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    return VapidKey(key).public_key
