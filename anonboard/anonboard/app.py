"""HTTP layer. Run with `python -m anonboard` or `uvicorn anonboard.app:app`."""
from __future__ import annotations

import hmac
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings, load_settings
from .identity import SignatureError, verify_headers
from .service import Board, BoardError, SmsProvider, Viewer

STATIC_DIR = Path(__file__).parent / "static"


class RegisterIn(BaseModel):
    public_key: str
    terms_version: str
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


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def create_app(settings: Settings | None = None, sms: SmsProvider | None = None, board: Board | None = None) -> FastAPI:
    settings = settings or (board.s if board else load_settings())
    board = board or Board(settings, sms=sms)
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
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        try:
            ident = verify_headers(request.headers, request.method, path, body, max_skew=settings.signature_max_skew)
        except SignatureError as exc:
            raise BoardError(401, "bad_signature", f"签名无效: {exc}") from exc
        if not board.consume_nonce(ident.nonce, ident.ts):
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
        token = request.headers.get("x-admin-token", "")
        if not settings.admin_token or not hmac.compare_digest(token, settings.admin_token):
            raise BoardError(401, "admin_required", "需要管理员令牌")
        return request.headers.get("x-admin-id", "admin")

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
            "thread_body_max": settings.thread_body_max,
            "comment_body_max": settings.comment_body_max,
            "report_hide_threshold": settings.report_hide_threshold,
        }

    @app.post("/v1/devices")
    async def register(payload: RegisterIn, request: Request, device_id: str = Depends(signed)) -> dict:
        from .identity import b64u_decode, device_id_for

        if device_id_for(b64u_decode(payload.public_key)) != device_id:
            raise BoardError(400, "key_mismatch", "public_key 与签名密钥不一致")
        return board.register_device(device_id, payload.public_key, client_ip(request), payload.terms_version, payload.attestation)

    @app.get("/v1/me")
    async def me(device_id: str = Depends(signed)) -> dict:
        return board.me(device_id)

    @app.get("/v1/threads")
    async def list_threads(v: Viewer = Depends(viewer), before: float | None = None, limit: int = 20) -> dict:
        return board.list_threads(v, before, limit)

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
