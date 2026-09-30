"""HTTP layer. Run with `python -m anonboard` or `uvicorn anonboard.app:app`."""
from __future__ import annotations

import hmac
import ipaddress
import time
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings, load_settings
from .identity import SignatureError, b64u_decode, b64u_encode, device_id_for, verify_headers
from .service import Board, BoardError, SmsProvider, Viewer

STATIC_DIR = Path(__file__).parent / "static"


class RegisterIn(BaseModel):
    public_key: str = Field(max_length=120)
    terms_version: str = Field(max_length=32)
    attestation: str | None = Field(default=None, max_length=8192)


class ThreadIn(BaseModel):
    body: str = Field(max_length=10_000)


class CommentIn(BaseModel):
    body: str = Field(max_length=10_000)
    reply_to: str | None = Field(default=None, max_length=64)


class ReportIn(BaseModel):
    target_type: Literal["thread", "comment"]
    target_id: str = Field(max_length=64)
    reason: str = Field(default="", max_length=200)


class BlockIn(BaseModel):
    alias_id: str = Field(max_length=64)


class PhoneStartIn(BaseModel):
    phone: str = Field(max_length=32)


class PhoneVerifyIn(BaseModel):
    code: str = Field(max_length=16)


class AdminContentIn(BaseModel):
    action: Literal["restore", "hide", "delete"]
    reason: str = Field(max_length=500)


class AdminBanIn(BaseModel):
    hours: float = Field(default=24 * 30, gt=0, le=24 * 3650)
    reason: str = Field(max_length=500)
    shadow: bool = False
    include_phone: bool = True


class AdminUnbanIn(BaseModel):
    reason: str = Field(max_length=500)


def _is_trusted_proxy(host: str, trusted: tuple[str, ...]) -> bool:
    for entry in trusted:
        if entry == host:
            return True
        try:
            if ipaddress.ip_address(host) in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def make_client_ip(trusted_proxies: tuple[str, ...]):
    """X-Forwarded-For is attacker-controlled unless the TCP peer is one of
    our own proxies; then the right-most address not belonging to a trusted
    proxy is the client. Otherwise the TCP peer is the client."""

    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        fwd = request.headers.get("x-forwarded-for")
        if not fwd or not _is_trusted_proxy(peer, trusted_proxies):
            return peer
        hops = [h.strip() for h in fwd.split(",") if h.strip()]
        for hop in reversed(hops):
            if not _is_trusted_proxy(hop, trusted_proxies):
                return hop
        return hops[0] if hops else peer

    return client_ip


