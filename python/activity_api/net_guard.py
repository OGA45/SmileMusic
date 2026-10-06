"""SSRF 対策の共通 URL バリデータ。

ユーザー入力 URL をサーバ側でフェッチする箇所 (画像プロキシ / プレイリスト
インポートの bandcamp・yt-dlp フェッチ) で、内部ネットワーク (private /
loopback / link-local / metadata IP 等) への到達を遮断する。

使い方:
    from .net_guard import validate_public_url, SsrfBlocked
    validate_public_url(url)          # NG なら SsrfBlocked を raise
    if is_public_url(url): ...        # bool 版
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class SsrfBlocked(ValueError):
    """内部到達 URL / 不正スキームを弾いたときに送出。"""


def _ip_is_blocked(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # パースできない IP は安全側に倒して遮断
    # is_global=False なら遮断 (private/loopback/link-local/reserved/multicast/
    # unspecified に加え CGNAT 100.64.0.0/10 等の特殊用途も一括で弾ける)。
    # 公開 CDN は is_global=True なので誤遮断しない。
    if not addr.is_global:
        return True
    # 念のため明示的にも弾く (将来の ipaddress 仕様差異への保険)
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def validate_public_url(url: str) -> str:
    """url が http/https かつ全解決 IP が公開アドレスであることを検証する。

    NG なら SsrfBlocked を raise。OK ならその url をそのまま返す。
    DNS 解決して得た全 IP を検査する (A/AAAA レコード両方)。
    """
    if not url or not isinstance(url, str):
        raise SsrfBlocked("empty url")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SsrfBlocked(f"scheme not allowed: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise SsrfBlocked("no host")
    # ホスト名を解決して全 IP を検査
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise SsrfBlocked(f"dns resolution failed: {e}") from e
    if not infos:
        raise SsrfBlocked("no address resolved")
    for info in infos:
        ip = info[4][0]
        if _ip_is_blocked(ip):
            raise SsrfBlocked(f"blocked internal address: {ip}")
    return url


def is_public_url(url: str) -> bool:
    try:
        validate_public_url(url)
        return True
    except SsrfBlocked:
        return False
