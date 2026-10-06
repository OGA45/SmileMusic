CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

create table guilds (
id varchar(255) not null,
prefix varchar(255),
volume float4,
stream boolean,
PRIMARY KEY (id)
);
create table guilds2 (
id varchar(255) not null,
prefix varchar(255),
volume float4,
stream boolean,
PRIMARY KEY (id)
);
create table guilds3 (
id varchar(255) not null,
prefix text,
volume float4,
stream boolean,
announce_channel varchar(255),
PRIMARY KEY (id)
);
create table history3 (
id UUID DEFAULT uuid_generate_v4() NOT NULL,
userid varchar(255) not null,
guild varchar(255) not null,
title text not null,
url text not null,
username text,
datetime TIMESTAMP DEFAULT current_timestamp NOT NULL,
PRIMARY KEY (id)
);
-- BC-DB-09: 履歴ページングのフルスキャン回避
CREATE INDEX IF NOT EXISTS idx_history3_guild_dt ON history3 (guild, datetime DESC);
CREATE INDEX IF NOT EXISTS idx_history3_user_guild_dt ON history3 (userid, guild, datetime DESC);

-- ユーザーマスタ (userid -> アカウント名)。
-- userid しか持たないテーブルから名前を引くための JOIN 先。FK は張らない。
CREATE TABLE discord_users3 (
    userid VARCHAR(255) PRIMARY KEY,
    username TEXT,
    avatar TEXT,
    first_seen TIMESTAMP DEFAULT current_timestamp NOT NULL,
    last_seen TIMESTAMP DEFAULT current_timestamp NOT NULL
);

-- サーバーマスタ (guildid -> 名前)。guilds3 は設定を変更した guild しか
-- 行が無いため、名前を引く用途にはこちらを使う。FK は張らない。
CREATE TABLE discord_guilds3 (
    guildid VARCHAR(255) PRIMARY KEY,
    name TEXT,
    icon TEXT,
    member_count INT,
    first_seen TIMESTAMP DEFAULT current_timestamp NOT NULL,
    last_seen TIMESTAMP DEFAULT current_timestamp NOT NULL
);

-- Activity の MyPlaylist 機能用
CREATE TABLE my_playlists3 (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    userid VARCHAR(255) NOT NULL,
    name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT current_timestamp,
    in_library BOOLEAN DEFAULT FALSE,
    library_added_at TIMESTAMP,
    tags TEXT[] DEFAULT ARRAY[]::TEXT[],
    UNIQUE (userid, name)
);

CREATE TABLE my_playlist_tracks3 (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    playlist_id UUID NOT NULL REFERENCES my_playlists3(id) ON DELETE CASCADE,
    position INT NOT NULL,
    title TEXT,
    url TEXT NOT NULL,
    artwork TEXT,
    duration_ms INT,
    created_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE user_prefs3 (
    userid VARCHAR(255) PRIMARY KEY,
    auto_select_last BOOLEAN DEFAULT FALSE,
    last_used_playlist_id UUID,
    auto_add_to_library BOOLEAN DEFAULT FALSE,
    bg_tint_enabled BOOLEAN DEFAULT TRUE,
    audio_visualizer VARCHAR(16) DEFAULT 'off',
    theme_color VARCHAR(16),
    visualizer_tint_enabled BOOLEAN DEFAULT FALSE
);

-- サーバー(guild)単位のライブラリ junction (guild メンバーで共有)
CREATE TABLE my_playlist_libraries3 (
    guildid VARCHAR(255) NOT NULL,
    playlist_id UUID NOT NULL REFERENCES my_playlists3(id) ON DELETE CASCADE,
    added_by_userid VARCHAR(255) NOT NULL,
    added_by_username VARCHAR(255),
    added_by_avatar VARCHAR(255),
    added_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (guildid, playlist_id)
);

-- 管理画面からのお知らせ配信の履歴
CREATE TABLE announcements3 (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    target_kind VARCHAR(16) NOT NULL DEFAULT 'all',
    sent_count INT NOT NULL DEFAULT 0,
    skipped_count INT NOT NULL DEFAULT 0,
    failed_count INT NOT NULL DEFAULT 0,
    results TEXT NOT NULL DEFAULT '[]',
    image_filename TEXT,
    created_at TIMESTAMP DEFAULT current_timestamp NOT NULL
);