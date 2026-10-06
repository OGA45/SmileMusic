"""WebSocket /ws/{guild_id} — 状態 push + 操作受信。

サーバ → クライアント: state / progress / queue / stopped
クライアント → サーバ: {type: play|pause|skip|prev|seek, ...}

認証 (MOD-SEC-03 / FE-SEC-01):
- 推奨: accept 後の最初のフレーム {"type":"auth","token":"..."} でトークンを渡す
  (URL クエリに載せるとログ/履歴/Referer に漏れるため)。
- 互換: 旧クライアント向けに ?token= クエリも当面受け付ける。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from .auth import AuthError, DiscordUser, fetch_user_and_guild_member
from .state_bus import bus

log = logging.getLogger(__name__)

router = APIRouter()

_AUTH_TIMEOUT_S = 10.0

# (guild_id, user, message) を受けて BOT に転送するコールバック。
CommandHandler = Callable[[int, DiscordUser, dict[str, Any]], Awaitable[None]]
_command_handler: CommandHandler | None = None


def register_command_handler(handler: CommandHandler) -> None:
    global _command_handler
    _command_handler = handler


# 接続イベントを受け取るコールバック (auto-join VC など)。
ConnectHandler = Callable[[int, DiscordUser], Awaitable[None]]
_connect_handler: ConnectHandler | None = None


def register_connect_handler(handler: ConnectHandler) -> None:
    global _connect_handler
    _connect_handler = handler


async def _resolve_token(websocket: WebSocket, query_token: str | None) -> str | None:
    """クエリ token があればそれ、無ければ最初のフレームから auth トークンを得る。"""
    if query_token:
        return query_token
    try:
        first = await asyncio.wait_for(
            websocket.receive_text(), timeout=_AUTH_TIMEOUT_S,
        )
    except (asyncio.TimeoutError, WebSocketDisconnect):
        return None
    try:
        msg = json.loads(first)
    except json.JSONDecodeError:
        return None
    if isinstance(msg, dict) and msg.get("type") == "auth":
        tok = msg.get("token")
        return tok if isinstance(tok, str) and tok else None
    return None


@router.websocket("/ws/{guild_id}")
async def ws_endpoint(
    websocket: WebSocket,
    guild_id: int,
    token: str | None = Query(None),
) -> None:
    # 第一フレーム認証のため先に accept する (不正なら即 close)。
    await websocket.accept()

    tok = await _resolve_token(websocket, token)
    if not tok:
        await websocket.close(code=4401, reason="missing auth token")
        return

    # MOD-PERF-07: user 取得と guild メンバー確認を 1 RTT で並列実行。
    try:
        user, is_member = await fetch_user_and_guild_member(tok, str(guild_id))
    except AuthError as e:
        await websocket.close(code=4401, reason=str(e))
        return
    if not is_member:
        await websocket.close(code=4403, reason="not a guild member")
        return

    log.info("ws accepted: user=%s guild=%s", user.username, guild_id)

    q = await bus.subscribe(guild_id, user_id=user.id)

    if _connect_handler is not None:
        try:
            await _connect_handler(guild_id, user)
        except Exception:
            log.exception("connect handler failed (guild=%s)", guild_id)

    async def sender() -> None:
        try:
            while True:
                # queue には JSON 文字列が入っている (MOD-PERF-03)
                payload = await q.get()
                await websocket.send_text(payload)
        except WebSocketDisconnect:
            pass
        except RuntimeError:
            # 送信中の切断系。予期せぬ RuntimeError は記録する (MOD-MAINT-03)。
            log.debug("ws sender stopped (guild=%s)", guild_id)
        except Exception:
            log.exception("ws sender error (guild=%s)", guild_id)

    async def receiver() -> None:
        try:
            while True:
                text = await websocket.receive_text()
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    continue
                # auth フレームはハンドシェイク済みなので無視
                if isinstance(msg, dict) and msg.get("type") == "auth":
                    continue
                if _command_handler is not None:
                    try:
                        await _command_handler(guild_id, user, msg)
                    except Exception:
                        log.exception("command handler failed (guild=%s)", guild_id)
        except WebSocketDisconnect:
            pass

    send_task = asyncio.create_task(sender())
    recv_task = asyncio.create_task(receiver())
    try:
        await asyncio.wait(
            {send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        send_task.cancel()
        recv_task.cancel()
        await bus.unsubscribe(guild_id, q)
        log.info("ws closed: user=%s guild=%s", user.username, guild_id)
