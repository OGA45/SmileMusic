"""公開ステータス文書 (botstatus/1)。status.oga.ninja の Worker が毎分 GET /api/status で取得する。

契約は gomatalk の pkg/health と同じ (正本: claudflare の workers/oga-status/SPEC.md)。
- 公開する文書なので、決まった状態語と補足 (note) だけを載せる。ID・URL・IP・ホスト名・
  バージョン・エラー文は一切含めない。
- 部品 (component) の状態関数は、事前に取ったサンプルを読むだけにして待たないこと。
  Discord との接続が固まっていても、文書は正しい状態を返す必要がある。
- 全体の状態が major_outage のときは HTTP 503 (Retry-After: 30)、それ以外は 200。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from fastapi import APIRouter, Request, Response

log = logging.getLogger(__name__)

SCHEMA = "botstatus/1"

OPERATIONAL = "operational"
DEGRADED = "degraded_performance"
PARTIAL = "partial_outage"
MAJOR = "major_outage"
_RANK = {OPERATIONAL: 0, DEGRADED: 1, PARTIAL: 2, MAJOR: 3}

# ステータスページが日本語に直す補足。これ以外の値は使わない (増やすときは claudflare 側と合わせる)。
NOTES = frozenset({
    "starting", "reconnecting", "no_heartbeat", "stale",
    "fallback_engine", "substitute_voice", "unavailable", "exec_failed",
})

_CACHE_TTL_S = 5.0
_PUSH_INTERVAL_S = 60.0

# (status, note) を返す。None を返した部品は文書に載せない (未設定の部品)。
StatusFn = Callable[[float], Optional[tuple]]
# 値 (int) を返す。None は不明 (null として載せる)。
MetricFn = Callable[[float], Optional[int]]


@dataclass
class _Entry:
    fn: StatusFn
    cap: str  # この部品が全体の状態をどこまで悪くできるか


_bot = "oga-music"
_started_wall = time.time()
_components: dict[str, _Entry] = {}
_metrics: dict[str, MetricFn] = {}
_cache: tuple[float, bytes, int] | None = None


def configure(bot: str, started_wall: float) -> None:
    global _bot, _started_wall, _cache
    _bot, _started_wall, _cache = bot, started_wall, None


def register_component(name: str, cap: str, fn: StatusFn) -> None:
    global _cache
    _components[name] = _Entry(fn=fn, cap=cap)
    _cache = None


def register_metric(name: str, fn: MetricFn) -> None:
    global _cache
    _metrics[name] = fn
    _cache = None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def render() -> tuple[bytes, int]:
    """(本文, HTTP ステータス) を返す。5 秒キャッシュする。"""
    global _cache
    now_mono = time.monotonic()
    if _cache is not None and 0 <= now_mono - _cache[0] < _CACHE_TTL_S:
        return _cache[1], _cache[2]

    overall = OPERATIONAL
    components: dict[str, dict] = {}
    for name in sorted(_components):
        entry = _components[name]
        try:
            res = entry.fn(now_mono)
        except Exception:
            log.exception("status component %s failed", name)
            res = (MAJOR, "stale")
        if res is None:
            continue
        status, note = res
        if status not in _RANK:
            status, note = MAJOR, "stale"
        comp = {"status": status}
        if note in NOTES:
            comp["note"] = note
        components[name] = comp
        effect = status if _RANK[status] <= _RANK[entry.cap] else entry.cap
        if _RANK[effect] > _RANK[overall]:
            overall = effect

    metrics: dict[str, int | None] = {}
    for name in sorted(_metrics):
        try:
            v = _metrics[name](now_mono)
        except Exception:
            log.exception("status metric %s failed", name)
            v = None
        metrics[name] = int(v) if isinstance(v, (int, float)) and math.isfinite(v) else None

    doc = {
        "schema": SCHEMA,
        "bot": _bot,
        "status": overall,
        "generated_at": _iso(time.time()),
        "started_at": _iso(_started_wall),
        "components": components,
        "metrics": metrics,
    }
    body = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    code = 503 if overall == MAJOR else 200
    _cache = (now_mono, body, code)
    return body, code


router = APIRouter()


@router.api_route("/api/status", methods=["GET", "HEAD"], include_in_schema=False)
async def get_status(request: Request) -> Response:
    body, code = render()
    headers = {"Cache-Control": "no-store"}
    if code == 503:
        headers["Retry-After"] = "30"
    return Response(
        content=b"" if request.method == "HEAD" else body,
        status_code=code,
        media_type="application/json; charset=utf-8",
        headers=headers,
    )


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """push の X-Status-Signature: "sha256=" + hex(HMAC-SHA256(secret, timestamp + "." + body))。"""
    m = hmac.new(secret.encode("utf-8"), digestmod=hashlib.sha256)
    m.update(timestamp.encode("ascii"))
    m.update(b".")
    m.update(body)
    return "sha256=" + m.hexdigest()


async def push_loop() -> None:
    """文書を 1 分ごとにステータスページへ POST する (フェーズ 2)。
    OGA_STATUS_PUSH_URL と OGA_STATUS_PUSH_SECRET が両方あるときだけ動く。"""
    url = os.environ.get("OGA_STATUS_PUSH_URL") or ""
    secret = os.environ.get("OGA_STATUS_PUSH_SECRET") or ""
    if not url or not secret:
        return
    import httpx

    failing = False
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            while True:
                await asyncio.sleep(_PUSH_INTERVAL_S)
                body, _ = render()
                ts = str(int(time.time()))
                try:
                    r = await client.post(url, content=body, headers={
                        "Content-Type": "application/json",
                        "User-Agent": f"{_bot}-status/1",
                        "X-Status-Timestamp": ts,
                        "X-Status-Signature": sign(secret, ts, body),
                    })
                    ok = r.status_code // 100 == 2
                    err = None if ok else f"status {r.status_code}"
                except Exception as e:
                    ok, err = False, type(e).__name__
                if not ok and not failing:
                    log.warning("status push failed: %s", err)
                elif ok and failing:
                    log.info("status push recovered")
                failing = not ok
    except asyncio.CancelledError:
        pass
