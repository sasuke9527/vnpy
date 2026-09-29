import secrets

import pytest

from anonboard.identity import DeviceKey, SignatureError, b64u_encode, verify_headers


def _lower(h: dict) -> dict:
    return {k.lower(): v for k, v in h.items()}


def test_roundtrip():
    k = DeviceKey()
    h = _lower(k.sign_headers("POST", "/v1/threads", b'{"body":"x"}'))
    ident = verify_headers(h, "POST", "/v1/threads", b'{"body":"x"}', max_skew=300)
    assert ident.device_id == k.device_id


def test_body_or_path_change_breaks_signature():
    k = DeviceKey()
    h = _lower(k.sign_headers("POST", "/v1/threads", b"a"))
    with pytest.raises(SignatureError):
        verify_headers(h, "POST", "/v1/threads", b"b", max_skew=300)
    with pytest.raises(SignatureError):
        verify_headers(h, "POST", "/v1/threads?x=1", b"a", max_skew=300)
    with pytest.raises(SignatureError):
        verify_headers(h, "DELETE", "/v1/threads", b"a", max_skew=300)


def test_skew_rejected():
    k = DeviceKey()
    h = _lower(k.sign_headers("GET", "/v1/me", ts=1_000_000))
    with pytest.raises(SignatureError):
        verify_headers(h, "GET", "/v1/me", b"", max_skew=300, now=1_000_000 + 301)
    verify_headers(h, "GET", "/v1/me", b"", max_skew=300, now=1_000_000 + 299)


def test_other_key_cannot_impersonate():
    k1, k2 = DeviceKey(), DeviceKey()
    h = _lower(k2.sign_headers("GET", "/v1/me"))
    h["x-device-key"] = k1.public_key_b64u
    with pytest.raises(SignatureError):
        verify_headers(h, "GET", "/v1/me", b"", max_skew=300)


def test_malformed_headers():
    k = DeviceKey()
    h = _lower(k.sign_headers("GET", "/v1/me"))
    with pytest.raises(SignatureError):
        verify_headers({}, "GET", "/v1/me", b"", max_skew=300)
    bad = dict(h, **{"x-device-sig": b64u_encode(secrets.token_bytes(10))})
    with pytest.raises(SignatureError):
        verify_headers(bad, "GET", "/v1/me", b"", max_skew=300)
    bad = dict(h, **{"x-device-key": b64u_encode(b"\x04" + b"\x00" * 64)})
    with pytest.raises(SignatureError):
        verify_headers(bad, "GET", "/v1/me", b"", max_skew=300)
