"""ユーザーごとのマイプレイリスト永続化 (PostgreSQL)。

スキーマ:
    my_playlists3      (id, userid, name, created_at)
    my_playlist_tracks3 (id, playlist_id, position, title, url, artwork, duration_ms)
    user_prefs3         (userid, auto_select_last, last_used_playlist_id)

スコープはユーザーごと (グローバル共有)。同じユーザー内で name は UNIQUE。
"""
from __future__ import annotations

import functools
import inspect
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any

import psycopg2
import psycopg2.errors
from psycopg2.extras import execute_values

from .db_safety import ensure_clean_txn, end_open_txn, rollback_quietly

# BC-PERF-07 / MOD-PERF-02: 単一の psycopg2 接続を複数スレッド
# (asyncio.to_thread のワーカ) から安全に触れるよう、全 DB 操作を 1 本の
# 再入可能ロックで直列化する。接続が 1 つなので元々並列実行はできず、
# ロックは event loop スレッドと executor スレッド間のアクセス競合を防ぐ。
# smile_music3 側の SQL ヘルパも同じ conn を使うため、この lock を import して共有する。
db_lock = threading.RLock()


def _locked(fn):
    """ロック下で実行し、実行前に残骸トランザクションを掃除、例外時は rollback、読み取りが開いたトランザクションは実行後に閉じる。"""
    @functools.wraps(fn)
    def wrapper(conn, *args, **kwargs):
        with db_lock:
            ensure_clean_txn(conn)
            try:
                result = fn(conn, *args, **kwargs)
            except Exception:
                rollback_quietly(conn)
                raise
            end_open_txn(conn)
            return result
    return wrapper


# リリースチャンネルごとのテーブル分離: alpha="3" (従来), beta="2", release=""。
# compose 側でインスタンスごとに設定する (release は明示的に空文字)。
TABLE_SUFFIX = os.environ.get("OGA_MUSIC_TABLE_SUFFIX", "3")

PLAYLIST_TABLE = f"my_playlists{TABLE_SUFFIX}"
TRACK_TABLE = f"my_playlist_tracks{TABLE_SUFFIX}"
PREF_TABLE = f"user_prefs{TABLE_SUFFIX}"
LIBRARY_TABLE = f"my_playlist_libraries{TABLE_SUFFIX}"
# smile_music3 側の SQL ヘルパもこの定数を参照する (サフィックスの単一情報源)
GUILDS_TABLE = f"guilds{TABLE_SUFFIX}"
HISTORY_TABLE = f"history{TABLE_SUFFIX}"
ANNOUNCE_TABLE = f"announcements{TABLE_SUFFIX}"
# ユーザーマスタ。userid しか持たない他テーブル (my_playlists / user_prefs 等)
# から名前を引くための参照先。FK は張らず LEFT JOIN で使う
# (ユーザー行が無くても履歴やプレイリストは独立して残る)。
USERS_TABLE = f"discord_users{TABLE_SUFFIX}"
# サーバーマスタ。GUILDS_TABLE (guilds{suffix}) は「設定を変更した guild だけ」の
# 設定テーブルなので全 guild を網羅しない。名前を引く用途はこちらを使う。
GUILD_MASTER_TABLE = f"discord_guilds{TABLE_SUFFIX}"

_INT32_MAX = 2_147_483_647


def _clamp_ms(v):
    """duration_ms を INT 列の範囲に丸める (超過すると INSERT が失敗するため)。"""
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        return 0
    return min(max(n, 0), _INT32_MAX)


@dataclass
class PlaylistTrack:
    title: str
    url: str
    artwork: str
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "artwork": self.artwork,
            "duration_ms": self.duration_ms,
        }


@dataclass
class Playlist:
    id: str
    user_id: str
    name: str
    tracks: list[PlaylistTrack]
    tags: list[str] = field(default_factory=list)


def init_schema(conn) -> None:
    """スキーマ初期化。CREATE (新規) と既存DB向けマイグレーション (ALTER 等) を
    MOD-MAINT-06 で内部関数に分離した。"""
    with conn.cursor() as cur:
        _create_tables(cur)
        _run_migrations(cur)
    conn.commit()


