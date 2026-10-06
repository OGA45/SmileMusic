"""FastAPI アプリのファクトリ。

smile_music3.py から `create_app()` を呼び、`uvicorn.Server` で起動する。
discord.py の event loop と同じループで動かす。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import routes_image, routes_token, routes_ws

log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    product = os.environ.get("OGA_MUSIC_PRODUCT_NAME", "OGA_Music")
    app = FastAPI(title=f"{product} Activity API", docs_url=None, redoc_url=None)

    activity_host = os.environ.get("SMILEMUSIC3_ACTIVITY_HOST", "")
    allowed_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    if activity_host:
        allowed_origins.append(f"https://{activity_host}")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        # Discord Activity iframe は https://<app_id>.discordsays.com から読まれる
        allow_origin_regex=r"https://[a-z0-9-]+\.discordsays\.com",
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    app.include_router(routes_token.router)
    app.include_router(routes_ws.router)
    app.include_router(routes_image.router)

    # 管理者ダッシュボード (SMILEMUSIC3_ADMIN_TOKEN 設定時のみ有効)。
    # static mount より前に登録するので /admin と /api/admin/* が優先される。
    if os.environ.get("SMILEMUSIC3_ADMIN_TOKEN"):
        from . import routes_admin
        app.include_router(routes_admin.router)
        app.include_router(routes_admin.page_router)
        log.info("admin dashboard enabled at /admin")
    else:
        log.info("admin dashboard disabled (SMILEMUSIC3_ADMIN_TOKEN not set)")

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # Activity フロントの静的ビルド成果物を root にマウント。
    # /api/* と /ws/* は上で登録済みなのでそちらが優先される。
    # 既定パスは Dockerfile の COPY 先と合わせる (`/srv/activity_dist`)。
    # `./python:/opt` の bind mount に上書きされないように /opt の外。
    static_dir = Path(os.environ.get("ACTIVITY_STATIC_DIR", "/srv/activity_dist"))
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="activity")
        log.info("serving Activity static files from %s", static_dir)
    else:
        log.warning("activity static dir not found (%s); only API endpoints will respond", static_dir)

    return app
