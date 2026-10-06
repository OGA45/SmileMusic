"""Discord OAuth code 交換と access_token 検証。"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

CLIENT_ID = os.environ["SMILEMUSIC3_DISCORD_CLIENT_ID"]
CLIENT_SECRET = os.environ["SMILEMUSIC3_DISCORD_CLIENT_SECRET"]
DISCORD_API = "https://discord.com/api/v10"
_HTTP_TIMEOUT = 10.0

# MOD-PERF-06: 認証ごとに AsyncClient を作らずモジュール共有で keepalive を効かせる。
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _client


@dataclass
class DiscordUser:
    id: str
    username: str
    avatar: str | None = None  # avatar hash or None


class AuthError(Exception):
    pass


async def exchange_code(code: str) -> str:
    """Discord Activity の OAuth code を access_token に交換する。"""
    r = await _get_client().post(
        f"{DISCORD_API}/oauth2/token",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if r.status_code != 200:
        # MOD-SEC-04: client_secret を含むやり取りの本文は残さずステータスのみ
        log.warning("token exchange failed: status=%s", r.status_code)
        raise AuthError(f"token exchange failed ({r.status_code})")
    return r.json()["access_token"]


async def fetch_user(access_token: str) -> DiscordUser:
    r = await _get_client().get(
        f"{DISCORD_API}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if r.status_code != 200:
        raise AuthError(f"user lookup failed ({r.status_code})")
    d = r.json()
    return DiscordUser(
        id=d["id"],
        username=d["username"],
        avatar=d.get("avatar"),
    )


async def fetch_user_and_guild_member(
    access_token: str, guild_id: str,
) -> tuple[DiscordUser, bool]:
    """MOD-PERF-07: /users/@me と /users/@me/guilds を並列取得し RTT を 1 つに。"""
    import asyncio
    client = _get_client()
    headers = {"Authorization": f"Bearer {access_token}"}
    user_r, guilds_r = await asyncio.gather(
        client.get(f"{DISCORD_API}/users/@me", headers=headers),
        client.get(f"{DISCORD_API}/users/@me/guilds", headers=headers),
    )
    if user_r.status_code != 200:
        raise AuthError(f"user lookup failed ({user_r.status_code})")
    if guilds_r.status_code != 200:
        raise AuthError(f"guilds lookup failed ({guilds_r.status_code})")
    d = user_r.json()
    user = DiscordUser(id=d["id"], username=d["username"], avatar=d.get("avatar"))
    is_member = any(g["id"] == str(guild_id) for g in guilds_r.json())
    return user, is_member


async def is_member_of_guild(access_token: str, guild_id: str) -> bool:
    r = await _get_client().get(
        f"{DISCORD_API}/users/@me/guilds",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if r.status_code != 200:
        raise AuthError(f"guilds lookup failed ({r.status_code})")
    return any(g["id"] == str(guild_id) for g in r.json())