def _create_tables(cur) -> None:
    """各テーブルの CREATE TABLE IF NOT EXISTS (新規DB用)。"""
    # uuid_generate_v4() を使う CREATE より先に extension を保証する
    cur.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";')
    # guild 設定 / 再生履歴 (beta/release インスタンスの初回起動時に作成される。
    # alpha では Init.sql 由来の既存テーブルがあるため no-op)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {GUILDS_TABLE} (
            id VARCHAR(255) NOT NULL,
            prefix TEXT,
            volume FLOAT4,
            stream BOOLEAN,
            announce_channel VARCHAR(255),
            PRIMARY KEY (id)
        );
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (
            id UUID DEFAULT uuid_generate_v4() NOT NULL,
            userid VARCHAR(255) NOT NULL,
            guild VARCHAR(255) NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            username TEXT,
            datetime TIMESTAMP DEFAULT current_timestamp NOT NULL,
            PRIMARY KEY (id)
        );
    """)
    # ユーザーマスタ (userid -> アカウント名)。Grafana などが
    # my_playlists / history / user_prefs から名前を引くときの JOIN 先。
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {USERS_TABLE} (
            userid VARCHAR(255) PRIMARY KEY,
            username TEXT,
            avatar TEXT,
            first_seen TIMESTAMP DEFAULT current_timestamp NOT NULL,
            last_seen TIMESTAMP DEFAULT current_timestamp NOT NULL
        );
    """)
    # サーバーマスタ (guildid -> 名前)。history / libraries から名前を引く JOIN 先。
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {GUILD_MASTER_TABLE} (
            guildid VARCHAR(255) PRIMARY KEY,
            name TEXT,
            icon TEXT,
            member_count INT,
            first_seen TIMESTAMP DEFAULT current_timestamp NOT NULL,
            last_seen TIMESTAMP DEFAULT current_timestamp NOT NULL
        );
    """)
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS idx_history{TABLE_SUFFIX}_guild_dt "
        f"ON {HISTORY_TABLE} (guild, datetime DESC);")
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS idx_history{TABLE_SUFFIX}_user_guild_dt "
        f"ON {HISTORY_TABLE} (userid, guild, datetime DESC);")
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {PLAYLIST_TABLE} (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            userid VARCHAR(255) NOT NULL,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT current_timestamp,
            in_library BOOLEAN DEFAULT FALSE,
            library_added_at TIMESTAMP,
            UNIQUE (userid, name)
        );
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {TRACK_TABLE} (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            playlist_id UUID NOT NULL REFERENCES {PLAYLIST_TABLE}(id) ON DELETE CASCADE,
            position INT NOT NULL,
            title TEXT,
            url TEXT NOT NULL,
            artwork TEXT,
            duration_ms INT,
            created_at TIMESTAMP DEFAULT current_timestamp
        );
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {PREF_TABLE} (
            userid VARCHAR(255) PRIMARY KEY,
            auto_select_last BOOLEAN DEFAULT FALSE,
            last_used_playlist_id UUID,
            auto_add_to_library BOOLEAN DEFAULT FALSE
        );
    """)
    # サーバー(guild)単位のライブラリ junction table (guild 内で共有)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {LIBRARY_TABLE} (
            guildid VARCHAR(255) NOT NULL,
            playlist_id UUID NOT NULL REFERENCES {PLAYLIST_TABLE}(id) ON DELETE CASCADE,
            added_by_userid VARCHAR(255) NOT NULL,
            added_by_username VARCHAR(255),
            added_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (guildid, playlist_id)
        );
    """)


def _run_migrations(cur) -> None:
    """既存DB向けの idempotent なマイグレーション (ALTER / 旧スキーマ移行)。"""
    # 既存DB用マイグレーション (idempotent)
    cur.execute(f"ALTER TABLE {PLAYLIST_TABLE} ALTER COLUMN name TYPE TEXT;")
    cur.execute(f"ALTER TABLE {TRACK_TABLE} ALTER COLUMN title TYPE TEXT;")
    cur.execute(f"ALTER TABLE {TRACK_TABLE} ALTER COLUMN url TYPE TEXT;")
    cur.execute(f"ALTER TABLE {TRACK_TABLE} ALTER COLUMN artwork TYPE TEXT;")
    cur.execute(f"""
        ALTER TABLE {PLAYLIST_TABLE}
        ADD COLUMN IF NOT EXISTS in_library BOOLEAN DEFAULT FALSE;
    """)
    cur.execute(f"""
        ALTER TABLE {PLAYLIST_TABLE}
        ADD COLUMN IF NOT EXISTS library_added_at TIMESTAMP;
    """)
    cur.execute(f"""
        ALTER TABLE {PLAYLIST_TABLE}
        ADD COLUMN IF NOT EXISTS tags TEXT[] DEFAULT ARRAY[]::TEXT[];
    """)
    cur.execute(f"""
        ALTER TABLE {PREF_TABLE}
        ADD COLUMN IF NOT EXISTS auto_add_to_library BOOLEAN DEFAULT FALSE;
    """)
    cur.execute(f"""
        ALTER TABLE {PREF_TABLE}
        ADD COLUMN IF NOT EXISTS bg_tint_enabled BOOLEAN DEFAULT TRUE;
    """)
    cur.execute(f"""
        ALTER TABLE {PREF_TABLE}
        ADD COLUMN IF NOT EXISTS audio_visualizer VARCHAR(16) DEFAULT 'off';
    """)
    cur.execute(f"""
        ALTER TABLE {PREF_TABLE}
        ADD COLUMN IF NOT EXISTS theme_color VARCHAR(16);
    """)
    cur.execute(f"""
        ALTER TABLE {PREF_TABLE}
        ADD COLUMN IF NOT EXISTS visualizer_tint_enabled BOOLEAN DEFAULT FALSE;
    """)
    # 旧スキーマ (PK が userid を含む user-scoped library) が残っていれば
    # guild-scoped に移行する
    cur.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = '{LIBRARY_TABLE}'
                  AND column_name = 'userid'
            ) THEN
                ALTER TABLE {LIBRARY_TABLE}
                    ADD COLUMN IF NOT EXISTS added_by_userid VARCHAR(255);
                UPDATE {LIBRARY_TABLE}
                    SET added_by_userid = userid
                    WHERE added_by_userid IS NULL;
                -- 同じ (guildid, playlist_id) は古い行だけ残して dedupe
                DELETE FROM {LIBRARY_TABLE} a
                    USING {LIBRARY_TABLE} b
                    WHERE a.ctid > b.ctid
                      AND a.guildid = b.guildid
                      AND a.playlist_id = b.playlist_id;
                ALTER TABLE {LIBRARY_TABLE}
                    DROP CONSTRAINT IF EXISTS {LIBRARY_TABLE}_pkey;
                ALTER TABLE {LIBRARY_TABLE}
                    ADD PRIMARY KEY (guildid, playlist_id);
                ALTER TABLE {LIBRARY_TABLE} DROP COLUMN userid;
                ALTER TABLE {LIBRARY_TABLE}
                    ALTER COLUMN added_by_userid SET NOT NULL;
            END IF;
        END$$;
    """)
    cur.execute(f"""
        ALTER TABLE {LIBRARY_TABLE}
            ADD COLUMN IF NOT EXISTS added_by_username VARCHAR(255);
    """)
    cur.execute(f"""
        ALTER TABLE {LIBRARY_TABLE}
            ADD COLUMN IF NOT EXISTS added_by_avatar VARCHAR(255);
    """)
    # varchar(255) 超過の INSERT 失敗が共有接続を汚染していたため TEXT 化。
    # 継承した旧テーブル (guilds/guilds2 等) もここで近代化される。
    cur.execute(f"ALTER TABLE IF EXISTS {HISTORY_TABLE} ALTER COLUMN title TYPE TEXT;")
    cur.execute(f"ALTER TABLE IF EXISTS {HISTORY_TABLE} ALTER COLUMN url TYPE TEXT;")
    # 再生したユーザーの表示名 (記録時点のスナップショット)。
    # userid だけだと退室後に誰か分からなくなるため。既存行は NULL のまま。
    cur.execute(
        f"ALTER TABLE IF EXISTS {HISTORY_TABLE} "
        f"ADD COLUMN IF NOT EXISTS username TEXT;"
    )
    cur.execute(f"ALTER TABLE IF EXISTS {GUILDS_TABLE} ALTER COLUMN prefix TYPE TEXT;")
    # お知らせ配信: guild ごとの配信先チャンネル + 配信履歴
    cur.execute(
        f"ALTER TABLE IF EXISTS {GUILDS_TABLE} "
        f"ADD COLUMN IF NOT EXISTS announce_channel VARCHAR(255);"
    )
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {ANNOUNCE_TABLE} (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            target_kind VARCHAR(16) NOT NULL DEFAULT 'all',
            sent_count INT NOT NULL DEFAULT 0,
            skipped_count INT NOT NULL DEFAULT 0,
            failed_count INT NOT NULL DEFAULT 0,
            results TEXT NOT NULL DEFAULT '[]',
            created_at TIMESTAMP DEFAULT current_timestamp NOT NULL
        );
    """)
    cur.execute(
        f"ALTER TABLE IF EXISTS {ANNOUNCE_TABLE} "
        f"ADD COLUMN IF NOT EXISTS image_filename TEXT;"
    )


# ---------- Playlist CRUD ----------

def list_playlists(conn, user_id: str, guild_id: str) -> list[dict[str, Any]]:
    """ユーザーの全プレイリストを (この guild の) library 加入状態 + 先頭曲のサムネ込みで返す。"""
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT p.id::text, p.name, COUNT(t.id),
                   (lib.playlist_id IS NOT NULL) AS in_library,
                   lib.added_at,
                   MAX(CASE WHEN t.position = 0 THEN t.artwork END) AS cover_url,
                   COALESCE(p.tags, ARRAY[]::TEXT[]) AS tags
            FROM {PLAYLIST_TABLE} p
            LEFT JOIN {TRACK_TABLE} t ON t.playlist_id = p.id
            LEFT JOIN {LIBRARY_TABLE} lib
                ON lib.playlist_id = p.id
                AND lib.guildid = %s
            WHERE p.userid = %s
            GROUP BY p.id, p.name, p.created_at, lib.playlist_id, lib.added_at, p.tags
            ORDER BY p.created_at ASC
        """, (guild_id, user_id))
        rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "name": r[1],
            "track_count": r[2],
            "in_library": bool(r[3]),
            "library_added_at": r[4].isoformat() if r[4] else None,
            "cover_url": r[5] or "",
            "tags": list(r[6] or []),
        }
        for r in rows
    ]