def create_app(settings: Settings | None = None, sms: SmsProvider | None = None, board: Board | None = None) -> FastAPI:
    settings = settings or (board.s if board else load_settings())
    board = board or Board(settings, sms=sms)
    client_ip = make_client_ip(settings.trusted_proxies)
    app = FastAPI(title="anonboard", version=__version__)
    app.state.board = board

    @app.exception_handler(BoardError)
    async def _board_error(_: Request, exc: BoardError) -> JSONResponse:
        payload = {"error": exc.code, "message": exc.message, **exc.extra}
        headers = {"Retry-After": str(exc.extra["retry_after"])} if "retry_after" in exc.extra else None
        return JSONResponse(payload, status_code=exc.status, headers=headers)

    async def _identity(request: Request, required: bool) -> str | None:
        if request.headers.get("x-device-key") is None:
            if required:
                raise BoardError(401, "signature_required", "该操作需要设备签名")
            return None
        body = await request.body()
        # Sign the bytes on the wire, not Starlette's re-parsed URL.
        raw_path = request.scope.get("raw_path") or request.url.path.encode("latin-1")
        path = raw_path.split(b"?", 1)[0].decode("latin-1")
        query = request.scope.get("query_string", b"").decode("latin-1")
        if query:
            path += "?" + query
        if "#" in path or any(ord(ch) < 32 for ch in path):
            raise BoardError(400, "bad_request", "非法的请求路径")
        try:
            ident = verify_headers(request.headers, request.method, path, body, max_skew=settings.signature_max_skew)
        except SignatureError as exc:
            raise BoardError(401, "bad_signature", f"签名无效: {exc}", server_time=int(time.time())) from exc
        # Reads are idempotent, so a replayed GET gains nothing; only writes
        # consume a nonce (and thus a write transaction).
        if request.method not in ("GET", "HEAD") and not board.consume_nonce(ident.device_id, ident.nonce, ident.ts):
            raise BoardError(401, "replay", "请求重放")
        request.state.public_key = ident.public_key_raw
        return ident.device_id

    async def signed(request: Request) -> str:
        return await _identity(request, required=True)  # type: ignore[return-value]

    async def maybe_signed(request: Request) -> str | None:
        return await _identity(request, required=False)

    async def viewer(request: Request, device_id: str | None = Depends(maybe_signed)) -> Viewer:
        return Viewer(device_id, client_ip(request))

    def admin(request: Request) -> str:
        """The audited admin name comes from which token matched, never from
        a client-supplied header."""
        token = request.headers.get("x-admin-token", "").encode("latin-1", "replace")
        matched = None
        for name, secret in settings.admin_tokens.items():
            if hmac.compare_digest(token, secret.encode("utf-8")):
                matched = name
        if matched is None:
            raise BoardError(401, "admin_required", "需要管理员令牌")
        return matched

    # ------------------------------------------------------------- public
    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "version": __version__}

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/v1/config")
    async def config() -> dict:
        return {
            "profile": settings.profile,
            "requires_phone": settings.profile == "cn",
            "terms_version": settings.terms_version,
            "contact_email": settings.contact_email,
            "server_time": int(time.time()),
            "thread_body_max": settings.thread_body_max,
            "comment_body_max": settings.comment_body_max,
            "report_hide_threshold": settings.report_hide_threshold,
        }

    @app.post("/v1/devices")
    async def register(payload: RegisterIn, request: Request, device_id: str = Depends(signed)) -> dict:
        try:
            claimed = device_id_for(b64u_decode(payload.public_key))
        except ValueError as exc:
            raise BoardError(400, "bad_public_key", "public_key 无法解析") from exc
        if claimed != device_id:
            raise BoardError(400, "key_mismatch", "public_key 与签名密钥不一致")
        # Store the key that actually verified the signature, canonically encoded.
        canonical = b64u_encode(request.state.public_key)
        return board.register_device(device_id, canonical, client_ip(request), payload.terms_version, payload.attestation)

    @app.get("/v1/me")
    async def me(device_id: str = Depends(signed)) -> dict:
        return board.me(device_id)

    @app.get("/v1/threads")
    async def list_threads(v: Viewer = Depends(viewer), cursor: str | None = None, limit: int = 20) -> dict:
        return board.list_threads(v, cursor, limit)

    @app.post("/v1/threads", status_code=201)
    async def create_thread(payload: ThreadIn, request: Request, device_id: str = Depends(signed)) -> dict:
        return board.create_thread(device_id, client_ip(request), payload.body)

    @app.get("/v1/threads/{thread_id}")
    async def get_thread(thread_id: str, v: Viewer = Depends(viewer)) -> dict:
        return board.get_thread(thread_id, v)

    @app.delete("/v1/threads/{thread_id}")
    async def delete_thread(thread_id: str, request: Request, device_id: str = Depends(signed)) -> dict:
        return board.delete_own(device_id, client_ip(request), "thread", thread_id)

    @app.post("/v1/threads/{thread_id}/comments", status_code=201)
    async def create_comment(thread_id: str, payload: CommentIn, request: Request, device_id: str = Depends(signed)) -> dict:
        return board.create_comment(device_id, client_ip(request), thread_id, payload.body, payload.reply_to)

    @app.delete("/v1/threads/{thread_id}/comments/{comment_id}")
    async def delete_comment(thread_id: str, comment_id: str, request: Request, device_id: str = Depends(signed)) -> dict:
        return board.delete_own(device_id, client_ip(request), "comment", comment_id)

    @app.post("/v1/reports")
    async def report(payload: ReportIn, request: Request, device_id: str = Depends(signed)) -> dict:
        return board.report(device_id, client_ip(request), payload.target_type, payload.target_id, payload.reason)

    @app.post("/v1/threads/{thread_id}/blocks")
    async def block(thread_id: str, payload: BlockIn, request: Request, device_id: str = Depends(signed)) -> dict:
        return board.block(device_id, client_ip(request), thread_id, payload.alias_id)

    @app.delete("/v1/threads/{thread_id}/blocks/{alias_id}")
    async def unblock(thread_id: str, alias_id: str, device_id: str = Depends(signed)) -> dict:
        return board.unblock(device_id, thread_id, alias_id)

    @app.post("/v1/me/phone/start")
    async def phone_start(payload: PhoneStartIn, request: Request, device_id: str = Depends(signed)) -> dict:
        return board.phone_start(device_id, client_ip(request), payload.phone)

    @app.post("/v1/me/phone/verify")
    async def phone_verify(payload: PhoneVerifyIn, request: Request, device_id: str = Depends(signed)) -> dict:
        return board.phone_verify(device_id, client_ip(request), payload.code)

    # ------------------------------------------------------------- admin
    @app.get("/admin/queue")
    async def admin_queue(status: str = "hidden", limit: int = 50, _: str = Depends(admin)) -> dict:
        return board.admin_queue(status, limit)

    @app.post("/admin/content/{kind}/{target_id}")
    async def admin_content(kind: str, target_id: str, payload: AdminContentIn, who: str = Depends(admin)) -> dict:
        return board.admin_content(who, kind, target_id, payload.action, payload.reason)

    @app.post("/admin/devices/{device_id}/ban")
    async def admin_ban(device_id: str, payload: AdminBanIn, who: str = Depends(admin)) -> dict:
        return board.admin_ban(who, device_id, payload.hours, payload.reason, payload.shadow, payload.include_phone)

    @app.post("/admin/devices/{device_id}/unban")
    async def admin_unban(device_id: str, payload: AdminUnbanIn, who: str = Depends(admin)) -> dict:
        return board.admin_unban(who, device_id, payload.reason)

    @app.get("/admin/trace/{kind}/{target_id}")
    async def admin_trace(kind: str, target_id: str, case_ref: str = "", reason: str = "", who: str = Depends(admin)) -> dict:
        return board.admin_trace(who, kind, target_id, case_ref, reason)

    @app.get("/admin/audit")
    async def admin_audit(limit: int = 100, _: str = Depends(admin)) -> list[dict]:
        return board.admin_audit(limit)

    @app.post("/admin/maintenance/purge")
    async def admin_purge(who: str = Depends(admin)) -> dict:
        return board.purge_expired(who)

    return app


def app_factory() -> FastAPI:  # for `uvicorn anonboard.app:app_factory --factory`
    return create_app()
