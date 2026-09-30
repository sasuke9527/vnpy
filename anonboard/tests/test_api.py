from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from anonboard.app import create_app
from anonboard.identity import DeviceKey
from anonboard.service import DevSmsProvider

from conftest import Device, make_settings, new_device


# ----------------------------------------------------------------- aliases
def test_same_person_same_thread_same_name_other_thread_other_name(client):
    alice, bob = new_device(client), new_device(client, ip="203.0.113.11")
    t1 = alice.post_thread("第一个帖子").json()["thread"]
    t2 = bob.post_thread("第二个帖子").json()["thread"]

    c1 = alice.comment(t1["thread_id"], "楼主补充一句").json()
    c2 = alice.comment(t1["thread_id"], "再补充一句").json()
    assert c1["alias"]["name"] == c2["alias"]["name"] == t1["alias"]["name"]
    assert c1["alias"]["is_op"] and t1["alias"]["is_op"]

    b1 = bob.comment(t1["thread_id"], "路过").json()
    b2 = bob.comment(t1["thread_id"], "又路过").json()
    assert b1["alias"]["name"] == b2["alias"]["name"]
    assert b1["alias"]["name"] != c1["alias"]["name"]
    assert not b1["alias"]["is_op"]

    a_in_t2 = alice.comment(t2["thread_id"], "换个帖子").json()
    assert a_in_t2["alias"]["name"] != c1["alias"]["name"]
    assert a_in_t2["alias"]["alias_id"] != c1["alias"]["alias_id"]
    assert not a_in_t2["alias"]["is_op"]
    assert t2["alias"]["is_op"]


def test_thread_view_marks_me_and_previews_alias(client):
    alice, bob = new_device(client), new_device(client, ip="203.0.113.11")
    tid = alice.post_thread("hello").json()["thread"]["thread_id"]
    view = bob.thread(tid).json()
    assert view["thread"]["is_me"] is False
    assert view["my_alias"]["provisional"] is True
    preview = view["my_alias"]["name"]
    posted = bob.comment(tid, "hi").json()["alias"]["name"]
    assert preview == posted
    view = bob.thread(tid).json()
    assert view["comments"][0]["is_me"] is True
    assert view["my_alias"]["name"] == posted and "provisional" not in view["my_alias"]
    anon = client.get(f"/v1/threads/{tid}").json()
    assert anon["my_alias"] is None and anon["thread"]["is_me"] is False


def test_preview_does_not_persist_link(client, board):
    alice, bob = new_device(client), new_device(client, ip="203.0.113.11")
    tid = alice.post_thread("hello").json()["thread"]["thread_id"]
    bob.thread(tid)
    assert board.db.scalar("SELECT COUNT(*) FROM thread_aliases WHERE thread_id=?", (tid,)) == 1


def test_alias_collision_gets_a_different_name(client, board):
    alice, bob = new_device(client), new_device(client, ip="203.0.113.11")
    tid = alice.post_thread("hello").json()["thread"]["thread_id"]
    op_name = board.db.scalar("SELECT name FROM thread_aliases WHERE thread_id=?", (tid,))
    from anonboard import service

    original = service.derive_alias
    # force bob's first candidate to collide with the OP's name
    calls = []

    def colliding(pepper, thread_id, device_id, counter=0):
        calls.append(counter)
        if counter == 0:
            return op_name, 1
        return original(pepper, thread_id, device_id, counter)

    service.derive_alias = colliding
    try:
        name = bob.comment(tid, "x").json()["alias"]["name"]
    finally:
        service.derive_alias = original
    assert name != op_name and calls[:2] == [0, 1]


def test_timestamps_are_coarsened(client):
    alice = new_device(client)
    t = alice.post_thread("x").json()["thread"]
    assert t["created_at"] % 60 == 0


