import base64
import json
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hmac import HMAC

from app.fcm import FcmTargetFile, _write_private_json
from app.push import FcmChannel, PushDevice, PushMessage, PushPreferences, WebPushChannel
from app.push_providers import (
    FcmPushProvider, VapidKey, WebPushProvider, allowed_endpoint, b64url, b64url_decode,
    generate_vapid_private_key, valid_subscription_keys, webpush_encrypt,
)
from app.push_runtime import live_fcm_token_lookup

T0 = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


def message(severity="warning", transition="started"):
    return PushMessage(
        id="psh_" + "1" * 32, dedupe_key="ab" * 32, device_id="dev", channel="webpush", transition=transition,
        alert_id="resource.workstation.disk_usage", alert_started_at=T0, severity=severity,
        title="RoomOS — Uyarı", body="Workstation: C: sürücüsü %87 dolu.", public_title="RoomOS — Uyarı",
        public_body="Ayrıntılar için RoomOS'u açın.", path="/notifications?alert=resource.workstation.disk_usage",
        created_at=T0, expires_at=T0 + timedelta(hours=1),
    )


def receiver():
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return key, public, os.urandom(16)


def _hmac(key, data):
    mac = HMAC(key, hashes.SHA256())
    mac.update(data)
    return mac.finalize()


def decrypt(body: bytes, ua_private, ua_public: bytes, auth: bytes) -> bytes:
    """Independent receiver side of RFC 8291/8188 used only to verify the sender."""
    salt, keylen = body[:16], body[20]
    as_public, record = body[21:21 + keylen], body[21 + keylen:]
    secret = ua_private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    ikm = _hmac(_hmac(auth, secret), b"WebPush: info\x00" + ua_public + as_public + b"\x01")[:32]
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    plain = AESGCM(cek).decrypt(nonce, record, None)
    assert plain.endswith(b"\x02")
    return plain[:-1]


