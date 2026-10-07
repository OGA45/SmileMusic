from ast import Continue
import asyncio
import functools
import json
from pickle import NONE
import re
import os
from re import match
import bs4
import discord
from discord import member
from discord.ext import commands
import yt_dlp
import requests
from urllib import request as req
from urllib import parse
from threading import Timer
from datetime import datetime, timedelta, timezone
import uuid as _uuid
from dataclasses import dataclass, asdict as _dc_asdict, field as _dc_field
import logging
import shlex
import subprocess
import random
import time
import collections
import threading
import platform
import importlib.metadata
import psycopg2
import numpy as np
# from niconico_dl_async import NicoNico as niconico_dl
from niconicodl.niconico_dl_async import NicoNico as niconico_dl
# from niconico import NicoNico
import ssl
import re
from discord.opus import Encoder as OpusEncoder
from discord.utils import MISSING
from discord.oggparse import OggStream
from io import BufferedReader, BytesIO
from googleapiclient.discovery import build
import subprocess
import math
import requests
import base64
import uvicorn
import urllib.parse as urllib_parse
from contextvars import ContextVar
from activity_api import (
    AdminProvider,
    PlaylistInfo,
    TrackInfo,
    bus,
    create_app,
    playlist_store,
    register_admin_provider,
    register_command_handler,
    register_connect_handler,
)
from activity_api.net_guard import SsrfBlocked, validate_public_url
# BC-PERF-07 / MOD-PERF-02: smile_music3 の SQL ヘルパも playlist_store と同じ
# グローバル接続 (conn) を使うため、スレッド安全化用の同一ロックを共有する。
from activity_api.playlist_store import db_lock as _db_lock
from activity_api.db_safety import ManagedConnection, ensure_clean_txn, end_open_txn, rollback_quietly


def _db_locked(fn):
    """同期 DB ヘルパを共有ロック (_db_lock) 下で実行するデコレータ。

    ロック下で実行するだけでなく、共有接続を呼び出し単位でクリーンに保つ:
    実行前に前回の残骸トランザクションを片付け (ensure_clean_txn)、
    エラー時は rollback して再送出 (rollback_quietly)、成功時は開いたままの
    トランザクションを閉じる (end_open_txn)。1 クエリの失敗が接続を
    aborted のまま残して以後全滅させる事故を防ぐ。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _db_lock:
            ensure_clean_txn(conn)
            try:
                result = fn(*args, **kwargs)
            except Exception:
                rollback_quietly(conn)
                raise
            end_open_txn(conn)
            return result
    return wrapper


def _valid_uuid(value):
    """クライアント由来の playlist_id 検証。UUID 以外を SQL に渡すと
    InvalidTextRepresentation でトランザクションが失敗するため入口で弾く。
    未選択を表す falsy は正常系なので黙って False、非空の不正値は
    フロントの異常の兆候なので警告ログを残す。"""
    if not value:
        return False
    if not isinstance(value, str):
        log.warning("invalid playlist id from client (non-str): %r", value)
        return False
    try:
        _uuid.UUID(value)
    except (ValueError, TypeError):
        log.warning("invalid playlist id from client: %r", value[:64])
        return False
    return True


async def _db(fn, *fargs, **fkwargs):
    """MOD-PERF-01 / BC-PERF-02: 同期 DB 呼び出しを event loop から外し、
    別スレッド (executor) でロック下に実行する。呼び出し先 (playlist_store の
    関数 / @_db_locked を付けた SQL ヘルパ) は内部で _db_lock を取得するので、
    ここで明示的にロックを取る必要はない (二重取得しても RLock なので安全)。"""
    return await asyncio.to_thread(fn, *fargs, **fkwargs)


# fire-and-forget で投げた背景タスクを GC から守るための参照保持セット。
_bg_tasks: set[asyncio.Task] = set()


def _spawn_db_write(fn, *fargs, **fkwargs):
    """BC-PERF-08: 履歴書き込み等を再生ループから待たずに背景実行する。
    例外はログのみ (再生は止めない)。"""
    async def _runner():
        try:
            await asyncio.to_thread(fn, *fargs, **fkwargs)
        except Exception:
            logging.getLogger(__name__).exception("background DB write failed")
    task = asyncio.create_task(_runner())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _assert_public_url(url: str) -> None:
    """BC-SEC-05: ユーザー指定 URL をフェッチする前に SSRF 検証する。
    内部到達 (private/loopback/link-local/metadata IP) なら ValueError。
    DNS 解決を伴うため別スレッドで実行。"""
    try:
        await asyncio.to_thread(validate_public_url, url)
    except SsrfBlocked as e:
        raise ValueError(f"このURLは取得できません: {e}") from e

log = logging.getLogger(__name__)


# BC-SEC-07: Jellyfin の stream URL (?api_key=<token>) や WS の ?token=<Discord
# access token> がログ/例外/uvicorn アクセスログに乗ると有効トークンが漏れる。
# 出力されるログ中のトークンをマスクする。
# (FFmpeg のプロセス引数 = ps 露出は単一テナント前提で許容する残存リスク)
_SECRET_RE = re.compile(
    r'((?:api_key|api-key|X-Emby-Token|access_token|token)=)[^&\s"\'<>]+',
    re.IGNORECASE,
)


def _mask_secrets(s: str) -> str:
    return _SECRET_RE.sub(r'\1***', s)


def _maybe_mask(s: str) -> str:
    low = s.lower()
    return _mask_secrets(s) if ('key=' in low or 'token=' in low) else s


class _SecretMaskingLogFilter(logging.Filter):
    """ロガー自身に付与してレコードの msg / args (文字列) を in-place でマスクする。

    - args はクリアしない (uvicorn.access の AccessFormatter は args の 5-tuple を
      期待するため、消すと壊れる)。path 引数中の ?token= をその場でマスクする。
    - ロガーレベルに付けることで、uvicorn が後から handler を差し替え/
      propagate=False にしても (ロガーオブジェクトは不変なので) 有効であり続ける。
    """
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = _maybe_mask(record.msg)
            args = record.args
            if isinstance(args, tuple):
                record.args = tuple(
                    _maybe_mask(a) if isinstance(a, str) else a for a in args)
            elif isinstance(args, dict):
                record.args = {
                    k: (_maybe_mask(v) if isinstance(v, str) else v)
                    for k, v in args.items()}
        except Exception:
            pass
        return True


class _MaskingFormatter(logging.Formatter):
    """整形後の最終文字列 (traceback 含む) をマスクする Formatter。
    例外トレースに stream URL が現れるケースを捕捉するため、root handler に使う。"""
    def format(self, record: logging.LogRecord) -> str:
        return _maybe_mask(super().format(record))


def _install_secret_log_masking() -> None:
    """トークンマスキングを有効化する。

    1. 主要ロガー (root / 本モジュール / discord / uvicorn 系) の *ロガー自身* に
       マスキングフィルタを付ける。これにより propagate=False の uvicorn.access でも、
       また uvicorn が serve() 内で後から handler を構成しても有効。
    2. root に handler が無ければ (現状 lastResort 任せ)、traceback もマスクする
       Formatter を持つ WARNING ハンドラを 1 つ追加する (verbosity は据え置き)。
    """
    flt = _SecretMaskingLogFilter()
    for name in ('', __name__, 'discord', 'uvicorn', 'uvicorn.error',
                 'uvicorn.access'):
        logging.getLogger(name).addFilter(flt)
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler()
        h.setLevel(logging.WARNING)
        h.addFilter(flt)
        h.setFormatter(
            _MaskingFormatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
        root.addHandler(h)
    else:
        # 既存 handler があればフィルタを付けて最低限マスクする
        for h in root.handlers:
            h.addFilter(flt)


# ===== 管理画面: エラーログのリングバッファ =====

# 直近の WARNING 以上を管理画面 (/admin) から参照するためのバッファ。
# DB ワーカースレッド (asyncio.to_thread) からも emit されるため Lock で守る
# (読み手の list() 変換が append と競合すると deque でも RuntimeError になる)。
_ERROR_BUFFER: collections.deque = collections.deque(maxlen=500)
_ERROR_BUFFER_LOCK = threading.Lock()


class _AdminLogBufferHandler(logging.Handler):
    """WARNING 以上をメモリに保持する。format 後の文字列 (traceback 含む) にも
    マスキングを通し、トークン類がバッファ経由で漏れないようにする。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = _maybe_mask(self.format(record))
            with _ERROR_BUFFER_LOCK:
                _ERROR_BUFFER.append({
                    "ts": record.created,
                    "level": record.levelname,
                    "logger": record.name,
                    "message": msg[:4000],
                })
        except Exception:
            pass


def _install_admin_error_buffer() -> None:
    """_install_secret_log_masking() の後に呼ぶ (msg/args のマスクが先に効くように)。"""
    h = _AdminLogBufferHandler(level=logging.WARNING)
    h.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(h)


def _admin_get_errors(limit: int) -> list[dict]:
    """新しい順で最大 limit 件返す。"""
    with _ERROR_BUFFER_LOCK:
        items = list(_ERROR_BUFFER)
    return items[::-1][:limit]


# Suppress noise about console usage from errors
yt_dlp.utils.bug_reports_ctx = lambda: ''

# インスタンスごとのダウンロード先。複数インスタンスが ./python の bind mount を
# 共有するため、.opus キャッシュを専用ディレクトリに分離する (未設定なら CWD)。
_DL_DIR = os.environ.get("OGA_MUSIC_DOWNLOAD_DIR", ".")
os.makedirs(_DL_DIR, exist_ok=True)

ytdl_format_options = {
    'format': 'bestaudio/best',
    # ℹ️ See help(yt_dlp.postprocessor) for a list of available Postprocessors and their arguments
    'postprocessors': [{  # Extract audio using ffmpeg
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'opus',
        'preferredquality': '256',
    }],
    'outtmpl': os.path.join(_DL_DIR, '%(id)s'),
    # プレイリストを許可しない
    'noplaylist': True,
    # SSL/TLS証明書を確認しない
    'nocheckcertificate': True,
    # ファイル名にスペース及び&を許可しない
    'restrictfilenames': True,
    # YouTube の JS チャレンジ解決に deno を使用し、ソルバースクリプトを GitHub から取得する
    'js_runtimes': {'deno': {}},
    'remote_components': ['ejs:github'],
}

ffmpeg_options = {
    'before_options': '-vn',
    'options': '-threads 20'
}

ffmpeg_stream_options = {
    'before_options':
    "-vn -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    'options':
    '-threads 20'
}

ffmpeg_livestream_options = {
    'before_options':
    '-vn -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options':
    '-threads 20'
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

# ---------- リアル音響 visualizer 用: 直近の PCM フレームを guild ごとに保持 ----------
# Discord audio thread (read() が呼ばれるスレッド) から append、asyncio タスクから読む。
# collections.deque の append は CPython では thread-safe なのでロック不要。
# 6 フレーム = 120ms ぶん貯めて FFT 4096 (~85ms) に充分な mono サンプルを確保する。
_PCM_FRAME_BUFLEN = 6
_pcm_frame_buffer: dict[int, "collections.deque[bytes]"] = {}

# FFT / バンド集約用の定数 (モジュールロード時に一度だけ計算)
_PCM_SR = 48000
_FFT_SIZE = 4096
_BAND_COUNT = 32
_HANN_WINDOW = np.hanning(_FFT_SIZE).astype(np.float32)
# 40 Hz – 16 kHz を対数で 32 バンドに分割し、それぞれを FFT bin index に対応付け。
# FFT 4096 / 48kHz → bin 幅 ≒ 11.7 Hz なので 32 バンド全てに個別 bin を割り当て可能。
_band_edges_hz = np.geomspace(40.0, 16000.0, _BAND_COUNT + 1)
_band_edges_bin = np.clip(
    (_band_edges_hz * _FFT_SIZE / _PCM_SR).astype(int),
    0, _FFT_SIZE // 2,
)
# MOD-PERF-05: 各バンドの [lo, hi) を python int で 1 度だけ確定 (lo<hi を保証)。
# per-frame の int() キャストと clamp 分岐を排除する。
_band_ranges: list[tuple[int, int]] = []
for _i in range(_BAND_COUNT):
    _lo = int(_band_edges_bin[_i])
    _hi = int(_band_edges_bin[_i + 1])
    if _hi <= _lo:
        _hi = _lo + 1
    _band_ranges.append((_lo, _hi))


def _push_pcm_frame(guild_id: int, pcm: bytes) -> None:
    dq = _pcm_frame_buffer.get(guild_id)
    if dq is None:
        dq = collections.deque(maxlen=_PCM_FRAME_BUFLEN)
        _pcm_frame_buffer[guild_id] = dq
    dq.append(pcm)


class _BandNormalizer:
    """各バンドのピークを追従させて 0..1 にオートレベリングする。"""
    __slots__ = ("peak",)

    def __init__(self) -> None:
        self.peak = np.full(_BAND_COUNT, 0.5, dtype=np.float32)

    def normalize(self, bands: np.ndarray) -> np.ndarray:
        # slow decay + 即時 attack
        self.peak *= 0.985
        np.maximum(self.peak, bands, out=self.peak)
        # ゼロ除算回避
        return np.clip(bands / np.maximum(self.peak, 1e-3), 0.0, 1.0)


class _BeatDetector:
    """低域の RMS 履歴から「平均より明らかに大きい」フレームをビートとして拾う。"""
    __slots__ = ("history", "last_beat_ts")

    def __init__(self) -> None:
        # ~1.7 秒分 (25 Hz × 43 frames)
        self.history: collections.deque[float] = collections.deque(maxlen=43)
        self.last_beat_ts = 0.0

    def feed(self, low_energy: float, now: float) -> bool:
        self.history.append(low_energy)
        if len(self.history) < 12:
            return False
        avg = sum(self.history) / len(self.history)
        if avg <= 1e-5:
            return False
        # 平均の 1.4 倍を超えたら beat 候補。ただし 180ms クールダウン
        if low_energy > avg * 1.4 and (now - self.last_beat_ts) > 0.18:
            self.last_beat_ts = now
            return True
        return False


_band_norm: dict[int, _BandNormalizer] = {}
_beat_detect: dict[int, _BeatDetector] = {}


def _compute_audio_features(
    guild_id: int, now: float,
) -> tuple[float, list[float], bool]:
    """直近フレーム群から RMS / 32 バンド / ビートを返す。"""
    dq = _pcm_frame_buffer.get(guild_id)
    if not dq:
        return 0.0, [0.0] * _BAND_COUNT, False
    # 別スレッド (to_thread) から呼ばれる。audio thread が同時に append しても
    # 安全なよう list() でスナップショットを取ってから結合する。
    combined = b"".join(list(dq))
    arr = np.frombuffer(combined, dtype=np.int16)
    if arr.size < 4:
        return 0.0, [0.0] * _BAND_COUNT, False
    # stereo → mono ダウンミックス + [-1, 1] 正規化
    mono = arr.reshape(-1, 2).mean(axis=1).astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(mono * mono)))

    # FFT: 末尾 _FFT_SIZE サンプルを Hann 窓掛けで解析
    if mono.size >= _FFT_SIZE:
        windowed = mono[-_FFT_SIZE:] * _HANN_WINDOW
    else:
        pad = np.zeros(_FFT_SIZE, dtype=np.float32)
        pad[-mono.size:] = mono
        windowed = pad * _HANN_WINDOW
    spectrum = np.abs(np.fft.rfft(windowed)).astype(np.float32)

    # バンドへ集約 (事前計算済みレンジを使用)
    raw_bands = np.empty(_BAND_COUNT, dtype=np.float32)
    for i, (lo, hi) in enumerate(_band_ranges):
        raw_bands[i] = spectrum[lo:hi].mean()

    # オートレベリング
    normalizer = _band_norm.setdefault(guild_id, _BandNormalizer())
    bands = normalizer.normalize(raw_bands)

    # ビート: 低域 (バンド 0..3 = 40Hz–約 100Hz 帯) の生エネルギーを履歴と比較
    low_energy = float(raw_bands[:4].mean())
    detector = _beat_detect.setdefault(guild_id, _BeatDetector())
    beat = detector.feed(low_energy, now)

    return min(1.0, rms), bands.tolist(), beat


def _reset_audio_features_state(guild_id: int) -> None:
    """再生停止 / ソース切替時に履歴をリセットする。"""
    _pcm_frame_buffer.pop(guild_id, None)
    _band_norm.pop(guild_id, None)
    _beat_detect.pop(guild_id, None)


# ---------- yt-dlp extract_info の TTL キャッシュ + 重複抽出抑止 ----------
# stream URL は ~6 時間で expire するので、それより短い TTL でキャッシュする。
_YTDL_CACHE_TTL_S = 4 * 3600
_ytdl_cache: dict[str, tuple[float, dict]] = {}
_ytdl_inflight: dict[str, "asyncio.Future"] = {}


def _ytdl_cache_get(url: str) -> dict | None:
    entry = _ytdl_cache.get(url)
    if entry is None:
        return None
    expires_at, data = entry
    if time.time() > expires_at:
        _ytdl_cache.pop(url, None)
        return None
    return data


def _ytdl_cache_set(url: str, data: dict) -> None:
    _ytdl_cache[url] = (time.time() + _YTDL_CACHE_TTL_S, data)


def _ytdl_cache_invalidate(url: str) -> None:
    _ytdl_cache.pop(url, None)


async def _extract_ytdl_cached(url: str, stream: bool) -> dict:
    """ytdl.extract_info をキャッシュ込みで呼ぶ。

    - stream=False (ファイルへ download) はキャッシュしない (ファイルが消える可能性)
    - 同一 URL の並行呼び出しは Future を共有して重複抽出を防ぐ
    """
    if not stream:
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(
            None, lambda: ytdl.extract_info(url, download=True),
        )
        return raw["entries"][0] if "entries" in raw else raw

    cached = _ytdl_cache_get(url)
    if cached is not None:
        return cached

    fut = _ytdl_inflight.get(url)
    if fut is None:
        loop = asyncio.get_event_loop()
        fut = loop.run_in_executor(
            None, lambda: ytdl.extract_info(url, download=False),
        )
        _ytdl_inflight[url] = fut
        fut.add_done_callback(lambda _f, _u=url: _ytdl_inflight.pop(_u, None))
        # 待ち手が全員キャンセルされても失敗が "never retrieved" にならないよう取り出しておく
        fut.add_done_callback(lambda _f: _f.cancelled() or _f.exception())
    # 共有の Future なので、待っている側の 1 つがキャンセルされても (再生タスクの
    # 差し替え等) 他の待ち手 (キュー追加・先読み) を巻き込まないよう、fut 自体は
    # キャンセルされない asyncio.wait で待つ (shield は 3.14 で失敗時に ERROR ログを出す)
    await asyncio.wait((fut,))
    raw = fut.result()
    data = raw["entries"][0] if "entries" in raw else raw
    _ytdl_cache_set(url, data)
    return data


client = discord.Client(intents=discord.Intents.all())
tree = discord.app_commands.CommandTree(client)


# ---------- 進行中プロセス (プレイリストインポートなど) のトラッキング ----------
@dataclass
class _Process:
    id: str
    kind: str  # "import" など
    source_url: str = ""
    name: str = ""
    status: str = "running"  # "running" | "success" | "error"
    progress_current: int = 0
    progress_total: int = 0
    message: str = ""
    started_at: str = ""
    finished_at: str = ""


# (guild_id, user_id) → 古い順のリスト。50 件で頭から捨てる。
_processes: dict[tuple[int, str], list[_Process]] = {}
_PROCESS_MAX = 50


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _process_list(guild_id: int, user_id: str) -> list[_Process]:
    return _processes.setdefault((int(guild_id), str(user_id)), [])


def _process_create(
    guild_id: int, user_id: str, kind: str, source_url: str, name: str = "",
) -> _Process:
    p = _Process(
        id=str(_uuid.uuid4()),
        kind=kind,
        source_url=source_url,
        name=name or source_url,
        started_at=_now_iso(),
    )
    lst = _process_list(guild_id, user_id)
    lst.append(p)
    while len(lst) > _PROCESS_MAX:
        lst.pop(0)
    return p


def _process_update(p: _Process, **fields) -> None:
    for k, v in fields.items():
        if hasattr(p, k):
            setattr(p, k, v)


async def _publish_processes(guild_id: int, user_id: str) -> None:
    items = _process_list(guild_id, user_id)
    await bus._broadcast(
        guild_id,
        {
            "type": "processes",
            "items": [_dc_asdict(p) for p in items],
        },
        user_id_filter=str(user_id),
    )


# ---------- Jellyfin 統合 ----------
# 環境変数で接続情報を受け取る (BOT 専用アカウント想定)
#   JELLYFIN_BASE_URL=https://jellyfin.example.com
#   JELLYFIN_USERNAME=smile
#   JELLYFIN_PASSWORD=...
_JELLYFIN_BASE_URL = (os.environ.get("JELLYFIN_BASE_URL") or "").rstrip("/")
_JELLYFIN_USERNAME = os.environ.get("JELLYFIN_USERNAME") or ""
_JELLYFIN_PASSWORD = os.environ.get("JELLYFIN_PASSWORD") or ""
_JELLYFIN_DEVICE_ID = "smile-music3-bot"
_JELLYFIN_CLIENT_NAME = "SmileMusic"
_jellyfin_auth_cache: dict | None = None
_jellyfin_auth_lock: asyncio.Lock | None = None
_jellyfin_bitrate_cache: dict[str, int] = {}  # item_id → kbps


def _jellyfin_configured() -> bool:
    return bool(_JELLYFIN_BASE_URL and _JELLYFIN_USERNAME and _JELLYFIN_PASSWORD)


def _is_jellyfin_url(url: str) -> bool:
    """このボットが知っている Jellyfin サーバの web URL かを判定する。"""
    if not _JELLYFIN_BASE_URL or not url:
        return False
    return url.startswith(_JELLYFIN_BASE_URL)