# ----------------------------------------------------------------- auth
def test_unsigned_write_rejected(client):
    d = Device(client)
    assert d.request("POST", "/v1/threads", {"body": "x"}, signed=False).status_code == 401


def test_unregistered_device_cannot_post(client):
    d = Device(client)
    r = d.post_thread("x")
    assert r.status_code == 401 and r.json()["error"] == "unregistered"


def test_replay_rejected_for_writes_only(client):
    alice = new_device(client)
    body = {"public_key": alice.key.public_key_b64u, "terms_version": "2026-09"}
    r1 = alice.request("POST", "/v1/devices", body, nonce="fixed-nonce-1234")
    r2 = alice.request("POST", "/v1/devices", body, nonce="fixed-nonce-1234")
    assert r1.status_code == 200 and r2.status_code == 401 and r2.json()["error"] == "replay"
    # reads are idempotent and do not burn a nonce (nor a write transaction)
    assert alice.request("GET", "/v1/me", nonce="read-nonce-1234").status_code == 200
    assert alice.request("GET", "/v1/me", nonce="read-nonce-1234").status_code == 200


def test_stale_timestamp_rejected_with_server_time_hint(client):
    alice = new_device(client)
    r = alice.request("GET", "/v1/me", ts=1_000_000)
    assert r.status_code == 401 and r.json()["error"] == "bad_signature"
    assert abs(r.json()["server_time"] - __import__("time").time()) < 5
    assert "server_time" in client.get("/v1/config").json()


def test_malformed_signing_input_never_500s(client, board):
    alice = new_device(client)
    huge = alice.key.sign_headers("GET", "/v1/me")
    huge["X-Device-Ts"] = "1" + "0" * 400
    assert client.get("/v1/me", headers=huge).status_code == 401
    assert client.get("/admin/queue", headers={b"x-admin-token": b"\xff\xfe"}).status_code == 401
    d = Device(client)
    r = d.request("POST", "/v1/devices", {"public_key": "A", "terms_version": "2026-09"})
    assert r.status_code == 400 and r.json()["error"] == "bad_public_key"
    # a sloppy but decodable key string is stored in canonical form, not verbatim
    key = d.key.public_key_b64u
    sloppy = key[:10] + " " + key[10:] + "=="
    assert d.request("POST", "/v1/devices", {"public_key": sloppy, "terms_version": "2026-09"}).status_code == 200
    assert board.db.scalar("SELECT public_key FROM devices WHERE device_id=?", (d.device_id,)) == key


def test_forwarded_header_ignored_from_untrusted_peer():
    settings = make_settings(trusted_proxies=())
    settings.rate_limits["register_ip"] = (2, 3600)
    with TestClient(create_app(settings)) as client:
        new_device(client, ip="198.51.100.1")
        new_device(client, ip="198.51.100.2")
        d = Device(client, ip="198.51.100.3")
        r = d.request("POST", "/v1/devices", {"public_key": d.key.public_key_b64u, "terms_version": "2026-09"})
        assert r.status_code == 429  # all three came from the same TCP peer
        board = client.app.state.board
        assert board.db.scalar("SELECT DISTINCT first_ip FROM devices") == "testclient"


def test_admin_identity_comes_from_token_not_header(client):
    settings = make_settings(admin_tokens={"mod-2": "tok2"})
    with TestClient(create_app(settings)) as c:
        alice = new_device(c)
        tid = alice.post_thread("x").json()["thread"]["thread_id"]
        hdrs = {"X-Admin-Token": "tok2", "X-Admin-Id": "someone-else"}
        assert c.get(f"/admin/trace/thread/{tid}?case_ref=C&reason=r", headers=hdrs).status_code == 200
        assert c.get("/admin/audit", headers=hdrs).json()[0]["admin"] == "mod-2"
        c.post(f"/admin/devices/{alice.device_id}/ban", json={"hours": 1, "reason": "x"}, headers={"X-Admin-Token": "admin-secret", "X-Admin-Id": "spoof"})
        assert c.get("/admin/audit", headers=hdrs).json()[0]["admin"] == "admin"


