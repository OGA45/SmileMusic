"""管理者ダッシュボード (/admin, /api/admin/*)。

SMILEMUSIC3_ADMIN_TOKEN が設定されているときだけ server.py が登録する
(未設定なら機能ごと無効)。認証は Bearer トークンの定数時間比較のみで、
オーナー 1 名が TLS (Traefik) 越しに使う前提。

BOT 側ロジック (メトリクス収集・ギルドスナップショット・再生操作) は
smile_music3.py が AdminProvider として register_admin_provider() で注入する
(routes_ws.register_command_handler と同じパターン — 本モジュールは BOT を
import しない)。
"""
from __future__ import annotations

import base64
import hmac
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse

_ADMIN_TOKEN = os.environ.get("SMILEMUSIC3_ADMIN_TOKEN", "")

_ALLOWED_ACTIONS = {"skip", "pause", "resume", "stop", "disconnect"}

_STATIC_DIR = Path(__file__).parent / "admin_static"


async def require_admin(authorization: str = Header(default="")) -> None:
    scheme, _, cred = authorization.partition(" ")
    ok = (
        bool(_ADMIN_TOKEN)
        and scheme.lower() == "bearer"
        # bytes 同士で比較する (非 ASCII ヘッダ値でも例外にならない)
        and hmac.compare_digest(cred.encode("utf-8"), _ADMIN_TOKEN.encode("utf-8"))
    )
    if not ok:
        raise HTTPException(
            status_code=401, detail="unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


@dataclass
class AdminProvider:
    """BOT 側から注入されるコールバック束。"""
    overview: Callable[[], Awaitable[dict]]
    guilds: Callable[[], Awaitable[list[dict]]]
    action: Callable[[int, str], Awaitable[tuple[bool, str]]]
    errors: Callable[[int], list[dict]]  # 同期 (リングバッファのスナップショット)
    # お知らせ配信: 検証済み payload dict -> 結果サマリ
    broadcast: Callable[[dict], Awaitable[dict]]
    broadcast_targets: Callable[[], Awaitable[list[dict]]]
    announcements: Callable[[int], Awaitable[list[dict]]]
    # 再生履歴: (guild_id, page) -> {total, page, page_size, items}
    guild_history: Callable[[int, int], Awaitable[dict]]
    # ユーザー/サーバーのマスタと履歴の username を埋め直す (冪等)
    backfill_masters: Callable[[bool], Awaitable[dict]]


_provider: AdminProvider | None = None


def register_admin_provider(provider: AdminProvider) -> None:
    global _provider
    _provider = provider


def _require_provider() -> AdminProvider:
    if _provider is None:
        raise HTTPException(status_code=503, detail="bot not ready")
    return _provider


# トークン入力前に配る画面本体 (秘匿情報は含まない) — 認証なし
page_router = APIRouter()
# データ/操作 API — 全エンドポイント Bearer 必須
router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])


@page_router.get("/admin", include_in_schema=False)
async def admin_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "admin.html", media_type="text/html")


@router.get("/overview")
async def admin_overview() -> dict:
    return await _require_provider().overview()


@router.get("/guilds")
async def admin_guilds() -> dict:
    return {"guilds": await _require_provider().guilds()}


@router.get("/errors")
async def admin_errors(limit: int = Query(default=200)) -> dict:
    limit = max(1, min(500, limit))
    return {"errors": _require_provider().errors(limit)}


@router.post("/guilds/{guild_id}/action")
async def admin_guild_action(
    guild_id: int, payload: dict[str, Any] = Body(...),
) -> dict:
    action = payload.get("action")
    if action not in _ALLOWED_ACTIONS:
        raise HTTPException(status_code=400, detail="unknown action")
    ok, message = await _require_provider().action(guild_id, action)
    return {"ok": ok, "message": message}


@router.get("/broadcast/targets")
async def admin_broadcast_targets() -> dict:
    return {"targets": await _require_provider().broadcast_targets()}


_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}
_MAX_IMAGE_BYTES = 8 * 1024 * 1024  # Discord の添付上限 (無課金サーバー)


@router.post("/broadcast")
async def admin_broadcast(payload: dict[str, Any] = Body(...)) -> dict:
    title = payload.get("title") or ""
    body = payload.get("body") or ""
    guild_ids = payload.get("guild_ids")
    target = payload.get("target") or (
        "selected" if guild_ids is not None else "all")
    use_embed = payload.get("use_embed", True)
    color = payload.get("color")
    image = payload.get("image")

    if target not in ("all", "selected", "unconfigured"):
        raise HTTPException(status_code=400, detail="invalid target")
    if not isinstance(title, str) or len(title) > 256:
        raise HTTPException(status_code=400, detail="invalid title")
    # Embed description の上限 4096 より手前で制限
    if not isinstance(body, str) or not (1 <= len(body) <= 4000):
        raise HTTPException(status_code=400, detail="invalid body")
    if not isinstance(use_embed, bool):
        raise HTTPException(status_code=400, detail="invalid use_embed")
    # プレーンメッセージは content 2000 文字制限 (装飾ぶんの余裕を残す)
    if not use_embed and len(title) + len(body) > 1900:
        raise HTTPException(
            status_code=400, detail="body too long for plain message")
    if color is not None and (
            not isinstance(color, str) or not _COLOR_RE.match(color)):
        raise HTTPException(status_code=400, detail="invalid color")
    if target == "selected":
        if (not isinstance(guild_ids, list) or not guild_ids
                or not all(isinstance(x, str) and x.isdigit() for x in guild_ids)):
            raise HTTPException(status_code=400, detail="invalid guild_ids")
    else:
        guild_ids = None

    image_bytes = None
    image_filename = None
    if image is not None:
        if not isinstance(image, dict):
            raise HTTPException(status_code=400, detail="invalid image")
        raw_name = image.get("filename")
        data_b64 = image.get("data_base64")
        if not isinstance(raw_name, str) or not isinstance(data_b64, str):
            raise HTTPException(status_code=400, detail="invalid image")
        name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(raw_name))
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in _IMAGE_EXTS:
            raise HTTPException(status_code=400, detail="unsupported image type")
        try:
            image_bytes = base64.b64decode(data_b64, validate=True)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid image data")
        if not image_bytes or len(image_bytes) > _MAX_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail="image too large")
        image_filename = name

    return await _require_provider().broadcast({
        "title": title, "body": body, "target": target, "guild_ids": guild_ids,
        "use_embed": use_embed, "color": color,
        "image_bytes": image_bytes, "image_filename": image_filename,
    })


@router.get("/announcements")
async def admin_announcements(limit: int = Query(default=20)) -> dict:
    limit = max(1, min(100, limit))
    return {"announcements": await _require_provider().announcements(limit)}


@router.get("/guilds/{guild_id}/history")
async def admin_guild_history(
    guild_id: int, page: int = Query(default=0),
) -> dict:
    page = max(0, min(100000, page))
    return await _require_provider().guild_history(guild_id, page)


@router.post("/backfill-masters")
async def admin_backfill_masters(force: bool = Query(default=False)) -> dict:
    """ユーザー/サーバーのマスタを再構築し、履歴の username も埋める。

    各テーブルに散らばっている userid / guildid を集め、Discord から
    名前を解決してマスタに登録する (退室済みユーザーは API 経由で解決)。
    何度実行しても安全。force=true なら履歴の既存 username も上書きする。
    """
    return await _require_provider().backfill_masters(force)
