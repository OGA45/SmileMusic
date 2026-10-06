"""GET /api/image?url=... — 外部画像を Discord proxy 越しで配るためのリレー。

Discord Activity iframe は CSP で外部 CDN への直接 `<img>` を弾くので、
自分のオリジン経由で配信する必要がある。yt-dlp / spotify などの
サムネイル URL を BOT 側で `/api/image?url=...` に書き換えてフロントに渡す。

サムネは同じ URL を頻繁に叩くので URL → bytes の LRU キャッシュを持ち、
event loop の帯域競合とサムネ取得バーストでビジュアライザの WS 送出が
詰まらないように外部 fetch には Semaphore を掛ける。

セキュリティ:
- MOD-SEC-01: SSRF 対策。net_guard で private/loopback/link-local/metadata IP を遮断。
  リダイレクトは追従せず、3xx の Location を都度再検証して手動でたどる。
- MOD-SEC-02: ストリーミング受信で最大バイト数を超えたら中断 (メモリ枯渇 DoS 防止)。
- MOD-PERF-08: キャッシュは件数だけでなく合計バイト数でも上限管理。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, HTTPException, Response

from .net_guard import SsrfBlocked, validate_public_url

log = logging.getLogger(__name__)

router = APIRouter()

# ---- 上限値 ----
_IMAGE_CACHE_MAX = 256                 # 件数上限
_IMAGE_CACHE_MAX_BYTES = 32 * 1024 * 1024  # 合計 32MB
_IMAGE_CACHE_TTL_S = 24 * 3600         # 24 時間
_MAX_IMAGE_BYTES = 6 * 1024 * 1024     # 1 画像あたり最大 6MB
_MAX_REDIRECTS = 3

# OrderedDict: 古い順 → 新しい順 に並び、上限超えたら古いものから捨てる
_image_cache: "OrderedDict[str, tuple[float, bytes, str]]" = OrderedDict()
_image_cache_bytes = 0

# 外部 fetch の並行数を絞る (event loop が詰まらないように)
_image_fetch_sem = asyncio.Semaphore(4)

# コネクション再利用のため httpx クライアントを 1 つだけ作って使い回す。
# follow_redirects=False: リダイレクトは手動で都度 SSRF 再検証する。
_image_client: httpx.AsyncClient | None = None


def _get_image_client() -> httpx.AsyncClient:
    global _image_client
    if _image_client is None:
        _image_client = httpx.AsyncClient(
            timeout=8.0,
            follow_redirects=False,
            limits=httpx.Limits(
                max_keepalive_connections=20, max_connections=40,
            ),
        )
    return _image_client


def _cache_get(url: str) -> tuple[bytes, str] | None:
    entry = _image_cache.get(url)
    if entry is None:
        return None
    expires_at, body, ct = entry
    if time.time() > expires_at:
        _cache_drop(url)
        return None
    _image_cache.move_to_end(url)
    return body, ct


def _cache_drop(url: str) -> None:
    global _image_cache_bytes
    entry = _image_cache.pop(url, None)
    if entry is not None:
        _image_cache_bytes -= len(entry[1])


def _cache_put(url: str, body: bytes, content_type: str) -> None:
    global _image_cache_bytes
    if len(body) > _MAX_IMAGE_BYTES:
        return  # 大きすぎる画像はキャッシュしない
    _cache_drop(url)  # 既存分のバイト数を一旦差し引く
    _image_cache[url] = (time.time() + _IMAGE_CACHE_TTL_S, body, content_type)
    _image_cache_bytes += len(body)
    _image_cache.move_to_end(url)
    # 件数 or 合計バイト数のどちらかが上限超なら古いものから捨てる
    while _image_cache and (
        len(_image_cache) > _IMAGE_CACHE_MAX
        or _image_cache_bytes > _IMAGE_CACHE_MAX_BYTES
    ):
        old_url, old_entry = _image_cache.popitem(last=False)
        _image_cache_bytes -= len(old_entry[1])


async def _fetch_capped(url: str) -> tuple[bytes, str]:
    """SSRF 検証しつつリダイレクトを手動追従し、サイズ上限付きで取得する。"""
    client = _get_image_client()
    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        # 各ホップで DNS 解決 IP を検証 (内部到達を遮断)
        await asyncio.to_thread(validate_public_url, current)
        async with client.stream("GET", current) as r:
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("location")
                if not loc:
                    raise HTTPException(status_code=502, detail="redirect without location")
                current = urljoin(current, loc)
                continue
            if r.status_code != 200:
                raise HTTPException(status_code=r.status_code, detail="upstream non-200")
            content_type = r.headers.get("content-type", "")
            if not content_type.startswith("image/"):
                raise HTTPException(status_code=400, detail="not an image")
            # Content-Length があれば事前足切り
            clen = r.headers.get("content-length")
            if clen and clen.isdigit() and int(clen) > _MAX_IMAGE_BYTES:
                raise HTTPException(status_code=413, detail="image too large")
            chunks: list[bytes] = []
            total = 0
            async for chunk in r.aiter_bytes():
                total += len(chunk)
                if total > _MAX_IMAGE_BYTES:
                    raise HTTPException(status_code=413, detail="image too large")
                chunks.append(chunk)
            return b"".join(chunks), content_type
    raise HTTPException(status_code=502, detail="too many redirects")


@router.get("/api/image")
async def proxy_image(url: str) -> Response:
    # SSRF: スキーム + 解決 IP を検証 (DNS は別スレッドで)
    try:
        await asyncio.to_thread(validate_public_url, url)
    except SsrfBlocked as e:
        log.warning("image proxy blocked: %s (%s)", url, e)
        raise HTTPException(status_code=400, detail="invalid or blocked url")

    cached = _cache_get(url)
    if cached is not None:
        body, ct = cached
        return Response(
            content=body, media_type=ct,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    async with _image_fetch_sem:
        cached = _cache_get(url)
        if cached is not None:
            body, ct = cached
            return Response(
                content=body, media_type=ct,
                headers={"Cache-Control": "public, max-age=86400"},
            )
        try:
            body, content_type = await _fetch_capped(url)
        except HTTPException:
            raise
        except SsrfBlocked as e:
            log.warning("image proxy blocked on redirect: %s (%s)", url, e)
            raise HTTPException(status_code=400, detail="blocked redirect")
        except httpx.HTTPError as e:
            log.warning("image proxy fetch failed: %s (%s)", url, e)
            raise HTTPException(status_code=502, detail="upstream fetch failed")

    _cache_put(url, body, content_type)
    return Response(
        content=body,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