def _jellyfin_item_id_from_url(url: str) -> str | None:
    """Jellyfin の web URL / API URL から item id (32 桁 hex) を取り出す。

    対応形式:
      - web UI: https://host/web/#/details?id=<id>&serverId=<sid>
      - API:    https://host/Items/<id>/Download?api_key=...
                https://host/Items/<id>/Stream
                https://host/Audio/<id>/stream
                https://host/Audio/<id>/universal
                https://host/Users/<userid>/Items/<id>
                等、`/Items/<id>` / `/Audio/<id>` を含むあらゆるエンドポイント
    """
    # 1. web UI の hash route
    m = re.search(r"#/details\?(?:[^#]*&)?id=([a-f0-9]{32})", url, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    # 2. API パス (/Items/{id}/... または /Audio/{id}/...)
    m = re.search(
        r"/(?:Items|Audio)/([a-f0-9]{32})(?:[/?#]|$)", url, re.IGNORECASE,
    )
    if m:
        return m.group(1).lower()
    return None


# === 履歴に残すタイトル / URL の正規化 =====================================
# yt-dlp の GenericIE は専用 extractor が無い URL の basename をそのまま
# タイトルにするため、Discord CDN の添付 (8f8a7c3556b16208.mp3 / MAD.ogg) や
# Jellyfin の /Items/<id>/Download が「曲名」として履歴に残り、集計を壊す。
_BARE_HEX_TITLE_RE = re.compile(r'^[0-9a-f]{8,}$', re.IGNORECASE)
_MEDIA_EXT_RE = re.compile(
    r'\.(mp3|m4a|aac|ogg|oga|opus|wav|flac|webm|mp4|mov|mkv|ts)$', re.IGNORECASE)
# 単体では曲名として意味を持たない汎用語 (yt-dlp が URL 末尾から拾ったもの)
_GENERIC_TITLE_WORDS = {
    "download", "video", "audio", "stream", "streaming", "index", "file",
    "play", "playlist", "track", "media", "untitled", "unknown", "output",
    "temp", "tmp", "attachment", "無題",
}


def _url_basename(url: str) -> str:
    """URL のクエリ/フラグメントを除いたファイル名部分 (percent-decode 済み)。"""
    try:
        path = urllib_parse.urlsplit(url).path or ""
    except Exception:
        return ""
    return urllib_parse.unquote(path.rsplit("/", 1)[-1])


def _looks_like_junk_title(title: str, url: str = "") -> bool:
    """曲名として使い物にならないタイトルか判定する。

    URL 由来判定は「メディア拡張子付き URL / Jellyfin item URL」かつ
    basename と完全一致する場合のみに限定する (YouTube や SoundCloud の
    正規タイトルを誤って弾かないため)。
    """
    t = (title or "").strip()
    if not t:
        return True
    if _BARE_HEX_TITLE_RE.match(t) or t.isdigit():
        return True
    if t.lower() in _GENERIC_TITLE_WORDS:
        return True
    if url:
        name = _url_basename(url)
        if name and (_MEDIA_EXT_RE.search(name) or _jellyfin_item_id_from_url(url)):
            if t == _MEDIA_EXT_RE.sub("", name) or t == name:
                return True
    return False


def _label_from_media_url(url: str) -> str:
    """最後の手段: URL から人間が読めるラベルを作る。

      cdn.discordapp.com/.../MAD.ogg?ex=..   -> 'Discord添付: MAD.ogg'
      <host>/Items/<32hex>/Download?api_key= -> 'Jellyfin: 8f8a7c35'
    """
    try:
        parts = urllib_parse.urlsplit(url)
    except Exception:
        return "Unknown"
    host = (parts.hostname or "").lower()
    name = _url_basename(url)
    jf_id = _jellyfin_item_id_from_url(url)
    if jf_id:
        return f"Jellyfin: {jf_id[:8]}"
    if host.endswith("discordapp.com") or host.endswith("discordapp.net"):
        return f"Discord添付: {name or 'attachment'}"
    if name and not _BARE_HEX_TITLE_RE.match(_MEDIA_EXT_RE.sub("", name)):
        return f"{host}: {name}"[:200]
    return host or "Unknown"


def _normalize_media_title(raw, data: dict | None = None, url: str = "") -> str:
    """取得したタイトルを検証し、ゴミなら他のメタ情報 -> URL の順で代替を作る。
    yt-dlp 系の取り込み口と履歴書き込みの両方で必ずこれを通す。"""
    t = (raw or "").strip()
    if not _looks_like_junk_title(t, url):
        return t[:200]
    d = data or {}
    # 1) yt-dlp の別フィールド (track / artist / alt_title / fulltitle)
    track = d.get("track") if isinstance(d.get("track"), str) else ""
    track = (track or "").strip()
    artist = d.get("artist") or d.get("creator") or ""
    if track:
        cand = (f"{artist} - {track}".strip(" -")
                if isinstance(artist, str) and artist else track)
        if not _looks_like_junk_title(cand, url):
            return cand[:200]
    for key in ("alt_title", "fulltitle"):
        cand = d.get(key) if isinstance(d.get(key), str) else ""
        cand = (cand or "").strip()
        if cand and cand != t and not _looks_like_junk_title(cand, url):
            return cand[:200]
    # 2) URL 由来のラベル
    return (_label_from_media_url(url) or "Unknown")[:200]


# history3.url は /histry の埋め込みと管理画面の表示専用で、再生の再解決には
# 一切使われない。Jellyfin の api_key や Discord CDN の署名 (hm) がそのまま
# 保存されると閲覧者に有効なトークンが漏れるため、値だけをマスクする
# (キー名と他のクエリは残すので、どこ由来の URL かは追える)。
_URL_SECRET_RE = re.compile(
    r'([?&](?:api_key|api-key|X-Emby-Token|access_token|token|hm|sig|signature)=)'
    r'[^&#\s]*',
    re.IGNORECASE,
)


def _sanitize_history_url(url: str) -> str:
    """履歴に保存する URL からトークン/署名をマスクする。"""
    if not url:
        return url
    return _URL_SECRET_RE.sub(r'\1***', url)[:2000]


async def _jellyfin_authenticate(force: bool = False) -> dict | None:
    """Jellyfin に認証してアクセストークンを取得 (キャッシュ込み)。"""
    global _jellyfin_auth_cache, _jellyfin_auth_lock
    if not _jellyfin_configured():
        return None
    if _jellyfin_auth_lock is None:
        _jellyfin_auth_lock = asyncio.Lock()
    async with _jellyfin_auth_lock:
        if _jellyfin_auth_cache and not force:
            return _jellyfin_auth_cache
        try:
            import httpx
            headers = {
                "X-Emby-Authorization": (
                    f'MediaBrowser Client="{_JELLYFIN_CLIENT_NAME}", '
                    f'Device="discord-bot", DeviceId="{_JELLYFIN_DEVICE_ID}", '
                    'Version="1.0"'
                ),
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"{_JELLYFIN_BASE_URL}/Users/AuthenticateByName",
                    json={"Username": _JELLYFIN_USERNAME, "Pw": _JELLYFIN_PASSWORD},
                    headers=headers,
                )
        except Exception:
            log.exception('unexpected error')
            _jellyfin_auth_cache = None
            return None
        if r.status_code != 200:
            log.warning("jellyfin auth failed: %s", r.status_code)
            _jellyfin_auth_cache = None
            return None
        d = r.json()
        _jellyfin_auth_cache = {
            "access_token": d.get("AccessToken") or "",
            "user_id": (d.get("User") or {}).get("Id") or "",
            "server_id": d.get("ServerId") or "",
        }
        return _jellyfin_auth_cache


def _jellyfin_stream_url(item_id: str, access_token: str) -> str:
    """指定アイテムを再生するためのストリーム URL (原音そのまま)。"""
    return (
        f"{_JELLYFIN_BASE_URL}/Audio/{item_id}/stream"
        f"?static=true&api_key={access_token}"
    )


def _jellyfin_image_url(item_id: str, tag: str | None) -> str:
    if not tag:
        return ""
    return (
        f"{_JELLYFIN_BASE_URL}/Items/{item_id}/Images/Primary"
        f"?tag={tag}&maxHeight=600"
    )


def _jellyfin_item_url(item_id: str, server_id: str) -> str:
    return f"{_JELLYFIN_BASE_URL}/web/#/details?id={item_id}&serverId={server_id}"


def _jellyfin_item_to_track(
    item: dict,
    album_id: str = "",
    album_image_tag: str = "",
    server_id: str = "",
) -> "playlist_store.PlaylistTrack | None":
    item_id = item.get("Id")
    if not item_id:
        return None
    name = item.get("Name") or "Unknown"
    duration_ms = (item.get("RunTimeTicks") or 0) // 10000  # 100ns ticks → ms
    own_tag = (item.get("ImageTags") or {}).get("Primary") or ""
    if own_tag:
        artwork = _jellyfin_image_url(item_id, own_tag)
    elif album_id and album_image_tag:
        artwork = _jellyfin_image_url(album_id, album_image_tag)
    else:
        artwork = ""
    # ビットレートをキャッシュ (kbps)
    ms = (item.get("MediaSources") or [None])[0]
    if ms and ms.get("Bitrate"):
        try:
            _jellyfin_bitrate_cache[item_id] = int(int(ms["Bitrate"]) / 1000)
        except (TypeError, ValueError):
            pass
    return playlist_store.PlaylistTrack(
        title=name,
        url=_jellyfin_item_url(item_id, server_id),
        artwork=artwork,
        duration_ms=int(duration_ms),
    )


async def _resolve_jellyfin_album(
    url: str,
) -> tuple[str, list["playlist_store.PlaylistTrack"]]:
    """Jellyfin の web URL からアルバム/プレイリスト/曲を解決する。"""
    if not _jellyfin_configured():
        raise ValueError("Jellyfin が未設定です (JELLYFIN_BASE_URL 等の環境変数)")
    item_id = _jellyfin_item_id_from_url(url)
    if not item_id:
        raise ValueError("Jellyfin の item ID が URL から取れません")
    auth = await _jellyfin_authenticate()
    if not auth:
        raise ValueError("Jellyfin の認証に失敗しました")
    import httpx
    headers = {"X-Emby-Token": auth["access_token"]}

    async def _get(client: "httpx.AsyncClient", path: str, **kwargs):
        r = await client.get(f"{_JELLYFIN_BASE_URL}{path}", **kwargs)
        if r.status_code == 401:
            # トークン失効 -> 再認証して 1 度だけリトライ
            new_auth = await _jellyfin_authenticate(force=True)
            if new_auth:
                headers["X-Emby-Token"] = new_auth["access_token"]
                client.headers["X-Emby-Token"] = new_auth["access_token"]
                r = await client.get(f"{_JELLYFIN_BASE_URL}{path}", **kwargs)
        return r

    async with httpx.AsyncClient(timeout=12.0, headers=headers) as client:
        item_r = await _get(client, f"/Users/{auth['user_id']}/Items/{item_id}")
        if item_r.status_code != 200:
            raise ValueError(
                f"Jellyfin アイテム取得失敗 ({item_r.status_code})",
            )
        item_data = item_r.json()
        item_type = item_data.get("Type", "")
        album_name = item_data.get("Name") or "Jellyfin Item"
        primary_tag = (item_data.get("ImageTags") or {}).get("Primary") or ""
        server_id = auth.get("server_id", "")

        # 単曲の場合は 1 トラックだけ返す
        if item_type == "Audio":
            track = _jellyfin_item_to_track(
                item_data, "", primary_tag, server_id=server_id,
            )
            return album_name, ([track] if track else [])

        # MusicAlbum / Playlist など: 子アイテム (Audio) を列挙
        tracks_r = await _get(
            client,
            f"/Users/{auth['user_id']}/Items",
            params={
                "ParentId": item_id,
                "IncludeItemTypes": "Audio",
                "Fields": "MediaSources,RunTimeTicks,IndexNumber,Artists",
                "Recursive": "false",
                "SortBy": "IndexNumber,Name",
            },
        )
        if tracks_r.status_code != 200:
            raise ValueError(
                f"Jellyfin トラック取得失敗 ({tracks_r.status_code})",
            )
        tracks_data = tracks_r.json()

    tracks: list[playlist_store.PlaylistTrack] = []
    for item in (tracks_data.get("Items") or []):
        t = _jellyfin_item_to_track(
            item, album_id=item_id, album_image_tag=primary_tag,
            server_id=server_id,
        )
        if t:
            tracks.append(t)
    return album_name, tracks


def _make_progress_bar(percent, length=20):
    filled = int(length * percent / 100)
    return '█' * filled + '░' * (length - filled)


def _format_bytes(b):
    if not b:
        return '?MB'
    mb = b / (1024 * 1024)
    return f'{mb:.1f}MB' if mb >= 1 else f'{b / 1024:.0f}KB'


async def _download_with_progress(ctx, url, loop):
    bar = _make_progress_bar(0)
    embed = discord.Embed(
        title="INFO",
        description=f"[キャッシュ]({url})を作成しています...\n{bar} 0%",
        colour=discord.Colour.from_rgb(0, 0, 255))
    progress_msg = await ctx.channel.send(embed=embed)

    last_percent = [0]

    def progress_hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            downloaded = d.get('downloaded_bytes', 0)
            if not total or total <= 0:
                return
            percent = int(downloaded / total * 100)
            if percent - last_percent[0] < 10:
                return
            last_percent[0] = percent
            bar = _make_progress_bar(percent)
            size_str = f'{_format_bytes(downloaded)} / {_format_bytes(total)}'
            desc = f"[キャッシュ]({url})を作成しています...\n{bar} {percent}% | {size_str}"
            new_embed = discord.Embed(
                title="INFO", description=desc,
                colour=discord.Colour.from_rgb(0, 0, 255))
            asyncio.run_coroutine_threadsafe(
                progress_msg.edit(embed=new_embed), loop)
        elif d['status'] == 'finished':
            bar = _make_progress_bar(100)
            desc = f"[キャッシュ]({url})を作成しています...\n{bar} 100% | エンコード中..."
            new_embed = discord.Embed(
                title="INFO", description=desc,
                colour=discord.Colour.from_rgb(0, 0, 255))
            asyncio.run_coroutine_threadsafe(
                progress_msg.edit(embed=new_embed), loop)

    dl_options = dict(ytdl_format_options)
    dl_options['progress_hooks'] = [progress_hook]
    dl_ytdl = yt_dlp.YoutubeDL(dl_options)

    data = await loop.run_in_executor(
        None, lambda: dl_ytdl.extract_info(url, download=True))

    embed = discord.Embed(
        title="INFO",
        description=f"[キャッシュ]({url})の作成が完了しました。",
        colour=discord.Colour.from_rgb(0, 255, 0))
    await progress_msg.edit(embed=embed)

    return data


# ---------- 再生失敗の検出・通知 ----------
# 以前は discord.py の probe() が ffprobe の失敗を握りつぶして (None, None) を返し、
# ffmpeg が即 403 で終わっても after(e) の中身を見ていなかったため、再生できない曲が
# 「正常に再生し終えた」扱いで履歴に残り、ループ系モードでは空回りしていた。

# ノーマライズの ffmpeg フィルタ。プレイヤーが自分の options として持ち回し、
# シーク (ffmpeg 再起動) でも外れないようにする。set_normalize は文字列一致で着脱する。
_LOUDNORM_AF = "-af loudnorm=I=-14:TP=-1.5:LRA=11"
# 実音声 1 秒 (20ms x 50)。これに届いたら「再生された」とみなして履歴を書き、
# 届く前に ffmpeg が異常終了したら「再生不可」として扱う。
_STARTED_FRAMES = 50
# 連続でこの曲数だけ再生に失敗したら再生ループを止める (全曲 403 等での空回り・連投防止)。
_MAX_CONSEC_FAILS = 5
# ffmpeg の stderr をログに流す上限 (1 プロセスあたり)。
_FFMPEG_LOG_LINES_PER_PROC = 20
# ログに出す URL はホスト名まで (googlevideo の URL には ip= や署名が入っている)。
_URL_IN_LOG_RE = re.compile(r'(https?://[^/\s?]+)\S*?(?=[.:,;]?(?:\s|$))')
# ffmpeg が exit 0 でも「途中で切れた」と判断する stderr の手掛かり
# (reconnect 先が 403 になった場合などは exit 0 で終わるため)。
_NET_ERR_MARKERS = ("HTTP error", "Server returned", "Will reconnect", "IO error",
                    "Connection reset", "Connection timed out", "Stream ends prematurely")
# 再接続で回復し得るものを除いた「確実に失敗」の手掛かり (1 秒未満で終わった曲の判定用)
_NET_FATAL_MARKERS = ("HTTP error", "Server returned", "Connection refused",
                      "Connection timed out", "Error opening")


class SourceUnavailable(Exception):
    """音源に到達できない (ffprobe 失敗)。
    discord.ClientException を継承しないこと (play_music で「VC 切断」扱いされてしまう)。"""


class PlayerFinished(Exception):
    """再生が終わった (または片付け済みの) プレイヤーに対するシーク。"""


def _redact_urls(s: str) -> str:
    return _URL_IN_LOG_RE.sub(r'\1/…', s)


def _drain_ffmpeg_stderr(pipe, tail, lock, pid, guild_id) -> None:
    """ffmpeg の stderr を行単位で読み、末尾をリングに残しつつログへ流す。
    読み続けないと pipe が詰まって ffmpeg が止まるので、例外が出ても読み続ける。
    EOF で必ず抜ける (discord.py の _pipe_reader のような空回りをしない)。"""
    logged = suppressed = 0
    try:
        while True:
            try:
                raw = pipe.readline(4096)
            except (ValueError, OSError):
                break
            if not raw:
                break
            try:
                line = _redact_urls(raw.decode('utf-8', 'replace').rstrip())
                if not line:
                    continue
                with lock:
                    tail.append(line)
                if logged < _FFMPEG_LOG_LINES_PER_PROC:
                    logged += 1
                    log.warning('ffmpeg[%s g=%s] %s', pid, guild_id, line)
                else:
                    suppressed += 1
            except Exception:
                pass
    finally:
        if suppressed:
            log.warning('ffmpeg[%s g=%s] %d more stderr lines suppressed',
                        pid, guild_id, suppressed)
        try:
            pipe.close()
        except Exception:
            pass


def _reap_ffmpeg(proc, timeout: float = 2.0):
    """SIGKILL して終了を待つ。stderr は drain スレッドが持っているので communicate() は使わない。"""
    if proc is None or proc is MISSING:
        return None
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    except Exception:
        log.exception('kill ffmpeg %s failed', getattr(proc, 'pid', '?'))
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning('ffmpeg %s still alive %.1fs after SIGKILL',
                    getattr(proc, 'pid', '?'), timeout)
        return None


async def _probe_source(src: str) -> None:
    """再生前の到達確認 (結果の codec は使っていない)。確実に失敗したら SourceUnavailable。
    タイムアウトは「不明」として再生側に判断を任せる (subprocess.run が ffprobe を kill する)。"""
    args = ['ffprobe', '-v', 'error', '-select_streams', 'a:0',
            '-show_entries', 'stream=codec_name', '-of', 'csv=p=0', src]

    def _run():
        return subprocess.run(args, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=20)
    try:
        r = await asyncio.get_running_loop().run_in_executor(None, _run)
    except subprocess.TimeoutExpired:
        log.warning('ffprobe timeout (20s), letting playback decide: %s', _redact_urls(src))
        return
    except OSError:
        log.exception('ffprobe could not run')
        return
    if r.returncode != 0 or not r.stdout.strip():
        msg = _redact_urls(r.stderr.decode('utf-8', 'replace')).strip()[-300:]
        raise SourceUnavailable(f'ffprobe rc={r.returncode}: {msg or "no audio stream"}')


def _classify_playback_end(player, err, expected_ms: int = 0) -> str:
    """再生終了の理由を 'ok' / 'stopped' / 'early_fail' / 'midtrack' に分類する。
    play_future 解決後は cleanup 済みの可能性があるので player 自身の属性だけを見る。"""
    eof = getattr(player, 'end_reason', None) == 'eof'
    frames = getattr(player, 'frames_read', 0)
    rc = getattr(player, 'eof_returncode', None)
    failed = err is not None or (eof and rc not in (0, None))
    stderr = getattr(player, 'eof_stderr', '') or ''
    fatal = eof and any(m in stderr for m in _NET_FATAL_MARKERS)
    if not eof and err is None:
        # read() が EOF を返していない = vc.stop() (skip/stop/prev/選曲/モード切替) か VC 切断
        return 'stopped'
    if (frames == 0 and not failed and not fatal and expected_ms > 0
            and player.total_milliseconds + 1000 >= expected_ms):
        # 曲の長さ以降へのシーク (/play seek: など)。鳴らす部分が無いだけで音源の失敗ではない
        return 'ok'
    if frames < _STARTED_FRAMES:
        # 1 秒未満で終わった: exit 0 でも接続エラーが出ていれば失敗 (途中で切れて
        # reconnect が 403 になると ffmpeg は exit 0 で終わる)。本当に短い音源は正常扱い。
        return 'early_fail' if (frames == 0 or failed or fatal) else 'ok'
    if failed:
        return 'midtrack'
    slack = min(10_000, expected_ms // 10)
    if (expected_ms > 0 and player.total_milliseconds + slack < expected_ms
            and any(m in stderr for m in _NET_ERR_MARKERS)):
        return 'midtrack'
    return 'ok'


def _forget_player(guild_id: int, player) -> None:
    """終わったプレイヤーを guild_table から外す (/seek 等で孤児 ffmpeg を作らないため)。"""
    st = guild_table.get(guild_id)
    if st is not None and st.get('player') is player:
        st['player'] = None


async def _playback_error_notice(ctx, guild_id: int, text: str, *, level: str = 'error') -> None:
    """再生失敗をテキストチャンネル (Activity 起動時の _FakeCtx では no-op) と
    Activity の全視聴者に知らせる。_notify() は操作ユーザーにしか届かないので使わない。"""
    rgb = (255, 0, 0) if level == 'error' else (0, 0, 255)
    try:
        await embed_async_send(ctx, "エラー" if level == 'error' else "INFO", text, *rgb)
    except Exception:
        log.debug('channel notice failed', exc_info=True)
    try:
        await bus._broadcast(guild_id, {"type": "notify", "level": level, "message": text})
    except Exception:
        log.debug('ws notice failed', exc_info=True)


def _want_normalize(guild_id: int, per_track) -> bool:
    """ノーマライズを掛けるか。曲ごとに ON/OFF を明示していればそれ (/play normalize)、
    未指定 (None) なら Activity のトグル (guild 設定。未操作なら OFF) に従う。"""
    if per_track is not None:
        return bool(per_track)
    return bool((guild_table.get(guild_id) or {}).get("normalize"))


def _expected_ms_from_queue_item(info: dict) -> int:
    t = info.get('time')
    s = to_total_second(t) if isinstance(t, datetime) else 0
    return s * 1000 if s > 1 else 0  # to_time(1) は「長さ不明 / ライブ」の番兵


def _make_audio_source(souce, option, guild_id, *, live=False):
    """音声出力は PCM (OriginalFFmpegPCMAudio)。discord.py が PCM→Opus エンコードする。
    read() で「実際に送出するフレーム」をそのまま visualizer に流す (_push_pcm_frame)
    ので visualizer が音と完全同期する。再生中にソースを差し替えないのでスキップしない。

    NOTE: BC-PERF-01 (購読者ゼロ時 Opus パススルー + 別プロセスタップ) は、ライブ差し替えの
    スキップ事故や別デコードによる visualizer の同期ズレを招いたため撤回した。完全同期と
    堅牢性 (全コーデック/シーク) を優先し、実績のある PCM 経路に統一する。
    (購読者ゼロ guild でも FFT 解析自体は BC-PERF-04 で skip されるので無駄計算は無い)"""
    return OriginalFFmpegPCMAudio(souce, **option, guild_id=guild_id, live=live)


async def from_url(ctx: discord.Interaction, url: str, *, loop: asyncio.AbstractEventLoop = None, stream=False, live=False, normalize=False) -> discord.FFmpegOpusAudio | discord.FFmpegPCMAudio:
    loop = loop or asyncio.get_event_loop()
    _data = None
    _direct_audio_url: str | None = None
    if url.startswith("https://www.nicovideo.jp/"):
        stream = False
        # 既存のファイルを調べる
        match = re.search(r'/watch/(sm[0-9]+)', url)
        if match:
            file_name = os.path.join(_DL_DIR, f"{match.group(1)}.opus")
            if os.path.exists(file_name):
                cache_option = dict(ffmpeg_options)
                if normalize:
                    cache_option['options'] = cache_option.get('options', '') + ' ' + _LOUDNORM_AF
                # BC-PERF-01: 購読者がいれば PCM (visualizer タップ)、いなければ Opus
                return _make_audio_source(
                    file_name, cache_option, ctx.guild.id, live=live,
                )
            else:
                _data = await _download_with_progress(ctx, url, loop)
    elif _SUNO_PATTERN.search(url):
        # SUNO は yt-dlp が未対応。UUID から直接 cdn1.suno.ai の MP3 URL を組み立てる。
        m = _SUNO_PATTERN.search(url)
        if m:
            _direct_audio_url = _suno_audio_url(m.group(1).lower())
            stream = True
    elif _is_jellyfin_url(url):
        # Jellyfin: 認証して item id から /Audio/{id}/stream URL を作る
        jf_item_id = _jellyfin_item_id_from_url(url)
        if jf_item_id:
            jf_auth = await _jellyfin_authenticate()
            if jf_auth and jf_auth.get("access_token"):
                _direct_audio_url = _jellyfin_stream_url(
                    jf_item_id, jf_auth["access_token"],
                )
                stream = True
    option: dict[str:str]
    if live:
        option = ffmpeg_livestream_options
    elif stream:
        option = ffmpeg_stream_options
    else:
        option = ffmpeg_options
    if normalize:
        option = dict(option)
        option['options'] = option.get('options', '') + ' ' + _LOUDNORM_AF
    if _direct_audio_url is not None:
        souce = _direct_audio_url
    elif _data is None:
        data = await _extract_ytdl_cached(url, stream)
        if 'entries' in data:
            data = data['entries'][0]
        souce = str(data['url'] if stream else (
            ytdl.prepare_filename(data) + ".opus"))
    else:
        data = _data
        if 'entries' in data:
            data = data['entries'][0]
        souce = str(data['url'] if stream else (
            ytdl.prepare_filename(data) + ".opus"))

    # discord.FFmpegOpusAudio.probe() は失敗しても例外を出さず (None, None) を返すので
    # 使わない (以前はこのせいで下のリトライが一度も動いていなかった)。
    try:
        await _probe_source(souce)
    except SourceUnavailable as first:
        log.warning('probe failed %s: %s', url, first)
        # 失敗パターン別に 1 度だけリトライ:
        #  - Jellyfin: トークン失効の可能性 → 再認証して URL を作り直す
        #  - yt-dlp 系: stream URL の expire の可能性 → キャッシュを invalidate して再抽出
        #  - SUNO / ローカルファイル: 決定的なので retry しても同じ → そのまま raise
        if _direct_audio_url is not None and _is_jellyfin_url(url):
            new_auth = await _jellyfin_authenticate(force=True)
            jf_item_id = _jellyfin_item_id_from_url(url)
            if new_auth and jf_item_id:
                souce = _jellyfin_stream_url(jf_item_id, new_auth["access_token"])
                await _probe_source(souce)
            else:
                raise
        elif _direct_audio_url is None and _data is None and stream:
            _ytdl_cache_invalidate(url)
            data = await _extract_ytdl_cached(url, stream)
            if 'entries' in data:
                data = data['entries'][0]
            souce = str(data['url'])
            try:
                await _probe_source(souce)
            except SourceUnavailable:
                # 失敗と分かっている URL を 4 時間キャッシュに残さない
                _ytdl_cache_invalidate(url)
                raise
        else:
            raise
    return _make_audio_source(souce, option, ctx.guild.id, live=live)


class OriginalFFmpegOpusAudio(discord.FFmpegOpusAudio):
    def __init__(self,
                 source,
                 *,
                 bitrate=256,
                 codec=None,
                 executable='ffmpeg',
                 pipe=False,
                 stderr=None,
                 before_options=None,
                 options=None):
        self.total_milliseconds = 0
        self.source = source
        self._codec = 'copy' if codec in ('opus', 'libopus', 'copy') else 'libopus'
        self._bitrate = bitrate if bitrate is not None else 128

        super().__init__(source,
                         bitrate=bitrate,
                         codec=codec,
                         executable=executable,
                         pipe=pipe,
                         stderr=stderr,
                         before_options=before_options,
                         options=options)

    def wait_buffer(self):
        self._stdout.peek(OpusEncoder.FRAME_SIZE)

    def read(self):
        ret = super().read()
        if ret:
            self.total_milliseconds += 20
        return ret

    def cleanup(self):
        super().cleanup()

    def get_tootal_millisecond(self, seek_time):
        if seek_time:
            list = reversed([int(x) for x in seek_time.split(":")])
            total = 0
            for i, x in enumerate(list):
                total += x * 3600 if i == 2 else x * 60 if i == 1 else x
            return max(1000 * total, 0)
        else:
            raise Exception()

    def rewind(self,
               rewind_time,
               *,
               executable='ffmpeg',
               pipe=False,
               stderr=None,
               before_options=None,
               options=None):
        seek_time = str(
            int((self.total_milliseconds -
                 self.get_tootal_millisecond(rewind_time)) / 1000))

        self.seek(seek_time=seek_time,
                  executable=executable,
                  pipe=pipe,
                  stderr=stderr,
                  before_options=before_options,
                  options=options)

    def seek(self,
             seek_time,
             *,
             executable='ffmpeg',
             pipe=False,
             stderr=None,
             before_options=None,
             options=None):
        self.total_milliseconds = self.get_tootal_millisecond(seek_time)
        proc = self._process
        before_options = f"-ss {seek_time} " + before_options
        args = []
        subprocess_kwargs = {
            'stdin': self.source if pipe else subprocess.DEVNULL,
            'stderr': stderr
        }

        if isinstance(before_options, str):
            args.extend(shlex.split(before_options))

        args.append('-i')
        args.append('-' if pipe else self.source)
        # FFmpegOpusAudio.read() は OggStream 経由で Ogg/Opus パケットを
        # 読み出すため、ffmpeg 出力も Ogg/Opus 形式でなければならない。
        # PCM 形式 (-f s16le) では OggStream がパースできず即座に EOF となる。
        args.extend(('-map_metadata', '-1',
                     '-f', 'opus',
                     '-c:a', self._codec,
                     '-ar', '48000',
                     '-ac', '2',
                     '-b:a', f'{self._bitrate}k',
                     '-loglevel', 'warning'))

        if isinstance(options, str):
            args.extend(shlex.split(options))

        args.append('pipe:1')

        args = [executable, *args]
        kwargs = {'stdout': subprocess.PIPE}
        kwargs.update(subprocess_kwargs)

        self._process = self._spawn_process(args, **kwargs)
        self._stdout = self._process.stdout
        self._packet_iter = OggStream(self._stdout).iter_packets()
        self.kill(proc)

    def kill(self, proc):
        if proc is None:
            return

        log.info('Preparing to terminate ffmpeg process %s.', proc.pid)

        try:
            proc.kill()
        except Exception:
            log.exception(
                "Ignoring error attempting to kill ffmpeg process %s",
                proc.pid)

        if proc.poll() is None:
            log.info(
                'ffmpeg process %s has not terminated. Waiting to terminate...',
                proc.pid)
            proc.communicate()
            log.info(
                'ffmpeg process %s should have terminated with a return code of %s.',
                proc.pid, proc.returncode)
        else:
            log.info(
                'ffmpeg process %s successfully terminated with return code of %s.',
                proc.pid, proc.returncode)


class OriginalFFmpegPCMAudio(discord.FFmpegPCMAudio):
    # 20ms ぶんの無音 PCM (s16le stereo 48kHz)
    _SILENCE_FRAME = b"\x00" * OpusEncoder.FRAME_SIZE

    def __init__(self,
                 source,
                 *,
                 executable='ffmpeg',
                 pipe=False,
                 stderr=None,
                 before_options=None,
                 options=None,
                 guild_id: int | None = None,
                 live: bool = False):
        self.total_milliseconds = 0
        self.source = source
        self._guild_id = guild_id
        # シーク中に audio thread が空 bytes を読んで discord.py が
        # EOF と誤判定するのを防ぐためのカウンタ。残数ぶん無音を返す。
        self._seek_silence_frames = 0
        # 擬似 pause フラグ。True の間 read() は無音フレームを返し続ける
        # (vc.pause() で送出を止めると RTP timestamp が wall-clock からズレて
        # 累積遅延・操作遅延の原因になるため、送出は継続して無音を流す)。
        self._paused = False
        # 再生結果の判定用 (play_music が play_future 解決後に読む)。
        # super().__init__ 内で _spawn_process が呼ばれるのでそれより前に用意する。
        self.frames_read = 0        # ffmpeg から実際に読めたフレーム数 (シークでリセットしない)
        self.end_reason = None      # read() が EOF を返したら 'eof'
        self.eof_returncode = None
        self.eof_stderr = ''
        self.is_live = live
        # _STARTED_FRAMES に届いた瞬間に audio thread から 1 回だけ呼ばれる
        self._on_started = None
        # シークで ffmpeg を起動し直すときも構築時と同じ引数を使う
        # (以前は呼び出し側の素の ffmpeg_options を使っていたため loudnorm が外れていた)。
        self._executable = executable
        self._pipe = pipe
        self._before_options = before_options or ''
        self._options = options or ''
        # seek() のプロセス差し替えと、audio thread 側の EOF 確定 / cleanup() を直列化する。
        # (これが無いと曲の終わりや停止とシークが数 ms 差で重なったときに ffmpeg が取り残される)
        self._proc_lock = threading.Lock()

        # stderr は _spawn_process で自前の drain スレッドに渡すので discord.py には渡さない
        # (discord.py の _pipe_reader は EOF で空回りし、シーク後は死んだ pipe を読み続ける)。
        super().__init__(source=source,
                         executable=executable,
                         pipe=pipe,
                         stderr=None,
                         before_options=before_options,
                         options=options)

    @property
    def normalize_enabled(self) -> bool:
        return _LOUDNORM_AF in self._options

    def _spawn_process(self, args, **kwargs):
        # discord.py 側の self._stderr は None のままなので、あちらは stderr を読まない
        kwargs['stderr'] = subprocess.PIPE
        proc = super()._spawn_process(args, **kwargs)
        try:
            tail, lock = collections.deque(maxlen=30), threading.Lock()
            t = threading.Thread(
                target=_drain_ffmpeg_stderr,
                args=(proc.stderr, tail, lock, proc.pid, getattr(self, '_guild_id', None)),
                daemon=True, name=f'ffmpeg-stderr:{proc.pid}')
            t.start()
        except Exception:
            _reap_ffmpeg(proc)  # 読み手のいない PIPE を残さない
            raise
        proc._sm_tail, proc._sm_tail_lock, proc._sm_drain = tail, lock, t
        return proc

    def _kill_process(self):
        # FFmpegAudio.cleanup() から呼ばれる。communicate() は drain スレッドと
        # stderr を取り合うので使わない。
        proc = getattr(self, '_process', MISSING)
        if proc is not MISSING and proc is not None:
            _reap_ffmpeg(proc)

    def _build_args(self, before_options: str, options: str) -> list:
        args = [self._executable, *shlex.split(before_options),
                '-i', '-' if self._pipe else self.source,
                '-f', 's16le', '-ar', '48000', '-ac', '2', '-loglevel', 'warning']
        args.extend(shlex.split(options))
        args.append('pipe:1')
        return args

    def wait_buffer(self):
        self._stdout.peek(OpusEncoder.FRAME_SIZE)

    def read(self):
        # 擬似 pause 中: 送出を止めず無音を流し続ける (RTP timestamp を wall-clock に
        # 追随させ、受信側の遅延累積を防ぐ)。位置は進めず visualizer にも流さない。
        if self._paused:
            return self._SILENCE_FRAME
        # シーク中は ffmpeg プロセス差し替え中に古い pipe から空 bytes を読んで
        # しまうことがあるので、しばらく無音フレームでつなぐ。位置 (total_milliseconds)
        # は seek() で既にシーク先に設定済みなので、無音区間では進めない。
        if self._seek_silence_frames > 0:
            self._seek_silence_frames -= 1
            return self._SILENCE_FRAME
        # seek() は event loop 側で _process を差し替えるので、(process, pipe) を
        # 1 組だけ取って以降はそれを使う。
        proc = self._process
        if proc is MISSING or proc is None:
            return b''
        out = proc.stdout
        try:
            ret = out.read(OpusEncoder.FRAME_SIZE)
        except (ValueError, OSError):
            ret = b''
        if len(ret) == OpusEncoder.FRAME_SIZE:
            self.frames_read += 1
            self.total_milliseconds += 20
            # visualizer 用に最新フレームを deque へ append (audio thread から)
            if self._guild_id is not None:
                _push_pcm_frame(self._guild_id, ret)
            if self.frames_read == _STARTED_FRAMES:
                cb, self._on_started = self._on_started, None
                if cb is not None:
                    try:
                        cb()
                    except Exception:
                        # read() から例外が漏れると再生が止まるので握る
                        log.exception('on_started callback failed')
            return ret
        # 読んでいた pipe がシークで差し替えられて kill された → EOF ではない
        if proc is not self._process:
            return self._SILENCE_FRAME
        # 終了コードは wait() で取る (poll() だと EOF 直後はまだ None のことがある)
        try:
            rc = proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            rc = None
        if proc is not self._process:
            return self._SILENCE_FRAME
        drain = getattr(proc, '_sm_drain', None)
        if drain is not None:
            drain.join(timeout=0.5)
        tail_lock = getattr(proc, '_sm_tail_lock', None)
        if tail_lock is not None:
            with tail_lock:
                self.eof_stderr = '\n'.join(proc._sm_tail)[-2000:]
        with self._proc_lock:
            # 直前にシークで差し替わっていたら、新しいプロセスで再生を続ける
            if proc is not self._process:
                return self._SILENCE_FRAME
            self.eof_returncode = rc
            self.end_reason = 'eof'
        if rc not in (0, None) and self._current_error is None:
            # AudioPlayer がこれを after(error) に渡す
            self._current_error = discord.errors.FFmpegProcessError(
                f'FFmpeg exited with code {rc}: {self.eof_stderr[-300:]}')
        return b''

    def cleanup(self):
        # 再生終了時に古いフレームと audio 解析状態を消しておく
        # (インタプリタ終了時の __del__ では関数が既に None のことがあるので握る)
        if self._guild_id is not None:
            try:
                _reset_audio_features_state(self._guild_id)
            except Exception:
                pass
        # 先に _stopped を立てる。以降の seek() は差し替えずに PlayerFinished になり、
        # それより前に差し替わっていれば super().cleanup() が新しいプロセスを kill する。
        lock = getattr(self, '_proc_lock', None)  # __init__ 途中の __del__ でも動くように
        if lock is not None:
            with lock:
                self._stopped = True
        super().cleanup()

    def get_tootal_millisecond(self, seek_time):
        if seek_time:
            list = reversed([int(x) for x in seek_time.split(":")])
            total = 0
            for i, x in enumerate(list):
                total += x * 3600 if i == 2 else x * 60 if i == 1 else x
            return max(1000 * total, 0)
        else:
            raise Exception()

    def rewind(self, rewind_time, **_legacy):
        new_ms = max(0, self.total_milliseconds - self.get_tootal_millisecond(rewind_time))
        self._respawn(new_ms, self._options)

    def seek(self, seek_time, **_legacy):
        # _legacy: 呼び出し側の ffmpeg_options / ffmpeg_stream_options は意図的に無視する。
        # 構築時の options (-af loudnorm、http なら -reconnect、ローカルファイルなら無し)
        # を使い回すので、シークしてもノーマライズが外れない。
        # 不正な形式は get_tootal_millisecond で例外になる (状態を変える前)。
        self._respawn(self.get_tootal_millisecond(seek_time), self._options)

    def _finished(self) -> bool:
        return (getattr(self, '_stopped', False) or self._process is MISSING
                or self.end_reason == 'eof')

    def _respawn(self, new_ms: int, options: str) -> None:
        """new_ms の位置から options で ffmpeg を起動し直して差し替える。"""
        if self._finished():
            # 終わったプレイヤーに ffmpeg を起動すると誰も読まない孤児プロセスが残る
            raise PlayerFinished()
        # 利用者の入力をそのまま argv に入れず、数値だけの秒数 (ms 精度) を渡す
        ss = f"{new_ms // 1000}.{new_ms % 1000:03d}"
        new = self._spawn_process(
            self._build_args(f"-ss {ss} {self._before_options}", options),
            stdout=subprocess.PIPE,
            stdin=(self.source if self._pipe else subprocess.DEVNULL))
        # 起動できてから差し替える (失敗したら古いプロセスが同じ位置・同じ設定で鳴り続ける)。
        # 起動している間に曲が終わった / 止められた場合は新しい方を捨てる。
        with self._proc_lock:
            finished = self._finished()
            if not finished:
                # 差し替え直後の ~200ms は無音でつなぎ、古い pipe の EOF を拾わないようにする
                self._seek_silence_frames = 10
                self.total_milliseconds = new_ms
                self._options = options
                old, self._process = self._process, new
                self._stdout = new.stdout  # wait_buffer() が使う
        if finished:
            _reap_ffmpeg(new)
            raise PlayerFinished()
        _reap_ffmpeg(old, timeout=1.0)

    def set_normalize(self, enabled: bool, *, restart: bool) -> bool:
        """loudnorm を着脱する。変わったら True。
        restart=True なら今の位置から ffmpeg を起動し直して即反映する (起動に失敗したら
        設定も元のまま)。False なら次に ffmpeg を起動するとき (再生開始前など) に効く。"""
        if bool(enabled) == self.normalize_enabled:
            return False
        if enabled:
            new_opts = f"{self._options} {_LOUDNORM_AF}".strip()
        else:
            new_opts = ' '.join(self._options.replace(_LOUDNORM_AF, '').split())
        if restart:
            self._respawn(self.total_milliseconds, new_opts)
        else:
            self._options = new_opts
        return True




class perpetualTimer():
    def __init__(self, t, hFunction, *args):
        self.t = t
        self.args = args
        self.hFunction = hFunction
        self.thread = Timer(self.t, self.handle_function)

    def handle_function(self):
        self.hFunction(*self.args)
        self.thread = Timer(self.t, self.handle_function)
        self.thread.start()

    def start(self):
        self.thread.start()

    def cancel(self):
        self.thread.cancel()


@_db_locked
def get_volume_sql(key):
    with conn.cursor() as cur:
        cur.execute(f'SELECT id, volume FROM {table_name} WHERE id=%s',
                    (key, ))
        d = cur.fetchone()
        return d[1] * defalut_volume if d and d[1] else defalut_volume


@_db_locked
def get_stream_sql(key):
    with conn.cursor() as cur:
        cur.execute(f'SELECT id, stream FROM {table_name} WHERE id=%s',
                    (key, ))
        d = cur.fetchone()
        return d[1] if d is not None else defalut_stream


@_db_locked
def get_user_history_count_sql(userid, guild):
    # BC-DB-09: 値は %s でバインド (f-string 直挿入の SQLi 潜在負債を排除)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM {histry_table_name} WHERE userid=%s AND guild=%s",
            (str(userid), str(guild)))
        d = cur.fetchone()
        return d if d is not None else None


@_db_locked
def get_user_history_sql(userid, guild, page: int):
    # BC-DB-09: %s バインド + ORDER BY datetime DESC + page を非負に正規化
    offset = max(0, int(page)) * 25
    with conn.cursor() as cur:
        # SELECT * は列追加で呼び出し側のアンパックが壊れるため明示指定する
        cur.execute(
            f"SELECT id, userid, guild, title, url, datetime, username "
            f"FROM {histry_table_name} WHERE userid=%s AND guild=%s "
            f"ORDER BY datetime DESC LIMIT 25 OFFSET %s",
            (str(userid), str(guild), offset))
        d = cur.fetchall()
        return d if d is not None else None


@_db_locked
def get_guild_history_count_sql(guild):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM {histry_table_name} WHERE guild=%s",
            (str(guild),))
        d = cur.fetchone()
        return d if d is not None else None


@_db_locked
def get_guild_history_sql(guild: str, page: int):
    offset = max(0, int(page)) * 25
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id, userid, guild, title, url, datetime, username "
            f"FROM {histry_table_name} WHERE guild=%s "
            f"ORDER BY datetime DESC LIMIT 25 OFFSET %s",
            (str(guild), offset))
        d = cur.fetchall()
        return d if d is not None else None


@_db_locked
def set_history_sql(userid, guild, title, url, username=None):
    with conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO {histry_table_name} '
            f'(userid,guild,title,url,username) VALUES (%s,%s,%s,%s,%s)',
            (userid, guild, title, url, username))
    conn.commit()


@_db_locked
def set_prefix_sql(key, value):
    with conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO {table_name} (id, prefix) VALUES (%s,%s) ON CONFLICT (id) DO UPDATE SET prefix=EXCLUDED.prefix',
            (key, value))
    conn.commit()


@_db_locked
def set_volume_sql(key, value):
    with conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO {table_name} (id, volume) VALUES (%s,%s) ON CONFLICT (id) DO UPDATE SET volume=EXCLUDED.volume',
            (key, value))
    conn.commit()


@_db_locked
def set_stream_sql(key, value):
    with conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO {table_name} (id, stream) VALUES (%s,%s) ON CONFLICT (id) DO UPDATE SET stream=EXCLUDED.stream',
            (key, value))
    conn.commit()


@_db_locked
def delete_setting_sql(key):
    with conn.cursor() as cur:
        cur.execute(f'DELETE FROM {table_name} WHERE id=%s', (key, ))
    conn.commit()


@_db_locked
def get_admin_db_stats_sql():
    """管理画面用の DB 統計。SELECT 1 の応答時間と主要テーブルの行数。"""
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        cur.execute(f"SELECT COUNT(*) FROM {histry_table_name}")
        history_rows = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM {playlist_store.PLAYLIST_TABLE}")
        playlist_rows = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM {playlist_store.TRACK_TABLE}")
        playlist_track_rows = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM {table_name}")
        guild_setting_rows = cur.fetchone()[0]
    return {
        "ok": True,
        "latency_ms": latency_ms,
        "history_rows": history_rows,
        "playlist_rows": playlist_rows,
        "playlist_track_rows": playlist_track_rows,
        "guild_setting_rows": guild_setting_rows,
    }


@_db_locked
def get_announce_channel_sql(key):
    """guild のお知らせチャンネル ID (str) を返す。未設定なら None。"""
    with conn.cursor() as cur:
        cur.execute(
            f'SELECT announce_channel FROM {table_name} WHERE id=%s', (str(key),))
        d = cur.fetchone()
        return d[0] if d else None


@_db_locked
def set_announce_channel_sql(key, value):
    """お知らせチャンネルを設定 (value=None で解除)。"""
    with conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO {table_name} (id, announce_channel) VALUES (%s,%s) '
            f'ON CONFLICT (id) DO UPDATE SET announce_channel=EXCLUDED.announce_channel',
            (str(key), value))
    conn.commit()


@_db_locked
def get_all_announce_channels_sql():
    """設定済みの全ギルド分を {guild_id(str): channel_id(str)} で一括取得。"""
    with conn.cursor() as cur:
        cur.execute(
            f'SELECT id, announce_channel FROM {table_name} '
            f'WHERE announce_channel IS NOT NULL')
        return dict(cur.fetchall())


@_db_locked
def insert_announcement_sql(title, body, target_kind, sent, skipped, failed,
                            results_json, image_filename=None):
    with conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO {announce_table_name} '
            '(title, body, target_kind, sent_count, skipped_count, failed_count, '
            'results, image_filename) '
            'VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id::text, created_at',
            (title, body, target_kind, sent, skipped, failed,
             results_json, image_filename))
        row = cur.fetchone()
    conn.commit()
    return {"id": row[0], "created_at": row[1].isoformat()}


@_db_locked
def upsert_discord_user_sql(userid, username, avatar=None):
    """ユーザーマスタを更新する。名前が空なら既存値を維持する。"""
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {playlist_store.USERS_TABLE} "
            f"(userid, username, avatar) VALUES (%s,%s,%s) "
            f"ON CONFLICT (userid) DO UPDATE SET "
            f"  username = COALESCE(EXCLUDED.username, {playlist_store.USERS_TABLE}.username), "
            f"  avatar   = COALESCE(EXCLUDED.avatar, {playlist_store.USERS_TABLE}.avatar), "
            f"  last_seen = current_timestamp",
            (str(userid), username or None, avatar or None))
    conn.commit()


@_db_locked
def upsert_discord_guild_sql(guildid, name, icon=None, member_count=None):
    """サーバーマスタを更新する。値が空なら既存値を維持する。"""
    t = playlist_store.GUILD_MASTER_TABLE
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {t} (guildid, name, icon, member_count) "
            f"VALUES (%s,%s,%s,%s) "
            f"ON CONFLICT (guildid) DO UPDATE SET "
            f"  name = COALESCE(EXCLUDED.name, {t}.name), "
            f"  icon = COALESCE(EXCLUDED.icon, {t}.icon), "
            f"  member_count = COALESCE(EXCLUDED.member_count, {t}.member_count), "
            f"  last_seen = current_timestamp",
            (str(guildid), name or None, icon or None, member_count))
    conn.commit()