def set_library_membership(
    conn,
    user_id: str,
    username: str,
    guild_id: str,
    playlist_id: str,
    in_library: bool,
    avatar: str | None = None,
) -> bool:
    """guild の library への追加/離脱。

    library は guild 全体で共有。追加は (guildid, playlist_id) で重複排除し、
    追加者の user_id / username / avatar hash を記録する。削除は誰でも可能。
    """
    with conn.cursor() as cur:
        if in_library:
            cur.execute(
                f"""INSERT INTO {LIBRARY_TABLE}
                    (guildid, playlist_id, added_by_userid, added_by_username, added_by_avatar)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (guildid, playlist_id) DO NOTHING""",
                (guild_id, playlist_id, user_id, username or "", avatar),
            )
        else:
            cur.execute(
                f"""DELETE FROM {LIBRARY_TABLE}
                    WHERE guildid = %s AND playlist_id = %s""",
                (guild_id, playlist_id),
            )
        changed = cur.rowcount > 0
    conn.commit()
    return changed


def list_library(conn, guild_id: str) -> list[dict[str, Any]]:
    """guild の library に登録されているプレイリスト一覧を返す。

    所有者は問わない (誰が作ったプレイリストでも、誰かが library に入れていれば出る)。
    """
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT p.id::text, p.name, p.userid AS owner_id,
                   COUNT(t.id) AS track_count,
                   MAX(CASE WHEN t.position = 0 THEN t.artwork END) AS cover_url,
                   lib.added_by_userid, lib.added_by_username, lib.added_at,
                   lib.added_by_avatar,
                   COALESCE(p.tags, ARRAY[]::TEXT[]) AS tags
            FROM {LIBRARY_TABLE} lib
            JOIN {PLAYLIST_TABLE} p ON p.id = lib.playlist_id
            LEFT JOIN {TRACK_TABLE} t ON t.playlist_id = p.id
            WHERE lib.guildid = %s
            GROUP BY p.id, p.name, p.userid,
                     lib.added_by_userid, lib.added_by_username, lib.added_at,
                     lib.added_by_avatar, p.tags
            ORDER BY lib.added_at ASC
        """, (guild_id,))
        rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "name": r[1],
            "owner_id": r[2],
            "track_count": r[3],
            "cover_url": r[4] or "",
            "added_by_userid": r[5],
            "added_by_username": r[6] or "",
            "added_at": r[7].isoformat() if r[7] else None,
            "added_by_avatar": r[8],
            "tags": list(r[9] or []),
        }
        for r in rows
    ]


def is_playlist_in_library(conn, guild_id: str, playlist_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT 1 FROM {LIBRARY_TABLE} WHERE guildid = %s AND playlist_id = %s",
            (guild_id, playlist_id),
        )
        return cur.fetchone() is not None


def get_playlist(
    conn, playlist_id: str, offset: int = 0, limit: int | None = None,
) -> Playlist | None:
    """プレイリストを取得。BC-PERF-06: offset/limit を指定すると曲を部分取得する
    (詳細画面のページング用)。limit=None なら従来どおり全曲。"""
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT id::text, userid, name, COALESCE(tags, ARRAY[]::TEXT[])
                FROM {PLAYLIST_TABLE} WHERE id = %s""",
            (playlist_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        pid, uid, name, tags = row
        if limit is None:
            cur.execute(f"""
                SELECT title, url, artwork, duration_ms
                FROM {TRACK_TABLE}
                WHERE playlist_id = %s
                ORDER BY position ASC
            """, (playlist_id,))
        else:
            cur.execute(f"""
                SELECT title, url, artwork, duration_ms
                FROM {TRACK_TABLE}
                WHERE playlist_id = %s
                ORDER BY position ASC
                LIMIT %s OFFSET %s
            """, (playlist_id, int(limit), max(0, int(offset))))
        tracks = [
            PlaylistTrack(
                title=r[0] or "",
                url=r[1],
                artwork=r[2] or "",
                duration_ms=r[3] or 0,
            )
            for r in cur.fetchall()
        ]
    return Playlist(
        id=pid, user_id=uid, name=name, tracks=tracks, tags=list(tags or []),
    )


def count_playlist_tracks(conn, playlist_id: str) -> int:
    """BC-PERF-06: プレイリストの総曲数 (ページング時の total 用)。"""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM {TRACK_TABLE} WHERE playlist_id = %s",
            (playlist_id,),
        )
        r = cur.fetchone()
        return int(r[0]) if r else 0