def test_register_key_mismatch(client):
    a, b = Device(client), Device(client)
    r = a.request("POST", "/v1/devices", {"public_key": b.key.public_key_b64u, "terms_version": "2026-09"})
    assert r.status_code == 400


def test_register_requires_current_terms(client):
    d = Device(client)
    r = d.request("POST", "/v1/devices", {"public_key": d.key.public_key_b64u, "terms_version": "old"})
    assert r.status_code == 400 and r.json()["error"] == "terms_required"


# ----------------------------------------------------------------- moderation
def test_pii_is_rejected(client):
    alice = new_device(client)
    r = alice.post_thread("他的手机是 13812345678，大家去骚扰他")
    assert r.status_code == 422 and "pii:mobile_number" in r.json()["reasons"]
    r = alice.post_thread("身份证 11010119900307123X")
    assert r.status_code == 422


def test_hold_terms_are_author_only_until_review(client, admin_headers):
    alice, bob = new_device(client), new_device(client, ip="203.0.113.11")
    r = alice.post_thread("公司内部消息：下周裁员")
    assert r.status_code == 201 and r.json()["thread"]["status"] == "held"
    tid = r.json()["thread"]["thread_id"]
    assert bob.thread(tid).status_code == 404
    assert client.get("/v1/threads").json()["threads"] == []
    assert alice.request("GET", "/v1/threads").json()["threads"][0]["thread_id"] == tid

    q = client.get("/admin/queue?status=held", headers=admin_headers).json()
    assert q["threads"][0]["id"] == tid
    r = client.post(f"/admin/content/thread/{tid}", json={"action": "restore", "reason": "ok"}, headers=admin_headers)
    assert r.status_code == 200
    assert bob.thread(tid).status_code == 200


def test_empty_and_oversized_bodies(client):
    alice = new_device(client)
    assert alice.post_thread("   ").status_code == 400
    assert alice.post_thread("x" * 1001).status_code == 400


# ----------------------------------------------------------------- reports
def test_three_reports_auto_hide_and_admin_can_restore(client, admin_headers):
    alice = new_device(client)
    tid = alice.post_thread("争议内容").json()["thread"]["thread_id"]
    reporters = [new_device(client, ip=f"203.0.113.{i}") for i in range(20, 23)]
    for i, r in enumerate(reporters):
        first = r.request("POST", "/v1/reports", {"target_type": "thread", "target_id": tid, "reason": "骚扰"})
        # duplicate report from the same device must not count twice
        again = r.request("POST", "/v1/reports", {"target_type": "thread", "target_id": tid, "reason": "骚扰"})
        assert first.status_code == 200 and again.status_code == (200 if i < 2 else 404)
        assert "report_count" not in first.json()  # never disclose counts to users
        assert first.json()["auto_hidden"] is (i == 2)
        if i < 2:
            assert again.json()["auto_hidden"] is False
    assert client.get(f"/v1/threads/{tid}").status_code == 404
    assert alice.thread(tid).json()["thread"]["status"] == "hidden"

    # hidden content is invisible to reporters too: no status oracle
    assert reporters[0].request("POST", "/v1/reports", {"target_type": "thread", "target_id": tid}).status_code == 404
    assert alice.request("POST", "/v1/reports", {"target_type": "thread", "target_id": tid}).status_code == 400

    q = client.get("/admin/queue", headers=admin_headers).json()
    assert q["threads"][0]["id"] == tid and len(q["threads"][0]["reports"]) == 3
    client.post(f"/admin/content/thread/{tid}", json={"action": "restore", "reason": "no violation"}, headers=admin_headers)
    assert client.get(f"/v1/threads/{tid}").status_code == 200
    audit = client.get("/admin/audit", headers=admin_headers).json()
    assert audit[0]["action"] == "content.restore" and audit[0]["admin"] == "admin"  # from the token, not X-Admin-Id