@_db_locked
def get_all_known_guildids_sql():
    """各テーブルに散らばっている guild id を全部集める (バックフィル用)。"""
    ps = playlist_store
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT guild FROM {histry_table_name} "
            f"UNION SELECT id FROM {ps.GUILDS_TABLE} "
            f"UNION SELECT guildid FROM {ps.LIBRARY_TABLE}")
        return [r[0] for r in cur.fetchall() if r[0]]


@_db_locked
def get_all_known_userids_sql():
    """各テーブルに散らばっている userid を全部集める (バックフィル用)。"""
    ps = playlist_store
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT userid FROM {histry_table_name} "
            f"UNION SELECT userid FROM {ps.PLAYLIST_TABLE} "
            f"UNION SELECT userid FROM {ps.PREF_TABLE} "
            f"UNION SELECT added_by_userid FROM {ps.LIBRARY_TABLE}")
        return [r[0] for r in cur.fetchall() if r[0]]


@_db_locked
def get_history_userids_missing_name_sql(force=False):
    """バックフィル対象のユニーク userid を返す。
    force=True なら既に名前が入っている行も対象にする (名前の方針変更時用)。"""
    with conn.cursor() as cur:
        if force:
            cur.execute(f"SELECT DISTINCT userid FROM {histry_table_name}")
        else:
            cur.execute(
                f"SELECT DISTINCT userid FROM {histry_table_name} "
                f"WHERE username IS NULL")
        return [r[0] for r in cur.fetchall()]


@_db_locked
def backfill_history_username_sql(userid, username, force=False):
    """該当ユーザーの username を埋める。更新行数を返す。
    force=False なら未設定行のみ、True なら値が違う行も上書きする。"""
    with conn.cursor() as cur:
        if force:
            cur.execute(
                f"UPDATE {histry_table_name} SET username=%s "
                f"WHERE userid=%s AND (username IS NULL OR username <> %s)",
                (username, str(userid), username))
        else:
            cur.execute(
                f"UPDATE {histry_table_name} SET username=%s "
                f"WHERE userid=%s AND username IS NULL",
                (username, str(userid)))
        n = cur.rowcount
    conn.commit()
    return n


@_db_locked
def get_admin_guild_history_sql(guild, page):
    """管理画面用: ギルドの再生履歴 1 ページ (25件) + 総件数。"""
    offset = max(0, int(page)) * 25
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM {histry_table_name} WHERE guild=%s",
            (str(guild),))
        total = cur.fetchone()[0]
        cur.execute(
            f"SELECT userid, title, url, datetime, username FROM {histry_table_name} "
            f"WHERE guild=%s ORDER BY datetime DESC LIMIT 25 OFFSET %s",
            (str(guild), offset))
        rows = cur.fetchall()
    return {
        "total": int(total),
        "items": [
            {"userid": r[0], "title": r[1], "url": r[2],
             "played_at": r[3].isoformat() if r[3] else None,
             "saved_username": r[4]}
            for r in rows
        ],
    }


@_db_locked
def list_announcements_sql(limit):
    with conn.cursor() as cur:
        cur.execute(
            'SELECT id::text, title, body, target_kind, sent_count, skipped_count, '
            f'failed_count, results, created_at, image_filename FROM {announce_table_name} '
            'ORDER BY created_at DESC LIMIT %s', (int(limit),))
        return [
            {"id": r[0], "title": r[1], "body": r[2], "target_kind": r[3],
             "sent_count": r[4], "skipped_count": r[5], "failed_count": r[6],
             "results": r[7], "created_at": r[8].isoformat(),
             "image_filename": r[9]}
            for r in cur.fetchall()
        ]


def get_timestr(t):
    d = t.day - 1
    if d != 0:
        return f"{d}days " + t.strftime('%H:%M:%S')
    if t.hour != 0:
        return t.strftime('%H:%M:%S')
    else:
        return t.strftime('%M:%S')


@tree.command(
    name="join",
    description="ボイスチャンネルに参加させます。"
)
async def command_join(ctx: discord.Interaction):
    if ctx.user.voice is None:
        await embed_response(ctx, "失敗", "ユーザーがボイスチャンネルに参加していません。", 255, 0, 0, True)
        return
    if ctx.guild.voice_client is not None:
        await embed_response(ctx, "失敗", "接続済みです。", 255, 0, 0, True)
        return
    await ctx.user.voice.channel.connect()
    await embed_response(ctx, "成功", "ボイスチャンネルに接続します。", 0, 255, 0, False)


async def join(ctx: discord.Interaction):
    if ctx.user.voice is None:
        embed = discord.Embed(title="失敗", description="ユーザーがボイスチャンネルに参加していません。",
                              colour=discord.Colour.from_rgb(255, 0, 0))
        await ctx.response.send_message(embed=embed, ephemeral=True)
    await ctx.user.voice.channel.connect()


@tree.command(
    name="leave",
    description="ボイスチャンネルから退出させます。"
)
async def leave(ctx: discord.Interaction):
    if ctx.user.voice is None:
        await embed_response(ctx, "失敗", "ユーザーが接続していません。", 255, 0, 0, True)
        return
    if ctx.guild.voice_client is None:
        await embed_response(ctx, "失敗", "BOTが接続していません。", 255, 0, 0, True)
        return
    guild_table.pop(ctx.guild.id, None)
    await ctx.guild.voice_client.disconnect()
    if ctx.guild.voice_client is not None:
        await embed_response(ctx, "失敗", "切断に失敗しました。しばらくしてもう一度試すか開発者にお問い合わせください。", 255, 0, 0, True)
    else:
        await _publish_stopped(ctx.guild.id)
        await embed_response(ctx, "成功", "切断しました。", 0, 255, 0, False)


# guild_id -> 直近トラックの送出が終わった monotonic 時刻 (RTP timestamp 再アンカー用)
_last_audio_end_ts: dict[int, float] = {}


# ユーザーマスタを二重更新しないためのプロセス内キャッシュ。
# (再起動時に再度 upsert されるので、名前の変更もいずれ反映される)
_touched_users: set = set()


def _touch_user(user_id, username=None, avatar=None) -> None:
    """ユーザーマスタ (discord_users) を更新する。背景実行・初回のみ。

    userid しか持たないテーブル (my_playlists / user_prefs 等) から
    名前を引けるようにするためのマスタ。名前が取れないときは登録しない
    (空行を作っても JOIN の役に立たないため)。
    """
    uid = str(user_id or "")
    if not uid or not username or uid in _touched_users:
        return
    _touched_users.add(uid)
    try:
        _spawn_db_write(upsert_discord_user_sql, uid, username, avatar)
    except Exception:
        log.exception("user master upsert failed")


def _touch_guild(guild) -> None:
    """サーバーマスタ (discord_guilds) を更新する。背景実行。

    guilds{suffix} は設定を変更した guild しか行が無いので、
    名前を引く用途のマスタを別に持つ。on_ready / on_guild_join から呼ぶ。
    """
    if guild is None:
        return
    try:
        icon = getattr(getattr(guild, "icon", None), "key", None)
        _spawn_db_write(
            upsert_discord_guild_sql, str(guild.id), guild.name, icon,
            getattr(guild, "member_count", None))
    except Exception:
        log.exception("guild master upsert failed")


def _spawn_history(guild_id: int, info: dict | None) -> None:
    """再生された曲の履歴を 1 行書く (背景実行)。

    _finish_playback が「実音声が 1 秒 (_STARTED_FRAMES) 出た」時点でだけ呼ぶ。
    ロード失敗・ffmpeg の即死・1 秒以内の停止では呼ばれないので、ここに来た曲は
    必ず鳴っている。キュー / プレイリストの両モードでこの 1 箇所に集約している。
    """
    if not info:
        return
    try:
        uid = info.get("userid")
        url = (info.get("url") or "").strip()
        if not uid or not url:
            log.info("history skipped (uid=%s has_url=%s)", uid, bool(url))
            return
        username = info.get("username") or None
        # BC-PERF-08: 履歴書き込みを待たずに背景実行 (再生ループを止めない)
        _spawn_db_write(
            set_history_sql, str(uid), str(guild_id),
            _normalize_media_title(info.get("title"), None, url),
            _sanitize_history_url(url),
            username,
        )
        _touch_user(uid, username)
    except Exception:
        log.exception("history write failed")


def _history_info_from_queue_item(current_info: dict) -> dict | None:
    """キューアイテムから履歴用の情報を作る。
    author (Member) が取れない Activity 経由の追加でも author_id で残す。"""
    author = current_info.get("author")
    uid = getattr(author, "id", None) or current_info.get("author_id")
    if not uid:
        return None
    # DB に残すのはアカウント名 (サーバーごとのニックネームだと同じ人が
    # 別名で記録されるため)。表示時はサーバーの表示名を優先して解決する。
    name = None
    if author is not None:
        name = getattr(author, "global_name", None) or getattr(author, "name", None)
    name = name or current_info.get("author_name")
    return {"userid": uid, "title": current_info.get("title"),
            "url": current_info.get("url"), "username": name}


def _history_info_from_playlist(state: dict, track_info: dict) -> dict | None:
    """プレイリストのトラックから履歴用の情報を作る。
    userid は 再生開始者 -> プレイリスト所有者 の順 (どちらも無ければ記録しない)。"""
    uid = state.get("playlist_started_by") or state.get("playlist_owner_id")
    if not uid:
        return None
    name = None
    if uid == state.get("playlist_started_by"):
        name = state.get("playlist_started_by_name")
    return {"userid": uid, "title": track_info.get("title"),
            "url": track_info.get("url"), "username": name}


def awaitable_voice_client_play(func: discord.VoiceClient, player: OriginalFFmpegOpusAudio, loop):
    f = asyncio.Future()
    gid = getattr(getattr(func, "guild", None), "id", None)
    # トラック遷移ギャップの RTP timestamp 欠損補正:
    # discord.py の VoiceClient.timestamp は「送ったパケット数ぶん」しか進まないため、
    # 前トラック終了〜今回開始までの無送出時間 (from_url のロード等) ぶん timestamp が
    # wall-clock から遅れる。これが受信側 jitter buffer に累積し、操作遅延・音/表示ズレ・
    # (VC 再参加でのみ解消) の原因になる。ギャップぶん timestamp を前進させて再同期する。
    try:
        if gid is not None:
            last = _last_audio_end_ts.get(gid)
            if last is not None:
                gap = time.monotonic() - last
                if 0.05 < gap < 600:
                    # 48000 samples/sec。32bit でラップ (discord.py の checked_add 上限に合わせる)
                    func.timestamp = (func.timestamp + int(gap * 48000)) & 0xFFFFFFFF
    except Exception:
        log.exception("RTP timestamp re-anchor failed (guild=%s)", gid)

    def _set(e):
        # vc.stop() と自然終了の両方で after が呼ばれて二重 set_result が
        # 起きるケースを防ぐ。
        if gid is not None:
            _last_audio_end_ts[gid] = time.monotonic()
        if not f.done():
            f.set_result(e)
    def after(e):
        return loop.call_soon_threadsafe(_set, e)
    func.play(player, after=after, bitrate=256,
              signal_type='music')  # 実際に再生している場所
    return f


def _arm_started_signal(player) -> asyncio.Future:
    """player が実音声を _STARTED_FRAMES 読んだら解決する Future (audio thread → loop)。
    vc.play() より前に仕込むこと。"""
    loop = client.loop
    started = loop.create_future()

    def _mark():
        if not started.done():
            started.set_result(True)
    player._on_started = lambda: loop.call_soon_threadsafe(_mark)
    return started


async def _finish_playback(ctx, url, player, play_future, started, history, expected_ms=0) -> bool:
    """再生の終了を待って結果を判定する。戻り値 True = 再生不可 (呼び出し元でスキップ扱い)。

    履歴は実音声が 1 秒出た時点で書く (ffmpeg が即死した曲を「再生した」と残さない)。"""
    gid = ctx.guild.id
    await asyncio.wait((play_future, started), return_when=asyncio.FIRST_COMPLETED)
    if started.done() or player.frames_read >= _STARTED_FRAMES:
        _spawn_history(gid, history)
    err = await play_future
    outcome = _classify_playback_end(player, err, expected_ms)
    if outcome in ('early_fail', 'midtrack'):
        log.warning('playback %s guild=%s url=%s frames=%d pos_ms=%d rc=%s err=%r stderr=%s',
                    outcome, gid, url, player.frames_read, player.total_milliseconds,
                    player.eof_returncode, err, (player.eof_stderr or '')[-500:])
        # 失敗した stream URL をキャッシュから外し、次回は再抽出させる
        _ytdl_cache_invalidate(url)
        _forget_player(gid, player)
    st = guild_table.get(gid)
    if outcome != 'midtrack' and st is not None:
        # 「同じ曲が続けて途中で切れた」回数は連続したときだけ数える
        st.pop('_midtrack_url', None)
        st.pop('_midtrack_n', None)
    if outcome == 'early_fail':
        await _playback_error_notice(ctx, gid, f"{url}は再生不可のためスキップします。")
        return True
    if outcome == 'midtrack':
        # 1 曲ループ等で同じ曲が毎回途中で切れる場合に、通知を連投せず 3 回目で諦める
        rep = st is not None and st.get('_midtrack_url') == url
        n = (st.get('_midtrack_n', 0) + 1) if rep else 1
        if st is not None:
            st['_midtrack_url'], st['_midtrack_n'] = url, n
        if n >= 3:
            if st is not None:
                st.pop('_midtrack_url', None)
                st.pop('_midtrack_n', None)
            await _playback_error_notice(
                ctx, gid, f"{url}は再生が繰り返し途中で中断されるためスキップします。")
            return True
        if not rep:
            await _playback_error_notice(
                ctx, gid,
                f"{url}の再生が途中で中断されました ({_ms_to_seek_str(player.total_milliseconds)})。",
                level='info')
    elif outcome == 'ok' and player.frames_read == 0 and st is not None:
        # 何も鳴らずに終わった (曲の長さ以降へのシーク等)。1 曲ループで空回りしないよう
        # 再生ループの空回り検出に「始まらなかった」と数えさせる。
        st['_play_started_at'] = 0
    return False


def _cleanup_unplayed(player) -> None:
    """vc.play() に渡せなかったプレイヤーの ffmpeg を片付ける。"""
    if player is None:
        return
    try:
        player.cleanup()
    except Exception:
        pass


async def play_music(ctx: discord.Interaction, url, first_seek=None, normalize=None, history=None, expected_ms=0):
    """1 曲再生して終わるまで待つ。戻り値 True = 再生不可 (呼び出し元はスキップ扱いにする)。
    normalize は曲ごとの指定 (True/False、未指定は None)。未指定なら Activity のトグルに従う。
    expected_ms は曲の長さ (不明なら 0)。途中で切れたかの判定に使う。"""
    player = None
    play_started = False
    t_load = time.monotonic()
    try:
        stream = await _db(get_stream_sql, str(ctx.guild.id))
        player = await from_url(ctx, url, loop=client.loop, stream=stream,
                                normalize=_want_normalize(ctx.guild.id, normalize))
        if not expected_ms:
            # 長さ不明 (ネットラジオ等の終わりの無いストリーム) は -ss で途中から起動し直すと
            # その秒数ぶん読み捨てて無音になるので、ライブと同じくトグルの途中反映をしない
            player.is_live = True

        # from_url は数秒〜十数秒かかることがある。その間に BOT が VC から切断
        # されたり guild_table がクリアされたりするケースがあるので、ここで一旦
        # 状態をチェックして「もう再生できない」なら静かに抜ける。
        # (これがないと "Not connected to voice" や KeyError が traceback として
        # ログに残り、また 1 曲が "再生不可スキップ" 扱いになる)
        if ctx.guild.id not in guild_table:
            log.info("play_music: guild state cleared during load; abort %s", url)
            _cleanup_unplayed(player)
            return False
        vc = ctx.guild.voice_client
        if vc is None or not vc.is_connected():
            log.info("play_music: voice disconnected during load; abort %s", url)
            _cleanup_unplayed(player)
            return False

        guild_table[ctx.guild.id]["player"] = player

        # 鳴らす前に設定を合わせ直す (await を挟まない)。ロード中にトグルが押されていたら、
        # それはこの曲に向けた操作なので曲ごとの指定より優先する。
        st = guild_table[ctx.guild.id]
        if st.get("_normalize_toggled_at", 0) >= t_load:
            want = bool(st.get("normalize"))
        else:
            want = _want_normalize(ctx.guild.id, normalize)
        if player.set_normalize(want, restart=False) and not first_seek:
            player.seek(seek_time='0')
        if first_seek:
            # プレイヤーは自分の options (loudnorm 含む) で ffmpeg を起動し直す
            player.seek(seek_time=first_seek)
        started = _arm_started_signal(player)
        # vc.play() を先に走らせて is_playing=True を確定させてから publish_state する。
        # 順序を逆にすると Activity 側の再生ボタンが ▶ に張り付いたまま戻らない。
        play_future = awaitable_voice_client_play(
            vc, player, client.loop)
        play_started = True
        # 再生ループの空回り検出用 (ここまで来た = 何か鳴らし始めた)
        guild_table[ctx.guild.id]["_play_started_at"] = time.monotonic()
        # 再生が始まった = ロード完了。`_loading` フラグをここで落としておかないと
        # 後段 (一時停止など) で vc.is_playing()=False になった時に loading=true と
        # 誤判定される。
        guild_table[ctx.guild.id]["_loading"] = False
        await _publish_state(ctx.guild.id)
        # 再生開始直後に「次の曲」を裏で先抽出してキャッシュに入れる。
        # 次の skip / 自然遷移ですぐに play_music に渡せるようになる。
        try:
            _start_prefetch_next(ctx.guild.id)
        except Exception:
            log.exception('unexpected error')
        return await _finish_playback(
            ctx, url, player, play_future, started, history, expected_ms)
    except asyncio.CancelledError:
        # ライブラリ切替などでタスクキャンセルされたら静かに抜ける
        raise
    except discord.ClientException as e:
        # "Not connected to voice." 等。BOT が切断された後の race condition。
        # エラーログは出すが「再生不可」とは扱わずスキップなしで終了。
        log.warning("play_music: voice client gone during play() for %s (%s)", url, e)
        if not play_started:
            _cleanup_unplayed(player)
        return False
    except SourceUnavailable as e:
        log.warning("play_music: %s unavailable: %s", url, e)
        await _playback_error_notice(ctx, ctx.guild.id, f"{url}は再生不可のためスキップします。")
        return True
    except BaseException:
        log.exception('unexpected error')
        if not play_started:
            _cleanup_unplayed(player)
        await _playback_error_notice(ctx, ctx.guild.id, f"{url}は再生不可のためスキップします。")
        return True


async def play_live_music(ctx: discord.Interaction, url, first_seek=None, history=None):
    """ライブを再生して終わるまで待つ。戻り値 True = 再生不可。"""
    player = None
    play_started = False
    try:
        volume = await _db(get_volume_sql, str(ctx.guild.id))
        player = await from_url(ctx, url, loop=client.loop, stream=True, live=True)
        player.wait_buffer()
        guild_table[ctx.guild.id]["player"] = player

        if first_seek:
            player.seek(seek_time=first_seek)
        started = _arm_started_signal(player)
        play_future = awaitable_voice_client_play(
            ctx.guild.voice_client, player, client.loop)
        play_started = True
        guild_table[ctx.guild.id]["_play_started_at"] = time.monotonic()
        await _publish_state(ctx.guild.id)
        # ライブは終端が無いので長さによる途中切れ判定はしない (expected_ms=0)
        return await _finish_playback(
            ctx, url, player, play_future, started, history, 0)
    except asyncio.CancelledError:
        raise
    except SourceUnavailable as e:
        log.warning("play_live_music: %s unavailable: %s", url, e)
        await _playback_error_notice(ctx, ctx.guild.id, f"{url}は再生不可のためスキップします。")
        return True
    except BaseException:
        log.exception('unexpected error')
        if not play_started:
            _cleanup_unplayed(player)
        await _playback_error_notice(ctx, ctx.guild.id, f"{url}は再生不可のためスキップします。")
        return True


async def playlist_queue(ctx: discord.Interaction, movie_infos_list):
    if (not movie_infos_list):
        await embed_send(ctx, "失敗", "検索に失敗しました", 255, 0, 0, True)
        return
    if ctx.guild.voice_client is None:
        await join(ctx)

    state = _ensure_state(ctx.guild.id)
    for info in movie_infos_list:
        await movie_info_log(info)
        state["music_queue"].append(info)

    await list_show(ctx)
    await _publish_state(ctx.guild.id)

    if state["mode"] == "queue" and not state["_queue_loop_running"]:
        state["_queue_loop_running"] = True
        try:
            await _run_queue_playback_loop(ctx)
        finally:
            state["_queue_loop_running"] = False


async def play_live_queue(ctx: discord.Interaction, movie_infos):
    if (not movie_infos):
        await embed_send(ctx, "失敗", "検索に失敗しました。", 255, 0, 0, True)
        return
    if ctx.guild.voice_client is None:
        await join(ctx)

    start_index = len(guild_table.get(ctx.guild.id, {}).get('music_queue') or [])

    info = movie_infos[0]
    await movie_info_log(info)
    author = info["author"]
    movie_embed = discord.Embed()
    movie_embed.set_thumbnail(url=info["image_url"])
    title = info["title"]
    url = info["url"]
    t = info["time"]
    movie_embed.add_field(
        name="\u200b", value=f"[{title}]({url})", inline=False)
    movie_embed.add_field(name="再生時間", value=f"{get_timestr(t)}")
    movie_embed.add_field(name="キューの順番", value=f"{start_index + 1}")
    movie_embed.set_author(
        name=f"{author.display_name} added", icon_url=author.display_avatar)
    await embed_send(ctx, "", "", 0, 0, 0, False, movie_embed)

    state = _ensure_state(ctx.guild.id)
    state["music_queue"].extend(movie_infos)
    await _publish_state(ctx.guild.id)

    if state["mode"] == "queue" and not state["_queue_loop_running"]:
        state["_queue_loop_running"] = True
        try:
            await _run_live_queue_playback_loop(ctx)
        finally:
            state["_queue_loop_running"] = False


# BC-MAINT-02: 通常キューとライブキューの再生ループはモード判定・切断停止・空キュー処理・
# 履歴記録・loop pop&append までほぼ同一。差分 (どの play 関数を呼ぶか / _loading フラグ /
# 事前 publish / normalize 解決) だけを「1 トラック再生」コルーチンとして差し込み、
# 共通ループ _run_queue_playback_loop_common に集約する。
async def _play_one_live(ctx, data, current_info) -> bool:
    """ライブキュー: play_live_music を呼ぶだけ (_loading/事前 publish/normalize なし)。"""
    return await play_live_music(
        ctx, current_info.get('url'),
        first_seek=current_info.get('first_seek'),
        history=_history_info_from_queue_item(current_info),
    )


async def _play_one_normal(ctx, data, current_info) -> bool:
    """通常キュー: _loading を立てて事前 publish し、normalize を解決して play_music。"""
    data["_loading"] = True
    await _publish_state(ctx.guild.id)
    # ノーマライズは曲ごとの指定を渡し、Activity のトグル (guild 設定) との合成は
    # play_music 側 (_want_normalize) で再生直前に行う。以前は per-track キーが常に
    # 入っているせいで guild 設定が一度も参照されず、キューモードではトグルが効かなかった。
    try:
        return await play_music(
            ctx, current_info.get('url'),
            first_seek=current_info.get('first_seek'),
            normalize=current_info.get('normalize'),
            history=_history_info_from_queue_item(current_info),
            expected_ms=_expected_ms_from_queue_item(current_info),
        )
    finally:
        data["_loading"] = False


async def _run_queue_playback_loop_common(ctx, play_one) -> None:
    """キューモードの共通再生ループ。play_one(ctx, data, current_info) -> is_error を
    差し込んで通常/ライブを共通化する。ctx は real Interaction か _FakeCtx。"""
    fails = 0   # 連続で再生不可だった曲数
    quick = 0   # 連続で 1 秒未満に終わった回数 (エラー扱いにならない空回りの検出)
    while True:
        data = guild_table.get(ctx.guild.id, {})
        if data.get("mode") != "queue":
            return
        if not ctx.guild.voice_client:
            guild_table.pop(ctx.guild.id, None)
            await _publish_stopped(ctx.guild.id)
            try:
                await embed_async_send(ctx, "INFO", "再生を停止しました。", 0, 0, 255)
            except Exception:
                pass
            return
        if not data.get('music_queue'):
            await _publish_state(ctx.guild.id)
            # publish を待つ間に Activity から曲が追加されていたら、終わらずに続けて再生する
            # (ここで return すると _queue_loop_running が立ったままの間に追加された曲が
            #  誰にも再生開始されない)
            if not guild_table.get(ctx.guild.id, {}).get('music_queue'):
                return
            continue
        current_info = data['music_queue'][0]
        t0 = time.monotonic()
        is_error = await play_one(ctx, data, current_info)
        if is_error:
            data['music_queue'].pop(0)
            fails += 1
            if fails >= _MAX_CONSEC_FAILS:
                # 残りのキューは残す (▶ や次の /play で再開できる)。
                # _publish_stopped は bus と guild_table がずれるので使わない。
                await _playback_error_notice(
                    ctx, ctx.guild.id,
                    f"{fails}曲連続で再生に失敗したため、再生を停止しました。")
                await _publish_state(ctx.guild.id)
                return
            # 待っている間に失敗した曲が「再生中」に見えないよう先に反映する
            await _publish_state(ctx.guild.id)
            await asyncio.sleep(min(0.5 * fails, 2.0))
            continue
        fails = 0
        # VC 再接続中や "Already playing audio" などで、何も鳴らさずに即 False が返り続けると
        # ループ系モードで空回りするので、続いたら少し待つ (利用者のスキップは数えない)。
        started = guild_table.get(ctx.guild.id, {}).get("_play_started_at", 0) >= t0
        quick = quick + 1 if (not started and time.monotonic() - t0 < 1.0) else 0
        if quick >= 3:
            await asyncio.sleep(min(quick - 2, 5))
        # トラックを終えた直後にモードがキューでなければ pop しない。
        # (キュー→プレイリスト切替で vc.stop() された場合に、今再生してた曲を消さないため)
        # 履歴は play_music/play_live_music が実音声 1 秒の時点で記録済み。
        data_now = guild_table.get(ctx.guild.id, {})
        if data_now.get("mode") != "queue":
            return
        has_loop = data_now.get('has_loop')
        has_loop_queue = data_now.get('has_loop_queue')
        if not has_loop:
            x = data_now['music_queue'].pop(0)
            if has_loop_queue:
                data_now['music_queue'].append(x)


async def _run_live_queue_playback_loop(ctx) -> None:
    await _run_queue_playback_loop_common(ctx, _play_one_live)


async def play_queue(ctx: discord.Interaction, movie_infos):
    if (not movie_infos):
        await embed_send(ctx, "失敗", "検索に失敗しました。", 255, 0, 0, True)
        return
    if ctx.guild.voice_client is None:
        await join(ctx)

    start_index = len(guild_table.get(ctx.guild.id, {}).get('music_queue') or [])

    info = movie_infos[0]
    await movie_info_log(info)
    author = info["author"]
    movie_embed = discord.Embed()
    movie_embed.set_thumbnail(url=info["image_url"])
    infos_len = len(movie_infos)
    if infos_len <= 1:
        title = info["title"]
        url = info["url"]
        t = info["time"]
        movie_embed.add_field(
            name="\u200b", value=f"[{title}]({url})", inline=False)
        movie_embed.add_field(name="再生時間", value=f"{get_timestr(t)}")
        movie_embed.add_field(name="キューの順番", value=f"{start_index + 1}")
    else:
        for x in movie_infos[:min(3, infos_len - 1)]:
            title = x["title"]
            url = x["url"]
            movie_embed.add_field(
                name="\u200b", value=f"[{title}]({url})", inline=False)
        movie_embed.add_field(name="\u200b", value=f"・・・", inline=False)
        last_info = movie_infos[-1]
        title = last_info["title"]
        url = last_info["url"]
        movie_embed.add_field(
            name="\u200b", value=f"[{title}]({url})", inline=False)
        total_datetime = get_timestr(
            to_time(sum([to_total_second(x["time"]) for x in movie_infos])))
        movie_embed.add_field(name="再生時間", value=f"{total_datetime}")
        movie_embed.add_field(name="キューの順番", value=f"{
                              start_index + 1}...{start_index + infos_len}")
        movie_embed.add_field(name="曲数", value=f"{infos_len}")
    movie_embed.set_author(
        name=f"{author.display_name} added", icon_url=author.display_avatar)
    await embed_send(ctx, "", "", 0, 255, 0, False, movie_embed)

    state = _ensure_state(ctx.guild.id)
    state["music_queue"].extend(movie_infos)
    await _publish_state(ctx.guild.id)

    # キューモードで loop が回ってなければ開始する。
    # playlist モードのときは追加のみ (ループは起動しない)。
    if state["mode"] == "queue" and not state["_queue_loop_running"]:
        state["_queue_loop_running"] = True
        try:
            await _run_queue_playback_loop(ctx)
        finally:
            state["_queue_loop_running"] = False


async def _run_queue_playback_loop(ctx) -> None:
    """キューモードの再生ループ。ctx は real Interaction か _FakeCtx。"""
    await _run_queue_playback_loop_common(ctx, _play_one_normal)


async def movie_info_log(movie_info):
    print("サーバー名:"+str(movie_info["author"].guild))
    print("ユーザー名:"+str(movie_info["author"]))
    print("URL:"+movie_info["url"])
    print("タイトル:"+movie_info["title"])
    print("時刻:"+str(datetime.now()))


@tree.command(
    name="skip",
    description="現在再生中の楽曲をスキップします。"
)
async def commandStop(ctx: discord.Interaction):
    if ctx.guild.voice_client is None:
        await embed_response(ctx, "失敗", "BOTがボイスチャンネルに参加していません。", 255, 0, 0, True)
        return

    if not ctx.guild.voice_client.is_playing():
        await embed_response(ctx, "失敗", "再生していません。", 255, 0, 0, True)
        return

    ctx.guild.voice_client.stop()
    await embed_response(ctx, "成功", "スキップしました。", 0, 255, 0, False)


@tree.command(
    name="stop",
    description="現在の再生を停止します。"
)
async def stop(ctx):
    if ctx.guild.voice_client is None:
        await embed_response(ctx, "失敗", "接続していません。", 255, 0, 0, True)
        return

    if not ctx.guild.voice_client.is_playing():
        await embed_response(ctx, "失敗", "再生していません。", 255, 0, 0, True)
        return

    ctx.guild.voice_client.stop()
    await embed_response(ctx, "成功", "停止しました。", 0, 255, 0, False)


async def list_show(ctx: discord.Integration):
    queue = guild_table.get(ctx.guild.id, {}).get('music_queue')
    if queue:
        queue_embed = discord.Embed()
        queue_embed.set_thumbnail(url=queue[0]["image_url"])
        total_time = sum([to_total_second(x["time"]) for x in queue])
        for i, x in enumerate(queue):
            title = x["title"]
            url = x["url"]
            t = x["time"]
            name = "__Now Playing:__" if i == 0 else "__Up Next:__" if i == 1 else "__End Queue:__" if i + \
                1 == len(queue) else "\u200b"
            if i < 20 or i+1 == len(queue):
                queue_embed.add_field(
                    name=name,
                    value=f"`{i + 1}.`[{title}]({url})|`{get_timestr(t)} Requested by: {_requester_name(x)}`", inline=False)
        player = guild_table.get(ctx.guild.id, {}).get('player')
        queue_embed.add_field(
            name="\u200b", value=f"残り時間: `{get_timestr(to_time(total_time))}`")
        await embed_send(ctx, "", "", 0, 0, 0, False, queue_embed)


@tree.command(
    name="queue",
    description="現在追加されているキューを表示します。"
)
async def show_queue(ctx: discord.Interaction):
    await ctx.response.defer()
    if ctx.guild.voice_client is None:
        await embed_response(ctx, "失敗", "BOTはボイスチャンネルに接続していません。", 255, 0, 0, True)
        return
    queue = guild_table.get(ctx.guild.id, {}).get('music_queue')
    if queue:
        queue_embed = discord.Embed()
        queue_embed.set_thumbnail(url=queue[0]["image_url"])
        total_time = sum([to_total_second(x["time"]) for x in queue])
        for i, x in enumerate(queue):
            title = x["title"]
            url = x["url"]
            t = x["time"]
            name = "__Now Playing:__" if i == 0 else "__Up Next:__" if i == 1 else "__End Queue:__" if i + \
                1 == len(queue) else "\u200b"
            if i < 20 or i+1 == len(queue):
                queue_embed.add_field(
                    name=name,
                    value=f"`{i + 1}.`[{title}]({url})|`{get_timestr(t)} Requested by: {_requester_name(x)}`", inline=False)
        player = guild_table.get(ctx.guild.id, {}).get('player')
        # 再生失敗の直後などは player が None (_forget_player)
        current_total_time = _get_current_position_ms(player) // 1000
        total_time -= current_total_time
        queue_embed.add_field(
            name="\u200b", value=f"残り時間: `{get_timestr(to_time(total_time))}`")
        await embed_send(ctx, "", "", 0, 0, 0, False, queue_embed)

    else:
        await embed_send(ctx, "INFO", "キューは空です。", 0, 0, 255, False)


@tree.command(
    name="now",
    description="現在再生している情報を返します。"
)
async def show_now_playing(ctx: discord.Interaction):
    if ctx.guild.voice_client is None:
        await embed_response(ctx, "失敗", "BOTはボイスチャンネルに接続していません。", 255, 0, 0, True)
        return

    player = guild_table.get(ctx.guild.id, {}).get('player')
    queue = guild_table.get(ctx.guild.id, {}).get('music_queue')
    if player and queue:
        title = queue[0]["title"]
        url = queue[0]["url"]
        t = queue[0]["time"]
        author = queue[0].get("author")
        try:
            current_time = to_time(player.original.total_milliseconds / 1000)
        except Exception:
            current_time = to_time(player.total_milliseconds / 1000)
        current_time_str = get_timestr(current_time)
        end_time_str = get_timestr(t)
        movie_embed = discord.Embed()
        movie_embed.set_thumbnail(url=queue[0]["image_url"])
        movie_embed.add_field(name="\u200b",
                              value=f"[{title}]({url})",
                              inline=False)
        current_pos = int(
            to_total_second(current_time) / to_total_second(t) * 18)
        bar = ''
        for i in range(18):
            bar += '🔘' if current_pos == i else '▬'
        movie_embed.add_field(name="\u200b", value=bar, inline=False)
        movie_embed.add_field(name="\u200b",
                              value=f"`{current_time_str}/{end_time_str}`",
                              inline=False)
        movie_embed.set_author(name=f"{_requester_name(queue[0])} added",
                               icon_url=getattr(author, "display_avatar", None))
        if (url.startswith("https://www.nicovideo.jp/")):
            movie_embed.add_field(name="\u200b",
                                  value=",".join(
                                      [f"`[{tag}]`" for tag in get_tags(url)]),
                                  inline=False)
        await embed_response(ctx, "", "", 0, 0, 0, False, movie_embed)
    else:
        await embed_response(ctx, "INFO", "現在再生していません。", 0, 0, 255, False)


