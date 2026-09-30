from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from anonboard.app import create_app
from anonboard.config import Settings
from anonboard.identity import DeviceKey
from anonboard.service import DevSmsProvider

TERMS = "2026-09"


def make_settings(profile: str = "overseas", **overrides) -> Settings:
    base = dict(
        profile=profile,
        db_path=":memory:",
        alias_pepper=b"\x01" * 32,
        phone_pepper=b"\x02" * 32,
        phone_enc_key=Fernet.generate_key().decode(),
        admin_token="admin-secret",
        terms_version=TERMS,
        new_device_hours=0,  # most tests are not about new-device throttles
        trusted_proxies=("testclient",),  # TestClient's peer; lets tests fake client IPs via X-Forwarded-For
    )
    base.update(overrides)
    return Settings(**base)


class Device:
    """A simulated app install: one key pair, signs every request."""

    def __init__(self, client: TestClient, ip: str = "203.0.113.10", key: DeviceKey | None = None) -> None:
        self.client = client
        self.key = key or DeviceKey()
        self.ip = ip

    @property
    def device_id(self) -> str:
        return self.key.device_id

    def request(self, method: str, path: str, body: dict | None = None, *, signed: bool = True, ts: int | None = None, nonce: str | None = None, headers: dict | None = None):
        raw = json.dumps(body).encode() if body is not None else b""
        hdrs = {"X-Forwarded-For": self.ip, "Content-Type": "application/json"}
        if signed:
            hdrs.update(self.key.sign_headers(method, path, raw, ts=ts, nonce=nonce))
        if headers:
            hdrs.update(headers)
        return self.client.request(method, path, content=raw if body is not None else None, headers=hdrs)

    def register(self):
        r = self.request("POST", "/v1/devices", {"public_key": self.key.public_key_b64u, "terms_version": TERMS})
        assert r.status_code == 200, r.text
        return r.json()

    def post_thread(self, body: str):
        return self.request("POST", "/v1/threads", {"body": body})

    def comment(self, thread_id: str, body: str, reply_to: str | None = None):
        return self.request("POST", f"/v1/threads/{thread_id}/comments", {"body": body, "reply_to": reply_to})

    def thread(self, thread_id: str, signed: bool = True):
        return self.request("GET", f"/v1/threads/{thread_id}", signed=signed)


@pytest.fixture
def sms() -> DevSmsProvider:
    return DevSmsProvider()


@pytest.fixture
def app(sms):
    return create_app(make_settings(), sms=sms)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def board(app):
    return app.state.board


@pytest.fixture
def admin_headers():
    return {"X-Admin-Token": "admin-secret", "X-Admin-Id": "mod-1"}


def new_device(client, ip="203.0.113.10") -> Device:
    d = Device(client, ip=ip)
    d.register()
    return d