def test_rfc8291_appendix_a_vector():
    as_private = ec.derive_private_key(int.from_bytes(b64url_decode("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
    out = webpush_encrypt(
        b"When I grow up, I want to be a watermelon",
        b64url_decode("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"),
        b64url_decode("BTBZMqHH6r4Tts7J_aSIgg"), as_private=as_private, salt=b64url_decode("DGv6ra1nlYgDCS1FRnbzlw"))
    assert b64url(out) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl"
        "7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


def test_vapid_jwt_is_es256_signed_for_the_push_origin(tmp_path):
    path = tmp_path / "vapid.pem"
    public = generate_vapid_private_key(str(path))
    assert os.stat(path).st_mode & 0o777 == 0o600
    vapid = VapidKey.load(str(path))
    assert vapid.public_key == public and len(b64url_decode(public)) == 65
    header = vapid.authorization("https://fcm.googleapis.com/fcm/send/abc", "https://roomos.example.com", now=1_000_000)
    token, key = header.removeprefix("vapid t=").split(", k=")
    head, claims, signature = token.split(".")
    payload = json.loads(b64url_decode(claims))
    assert payload == {"aud": "https://fcm.googleapis.com", "exp": 1_000_000 + 12 * 3600, "sub": "https://roomos.example.com"}
    raw = b64url_decode(signature)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    verifier = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64url_decode(key))
    verifier.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    with pytest.raises(InvalidSignature):
        verifier.verify(der, f"{head}.tampered".encode(), ec.ECDSA(hashes.SHA256()))


def test_vapid_key_with_loose_permissions_is_refused(tmp_path):
    path = tmp_path / "vapid.pem"
    generate_vapid_private_key(str(path))
    os.chmod(path, 0o644)
    with pytest.raises(ValueError):
        VapidKey.load(str(path))


def webpush_device(public, auth, details=False):
    return PushDevice(device_id="dev", display_name="Tab", created_at=T0,
                      preferences=PushPreferences(enabled=True, lock_screen_details=details),
                      webpush=WebPushChannel(endpoint="https://fcm.googleapis.com/fcm/send/xyz", p256dh=b64url(public),
                                             auth=b64url(auth), endpoint_fingerprint="0" * 64,
                                             endpoint_host="fcm.googleapis.com", subscribed_at=T0))


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [(201, "accepted"), (410, "invalid_target"), (404, "invalid_target"),
                                             (429, "temporary_failure"), (503, "temporary_failure"),
                                             (400, "permanent_failure"), (403, "permanent_failure")])
async def test_webpush_status_mapping_headers_and_decryptable_payload(tmp_path, status, expected):
    generate_vapid_private_key(str(tmp_path / "v.pem"))
    ua_private, ua_public, auth = receiver()
    seen = {}

    def handler(request):
        seen["headers"], seen["body"], seen["url"] = request.headers, request.content, str(request.url)
        return httpx.Response(status)

    provider = WebPushProvider(VapidKey.load(str(tmp_path / "v.pem")), "https://roomos.example.com",
                               client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result, _code = await provider.send(message("critical"), webpush_device(ua_public, auth), 3600)
    assert result == expected
    headers = seen["headers"]
    assert headers["content-encoding"] == "aes128gcm" and headers["ttl"] == "3600" and headers["urgency"] == "high"
    assert headers["authorization"].startswith("vapid t=") and len(headers["topic"]) <= 32
    payload = json.loads(decrypt(seen["body"], ua_private, ua_public, auth))
    assert payload["body"] == "Ayrıntılar için RoomOS'u açın."  # lock-screen details off by default
    assert payload["path"] == "/notifications?alert=resource.workstation.disk_usage"
    assert b"Workstation" not in seen["body"]


@pytest.mark.asyncio
async def test_webpush_network_error_is_temporary(tmp_path):
    generate_vapid_private_key(str(tmp_path / "v.pem"))
    _key, public, auth = receiver()

    def handler(_request):
        raise httpx.ConnectError("down")

    provider = WebPushProvider(VapidKey.load(str(tmp_path / "v.pem")), "https://x",
                               client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await provider.send(message(), webpush_device(public, auth), 60) == ("temporary_failure", "network")


class TokenSource:
    def __init__(self):
        self.invalidated = 0

    async def token(self):
        return "oauth-access"

    def invalidate(self):
        self.invalidated += 1


def fcm_device(details=False):
    return PushDevice(device_id="handset", display_name="Phone", created_at=T0,
                      preferences=PushPreferences(enabled=True, lock_screen_details=details),
                      fcm=FcmChannel(target_ref="android-abc", token_fingerprint="f" * 64, registered_at=T0))


@pytest.mark.asyncio
async def test_fcm_is_data_only_high_priority_for_critical_and_reads_rotated_token_live(tmp_path):
    targets = tmp_path / "fcm-targets.json"
    _write_private_json(str(targets), FcmTargetFile(targets={"android-abc": "TOKEN-ONE"}))
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer oauth-access"
        assert str(request.url) == "https://fcm.example.test/v1/projects/p/messages:send"
        return httpx.Response(200, json={"name": "projects/p/messages/1"})

    provider = FcmPushProvider(TokenSource(), "p", live_fcm_token_lookup(str(targets)),
                               endpoint_base="https://fcm.example.test",
                               client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await provider.send(message("critical"), fcm_device(), 1200) == ("accepted", None)
    _write_private_json(str(targets), FcmTargetFile(targets={"android-abc": "TOKEN-TWO"}))  # onNewToken rotation
    assert (await provider.send(message("warning"), fcm_device(), 60))[0] == "accepted"
    first, second = (body["message"] for body in bodies)
    assert "notification" not in first and first["token"] == "TOKEN-ONE" and second["token"] == "TOKEN-TWO"
    assert first["android"] == {"priority": "HIGH", "ttl": "1200s", "collapse_key": "resource.workstation.disk_usage"}
    assert second["android"]["priority"] == "NORMAL"
    assert first["data"]["channel"] == "roomos_critical" and first["data"]["lock_screen_details"] == "0"
    assert all(isinstance(value, str) for value in first["data"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,expected", [
    (404, {"error": {"status": "NOT_FOUND", "details": [{"errorCode": "UNREGISTERED"}]}}, "invalid_target"),
    (400, {"error": {"status": "INVALID_ARGUMENT", "details": [{"errorCode": "INVALID_ARGUMENT"}]}}, "permanent_failure"),
    (503, {"error": {"status": "UNAVAILABLE"}}, "temporary_failure"),
    (429, {"error": {"status": "RESOURCE_EXHAUSTED"}}, "temporary_failure"),
    (401, {"error": {"status": "UNAUTHENTICATED"}}, "temporary_failure"),
])
async def test_fcm_error_mapping(tmp_path, status, body, expected):
    targets = tmp_path / "t.json"
    _write_private_json(str(targets), FcmTargetFile(targets={"android-abc": "TOKEN"}))
    source = TokenSource()
    provider = FcmPushProvider(source, "p", live_fcm_token_lookup(str(targets)),
                               client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body))))
    assert (await provider.send(message(), fcm_device(), 60))[0] == expected
    assert source.invalidated == (1 if status == 401 else 0)


@pytest.mark.asyncio
async def test_fcm_missing_token_is_invalid_target(tmp_path):
    provider = FcmPushProvider(TokenSource(), "p", lambda _ref: None,
                               client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    assert await provider.send(message(), fcm_device(), 60) == ("invalid_target", "token_missing")


def test_subscription_endpoint_allowlist_blocks_ssrf():
    assert allowed_endpoint("https://fcm.googleapis.com/fcm/send/abc") == "fcm.googleapis.com"
    assert allowed_endpoint("https://wns2-db5p.notify.windows.com/w/?token=x") == "wns2-db5p.notify.windows.com"
    for bad in ("http://fcm.googleapis.com/x", "https://192.0.2.20:8123/api", "https://roomos.example.com/x",
                "https://user:pw@fcm.googleapis.com/x", "https://fcm.googleapis.com.evil.test/x",  # privacy-scan:allow rejected-credentials fixture
                "https://fcm.googleapis.com:8443/x", "ftp://fcm.googleapis.com/x", "https://localhost/x"):
        assert allowed_endpoint(bad) is None, bad
    assert allowed_endpoint("https://push.test:8443/x", frozenset({"push.test"})) == "push.test"


def test_subscription_keys_must_be_a_real_p256_point_and_16_byte_secret():
    _key, public, auth = receiver()
    assert valid_subscription_keys(b64url(public), b64url(auth))
    assert not valid_subscription_keys(b64url(b"\x04" + b"\x00" * 64), b64url(auth))
    assert not valid_subscription_keys(b64url(public), b64url(b"short"))