@tree.command(
    name="seek",
    description="	指定した時間まで曲をシークします。"
)
@discord.app_commands.describe(

    seek="再生開始時間を指定します。"
)
async def seek(ctx: discord.Interaction, seek: str):
    if ctx.guild.voice_client is None:
        await embed_response(ctx, "失敗", "接続していません。", 255, 0, 0, True)
        return
    player = guild_table.get(ctx.guild.id, {}).get('player')
    if not player:
        await embed_response(ctx, "失敗", "現在再生していません。", 255, 0, 0, True)
        return
    # ffmpeg プロセスの kill→spawn で 3 秒のインタラクション応答期限を
    # 超え得るので defer しておき、以降の応答は followup(embed_send)で返す。
    await ctx.response.defer(ephemeral=True)
    # vc.pause/resume は使わない (RTP timestamp 欠損→累積遅延の原因)。
    # player.seek が _seek_silence_frames で差し替え隙間を無音で埋める。
    # ffmpeg の引数 (loudnorm / -reconnect) はプレイヤーが構築時のものを使い回す。
    try:
        player.seek(seek_time=seek)
    except PlayerFinished:
        await embed_send(ctx, "失敗", "現在再生していません。", 255, 0, 0, True)
        return
    except Exception:
        await embed_send(ctx, "失敗", "無効な形式です。", 255, 0, 0, True)
        return
    await _publish_state(ctx.guild.id)
    await embed_send(ctx, "成功", "シークしました。", 0, 255, 0, True)


@tree.command(
    name="rewind",
    description="指定した時間分曲を戻します。"
)
@discord.app_commands.describe(
    rewind="指定した時間分曲を戻します。"
)
async def rewind(ctx: discord.Interaction, rewind: str):
    if ctx.guild.voice_client is None:
        await embed_response(ctx, "失敗", "接続していません。", 255, 0, 0, True)
        return
    player = guild_table.get(ctx.guild.id, {}).get('player')
    if not player:
        await embed_response(ctx, "失敗", "現在再生していません。", 255, 0, 0, True)
        return
    # /seek と同様、ffmpeg の巻き戻し処理で 3 秒を超え得るので defer する。
    await ctx.response.defer(ephemeral=True)
    # vc.pause/resume は使わない (RTP timestamp 欠損対策)。rewind が _seek_silence_frames で繋ぐ。
    try:
        player.rewind(rewind_time=rewind)
    except PlayerFinished:
        await embed_send(ctx, "失敗", "現在再生していません。", 255, 0, 0, True)
        return
    except Exception:
        await embed_send(ctx, "失敗", "無効な形式です。", 255, 0, 0, True)
        return
    await _publish_state(ctx.guild.id)
    await embed_send(ctx, "成功", "巻き戻しました。", 0, 255, 0, True)


@tree.command(
    name="loop",
    description="現在再生している曲をループします。"
)
async def loop(ctx: discord.Interaction):
    if ctx.guild.voice_client is None:
        await embed_response(ctx, "失敗", "接続していません。", 255, 0, 0, True)
        return

    data = guild_table.get(ctx.guild.id)
    if data:
        value = not data.get("has_loop")
        data["has_loop"] = value
        if (value):
            await embed_response(ctx, "成功", "ループが有効になりました。", 0, 255, 0, False)
        else:
            await embed_response(ctx, "成功", "ループが無効になりました。", 0, 255, 0, False)
    else:
        await embed_response(ctx, "失敗", "現在再生していません。", 255, 0, 0, True)


@tree.command(
    name="loopqueue",
    description="キューをループします。"
)
async def loopqueue(ctx: discord.Interaction):
    if ctx.guild.voice_client is None:
        await embed_response(ctx, "失敗", "接続していません。", 255, 0, 0, True)
        return

    data = guild_table.get(ctx.guild.id)
    if data:
        value = not data.get("has_loop_queue")
        data["has_loop_queue"] = value
        if (value):
            await embed_response(ctx, "成功", "キューループが有効になりました。", 0, 255, 0, False)
        else:
            await embed_response(ctx, "成功", "キューループが無効になりました。", 0, 255, 0, False)
    else:
        await embed_response(ctx, "失敗", "現在再生していません。", 255, 0, 0, True)


@tree.command(
    name="clear",
    description="キューを空にします。"
)
async def clear(ctx: discord.Interaction):
    if ctx.guild.voice_client is None:
        await embed_response(ctx, "失敗", "接続していません。", 255, 0, 0, True)
        return

    data = guild_table.get(ctx.guild.id)
    if data:
        data['music_queue'] = data['music_queue'][:1]
        await _publish_state(ctx.guild.id)
        await embed_response(ctx, "成功", "キューを空にしました。", 0, 255, 0, False)
    else:
        await embed_response(ctx, "情報", "キューは空です。", 0, 0, 255, False)


@tree.command(
    name="shuffle",
    description="キューをシャッフルします。"
)
async def shuffle(ctx: discord.Interaction):
    if ctx.guild.voice_client is None:
        await embed_response(ctx, "失敗", "接続していません。", 255, 0, 0, True)
        return
    data = guild_table.get(ctx.guild.id)
    if data:
        data['music_queue'] = data['music_queue'][:1] + random.sample(
            data['music_queue'][1:],
            len(data['music_queue']) - 1)
        await _publish_state(ctx.guild.id)
        await embed_response(ctx, "成功", "キューをシャッフルしました。", 0, 255, 0, False)
    else:
        await embed_response(ctx, "情報", "キューは空です。", 0, 0, 255, False)


@tree.command(
    name="skipto",
    description="指定された番号の曲までスキップします。"
)
@discord.app_commands.describe(
    index="指定する番号"
)
async def skipto(ctx: discord.Interaction, index: int):
    if ctx.guild.voice_client is None:
        await embed_response(ctx, "失敗", "接続していません。", 255, 0, 0, True)
        return

    data = guild_table.get(ctx.guild.id)
    if data:
        if index < 2 or index > len(data['music_queue']):
            await embed_response(ctx, "失敗", "キューの範囲外です。", 255, 0, 0, True)
            return
        data['music_queue'] = data['music_queue'][:1] + data['music_queue'][
            index - 1:]
        await _publish_state(ctx.guild.id)
        await stop(ctx)
        await embed_response(ctx, "成功", (f"キューを{index}番目まで飛ばしました。"), 0, 255, 0, False)
    else:
        await embed_response(ctx, "情報", "キューは空です。", 0, 0, 255, False)


@tree.command(
    name="remove",
    description="指定された番号の曲までスキップします。"
)
@discord.app_commands.describe(
    index="指定する番号"
)
async def remove(ctx: discord.Interaction, index: str):
    if ctx.guild.voice_client is None:
        await embed_response(ctx, "失敗", "接続していません。", 255, 0, 0, True)
        return

    data = guild_table.get(ctx.guild.id)
    if data:
        if index < 2 or index > len(data['music_queue']):
            await embed_response(ctx, "失敗", "キューの範囲外です。", 255, 0, 0, True)
            return
        data['music_queue'].pop(index - 1)
        await _publish_state(ctx.guild.id)
        await embed_response(ctx, "成功", f"キューの{index}番目を削除しました", 0, 255, 0, False)
    else:
        await embed_response(ctx, "情報", "キューは空です。", 0, 0, 255, False)


@tree.command(
    name="pause",
    description="現在再生中の楽曲を一時停止します。"
)
async def CommandPause(ctx: discord.Interaction):
    if ctx.guild.voice_client is None:
        await embed_response(ctx, "失敗", "BOTがボイスチャンネルに参加していません。", 255, 0, 0, True)
        return
    # 擬似 pause (player._paused) を使う。vc.pause() は RTP timestamp 欠損で累積遅延を招く。
    if (guild_table.get(ctx.guild.id) or {}).get("player") is None:
        await embed_response(ctx, "失敗", "再生していません。", 255, 0, 0, True)
        return

    if _player_is_paused(ctx.guild.id):
        _player_set_paused(ctx.guild.id, False)
        await _publish_state(ctx.guild.id)
        await embed_response(ctx, "成功", "再生を再開しました。", 0, 255, 0, False)
        return

    _player_set_paused(ctx.guild.id, True)
    await _publish_state(ctx.guild.id)

    await embed_response(ctx, "成功", "一時停止しました、resumeまたはpauseコマンドで解除できます。", 0, 255, 0, False)


async def pause(ctx: discord.Interaction):
    if ctx.guild.voice_client is None:
        await embed_response(ctx, "失敗", "接続していません。", 255, 0, 0, True)
        return
    if (guild_table.get(ctx.guild.id) or {}).get("player") is None:
        await embed_response(ctx, "失敗", "再生していません。", 255, 0, 0, True)
        return
    if _player_is_paused(ctx.guild.id):
        await resume(ctx)
        return
    _player_set_paused(ctx.guild.id, True)
    await _publish_state(ctx.guild.id)
    await embed_response(ctx, "成功", "一時停止しました、resumeまたはpauseコマンドで解除できます。", 0, 255, 0, False)


@tree.command(
    name="resume",
    description="現在一時停止中の楽曲を再生します。"
)
async def CommandResume(ctx: discord.Interaction):
    if ctx.guild.voice_client is None:
        await embed_response(ctx, "失敗", "BOTがボイスチャンネルに参加していません。", 255, 0, 0, True)
        return
    if not _player_is_paused(ctx.guild.id):
        await embed_response(ctx, "失敗", "一時停止していません。", 255, 0, 0, True)
        return
    _player_set_paused(ctx.guild.id, False)
    await _publish_state(ctx.guild.id)
    await embed_response(ctx, "成功", "再生を再開しました。", 0, 255, 0, False)


async def resume(ctx):
    if ctx.guild.voice_client is None:
        await embed_response(ctx, "失敗", "接続していません。", 255, 0, 0, True)
        return
    _player_set_paused(ctx.guild.id, False)
    await _publish_state(ctx.guild.id)
    await embed_response(ctx, "成功", "再生を再開しました。", 0, 255, 0, False)


@tree.command(
    name="list",
    description="指定されたYouTubeのリストの音楽をすべてキューに追加して再生します。"
)
@discord.app_commands.describe(
    args="YouTubeのリストIDを含むURL。",
    normalize="オンにすると音量をノーマライズ（均一化）します。",
)
@discord.app_commands.choices(
    normalize=[
        discord.app_commands.Choice(name="ON", value="True"),
        discord.app_commands.Choice(name="OFF", value="False")
    ]
)
async def playlist(ctx: discord.Interaction, args: str, normalize: str = None):
    add_infos = {}
    await ctx.response.defer()
    if ctx.user.voice is None:
        await embed_send(ctx, "失敗", "貴方はボイスチャンネルに接続していません", 255, 0, 0, True)
        return
    # ON / OFF / 未指定 (None = Activity のトグルに従う)
    normalize_flag = None if normalize is None else normalize == "True"
    if args.startswith("https://www.nicovideo.jp/"):
        await embed_send(ctx, "失敗", "このコマンドはniconicoに対応してません。", 255, 0, 0, True)
        return
    if re.match("https?://www.youtube.com.*", args) or re.match("https?://youtube.com.*", args):
        pattern = re.compile(r'(?<=list=)[^?&]+')
        listid_m = pattern.search(args)
        if not listid_m:
            await embed_send(ctx, "失敗", "URL に list= が含まれていません。", 255, 0, 0, True)
            return
        # 50 件バッチで全曲取得 (旧: 各 video を 1件ずつ videos.list → 50倍のクォータ)
        try:
            infos = await _yt_collect_playlist_infos(ctx, listid_m.group(), normalize_flag)
        except Exception:
            log.exception('unexpected error')
            await embed_send(ctx, "失敗", "データの分類に失敗しました。処理を中断します。", 255, 0, 0, True)
            return
        movie_infos_list = []
        for info in infos:
            info["author"] = ctx.user
            info.update(add_infos)
            movie_infos_list.append(info)
        await playlist_queue(ctx, movie_infos_list)
    elif re.match("https://open.spotify.com/album/.*", args):
        movie_infos_list = await infos_spotify_album(args, normalize_flag)
        for info in movie_infos_list:
            info["author"] = ctx.author
            info.update(add_infos)
        await playlist_queue(ctx, movie_infos_list)
    elif re.match("https://open.spotify.com/playlist/.*", args):
        movie_infos_list = await infos_spotify_playlist(args, normalize_flag)
        for info in movie_infos_list:
            info["author"] = ctx.author
            info.update(add_infos)
        await playlist_queue(ctx, movie_infos_list)


@tree.command(
    name="live",
    description="指定されたURLのライブを再生します。"
)
@discord.app_commands.describe(
    args="再生するソースの参照先を指定します。",
)
async def live(ctx: discord.Interaction, args: str):
    await ctx.response.defer()
    add_infos = {}
    if ctx.user.voice is None:
        await embed_send(ctx, "失敗", "貴方はボイスチャンネルに接続していません", 255, 0, 0, True)
        return
    if args.startswith("https://www.nicovideo.jp/"):
        await ctx.channel.send("このコマンドはniconicoに対応してません。")
        return
    if re.match("https?://www.youtube.com.*", args) or re.match("https?://youtube.com.*", args):
        try:
            pattern = re.compile(r'(?<=v=)[^?]*')
            liveid = pattern.search(args)
            movie_infos = await live_infos_youtube_api(liveid.group())
            for info in movie_infos:
                info["author"] = ctx.user
                info.update(add_infos)
        except Exception:
            log.exception('unexpected error')
            await embed_send(ctx, "失敗", "検索に失敗しました。", 255, 0, 0, True)
            return
    else:
        movie_infos = await infos_from_ytdl(args)
        if movie_infos is False:
            await embed_send(ctx, "失敗", "現在LIVE中ではないようです。再生に失敗しました。", 255, 0, 0, True)
            return
        for infos in movie_infos:
            infos["author"] = ctx.user
            infos.update(add_infos)
    await play_live_queue(ctx, movie_infos)


@tree.command(
    name="histry",
    description="このサーバーでの再生履歴を返します。"
)
@discord.app_commands.describe(
    page="表示するページ数を指定します。(1ページ25件)",
)
async def histry_guild(ctx: discord.Interaction, page: int):
    await ctx.response.defer()
    historys = await _db(get_guild_history_sql, ctx.guild.id, page)
    if historys == None:
        await embed_send(ctx, "INFO", "履歴はありません。", 0, 0, 255, False)
    else:
        count = await _db(get_guild_history_count_sql, ctx.guild.id)
        historys_embed = discord.Embed()
        historys_embed.set_footer(
            text=f"`{page+1}/{math.ceil(count[0]/25)}`総数: `{count[0]}`件")
        for i, history in enumerate(historys):
            id, userid, guild, title, url, datetime_value, username = history
            # 表示はこのサーバーでの表示名 (ニックネーム) を優先し、
            # 退室済みなら DB のアカウント名 -> ID の順でフォールバック
            who = (_resolve_display_name(ctx.guild, userid)
                   or username or f"ID:{userid}")
            historys_embed.add_field(
                name=f"__History__\n`No:{
                    page*25+i+1} 日付:{datetime_value}`" if i == 0 else f"`No:{page*25+i+1} 日付:{datetime_value}`",
                value=f"[{title}]({url})\n`{who}`",
                inline=False)
        await embed_send(ctx, "", "", 0, 0, 0, False, historys_embed)


@tree.command(
    name="histry_user",
    description="貴方のこのサーバーでの再生履歴を返します。"
)
@discord.app_commands.describe(
    page="表示するページ数を指定します。(1ページ25件)",
)
async def histry_user(ctx: discord.Interaction, page: int):
    await ctx.response.defer()
    historys = await _db(get_user_history_sql, ctx.user.id, ctx.guild.id, page)
    if historys == None:
        await embed_send(ctx, "INFO", "履歴はありません。", 0, 0, 255, False)
    else:
        count = await _db(get_user_history_count_sql, ctx.user.id, ctx.guild.id)
        historys_embed = discord.Embed()
        for i, history in enumerate(historys):
            # 本人の履歴なので username は表示しない (列は受け取るだけ)
            id, userid, guild, title, url, datetime_value, username = history
            historys_embed.add_field(
                name="__History__" if i == 0 else "",
                value=f"`{i + 1}.`[{title}]({url})'{datetime_value}'",
                inline=False)
        historys_embed.add_field(
            name="\u200b", value=f"総数: `{count[0]}件`")
        await embed_send(ctx, "", "", 0, 0, 0, False, historys_embed)


@tree.command(
    name="play",
    description="指定されたURLで音楽を再生します。"
)
@discord.app_commands.describe(
    args="オプションを含む再生するソースの参照先を指定します。",
    normalize="オンにすると音量をノーマライズ（均一化）します。",
    seek="再生開始時間を指定します。"
)
@discord.app_commands.choices(
    normalize=[
        discord.app_commands.Choice(name="ON", value="True"),
        discord.app_commands.Choice(name="OFF", value="False")
    ]
)
async def play(ctx: discord.Interaction, args: str, normalize: str = None, seek: str = None):
    await ctx.response.defer()
    if ctx.user.voice is None:
        await embed_send(ctx, "失敗", "貴方はボイスチャンネルに接続していません", 255, 0, 0, True)
        return
    # ON / OFF / 未指定 (None = Activity のトグルに従う)
    normalize_flag = None if normalize is None else normalize == "True"
    if seek is None:
        add_infos = {}
    else:
        add_infos = {"first_seek": seek}
    args = re.split('[\u3000 \t]+', args)
    optionbases = [x for x in args if x.startswith('-')]
    args = [i for i in args if i not in optionbases]
    options = ''.join([x[1:] for x in optionbases])
    sort = next((x for x in ['h', 'f', 'm', 'n'] if x in options), 'v')

    slice_dict = {}
    if len(args) >= 3 and args[0].isdecimal() and args[1].isdecimal():
        slice_dict = {"start": int(args[0]) - 1, "stop": int(args[1])}
        del (args[0:2])
    elif len(args) >= 2 and args[1].isdecimal():
        slice_dict = {"start": int(args[0]) - 1, "stop": int(args[0])}
        del (args[0])

    keyword = ' '.join(args[0:])
    movie_infos = None
    # niconico もノーマライズ対応済み (キャッシュ再生・ダウンロード再生の両方で loudnorm を掛ける)
    if re.match("https://open.spotify.com/track/.*", args[0]):
        await ctx.channel.send("このコマンドはSpotifyに対応してません。")
        await embed_send(ctx, "失敗", "このコマンドはSpotifyに対応してません。", 255, 0, 0, True)
        return
    try:
        if re.match("https://open.spotify.com/track/.*", args[0]):
            movie_infos = await infos_spotify_track(args[0], normalize_flag)
        elif _is_jellyfin_url(args[0]):
            item_id = _jellyfin_item_id_from_url(args[0])
            if not item_id:
                await embed_send(ctx, "失敗", "Jellyfin の item ID を URL から取得できませんでした。", 255, 0, 0, True)
                return
            movie_infos = await infos_jellyfin_api(args[0], item_id, normalize_flag)
        elif re.match("https?://.*", args[0]):
            movie_infos = await infos_from_ytdl(args[0], client.loop, normalize_flag)
        elif "y" in options:
            movie_infos = await infos_from_ytdl(keyword, client.loop, normalize_flag)
        for info in movie_infos:
            info["author"] = ctx.user
            info.update(add_infos)
    except Exception:
        log.exception('unexpected error')
        await embed_send(ctx, "失敗", "検索に失敗しました。対応していないサイトの可能性があります。", 255, 0, 0, True)
        return
    await play_queue(ctx, movie_infos)


async def embed_send(ctx: discord.Interaction, title: str, description: str, r: int, g: int, b: int, ephemeral: bool, embed=None):
    if embed is not None:
        print("用意されたEmbed")
        await ctx.followup.send(embed=embed, ephemeral=ephemeral)
    else:
        embed = discord.Embed(
            title=title, description=description, colour=discord.Colour.from_rgb(r, g, b))
        await ctx.followup.send(embed=embed, ephemeral=ephemeral)


async def embed_response(ctx: discord.Interaction, title: str, description: str, r: int, g: int, b: int, ephemeral: bool, embed=None):
    if embed is not None:
        print("用意されたEmbed")
        await ctx.response.send_message(embed=embed, ephemeral=ephemeral)
    else:
        embed = discord.Embed(
            title=title, description=description, colour=discord.Colour.from_rgb(r, g, b))
        await ctx.response.send_message(embed=embed, ephemeral=ephemeral)


async def embed_async_send(ctx: discord.Interaction, title: str, description: str, r: int, g: int, b: int, embed=None):
    if embed is not None:
        print("用意されたEmbed")
        await ctx.channel.send(embed=embed)
    else:
        embed = discord.Embed(
            title=title, description=description, colour=discord.Colour.from_rgb(r, g, b))
        await ctx.channel.send(embed=embed)


async def set_prefix(ctx, key, value):
    try:
        await _db(set_prefix_sql, key, value)
        client_id = client.user.id
        bot_name = client.user.name
        await ctx.channel.send("prefixを変更しました。")
    except Exception:
        log.exception('unexpected error')
        await ctx.channel.send("prefixの変更に失敗しました")


@tree.command(
    name="set_volume",
    description="ボリュームを設定します。デフォルトは「1」です。(注意: 次の曲から適用されます)"
)
@discord.app_commands.describe(
    volume="ボリュームを設定します。",
)
async def set_volume(ctx, volume: float):
    try:
        await _db(set_volume_sql, ctx.guild.id, volume)
        await embed_response(ctx, "成功", "音量を変更しました。", 0, 255, 0, False)
    except Exception:
        log.exception('unexpected error')
        await embed_response(ctx, "失敗", "音量の変更に失敗しました", 255, 0, 0, False)


@tree.command(
    name="set_stream",
    description="ストリーム再生の有効無効を設定します。デフォルトは有効です。"
)
@discord.app_commands.choices(
    stream_mode=[
        discord.app_commands.Choice(name="ON", value="True"),
        discord.app_commands.Choice(name="OFF", value="False")
    ]
)
async def set_stream(ctx, stream_mode: str):
    try:
        if stream_mode == "True":
            await _db(set_stream_sql, ctx.guild.id, True)
            await ctx.channel.send("ストリーム再生を有効化しました。")
            await embed_response(ctx, "成功", "ストリーム再生を有効化しました。", 0, 255, 0, False)
        elif stream_mode == "False":
            await _db(set_stream_sql, ctx.guild.id, False)
            await ctx.channel.send("ストリーム再生を無効化しました。")
            await embed_response(ctx, "成功", "ストリーム再生を無効化しました。", 0, 255, 0, False)
    except Exception:
        log.exception('unexpected error')
        await embed_response(ctx, "失敗", "ストリーム再生の変更に失敗しました", 255, 0, 0, False)


@tree.command(
    name="info_stream",
    description="ストリーム再生の有効無効を確認します。"
)
async def info_stream(ctx):
    try:
        stream = await _db(get_stream_sql, str(ctx.guild.id))
        if stream:
            await embed_response(ctx, "成功", "ストリーム再生は有効です。", 0, 255, 0, False)
        else:
            await embed_response(ctx, "成功", "ストリーム再生は無効です。", 0, 255, 0, False)
    except Exception:
        log.exception('unexpected error')
        await embed_response(ctx, "失敗", "ストリーム再生の確認に失敗しました", 255, 0, 0, False)


@tree.command(
    name="set_notice_channel",
    description="BOTからのお知らせを受け取るチャンネルを設定します。(サーバー管理権限が必要)"
)
@discord.app_commands.describe(
    channel="お知らせを受け取るテキストチャンネル (省略時は現在の設定を表示)",
    reset="Trueで設定を解除します (以後はシステムチャンネルに届きます)",
)
@discord.app_commands.default_permissions(manage_guild=True)
@discord.app_commands.guild_only()
async def set_notice_channel(ctx: discord.Interaction, channel: discord.TextChannel = None, reset: bool = False):
    try:
        # default_permissions はサーバー側で上書きできるため、実権限も確認する
        if ctx.guild is None or not isinstance(ctx.user, discord.Member) \
                or not ctx.user.guild_permissions.manage_guild:
            await embed_response(ctx, "失敗", "この操作にはサーバー管理権限が必要です。", 255, 0, 0, True)
            return
        if reset:
            await _db(set_announce_channel_sql, ctx.guild.id, None)
            await embed_response(
                ctx, "成功",
                "お知らせチャンネルを解除しました。今後はシステムチャンネルに届きます。",
                0, 255, 0, True)
            await _publish_guild_settings(ctx.guild.id)
            return
        if channel is not None:
            if not channel.permissions_for(ctx.guild.me).send_messages:
                await embed_response(
                    ctx, "失敗",
                    f"BOTが {channel.mention} に送信できません。権限を確認してください。",
                    255, 0, 0, True)
                return
            await _db(set_announce_channel_sql, ctx.guild.id, str(channel.id))
            await embed_response(
                ctx, "成功",
                f"お知らせチャンネルを {channel.mention} に設定しました。",
                0, 255, 0, True)
            await _publish_guild_settings(ctx.guild.id)
            return
        # 引数なし: 現在の設定を表示
        cid = await _db(get_announce_channel_sql, ctx.guild.id)
        ch = ctx.guild.get_channel(int(cid)) if cid and str(cid).isdigit() else None
        if isinstance(ch, discord.TextChannel):
            await embed_response(
                ctx, "成功", f"現在のお知らせチャンネル: {ch.mention}", 0, 255, 0, True)
        else:
            sc = ctx.guild.system_channel
            dest = f"システムチャンネル ({sc.mention})" if sc \
                else "システムチャンネル (未設定のため届きません)"
            await embed_response(
                ctx, "成功",
                f"お知らせチャンネルは未設定です。{dest} に届きます。",
                0, 255, 0, True)
    except Exception:
        log.exception('unexpected error')
        try:
            await embed_response(ctx, "失敗", "お知らせチャンネルの設定に失敗しました。", 255, 0, 0, True)
        except Exception:
            pass


@tree.command(
    name="delete_setting",
    description="全ての設定を削除してデフォルトにします。"
)
async def delete_setting(ctx: discord.Interaction):
    try:
        await _db(delete_setting_sql, ctx.guild.id)
        client_id = client.user.id
        bot_name = client.user.name
        await ctx.channel.send("全ての設定を削除しました。")
    except Exception:
        log.exception('unexpected error')
        await ctx.channel.send("設定の削除に失敗しました")


@tree.command(
    name="help",
    description="ヘルプメニューを表示します"
)
async def help(ctx):
    help_embed = discord.Embed(title="ヘルプメニュー", color=0x0000ff)
    help_embed.add_field(
        name="\u200b",
        value=":white_check_mark:コマンド一覧は[こちら](https://github.com/OGA45/SmileMusic/blob/main/README.md)"
    )
    help_embed.add_field(
        name="\u200b",
        value=":computer: 質問, 要望などは、[こちら](https://twitter.com/IsthisOga)のTwitterアカウントにお願いします。",
        inline=False)
    help_embed.add_field(
        name="\u200b",
        value=":scroll: [利用規約](https://smilemusic3-legal.oga.ninja/terms) / [プライバシーポリシー](https://smilemusic3-legal.oga.ninja/privacy)",
        inline=False)
    # channel.send だとインタラクションに応答せず「アプリケーションが応答しませんでした」になる
    await ctx.response.send_message(embed=help_embed)


def get_keyword_url(keyword, sort='v'):
    urlKeyword = parse.quote(keyword)
    url = f"https://www.nicovideo.jp/search/{urlKeyword}?sort={sort}"
    return url


def get_tag_url(keyword, sort='v'):
    urlKeyword = parse.quote(keyword)
    url = f"https://www.nicovideo.jp/tag/{urlKeyword}?sort={sort}"
    return url


def to_time(total_second):
    total_second = int(total_second)
    day = total_second / 86400
    total_second %= 86400
    hour = total_second / 3600
    total_second %= 3600
    minute = total_second / 60
    total_second %= 60
    second = total_second

    return datetime(year=1,
                    month=1,
                    day=int(day) + 1,
                    hour=int(hour),
                    minute=int(minute),
                    second=second)


def to_total_second(t):
    return (t.day - 1) * 86400 + t.hour * 3600 + t.minute * 60 + t.second


def get_tags(url):
    r = requests.get(url)
    html = r.text
    soup = bs4.BeautifulSoup(html, "html.parser")
    soup = soup.select_one('meta[name="keywords"]')
    return soup.get("content").split(",")


def _best_thumbnail_url(thumbnails) -> str | None:
    """yt-dlp の thumbnails 配列から最大解像度の URL を選ぶ。

    yt-dlp はだいたい昇順に並べるが保証はないので width*height で最大を取る。
    """
    if not thumbnails:
        return None
    def _score(t):
        return (t.get("width") or 0) * (t.get("height") or 0)
    best = max(thumbnails, key=_score)
    return best.get("url")


# ytdlpで解決する
async def infos_from_ytdl(url, loop=None, normalize=False):
    movie_infos = []
    loop = loop or asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))
    except Exception:
        return False
    if 'entries' in data:
        data = data['entries'][0]

    image_url = _best_thumbnail_url(data.get("thumbnails")) or data.get("thumbnail")
    # yt-dlp が URL の basename から作ったゴミタイトルを弾いて代替を作る。
    # (except 節でも同じ値を使う。以前は data["title"] を再評価しており、
    #  title 欠落時は except の中で KeyError が再送出されて info が消えていた)
    title = _normalize_media_title(data.get("title"), data, url)
    try:
        info = {
            "url": url,
            "title": title,
            "image_url": image_url,
            "time": to_time(int(data["duration"])),
            "normalize": normalize
        }
    except Exception:
        info = {
            "url": url,
            "title": title,
            "image_url": image_url,
            "time": to_time(1),
            "normalize": normalize
        }
    movie_infos.append(info)
    return movie_infos


def _yt_video_item_to_info(v: dict, normalize) -> dict | None:
    """videos.list の 1 item を /list 用の info dict に変換する。
    unlisted は None を返す (呼び元でスキップメッセージを出す想定)。
    """
    if (v.get('status') or {}).get('privacyStatus') == "unlisted":
        return None
    pttn_time = re.compile(r'PT(\d+H)?(\d+M)?(\d+S)?')
    m = pttn_time.search(v['contentDetails']['duration'])
    keys = ['hours', 'minutes', 'seconds']
    kw = {k: 0 if vv is None else int(vv[:-1])
          for k, vv in zip(keys, m.groups())}
    # YouTube API のサムネは default(120) / medium(320) / high(480) /
    # standard(640) / maxres(1280) の順に解像度が上がる。利用可能な最大を選ぶ。
    thumbs = v['snippet']['thumbnails']
    thumb = (thumbs.get('maxres') or thumbs.get('standard')
             or thumbs.get('high') or thumbs.get('medium')
             or thumbs.get('default'))
    return {
        "url": 'https://www.youtube.com/watch?v=' + str(v['id']),
        "title": v['snippet']['title'],
        "image_url": thumb['url'] if thumb else None,
        "time": to_time(timedelta(**kw).total_seconds()),
        "normalize": normalize,
    }


async def infos_youtube_api(ctx: discord.Interaction, data, normalize):
    """旧 API 互換ラッパ: 1 件の playlistItem から info を生成する (1 video.list call)。

    新規利用は推奨しない。プレイリストインポートは
    `_yt_collect_playlist_infos` を使ってバッチ取得する。
    """
    movie_infos = []
    part = ['snippet', 'contentDetails', 'status']
    response2 = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: youtube.videos().list(
            part=part, id=data['contentDetails']['videoId'],
        ).execute(),
    )
    for data2 in response2.get('items', []):
        info = _yt_video_item_to_info(data2, normalize)
        if info is None:
            await embed_async_send(
                ctx, "例外",
                f"[{data2['snippet']['title']}](https://www.youtube.com/watch?v={data2['id']})は非公開のためスキップします。",
                255, 255, 0,
            )
            continue
        movie_infos.append(info)
    return movie_infos


async def _yt_collect_playlist_infos(
    ctx: discord.Interaction,
    list_id: str,
    normalize,
) -> list[dict]:
    """YouTube 再生リストの全曲を 50 件バッチで取得して info dict のリストにする。

    - playlistItems.list でページネートしながら videoId を全部集める
    - videos.list を 50 件バッチで呼ぶ (旧: 1 件ずつ → クォータ 25x 削減 + 速度 10x 改善)
    - unlisted はスキップ通知を送ってから除外
    """
    loop = asyncio.get_event_loop()

    # フェーズ 1: videoId を全ページから集める
    video_ids: list[str] = []
    page_token: str | None = None
    while True:
        kwargs = dict(part='contentDetails', playlistId=list_id, maxResults=50)
        if page_token:
            kwargs['pageToken'] = page_token
        response = await loop.run_in_executor(
            None,
            lambda kw=kwargs: youtube.playlistItems().list(**kw).execute(),
        )
        for res in response.get('items', []):
            vid = (res.get('contentDetails') or {}).get('videoId')
            if vid:
                video_ids.append(vid)
        page_token = response.get('nextPageToken')
        if not page_token:
            break

    if not video_ids:
        return []

    # フェーズ 2: 50 件ずつバッチで videos.list
    infos: list[dict] = []
    BATCH = 50
    for i in range(0, len(video_ids), BATCH):
        chunk = video_ids[i:i + BATCH]
        try:
            v_resp = await loop.run_in_executor(
                None,
                lambda c=chunk: youtube.videos().list(
                    part=['snippet', 'contentDetails', 'status'],
                    id=','.join(c),
                    maxResults=len(c),
                ).execute(),
            )
        except Exception:
            log.exception('unexpected error')
            continue
        by_id = {v.get('id'): v for v in v_resp.get('items', []) if v.get('id')}
        for vid in chunk:
            v = by_id.get(vid)
            if v is None:
                continue
            info = _yt_video_item_to_info(v, normalize)
            if info is None:
                await embed_async_send(
                    ctx, "例外",
                    f"[{v['snippet']['title']}](https://www.youtube.com/watch?v={v['id']})は非公開のためスキップします。",
                    255, 255, 0,
                )
                continue
            infos.append(info)
    return infos


async def infos_jellyfin_api(url, itemId: str, normalize):
    # BC-SEC-02/03 + BC-PERF-05: ハードコード鍵 / imgur を廃止し、env 認証 +
    # X-Emby-Token + 画像プロキシ経由に統一。同期 requests も httpx 非同期へ。
    movie_infos = []
    if not _jellyfin_configured():
        raise ValueError("Jellyfin が未設定です (JELLYFIN_BASE_URL 等の環境変数)")
    auth = await _jellyfin_authenticate()
    if not auth:
        raise ValueError("Jellyfin の認証に失敗しました")
    import httpx
    headers = {"X-Emby-Token": auth["access_token"]}
    async with httpx.AsyncClient(timeout=12.0, headers=headers) as client:
        r = await client.get(
            f"{_JELLYFIN_BASE_URL}/Users/{auth['user_id']}/Items/{itemId}",
        )
        if r.status_code == 401:
            new_auth = await _jellyfin_authenticate(force=True)
            if new_auth:
                client.headers["X-Emby-Token"] = new_auth["access_token"]
                r = await client.get(
                    f"{_JELLYFIN_BASE_URL}/Users/{new_auth['user_id']}/Items/{itemId}",
                )
        if r.status_code != 200:
            raise ValueError(f"Jellyfin アイテム取得失敗 ({r.status_code})")
    jellyfin = r.json()
    primary_tag = (jellyfin.get("ImageTags") or {}).get("Primary") or ""
    image_url = _artwork_proxy_url(_jellyfin_image_url(itemId, primary_tag)) if primary_tag else None
    info = {
        "url": url,
        "title": jellyfin.get("Name") or "Unknown",
        "image_url": image_url,
        "time": to_time((jellyfin.get("RunTimeTicks") or 0) / 10000000),
        "normalize": normalize
    }
    movie_infos.append(info)
    return movie_infos


def _spotify_first_image(images) -> str | None:
    return images[0]["url"] if images else None


async def infos_spotify_track(url, normalize):
    # spotipy は同期なので to_thread で event loop を止めない (BC-PERF-03)
    track_response = await asyncio.to_thread(sp.track, url)
    return [{
        "url": url,
        "title": track_response["name"],
        "image_url": _spotify_first_image((track_response.get("album") or {}).get("images")),
        "time": to_time(track_response["duration_ms"] / 1000),
        "normalize": normalize,
    }]


async def infos_spotify_album(url, normalize):
    # BC-PERF-03: per-track sp.track() の N+1 を廃止。アルバム画像は1回取得、
    # トラックは album_tracks のページを辿る。spotipy 呼び出しは to_thread。
    album = await asyncio.to_thread(sp.album, url)
    image_url = _spotify_first_image(album.get("images"))
    page = album.get("tracks") or {}
    items = list(page.get("items") or [])
    while page.get("next"):
        page = await asyncio.to_thread(sp.next, page)
        items.extend(page.get("items") or [])
    movie_infos = []
    for t in items:
        if not t:
            continue
        ext = (t.get("external_urls") or {}).get("spotify")
        if not ext:
            continue
        movie_infos.append({
            "url": ext,
            "title": t.get("name") or "Unknown",
            "image_url": image_url,
            "time": to_time((t.get("duration_ms") or 0) / 1000),
            "normalize": normalize,
        })
    return movie_infos


async def infos_spotify_playlist(url, normalize):
    # BC-PERF-03: playlist_tracks の track オブジェクトは album.images まで含むので
    # per-track sp.track() は不要。next を辿って全件取得 (100件超対応)。
    page = await asyncio.to_thread(sp.playlist_tracks, url)
    items = list(page.get("items") or [])
    while page.get("next"):
        page = await asyncio.to_thread(sp.next, page)
        items.extend(page.get("items") or [])
    movie_infos = []
    for it in items:
        tr = (it or {}).get("track")
        if not tr:
            continue  # 利用不可トラック (None) はスキップ
        ext = (tr.get("external_urls") or {}).get("spotify")
        if not ext:
            continue
        imgs = (tr.get("album") or {}).get("images")
        movie_infos.append({
            "url": ext,
            "title": tr.get("name") or "Unknown",
            "image_url": _spotify_first_image(imgs),
            "time": to_time((tr.get("duration_ms") or 0) / 1000),
            "normalize": normalize,
        })
    return movie_infos


async def live_infos_youtube_api(liveid):
    movie_infos = []
    part = ['snippet', 'contentDetails']
    response2 = youtube.videos().list(part=part, id=liveid).execute()
    for data2 in response2['items']:
        thumbs = data2['snippet']['thumbnails']
        thumb = (thumbs.get('maxres') or thumbs.get('standard')
                 or thumbs.get('high') or thumbs.get('medium')
                 or thumbs.get('default'))
        info = {
            "url": 'https://www.youtube.com/watch?v='+str(liveid),
            "title": data2['snippet']['title'],
            "image_url": thumb['url'] if thumb else None,
            "time": to_time(1),
            "normalize": False
        }
        movie_infos.append(info)
    return movie_infos


async def _safe_tree_sync() -> None:
    """スラッシュコマンドを同期する。

    Activity を有効にしたアプリには Discord 管理の「Entry Point」コマンドがあり、
    古い discord.py の bulk sync はそれを消そうとして 50240 で失敗する
    (discord.py>=2.5 は自動で保持する)。起動を止めないよう、その失敗は
    traceback ではなく警告ログにして握りつぶす (既存コマンドはそのまま有効)。"""
    try:
        synced = await tree.sync()
        # 同期されたコマンド一覧を残す (新コマンドが Discord に出ないときの
        # 切り分け用)。root handler は WARNING レベルなので print で出す。
        print(
            f"[tree.sync] {len(synced)} commands: "
            + ", ".join(sorted(c.name for c in synced)),
            flush=True)
    except discord.HTTPException as e:
        if getattr(e, "code", None) == 50240:
            # Activity 有効化で Discord が自動生成する Entry Point コマンドは
            # bulk 更新で消せないため 50240 になる。Discord の案内どおり、
            # 既存の Entry Point コマンド (type=4) を payload に含めて再送する。
            await _tree_sync_with_entry_point()
        else:
            log.exception("tree.sync failed")
    except Exception:
        log.exception("tree.sync failed")


async def _tree_sync_with_entry_point() -> None:
    """Entry Point コマンドを含めた bulk 再同期 (50240 のフォールバック)。"""
    try:
        app_id = client.application_id
        existing = await client.http.get_global_commands(app_id)
        entry_points = [c for c in existing if c.get("type") == 4]
        payload = []
        for cmd in tree.get_commands():
            try:
                payload.append(cmd.to_dict(tree))
            except TypeError:
                # 古い discord.py は to_dict() が引数なし
                payload.append(cmd.to_dict())
        payload += entry_points
        data = await client.http.bulk_upsert_global_commands(app_id, payload)
        names = sorted(
            d.get("name", "?") for d in data if d.get("type") != 4)
        print(
            f"[tree.sync] Entry Point を含めて再同期: {len(names)} commands: "
            + ", ".join(names),
            flush=True)
    except Exception:
        log.exception(
            "tree.sync fallback (Entry Point 込みの再同期) も失敗しました。"
            "スラッシュコマンドの追加/変更は Discord に反映されません。")


@client.event
async def on_ready():
    # 管理画面の Gateway 統計 (同名の @client.event を新設すると既存処理が
    # 黙って置換されるため、必ずこの中に追記する)
    _gw_stats["ready"] += 1
    _gw_stats["last_ready_mono"] = time.monotonic()
    _gw_stats["last_ready_wall"] = time.time()
    await client.change_presence(activity=discord.Game(
        f'/help {str(len(client.guilds))}サーバー'))
    # サーバーマスタを最新化 (Grafana 等が guildid から名前を引けるように)
    for _g in client.guilds:
        _touch_guild(_g)
    await _safe_tree_sync()
    print("ready!")


@client.event
async def on_guild_join(guild):
    _touch_guild(guild)
    await client.change_presence(activity=discord.Game(
        f'/help {str(len(client.guilds))}サーバー'))
    client_id = client.user.id
    bot_name = client.user.name


@client.event
async def on_guild_remove(guild):
    key = str(guild.id)
    await _db(delete_setting_sql, key)


@client.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    vch = before.channel
    vcl = discord.utils.get(client.voice_clients, channel=vch)
    bot_user = 0
    if vcl != None:
        for user in vch.members:
            if user.bot:
                bot_user += 1
        if (len(vch.members) == 1 or bot_user == len(vch.members)) and vcl.is_connected():
            guild_id = vcl.guild.id if vcl.guild else None
            await vcl.disconnect()
            if guild_id is not None:
                guild_table.pop(guild_id, None)
                await _publish_stopped(guild_id)


# 管理画面の Gateway 統計用 (接続品質の可視化のみ。他の処理は追加しない)
@client.event
async def on_connect():
    _gw_stats["connect"] += 1


@client.event
async def on_disconnect():
    _gw_stats["disconnect"] += 1


@client.event
async def on_resumed():
    _gw_stats["resumed"] += 1





# テーブル名はリリースチャンネルごとのサフィックス付き (playlist_store が単一情報源)
table_name = playlist_store.GUILDS_TABLE
histry_table_name = playlist_store.HISTORY_TABLE
announce_table_name = playlist_store.ANNOUNCE_TABLE
defalut_volume = 0.1
defalut_stream = True
guild_table = {}

# ===== 管理画面: プロセス/Gateway 計測用グローバル =====
_PROC_STARTED_MONO = time.monotonic()
_PROC_STARTED_WALL = time.time()
# Discord gateway のイベント回数 (on_connect / on_resumed / on_disconnect / on_ready)
_gw_stats = {
    "connect": 0, "resumed": 0, "disconnect": 0, "ready": 0,
    "last_ready_mono": None, "last_ready_wall": None,
}
# CPU% 算出用の前回サンプル (monotonic, プロセス累積CPU秒)
_last_cpu_sample: tuple[float, float] | None = None
# event loop 遅延 (1秒 sleep の超過分) の EWMA / 減衰 max [ms]
_loop_lag_avg_ms = 0.0
_loop_lag_max_ms = 0.0
ssl._create_default_https_context = ssl._create_unverified_context
token = os.environ['SMILEMUSIC3_DISCORD_TOKEN']
defalut_prefix = os.environ['SMILEMUSIC3_PREFIX']
env = os.environ['SMILEMUSIC_ENV']

# 製品名とリリースチャンネル表示名 (管理画面・Embed フッター等で使用)
PRODUCT_NAME = os.environ.get("OGA_MUSIC_PRODUCT_NAME", "OGA_Music")
_CHANNEL_DEFAULTS = {"3": "アルファ", "2": "ベータ", "": "リリース"}
CHANNEL_NAME = os.environ.get("OGA_MUSIC_CHANNEL_NAME") or _CHANNEL_DEFAULTS.get(
    playlist_store.TABLE_SUFFIX, playlist_store.TABLE_SUFFIX)

youtube_token=os.environ['YOUTUBE_TOKEN']
YOUTUBE_API_SERVICE_NAME = 'youtube'
YOUTUBE_API_VERSION = 'v3'
YOUTUBE_API_KEY = os.environ['YOUTUBE_TOKEN']
youtube = build(YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION,developerKey=YOUTUBE_API_KEY)

def _connect_db():
    kwargs = dict(keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3, connect_timeout=10)
    # DATABASE_URL があればそれを使い、無ければ POSTGRES_* から組み立てる。
    # 以前は SMILEMUSIC_ENV != "dev" のとき DATABASE_URL を必須にしていたため、
    # 本番で env を "prod" にすると未設定の DATABASE_URL で起動できなかった。
    # これで SMILEMUSIC_ENV は表示ラベル専用になる。
    url = os.environ.get('DATABASE_URL')
    if url:
        return psycopg2.connect(url, **kwargs)
    return psycopg2.connect(host=os.environ.get('POSTGRES_HOST'), user=os.environ.get('POSTGRES_USER'), password=os.environ.get('POSTGRES_PASSWORD'), database=os.environ.get('POSTGRES_DB'), port=int(os.environ.get('POSTGRES_PORT')), **kwargs)


# 切断時は次の利用時に自動再接続 (db_safety.ManagedConnection)。
# keepalives で NAT/FW の無通知切断を早期検知。
conn = ManagedConnection(_connect_db)
niconico_pattern = re.compile(r'https://(www.nicovideo.jp|sp.nicovideo.jp)')
niconico_ms_pattern = re.compile(r'https://nico.ms')
niconico_id_pattern = re.compile(r'^[a-z]{2}[0-9]+$')


# ===== Discord Activity API integration =====

def _artwork_proxy_url(raw: str) -> str:
    """外部CDNサムネを `/api/image?url=...` 経由で配るためのURL変換。

    iframe origin (`<app_id>.discordsays.com`) からは外部CDNに直接アクセス
    できないので、FastAPI 側のリレーを通す。
    """
    if not raw:
        return ""
    if raw.startswith("/api/image"):
        return raw
    return f"/api/image?url={urllib_parse.quote(raw, safe='')}"


class _NullMessage:
    """fake ctx の channel.send() が返すダミーメッセージ。edit/delete は no-op。"""
    async def edit(self, *args, **kwargs):
        return self

    async def delete(self, *args, **kwargs):
        return None


class _NullChannel:
    async def send(self, *args, **kwargs):
        return _NullMessage()


class _FakeCtx:
    """WS 発火の playback で使う、最小限の ctx 互換オブジェクト。

    play_music / from_url が利用するのは ctx.guild / ctx.channel / (ctx.user)
    なので、それぞれをダミー化したものを渡せば動かせる。
    """
    def __init__(self, guild):
        self.guild = guild
        self.channel = _NullChannel()
        self.user = None


def _make_fake_ctx(guild) -> _FakeCtx:
    return _FakeCtx(guild)


def _ensure_state(guild_id: int) -> dict:
    """guild_table[guild_id] の必須フィールドを揃える。既存値は保持する。"""
    state = guild_table.setdefault(guild_id, {})
    state.setdefault("mode", "queue")
    state.setdefault("has_loop", False)
    state.setdefault("has_loop_queue", False)
    state.setdefault("player", None)
    state.setdefault("music_queue", [])
    state.setdefault("_queue_loop_running", False)
    state.setdefault("playlist_tracks", [])
    state.setdefault("playlist_index", 0)
    state.setdefault("playlist_loop", False)
    state.setdefault("playlist_id", None)
    state.setdefault("playlist_name", None)
    state.setdefault("_playlist_loop_running", False)
    state.setdefault("_playlist_advance", "next")
    # プレイリスト再生の履歴に載せるユーザー (再生を開始した人 / 所有者は予備)
    state.setdefault("playlist_started_by", None)
    state.setdefault("playlist_owner_id", None)
    return state


def _resolve_display_name(guild, user_id, fallback_user=None):
    """そのサーバーでの表示名 (ニックネーム込み) を解決する。表示用。
    サーバーごとに違うので DB には保存せず、/histry などの描画時に使う。"""
    try:
        m = guild.get_member(int(user_id)) if guild else None
    except (TypeError, ValueError):
        m = None
    if m is not None:
        return m.display_name
    return getattr(fallback_user, "username", None) or None


def _resolve_account_name(guild, user_id, fallback_user=None):
    """Discord アカウントのユーザー名を解決する。DB 保存用。

    サーバーごとのニックネームだと同じ人が別名で記録されてしまうため、
    履歴に残すのはアカウント名 (global_name -> username) で統一する。
    """
    try:
        m = guild.get_member(int(user_id)) if guild else None
    except (TypeError, ValueError):
        m = None
    if m is not None:
        return getattr(m, "global_name", None) or m.name
    return getattr(fallback_user, "username", None) or None


def _mark_playlist_starter(state: dict, user_id, owner_id=None,
                           guild=None, user=None) -> None:
    """このプレイリスト再生を開始/切り替えたユーザーを記録する。

    _run_playlist_loop はこの ID で履歴を書く。表示名も記録時点で控えておく
    (退室後に誰か分からなくなるのを防ぐ)。owner_id を渡せる箇所では
    所有者もキャッシュしておく (開始者が取れないときの予備)。
    """
    if user_id:
        state["playlist_started_by"] = str(user_id)
        # DB に残すのはアカウント名 (サーバーごとのニックネームではない)
        name = _resolve_account_name(guild, user_id, user)
        if name:
            state["playlist_started_by_name"] = name
    if owner_id:
        state["playlist_owner_id"] = str(owner_id)


def _detect_source(url: str) -> str:
    """URL から取得元サービス名を判定して返す。判定できなければ空文字。"""
    if not url:
        return ""
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "YouTube"
    if "soundcloud.com" in u:
        return "SoundCloud"
    if "nicovideo.jp" in u or "nico.ms" in u:
        return "niconico"
    if "suno.com" in u or "cdn1.suno.ai" in u:
        return "SUNO"
    if _is_jellyfin_url(url):
        return "Jellyfin"
    if "open.spotify.com" in u or "spotify.com" in u:
        return "Spotify"
    if "bandcamp.com" in u:
        return "Bandcamp"
    if "twitch.tv" in u:
        return "Twitch"
    if "bilibili.com" in u:
        return "Bilibili"
    if "music.apple.com" in u:
        return "Apple Music"
    if "vimeo.com" in u:
        return "Vimeo"
    if "twitter.com" in u or "x.com" in u:
        return "Twitter"
    return ""


# niconico などローカル opus キャッシュのビットレートを per-file でキャッシュ
_file_bitrate_cache: dict[str, int] = {}


def _bitrate_from_file_size(file_path: str, duration_ms: int) -> int:
    """ファイルサイズと長さから平均ビットレート (kbps) を概算する。

    libopus VBR や元素材の品質変動を反映できるので、固定値より正確。
    duration_ms が 0 や file が無い場合は 0。
    """
    if duration_ms <= 0:
        return 0
    cached = _file_bitrate_cache.get(file_path)
    if cached is not None:
        return cached
    try:
        size = os.path.getsize(file_path)
    except OSError:
        return 0
    duration_s = duration_ms / 1000.0
    if duration_s <= 0 or size <= 0:
        return 0
    kbps = int(round(size * 8 / 1000 / duration_s))
    _file_bitrate_cache[file_path] = kbps
    return kbps


def _bitrate_for_url(url: str, duration_ms: int = 0) -> int:
    """ビットレート (kbps) を返す。未取得は 0。

    取得元別:
    - SUNO: 192 kbps MP3 固定 (CDN 配信形式が固定)
    - niconico: ローカル opus キャッシュをファイルサイズ ÷ 長さで概算 (libopus VBR 反映)
    - その他: yt-dlp の extract_info キャッシュから abr/tbr/bitrate を読む
    """
    if not url:
        return 0
    if _SUNO_PATTERN.search(url) or "cdn1.suno.ai" in url:
        return 192
    if "nicovideo.jp" in url or "nico.ms" in url:
        m = re.search(r"/watch/(sm[0-9]+)", url)
        if m and duration_ms > 0:
            file_path = os.path.join(_DL_DIR, f"{m.group(1)}.opus")
            return _bitrate_from_file_size(file_path, duration_ms)
        return 0
    if _is_jellyfin_url(url):
        item_id = _jellyfin_item_id_from_url(url)
        if item_id and item_id in _jellyfin_bitrate_cache:
            return _jellyfin_bitrate_cache[item_id]
        return 0
    cached = _ytdl_cache_get(url)
    if not cached:
        return 0
    for key in ("abr", "tbr", "bitrate"):
        v = cached.get(key)
        if v:
            try:
                return int(round(float(v)))
            except (TypeError, ValueError):
                continue
    return 0


# ---------- SUNO (AI生成楽曲サービス) extractor ----------
# yt-dlp が SUNO に対応していないので独自に解決する。
# ページ HTML を bot UA で取得 → インライン JSON からタイトル/作者/長さを抽出。
# オーディオ URL は UUID から決定的に組み立てる (cdn1.suno.ai/{uuid}.mp3、署名なし)。
_SUNO_PATTERN = re.compile(
    r"https?://(?:www\.)?suno\.com/(?:song|s)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_SUNO_UA = (
    "Mozilla/5.0 (compatible; Twitterbot/1.0)"  # SSR メタを返してくれる UA
)


def _suno_audio_url(song_id: str) -> str:
    return f"https://cdn1.suno.ai/{song_id}.mp3"


def _suno_image_url(song_id: str) -> str:
    return f"https://cdn2.suno.ai/image_{song_id}.jpeg"


async def _resolve_suno(url: str) -> dict | None:
    """SUNO 楽曲 URL からオーディオ直 URL とメタ情報を返す。

    UUID 抽出に失敗したら None。
    メタ情報の取得に失敗してもオーディオ URL だけは返す (UUID から決定的に作れるため)。
    """
    m = _SUNO_PATTERN.search(url)
    if not m:
        return None
    song_id = m.group(1).lower()
    audio_url = _suno_audio_url(song_id)
    image_url = _suno_image_url(song_id)
    title = "SUNO Track"
    artist = ""
    duration_s = 0
    try:
        import httpx
        async with httpx.AsyncClient(
            timeout=8.0, follow_redirects=True,
            headers={"User-Agent": _SUNO_UA},
        ) as client:
            r = await client.get(f"https://suno.com/song/{song_id}")
        if r.status_code == 200:
            html = r.text
            # インライン JSON から取得。`\"title\":\"...\"` のような escape された
            # 形で書かれているので JSON-parse はせず正規表現で取り出す。
            mt = re.search(r'\\"title\\":\\"([^"\\]+)\\"', html)
            md = re.search(r'\\"duration\\":(\d+(?:\.\d+)?)', html)
            mh = re.search(r'\\"display_name\\":\\"([^"\\]+)\\"', html)
            if mt:
                raw = mt.group(1)
                if "\\u" in raw:
                    try:
                        title = json.loads(f'"{raw}"')
                    except Exception:
                        title = raw
                else:
                    title = raw
            if md:
                try:
                    duration_s = int(float(md.group(1)))
                except (TypeError, ValueError):
                    duration_s = 0
            if mh:
                # 値は通常そのままの UTF-8 で埋め込まれている。\\u0041 形式の
                # エスケープが混じっていた場合のみ json で復元する。
                raw = mh.group(1)
                if "\\u" in raw:
                    try:
                        artist = json.loads(f'"{raw}"')
                    except Exception:
                        artist = raw
                else:
                    artist = raw
            # description meta タグから補完 (タイトルが取れなかった場合の fallback)
            if title == "SUNO Track":
                mdesc = re.search(
                    r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']+)["\']',
                    html,
                )
                if mdesc:
                    md2 = re.match(r"^(.+?) by (.+?) \(@", mdesc.group(1))
                    if md2:
                        title = md2.group(1)
                        if not artist:
                            artist = md2.group(2)
    except Exception:
        log.exception('unexpected error')
    return {
        "audio_url": audio_url,
        "title": title,
        "artist": artist,
        "artwork": image_url,
        "duration_s": duration_s,
    }