class PlaylistNameConflict(Exception):
    pass


def create_playlist(conn, user_id: str, name: str, tracks: list[PlaylistTrack]) -> str:
    """プレイリストを作成。同名がある場合は PlaylistNameConflict。

    多数トラックでも 1 SQL 文で投入するため `execute_values` を使う。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {PLAYLIST_TABLE} (userid, name) VALUES (%s, %s) RETURNING id::text",
                (user_id, name),
            )
            playlist_id = cur.fetchone()[0]
            if tracks:
                rows = [
                    (playlist_id, i, t.title, t.url, t.artwork, _clamp_ms(t.duration_ms))
                    for i, t in enumerate(tracks)
                ]
                execute_values(
                    cur,
                    f"""INSERT INTO {TRACK_TABLE}
                        (playlist_id, position, title, url, artwork, duration_ms)
                        VALUES %s""",
                    rows,
                    page_size=200,
                )
        conn.commit()
        return playlist_id
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        raise PlaylistNameConflict(name)


def rename_playlist(conn, user_id: str, playlist_id: str, new_name: str) -> bool:
    """プレイリスト名を変更。同名ありなら PlaylistNameConflict。所有者でないなら False。"""
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""UPDATE {PLAYLIST_TABLE} SET name = %s
                    WHERE id = %s AND userid = %s""",
                (new_name, playlist_id, user_id),
            )
            changed = cur.rowcount > 0
        conn.commit()
        return changed
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        raise PlaylistNameConflict(new_name)