def test_report_votes_need_distinct_ips_and_established_devices(client):
    alice = new_device(client)
    tid = alice.post_thread("target").json()["thread"]["thread_id"]
    # three devices behind one IP: recorded for the queue, but no auto-hide
    for _ in range(3):
        r = new_device(client, ip="198.51.100.7").request("POST", "/v1/reports", {"target_type": "thread", "target_id": tid})
        assert r.status_code == 200 and r.json()["auto_hidden"] is False
    assert client.get(f"/v1/threads/{tid}").status_code == 200
    # two more from distinct IPs -> 5 devices, 3 IPs -> hidden
    new_device(client, ip="198.51.100.8").request("POST", "/v1/reports", {"target_type": "thread", "target_id": tid})
    r = new_device(client, ip="198.51.100.9").request("POST", "/v1/reports", {"target_type": "thread", "target_id": tid})
    assert r.json()["auto_hidden"] is True

    # brand-new devices never count toward auto-hide
    settings = make_settings(new_device_hours=24)
    with TestClient(create_app(settings)) as c:
        author = new_device(c)
        tid = author.post_thread("target").json()["thread"]["thread_id"]
        for i in range(3):
            r = new_device(c, ip=f"198.51.100.{20 + i}").request("POST", "/v1/reports", {"target_type": "thread", "target_id": tid})
            assert r.status_code == 200 and r.json()["auto_hidden"] is False
        assert c.get(f"/v1/threads/{tid}").status_code == 200
        q = c.get("/admin/queue", headers={"X-Admin-Token": "admin-secret"}).json()
        assert q["threads"] == []  # still visible, so not in the hidden queue
        assert c.app.state.board.db.scalar("SELECT report_count FROM threads WHERE thread_id=?", (tid,)) == 3


def test_report_and_block_do_not_reveal_invisible_content(client):
    alice, bob = new_device(client), new_device(client, ip="203.0.113.11")
    held = alice.post_thread("公司内部消息：下周裁员").json()["thread"]["thread_id"]
    gone = alice.post_thread("bye").json()["thread"]["thread_id"]
    alice.request("DELETE", f"/v1/threads/{gone}")
    for tid in (held, gone, "0" * 32):
        assert bob.request("POST", "/v1/reports", {"target_type": "thread", "target_id": tid}).status_code == 404
        assert bob.request("POST", f"/v1/threads/{tid}/blocks", {"alias_id": "*"}).status_code == 404


def test_comment_reports_adjust_counts(client):
    alice = new_device(client)
    tid = alice.post_thread("t").json()["thread"]["thread_id"]
    bob = new_device(client, ip="203.0.113.11")
    cid = bob.comment(tid, "c").json()["comment_id"]
    assert client.get(f"/v1/threads/{tid}").json()["thread"]["comment_count"] == 1
    for i in range(3):
        new_device(client, ip=f"203.0.113.{30 + i}").request("POST", "/v1/reports", {"target_type": "comment", "target_id": cid})
    view = client.get(f"/v1/threads/{tid}").json()
    assert view["thread"]["comment_count"] == 0 and view["comments"] == []
    assert bob.thread(tid).json()["comments"][0]["status"] == "hidden"