def _requester_name(info: dict) -> str:
    """キュー項目の追加者名。Activity 経由で member キャッシュが取れず author=None でも
    落ちないよう author_name で補う。"""
    a = info.get("author")
    return (getattr(a, "display_name", None) or getattr(a, "name", None)
            or info.get("author_name") or "Activity")


def _track_info_from_queue_item(info: dict) -> TrackInfo:
    duration_seconds = to_total_second(info["time"]) if "time" in info else 0
    author = info.get("author")
    artist = ""
    if author is not None:
        artist = getattr(author, "display_name", "") or getattr(author, "name", "")
    artist = artist or info.get("author_name") or ""
    url = info.get("url", "")
    duration_ms = int(duration_seconds * 1000)
    return TrackInfo(
        title=info.get("title", "Unknown"),
        artist=artist,
        artwork=_artwork_proxy_url(info.get("image_url") or ""),
        duration_ms=duration_ms,
        url=url,
        source=_detect_source(url),
        bitrate_kbps=_bitrate_for_url(url, duration_ms),
    )


def _get_current_position_ms(player) -> int:
    if player is None:
        return 0
    try:
        return int(player.original.total_milliseconds)
    except AttributeError:
        return int(getattr(player, "total_milliseconds", 0))


def _player_is_paused(guild_id: int) -> bool:
    """擬似 pause 中か (player._paused)。pause/resume 判定の単一の真実。"""
    p = (guild_table.get(guild_id) or {}).get("player")
    return bool(p is not None and getattr(p, "_paused", False))


def _player_set_paused(guild_id: int, paused: bool) -> bool:
    """player._paused を設定する。player が無ければ False、設定したら True。"""
    p = (guild_table.get(guild_id) or {}).get("player")
    if p is None:
        return False
    p._paused = paused
    return True


def _playlist_track_to_track_info(t: dict) -> TrackInfo:
    """playlist_tracks に入っている dict (DB由来) を TrackInfo に変換。"""
    url = t.get("url", "")
    duration_ms = int(t.get("duration_ms") or 0)
    return TrackInfo(
        title=t.get("title") or "Unknown",
        artist=t.get("artist", ""),
        artwork=_artwork_proxy_url(t.get("artwork") or ""),
        duration_ms=duration_ms,
        url=url,
        source=_detect_source(url),
        bitrate_kbps=_bitrate_for_url(url, duration_ms),
    )


async def _publish_state(guild_id: int) -> None:
    data = guild_table.get(guild_id) or {}
    mode = data.get("mode", "queue")
    player = data.get("player")
    g = client.get_guild(guild_id)
    vc = g.voice_client if g else None

    state = bus.get_state(guild_id)
    state.mode = mode
    state.guild_name = g.name if g else ""

    if mode == "playlist":
        tracks = data.get("playlist_tracks") or []
        idx = data.get("playlist_index", 0)
        if tracks and 0 <= idx < len(tracks):
            state.track = _playlist_track_to_track_info(tracks[idx])
        else:
            state.track = None
        # Drawer + UpNext は全曲リストを必要とする。
        # シャッフル ON で next_index が 200 超を返す事もあるので頭打ちにしない。
        # 大きいプレイリスト (~数千曲) でも 1 トラックあたり ~400 byte、
        # state 送出は user 操作起点で 1 秒に複数回も飛ばないので帯域許容範囲。
        state.upnext = [_playlist_track_to_track_info(t) for t in tracks]
        if data.get("playlist_id"):
            state.playlist = PlaylistInfo(
                id=str(data["playlist_id"]),
                name=data.get("playlist_name") or "",
                index=idx,
                loop=bool(data.get("playlist_loop")),
                shuffle=bool(data.get("playlist_shuffle")),
                next_index=_preview_next_playlist_index(data, idx),
                loop_single=bool(data.get("playlist_loop_single")),
            )
        else:
            state.playlist = None
        state.queue_loop = False
        state.queue_loop_single = False
        state.queue_next_index = -1
    else:
        queue = data.get("music_queue") or []
        if queue:
            state.track = _track_info_from_queue_item(queue[0])
            state.upnext = [_track_info_from_queue_item(q) for q in queue[1:101]]
        else:
            state.track = None
            state.upnext = []
        state.playlist = None
        state.queue_loop = bool(data.get("has_loop_queue"))
        state.queue_loop_single = bool(data.get("has_loop"))
        # 次の曲は upnext[0] (= queue[1])。ループで唯一の曲なら自身。
        if state.upnext:
            state.queue_next_index = 0
        elif (state.queue_loop or state.queue_loop_single) and queue:
            state.queue_next_index = -2  # 「現在の曲をリピート」表示用センチネル
        else:
            state.queue_next_index = -1
    playing = bool(vc and vc.is_playing())
    # 擬似 pause: vc.is_paused() ではなく player._paused で判定する
    # (pause 中も送出継続するので vc.is_playing()=True / vc.is_paused()=False のため)。
    paused = bool(player is not None and getattr(player, "_paused", False))
    # 鳴っている間は今の曲に実際に掛かっているか (/play normalize の曲・反映失敗も正しく出る)。
    # トグル直後の反映待ち (0.8 秒) と、再生していない間はトグルの値。
    # (擬似 pause 中も vc.is_playing() は True。ライブにはトグルが効かないのでトグルの値を出す)
    pending = data.get("_normalize_apply_task")
    pending = pending is not None and not pending.done()
    if (playing and player is not None and not pending
            and not getattr(player, "is_live", False)):
        state.normalize = bool(getattr(player, "normalize_enabled", False))
    else:
        state.normalize = bool(data.get("normalize"))
    state.is_playing = playing and not paused
    # 次のトラックを yt-dlp で読み込み中。再生が始まったら / 一時停止中なら false。
    state.loading = bool(data.get("_loading")) and not playing and not paused
    # 純粋なキュー (music_queue) は mode に関係なく常に同期する。
    mq = data.get("music_queue") or []
    state.music_queue = [_track_info_from_queue_item(q) for q in mq[:200]]
    state.music_queue_total = len(mq)
    if not playing and not paused:
        # idle (停止中 / モード切替直後) は 0 に揃える
        state.position_ms = 0
    else:
        state.position_ms = _get_current_position_ms(player)
    await bus.publish_state(guild_id)
    # MOD-PERF-04: キューが変化していれば別メッセージで送る (state は軽量に保つ)
    await bus.publish_queue_full(guild_id)


async def _publish_stopped(guild_id: int) -> None:
    state = bus.get_state(guild_id)
    state.track = None
    state.upnext = []
    state.position_ms = 0
    state.is_playing = False
    await bus.publish_stopped(guild_id)


async def _progress_loop() -> None:
    """1秒ごとに現在の再生位置を流すバックグラウンドタスク。"""
    while True:
        try:
            await asyncio.sleep(1.0)
            for gid, data in list(guild_table.items()):
                player = data.get("player")
                if player is None:
                    continue
                g = client.get_guild(int(gid))
                vc = g.voice_client if g else None
                if (not vc or not vc.is_playing() or vc.is_paused()
                        or _player_is_paused(int(gid))):
                    continue
                ms = _get_current_position_ms(player)
                state = bus.get_state(int(gid))
                state.position_ms = ms
                await bus.publish_progress(int(gid), ms)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("progress loop iteration failed")


# ===== 管理画面: メトリクス収集とギルドスナップショット =====

def _read_cpu_seconds():
    """プロセス累積 CPU 時間 [s] (/proc/self/stat)。非 Linux では None。"""
    try:
        with open("/proc/self/stat") as f:
            # comm にスペースが入り得るので ")" の後ろで切る。
            # 切った後は parts[11]=utime, parts[12]=stime (man proc の field 14/15)
            parts = f.read().rsplit(")", 1)[1].split()
        return (int(parts[11]) + int(parts[12])) / os.sysconf("SC_CLK_TCK")
    except Exception:
        return None


def _read_rss_bytes():
    """常駐メモリ [bytes] (/proc/self/statm)。非 Linux では None。"""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        return None


def _sample_cpu_percent():
    """前回呼び出しからの平均 CPU 使用率 [%]。初回・非 Linux・
    間隔 0.5 秒未満 (アンカーは保持) は None。sleep はしない。"""
    global _last_cpu_sample
    cpu = _read_cpu_seconds()
    if cpu is None:
        return None
    now = time.monotonic()
    prev = _last_cpu_sample
    if prev is not None:
        dt = now - prev[0]
        if dt < 0.5:
            return None
        _last_cpu_sample = (now, cpu)
        return round((cpu - prev[1]) / dt * 100, 1)
    _last_cpu_sample = (now, cpu)
    return None


def _read_net_bytes():
    """コンテナ netns 全体の累積 RX/TX bytes (/proc/net/dev、lo 除く)。
    ffmpeg 子プロセスの通信も同じ netns なので含まれる。非 Linux では None。"""
    try:
        rx = tx = 0
        with open("/proc/net/dev") as f:
            for line in f.read().splitlines()[2:]:
                if ":" not in line:
                    continue
                iface, rest = line.split(":", 1)
                if iface.strip() == "lo":
                    continue
                fields = rest.split()
                rx += int(fields[0])
                tx += int(fields[8])
        return rx, tx
    except Exception:
        return None


def _read_disk_bytes():
    """累積 Disk I/O bytes。(read, write, scope) を返す。
    cgroup v2 の io.stat (コンテナ全体 = ffmpeg 子プロセス込み) を優先し、
    無い環境では /proc/self/io (BOT プロセスのみ) にフォールバック。"""
    try:
        r = w = 0
        with open("/sys/fs/cgroup/io.stat") as f:
            for line in f.read().splitlines():
                for kv in line.split()[1:]:
                    k, _, v = kv.partition("=")
                    if k == "rbytes":
                        r += int(v)
                    elif k == "wbytes":
                        w += int(v)
        return r, w, "cgroup"
    except Exception:
        pass
    try:
        stats = {}
        with open("/proc/self/io") as f:
            for line in f.read().splitlines():
                k, _, v = line.partition(":")
                stats[k.strip()] = int(v.strip())
        return stats["read_bytes"], stats["write_bytes"], "process"
    except Exception:
        return None


# ネットワーク/ディスクのレート算出用アンカー (monotonic, net, disk)
_last_io_sample = None


def _sample_io_rates() -> dict:
    """前回呼び出しからのネットワーク/ディスク転送レート [bytes/s]。
    CPU% と同じ方式: 初回・非 Linux は None、間隔 0.5 秒未満は
    アンカーを保持したまま None を返す。"""
    global _last_io_sample
    net = _read_net_bytes()
    disk = _read_disk_bytes()
    result = {
        "net_rx_bps": None, "net_tx_bps": None,
        "disk_read_bps": None, "disk_write_bps": None,
        "disk_scope": disk[2] if disk else None,
    }
    if net is None and disk is None:
        return result
    now = time.monotonic()
    prev = _last_io_sample
    if prev is not None:
        dt = now - prev[0]
        if dt < 0.5:
            return result
    _last_io_sample = (now, net, disk)
    if prev is None:
        return result

    def _rate(cur, old):
        if cur is None or old is None:
            return None
        # カウンタリセット (コンテナ再作成等) は負になるので 0 に丸める
        return round(max(0, cur - old) / dt, 1)

    pnet, pdisk = prev[1], prev[2]
    if net is not None and pnet is not None:
        result["net_rx_bps"] = _rate(net[0], pnet[0])
        result["net_tx_bps"] = _rate(net[1], pnet[1])
    if disk is not None and pdisk is not None:
        result["disk_read_bps"] = _rate(disk[0], pdisk[0])
        result["disk_write_bps"] = _rate(disk[1], pdisk[1])
    return result


async def _loop_lag_monitor() -> None:
    """event loop の詰まり (1 秒 sleep の超過分) を EWMA / 減衰 max で記録する。"""
    global _loop_lag_avg_ms, _loop_lag_max_ms
    try:
        while True:
            t0 = time.monotonic()
            await asyncio.sleep(1.0)
            lag = max(0.0, (time.monotonic() - t0 - 1.0) * 1000.0)
            _loop_lag_avg_ms = _loop_lag_avg_ms * 0.9 + lag * 0.1
            _loop_lag_max_ms = max(_loop_lag_max_ms * 0.95, lag)
    except asyncio.CancelledError:
        pass


# コアライブラリのバージョン (プロセス中は不変なので一度だけ収集してキャッシュ)
_lib_versions_cache: dict | None = None


def _get_lib_versions() -> dict:
    """管理画面用のライブラリバージョン一覧。ffmpeg はバイナリ実行を伴うので
    初回のみ別スレッド (asyncio.to_thread) から呼ぶこと。キーはそのまま
    表示ラベルになる (順序も保持される)。"""
    global _lib_versions_cache
    if _lib_versions_cache is not None:
        return _lib_versions_cache

    def _ver(fn):
        try:
            return fn()
        except Exception:
            return None

    def _ffmpeg_version():
        out = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        first = out.splitlines()[0]
        # 例: "ffmpeg version 6.1.1-static Copyright ..."
        return first.split()[2] if first.startswith("ffmpeg version") else None

    _lib_versions_cache = {
        "discord.py": _ver(lambda: discord.__version__),
        "yt-dlp": _ver(lambda: yt_dlp.version.__version__),
        "FFmpeg": _ver(_ffmpeg_version),
        "FastAPI": _ver(lambda: importlib.metadata.version("fastapi")),
        "uvicorn": _ver(lambda: uvicorn.__version__),
        "psycopg2": _ver(lambda: psycopg2.__version__.split()[0]),
    }
    return _lib_versions_cache


# DB 統計は COUNT(*) を含むため 15 秒キャッシュ (3 秒ポーリングで連打しない)
_admin_db_stats_cache: dict = {"ts": 0.0, "data": None}


async def _admin_db_stats() -> dict:
    now = time.monotonic()
    cache = _admin_db_stats_cache
    if cache["data"] is not None and now - cache["ts"] < 15.0:
        return cache["data"]
    try:
        data = await _db(get_admin_db_stats_sql)
        data["cached_at"] = time.time()
    except Exception as e:
        # 接続文字列等を漏らさないよう例外クラス名のみ返す
        data = {"ok": False, "error": e.__class__.__name__}
    data["closed"] = bool(conn.closed)
    cache["ts"] = now
    cache["data"] = data
    return data