def set_tags(conn, user_id: str, playlist_id: str, tags: list[str]) -> bool:
    """プレイリストのタグを上書き保存。所有者のみ。"""
    # 空文字 / 重複除去 / 50 文字制限 / 最大 16 個
    norm: list[str] = []
    seen: set[str] = set()
    for t in tags:
        if not isinstance(t, str):
            continue
        s = t.strip()[:50]
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        norm.append(s)
        if len(norm) >= 16:
            break
    with conn.cursor() as cur:
        cur.execute(
            f"""UPDATE {PLAYLIST_TABLE} SET tags = %s
                WHERE id = %s AND userid = %s""",
            (norm, playlist_id, user_id),
        )
        changed = cur.rowcount > 0
    conn.commit()
    return changed


def append_track(conn, playlist_id: str, track: PlaylistTrack) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COALESCE(MAX(position), -1) + 1 FROM {TRACK_TABLE} WHERE playlist_id = %s",
            (playlist_id,),
        )
        pos = cur.fetchone()[0]
        cur.execute(
            f"""INSERT INTO {TRACK_TABLE}
                (playlist_id, position, title, url, artwork, duration_ms)
                VALUES (%s, %s, %s, %s, %s, %s)""",
            (playlist_id, pos, track.title, track.url, track.artwork, _clamp_ms(track.duration_ms)),
        )
    conn.commit()