# ----------------------------------------------------------------- blocks
def test_block_is_thread_scoped(client):
    alice, bob, carol = new_device(client), new_device(client, ip="203.0.113.11"), new_device(client, ip="203.0.113.12")
    t1 = alice.post_thread("t1").json()["thread"]["thread_id"]
    t2 = alice.post_thread("t2").json()["thread"]["thread_id"]
    b1 = bob.comment(t1, "bob in t1").json()
    b2 = bob.comment(t2, "bob in t2").json()
    r = carol.request("POST", f"/v1/threads/{t1}/blocks", {"alias_id": b1["alias"]["alias_id"]})
    assert r.status_code == 200
    assert carol.thread(t1).json()["comments"] == []
    assert carol.thread(t1).json()["blocked_alias_ids"] == [b1["alias"]["alias_id"]]
    # the same person is still visible in the other thread: no cross-thread linkage
    assert [c["body"] for c in carol.thread(t2).json()["comments"]] == ["bob in t2"]
    assert len(client.get(f"/v1/threads/{t1}").json()["comments"]) == 1
    # alias ids are thread-bound: blocking t1's alias in t2 fails
    assert carol.request("POST", f"/v1/threads/{t2}/blocks", {"alias_id": b1["alias"]["alias_id"]}).status_code == 404
    carol.request("DELETE", f"/v1/threads/{t1}/blocks/{b1['alias']['alias_id']}")
    assert len(carol.thread(t1).json()["comments"]) == 1


def test_hide_whole_thread_from_feed(client):
    alice, bob = new_device(client), new_device(client, ip="203.0.113.11")
    tid = alice.post_thread("noise").json()["thread"]["thread_id"]
    bob.request("POST", f"/v1/threads/{tid}/blocks", {"alias_id": "*"})
    assert bob.request("GET", "/v1/threads").json()["threads"] == []
    assert len(client.get("/v1/threads").json()["threads"]) == 1


def test_cannot_block_self(client):
    alice = new_device(client)
    t = alice.post_thread("x").json()["thread"]
    assert alice.request("POST", f"/v1/threads/{t['thread_id']}/blocks", {"alias_id": t["alias"]["alias_id"]}).status_code == 400


# ----------------------------------------------------------------- deletion
def test_author_can_delete_own_content_only(client):
    alice, bob = new_device(client), new_device(client, ip="203.0.113.11")
    tid = alice.post_thread("x").json()["thread"]["thread_id"]
    cid = bob.comment(tid, "c").json()["comment_id"]
    assert alice.request("DELETE", f"/v1/threads/{tid}/comments/{cid}").status_code == 404
    assert bob.request("DELETE", f"/v1/threads/{tid}/comments/{cid}").status_code == 200
    assert client.get(f"/v1/threads/{tid}").json()["comment_count" if False else "thread"]["comment_count"] == 0
    assert bob.request("DELETE", f"/v1/threads/{tid}").status_code == 404
    assert alice.request("DELETE", f"/v1/threads/{tid}").status_code == 200
    assert client.get(f"/v1/threads/{tid}").status_code == 404
    assert alice.thread(tid).status_code == 404


# ----------------------------------------------------------------- rate limits
def test_new_device_thread_throttle():
    settings = make_settings(new_device_hours=24)
    with TestClient(create_app(settings)) as client:
        d = new_device(client)
        assert d.post_thread("one").status_code == 201
        r = d.post_thread("two")
        assert r.status_code == 429 and r.headers["retry-after"] == "3600"


def test_ip_registration_throttle():
    settings = make_settings()
    settings.rate_limits["register_ip"] = (2, 3600)
    with TestClient(create_app(settings)) as client:
        new_device(client, ip="198.51.100.1")
        new_device(client, ip="198.51.100.1")
        d = Device(client, ip="198.51.100.1")
        r = d.request("POST", "/v1/devices", {"public_key": d.key.public_key_b64u, "terms_version": "2026-09"})
        assert r.status_code == 429
        assert Device(client, ip="198.51.100.2").register()["device_id"]