def _admin_guild_snapshot(g) -> dict:
    """管理画面用のギルド 1 行分。読み取り専用 —
    guild_table / bus に state を挿入しない (_ensure_state / get_state 禁止)。"""
    state = guild_table.get(g.id) or {}
    vc = g.voice_client
    connected = bool(vc and vc.is_connected())
    vc_latency = vc.latency if connected else None
    voice = {
        "connected": connected,
        "channel": vc.channel.name if connected and vc.channel else None,
        "latency_ms": round(vc_latency * 1000, 1)
        if isinstance(vc_latency, float) and math.isfinite(vc_latency) else None,
    }
    playing = None
    bstate = bus.peek_state(g.id)
    if state or bstate is not None:
        paused = _player_is_paused(g.id)
        track = None
        if bstate is not None and bstate.track is not None:
            track = {
                "title": bstate.track.title,
                "url": bstate.track.url,
                "duration_ms": bstate.track.duration_ms,
            }
        playlist = None
        if state.get("mode") == "playlist":
            playlist = {
                "name": state.get("playlist_name") or "",
                "index": state.get("playlist_index", 0),
                "total": len(state.get("playlist_tracks") or []),
            }
        playing = {
            "mode": state.get("mode", "queue"),
            "is_playing": bool(vc and vc.is_playing() and not paused),
            "paused": paused,
            "loading": bool(state.get("_loading")),
            "track": track,
            "position_ms": _get_current_position_ms(state.get("player")),
            "queue_length": len(state.get("music_queue") or []),
            "playlist": playlist,
        }
    return {
        "id": str(g.id),
        "name": g.name,
        "member_count": g.member_count,
        "voice": voice,
        "playing": playing,
        "ws_connections": 0,  # 呼び出し側で bus.subscriber_counts() から埋める
    }


async def _admin_get_guilds() -> list[dict]:
    counts = bus.subscriber_counts()
    rows = []
    for g in client.guilds:
        try:
            row = _admin_guild_snapshot(g)
        except Exception:
            # 1 ギルドの異常で一覧全体を落とさない
            log.exception("admin snapshot failed (guild=%s)", g.id)
            row = {
                "id": str(g.id), "name": g.name, "member_count": None,
                "voice": {"connected": False, "channel": None, "latency_ms": None},
                "playing": None, "ws_connections": 0,
            }
        row["ws_connections"] = counts.get(g.id, 0)
        rows.append(row)
    return rows


async def _admin_get_overview() -> dict:
    lat = client.latency  # 未接続時は nan / inf があり得る (生 NaN は不正 JSON)
    latency_ms = round(lat * 1000.0, 1) \
        if isinstance(lat, float) and math.isfinite(lat) else None
    counts = bus.subscriber_counts()
    ready_mono = _gw_stats["last_ready_mono"]
    # 初回のみ ffmpeg -version の subprocess 実行があるため別スレッドで収集
    versions = _lib_versions_cache
    if versions is None:
        versions = await asyncio.to_thread(_get_lib_versions)
    return {
        "bot": {
            "user": str(client.user) if client.user else None,
            "guild_count": len(client.guilds),
            "env": env,
            "product_name": PRODUCT_NAME,
            "channel_name": CHANNEL_NAME,
            "started_at": _PROC_STARTED_WALL,
            "uptime_s": round(time.monotonic() - _PROC_STARTED_MONO, 1),
            "versions": versions,
        },
        "system": {
            "cpu_percent": _sample_cpu_percent(),
            "rss_bytes": _read_rss_bytes(),
            "loop_lag_ms_avg": round(_loop_lag_avg_ms, 1),
            "loop_lag_ms_max": round(_loop_lag_max_ms, 1),
            "python": platform.python_version(),
            **_sample_io_rates(),
        },
        "discord": {
            "latency_ms": latency_ms,
            "is_ready": client.is_ready(),
            "connect_count": _gw_stats["connect"],
            "resumed_count": _gw_stats["resumed"],
            "disconnect_count": _gw_stats["disconnect"],
            # resume は再接続、ready の 2 回目以降はフル再接続
            "reconnect_count": _gw_stats["resumed"] + max(0, _gw_stats["ready"] - 1),
            "last_ready_at": _gw_stats["last_ready_wall"],
            "gateway_uptime_s": round(time.monotonic() - ready_mono, 1)
            if ready_mono else None,
        },
        "db": await _admin_db_stats(),
        "activity": {"total_ws_connections": sum(counts.values())},
    }


async def _audio_features_loop() -> None:
    """~25 Hz で各 guild のロールバッファから RMS / バンド / ビートを送る。"""
    interval = 1 / 25
    zero_bands = [0.0] * _BAND_COUNT
    while True:
        try:
            await asyncio.sleep(interval)
            now = time.time()
            for gid in list(_pcm_frame_buffer.keys()):
                dq = _pcm_frame_buffer.get(gid)
                if not dq:
                    continue
                # BC-PERF-04: 視聴者 (Activity WS 購読者) がいない guild では
                # FFT/ビート検出も broadcast もスキップして CPU を節約する。
                if not bus.has_subscribers(int(gid)):
                    continue
                g = client.get_guild(int(gid))
                vc = g.voice_client if g else None
                if (not vc or not vc.is_playing() or vc.is_paused()
                        or _player_is_paused(int(gid))):
                    # 静止状態 (停止/擬似pause) をフロントに伝えて visualizer をフラットに
                    _reset_audio_features_state(int(gid))
                    await bus._broadcast(int(gid), {
                        "type": "audio_features",
                        "rms": 0.0,
                        "bands": zero_bands,
                        "beat": False,
                    })
                    continue
                # MOD-PERF-09: FFT (rfft 4096) は event loop を止めないよう別スレッドで
                rms, bands, beat = await asyncio.to_thread(
                    _compute_audio_features, int(gid), now,
                )
                await bus._broadcast(int(gid), {
                    "type": "audio_features",
                    "rms": rms,
                    "bands": bands,
                    "beat": beat,
                })
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("audio features loop iteration failed")


