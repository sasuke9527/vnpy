"""Business logic. The HTTP layer (app.py) is a thin adapter over `Board`."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography.fernet import Fernet

from .config import Settings
from .db import Database
from .moderation import ContentFilter, normalise_body
from .pseudonym import MAX_DERIVATION_TRIES, derive_alias

VISIBLE = "visible"
HIDDEN = "hidden"      # auto-hidden by reports or by a moderator; queued for review
HELD = "held"          # keyword hold; author-only until reviewed
SHADOW = "shadow"      # author is shadow-banned; author-only
DELETED = "deleted"    # removed by author or moderator; body kept until retention purge
STATUSES = (VISIBLE, HIDDEN, HELD, SHADOW, DELETED)

OTP_TTL = 600
OTP_MAX_ATTEMPTS = 5
CN_MOBILE = re.compile(r"^(?:\+?86)?(1[3-9]\d{9})$")


class BoardError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


class SmsProvider(Protocol):
    def send(self, phone: str, code: str) -> None: ...


class DevSmsProvider:
    """Records codes instead of sending them. Never use in production."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, phone: str, code: str) -> None:
        self.sent.append((phone, code))


@dataclass(frozen=True)
class Viewer:
    device_id: str | None
    ip: str


def now() -> float:
    return time.time()


def new_id() -> str:
    return secrets.token_hex(16)


