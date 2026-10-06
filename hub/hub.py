"""OGA_Music Hub — 複数インスタンスの集約ダッシュボード。

アルファ / ベータ / リリースの 3 インスタンスに並列でリクエストし、
単一ページにまとめて返す。認証は Bearer トークンの定数時間比較のみ。
"""
from __future__ import annotations

import asyncio
import hmac
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

# ===== 管理トークン =====
_HUB_TOKEN = os.environ.get("OGA_HUB_ADMIN_TOKEN", "")

_STATIC_DIR = Path(__file__).parent


async def require_admin(authorization: str = Header(default="")) -> None:
    """Bearer トークンを定数時間比較で検証する。未設定時も 401 を返す。"""
    scheme, _, cred = authorization.partition(" ")
    ok = (
        bool(_HUB_TOKEN)
        and scheme.lower() == "bearer"
        # bytes 同士で比較する (非 ASCII ヘッダ値でも例外にならない)
        and hmac.compare_digest(cred.encode("utf-8"), _HUB_TOKEN.encode("utf-8"))
    )
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ===== インスタンスレジストリ =====
# 環境変数から構築。URL が空のエントリはスキップ。
# 例: OGA_HUB_ALPHA_URL=http://oga_music_alpha:8080
_INSTANCE_KEYS = ("alpha", "beta", "release")
_NAME_JA = {"alpha": "アルファ", "beta": "ベータ", "release": "リリース"}

_REGISTRY: list[dict[str, str]] = []
for _key in _INSTANCE_KEYS:
    _url = os.environ.get(f"OGA_HUB_{_key.upper()}_URL", "").rstrip("/")
    if not _url:
        continue  # URL 未設定のインスタンスは除外
    _REGISTRY.append({
        "key": _key,
        "name": os.environ.get(f"OGA_HUB_{_key.upper()}_NAME", _NAME_JA.get(_key, _key)),
        "url": _url,
        "token": os.environ.get(f"OGA_HUB_{_key.upper()}_TOKEN", ""),
        "public_admin_url": os.environ.get(f"OGA_HUB_{_key.upper()}_PUBLIC_ADMIN_URL", ""),
    })

# ===== 共有 HTTP クライアント (モジュール起動時に 1 つだけ生成) =====
_client = httpx.AsyncClient(timeout=5.0)

# ===== FastAPI アプリ =====
app = FastAPI(title="OGA_Music Hub", docs_url=None, redoc_url=None)


# ----- 静的ページ (認証なし; トークンゲートはクライアント側) -----

@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "hub.html", media_type="text/html")


@app.get("/admin", include_in_schema=False)
async def admin_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "hub.html", media_type="text/html")


# ----- ヘルスチェック -----

@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


# ----- メインの集約エンドポイント -----

@app.get("/api/hub/overview", dependencies=[Depends(require_admin)])
async def hub_overview() -> dict:
    """全インスタンスの overview + guilds を並列取得して集約する。"""
    results = await asyncio.gather(
        *[_fetch_instance(inst) for inst in _REGISTRY],
        return_exceptions=False,
    )
    return {"instances": list(results)}


async def _fetch_instance(inst: dict[str, str]) -> dict[str, Any]:
    """1 インスタンス分の overview と guilds を並列取得して結果を返す。"""
    key = inst["key"]
    name = inst["name"]
    public_admin_url = inst["public_admin_url"]
    url = inst["url"]
    token = inst["token"]
    headers = {"Authorization": f"Bearer {token}"}

    try:
        # overview と guilds を同時にリクエスト
        overview_resp, guilds_resp = await asyncio.gather(
            _client.get(f"{url}/api/admin/overview", headers=headers),
            _client.get(f"{url}/api/admin/guilds", headers=headers),
        )

        # overview の非 200 を処理
        if overview_resp.status_code != 200:
            return _error_result(key, name, public_admin_url, overview_resp.status_code)

        # guilds の非 200 を処理
        if guilds_resp.status_code != 200:
            return _error_result(key, name, public_admin_url, guilds_resp.status_code)

        overview_data: dict = overview_resp.json()
        guilds_data: dict = guilds_resp.json()
        guilds: list[dict] = guilds_data.get("guilds", [])

        # ギルドサマリを計算
        guild_count = len(guilds)
        voice_connected = sum(
            1 for g in guilds if (g.get("voice") or {}).get("connected", False)
        )
        playing = sum(
            1 for g in guilds
            if (g.get("playing") or {}).get("is_playing", False)
        )

        return {
            "key": key,
            "name": name,
            "public_admin_url": public_admin_url,
            "ok": True,
            "overview": overview_data,
            "guilds_summary": {
                "guild_count": guild_count,
                "voice_connected": voice_connected,
                "playing": playing,
            },
        }

    except Exception as exc:
        # ネットワークエラーや JSON パースエラーなど
        return {
            "key": key,
            "name": name,
            "public_admin_url": public_admin_url,
            "ok": False,
            "error": str(exc)[:120],
        }


def _error_result(key: str, name: str, public_admin_url: str, status: int) -> dict[str, Any]:
    """HTTP エラーコードから統一フォーマットのエラー結果を返す。"""
    if status == 404:
        msg = "404 — 対象インスタンスの ADMIN_TOKEN が未設定の可能性"
    elif status == 401:
        msg = "401 — トークン不一致"
    else:
        msg = f"HTTP {status}"
    return {
        "key": key,
        "name": name,
        "public_admin_url": public_admin_url,
        "ok": False,
        "error": msg,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