def delete_playlist(conn, user_id: str, playlist_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM {PLAYLIST_TABLE} WHERE id = %s AND userid = %s",
            (playlist_id, user_id),
        )
        deleted = cur.rowcount > 0
    conn.commit()
    return deleted


def remove_track(conn, playlist_id: str, position: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM {TRACK_TABLE} WHERE playlist_id = %s AND position = %s",
            (playlist_id, position),
        )
        # 後続の position を詰める
        cur.execute(
            f"""UPDATE {TRACK_TABLE} SET position = position - 1
                WHERE playlist_id = %s AND position > %s""",
            (playlist_id, position),
        )
    conn.commit()


def reorder_track(
    conn, playlist_id: str, from_position: int, to_position: int,
) -> None:
    """from_position の曲を to_position に移動する。

    例: [A, B, C, D] で from=0, to=2 -> [B, C, A, D]
    UNIQUE 制約は無いが、衝突を避けるため一旦 -1 に逃がしてから更新する。
    """
    if from_position == to_position:
        return
    with conn.cursor() as cur:
        cur.execute(
            f"""UPDATE {TRACK_TABLE} SET position = -1
                WHERE playlist_id = %s AND position = %s""",
            (playlist_id, from_position),
        )
        if cur.rowcount == 0:
            return
        if from_position < to_position:
            cur.execute(
                f"""UPDATE {TRACK_TABLE} SET position = position - 1
                    WHERE playlist_id = %s AND position > %s AND position <= %s""",
                (playlist_id, from_position, to_position),
            )
        else:
            cur.execute(
                f"""UPDATE {TRACK_TABLE} SET position = position + 1
                    WHERE playlist_id = %s AND position >= %s AND position < %s""",
                (playlist_id, to_position, from_position),
            )
        cur.execute(
            f"""UPDATE {TRACK_TABLE} SET position = %s
                WHERE playlist_id = %s AND position = -1""",
            (to_position, playlist_id),
        )
    conn.commit()


# ---------- User prefs ----------