def public_ts(ts: float) -> int:
    """Timestamps are coarsened to the minute in public responses so that a
    reader cannot correlate a person's activity across threads by the second."""
    return int(ts // 60) * 60


class Board:
    def __init__(self, settings: Settings, sms: SmsProvider | None = None, content_filter: ContentFilter | None = None) -> None:
        self.s = settings
        self.db = Database(settings.db_path)
        self.sms: SmsProvider = sms or DevSmsProvider()
        if content_filter is not None:
            self.filter = content_filter
        elif settings.blocklist_path:
            self.filter = ContentFilter.from_file(settings.blocklist_path)
        else:
            self.filter = ContentFilter()
        self.fernet = Fernet(settings.phone_enc_key.encode()) if settings.phone_enc_key else None

    # ------------------------------------------------------------------ util
    def _log(self, action: str, device_id: str | None, ip: str | None, target_id: str | None = None, meta: dict | None = None) -> None:
        self.db.execute(
            "INSERT INTO events(ts, device_id, ip, action, target_id, meta) VALUES(?,?,?,?,?,?)",
            (now(), device_id, ip, action, target_id, json.dumps(meta, ensure_ascii=False) if meta else None),
        )

    def _audit(self, admin: str, action: str, target: str, case_ref: str | None, reason: str | None, result: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO audit_log(ts, admin, action, target, case_ref, reason, result) VALUES(?,?,?,?,?,?,?)",
            (now(), admin, action, target, case_ref, reason, result),
        )

    def _rate_check(self, action: str, *, device_id: str | None = None, ip: str | None = None) -> None:
        limit = self.s.rate_limits.get(action)
        if not limit:
            return
        max_events, window = limit
        since = now() - window
        base_action = action.removesuffix("_new").removesuffix("_ip")
        if action.endswith("_ip"):
            count = self.db.scalar("SELECT COUNT(*) FROM events WHERE ip=? AND action=? AND ts>?", (ip, base_action, since))
        else:
            count = self.db.scalar("SELECT COUNT(*) FROM events WHERE device_id=? AND action=? AND ts>?", (device_id, base_action, since))
        if count >= max_events:
            raise BoardError(429, "rate_limited", "操作过于频繁，请稍后再试", retry_after=window)

    def _write_rate_check(self, device_id: str, ip: str) -> None:
        count = self.db.scalar(
            "SELECT COUNT(*) FROM events WHERE ip=? AND action IN ('thread','comment','report') AND ts>?",
            (ip, now() - self.s.rate_limits["write_ip"][1]),
        )
        if count >= self.s.rate_limits["write_ip"][0]:
            raise BoardError(429, "rate_limited", "该网络下操作过于频繁", retry_after=self.s.rate_limits["write_ip"][1])

    # --------------------------------------------------------------- devices
    def register_device(self, device_id: str, public_key_b64u: str, ip: str, terms_version: str, attestation: str | None = None) -> dict:
        if terms_version != self.s.terms_version:
            raise BoardError(400, "terms_required", "请先同意最新的社区规范", terms_version=self.s.terms_version)
        existing = self.db.one("SELECT * FROM devices WHERE device_id=?", (device_id,))
        if existing is None:
            self._rate_check("register_ip", ip=ip)
            with self.db.tx() as c:
                c.execute(
                    "INSERT INTO devices(device_id, public_key, created_at, first_ip, last_seen_at, terms_version, attestation) VALUES(?,?,?,?,?,?,?)",
                    (device_id, public_key_b64u, now(), ip, now(), terms_version, attestation),
                )
            self._log("register", device_id, ip)
        else:
            self.db.execute("UPDATE devices SET last_seen_at=?, terms_version=? WHERE device_id=?", (now(), terms_version, device_id))
        return self.me(device_id)

    def me(self, device_id: str) -> dict:
        d = self.db.one("SELECT * FROM devices WHERE device_id=?", (device_id,))
        if d is None:
            raise BoardError(401, "unregistered", "设备未注册")
        bound = self.db.one("SELECT verified_at FROM identity_bindings WHERE device_id=?", (device_id,)) is not None
        ban = self._active_ban(device_id)
        return {
            "device_id": device_id,
            "created_at": public_ts(d["created_at"]),
            "profile": self.s.profile,
            "phone_bound": bound,
            "can_post": self.s.profile != "cn" or bound,
            "banned_until": int(ban["until"]) if ban and not ban["shadow"] else None,
        }

    def _active_ban(self, device_id: str):
        subjects = [f"device:{device_id}"]
        binding = self.db.one("SELECT phone_hash FROM identity_bindings WHERE device_id=?", (device_id,))
        if binding is not None:
            subjects.append(f"phone:{binding['phone_hash']}")
        placeholders = ",".join("?" * len(subjects))
        return self.db.one(
            f"SELECT * FROM bans WHERE subject IN ({placeholders}) AND until>? ORDER BY shadow ASC, until DESC LIMIT 1",
            (*subjects, now()),
        )

    def _writer(self, device_id: str, ip: str) -> tuple[Any, bool]:
        """Common gate for every write. Returns (device row, shadow_banned)."""
        d = self.db.one("SELECT * FROM devices WHERE device_id=?", (device_id,))
        if d is None:
            raise BoardError(401, "unregistered", "设备未注册，请先注册")
        if d["status"] != "active":
            raise BoardError(403, "device_disabled", "设备已被停用")
        ban = self._active_ban(device_id)
        if ban is not None and not ban["shadow"]:
            raise BoardError(403, "banned", "账号已被限制发言", until=int(ban["until"]), reason=ban["reason"])
        if self.s.profile == "cn" and self.db.one("SELECT 1 FROM identity_bindings WHERE device_id=?", (device_id,)) is None:
            raise BoardError(403, "phone_required", "根据法规要求，发言前需绑定手机号（仅后台保存，前台不展示）")
        self.db.execute("UPDATE devices SET last_seen_at=? WHERE device_id=?", (now(), device_id))
        return d, ban is not None

    def _is_new(self, device_row) -> bool:
        return now() - device_row["created_at"] < self.s.new_device_hours * 3600

    # --------------------------------------------------------------- aliases
    def _alias_row(self, thread_id: str, device_id: str):
        return self.db.one("SELECT * FROM thread_aliases WHERE thread_id=? AND device_id=?", (thread_id, device_id))

    def _allocate_alias(self, conn, thread_id: str, device_id: str, is_op: bool) -> dict:
        """Must be called inside a transaction. Idempotent per (thread, device)."""
        row = conn.execute("SELECT * FROM thread_aliases WHERE thread_id=? AND device_id=?", (thread_id, device_id)).fetchone()
        if row is not None:
            return dict(row)
        taken = {r[0] for r in conn.execute("SELECT name FROM thread_aliases WHERE thread_id=?", (thread_id,))}
        for counter in range(MAX_DERIVATION_TRIES + 10_000):
            name, hue = derive_alias(self.s.alias_pepper, thread_id, device_id, counter)
            if name not in taken:
                break
        else:  # pragma: no cover
            raise BoardError(500, "alias_exhausted", "无法分配昵称")
        alias_id = new_id()[:16]
        conn.execute(
            "INSERT INTO thread_aliases(alias_id, thread_id, device_id, name, hue, is_op, created_at) VALUES(?,?,?,?,?,?,?)",
            (alias_id, thread_id, device_id, name, hue, int(is_op), now()),
        )
        return {"alias_id": alias_id, "thread_id": thread_id, "device_id": device_id, "name": name, "hue": hue, "is_op": int(is_op)}

    def preview_alias(self, thread_id: str, device_id: str) -> dict:
        """What this device would be called in the thread. Does not persist
        anything, so lurking never creates a (thread, device) link."""
        row = self._alias_row(thread_id, device_id)
        if row is not None:
            return self._alias_public(row)
        taken = {r[0] for r in self.db.query("SELECT name FROM thread_aliases WHERE thread_id=?", (thread_id,))}
        for counter in range(MAX_DERIVATION_TRIES + 10_000):
            name, hue = derive_alias(self.s.alias_pepper, thread_id, device_id, counter)
            if name not in taken:
                return {"alias_id": None, "name": name, "hue": hue, "is_op": False, "provisional": True}
        raise BoardError(500, "alias_exhausted", "无法分配昵称")  # pragma: no cover

    @staticmethod
    def _alias_public(row) -> dict:
        return {"alias_id": row["alias_id"], "name": row["name"], "hue": row["hue"], "is_op": bool(row["is_op"])}

    # --------------------------------------------------------------- threads
    def _moderate(self, body: str, max_len: int, shadow: bool) -> tuple[str, str, list[str]]:
        try:
            body = normalise_body(body, max_len)
        except ValueError as exc:
            raise BoardError(400, "invalid_body", str(exc)) from exc
        verdict = self.filter.check(body)
        if verdict.decision == "reject":
            raise BoardError(422, "content_rejected", "内容疑似包含他人隐私或违规信息，无法发布", reasons=verdict.reasons)
        status = SHADOW if shadow else (HELD if verdict.decision == "hold" else VISIBLE)
        return body, status, verdict.reasons

    def create_thread(self, device_id: str, ip: str, body: str) -> dict:
        d, shadow = self._writer(device_id, ip)
        self._write_rate_check(device_id, ip)
        self._rate_check("thread_new" if self._is_new(d) else "thread", device_id=device_id)
        body, status, reasons = self._moderate(body, self.s.thread_body_max, shadow)
        thread_id = new_id()
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO threads(thread_id, device_id, body, created_at, status) VALUES(?,?,?,?,?)",
                (thread_id, device_id, body, now(), status),
            )
            self._allocate_alias(c, thread_id, device_id, is_op=True)
        self._log("thread", device_id, ip, thread_id, {"status": status, "reasons": reasons} if reasons else {"status": status})
        return self.get_thread(thread_id, Viewer(device_id, ip))

    def _visible(self, row, viewer_id: str | None) -> bool:
        st = row["status"]
        if st == VISIBLE:
            return True
        if st == DELETED:
            return False
        return viewer_id is not None and row["device_id"] == viewer_id

    def _blocked_aliases(self, viewer_id: str | None, thread_id: str) -> set[str]:
        if viewer_id is None:
            return set()
        return {r[0] for r in self.db.query("SELECT alias_id FROM blocks WHERE device_id=? AND thread_id=?", (viewer_id, thread_id))}

    def _thread_public(self, row, viewer_id: str | None) -> dict:
        op = self.db.one("SELECT * FROM thread_aliases WHERE thread_id=? AND is_op=1", (row["thread_id"],))
        return {
            "thread_id": row["thread_id"],
            "body": row["body"],
            "created_at": public_ts(row["created_at"]),
            "status": row["status"],
            "comment_count": row["comment_count"],
            "alias": self._alias_public(op) if op else None,
            "is_me": viewer_id is not None and row["device_id"] == viewer_id,
        }

    def list_threads(self, viewer: Viewer, before: float | None = None, limit: int = 20) -> dict:
        limit = max(1, min(limit, 50))
        params: list[Any] = [VISIBLE]
        sql = "SELECT * FROM threads WHERE status=?"
        if viewer.device_id:
            sql = "SELECT * FROM threads WHERE (status=? OR (device_id=? AND status IN ('held','shadow','hidden')))"
            params.append(viewer.device_id)
            sql += " AND thread_id NOT IN (SELECT thread_id FROM blocks WHERE device_id=? AND alias_id='*')"
            params.append(viewer.device_id)
        if before is not None:
            sql += " AND created_at<?"
            params.append(before)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit + 1)
        rows = self.db.query(sql, params)
        items = [self._thread_public(r, viewer.device_id) for r in rows[:limit]]
        next_cursor = rows[limit - 1]["created_at"] if len(rows) > limit else None
        return {"threads": items, "next_before": next_cursor}

    def get_thread(self, thread_id: str, viewer: Viewer) -> dict:
        t = self.db.one("SELECT * FROM threads WHERE thread_id=?", (thread_id,))
        if t is None or not self._visible(t, viewer.device_id):
            raise BoardError(404, "not_found", "帖子不存在或已删除")
        blocked = self._blocked_aliases(viewer.device_id, thread_id)
        comments = []
        for c in self.db.query("SELECT * FROM comments WHERE thread_id=? ORDER BY created_at ASC, rowid ASC", (thread_id,)):
            if not self._visible(c, viewer.device_id) or c["alias_id"] in blocked:
                continue
            a = self.db.one("SELECT * FROM thread_aliases WHERE alias_id=?", (c["alias_id"],))
            comments.append({
                "comment_id": c["comment_id"],
                "body": c["body"],
                "created_at": public_ts(c["created_at"]),
                "reply_to": c["reply_to"],
                "status": c["status"],
                "alias": self._alias_public(a),
                "is_me": viewer.device_id is not None and c["device_id"] == viewer.device_id,
            })
        out = {"thread": self._thread_public(t, viewer.device_id), "comments": comments, "my_alias": None}
        if viewer.device_id:
            out["my_alias"] = self.preview_alias(thread_id, viewer.device_id)
            out["blocked_alias_ids"] = sorted(blocked)
        return out

    def create_comment(self, device_id: str, ip: str, thread_id: str, body: str, reply_to: str | None = None) -> dict:
        d, shadow = self._writer(device_id, ip)
        t = self.db.one("SELECT * FROM threads WHERE thread_id=?", (thread_id,))
        if t is None or not self._visible(t, device_id):
            raise BoardError(404, "not_found", "帖子不存在或已删除")
        if t["status"] != VISIBLE and t["device_id"] != device_id:
            raise BoardError(404, "not_found", "帖子不存在或已删除")
        if reply_to is not None:
            parent = self.db.one("SELECT * FROM comments WHERE comment_id=? AND thread_id=?", (reply_to, thread_id))
            if parent is None or not self._visible(parent, device_id):
                raise BoardError(404, "not_found", "被回复的评论不存在")
        self._write_rate_check(device_id, ip)
        self._rate_check("comment_new" if self._is_new(d) else "comment", device_id=device_id)
        body, status, reasons = self._moderate(body, self.s.comment_body_max, shadow)
        comment_id = new_id()
        with self.db.tx() as c:
            alias = self._allocate_alias(c, thread_id, device_id, is_op=(t["device_id"] == device_id))
            c.execute(
                "INSERT INTO comments(comment_id, thread_id, device_id, alias_id, body, reply_to, created_at, status) VALUES(?,?,?,?,?,?,?,?)",
                (comment_id, thread_id, device_id, alias["alias_id"], body, reply_to, now(), status),
            )
            if status == VISIBLE:
                c.execute("UPDATE threads SET comment_count=comment_count+1 WHERE thread_id=?", (thread_id,))
        self._log("comment", device_id, ip, comment_id, {"thread_id": thread_id, "status": status, "reasons": reasons} if reasons else {"thread_id": thread_id, "status": status})
        return {
            "comment_id": comment_id,
            "thread_id": thread_id,
            "body": body,
            "status": status,
            "created_at": public_ts(now()),
            "reply_to": reply_to,
            "alias": {"alias_id": alias["alias_id"], "name": alias["name"], "hue": alias["hue"], "is_op": bool(alias["is_op"])},
            "is_me": True,
        }

    def delete_own(self, device_id: str, ip: str, kind: str, target_id: str) -> dict:
        table, key = self._table(kind)
        row = self.db.one(f"SELECT * FROM {table} WHERE {key}=?", (target_id,))
        if row is None or row["device_id"] != device_id or row["status"] == DELETED:
            raise BoardError(404, "not_found", "内容不存在")
        with self.db.tx() as c:
            c.execute(f"UPDATE {table} SET status=? WHERE {key}=?", (DELETED, target_id))
            if kind == "comment" and row["status"] == VISIBLE:
                c.execute("UPDATE threads SET comment_count=comment_count-1 WHERE thread_id=?", (row["thread_id"],))
        self._log("delete", device_id, ip, target_id, {"kind": kind})
        return {"ok": True}

    @staticmethod
    def _table(kind: str) -> tuple[str, str]:
        if kind == "thread":
            return "threads", "thread_id"
        if kind == "comment":
            return "comments", "comment_id"
        raise BoardError(400, "bad_target", "target_type 必须是 thread 或 comment")

    # --------------------------------------------------------- report/block
    def report(self, device_id: str, ip: str, kind: str, target_id: str, reason: str) -> dict:
        self._writer(device_id, ip)
        table, key = self._table(kind)
        row = self.db.one(f"SELECT * FROM {table} WHERE {key}=?", (target_id,))
        if row is None or row["status"] == DELETED:
            raise BoardError(404, "not_found", "内容不存在")
        if row["device_id"] == device_id:
            raise BoardError(400, "self_report", "不能举报自己的内容")
        reason = reason.strip()[:200] or "unspecified"
        self._write_rate_check(device_id, ip)
        self._rate_check("report", device_id=device_id)
        auto_hidden = False
        with self.db.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO reports(target_type, target_id, device_id, reason, created_at) VALUES(?,?,?,?,?)",
                (kind, target_id, device_id, reason, now()),
            )
            count = c.execute("SELECT COUNT(*) FROM reports WHERE target_type=? AND target_id=?", (kind, target_id)).fetchone()[0]
            c.execute(f"UPDATE {table} SET report_count=? WHERE {key}=?", (count, target_id))
            if count >= self.s.report_hide_threshold and row["status"] == VISIBLE:
                c.execute(f"UPDATE {table} SET status=? WHERE {key}=?", (HIDDEN, target_id))
                if kind == "comment":
                    c.execute("UPDATE threads SET comment_count=comment_count-1 WHERE thread_id=?", (row["thread_id"],))
                auto_hidden = True
        self._log("report", device_id, ip, target_id, {"kind": kind, "reason": reason})
        return {"ok": True, "report_count": count, "auto_hidden": auto_hidden}

    def block(self, device_id: str, ip: str, thread_id: str, alias_id: str) -> dict:
        """Block is scoped to one thread on purpose: a cross-thread block
        would make the blocked person's posts vanish from other threads too,
        which lets the blocker link that person's aliases across threads."""
        self._writer(device_id, ip)
        if alias_id != "*":
            a = self.db.one("SELECT * FROM thread_aliases WHERE alias_id=? AND thread_id=?", (alias_id, thread_id))
            if a is None:
                raise BoardError(404, "not_found", "昵称不存在")
            if a["device_id"] == device_id:
                raise BoardError(400, "self_block", "不能屏蔽自己")
        elif self.db.one("SELECT 1 FROM threads WHERE thread_id=?", (thread_id,)) is None:
            raise BoardError(404, "not_found", "帖子不存在")
        self._rate_check("block", device_id=device_id)
        self.db.execute(
            "INSERT OR IGNORE INTO blocks(device_id, thread_id, alias_id, created_at) VALUES(?,?,?,?)",
            (device_id, thread_id, alias_id, now()),
        )
        self._log("block", device_id, ip, thread_id, {"alias_id": alias_id})
        return {"ok": True}

    def unblock(self, device_id: str, thread_id: str, alias_id: str) -> dict:
        self.db.execute("DELETE FROM blocks WHERE device_id=? AND thread_id=? AND alias_id=?", (device_id, thread_id, alias_id))
        return {"ok": True}

    # --------------------------------------------------------- phone (cn)
    def _phone_hash(self, phone: str) -> str:
        return hmac.new(self.s.phone_pepper, phone.encode(), hashlib.sha256).hexdigest()

    def phone_start(self, device_id: str, ip: str, phone: str) -> dict:
        if self.s.profile != "cn":
            raise BoardError(400, "not_applicable", "当前部署不需要绑定手机号")
        if self.db.one("SELECT 1 FROM devices WHERE device_id=?", (device_id,)) is None:
            raise BoardError(401, "unregistered", "设备未注册")
        m = CN_MOBILE.match(phone.replace(" ", "").replace("-", ""))
        if not m:
            raise BoardError(400, "invalid_phone", "请输入中国大陆手机号")
        e164 = "+86" + m.group(1)
        self._rate_check("otp_start", device_id=device_id)
        phone_hash = self._phone_hash(e164)
        bound = self.db.scalar("SELECT COUNT(*) FROM identity_bindings WHERE phone_hash=?", (phone_hash,))
        if bound >= self.s.max_devices_per_phone:
            raise BoardError(409, "phone_device_limit", "该手机号绑定的设备已达上限")
        code = f"{secrets.randbelow(1_000_000):06d}"
        assert self.fernet is not None
        self.db.execute(
            "INSERT OR REPLACE INTO otp_challenges(device_id, phone_hash, phone_enc, code_hash, expires_at, attempts) VALUES(?,?,?,?,?,0)",
            (device_id, phone_hash, self.fernet.encrypt(e164.encode()).decode(), self._phone_hash(code), now() + OTP_TTL),
        )
        self.sms.send(e164, code)
        self._log("otp_start", device_id, ip)
        return {"ok": True, "expires_in": OTP_TTL}

    def phone_verify(self, device_id: str, ip: str, code: str) -> dict:
        ch = self.db.one("SELECT * FROM otp_challenges WHERE device_id=?", (device_id,))
        self._rate_check("otp_verify", device_id=device_id)
        self._log("otp_verify", device_id, ip)
        if ch is None or ch["expires_at"] < now() or ch["attempts"] >= OTP_MAX_ATTEMPTS:
            raise BoardError(400, "otp_expired", "验证码已失效，请重新获取")
        if not hmac.compare_digest(ch["code_hash"], self._phone_hash(code.strip())):
            self.db.execute("UPDATE otp_challenges SET attempts=attempts+1 WHERE device_id=?", (device_id,))
            raise BoardError(400, "otp_wrong", "验证码错误")
        with self.db.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO identity_bindings(device_id, phone_hash, phone_enc, verified_at) VALUES(?,?,?,?)",
                (device_id, ch["phone_hash"], ch["phone_enc"], now()),
            )
            c.execute("DELETE FROM otp_challenges WHERE device_id=?", (device_id,))
        return self.me(device_id)

    # ------------------------------------------------------------- admin
    def admin_queue(self, status: str = HIDDEN, limit: int = 50) -> dict:
        if status not in (HIDDEN, HELD):
            raise BoardError(400, "bad_status", "status 必须是 hidden 或 held")
        threads = self.db.query("SELECT * FROM threads WHERE status=? ORDER BY created_at ASC LIMIT ?", (status, limit))
        comments = self.db.query("SELECT * FROM comments WHERE status=? ORDER BY created_at ASC LIMIT ?", (status, limit))
        return {
            "threads": [self._admin_row("thread", r) for r in threads],
            "comments": [self._admin_row("comment", r) for r in comments],
        }

    def _admin_row(self, kind: str, r) -> dict:
        key = "thread_id" if kind == "thread" else "comment_id"
        reports = self.db.query("SELECT reason, created_at FROM reports WHERE target_type=? AND target_id=?", (kind, r[key]))
        return {
            "kind": kind,
            "id": r[key],
            "thread_id": r["thread_id"],
            "body": r["body"],
            "status": r["status"],
            "created_at": int(r["created_at"]),
            "report_count": r["report_count"],
            "reports": [{"reason": x["reason"], "created_at": int(x["created_at"])} for x in reports],
        }

    def admin_content(self, admin: str, kind: str, target_id: str, action: str, reason: str) -> dict:
        table, key = self._table(kind)
        row = self.db.one(f"SELECT * FROM {table} WHERE {key}=?", (target_id,))
        if row is None:
            raise BoardError(404, "not_found", "内容不存在")
        new_status = {"restore": VISIBLE, "hide": HIDDEN, "delete": DELETED}.get(action)
        if new_status is None:
            raise BoardError(400, "bad_action", "action 必须是 restore/hide/delete")
        with self.db.tx() as c:
            c.execute(f"UPDATE {table} SET status=? WHERE {key}=?", (new_status, target_id))
            if kind == "comment":
                delta = int(new_status == VISIBLE) - int(row["status"] == VISIBLE)
                if delta:
                    c.execute("UPDATE threads SET comment_count=comment_count+? WHERE thread_id=?", (delta, row["thread_id"]))
            if action == "restore":
                c.execute("DELETE FROM reports WHERE target_type=? AND target_id=?", (kind, target_id))
                c.execute(f"UPDATE {table} SET report_count=0 WHERE {key}=?", (target_id,))
        self._audit(admin, f"content.{action}", f"{kind}:{target_id}", None, reason, new_status)
        return {"ok": True, "status": new_status}

    def admin_ban(self, admin: str, device_id: str, hours: float, reason: str, shadow: bool = False, include_phone: bool = True) -> dict:
        if self.db.one("SELECT 1 FROM devices WHERE device_id=?", (device_id,)) is None:
            raise BoardError(404, "not_found", "设备不存在")
        until = now() + hours * 3600
        subjects = [f"device:{device_id}"]
        if include_phone:
            b = self.db.one("SELECT phone_hash FROM identity_bindings WHERE device_id=?", (device_id,))
            if b is not None:
                subjects.append(f"phone:{b['phone_hash']}")
        with self.db.tx() as c:
            for subject in subjects:
                c.execute(
                    "INSERT OR REPLACE INTO bans(subject, until, reason, shadow, created_at) VALUES(?,?,?,?,?)",
                    (subject, until, reason, int(shadow), now()),
                )
        self._audit(admin, "ban.shadow" if shadow else "ban", device_id, None, reason, ",".join(subjects))
        return {"ok": True, "until": int(until), "subjects": subjects}

    def admin_unban(self, admin: str, device_id: str, reason: str) -> dict:
        subjects = [f"device:{device_id}"]
        b = self.db.one("SELECT phone_hash FROM identity_bindings WHERE device_id=?", (device_id,))
        if b is not None:
            subjects.append(f"phone:{b['phone_hash']}")
        with self.db.tx() as c:
            for subject in subjects:
                c.execute("DELETE FROM bans WHERE subject=?", (subject,))
        self._audit(admin, "unban", device_id, None, reason)
        return {"ok": True}

    def admin_trace(self, admin: str, kind: str, target_id: str, case_ref: str, reason: str) -> dict:
        """Lawful-request lookup: who posted this. Every call is audit-logged
        with a case reference; there is deliberately no bulk export."""
        if not case_ref.strip() or not reason.strip():
            raise BoardError(400, "case_ref_required", "调取必须填写案件编号和事由")
        table, key = self._table(kind)
        row = self.db.one(f"SELECT * FROM {table} WHERE {key}=?", (target_id,))
        if row is None:
            self._audit(admin, "trace", f"{kind}:{target_id}", case_ref, reason, "not_found")
            raise BoardError(404, "not_found", "内容不存在")
        device_id = row["device_id"]
        d = self.db.one("SELECT * FROM devices WHERE device_id=?", (device_id,))
        binding = self.db.one("SELECT * FROM identity_bindings WHERE device_id=?", (device_id,))
        phone = None
        if binding is not None and self.fernet is not None:
            phone = self.fernet.decrypt(binding["phone_enc"].encode()).decode()
        events = self.db.query(
            "SELECT ts, ip, action, target_id FROM events WHERE device_id=? ORDER BY ts DESC LIMIT 200", (device_id,)
        )
        self._audit(admin, "trace", f"{kind}:{target_id}", case_ref, reason, "ok")
        return {
            "content": {"kind": kind, "id": target_id, "body": row["body"], "created_at": row["created_at"], "status": row["status"]},
            "device": {
                "device_id": device_id,
                "public_key": d["public_key"] if d else None,
                "created_at": d["created_at"] if d else None,
                "first_ip": d["first_ip"] if d else None,
                "attestation": d["attestation"] if d else None,
            },
            "identity": {"phone": phone, "verified_at": binding["verified_at"] if binding else None},
            "events": [dict(e) for e in events],
        }

    def admin_audit(self, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------- retention
    def purge_expired(self, admin: str = "system") -> dict:
        cutoff = now() - self.s.retention_days * 86400
        with self.db.tx() as c:
            events = c.execute("DELETE FROM events WHERE ts<?", (cutoff,)).rowcount
            nonces = c.execute("DELETE FROM nonces WHERE ts<?", (now() - 2 * self.s.signature_max_skew,)).rowcount
            otps = c.execute("DELETE FROM otp_challenges WHERE expires_at<?", (now(),)).rowcount
            bans = c.execute("DELETE FROM bans WHERE until<?", (now(),)).rowcount
            bodies = 0
            for table, key in (("threads", "thread_id"), ("comments", "comment_id")):
                bodies += c.execute(
                    f"UPDATE {table} SET body='' WHERE status=? AND created_at<? AND body<>''", (DELETED, cutoff)
                ).rowcount
        result = {"events": events, "nonces": nonces, "otps": otps, "bans": bans, "deleted_bodies": bodies}
        self._audit(admin, "purge", "retention", None, f"retention_days={self.s.retention_days}", json.dumps(result))
        return result

    # ------------------------------------------------------------- nonces
    def consume_nonce(self, nonce: str, ts: float) -> bool:
        """True if the nonce was fresh. Nonces expire after 2*max_skew."""
        try:
            with self.db.tx() as c:
                c.execute("INSERT INTO nonces(nonce, ts) VALUES(?,?)", (nonce, ts))
        except Exception as exc:  # sqlite3.IntegrityError
            if "UNIQUE" in str(exc) or "unique" in str(exc):
                return False
            raise
        return True