def _ms_to_seek_str(position_ms: int) -> str:
    seconds = max(0, position_ms // 1000)
    return f"{seconds // 3600}:{(seconds // 60) % 60:02d}:{seconds % 60:02d}"


def _ensure_shuffle_order(state: dict) -> list[int]:
    tracks = state.get("playlist_tracks") or []
    order = state.get("playlist_shuffle_order") or []
    if len(order) != len(tracks) or set(order) != set(range(len(tracks))):
        order = list(range(len(tracks)))
        random.shuffle(order)
        state["playlist_shuffle_order"] = order
    return order


def _compute_next_playlist_index(state: dict, current_idx: int) -> int | None:
    """次のトラック index を返す。state を更新する。

    シャッフル ON の場合は末尾でもリシャッフルして先頭から続ける(暗黙ループ)。
    シャッフル OFF + ループ OFF で末尾の場合のみ None。
    """
    tracks = state.get("playlist_tracks") or []
    if not tracks:
        return None
    if state.get("playlist_shuffle"):
        order = _ensure_shuffle_order(state)
        try:
            pos = order.index(current_idx)
        except ValueError:
            pos = -1
        next_pos = pos + 1
        if next_pos >= len(order):
            # 末尾なら順序をシャッフルし直して先頭から
            random.shuffle(order)
            state["playlist_shuffle_order"] = order
            return order[0]
        return order[next_pos]
    new_idx = current_idx + 1
    if new_idx >= len(tracks):
        if state.get("playlist_loop"):
            return 0
        return None
    return new_idx


def _compute_prev_playlist_index(state: dict, current_idx: int) -> int:
    tracks = state.get("playlist_tracks") or []
    if not tracks:
        return 0
    if state.get("playlist_shuffle"):
        order = _ensure_shuffle_order(state)
        try:
            pos = order.index(current_idx)
        except ValueError:
            pos = 0
        prev_pos = pos - 1
        if prev_pos < 0:
            return order[-1] if state.get("playlist_loop") else order[0]
        return order[prev_pos]
    new_idx = current_idx - 1
    if new_idx < 0:
        return (len(tracks) - 1) if state.get("playlist_loop") else 0
    return new_idx


def _preview_next_playlist_index(state: dict, current_idx: int) -> int:
    """state を変更せずに「次に再生される予定の index」を返す。-1 なら無し。

    シャッフル ON のときは末尾でも先頭にラップさせる(UP NEXT が空にならないように)。
    """
    tracks = state.get("playlist_tracks") or []
    if not tracks:
        return -1
    if state.get("playlist_shuffle"):
        order = state.get("playlist_shuffle_order") or []
        if order and len(order) == len(tracks):
            try:
                pos = order.index(current_idx)
                next_pos = (pos + 1) % len(order)
                return order[next_pos]
            except ValueError:
                pass
        # order が無効/未生成の fallback: 線形ラップ
        return (current_idx + 1) % len(tracks)
    new_idx = current_idx + 1
    if new_idx >= len(tracks):
        return 0 if state.get("playlist_loop") else -1
    return new_idx


def _peek_next_track_url(state: dict) -> str | None:
    """state から「次に再生される予定のトラック」の URL を取り出す。

    プレイリストモードはシャッフル/ループを考慮、キューモードは music_queue[1]。
    取得不能なら None。
    """
    mode = state.get("mode", "queue")
    if mode == "playlist":
        tracks = state.get("playlist_tracks") or []
        if not tracks:
            return None
        idx = state.get("playlist_index", 0)
        ni = _preview_next_playlist_index(state, idx)
        if 0 <= ni < len(tracks):
            url = (tracks[ni] or {}).get("url") or ""
            return url or None
    else:
        queue = state.get("music_queue") or []
        if len(queue) >= 2:
            url = (queue[1] or {}).get("url") or ""
            return url or None
        if state.get("has_loop_queue") and queue:
            url = (queue[0] or {}).get("url") or ""
            return url or None
    return None


def _start_prefetch_next(guild_id: int) -> None:
    """次のトラックを yt-dlp で先回り抽出して、_ytdl_cache に温める。

    バックグラウンドタスクで走らせ、失敗しても無視。
    既にキャッシュ済み / 抽出中ならスキップ。
    """
    state = guild_table.get(guild_id, {})
    next_url = _peek_next_track_url(state)
    if not next_url:
        return
    if next_url.startswith("https://www.nicovideo.jp/"):
        return  # niconico は別パス(ローカルファイル)なのでスキップ
    if _SUNO_PATTERN.search(next_url):
        return  # SUNO は UUID から決定的に URL が出るので prefetch 不要
    if _is_jellyfin_url(next_url):
        return  # Jellyfin も item id から決定的に stream URL が出るので prefetch 不要
    if _ytdl_cache_get(next_url) is not None:
        return
    if next_url in _ytdl_inflight:
        return
    # 古い prefetch があれば残しておく (キャッシュ温存のため kill しない)
    async def _do() -> None:
        try:
            await _extract_ytdl_cached(next_url, stream=True)
        except Exception:
            log.exception('unexpected error')
    asyncio.create_task(_do(), name=f"prefetch_{guild_id}")


async def _run_playlist_loop(guild_id: int) -> None:
    """マイプレイリストモードの再生ループ。

    state['_playlist_advance'] が 'next' / 'prev' / 'select' / 'stay' のいずれかで、
    各トラック終了時に次のインデックスを決める。
    """
    g = client.get_guild(guild_id)
    if not g:
        return
    fake_ctx = _make_fake_ctx(g)
    fails = 0   # 連続で再生不可だった曲数
    quick = 0   # 連続で 1 秒未満に終わった回数
    while True:
        state = guild_table.get(guild_id, {})
        if state.get("mode") != "playlist":
            return
        if not g.voice_client:
            await _publish_stopped(guild_id)
            return
        tracks = state.get("playlist_tracks") or []
        if not tracks:
            await _publish_state(guild_id)
            return
        idx = state.get("playlist_index", 0)
        if idx >= len(tracks):
            if state.get("playlist_loop"):
                idx = 0
                state["playlist_index"] = 0
            else:
                state["playlist_index"] = 0
                await _publish_state(guild_id)
                return
        if idx < 0:
            idx = 0
            state["playlist_index"] = 0
        track_info = tracks[idx]
        state["_playlist_advance"] = "next"  # 既定値。WS コマンドで上書きされ得る
        # yt-dlp ロード中はサーバ側で先にトラック情報だけ反映する。
        # フロントは is_playing=false + loading=true でシークバーを止めて
        # 「読み込み中」と表示できる。
        state["_loading"] = True
        await _publish_state(guild_id)
        t0 = time.monotonic()
        try:
            is_error = await play_music(
                fake_ctx, track_info["url"],
                normalize=None,  # 曲ごとの指定は無い → Activity のトグルに従う
                history=_history_info_from_playlist(state, track_info),
                expected_ms=int(track_info.get("duration_ms") or 0),
            )
        finally:
            state["_loading"] = False
        # state["playlist_index"] は reorder/remove などで再生中に書き換えられている
        # 可能性があるため、ローカル変数 idx (= 開始時点の値) ではなく state の最新値を基点にする。
        cur_idx = state.get("playlist_index", idx)
        if is_error:
            state["playlist_index"] = cur_idx + 1
            fails += 1
            # 全曲再生できない状態で playlist_loop / shuffle だと無限に回り続けるので止める。
            # 1 曲だけのプレイリストなら 1 回で止める。
            if fails >= min(_MAX_CONSEC_FAILS, max(1, len(tracks))):
                await _playback_error_notice(
                    fake_ctx, guild_id,
                    f"{fails}曲連続で再生に失敗したため、再生を停止しました。" if fails > 1
                    else "再生に失敗したため、再生を停止しました。")
                await _publish_state(guild_id)
                return
            await _publish_state(guild_id)
            await asyncio.sleep(min(0.5 * fails, 2.0))
            continue
        fails = 0
        started = state.get("_play_started_at", 0) >= t0
        quick = quick + 1 if (not started and time.monotonic() - t0 < 1.0) else 0
        if quick >= 3:
            await asyncio.sleep(min(quick - 2, 5))
        # 履歴は play_music が実音声 1 秒の時点で記録済み
        # 終了後の遷移 (shuffle / loop を考慮)
        advance = state.pop("_playlist_advance", "next")
        if advance == "prev":
            state["playlist_index"] = _compute_prev_playlist_index(state, cur_idx)
        elif advance == "select":
            # select_playlist が外部で index を書き換えている
            pass
        elif advance == "stay":
            # reorder などで cur_idx が動いている可能性があるのでそのまま据え置く
            state["playlist_index"] = cur_idx
        else:  # "next"
            # 1曲ループが有効なら同じ index を再生し続ける
            if state.get("playlist_loop_single"):
                state["playlist_index"] = cur_idx
            else:
                next_idx = _compute_next_playlist_index(state, cur_idx)
                if next_idx is None:
                    # ループ無しで末尾。次回再開用に index を 0 に戻して終了
                    state["playlist_index"] = 0
                    await _publish_state(guild_id)
                    return
                state["playlist_index"] = next_idx


async def _start_playback_via_ws(guild_id: int) -> None:
    """Activity の ▶ / ライブラリ選択から現在モードの再生を開始する。

    既に走っている再生タスク (yt-dlp ロード中も含む) はキャンセルしてから
    新しいタスクを始める。これでライブラリ連打時に古いトラックが
    遅れて再生されるのを防ぐ。
    """
    state = guild_table.get(guild_id, {})
    me = asyncio.current_task()
    old_task = state.get("_active_playback_task")
    if old_task is not None and old_task is not me and not old_task.done():
        old_task.cancel()
        try:
            await old_task
        except (asyncio.CancelledError, Exception):
            pass

    state["_active_playback_task"] = me
    try:
        mode = state.get("mode", "queue")
        if mode == "queue":
            if not state.get("music_queue") or state.get("_queue_loop_running"):
                return
            g = client.get_guild(guild_id)
            if not g:
                return
            if not (g.voice_client and g.voice_client.is_connected()):
                # 再生ループは VC が無いと guild の状態ごと消してしまう (キューが消える) ので始めない
                await _notify(
                    guild_id, "error",
                    "BOT がボイスチャンネルにいないため再生できません。/play か /join で BOT を呼んでください")
                await _publish_state(guild_id)
                return
            fake_ctx = _make_fake_ctx(g)
            state["_queue_loop_running"] = True
            try:
                await _run_queue_playback_loop(fake_ctx)
            finally:
                state["_queue_loop_running"] = False
        elif mode == "playlist":
            if not state.get("playlist_tracks"):
                return
            if state.get("_playlist_loop_running"):
                return
            # 保険: 各 WS ハンドラで記録し損ねていても、操作したユーザーを拾う
            if not state.get("playlist_started_by"):
                _mark_playlist_starter(
                    state, _current_ws_user.get(), guild=client.get_guild(guild_id))
            state["_playlist_loop_running"] = True
            try:
                await _run_playlist_loop(guild_id)
            finally:
                state["_playlist_loop_running"] = False
    finally:
        if state.get("_active_playback_task") is me:
            state["_active_playback_task"] = None


_current_ws_user: ContextVar[str | None] = ContextVar(
    "_current_ws_user", default=None,
)


def _guild_can_manage(g, user_id) -> bool:
    """Manage Guild 権限を持つメンバーか。キャッシュミス (get_member=None) は
    安全側で False (お知らせチャンネルは guild 全体の設定なので権限必須)。"""
    if g is None:
        return False
    try:
        m = g.get_member(int(user_id))
    except (TypeError, ValueError):
        return False
    return bool(m and m.guild_permissions.manage_guild)


async def _guild_settings_payload(g, *, include_manage: bool, user_id=None) -> dict:
    """Activity へ送る guild_settings メッセージ。

    include_manage=True (本人宛て返信) のときだけ can_manage と、管理者には
    channels (BOT が送信できるテキスト ch 一覧) を含める。設定変更後の
    guild 全体ブロードキャストでは含めない (権限情報を他人に流さない)。
    """
    cid = await _db(get_announce_channel_sql, g.id)
    ch = g.get_channel(int(cid)) if cid and str(cid).isdigit() else None
    payload = {
        "type": "guild_settings",
        "announce_channel_id": str(cid) if cid else None,
        "announce_channel_name": ch.name if isinstance(ch, discord.TextChannel) else "",
        "system_channel_name": g.system_channel.name if g.system_channel else "",
    }
    if include_manage:
        can = _guild_can_manage(g, user_id)
        payload["can_manage"] = can
        if can:
            payload["channels"] = [
                {"id": str(c.id), "name": c.name}
                for c in g.text_channels
                if c.permissions_for(g.me).send_messages
            ][:100]
    return payload


async def _publish_guild_settings(guild_id: int) -> None:
    """お知らせチャンネル変更後の guild 全員向け同期。"""
    g = client.get_guild(guild_id)
    if g is None:
        return
    await bus._broadcast(
        guild_id, await _guild_settings_payload(g, include_manage=False))


async def _notify(
    guild_id: int, level: str, message: str, user_id: str | None = None,
    req_id: str | None = None,
) -> None:
    """通知を流す。

    user_id を指定すると当該ユーザーのみに送る。
    省略時は contextvar 経由で現在の WS ハンドラが扱っているユーザー
    (= 操作を起こしたユーザー) にだけ送る。
    req_id を付けると Activity 側がどの要求への返事か突き合わせられる
    (キューへの曲追加の「取得中…」表示を解除するのに使う)。
    """
    target = user_id if user_id is not None else _current_ws_user.get()
    payload = {"type": "notify", "level": level, "message": message}
    if req_id:
        payload["req_id"] = req_id
    await bus._broadcast(guild_id, payload, user_id_filter=target)


async def _publish_playlists(guild_id: int, user_id: str) -> None:
    """確認画面用: 当該ユーザーの自作プレイリスト一覧を当該ユーザーにだけ送る。"""
    items = await _db(playlist_store.list_playlists, conn, user_id, str(guild_id))
    for it in items:
        cover = it.get("cover_url") or ""
        it["cover_url"] = _artwork_proxy_url(cover) if cover else ""
    prefs = await _db(playlist_store.get_prefs, conn, user_id)
    await bus._broadcast(
        guild_id,
        {
            "type": "playlists",
            "playlists": items,
            "auto_select_last": prefs["auto_select_last"],
            "last_used_playlist_id": prefs["last_used_playlist_id"],
            "auto_add_to_library": prefs.get("auto_add_to_library", False),
            "bg_tint_enabled": prefs.get("bg_tint_enabled", True),
            "audio_visualizer": prefs.get("audio_visualizer", "off"),
            "theme_color": prefs.get("theme_color"),
            "visualizer_tint_enabled": prefs.get("visualizer_tint_enabled", False),
        },
        user_id_filter=user_id,
    )


def _discord_avatar_url(user_id: str | None, avatar_hash: str | None) -> str:
    """Discord ユーザーアバターの URL を組み立てる。

    avatar_hash が None なら default avatar (5枚のジェネリック)。
    image proxy 経由で iframe CSP を回避する。
    """
    if not user_id:
        return ""
    if avatar_hash:
        ext = "gif" if avatar_hash.startswith("a_") else "png"
        raw = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.{ext}?size=64"
    else:
        try:
            idx = (int(user_id) >> 22) % 6
        except Exception:
            idx = 0
        raw = f"https://cdn.discordapp.com/embed/avatars/{idx}.png"
    return _artwork_proxy_url(raw)


async def _publish_library(guild_id: int) -> None:
    """ライブラリ用: guild 全員に共有の library 一覧を送る。"""
    items = await _db(playlist_store.list_library, conn, str(guild_id))
    for it in items:
        cover = it.get("cover_url") or ""
        it["cover_url"] = _artwork_proxy_url(cover) if cover else ""
        it["added_by_avatar_url"] = _discord_avatar_url(
            it.get("added_by_userid"), it.get("added_by_avatar"),
        )
    await bus._broadcast(guild_id, {
        "type": "library",
        "items": items,
    })


async def _publish_playlists_for_owner_if_needed(
    guild_id: int, actor_user_id: str, playlist_owner_id: str | None,
) -> None:
    """library 操作時に、所有者(actor 以外)の確認画面の in_library フラグも更新する。"""
    await _publish_playlists(guild_id, actor_user_id)
    if playlist_owner_id and playlist_owner_id != actor_user_id:
        await _publish_playlists(guild_id, playlist_owner_id)


# BC-PERF-06: 詳細画面は 1 ページ最大この曲数まで送り、残りはスクロールで追加取得する。
# 大半のプレイリスト (< 500曲) はこれで 1 メッセージに収まり従来どおり。
_DETAIL_PAGE_SIZE = 500


async def _publish_playlist_detail(
    guild_id: int, user_id: str, playlist_id: str, *, offset: int = 0,
) -> None:
    """指定 user の詳細画面用にプレイリストの曲リストを送る (BC-PERF-06: ページング)。

    offset=0 は 'playlist_detail' (メタ情報込み・置換)、offset>0 は
    'playlist_detail_page' (追記) として送る。要求した user にだけ届くようフィルタ。
    所有者 or この guild のライブラリに入っているプレイリストなら閲覧可。
    """
    offset = max(0, int(offset))
    try:
        pl = await _db(
            playlist_store.get_playlist, conn, playlist_id, offset,
            _DETAIL_PAGE_SIZE,
        )
    except Exception:
        log.exception('unexpected error')
        return
    if not pl:
        return
    if pl.user_id != str(user_id):
        # 他人所有でも、この guild の共有ライブラリに入っていれば閲覧可
        try:
            if not await _db(playlist_store.is_playlist_in_library,
                conn, str(guild_id), playlist_id,
            ):
                return
        except Exception:
            log.exception('unexpected error')
            return
    tracks = [
        {
            "title": t.title,
            "url": t.url,
            "artwork": _artwork_proxy_url(t.artwork) if t.artwork else "",
            "duration_ms": t.duration_ms,
        }
        for t in pl.tracks
    ]
    if offset > 0:
        # 追加ページ: 既存の詳細に追記する軽量メッセージ
        await bus._broadcast(guild_id, {
            "type": "playlist_detail_page",
            "id": pl.id,
            "offset": offset,
            "tracks": tracks,
        }, user_id_filter=str(user_id))
        return
    try:
        total = await _db(playlist_store.count_playlist_tracks, conn, playlist_id)
    except Exception:
        log.exception('unexpected error')
        total = len(tracks)
    cover_raw = pl.tracks[0].artwork if pl.tracks else ""
    await bus._broadcast(guild_id, {
        "type": "playlist_detail",
        "id": pl.id,
        "name": pl.name,
        "cover_url": _artwork_proxy_url(cover_raw) if cover_raw else "",
        "tracks": tracks,
        "total": total,
        "tags": list(pl.tags or []),
        "is_owner": pl.user_id == str(user_id),
    }, user_id_filter=str(user_id))


def _ytdl_info_to_playlist_track(info: dict) -> playlist_store.PlaylistTrack:
    duration_seconds = 0
    try:
        duration_seconds = int(info.get("duration") or 0)
    except (TypeError, ValueError):
        duration_seconds = 0
    return playlist_store.PlaylistTrack(
        title=_normalize_media_title(
            info.get("title"), info,
            info.get("url") or info.get("webpage_url") or ""),
        url=info.get("url") or info.get("webpage_url") or "",
        artwork=info.get("thumbnail") or _best_thumbnail_url(info.get("thumbnails")) or "",
        duration_ms=duration_seconds * 1000,
    )


async def _resolve_single_track_via_ytdl(url: str) -> playlist_store.PlaylistTrack | None:
    # SUNO は yt-dlp が未対応。専用 resolver でメタ情報を取る。
    if _SUNO_PATTERN.search(url):
        info = await _resolve_suno(url)
        if not info:
            return None
        return playlist_store.PlaylistTrack(
            title=info["title"] or "SUNO Track",
            url=url,  # 元の suno.com URL を保存 (再生時に from_url で再解決)
            artwork=info["artwork"] or "",
            duration_ms=int(info["duration_s"] or 0) * 1000,
        )
    # Jellyfin は専用 API でメタ情報を取る (内部URLなので SSRF 検証はしない)
    if _is_jellyfin_url(url):
        try:
            _name, tracks = await _resolve_jellyfin_album(url)
        except Exception:
            log.exception('unexpected error')
            return None
        return tracks[0] if tracks else None
    # BC-SEC-05: 任意 URL を yt-dlp に渡す前に内部到達を遮断
    await _assert_public_url(url)
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(
            None, lambda: ytdl.extract_info(url, download=False),
        )
    except Exception:
        log.exception('unexpected error')
        return None
    if "entries" in data:
        data = data["entries"][0]
    return playlist_store.PlaylistTrack(
        title=_normalize_media_title(data.get("title"), data, url),
        url=url,
        artwork=_best_thumbnail_url(data.get("thumbnails")) or data.get("thumbnail") or "",
        duration_ms=int(data.get("duration") or 0) * 1000,
    )


# ---- Activity: キューに 1 曲追加 ----
_QUEUE_ADD_URL_MAX = 2000
_QUEUE_ADD_TIMEOUT_S = 60.0
_queue_add_sem = asyncio.Semaphore(3)              # yt-dlp の同時解決数 (全 guild 合計)
_queue_add_inflight: set[tuple[int, str]] = set()  # (guild_id, user_id) ごとに 1 件まで
_REQ_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SPOTIFY_RE = re.compile(r"^https?://(?:open\.spotify\.com|spotify\.link)/", re.I)


_ie_classes: list | None = None


def _matching_extractor(url: str):
    """yt-dlp がこの URL に使う抽出器クラス (最後は必ず Generic が当たる)。"""
    global _ie_classes
    if _ie_classes is None:
        from yt_dlp.extractor import gen_extractor_classes
        _ie_classes = list(gen_extractor_classes())
    return next((c for c in _ie_classes if c.suitable(url)), None)


def _is_collection_url(url: str) -> bool:
    """1 曲ではなく再生リスト / アルバム / チャンネル / 検索結果などの URL か。
    (yt-dlp に渡すと全曲を展開してしまい、数分〜数時間かかることがある)"""
    try:
        p = urllib_parse.urlsplit(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    path = p.path or ""
    if host == "youtube.com" or host.endswith(".youtube.com"):
        if path.startswith(("/playlist", "/@", "/channel/", "/c/", "/user/", "/browse/")):
            return True
        q = urllib_parse.parse_qs(p.query)
        if "list" in q and "v" not in q and path in ("/watch", "/embed/videoseries"):
            return True
    elif host.endswith("bandcamp.com"):
        if not path.startswith("/track/"):
            return True
    elif host in ("soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"):
        segs = [s for s in path.split("/") if s]
        if len(segs) < 2 or segs[1] in (
                "sets", "tracks", "likes", "reposts", "albums", "popular-tracks", "toptracks"):
            return True
    elif host.endswith("nicovideo.jp"):
        if re.search(r"/(?:mylist|series)/", path):
            return True
    # 上で拾えない形 (YouTube の検索結果・ハッシュタグ・独自 URL のチャンネル、ニコニコの
    # ユーザー/タグ/検索、Vimeo チャンネル等) は yt-dlp が選ぶ抽出器の種類で判定する
    try:
        ie = _matching_extractor(url)
    except Exception:
        log.exception("extractor lookup failed")
        return False
    if ie is None:
        return False
    if getattr(ie, "_RETURN_TYPE", None) == "playlist":
        return True
    if ie.ie_key() == "YoutubeTab":
        # watch?v=X&list=Y は noplaylist で 1 曲になる。それ以外 (チャンネル等) は一覧
        return "v" not in urllib_parse.parse_qs(p.query)
    return False


async def _resolve_track_for_queue(url: str) -> playlist_store.PlaylistTrack:
    """キューに入れる 1 曲を解決する。失敗したら利用者向けの文言で ValueError。"""
    if _SPOTIFY_RE.match(url):
        raise ValueError("Spotify の URL には対応していません")
    m = _SUNO_PATTERN.match(url)
    if m:
        t = await _resolve_single_track_via_ytdl(m.group(0))
        if t is None:
            raise ValueError("URL を解決できませんでした")
        return t
    if _is_jellyfin_url(url):
        # 内部 URL なので SSRF 検証より前に分岐する
        try:
            _name, tracks = await _resolve_jellyfin_album(url)
        except ValueError as e:
            raise ValueError(str(e)) from None
        except Exception:
            log.exception("queue add: jellyfin resolve failed")
            raise ValueError("URL を解決できませんでした") from None
        if len(tracks) != 1:
            raise ValueError("アルバム / プレイリストの URL は追加できません。曲の URL を指定してください")
        return tracks[0]
    # 抽出器一覧の初回構築が重いので event loop の外で判定する
    if await asyncio.to_thread(_is_collection_url, url):
        raise ValueError("再生リスト・アルバム・チャンネル・検索結果の URL は追加できません。曲の URL を指定してください")
    try:
        # BC-SEC-05: 任意 URL を yt-dlp に渡す前に内部到達を遮断 (SsrfBlocked は ValueError)
        await _assert_public_url(url)
    except ValueError:
        raise ValueError("この URL は追加できません") from None

    async def _extract() -> dict:
        # 同時解決数の枠は「抽出が本当に終わるまで」持つ。タイムアウトで待つのを
        # やめても yt-dlp のスレッドは止められないので、待ち手側で枠を返すと
        # スレッドが際限なく積み上がる。
        await _queue_add_sem.acquire()
        # 再生用の抽出キャッシュも温める
        inner = asyncio.ensure_future(_extract_ytdl_cached(url, stream=True))

        def _release(f) -> None:
            _queue_add_sem.release()
            if not f.cancelled():
                f.exception()  # 取り出し済みにして "never retrieved" 警告を出さない
        inner.add_done_callback(_release)
        # タイムアウトしても抽出自体は止めない (同じ URL を再生側が待っていることがある)。
        # asyncio.wait は待ち手がキャンセルされても inner をキャンセルしない
        await asyncio.wait((inner,))
        return inner.result()
    try:
        data = await asyncio.wait_for(_extract(), _QUEUE_ADD_TIMEOUT_S)
    except TimeoutError:
        raise ValueError("URL の解析がタイムアウトしました") from None
    except Exception:
        log.exception("queue add: extract failed")
        raise ValueError("URL を解決できませんでした") from None
    if data.get("is_live"):
        raise ValueError("ライブ配信は追加できません。/live コマンドを使ってください")
    store_url = url
    if (urllib_parse.urlsplit(url).hostname or "").lower().endswith(("nicovideo.jp", "nico.ms")):
        # from_url は "https://www.nicovideo.jp/" で始まる URL だけをダウンロード再生に回す
        store_url = data.get("webpage_url") or url
    return playlist_store.PlaylistTrack(
        title=_normalize_media_title(data.get("title"), data, url),
        url=store_url,
        artwork=_best_thumbnail_url(data.get("thumbnails")) or data.get("thumbnail") or "",
        duration_ms=int(data.get("duration") or 0) * 1000,
    )


def _yt_video_to_playlist_track(v: dict) -> playlist_store.PlaylistTrack | None:
    if v.get("status", {}).get("privacyStatus") == "unlisted":
        return None
    thumbs = v["snippet"]["thumbnails"]
    thumb = (thumbs.get("maxres") or thumbs.get("standard")
             or thumbs.get("high") or thumbs.get("medium")
             or thumbs.get("default"))
    pttn_time = re.compile(r"PT(\d+H)?(\d+M)?(\d+S)?")
    m = pttn_time.search(v["contentDetails"]["duration"])
    keys = ["hours", "minutes", "seconds"]
    kw = {k: 0 if vv is None else int(vv[:-1])
          for k, vv in zip(keys, m.groups())}
    dur_sec = timedelta(**kw).total_seconds()
    return playlist_store.PlaylistTrack(
        title=v["snippet"]["title"],
        url=f"https://www.youtube.com/watch?v={v['id']}",
        artwork=thumb["url"] if thumb else "",
        duration_ms=int(dur_sec) * 1000,
    )


async def _resolve_youtube_playlist(
    args: str,
    progress_cb=None,
) -> tuple[str, list[playlist_store.PlaylistTrack]]:
    """YouTube 再生リスト URL から (推定タイトル, トラック群) を返す。

    手順:
      1. playlists.list で名前と総曲数を取得 (1 unit)
      2. playlistItems.list でページネートしながら videoId をすべて集める
         (ceil(N/50) units)
      3. **videos.list を 50 件バッチで呼ぶ** (ceil(N/50) units)
         → 50 曲 = 2 unit、200 曲 = 5 unit
         以前は各 video を 1 件ずつ呼んでいた (50 unit/プレイリスト) のを
         50 件まとめて取得することでクォータ 25x 削減 + 速度 10x 改善

    削除済み / 非公開動画は videos.list の response から自動的に省かれる。

    progress_cb(current, total, name) を渡すと取り込み中に進捗を通知する。
    """
    list_id_match = re.search(r"list=([^?&]+)", args)
    if not list_id_match:
        raise ValueError("YouTube プレイリストの list= が見つかりません")
    list_id = list_id_match.group(1)

    loop = asyncio.get_event_loop()

    # 再生リスト名 + 総曲数を取る。googleapiclient は同期 HTTP なので
    # run_in_executor で別スレッド送りにして event loop を止めない。
    playlist_name = "YouTube Playlist"
    total_estimate = 0
    try:
        meta = await loop.run_in_executor(
            None,
            lambda: youtube.playlists().list(
                part="snippet,contentDetails", id=list_id,
            ).execute(),
        )
        if meta.get("items"):
            it0 = meta["items"][0]
            playlist_name = it0["snippet"].get("title") or playlist_name
            total_estimate = int(
                (it0.get("contentDetails") or {}).get("itemCount") or 0,
            )
    except Exception:
        log.exception('unexpected error')

    if progress_cb:
        try:
            await progress_cb(0, total_estimate, playlist_name)
        except Exception:
            log.exception('unexpected error')

    # フェーズ 1: videoId をすべて集める (順序保持)
    def _fetch_playlist_items(page_token: str | None) -> dict:
        kwargs: dict = dict(part="contentDetails", playlistId=list_id, maxResults=50)
        if page_token:
            kwargs["pageToken"] = page_token
        return youtube.playlistItems().list(**kwargs).execute()

    video_ids: list[str] = []
    page_token: str | None = None
    while True:
        response = await loop.run_in_executor(
            None, _fetch_playlist_items, page_token,
        )
        for res in response.get("items", []):
            vid = (res.get("contentDetails") or {}).get("videoId")
            if vid:
                video_ids.append(vid)
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    if not video_ids:
        return playlist_name, []

    # フェーズ 2: videos.list を 50 件バッチで呼ぶ
    def _fetch_videos_batch(ids: list[str]) -> dict:
        return youtube.videos().list(
            part=["snippet", "contentDetails", "status"],
            id=",".join(ids),
            maxResults=len(ids),
        ).execute()

    items: list[playlist_store.PlaylistTrack] = []
    BATCH = 50
    for i in range(0, len(video_ids), BATCH):
        chunk = video_ids[i:i + BATCH]
        try:
            v_resp = await loop.run_in_executor(None, _fetch_videos_batch, chunk)
        except Exception:
            log.exception('unexpected error')
            continue
        # 返ってきた items を id→video の dict にして、元の chunk 順で取り出す。
        # (削除/非公開動画は response から落ちるので by_id.get(vid) が None になる)
        by_id = {
            v.get("id"): v for v in v_resp.get("items", []) if v.get("id")
        }
        for vid in chunk:
            v = by_id.get(vid)
            if v is None:
                continue
            track = _yt_video_to_playlist_track(v)
            if track is not None:
                items.append(track)
        if progress_cb:
            try:
                await progress_cb(
                    len(items),
                    max(total_estimate, len(video_ids)),
                    playlist_name,
                )
            except Exception:
                log.exception('unexpected error')

    return playlist_name, items


# niconico マイリスト URL の検出 (新旧両方の URL 形式に対応)
_NICONICO_MYLIST_PATTERN = re.compile(
    r"nicovideo\.jp/(?:user/\d+/)?mylist/(\d+)", re.IGNORECASE,
)


async def _resolve_niconico_mylist(
    url: str,
    progress_cb=None,
) -> tuple[str, list[playlist_store.PlaylistTrack]]:
    """niconico マイリスト URL から (マイリスト名, トラック群) を返す。

    公開マイリストは認証不要で `nvapi.nicovideo.jp/v2/mylists/{id}` から取得可。
    `X-Frontend-Id: 6` ヘッダだけ必要。ページサイズ 100、`hasNext` ループで全件取得。
    """
    m = _NICONICO_MYLIST_PATTERN.search(url)
    if not m:
        raise ValueError("niconico マイリスト ID が URL から取れません")
    mylist_id = m.group(1)

    import httpx
    headers = {
        "X-Frontend-Id": "6",
        "X-Frontend-Version": "0",
        "User-Agent": "Mozilla/5.0 (compatible; SmileMusic/3.0)",
    }
    mylist_name = "niconico マイリスト"
    tracks: list[playlist_store.PlaylistTrack] = []
    total_estimate = 0

    PAGE_SIZE = 100
    page = 1
    try:
        async with httpx.AsyncClient(
            timeout=15.0, headers=headers, follow_redirects=True,
        ) as client:
            while True:
                r = await client.get(
                    f"https://nvapi.nicovideo.jp/v2/mylists/{mylist_id}",
                    params={"pageSize": PAGE_SIZE, "page": page},
                )
                if r.status_code != 200:
                    raise ValueError(
                        f"niconico マイリスト取得失敗 ({r.status_code})",
                    )
                data = (r.json().get("data") or {}).get("mylist") or {}
                if page == 1:
                    mylist_name = data.get("name") or mylist_name
                    total_estimate = int(data.get("totalItemCount") or 0)
                    if progress_cb:
                        try:
                            await progress_cb(0, total_estimate, mylist_name)
                        except Exception:
                            log.exception('unexpected error')
                for it in (data.get("items") or []):
                    video = it.get("video") or {}
                    vid = video.get("id") or it.get("watchId") or ""
                    if not vid:
                        continue
                    # 削除済み / 非公開動画は API が item shell だけ残し、
                    # video.duration=0、status が "regular"/"isVisible" 以外、
                    # title が空などの形で返ってくる。
                    # duration <= 0 で弾くのが一番確実。
                    duration_s = int(video.get("duration") or 0)
                    if duration_s <= 0:
                        continue
                    title = video.get("title") or ""
                    if not title:
                        continue
                    # item.status が明らかな非可視ステータスなら除外
                    # (regular / isVisible 以外を念のため弾く)
                    raw_status = it.get("status")
                    if isinstance(raw_status, str) and raw_status.lower() in {
                        "deleted", "private", "hidden", "restricted",
                    }:
                        continue
                    thumb_obj = video.get("thumbnail") or {}
                    thumb = (
                        thumb_obj.get("largeUrl")
                        or thumb_obj.get("url")
                        or thumb_obj.get("middleUrl")
                        or ""
                    )
                    tracks.append(playlist_store.PlaylistTrack(
                        title=title,
                        url=f"https://www.nicovideo.jp/watch/{vid}",
                        artwork=thumb,
                        duration_ms=duration_s * 1000,
                    ))
                if progress_cb:
                    try:
                        await progress_cb(
                            len(tracks),
                            max(total_estimate, len(tracks)),
                            mylist_name,
                        )
                    except Exception:
                        log.exception('unexpected error')
                if not data.get("hasNext"):
                    break
                page += 1
                if page > 50:  # 安全弁: 最大 5000 件
                    break
    except httpx.HTTPError as e:
        log.exception('unexpected error')
        raise ValueError(f"niconico マイリスト取得失敗: {e}") from e

    return mylist_name, tracks


async def _resolve_bandcamp_album_fast(
    url: str,
) -> tuple[str, list[playlist_store.PlaylistTrack]] | None:
    """Bandcamp アルバムページの埋め込み JSON (data-tralbum) から全曲メタを一括取得。

    yt-dlp は per-track HTTP を 16 回回すので遅い。アルバムページ自体に
    全曲のタイトル/長さ/URL が JSON で埋まっているので、これを 1 回の GET で
    取り出す。失敗したら None を返して呼び元で yt-dlp フォールバック。
    """
    try:
        # BC-SEC-05: 内部到達を遮断
        await _assert_public_url(url)
        import httpx
        import html as html_module
        async with httpx.AsyncClient(
            timeout=15.0, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SmileMusic/3.0)"},
        ) as client:
            r = await client.get(url)
    except Exception:
        log.exception('unexpected error')
        return None
    if r.status_code != 200:
        return None
    page = r.text
    # data-tralbum=" ... " (HTML エンティティでエスケープされた JSON)
    m = re.search(r'data-tralbum="([^"]+)"', page)
    if not m:
        return None
    try:
        raw = html_module.unescape(m.group(1))
        data = json.loads(raw)
    except (json.JSONDecodeError, Exception):
        log.exception('unexpected error')
        return None
    # アルバムタイトル
    album_title = ((data.get("current") or {}).get("title")) or "Bandcamp Album"
    # アートワーク (art_id の "_10" は 1200px サイズ)
    cover = ""
    art_id = data.get("art_id")
    if art_id:
        cover = f"https://f4.bcbits.com/img/a{art_id}_10.jpg"
    else:
        og_m = re.search(
            r'<meta\s+property="og:image"\s+content="([^"]+)"', page,
        )
        if og_m:
            cover = og_m.group(1)
    # トラック URL のベース (artist host)
    base_match = re.match(r"(https?://[^/]+)", url)
    base_url = base_match.group(1) if base_match else ""
    # トラックリスト
    tracks: list[playlist_store.PlaylistTrack] = []
    for tinfo in (data.get("trackinfo") or []):
        title = tinfo.get("title") or "Unknown"
        try:
            duration = int(float(tinfo.get("duration") or 0))
        except (TypeError, ValueError):
            duration = 0
        title_link = tinfo.get("title_link") or ""
        if not title_link or not base_url:
            continue
        tracks.append(playlist_store.PlaylistTrack(
            title=title,
            url=base_url + title_link,
            artwork=cover,
            duration_ms=duration * 1000,
        ))
    if not tracks:
        return None
    return album_title, tracks


async def _resolve_bandcamp_album(
    url: str,
) -> tuple[str, list[playlist_store.PlaylistTrack]]:
    """Bandcamp アルバム URL から (アルバム名, トラック群) を返す。

    まず埋め込み JSON 経由の高速パスを試し、失敗したら yt-dlp にフォールバック。
    yt-dlp 既定の ytdl は noplaylist=True なので、専用に一時インスタンスを作る。
    """
    # 高速パス: アルバムページ 1 回で全曲取得
    try:
        fast = await _resolve_bandcamp_album_fast(url)
    except Exception:
        log.exception('unexpected error')
        fast = None
    if fast is not None:
        return fast

    # フォールバック: yt-dlp (アルバム + 各トラック計 N+1 回 HTTP、遅い)
    log.warning("bandcamp fast path failed for %s, falling back to yt-dlp", url)
    loop = asyncio.get_event_loop()
    bc_options = dict(ytdl_format_options)
    bc_options["noplaylist"] = False
    bc_options["extract_flat"] = False
    bc_ytdl = yt_dlp.YoutubeDL(bc_options)
    try:
        data = await loop.run_in_executor(
            None, lambda: bc_ytdl.extract_info(url, download=False),
        )
    except Exception as e:
        log.exception('unexpected error')
        raise ValueError(f"Bandcamp の取り込みに失敗しました ({e})") from e

    entries = data.get("entries") if isinstance(data, dict) else None
    if not entries:
        # 単曲 URL (track ページ) を渡された場合は entries が無い
        raise ValueError(
            "Bandcamp アルバム URL を指定してください "
            "(例: https://artist.bandcamp.com/album/...)",
        )

    album_title = (
        data.get("title")
        or data.get("playlist_title")
        or "Bandcamp Album"
    )
    tracks: list[playlist_store.PlaylistTrack] = []
    for entry in entries:
        if not entry:
            continue
        track_url = (
            entry.get("webpage_url")
            or entry.get("original_url")
            or entry.get("url")
        )
        if not track_url:
            continue
        title = entry.get("title") or "Unknown"
        thumb = (
            _best_thumbnail_url(entry.get("thumbnails"))
            or entry.get("thumbnail")
            or ""
        )
        duration = int(entry.get("duration") or 0)
        tracks.append(playlist_store.PlaylistTrack(
            title=title,
            url=track_url,
            artwork=thumb,
            duration_ms=duration * 1000,
        ))
    return album_title, tracks


def _detect_import_source(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "bandcamp.com" in u:
        return "bandcamp"
    if _NICONICO_MYLIST_PATTERN.search(url):
        return "niconico"
    if _is_jellyfin_url(url):
        return "jellyfin"
    return ""


def _can_control_playback(g, vc, user_id) -> bool:
    """BC-SEC-04: 操作ユーザーが BOT の在室 VC に同席しているか。
    BOT がどの VC にもいなければ制御対象が無いので許可 (誤ブロック回避)。"""
    if vc is None or getattr(vc, "channel", None) is None:
        return True
    try:
        member = g.get_member(int(user_id))
    except (TypeError, ValueError):
        return False
    return bool(
        member and member.voice and member.voice.channel
        and member.voice.channel.id == vc.channel.id
    )


# === BC-MAINT-01: _handle_ws_command を 1 コマンド=1 ハンドラのディスパッチ表に分割 ===
# 各ハンドラは _WsCtx を受け取り共通ローカルをアンパックする (本体は元の分岐と同一)。
@dataclass
class _WsCtx:
    guild_id: int
    user: object
    msg: dict
    g: object
    state: dict
    vc: object
    user_id: object
    cmd: object


# 再生制御系 (BC-SEC-04: BOT と同じ VC 在室を要求)
_WS_PLAYBACK_CONTROL = {"pause", "play", "skip", "prev", "seek", "set_normalize"}
_WS_HANDLERS: dict = {}


def _ws(*names):
    def deco(fn):
        for n in names:
            _WS_HANDLERS[n] = fn
        return fn
    return deco


@_ws("pause")
async def _wsh_pause(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    # 擬似 pause: vc.pause() ではなく player._paused を立てる。送出は継続 (無音) され、
    # RTP timestamp が wall-clock に追随するので、再開/曲送り時の累積遅延が起きない。
    # フラグは次の read() (≤20ms) で反映されるので操作は即時。
    player = state.get("player")
    if player is not None and vc and vc.is_playing() and not getattr(player, "_paused", False):
        player._paused = True
        await _publish_state(guild_id)
    else:
        log.info("ws cmd pause: not playing / already paused")
    return


@_ws("play")
async def _wsh_play(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    player = state.get("player")
    if player is not None and getattr(player, "_paused", False):
        # 擬似 pause からの再開
        player._paused = False
        await _publish_state(guild_id)
    elif vc and vc.is_playing():
        # 何もしない (再生中)
        pass
    else:
        # idle → 現在モードで再生開始
        if state.get("mode") == "playlist":
            _mark_playlist_starter(state, user_id, guild=g, user=user)
        asyncio.create_task(_start_playback_via_ws(guild_id))
    return


@_ws("skip")
async def _wsh_skip(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    if vc and (vc.is_playing() or vc.is_paused()):
        if state.get("mode") == "playlist":
            state["_playlist_advance"] = "next"
        vc.stop()
    return


@_ws("prev")
async def _wsh_prev(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    # マイプレイリスト専用
    if state.get("mode") != "playlist":
        return
    _mark_playlist_starter(state, user_id, guild=g, user=user)
    if vc and (vc.is_playing() or vc.is_paused()):
        state["_playlist_advance"] = "prev"
        vc.stop()
    else:
        # idle 中なら index を戻して再生開始
        tracks = state.get("playlist_tracks") or []
        if tracks:
            idx = state.get("playlist_index", 0)
            new_idx = idx - 1
            if new_idx < 0:
                new_idx = len(tracks) - 1 if state.get("playlist_loop") else 0
            state["playlist_index"] = new_idx
            asyncio.create_task(_start_playback_via_ws(guild_id))
    return


@_ws("seek")
async def _wsh_seek(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    position_ms = int(msg.get("position_ms", 0))
    player = state.get("player")
    if player is None or vc is None:
        return
    seek_str = _ms_to_seek_str(position_ms)
    # ffmpeg の引数はプレイヤーが構築時のものを使い回す (ローカルファイルなら
    # -reconnect 無し、ノーマライズ中なら loudnorm 付き)。
    # vc.pause()/resume() は使わない: 送出が止まると RTP timestamp が wall-clock から
    # ズレて累積遅延の原因になる。seek() が ffmpeg 差し替え中の隙間を
    # _seek_silence_frames(無音) で埋めるので、送出を継続したまま安全にシークできる。
    try:
        player.seek(seek_time=seek_str)
    except PlayerFinished:
        pass  # 再生が終わった曲へのシーク (次曲のロード中など)。何もしない
    except Exception:
        log.exception('unexpected error')
    finally:
        await _publish_state(guild_id)
    return


@_ws("set_mode")
async def _wsh_set_mode(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    new_mode = msg.get("mode")
    if new_mode not in ("queue", "playlist"):
        return
    old_mode = state.get("mode", "queue")
    if old_mode == new_mode:
        await _publish_state(guild_id)
        return
    state["mode"] = new_mode
    if new_mode == "playlist":
        _mark_playlist_starter(state, user_id, guild=g, user=user)
    # 既存の再生は停止する。
    # playlist → 他モードの場合は _playlist_advance="stay" で index を据え置く。
    # queue → 他モードの場合は loop 側で mode != "queue" を検出して pop しない。
    if vc and (vc.is_playing() or vc.is_paused()):
        if old_mode == "playlist":
            state["_playlist_advance"] = "stay"
        vc.stop()
    state["player"] = None  # 位置を 0 にリセット
    # mode 切替で auto-select が有効なら直近のプレイリストを引き当てる
    if new_mode == "playlist" and user_id:
        try:
            prefs = await _db(playlist_store.get_prefs, conn, str(user_id))
            if prefs["auto_select_last"] and prefs["last_used_playlist_id"]:
                pl = await _db(playlist_store.get_playlist, conn, prefs["last_used_playlist_id"])
                if pl and pl.user_id == str(user_id):
                    state["playlist_id"] = pl.id
                    state["playlist_name"] = pl.name
                    state["playlist_tracks"] = [t.to_dict() for t in pl.tracks]
                    state["playlist_index"] = 0
                    _mark_playlist_starter(state, user_id, pl.user_id, guild=g, user=user)
        except Exception:
            log.exception('unexpected error')
    await _publish_state(guild_id)
    return


@_ws("set_loop_playlist")
async def _wsh_set_loop_playlist(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    state["playlist_loop"] = bool(msg.get("enabled"))
    await _publish_state(guild_id)
    return


@_ws("set_loop_mode")
async def _wsh_set_loop_mode(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    # 統合ループモード切替: 'off' | 'all' | 'one'
    # 現在 mode (queue / playlist) に応じてフラグ群を上書きする。
    kind = msg.get("mode")
    if kind not in ("off", "all", "one"):
        return
    if state.get("mode") == "playlist":
        state["playlist_loop"] = (kind == "all")
        state["playlist_loop_single"] = (kind == "one")
    else:
        state["has_loop_queue"] = (kind == "all")
        state["has_loop"] = (kind == "one")
    await _publish_state(guild_id)
    return


@_ws("set_normalize")
async def _wsh_set_normalize(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    state["normalize"] = bool(msg.get("enabled"))
    state["_normalize_toggled_at"] = time.monotonic()
    # キューモードでは先頭が今鳴っている (またはロード中の) 曲。押したのはこの曲なので
    # 曲ごとの指定を「未指定」に戻し、1 曲ループ / キューループで繰り返しても /play の
    # 指定に戻らず、以後もトグルに従うようにする。
    if state.get("mode") != "playlist" and state.get("music_queue"):
        state["music_queue"][0]["normalize"] = None
    # 再生中の曲にも反映する (以前は次の曲からしか効かなかった)。loudnorm は ffmpeg の
    # フィルタなので今の位置から ffmpeg を起動し直す。連打で毎回起動し直さないよう、
    # 最後の操作から少し待ってまとめて 1 回だけ反映する。
    prev = state.get("_normalize_apply_task")
    if prev is not None and not prev.done():
        prev.cancel()
    state["_normalize_apply_task"] = asyncio.create_task(
        _apply_normalize_to_current(guild_id, state.get("player")))
    await _publish_state(guild_id)
    return


async def _apply_normalize_to_current(guild_id: int, target) -> None:
    """トグルの値を、押されたときに鳴っていた曲 (target) に反映する (曲ごとの指定より優先。
    押したのは今の曲なので)。待っている間に次の曲へ移っていたら何もしない (次の曲は
    play_music がロード中のトグルだけを見て合わせる)。ロード中も何もしない。ライブは対象外。"""
    await asyncio.sleep(0.8)
    state = guild_table.get(guild_id)
    if not state:
        return
    player = state.get("player")
    g = client.get_guild(guild_id)
    vc = g.voice_client if g else None
    if (player is not None and player is target and hasattr(player, "set_normalize")
            and not getattr(player, "is_live", False) and vc is not None and vc.is_playing()):
        try:
            player.set_normalize(bool(state.get("normalize")), restart=True)
        except PlayerFinished:
            pass
        except Exception:
            # 起動し直せなかった場合は設定も元のまま (表示は下の publish で実際の状態に戻る)
            log.exception('apply normalize to current track failed')
    if state.get("_normalize_apply_task") is asyncio.current_task():
        state["_normalize_apply_task"] = None
    await _publish_state(guild_id)


@_ws("list_playlists")
async def _wsh_list_playlists(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    await _publish_playlists(guild_id, str(user_id))
    await _publish_library(guild_id)
    return


@_ws("select_playlist")
async def _wsh_select_playlist(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    pid = msg.get("playlist_id")
    if not _valid_uuid(pid):
        return
    try:
        pl = await _db(playlist_store.get_playlist, conn, pid)
    except Exception:
        log.exception('unexpected error')
        await _notify(guild_id, "error", "プレイリストの取得に失敗しました")
        return
    if not pl:
        await _notify(guild_id, "error", "プレイリストが見つかりません")
        return
    if pl.user_id != str(user_id):
        if not await _db(playlist_store.is_playlist_in_library, conn, str(guild_id), pid):
            await _notify(guild_id, "error", "プレイリストが見つかりません")
            return
    # アクティブ化 (mode は変えない。playlist モード中の差し替え or queue モードでもセットだけする)
    state["playlist_id"] = pl.id
    state["playlist_name"] = pl.name
    state["playlist_tracks"] = [t.to_dict() for t in pl.tracks]
    state["playlist_index"] = 0
    _mark_playlist_starter(state, user_id, pl.user_id, guild=g, user=user)
    await _db(playlist_store.set_last_used, conn, str(user_id), pl.id)
    # 既に playlist モード再生中なら、新しいプレイリストの先頭から仕切り直す
    if state.get("mode") == "playlist" and vc and (vc.is_playing() or vc.is_paused()):
        state["_playlist_advance"] = "select"
        vc.stop()
    await _publish_state(guild_id)
    return


@_ws("create_playlist")
async def _wsh_create_playlist(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    name = (msg.get("name") or "").strip()
    name = name[:255]  # 名前は表示用途のみなので 255 文字に丸める
    url = (msg.get("url") or "").strip()
    if not name or not url:
        await _notify(guild_id, "error", "プレイリスト名とURLを入力してください")
        return
    await _notify(guild_id, "info", "URL を解析中…")
    # プロセス記録
    ul = url.lower()
    is_bulk = (
        "bandcamp.com/album/" in ul
        or "youtube.com/playlist" in ul
        or "list=" in ul
        or _is_jellyfin_url(url)
        or bool(_NICONICO_MYLIST_PATTERN.search(url))
    )
    proc = None
    if is_bulk:
        proc = _process_create(
            guild_id, str(user_id), "import", url, name=f"新規登録: {name}",
        )
        await _publish_processes(guild_id, str(user_id))

    async def _proc_progress(cur: int, total: int, pname: str | None = None):
        if not proc:
            return
        _process_update(
            proc,
            progress_current=cur,
            progress_total=total,
            message=f"{cur}/{total or '?'} 取り込み中",
        )
        await _publish_processes(guild_id, str(user_id))

    async def _proc_fail(msg_text: str):
        if not proc:
            return
        _process_update(
            proc, status="error", message=msg_text, finished_at=_now_iso(),
        )
        await _publish_processes(guild_id, str(user_id))

    # Bandcamp アルバム / YouTube 再生リスト URL を渡された場合は全曲展開する
    tracks_seed: list[playlist_store.PlaylistTrack] = []
    if "bandcamp.com/album/" in ul:
        try:
            _album_name, bc_tracks = await _resolve_bandcamp_album(url)
            tracks_seed = bc_tracks
        except ValueError as e:
            await _notify(guild_id, "error", str(e))
            await _proc_fail(str(e))
            return
        except Exception as e:
            log.exception('unexpected error')
            await _notify(guild_id, "error", "Bandcamp アルバムの取り込みに失敗しました")
            await _proc_fail(f"Bandcamp 失敗: {e}")
            return
    elif "youtube.com/playlist" in ul or "list=" in ul:
        try:
            _list_name, yt_tracks = await _resolve_youtube_playlist(
                url, progress_cb=_proc_progress,
            )
            tracks_seed = yt_tracks
        except ValueError as e:
            await _notify(guild_id, "error", str(e))
            await _proc_fail(str(e))
            return
        except Exception as e:
            log.exception('unexpected error')
            await _notify(guild_id, "error", "YouTube プレイリストの取り込みに失敗しました")
            await _proc_fail(f"YouTube 失敗: {e}")
            return
    elif _is_jellyfin_url(url):
        try:
            _list_name, jf_tracks = await _resolve_jellyfin_album(url)
            tracks_seed = jf_tracks
        except ValueError as e:
            await _notify(guild_id, "error", str(e))
            await _proc_fail(str(e))
            return
        except Exception as e:
            log.exception('unexpected error')
            await _notify(guild_id, "error", "Jellyfin アルバムの取り込みに失敗しました")
            await _proc_fail(f"Jellyfin 失敗: {e}")
            return
    elif _NICONICO_MYLIST_PATTERN.search(url):
        try:
            _list_name, nc_tracks = await _resolve_niconico_mylist(
                url, progress_cb=_proc_progress,
            )
            tracks_seed = nc_tracks
        except ValueError as e:
            await _notify(guild_id, "error", str(e))
            await _proc_fail(str(e))
            return
        except Exception as e:
            log.exception('unexpected error')
            await _notify(guild_id, "error", "niconico マイリストの取り込みに失敗しました")
            await _proc_fail(f"niconico 失敗: {e}")
            return
    else:
        track = await _resolve_single_track_via_ytdl(url)
        if track is None:
            await _notify(guild_id, "error", "URL を解決できませんでした")
            return
        tracks_seed = [track]
    if not tracks_seed:
        await _notify(guild_id, "error", "曲が見つかりませんでした")
        await _proc_fail("曲が見つかりませんでした")
        return
    try:
        new_id = await _db(playlist_store.create_playlist, conn, str(user_id), name, tracks_seed)
    except playlist_store.PlaylistNameConflict:
        await _notify(guild_id, "error", f"プレイリスト名 '{name}' は既に使われています")
        await _proc_fail(f"同名のプレイリストが存在します")
        return
    except Exception as e:
        log.exception('unexpected error')
        await _notify(guild_id, "error", "プレイリストの作成に失敗しました")
        await _proc_fail(f"DB 保存に失敗: {e}")
        return
    # 自動ライブラリ追加が ON ならこの guild のライブラリへ登録
    added_to_library = False
    try:
        prefs = await _db(playlist_store.get_prefs, conn, str(user_id))
        if prefs.get("auto_add_to_library"):
            await _db(playlist_store.set_library_membership,
                conn, str(user_id), getattr(user, "username", ""),
                str(guild_id), new_id, True,
                avatar=getattr(user, "avatar", None),
            )
            added_to_library = True
    except Exception:
        log.exception('unexpected error')
    await _notify(
        guild_id, "success",
        f"プレイリスト '{name}' ({len(tracks_seed)}曲) を作成しました",
    )
    if proc:
        _process_update(
            proc,
            status="success",
            progress_current=len(tracks_seed),
            progress_total=len(tracks_seed),
            name=f"新規登録: {name}",
            message=f"{len(tracks_seed)} 曲を取り込みました",
            finished_at=_now_iso(),
        )
        await _publish_processes(guild_id, str(user_id))
    await _publish_playlists(guild_id, str(user_id))
    if added_to_library:
        await _publish_library(guild_id)
    return


@_ws("import_playlist_youtube", "import_playlist_url")
async def _wsh_import_playlist_youtube(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    url = (msg.get("url") or "").strip()
    custom_name = (msg.get("name") or "").strip()
    custom_name = custom_name[:255]  # 名前は表示用途のみなので 255 文字に丸める
    if not url:
        return
    source = _detect_import_source(url)
    if not source:
        await _notify(
            guild_id, "error",
            "対応していない URL です (YouTube プレイリスト / Bandcamp / Jellyfin)",
        )
        return
    # プロセス記録を開始
    source_label = {
        "youtube": "YouTube プレイリスト",
        "bandcamp": "Bandcamp アルバム",
        "jellyfin": "Jellyfin アルバム",
        "niconico": "niconico マイリスト",
    }.get(source, "URL")
    proc = _process_create(
        guild_id, str(user_id), "import", url, name=f"{source_label}: {url}",
    )
    await _publish_processes(guild_id, str(user_id))

    async def _proc_progress(cur: int, total: int, pname: str | None = None):
        _process_update(
            proc,
            progress_current=cur,
            progress_total=total,
            message=f"{cur}/{total or '?'} 取り込み中",
        )
        if pname:
            _process_update(proc, name=f"{source_label}: {pname}")
        await _publish_processes(guild_id, str(user_id))

    async def _proc_fail(msg_text: str):
        _process_update(
            proc,
            status="error",
            message=msg_text,
            finished_at=_now_iso(),
        )
        await _publish_processes(guild_id, str(user_id))

    if source == "youtube":
        await _notify(guild_id, "info", "YouTube から再生リストを取り込み中…")
        try:
            name, tracks = await _resolve_youtube_playlist(
                url, progress_cb=_proc_progress,
            )
        except ValueError as e:
            await _notify(guild_id, "error", str(e))
            await _proc_fail(str(e))
            return
        except Exception as e:
            log.exception('unexpected error')
            await _notify(guild_id, "error", "YouTube インポートに失敗しました")
            await _proc_fail(f"YouTube インポート失敗: {e}")
            return
    elif source == "bandcamp":
        await _notify(guild_id, "info", "Bandcamp からアルバムを取り込み中…")
        try:
            name, tracks = await _resolve_bandcamp_album(url)
        except ValueError as e:
            await _notify(guild_id, "error", str(e))
            await _proc_fail(str(e))
            return
        except Exception as e:
            log.exception('unexpected error')
            await _notify(guild_id, "error", "Bandcamp インポートに失敗しました")
            await _proc_fail(f"Bandcamp インポート失敗: {e}")
            return
    elif source == "jellyfin":
        await _notify(guild_id, "info", "Jellyfin からアルバムを取り込み中…")
        try:
            name, tracks = await _resolve_jellyfin_album(url)
        except ValueError as e:
            await _notify(guild_id, "error", str(e))
            await _proc_fail(str(e))
            return
        except Exception as e:
            log.exception('unexpected error')
            await _notify(guild_id, "error", "Jellyfin インポートに失敗しました")
            await _proc_fail(f"Jellyfin インポート失敗: {e}")
            return
    elif source == "niconico":
        await _notify(guild_id, "info", "niconico からマイリストを取り込み中…")
        try:
            name, tracks = await _resolve_niconico_mylist(
                url, progress_cb=_proc_progress,
            )
        except ValueError as e:
            await _notify(guild_id, "error", str(e))
            await _proc_fail(str(e))
            return
        except Exception as e:
            log.exception('unexpected error')
            await _notify(guild_id, "error", "niconico インポートに失敗しました")
            await _proc_fail(f"niconico インポート失敗: {e}")
            return
    if not tracks:
        await _notify(guild_id, "error", "曲が見つかりませんでした")
        await _proc_fail("曲が見つかりませんでした")
        return
    if custom_name:
        name = custom_name
    # 名前が衝突したら連番を付ける
    base = name
    suffix = 1
    new_id: str | None = None
    while True:
        try:
            new_id = await _db(playlist_store.create_playlist, conn, str(user_id), name, tracks)
            break
        except playlist_store.PlaylistNameConflict:
            suffix += 1
            name = f"{base} ({suffix})"
            if suffix > 50:
                await _notify(guild_id, "error", "同名のプレイリストが多すぎます")
                await _proc_fail("同名のプレイリストが多すぎます")
                return
        except Exception as e:
            log.exception('unexpected error')
            await _notify(guild_id, "error", "プレイリストの保存に失敗しました")
            await _proc_fail(f"DB 保存に失敗: {e}")
            return
    added_to_library = False
    if new_id:
        try:
            prefs = await _db(playlist_store.get_prefs, conn, str(user_id))
            if prefs.get("auto_add_to_library"):
                await _db(playlist_store.set_library_membership,
                    conn, str(user_id), getattr(user, "username", ""),
                    str(guild_id), new_id, True,
                    avatar=getattr(user, "avatar", None),
                )
                added_to_library = True
        except Exception:
            log.exception('unexpected error')
    await _notify(guild_id, "success", f"'{name}' ({len(tracks)}曲) を取り込みました")
    _process_update(
        proc,
        status="success",
        progress_current=len(tracks),
        progress_total=len(tracks),
        name=f"{source_label}: {name}",
        message=f"{len(tracks)} 曲を取り込みました",
        finished_at=_now_iso(),
    )
    await _publish_processes(guild_id, str(user_id))
    await _publish_playlists(guild_id, str(user_id))
    if added_to_library:
        await _publish_library(guild_id)
    return


@_ws("add_track_to_playlist")
async def _wsh_add_track_to_playlist(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    pid = msg.get("playlist_id")
    url = (msg.get("url") or "").strip()
    if not _valid_uuid(pid) or not url:
        return
    pl = await _db(playlist_store.get_playlist, conn, pid)
    if not pl or pl.user_id != str(user_id):
        await _notify(guild_id, "error", "プレイリストが見つかりません")
        return
    track = await _resolve_single_track_via_ytdl(url)
    if track is None:
        await _notify(guild_id, "error", "URL を解決できませんでした")
        return
    try:
        await _db(playlist_store.append_track, conn, pid, track)
    except Exception:
        log.exception('unexpected error')
        await _notify(guild_id, "error", "曲の追加に失敗しました")
        return
    await _notify(guild_id, "success", f"'{track.title}' を追加しました")
    await _publish_playlists(guild_id, str(user_id))
    # 詳細画面が開かれているかは分からないが、所有者にだけ追加なので一応送る
    await _publish_playlist_detail(guild_id, str(user_id), pid)
    # 現在このプレイリストを再生中なら state にも追加し、UP NEXT を更新
    if state.get("playlist_id") == pid:
        pl2 = await _db(playlist_store.get_playlist, conn, pid)
        if pl2:
            state["playlist_tracks"] = [t.to_dict() for t in pl2.tracks]
            if state.get("playlist_shuffle"):
                order = state.get("playlist_shuffle_order") or []
                # 既存 order に新規 index を追加(末尾にランダムで差し込む)
                existing = set(order)
                for i in range(len(pl2.tracks)):
                    if i not in existing:
                        order.insert(random.randint(0, len(order)), i)
                state["playlist_shuffle_order"] = order
            await _publish_state(guild_id)
    return


@_ws("remove_playlist_track")
async def _wsh_remove_playlist_track(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    pid = msg.get("playlist_id")
    position = msg.get("position")
    if not _valid_uuid(pid) or not isinstance(position, int) or position < 0:
        return
    pl = await _db(playlist_store.get_playlist, conn, pid)
    if not pl or pl.user_id != str(user_id):
        await _notify(guild_id, "error", "プレイリストが見つかりません")
        return
    if position >= len(pl.tracks):
        return
    try:
        await _db(playlist_store.remove_track, conn, pid, position)
    except Exception:
        log.exception('unexpected error')
        await _notify(guild_id, "error", "曲の削除に失敗しました")
        return
    await _notify(guild_id, "success", "曲を削除しました")
    await _publish_playlists(guild_id, str(user_id))
    await _publish_playlist_detail(guild_id, str(user_id), pid)
    # 再生中のプレイリストならスキップ/index 補正
    if state.get("playlist_id") == pid:
        pl2 = await _db(playlist_store.get_playlist, conn, pid)
        if pl2:
            old_idx = state.get("playlist_index", 0)
            cur_removed = (old_idx == position)
            state["playlist_tracks"] = [t.to_dict() for t in pl2.tracks]
            if cur_removed:
                if state["playlist_index"] >= len(pl2.tracks):
                    state["playlist_index"] = 0
                if vc and (vc.is_playing() or vc.is_paused()):
                    state["_playlist_advance"] = "select"
                    vc.stop()
            elif old_idx > position:
                state["playlist_index"] = old_idx - 1
            if state.get("playlist_shuffle"):
                order = list(range(len(pl2.tracks)))
                random.shuffle(order)
                state["playlist_shuffle_order"] = order
            await _publish_state(guild_id)
    return


@_ws("reorder_playlist_track")
async def _wsh_reorder_playlist_track(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    pid = msg.get("playlist_id")
    from_pos = msg.get("from")
    to_pos = msg.get("to")
    if (
        not _valid_uuid(pid)
        or not isinstance(from_pos, int)
        or not isinstance(to_pos, int)
        or from_pos < 0
        or to_pos < 0
    ):
        return
    pl = await _db(playlist_store.get_playlist, conn, pid)
    if not pl or pl.user_id != str(user_id):
        await _notify(guild_id, "error", "プレイリストが見つかりません")
        return
    if from_pos >= len(pl.tracks) or to_pos >= len(pl.tracks):
        return
    if from_pos == to_pos:
        return
    try:
        await _db(playlist_store.reorder_track, conn, pid, from_pos, to_pos)
    except Exception:
        log.exception('unexpected error')
        await _notify(guild_id, "error", "並び替えに失敗しました")
        return
    await _publish_playlists(guild_id, str(user_id))
    await _publish_playlist_detail(guild_id, str(user_id), pid)
    # 再生中なら index を追従
    if state.get("playlist_id") == pid:
        pl2 = await _db(playlist_store.get_playlist, conn, pid)
        if pl2:
            old_idx = state.get("playlist_index", 0)
            new_idx = old_idx
            if old_idx == from_pos:
                new_idx = to_pos
            elif from_pos < old_idx <= to_pos:
                new_idx = old_idx - 1
            elif to_pos <= old_idx < from_pos:
                new_idx = old_idx + 1
            state["playlist_index"] = new_idx
            state["playlist_tracks"] = [t.to_dict() for t in pl2.tracks]
            if state.get("playlist_shuffle"):
                order = list(range(len(pl2.tracks)))
                random.shuffle(order)
                state["playlist_shuffle_order"] = order
            await _publish_state(guild_id)
    return


@_ws("get_playlist_detail")
async def _wsh_get_playlist_detail(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    pid = msg.get("playlist_id")
    if not _valid_uuid(pid):
        return
    try:
        pl = await _db(playlist_store.get_playlist, conn, pid)
    except Exception:
        log.exception('unexpected error')
        await _notify(guild_id, "error", "プレイリストの取得に失敗しました")
        return
    if not pl:
        await _notify(guild_id, "error", "プレイリストが見つかりません")
        return
    # 所有者なら無条件で OK。他人所有でも、この guild の共有ライブラリに
    # 入っていれば読み取り可。
    if pl.user_id != str(user_id):
        if not await _db(playlist_store.is_playlist_in_library, conn, str(guild_id), pid):
            await _notify(guild_id, "error", "プレイリストが見つかりません")
            return
    await _publish_playlist_detail(guild_id, str(user_id), pid)
    return


@_ws("load_playlist_detail_page")
async def _wsh_load_playlist_detail_page(c):
    # BC-PERF-06: 詳細画面のスクロールで次ページを要求する
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    pid = msg.get("playlist_id")
    if not _valid_uuid(pid):
        return
    raw_off = msg.get("offset")
    offset = raw_off if isinstance(raw_off, int) and raw_off > 0 else 0
    if offset <= 0:
        return
    await _publish_playlist_detail(guild_id, str(user_id), pid, offset=offset)
    return


@_ws("add_playlist_to_queue")
async def _wsh_add_playlist_to_queue(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    pid = msg.get("playlist_id")
    if not _valid_uuid(pid):
        return
    try:
        pl = await _db(playlist_store.get_playlist, conn, pid)
    except Exception:
        log.exception('unexpected error')
        await _notify(guild_id, "error", "プレイリストの取得に失敗しました")
        return
    if not pl:
        await _notify(guild_id, "error", "プレイリストが見つかりません")
        return
    # 自分のプレイリスト or guild library に入っているものに限る
    if pl.user_id != str(user_id):
        if not await _db(playlist_store.is_playlist_in_library, conn, str(guild_id), pid):
            await _notify(guild_id, "error", "プレイリストが見つかりません")
            return
    if not pl.tracks:
        await _notify(guild_id, "info", "プレイリストに曲がありません")
        return
    # トラックを queue 用の info dict に変換
    # author は Discord Member。get_member で取れなくても author_id で履歴は残す
    author = g.get_member(int(user_id)) if g else None
    movie_infos: list[dict] = []
    for t in pl.tracks:
        dur_sec = max(1, (t.duration_ms or 0) // 1000)
        info = {
            "url": t.url,
            # 保存済みの汚染タイトルが履歴へ再流入するのをここで止める
            "title": _normalize_media_title(t.title, None, t.url),
            "image_url": t.artwork or "",
            "time": to_time(dur_sec),
            "normalize": None,  # 未指定 = Activity のトグルに従う
            "first_seek": None,
            "author": author,
            # member キャッシュミスで author=None でも履歴を残せるようにしておく
            "author_id": str(user_id),
        }
        movie_infos.append(info)
    state["music_queue"].extend(movie_infos)
    await _notify(
        guild_id, "success",
        f"'{pl.name}' ({len(movie_infos)}曲) をキューに追加しました",
    )
    await _publish_state(guild_id)
    # キューモード + VC 接続済み + ループ未稼働なら自動で再生開始
    if (state["mode"] == "queue"
            and vc and vc.is_connected()
            and not state.get("_queue_loop_running")):
        asyncio.create_task(_start_playback_via_ws(guild_id))
    return


@_ws("add_track_to_queue")
async def _wsh_add_track_to_queue(c):
    guild_id, user, msg, g, vc, user_id = c.guild_id, c.user, c.msg, c.g, c.vc, c.user_id
    uid = str(user_id)
    rid = msg.get("req_id")
    req_id = rid if isinstance(rid, str) and _REQ_ID_RE.match(rid) else None

    async def reply(level: str, text: str) -> None:
        # 1 要求につき結果は必ず 1 回、操作ユーザーにだけ req_id 付きで返す
        await _notify(guild_id, level, text, user_id=uid, req_id=req_id)

    raw = msg.get("url")
    url = raw.strip() if isinstance(raw, str) else ""
    if not url:
        await reply("error", "URL を入力してください")
        return
    if len(url) > _QUEUE_ADD_URL_MAX:
        await reply("error", "URL が長すぎます")
        return
    if not re.match(r"https?://", url, re.I):
        await reply("error", "https:// で始まる URL を入力してください")
        return
    # BC-SEC-04 と同じ条件。_WS_PLAYBACK_CONTROL で弾くと req_id を返せないのでここで見る
    if not _can_control_playback(g, vc, user_id):
        await reply("error", "BOT と同じボイスチャンネルに参加してから追加してください")
        return
    key = (guild_id, uid)
    if key in _queue_add_inflight:
        await reply("info", "前の曲を追加中です。完了してからもう一度お試しください")
        return
    _queue_add_inflight.add(key)

    async def _run() -> None:
        try:
            try:
                track = await _resolve_track_for_queue(url)
            except ValueError as e:
                await reply("error", str(e))
                return
            # 解決に最大 60 秒かかる。その間に BOT の切断等で guild の状態が作り直されて
            # いることがあるので取り直す
            state = _ensure_state(guild_id)
            g2 = client.get_guild(guild_id)
            vc2 = g2.voice_client if g2 else None
            info = {
                "url": track.url,
                "title": track.title,
                "image_url": track.artwork or "",
                # 0 秒は to_time(1) (= 長さ不明の番兵)。to_time は 31 日以上で例外になるので丸める
                "time": to_time(min(max(1, (track.duration_ms or 0) // 1000), 30 * 86400)),
                "normalize": None,  # 未指定 = Activity のトグルに従う
                "first_seek": None,
                "author": g2.get_member(int(user_id)) if g2 else None,
                "author_id": uid,
                "author_name": _resolve_account_name(g2, user_id, user),
            }
            state["music_queue"].append(info)
            try:
                await _publish_state(guild_id)  # 通知より先に一覧を更新しておく
            except Exception:
                # 追加自体は済んでいる。ここで失敗扱いにすると再試行で二重に入る
                log.exception("add_track_to_queue: publish failed")
            t = info["title"]
            if state.get("mode") != "queue":
                # プレイリストモードで再生されない理由は、キュー画面の入力欄の下に常に出ている
                # 案内 (detail-add-hint) が説明するので、ここでは繰り返さない
                await reply("success", f"'{t}' をキューに追加しました")
            elif not (vc2 and vc2.is_connected()):
                await reply("success", f"'{t}' をキューに追加しました (BOT がボイスチャンネルにいないため、まだ再生されません)")
            else:
                await reply("success", f"'{t}' をキューに追加しました")
                # 同時に 2 件追加が終わったときに再生開始を 2 回起動しない
                # (2 回目が 1 回目を取り消して 1 曲目のロードがやり直しになる)
                pending = state.get("_queue_start_task")
                if not state.get("_queue_loop_running") and not (pending and not pending.done()):
                    st = asyncio.create_task(_start_playback_via_ws(guild_id))
                    state["_queue_start_task"] = st
                    _bg_tasks.add(st)
                    st.add_done_callback(_bg_tasks.discard)
        except Exception:
            log.exception("add_track_to_queue failed")
            await reply("error", "キューへの追加に失敗しました")
        finally:
            _queue_add_inflight.discard(key)

    # 解決は数秒かかるので裏で回す (WS の受信ループはコマンドを 1 つずつ await するため、
    # 待つと同じ利用者の一時停止やスキップが詰まる)
    task = asyncio.create_task(_run())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return


@_ws("delete_playlist")
async def _wsh_delete_playlist(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    pid = msg.get("playlist_id")
    if not _valid_uuid(pid):
        return
    if await _db(playlist_store.delete_playlist, conn, str(user_id), pid):
        # アクティブだったら解除
        if state.get("playlist_id") == pid:
            state["playlist_id"] = None
            state["playlist_name"] = None
            state["playlist_tracks"] = []
            state["playlist_index"] = 0
            if vc and (vc.is_playing() or vc.is_paused()) and state.get("mode") == "playlist":
                state["_playlist_advance"] = "select"
                vc.stop()
            await _publish_state(guild_id)
        await _notify(guild_id, "success", "プレイリストを削除しました")
        await _publish_playlists(guild_id, str(user_id))
        # library cascade で消えているので全員に通知
        await _publish_library(guild_id)
    else:
        await _notify(guild_id, "error", "削除に失敗しました")
    return


@_ws("set_pref")
async def _wsh_set_pref(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    if isinstance(msg.get("auto_select_last"), bool):
        await _db(playlist_store.set_auto_select, conn, str(user_id), msg["auto_select_last"])
    if isinstance(msg.get("auto_add_to_library"), bool):
        await _db(playlist_store.set_auto_add_to_library,
            conn, str(user_id), msg["auto_add_to_library"],
        )
    if isinstance(msg.get("bg_tint_enabled"), bool):
        await _db(playlist_store.set_bg_tint_enabled,
            conn, str(user_id), msg["bg_tint_enabled"],
        )
    if isinstance(msg.get("audio_visualizer"), str):
        await _db(playlist_store.set_audio_visualizer,
            conn, str(user_id), msg["audio_visualizer"],
        )
    if "theme_color" in msg:
        tc = msg.get("theme_color")
        await _db(playlist_store.set_theme_color,
            conn, str(user_id), tc if isinstance(tc, str) or tc is None else None,
        )
    if isinstance(msg.get("visualizer_tint_enabled"), bool):
        await _db(playlist_store.set_visualizer_tint_enabled,
            conn, str(user_id), msg["visualizer_tint_enabled"],
        )
    await _publish_playlists(guild_id, str(user_id))
    return


@_ws("get_guild_settings")
async def _wsh_get_guild_settings(c):
    if c.g is None:
        return
    payload = await _guild_settings_payload(
        c.g, include_manage=True, user_id=c.user_id)
    await bus._broadcast(c.guild_id, payload, user_id_filter=str(c.user_id))
    return


@_ws("set_guild_pref")
async def _wsh_set_guild_pref(c):
    if c.g is None:
        return
    if not _guild_can_manage(c.g, c.user_id):
        await _notify(c.guild_id, "error", "この操作にはサーバー管理権限が必要です")
        return
    if "announce_channel_id" not in c.msg:
        return
    v = c.msg.get("announce_channel_id")
    if v is None:
        await _db(set_announce_channel_sql, c.guild_id, None)
    else:
        if not isinstance(v, str) or not v.isdigit():
            await _notify(c.guild_id, "error", "不正なチャンネル指定です")
            return
        ch = c.g.get_channel(int(v))
        if not isinstance(ch, discord.TextChannel) \
                or not ch.permissions_for(c.g.me).send_messages:
            await _notify(c.guild_id, "error", "BOTが送信できるテキストチャンネルを指定してください")
            return
        await _db(set_announce_channel_sql, c.guild_id, v)
    await _notify(c.guild_id, "success", "お知らせチャンネルを更新しました")
    await _publish_guild_settings(c.guild_id)
    return


@_ws("set_playlist_tags")
async def _wsh_set_playlist_tags(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    pid = msg.get("playlist_id")
    tags = msg.get("tags")
    if not _valid_uuid(pid) or not isinstance(tags, list):
        return
    tags = tags[:32]  # store側で16個に正規化されるが、巨大リストの全走査を避ける
    pl = await _db(playlist_store.get_playlist, conn, pid)
    if not pl or pl.user_id != str(user_id):
        await _notify(guild_id, "error", "プレイリストが見つかりません")
        return
    try:
        await _db(playlist_store.set_tags, conn, str(user_id), pid, tags)
    except Exception:
        log.exception('unexpected error')
        await _notify(guild_id, "error", "タグの保存に失敗しました")
        return
    await _publish_playlists(guild_id, str(user_id))
    # 詳細画面が開いている場合に即時反映するため再配信
    await _publish_playlist_detail(guild_id, str(user_id), pid)
    await _publish_library(guild_id)
    return


@_ws("rename_playlist")
async def _wsh_rename_playlist(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    pid = msg.get("playlist_id")
    new_name = (msg.get("new_name") or "").strip()
    new_name = new_name[:255]  # 名前は表示用途のみなので 255 文字に丸める
    if not _valid_uuid(pid) or not new_name:
        return
    pl = await _db(playlist_store.get_playlist, conn, pid)
    if not pl or pl.user_id != str(user_id):
        await _notify(guild_id, "error", "プレイリストが見つかりません")
        return
    try:
        ok = await _db(playlist_store.rename_playlist, conn, str(user_id), pid, new_name)
    except playlist_store.PlaylistNameConflict:
        await _notify(guild_id, "error", "同じ名前のプレイリストが既に存在します")
        return
    except Exception:
        log.exception('unexpected error')
        await _notify(guild_id, "error", "改名に失敗しました")
        return
    if not ok:
        return
    # アクティブなプレイリストならローカル state も更新
    if state.get("playlist_id") == pid:
        state["playlist_name"] = new_name
        await _publish_state(guild_id)
    await _notify(guild_id, "success", "プレイリスト名を変更しました")
    await _publish_playlists(guild_id, str(user_id))
    # 詳細画面が開かれていれば名前を更新
    await _publish_playlist_detail(guild_id, str(user_id), pid)
    # library にも入っていれば名前が変わるので全員に再配信
    await _publish_library(guild_id)
    return


@_ws("set_shuffle_playlist")
async def _wsh_set_shuffle_playlist(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    enabled = bool(msg.get("enabled"))
    state["playlist_shuffle"] = enabled
    if enabled:
        # 有効化時は order を作り直す (現在曲を最初に置かない: 自然なシャッフル)
        tracks = state.get("playlist_tracks") or []
        order = list(range(len(tracks)))
        random.shuffle(order)
        state["playlist_shuffle_order"] = order
    else:
        state["playlist_shuffle_order"] = []
    await _publish_state(guild_id)
    return


@_ws("set_loop_queue")
async def _wsh_set_loop_queue(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    state["has_loop_queue"] = bool(msg.get("enabled"))
    await _publish_state(guild_id)
    return


@_ws("playlist_jump")
async def _wsh_playlist_jump(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    # プレイリスト中の任意 index へジャンプして再生する。
    # Show More のリストから特定の曲をタップした時に使う。
    if state.get("mode") != "playlist":
        return
    target = msg.get("index")
    if not isinstance(target, int):
        return
    tracks = state.get("playlist_tracks") or []
    if not (0 <= target < len(tracks)):
        return
    state["playlist_index"] = target
    _mark_playlist_starter(state, user_id, guild=g, user=user)
    if vc and (vc.is_playing() or vc.is_paused()):
        # ループ走行中: 次回反復で新 index を使う
        state["_playlist_advance"] = "select"
        vc.stop()
    else:
        # idle: 新しく再生を開始
        asyncio.create_task(_start_playback_via_ws(guild_id))
    await _publish_state(guild_id)
    return


@_ws("set_library_membership")
async def _wsh_set_library_membership(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    pid = msg.get("playlist_id")
    in_library = bool(msg.get("in_library"))
    if not _valid_uuid(pid):
        return
    # 所有者を取得 (削除時に所有者の確認画面の in_library も更新する用)
    owner_id: str | None = None
    try:
        pl = await _db(playlist_store.get_playlist, conn, pid)
        if pl:
            owner_id = pl.user_id
    except Exception:
        log.exception('unexpected error')
    await _db(playlist_store.set_library_membership,
        conn, str(user_id), getattr(user, "username", ""),
        str(guild_id), pid, in_library,
        avatar=getattr(user, "avatar", None),
    )
    # library は guild 全員に broadcast
    await _publish_library(guild_id)
    # 確認画面は所有者にだけ反映が必要
    await _publish_playlists_for_owner_if_needed(
        guild_id, str(user_id), owner_id,
    )
    return


@_ws("library_select")
async def _wsh_library_select(c):
    guild_id, user, msg, g, state, vc, user_id, cmd = (
        c.guild_id, c.user, c.msg, c.g, c.state, c.vc, c.user_id, c.cmd)
    # ライブラリ項目クリック: モード切替 + 即時再生開始。
    kind = msg.get("kind")
    old_mode = state.get("mode", "queue")
    if kind == "queue":
        state["mode"] = "queue"
        if vc and (vc.is_playing() or vc.is_paused()):
            if old_mode == "playlist":
                state["_playlist_advance"] = "stay"
            vc.stop()
        state["player"] = None  # 位置を 0 にリセット
        await _publish_state(guild_id)
        asyncio.create_task(_start_playback_via_ws(guild_id))
        return
    if kind == "playlist":
        pid = msg.get("playlist_id")
        if not _valid_uuid(pid):
            return
        try:
            pl = await _db(playlist_store.get_playlist, conn, pid)
        except Exception:
            log.exception('unexpected error')
            await _notify(guild_id, "error", "プレイリストの取得に失敗しました")
            return
        if not pl:
            await _notify(guild_id, "error", "プレイリストが見つかりません")
            return
        # 他人所有でも、この guild の library に入っていれば再生OK (共有ライブラリ)
        if pl.user_id != str(user_id):
            if not await _db(playlist_store.is_playlist_in_library, conn, str(guild_id), pid):
                await _notify(guild_id, "error", "プレイリストが見つかりません")
                return
        raw_start = msg.get("start_index")
        start_index = raw_start if isinstance(raw_start, int) else 0
        if start_index < 0 or start_index >= len(pl.tracks):
            start_index = 0
        state["mode"] = "playlist"
        state["playlist_id"] = pl.id
        state["playlist_name"] = pl.name
        state["playlist_tracks"] = [t.to_dict() for t in pl.tracks]
        state["playlist_index"] = start_index
        state["playlist_shuffle_order"] = []  # 古い順序を破棄 (次曲計算時に再生成)
        _mark_playlist_starter(state, user_id, pl.user_id, guild=g, user=user)
        await _db(playlist_store.set_last_used, conn, str(user_id), pl.id)
        if vc and (vc.is_playing() or vc.is_paused()):
            state["_playlist_advance"] = "select"
            vc.stop()
        state["player"] = None  # 位置を 0 にリセット
        await _publish_state(guild_id)
        asyncio.create_task(_start_playback_via_ws(guild_id))
        return
    return


# マイプレイリスト管理系は user_id 必須 (旧: 関数途中の `if user_id is None: return`)
_WS_REQUIRES_USER = {
    "list_playlists",
    "select_playlist",
    "create_playlist",
    "import_playlist_youtube",
    "import_playlist_url",
    "add_track_to_playlist",
    "remove_playlist_track",
    "reorder_playlist_track",
    "get_playlist_detail",
    "load_playlist_detail_page",
    "add_playlist_to_queue",
    "add_track_to_queue",
    "delete_playlist",
    "set_pref",
    "set_playlist_tags",
    "rename_playlist",
    "set_shuffle_playlist",
    "set_loop_queue",
    "playlist_jump",
    "set_library_membership",
    "library_select",
    "get_guild_settings",
    "set_guild_pref",
}


async def _handle_ws_command(guild_id: int, user, msg: dict) -> None:
    log.info(
        "ws cmd: guild=%s user=%s msg=%s",
        guild_id, getattr(user, "username", "?"), msg,
    )
    g = client.get_guild(guild_id)
    if g is None:
        log.warning("ws cmd: guild not found in client.guilds (id=%s)", guild_id)
        return

    state = _ensure_state(guild_id)
    vc = g.voice_client
    user_id = getattr(user, "id", None)
    # 以降の _notify 呼び出しが他ユーザーに漏れないように、この WS の操作元を記録
    if user_id is not None:
        _current_ws_user.set(str(user_id))
    cmd = msg.get("type")

    # BC-SEC-04: 再生制御は BOT が在室する VC に同席するユーザーのみ許可
    if cmd in _WS_PLAYBACK_CONTROL and user_id is not None:
        if not _can_control_playback(g, vc, user_id):
            await _notify(
                guild_id, "error",
                "BOT と同じボイスチャンネルに参加してから操作してください",
                user_id=str(user_id),
            )
            return

    handler = _WS_HANDLERS.get(cmd)
    if handler is None:
        log.info("unhandled ws command: %s", cmd)
        return
    if cmd in _WS_REQUIRES_USER and user_id is None:
        return
    c = _WsCtx(guild_id=guild_id, user=user, msg=msg, g=g,
               state=state, vc=vc, user_id=user_id, cmd=cmd)
    await handler(c)


async def _handle_ws_connect(guild_id: int, user) -> None:
    """WS 接続時の初期化:
       - BOT が対象 guild の VC に未参加なら自動参加
       - 既存のプロセス一覧 (もしあれば) を初回 push
       - ユーザーマスタを更新 (プレイリスト作成者などを名前で引けるように)
    """
    # Activity を開いた時点でマスタに載せる。プレイリストを作るのはここを
    # 通ったユーザーなので、my_playlists の作成者名がこれで引けるようになる。
    try:
        g0 = client.get_guild(guild_id)
        _touch_user(user.id,
                    _resolve_account_name(g0, user.id, user),
                    getattr(user, "avatar", None))
    except Exception:
        log.exception('unexpected error')

    # 既存プロセス一覧があれば送る (再接続/別タブからの開き直しに備えて)
    try:
        if _process_list(guild_id, str(user.id)):
            await _publish_processes(guild_id, str(user.id))
    except Exception:
        log.exception('unexpected error')

    g = client.get_guild(guild_id)
    if g is None:
        return
    if g.voice_client is not None:
        return
    try:
        member = g.get_member(int(user.id))
    except (TypeError, ValueError):
        return
    if not member or not member.voice or not member.voice.channel:
        return
    try:
        await member.voice.channel.connect()
        await _notify(
            guild_id,
            "info",
            f"#{member.voice.channel.name} に参加しました",
            user_id=str(user.id),
        )
    except Exception:
        log.exception('unexpected error')


async def _admin_do_action(guild_id: int, action: str) -> tuple[bool, str]:
    """管理画面からのギルド操作。(ok, ユーザー向けメッセージ) を返す。

    skip / pause は _wsh_skip / _wsh_pause と同じ機構の複製 (合成 ctx で
    _wsh_* を呼ぶと _WsCtx の内部契約に結合するため、数行を複製する)。
    """
    g = client.get_guild(int(guild_id))
    if g is None:
        return False, "ギルドが見つかりません"
    state = guild_table.get(g.id) or {}
    vc = g.voice_client

    if action == "skip":
        # _wsh_skip と同一: 再生ループが play_one 復帰後に次曲へ進む
        if vc and (vc.is_playing() or vc.is_paused()):
            if state.get("mode") == "playlist":
                state["_playlist_advance"] = "next"
            vc.stop()
            return True, "スキップしました"
        return False, "再生していません"

    if action == "pause":
        # _wsh_pause と同一の擬似 pause (vc.pause() は RTP timestamp が
        # 止まり遅延が累積するため使わない)
        player = state.get("player")
        if player is not None and vc and vc.is_playing() \
                and not getattr(player, "_paused", False):
            player._paused = True
            await _publish_state(g.id)
            return True, "一時停止しました"
        return False, "再生していないか、既に一時停止中です"

    if action == "resume":
        # 擬似 pause からの再開のみ。idle からの再生開始 (_wsh_play の挙動) は
        # 管理操作としては意図しない再生になり得るため行わない。
        if _player_is_paused(g.id):
            _player_set_paused(g.id, False)
            await _publish_state(g.id)
            return True, "再開しました"
        return False, "一時停止していません"

    if action == "stop":
        # キューモードでは vc.stop() 単体は「スキップ」になる (再生ループが
        # 次曲へ進む)。先に guild_table から状態を消して mode チェックで
        # ループを終了させてから vc.stop() することで「停止」になる。
        had_state = guild_table.pop(g.id, None) is not None
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            await _publish_stopped(g.id)
            return True, "停止しました"
        if had_state:
            await _publish_stopped(g.id)
            return True, "待機状態を解除しました"
        return False, "再生していません"

    if action == "disconnect":
        # /leave と同じ手順
        if vc is None:
            return False, "接続していません"
        guild_table.pop(g.id, None)
        await vc.disconnect()
        await _publish_stopped(g.id)
        return True, "切断しました"

    return False, "不明な操作です"


def _resolve_announce_channel(g, configured: dict):
    """ギルドの実際の配信先を解決する。(channel, kind) を返す。
    設定チャンネル (送信可) → システムチャンネル (送信可) → (None, "none")。"""
    cid = configured.get(str(g.id))
    if cid and str(cid).isdigit():
        c = g.get_channel(int(cid))
        if isinstance(c, discord.TextChannel) and c.permissions_for(g.me).send_messages:
            return c, "configured"
    sc = g.system_channel
    if sc and sc.permissions_for(g.me).send_messages:
        return sc, "system"
    return None, "none"


async def _admin_broadcast_targets() -> list[dict]:
    """お知らせ配信先プレビュー (管理画面の対象選択リスト用)。"""
    configured = await _db(get_all_announce_channels_sql)
    out = []
    for g in client.guilds:
        ch, kind = _resolve_announce_channel(g, configured)
        out.append({
            "id": str(g.id),
            "name": g.name,
            "member_count": g.member_count,
            "channel": f"#{ch.name}" if ch else None,
            "channel_kind": kind,
        })
    return out


async def _admin_broadcast_announcement(payload: dict) -> dict:
    """お知らせを各ギルドへ送信し、結果を announcements3 に永続化する。

    payload は routes_admin で検証済み:
    {title, body, target: "all"|"selected"|"unconfigured", guild_ids,
     use_embed: bool, color: "#rrggbb"|None,
     image_bytes: bytes|None, image_filename: str|None}
    """
    title = payload["title"]
    body = payload["body"]
    target_kind = payload["target"]
    guild_ids = payload.get("guild_ids")
    use_embed = payload.get("use_embed", True)
    color = payload.get("color")
    image_bytes = payload.get("image_bytes")
    image_filename = payload.get("image_filename")

    configured = await _db(get_all_announce_channels_sql)
    if target_kind == "selected":
        targets = [g for gid in guild_ids
                   if (g := client.get_guild(int(gid))) is not None]
    elif target_kind == "unconfigured":
        # お知らせチャンネル未設定のサーバーのみ (設定の呼びかけ等に使う)
        targets = [g for g in client.guilds if str(g.id) not in configured]
    else:
        targets = list(client.guilds)

    embed = None
    content = None
    if use_embed:
        colour = discord.Colour.from_rgb(29, 185, 84)
        if isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            colour = discord.Colour(int(color[1:], 16))
        embed = discord.Embed(
            title=title or "お知らせ", description=body, colour=colour)
        embed.set_footer(text=f"{PRODUCT_NAME} お知らせ")
        if image_bytes and image_filename:
            embed.set_image(url=f"attachment://{image_filename}")
    else:
        # プレーンメッセージ (content は 2000 文字制限)
        content = (f"**{title}**\n{body}" if title else body)[:2000]

    results = []
    sent = skipped = failed = 0
    via_label = {"configured": "設定チャンネル", "system": "システムチャンネル"}
    for g in targets:
        ch, kind = _resolve_announce_channel(g, configured)
        if ch is None:
            skipped += 1
            results.append({"guild_id": str(g.id), "guild_name": g.name,
                            "status": "skipped",
                            "detail": "送信可能なチャンネルがありません"})
            continue
        try:
            # None を明示的に渡さない (discord.py の引数既定と衝突させない)。
            # discord.File は使い切りなのでギルドごとに作り直す。
            kwargs = {}
            if content is not None:
                kwargs["content"] = content
            if embed is not None:
                kwargs["embed"] = embed
            if image_bytes and image_filename:
                kwargs["file"] = discord.File(
                    BytesIO(image_bytes), filename=image_filename)
            await ch.send(**kwargs)
            sent += 1
            results.append({"guild_id": str(g.id), "guild_name": g.name,
                            "status": "sent",
                            "detail": f"#{ch.name} ({via_label[kind]})"})
        except Exception as e:
            failed += 1
            results.append({"guild_id": str(g.id), "guild_name": g.name,
                            "status": "failed", "detail": str(e)[:200]})
        # ギルド間の送信間隔 (レート制限配慮)
        await asyncio.sleep(0.3)
    meta = await _db(
        insert_announcement_sql, title, body, target_kind,
        sent, skipped, failed, json.dumps(results, ensure_ascii=False),
        image_filename)
    return {"id": meta["id"], "created_at": meta["created_at"],
            "sent": sent, "skipped": skipped, "failed": failed,
            "results": results}


async def _admin_list_announcements(limit: int) -> list[dict]:
    rows = await _db(list_announcements_sql, limit)
    for r in rows:
        try:
            r["results"] = json.loads(r["results"])
        except Exception:
            r["results"] = []
    return rows


async def _admin_guild_history(guild_id: int, page: int) -> dict:
    """管理画面用: ギルドの再生履歴。表示名は
    このサーバーでの表示名 (ニックネーム) -> DB のアカウント名 の順で解決する
    (退室済みでも当時のアカウント名が出る)。"""
    data = await _db(get_admin_guild_history_sql, str(guild_id), page)
    g = client.get_guild(int(guild_id))
    name_cache: dict = {}
    for it in data["items"]:
        uid = it.get("userid")
        saved = it.pop("saved_username", None)
        if uid not in name_cache:
            name_cache[uid] = _resolve_display_name(g, uid)
        it["username"] = name_cache[uid] or saved
    data["page"] = max(0, int(page))
    data["page_size"] = 25
    return data


async def _admin_backfill_masters(force: bool = False) -> dict:
    """ユーザー/サーバーのマスタを再構築し、履歴の username も埋める。

    保存するのはサーバーごとのニックネームではなく **アカウント名**
    (global_name -> username)。同じ人がサーバーごとに別名で記録されるのを防ぐ。
    表示側 (/histry・管理画面) はそのサーバーの表示名を優先して解決する。

    1) 参加中のギルドのメンバーキャッシュから
    2) 退室済みでも fetch_user で Discord API から取得
    force=True なら既存の値も上書きする。何度実行しても安全 (冪等)。
    """
    # 履歴だけでなく、プレイリスト/設定/ライブラリに出てくる userid も
    # ユーザーマスタに載せる (Grafana から作成者名を引けるようにするため)
    all_uids = await _db(get_all_known_userids_sql)
    hist_uids = set(await _db(get_history_userids_missing_name_sql, force))
    uids = list(dict.fromkeys(list(hist_uids) + list(all_uids)))
    results = []
    updated = 0
    failed = 0
    users_upserted = 0
    for uid in uids:
        name = None
        source = ""
        # 1) 参加中のギルドのキャッシュから (API を使わずに済む)
        for g in client.guilds:
            n = _resolve_account_name(g, uid)
            if n:
                name, source = n, "member"
                break
        # 2) 退室済みでも Discord API から引く
        if not name:
            try:
                u = await client.fetch_user(int(uid))
                name = (getattr(u, "global_name", None) or u.name)
                source = "api"
                await asyncio.sleep(0.3)  # レート制限に配慮
            except Exception as e:
                failed += 1
                results.append({"userid": uid, "ok": False,
                                "error": f"{e.__class__.__name__}: {str(e)[:60]}"})
                continue
        # ユーザーマスタ (Grafana の JOIN 先) を更新
        await _db(upsert_discord_user_sql, uid, name, None)
        users_upserted += 1
        # 履歴のスナップショット列は履歴に出てくるユーザーのみ更新する
        rows = 0
        if uid in hist_uids:
            rows = await _db(backfill_history_username_sql, uid, name, force)
            updated += rows
        results.append({"userid": uid, "ok": True, "username": name,
                        "rows": rows, "source": source})
    # サーバーマスタも同時に埋める (参加中の guild は名前が取れる)
    guilds_upserted = 0
    guild_missing = []
    for gid in await _db(get_all_known_guildids_sql):
        g = client.get_guild(int(gid)) if str(gid).isdigit() else None
        if g is None:
            guild_missing.append(gid)   # 退出済み: 名前を取得する手段がない
            continue
        icon = getattr(getattr(g, "icon", None), "key", None)
        await _db(upsert_discord_guild_sql, str(g.id), g.name, icon,
                  getattr(g, "member_count", None))
        guilds_upserted += 1
    log.info(
        "master backfill: %d users (%d history rows), %d guilds, "
        "%d failed (force=%s)",
        len(uids), updated, guilds_upserted, failed, force)
    return {"users": len(uids), "updated_rows": updated,
            "users_upserted": users_upserted, "failed": failed,
            "guilds_upserted": guilds_upserted,
            "guilds_unresolved": guild_missing,
            "force": force, "results": results}


async def _run() -> None:
    # BC-SEC-07: 起動時にログのトークンマスキングを有効化
    _install_secret_log_masking()
    # 管理画面のエラーログバッファ (マスキングより後に付ける)
    _install_admin_error_buffer()
    # 既存DBへのマイグレーション (CREATE TABLE IF NOT EXISTS)
    # DB 起動待ち等の一時失敗に備えて最大 5 回リトライする。
    for _attempt in range(1, 6):
        try:
            await _db(playlist_store.init_schema, conn)
            print("[playlist_store] schema ensured", flush=True)
            break
        except Exception:
            log.exception('init_schema failed (attempt %d/5)', _attempt)
            if _attempt < 5:
                await asyncio.sleep(3)
    register_command_handler(_handle_ws_command)
    register_connect_handler(_handle_ws_connect)
    register_admin_provider(AdminProvider(
        overview=_admin_get_overview,
        guilds=_admin_get_guilds,
        action=_admin_do_action,
        errors=_admin_get_errors,
        broadcast=_admin_broadcast_announcement,
        broadcast_targets=_admin_broadcast_targets,
        announcements=_admin_list_announcements,
        guild_history=_admin_guild_history,
        backfill_masters=_admin_backfill_masters,
    ))
    api_app = create_app()
    config = uvicorn.Config(
        api_app, host="0.0.0.0", port=8080, log_level="info",
    )
    server = uvicorn.Server(config)

    api_task = asyncio.create_task(server.serve(), name="activity_api")
    tick_task = asyncio.create_task(_progress_loop(), name="progress_loop")
    audio_task = asyncio.create_task(_audio_features_loop(), name="audio_features_loop")
    lag_task = asyncio.create_task(_loop_lag_monitor(), name="loop_lag_monitor")
    try:
        async with client:
            await client.start(token)
    finally:
        server.should_exit = True
        tick_task.cancel()
        audio_task.cancel()
        lag_task.cancel()
        await asyncio.gather(
            api_task, tick_task, audio_task, lag_task, return_exceptions=True)


asyncio.run(_run())