_ALLOWED_VISUALIZERS = {
    "off", "bars", "wave", "pulse", "mirror", "radial", "particles",
    "wmp", "dots", "digital",
}


def get_prefs(conn, user_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT auto_select_last, last_used_playlist_id::text,
                       COALESCE(auto_add_to_library, FALSE),
                       COALESCE(bg_tint_enabled, TRUE),
                       COALESCE(audio_visualizer, 'off'),
                       theme_color,
                       COALESCE(visualizer_tint_enabled, FALSE)
                FROM {PREF_TABLE} WHERE userid = %s""",
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        return {
            "auto_select_last": False,
            "last_used_playlist_id": None,
            "auto_add_to_library": False,
            "bg_tint_enabled": True,
            "audio_visualizer": "off",
            "theme_color": None,
            "visualizer_tint_enabled": False,
        }
    vis = row[4] if row[4] in _ALLOWED_VISUALIZERS else "off"
    return {
        "auto_select_last": bool(row[0]),
        "last_used_playlist_id": row[1],
        "auto_add_to_library": bool(row[2]),
        "bg_tint_enabled": bool(row[3]),
        "audio_visualizer": vis,
        "theme_color": row[5],
        "visualizer_tint_enabled": bool(row[6]),
    }


# MOD-MAINT-05: pref の単一列 UPSERT を共通化。column は許可リストで検証し
# SQL インジェクションを防ぐ (column はコード内定数のみ渡す想定だが二重で安全に)。
_PREF_COLUMNS = {
    "auto_select_last",
    "auto_add_to_library",
    "bg_tint_enabled",
    "visualizer_tint_enabled",
    "audio_visualizer",
    "theme_color",
    "last_used_playlist_id",
}


def _set_pref(conn, user_id: str, column: str, value) -> None:
    if column not in _PREF_COLUMNS:
        raise ValueError(f"unknown pref column: {column!r}")
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {PREF_TABLE} (userid, {column}) VALUES (%s, %s)
                ON CONFLICT (userid) DO UPDATE SET {column} = EXCLUDED.{column}""",
            (user_id, value),
        )
    conn.commit()


def set_auto_select(conn, user_id: str, enabled: bool) -> None:
    _set_pref(conn, user_id, "auto_select_last", enabled)


def set_auto_add_to_library(conn, user_id: str, enabled: bool) -> None:
    _set_pref(conn, user_id, "auto_add_to_library", enabled)


def set_bg_tint_enabled(conn, user_id: str, enabled: bool) -> None:
    _set_pref(conn, user_id, "bg_tint_enabled", enabled)


def set_visualizer_tint_enabled(conn, user_id: str, enabled: bool) -> None:
    _set_pref(conn, user_id, "visualizer_tint_enabled", enabled)


def set_audio_visualizer(conn, user_id: str, kind: str) -> None:
    if kind not in _ALLOWED_VISUALIZERS:
        kind = "off"
    _set_pref(conn, user_id, "audio_visualizer", kind)


def set_theme_color(conn, user_id: str, color: str | None) -> None:
    # 簡易 validation: #RRGGBB のみ受け付ける (空文字や不正値は NULL に)
    val: str | None = None
    if isinstance(color, str):
        c = color.strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", c):
            val = c.lower()
    _set_pref(conn, user_id, "theme_color", val)


def set_last_used(conn, user_id: str, playlist_id: str | None) -> None:
    _set_pref(conn, user_id, "last_used_playlist_id", playlist_id)


# このモジュールの公開 DB 関数を全て db_lock で保護する (BC-PERF-07 / MOD-PERF-02)。
# dataclass / 例外クラスは inspect.isfunction で除外される。アンダースコア始まり
# (_set_pref / _locked) は除外 — _set_pref はロック保持中の setter からのみ呼ばれる
# (RLock なので再入可)。
for _name in list(globals()):
    _obj = globals()[_name]
    if (
        inspect.isfunction(_obj)
        and getattr(_obj, "__module__", None) == __name__
        and not _name.startswith("_")
    ):
        globals()[_name] = _locked(_obj)
del _name, _obj
