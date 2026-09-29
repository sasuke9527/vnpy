"""Device identity without accounts.

A device generates a P-256 key pair on first launch and keeps the private key
in the platform key store (iOS Keychain / Android Keystore; the web demo uses
localStorage). Every write request is signed. The server never sees the
private key; the principal id is a hash of the public key.

Canonical string signed by the client (UTF-8, '\n'-joined):

    <unix seconds>\n<nonce>\n<METHOD>\n<path?query>\n<sha256 hex of body>

Headers: X-Device-Key (base64url raw 65-byte uncompressed point),
         X-Device-Ts, X-Device-Nonce, X-Device-Sig (base64url raw r||s).

P-256 was chosen over Ed25519 because it is the only curve WebCrypto supports
everywhere and the only one Secure Enclave / App Attest / Play Integrity can
produce, so the same verification code can later accept attested keys.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

HDR_KEY = "x-device-key"
HDR_TS = "x-device-ts"
HDR_NONCE = "x-device-nonce"
HDR_SIG = "x-device-sig"

DEVICE_ID_DOMAIN = b"anonboard:device:v1:"


class SignatureError(Exception):
    pass


def b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def device_id_for(public_key_raw: bytes) -> str:
    return hashlib.sha256(DEVICE_ID_DOMAIN + public_key_raw).hexdigest()


def canonical_string(ts: int | str, nonce: str, method: str, path: str, body: bytes) -> bytes:
    return "\n".join([str(ts), nonce, method.upper(), path, hashlib.sha256(body).hexdigest()]).encode("utf-8")


def load_public_key(raw: bytes) -> ec.EllipticCurvePublicKey:
    if len(raw) != 65 or raw[0] != 0x04:
        raise SignatureError("public key must be a 65-byte uncompressed P-256 point")
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError as exc:
        raise SignatureError("invalid public key") from exc


def verify(public_key_raw: bytes, signature_raw: bytes, message: bytes) -> None:
    if len(signature_raw) != 64:
        raise SignatureError("signature must be 64 bytes (r||s)")
    r = int.from_bytes(signature_raw[:32], "big")
    s = int.from_bytes(signature_raw[32:], "big")
    key = load_public_key(public_key_raw)
    try:
        key.verify(encode_dss_signature(r, s), message, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise SignatureError("bad signature") from exc


@dataclass(frozen=True)
class SignedIdentity:
    device_id: str
    public_key_raw: bytes
    ts: int
    nonce: str


def verify_headers(headers, method: str, path: str, body: bytes, *, max_skew: int, now: float | None = None) -> SignedIdentity:
    """Validate the signing headers of one request. Replay protection (nonce
    uniqueness) is the caller's job because it needs storage."""
    try:
        key_raw = b64u_decode(headers[HDR_KEY])
        ts = int(headers[HDR_TS])
        nonce = headers[HDR_NONCE]
        sig_raw = b64u_decode(headers[HDR_SIG])
    except (KeyError, ValueError) as exc:
        raise SignatureError("missing or malformed signing headers") from exc
    if not (8 <= len(nonce) <= 64):
        raise SignatureError("nonce length")
    now = time.time() if now is None else now
    if abs(now - ts) > max_skew:
        raise SignatureError("timestamp outside allowed skew")
    verify(key_raw, sig_raw, canonical_string(ts, nonce, method, path, body))
    return SignedIdentity(device_id=device_id_for(key_raw), public_key_raw=key_raw, ts=ts, nonce=nonce)


class DeviceKey:
    """Client-side helper (tests, scripts, a future Python SDK)."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey | None = None) -> None:
        self.private_key = private_key or ec.generate_private_key(ec.SECP256R1())

    @property
    def public_key_raw(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )

    @property
    def public_key_b64u(self) -> str:
        return b64u_encode(self.public_key_raw)

    @property
    def device_id(self) -> str:
        return device_id_for(self.public_key_raw)

    def sign_headers(self, method: str, path: str, body: bytes = b"", *, ts: int | None = None, nonce: str | None = None) -> dict[str, str]:
        ts = int(time.time()) if ts is None else ts
        nonce = nonce or b64u_encode(secrets.token_bytes(16))
        der = self.private_key.sign(canonical_string(ts, nonce, method, path, body), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return {
            "X-Device-Key": self.public_key_b64u,
            "X-Device-Ts": str(ts),
            "X-Device-Nonce": nonce,
            "X-Device-Sig": b64u_encode(sig),
        }