# ----------------------------------------------------------------- feed
def test_feed_pagination(client):
    alice = new_device(client)
    alice.client.app.state.board.s.rate_limits["thread"] = (100, 3600)
    ids = [alice.post_thread(f"t{i}").json()["thread"]["thread_id"] for i in range(5)]
    page = client.get("/v1/threads?limit=2").json()
    assert [t["thread_id"] for t in page["threads"]] == ids[4:2:-1]
    cursor = page["next_cursor"]
    assert cursor and "." not in cursor and str(int(alice.client.app.state.board.db.scalar("SELECT created_at FROM threads LIMIT 1"))) not in cursor
    page2 = client.get(f"/v1/threads?limit=2&cursor={cursor}").json()
    assert [t["thread_id"] for t in page2["threads"]] == ids[2:0:-1]
    page3 = client.get(f"/v1/threads?limit=2&cursor={page2['next_cursor']}").json()
    assert [t["thread_id"] for t in page3["threads"]] == ids[0:1] and page3["next_cursor"] is None
    assert client.get("/v1/threads?cursor=not-a-cursor").status_code == 400


def test_reply_to_must_exist_in_thread(client):
    alice = new_device(client)
    t1 = alice.post_thread("a").json()["thread"]["thread_id"]
    t2 = alice.post_thread("b").json()["thread"]["thread_id"]
    cid = alice.comment(t1, "c").json()["comment_id"]
    assert alice.comment(t2, "reply", reply_to=cid).status_code == 404
    assert alice.comment(t1, "reply", reply_to=cid).status_code == 201


# ----------------------------------------------------------------- admin
def test_admin_requires_token(client):
    assert client.get("/admin/queue").status_code == 401
    assert client.get("/admin/queue", headers={"X-Admin-Token": "wrong"}).status_code == 401


