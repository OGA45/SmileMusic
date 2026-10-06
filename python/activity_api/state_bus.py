"""Guild ごとの再生状態を pub/sub で配るバス。

WebSocket 接続側が subscribe して `asyncio.Queue` を受け取り、
BOT 側は publish_state / publish_progress / publish_queue で更新を流す。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class TrackInfo:
    title: str
    artist: str
    artwork: str
    duration_ms: int
    url: str
    source: str = ""           # "YouTube" / "SoundCloud" / "niconico" ...
    bitrate_kbps: int = 0      # 取得元のオーディオビットレート (kbps)


@dataclass
class PlaylistInfo:
    id: str
    name: str
    index: int
    loop: bool
    shuffle: bool = False
    next_index: int = -1  # 次に再生される予定の index (shuffle 込み)。-1 なら次無し
    loop_single: bool = False  # 現在曲を1曲ループ (loop よりも優先)


@dataclass
class GuildState:
    mode: str = "queue"  # "queue" | "playlist"
    guild_name: str = ""
    track: TrackInfo | None = None
    position_ms: int = 0
    is_playing: bool = False
    loading: bool = False  # 次の曲を yt-dlp で読み込んでる最中
    upnext: list[TrackInfo] = field(default_factory=list)
    music_queue: list[TrackInfo] = field(default_factory=list)
    # music_queue は先頭 200 件だけ送るので、実際の件数を別に持つ
    music_queue_total: int = 0
    playlist: PlaylistInfo | None = None
    queue_loop: bool = False
    queue_loop_single: bool = False  # キューで現在曲を1曲ループ (queue_loop より優先)
    queue_next_index: int = -1
    normalize: bool = False  # 音量ノーマライズ (loudnorm)

    def to_state_message(self) -> dict[str, Any]:
        # MOD-PERF-04: upnext / music_queue は state には含めず、変化時のみ
        # 別メッセージ (queue_full) で送る。state は頻繁に飛ぶが軽量に保つ。
        return {
            "type": "state",
            "mode": self.mode,
            "guild_name": self.guild_name,
            "track": asdict(self.track) if self.track else None,
            "position_ms": self.position_ms,
            "is_playing": self.is_playing,
            "loading": self.loading,
            "playlist": asdict(self.playlist) if self.playlist else None,
            "queue_loop": self.queue_loop,
            "queue_loop_single": self.queue_loop_single,
            "queue_next_index": self.queue_next_index,
            "normalize": self.normalize,
        }

    def to_queue_message(self) -> dict[str, Any]:
        return {
            "type": "queue_full",
            "upnext": [asdict(t) for t in self.upnext],
            "music_queue": [asdict(t) for t in self.music_queue],
            "music_queue_total": self.music_queue_total,
        }

    def queue_signature(self) -> int:
        # asdict/dumps を避けるための軽量シグネチャ (url+title のみ走査)。
        return hash((
            tuple((t.url, t.title) for t in self.upnext),
            tuple((t.url, t.title) for t in self.music_queue),
            self.music_queue_total,
        ))


class StateBus:
    # NOTE: 本クラスは単一 asyncio event loop 上でのみ使われる前提。
    # _state は同 loop からしか触らないため _lock 外アクセスで安全。
    # _lock は _subs の列挙と変更の競合 (subscribe/unsubscribe vs broadcast) のみ守る。
    def __init__(self) -> None:
        self._state: dict[int, GuildState] = {}
        # guild_id -> { queue: user_id|None }
        self._subs: dict[int, dict[asyncio.Queue, str | None]] = {}
        self._lock = asyncio.Lock()
        # MOD-PERF-04: 直近に送ったキューのシグネチャ (変化検出用)
        self._queue_sig: dict[int, int] = {}

    def get_state(self, guild_id: int) -> GuildState:
        return self._state.setdefault(guild_id, GuildState())

    def has_subscribers(self, guild_id: int) -> bool:
        """BC-PERF-04: その guild に Activity の WS 購読者がいるか (best-effort)。
        bool 判定のみなのでロック無しの racy read で許容。"""
        subs = self._subs.get(int(guild_id))
        return bool(subs)

    def peek_state(self, guild_id: int) -> GuildState | None:
        """状態を挿入せずに参照する (管理画面の読み取り用)。

        get_state は setdefault で空 GuildState を作ってしまい、
        publish_stopped の掃除 (_state からの削除) を無効化するため、
        ポーリング読み取りには必ずこちらを使う。"""
        return self._state.get(int(guild_id))

    def subscriber_counts(self) -> dict[int, int]:
        """guild_id → Activity WS 購読者数 (has_subscribers と同じく
        ロック無しの best-effort read。表示用途のみ)。"""
        return {gid: len(subs) for gid, subs in self._subs.items() if subs}

    async def _broadcast(
        self,
        guild_id: int,
        message: dict[str, Any],
        user_id_filter: str | None = None,
    ) -> None:
        """guild_id の全 subscriber にメッセージを送る。

        MOD-PERF-03: ここで 1 回だけ JSON 文字列化し、各 queue には文字列を流す。
        sender は送るだけ (購読者数 N に比例した重複 dumps を避ける)。
        user_id_filter を指定するとそのユーザーの queue にだけ送る。
        """
        async with self._lock:
            subs = list(self._subs.get(guild_id, {}).items())
        if not subs:
            return
        payload = json.dumps(message)
        for q, uid in subs:
            if user_id_filter is not None and uid != user_id_filter:
                continue
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                log.warning("subscriber queue full, dropping message guild=%s", guild_id)

    async def publish_state(self, guild_id: int) -> None:
        await self._broadcast(guild_id, self.get_state(guild_id).to_state_message())

    async def publish_queue_full(self, guild_id: int) -> None:
        """MOD-PERF-04: キュー (upnext / music_queue) が前回送出時から変化していれば
        だけ送る。位置/再生状態だけ変わった頻繁な state 更新では曲リストを再送しない。"""
        s = self.get_state(guild_id)
        sig = s.queue_signature()
        if self._queue_sig.get(guild_id) == sig:
            return
        self._queue_sig[guild_id] = sig
        await self._broadcast(guild_id, s.to_queue_message())

    async def publish_progress(self, guild_id: int, position_ms: int) -> None:
        await self._broadcast(guild_id, {"type": "progress", "position_ms": position_ms})

    async def publish_queue(self, guild_id: int) -> None:
        s = self.get_state(guild_id)
        await self._broadcast(
            guild_id,
            {"type": "queue", "queue": [asdict(t) for t in s.upnext]},
        )

    async def publish_stopped(self, guild_id: int) -> None:
        # MOD-PERF-04: 停止/切断時はキャッシュした状態とキュー signature を破棄する。
        # これをしないと _queue_sig に古いキューのハッシュが残り、同じ曲を再投入した
        # ときに queue_full が「変化なし」と誤判定されて UI のキューが空のままになる。
        # 併せて空の queue_full を送って既存購読者の表示も空にし、_state/_queue_sig を
        # 解放して guild 単位のメモリ蓄積も防ぐ。
        s = self.get_state(guild_id)
        s.track = None
        s.upnext = []
        s.music_queue = []
        s.music_queue_total = 0
        s.playlist = None
        s.is_playing = False
        s.loading = False
        s.position_ms = 0
        await self._broadcast(guild_id, {"type": "stopped"})
        await self._broadcast(guild_id, s.to_queue_message())
        self._state.pop(guild_id, None)
        self._queue_sig.pop(guild_id, None)

    async def subscribe(self, guild_id: int, user_id: str | None = None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        async with self._lock:
            self._subs.setdefault(guild_id, {})[q] = user_id
        # 接続直後に現在の state を1通だけ送る (補間の基点になる)。文字列で投入。
        # MOD-PERF-04: 新規購読者にはキューも直接送る (グローバルなシグネチャに
        # 依存せず、この接続が確実に最新キューを得るため)。
        s = self.get_state(guild_id)
        q.put_nowait(json.dumps(s.to_state_message()))
        q.put_nowait(json.dumps(s.to_queue_message()))
        return q

    async def unsubscribe(self, guild_id: int, q: asyncio.Queue) -> None:
        async with self._lock:
            subs = self._subs.get(guild_id)
            if subs and q in subs:
                del subs[q]


bus = StateBus()