def test_admin_trace_requires_case_ref_and_is_audited(client, admin_headers):
    alice = new_device(client, ip="203.0.113.77")
    tid = alice.post_thread("x").json()["thread"]["thread_id"]
    assert client.get(f"/admin/trace/thread/{tid}", headers=admin_headers).status_code == 400
    r = client.get(f"/admin/trace/thread/{tid}?case_ref=CASE-1&reason=court+order", headers=admin_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["device"]["device_id"] == alice.device_id
    assert data["device"]["first_ip"] == "203.0.113.77"
    assert data["identity"]["phone"] is None
    assert any(e["action"] == "thread" and e["ip"] == "203.0.113.77" for e in data["events"])
    audit = client.get("/admin/audit", headers=admin_headers).json()
    assert audit[0]["action"] == "trace" and audit[0]["case_ref"] == "CASE-1"


def test_admin_ban_and_shadow_ban(client, admin_headers):
    alice, bob = new_device(client), new_device(client, ip="203.0.113.11")
    r = client.post(f"/admin/devices/{alice.device_id}/ban", json={"hours": 1, "reason": "spam"}, headers=admin_headers)
    assert r.status_code == 200
    r = alice.post_thread("x")
    assert r.status_code == 403 and r.json()["error"] == "banned"
    assert alice.request("GET", "/v1/me").json()["banned_until"] is not None
    client.post(f"/admin/devices/{alice.device_id}/unban", json={"reason": "appeal"}, headers=admin_headers)
    assert alice.post_thread("x").status_code == 201

    client.post(f"/admin/devices/{bob.device_id}/ban", json={"hours": 1, "reason": "spam", "shadow": True}, headers=admin_headers)
    r = bob.post_thread("shadow")
    # the shadow-banned author sees nothing unusual
    assert r.status_code == 201 and r.json()["thread"]["status"] == "visible"
    tid = r.json()["thread"]["thread_id"]
    assert bob.thread(tid).json()["thread"]["status"] == "visible"
    assert bob.request("GET", "/v1/threads").json()["threads"][0]["status"] == "visible"
    assert bob.comment(tid, "c").json()["status"] == "visible"
    assert client.get(f"/v1/threads/{tid}").status_code == 404
    assert bob.request("GET", "/v1/me").json()["banned_until"] is None
    assert client.app.state.board.db.scalar("SELECT status FROM threads WHERE thread_id=?", (tid,)) == "shadow"


def test_purge_removes_old_events_and_deleted_bodies(client, board, admin_headers):
    alice = new_device(client)
    tid = alice.post_thread("secret body").json()["thread"]["thread_id"]
    alice.request("DELETE", f"/v1/threads/{tid}")
    old = 1_000_000.0
    board.db.execute("UPDATE events SET ts=?", (old,))
    board.db.execute("UPDATE threads SET created_at=?", (old,))
    r = client.post("/admin/maintenance/purge", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["events"] >= 2 and r.json()["deleted_bodies"] == 1
    assert board.db.scalar("SELECT body FROM threads WHERE thread_id=?", (tid,)) == ""
    assert board.db.scalar("SELECT COUNT(*) FROM events") == 0


# ----------------------------------------------------------------- cn profile
def test_cn_profile_requires_phone_binding_and_bans_follow_the_phone():
    sms = DevSmsProvider()
    settings = make_settings(profile="cn")
    with TestClient(create_app(settings, sms=sms)) as client:
        assert client.get("/v1/config").json()["requires_phone"] is True
        alice = new_device(client)
        assert alice.request("GET", "/v1/me").json()["can_post"] is False
        r = alice.post_thread("x")
        assert r.status_code == 403 and r.json()["error"] == "phone_required"

        assert alice.request("POST", "/v1/me/phone/start", {"phone": "12345"}).status_code == 400
        assert alice.request("POST", "/v1/me/phone/start", {"phone": "138 0013 8000"}).status_code == 200
        phone, code = sms.sent[-1]
        assert phone == "+8613800138000"
        assert alice.request("POST", "/v1/me/phone/verify", {"code": "000000" if code != "000000" else "111111"}).status_code == 400
        me = alice.request("POST", "/v1/me/phone/verify", {"code": code}).json()
        assert me["phone_bound"] and me["can_post"]
        tid = alice.post_thread("now allowed").json()["thread"]["thread_id"]

        # phone number is stored encrypted + hashed, never in clear
        board = client.app.state.board
        row = board.db.one("SELECT * FROM identity_bindings")
        assert "13800138000" not in row["phone_enc"] and "13800138000" not in row["phone_hash"]

        # lawful trace can recover it
        data = client.get(f"/admin/trace/thread/{tid}?case_ref=C2&reason=r", headers={"X-Admin-Token": "admin-secret"}).json()
        assert data["identity"]["phone"] == "+8613800138000"

        # a second device on the same phone inherits a ban placed on the first
        bob = new_device(client, ip="203.0.113.11")
        bob.request("POST", "/v1/me/phone/start", {"phone": "+8613800138000"})
        bob.request("POST", "/v1/me/phone/verify", {"code": sms.sent[-1][1]})
        assert bob.post_thread("bob").status_code == 201
        client.post(f"/admin/devices/{alice.device_id}/ban", json={"hours": 1, "reason": "abuse"}, headers={"X-Admin-Token": "admin-secret"})
        assert bob.post_thread("bob again").json()["error"] == "banned"

        # device limit per phone is enforced only after the code proves possession,
        # so the start call never tells a stranger whether a number is bound
        settings.max_devices_per_phone = 2
        carol = new_device(client, ip="203.0.113.12")
        assert carol.request("POST", "/v1/me/phone/start", {"phone": "13800138000"}).status_code == 200
        r = carol.request("POST", "/v1/me/phone/verify", {"code": sms.sent[-1][1]})
        assert r.status_code == 409 and r.json()["error"] == "phone_device_limit"
        # per-phone send cap: further starts return 200 but stop sending SMS
        before = len(sms.sent)
        for _ in range(8):
            carol.request("POST", "/v1/me/phone/start", {"phone": "13800138000"})
        assert len(sms.sent) - before <= 3


def test_overseas_profile_rejects_phone_flow(client):
    alice = new_device(client)
    assert alice.request("POST", "/v1/me/phone/start", {"phone": "13800138000"}).status_code == 400
