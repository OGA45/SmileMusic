# SmileMusic

[akomekagome/SmileMusic](https://github.com/akomekagome/SmileMusic) を fork して、自分が欲しい機能を継続的に追加している Discord 音楽 BOT です。

オリジナルの **prefix / slash コマンド** ベースの操作に加え、**Discord Activity (Embedded App)** による **Spotify ライクな Web UI** を備えていて、ライブラリ管理・プレイリスト操作・リアルタイムオーディオビジュアライザなどがブラウザ的な UX で使えます。

---

## 主な機能

### Discord Activity (Web UI) - 主モード

`smile_music3` を起動すると、対象ボイスチャンネルから Activity を開くだけで以下が使えます。

- **プレイヤー UI** : アートワーク + プログレスバー + 再生/スキップ/シークバー (ホバーで時刻ツールチップ)
- **ライブラリ (guild 共有)** :
  - 各ユーザ作成プレイリストを guild の library に登録 → 全員から見える
  - 並び替え (追加日 / 名前 / 曲数)、検索、フィルター (タグ・追加者)
  - 追加者アバター付きの一覧
- **プレイリスト詳細画面** :
  - 再生 / 楽曲リスト / アートワーク / 統計
  - 編集 (自分のプレイリスト) : リネーム / 順序入替 / 楽曲追加・削除 / タグ
  - 読み取り専用 (ライブラリ経由) : 楽曲別の再生・全曲再生
- **オーディオビジュアライザ (リアル音響駆動)** :
  - FFT 32 バンド + ビート検出を bot 側で計算 → 25 Hz で WS broadcast
  - 9 種類: bars / mirror / wmp / dots / digital / wave / radial / pulse / particles
- **個人設定 (per user, persistent)** :
  - テーマカラー (任意色、サムネ自動連動)
  - 背景色をサムネに合わせる
  - ビジュアライザ種類・サムネ連動
- **プレイバックモード** : 単曲ループ / 全体ループ / シャッフル / ノーマライズ (loudnorm)
- **プロセス一覧** : 進行中・完了済みのインポート処理を一覧で確認
- **WS 自動再接続** + 認証エラー時の停止
- **VC 自動参加** : Activity 開いた瞬間に BOT が自動 join

### 対応する音源 / インポート元

| サービス | 単曲再生 | プレイリスト一括取込 | 備考 |
|---|---|---|---|
| YouTube | ✓ | ✓ (再生リスト) | YouTube Data API v3 + バッチ取得 |
| niconico | ✓ | ✓ (マイリスト) | nvapi 経由、ローカル opus キャッシュで再生 |
| Bandcamp | ✓ | ✓ (アルバム) | data-tralbum JSON 抽出 (yt-dlp フォールバック) |
| SoundCloud | ✓ | - | yt-dlp 経由 |
| Jellyfin | ✓ | ✓ (アルバム / プレイリスト) | 自前サーバ前提 (環境変数で設定) |
| SUNO | ✓ | - | UUID → 直接 MP3 URL を構築 |
| Spotify | track / album / playlist | - | spotdl 経由 |

### 既存の slash コマンド (legacy)

Activity を使わなくても、テキストチャンネルからすべて操作できます。一覧は[後述](#slash-コマンド)。

---

## アーキテクチャ

```
┌────────────────────────────────────────────────┐
│ Discord クライアント                            │
│  ├─ Bot メッセージ / slash コマンド             │
│  └─ Activity (iframe: React + Vite)            │
│        ↕ WebSocket / HTTP                       │
└────────────────────────────────────────────────┘
                    ↕
┌────────────────────────────────────────────────┐
│ smile_music_py3 (Python 3.14)                  │
│  ├─ discord.py: voice playback + slash cmd     │
│  ├─ FastAPI / uvicorn (port 8080)              │
│  │   ├─ /ws/{guild_id} (WebSocket)             │
│  │   ├─ /api/token (OAuth code 交換)            │
│  │   ├─ /api/image (CDN 画像 proxy + LRU)       │
│  │   └─ 静的: /srv/activity_dist (Vite build)   │
│  ├─ FFmpeg subprocess (PCM 抽出 + RMS タップ)   │
│  └─ Activity の状態管理 (per-guild)             │
└────────────────────────────────────────────────┘
       ↕                                ↕
   PostgreSQL                  外部 (YouTube / niconico / ...)
   (playlists,
    library,
    user_prefs,
    history)
```

- BOT と Activity API は同一プロセス・同一 event loop で動作
- discord.py の voice playback は別スレッド (audio thread)。`OriginalFFmpegPCMAudio.read()` で PCM フレームをタップ → FFT + ビート検出 → Activity に WS 送出
- yt-dlp は同期なので `loop.run_in_executor` でスレッド送り

---

## セットアップ

### 必要なもの

- Docker / docker-compose
- Discord Developer Portal で作成した BOT (token + Activity 用の client id/secret)
- YouTube Data API v3 キー
- (任意) Spotify Client ID/Secret
- (任意) Jellyfin インスタンス + BOT 用アカウント
- Traefik ネットワーク (production の場合) または `127.0.0.1:8080` 直 (dev)

### 環境変数 (`.env`)

```
# DB
POSTGRES_HOST=smile_music_db
POSTGRES_USER=postgres
POSTGRES_PASSWORD=...
POSTGRES_DB=smilemusic
POSTGRES_PORT=5432

# Discord (BOT)
SMILEMUSIC3_DISCORD_TOKEN=...
SMILEMUSIC3_PREFIX=?

# Discord (Activity / Embedded App)
SMILEMUSIC3_DISCORD_CLIENT_ID=...
SMILEMUSIC3_DISCORD_CLIENT_SECRET=...
SMILEMUSIC3_ACTIVITY_HOST=activity.example.com   # 本番のホスト名

# Music sources
YOUTUBE_TOKEN=AIza...                            # YouTube Data API v3
SPOTIFY_CLIENT_ID=...                            # 任意
SPOTIFY_CLIENT_SECRET=...

# Jellyfin (任意。設定すれば Activity から URL インポート可)
JELLYFIN_BASE_URL=https://jellyfin.example.com
JELLYFIN_USERNAME=bot-account
JELLYFIN_PASSWORD=...
```

### Discord Developer Portal 側の設定

- **OAuth2 → Redirects**: `https://<your-client-id>.discordsays.com/` を登録 (Activity 用)
- **Activities → URL Mappings**: 自前ホスト名 → `SMILEMUSIC3_ACTIVITY_HOST` をマップ
- **Bot Intents**: Server Members + Message Content + Voice State 等を有効

### ビルドと起動

```bash
docker compose build smile_music_py3 --no-cache
docker compose up -d
```

ローカル確認は `http://localhost:8080/` で Activity の静的フロントが見えます (実際の動作には Discord 経由のアクセスが必要)。

### DB 初期化

`initdb/Init.sql` がコンテナ初回起動時に自動実行されます。スキーマ移行は BOT 起動時の `playlist_store.init_schema` で `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN IF NOT EXISTS` 形式で実施されます。

---

## Activity の使い方

1. Discord 音声チャンネルから Activity を起動
2. BOT が自動で同じ VC に join
3. メニュー (右上アバター) :
   - マイプレイリストの新規登録 / URL からインポート / 確認
   - プロセス一覧 (取り込みの進捗確認)
   - 個人設定 (テーマカラー / 背景色 / ビジュアライザ)
4. ライブラリ (左サイドバー) :
   - 「キュー」アイコン: 即興で /play で積まれたキューを表示
   - 自分が作成したプレイリストを「＋」でライブラリに追加 → guild の他メンバから見える
5. プレイリスト詳細 :
   - ライブラリから開く → 読み取り専用 + 再生
   - メニュー > マイプレイリストを確認 → ⓘ で開く → 編集モード

### URL インポート対応

「マイプレイリストを URL からインポート」または「新規登録」で URL を貼ると自動判別:

| 形式 | 例 |
|---|---|
| YouTube 再生リスト | `youtube.com/playlist?list=...` |
| Bandcamp アルバム | `artist.bandcamp.com/album/...` |
| niconico マイリスト | `nicovideo.jp/mylist/12345` |
| Jellyfin アルバム / プレイリスト | `jellyfin.example.com/web/#/details?id=...` |
| 単曲 URL | YouTube / niconico / SoundCloud / SUNO 等 |

---

## Slash コマンド

(legacy - Activity を使わない場合の操作)

|コマンド名|内容|
|---|---|
|`/play <url or キーワード>`|指定 URL / キーワードから再生 (オプションで normalize 付与可能)|
|`/list <url>`|YouTube 再生リスト / Spotify アルバム&プレイリストをキューに追加|
|`/live <url>`|YouTube ライブ配信を再生|
|`/queue`|現在のキュー一覧|
|`/now`|再生中の曲情報 (niconico はタグ付き)|
|`/skip`|現在の曲をスキップ|
|`/pause` / `/resume`|一時停止 / 再開|
|`/clear`|キューを空にする|
|`/seek <時間>`|指定時間までシーク (例: `1:30`)|
|`/rewind <時間>`|指定時間ぶん巻き戻し|
|`/join` / `/leave`|VC に参加 / 退出|
|`/set_volume <値>`|ボリューム設定 (デフォルト 1)|
|`/set_stream <0/1>`|ストリーム再生のオンオフ|
|`/info_stream`|ストリーム再生設定の確認|
|`/loop`|現在の曲をループ|
|`/loopqueue`|キュー全体をループ|
|`/shuffle`|キューをシャッフル|
|`/skipto <n>`|n 番目までスキップ|
|`/remove <n>`|n 番目をキューから削除|
|`/histry`|再生履歴を表示|
|`/delete_setting`|全設定をデフォルトに戻す|
|`/help`|ヘルプ|

---

## 主な変更点 (fork からの)

### コア
- Python 3.14 / discord.py 最新 / yt-dlp / FFmpeg 自前ビルド (libopus 込み)
- PostgreSQL でプレイリスト / ライブラリ / 個人設定を永続化
- FastAPI + uvicorn を discord.py と同一 event loop で動作

### Activity / Embedded App
- React + Vite + TypeScript で Spotify ライクなフロントエンドを実装
- WebSocket で per-guild の再生状態を双方向同期
- 1 ユーザ複数 guild に対応 (per-guild の WS セッション)

### 再生まわり
- yt-dlp の extract_info を **TTL 4 時間の LRU キャッシュ** + 同一 URL の inflight Future 共有
- 次曲の **プリフェッチ** (現在曲再生中に裏で抽出 → 切替時ほぼ瞬時)
- 全曲を **PCM 経路** に統一して FFT タップ (visualizer 用) を可能に
- libopus VBR の実ビットレートを **ファイルサイズ ÷ 長さ** で算出して表示
- niconico ローカルダウンロードキャッシュ (`.opus`)
- SUNO / Jellyfin 等の専用 resolver
- ノーマライズ (loudnorm) / 単曲ループ / 3-state loop / シャッフル

### Activity / UI 機能
- 詳細画面 + ミニプレイヤー (Spotify 風)
- マルチサービスインポート (YouTube / Bandcamp / niconico mylist / Jellyfin / SUNO)
- リアルタイム visualizer (9 種類、theme color 連動)
- タグ + 検索 + フィルター
- プロセス一覧 (取り込み進捗)
- 画像 proxy + 24h LRU キャッシュ + 並行数制限 + コネクション再利用
- 全テキストのマーキー (長文タイトルを hover でスクロール)

### YouTube Data API 最適化
- `videos.list` を 50 件バッチ呼出に変更 (旧: 1 件ずつ) → クォータ 25x 削減、速度 10x 改善
- playlistItems 全ページから videoId を集めてから 1 度にバッチ呼出

---

## 開発

```
.
├── activity/              # Discord Activity (React + Vite + TS)
│   ├── src/App.tsx        # メインコンポーネント
│   ├── src/App.css
│   └── src/ws.ts          # WebSocket クライアント + 型
├── python/
│   ├── smile_music3.py    # メイン (Activity 対応版)
│   ├── smile_music2.py    # legacy
│   ├── smile_music.py     # legacy
│   └── activity_api/
│       ├── server.py      # FastAPI factory
│       ├── routes_ws.py   # WebSocket
│       ├── routes_image.py# 画像 proxy (LRU)
│       ├── routes_token.py# Discord OAuth2 code 交換
│       ├── state_bus.py   # guild 状態 pub/sub
│       ├── playlist_store.py  # PostgreSQL アクセス
│       └── auth.py        # Discord OAuth ユーザ確認
├── initdb/Init.sql        # DB 初期スキーマ
├── Dockerfile             # 2 stage build (vite + python)
└── docker-compose.yml
```

### Activity フロントの開発サーバ

```bash
cd activity
npm install
npm run dev   # http://localhost:5173
```

`vite.config.ts` で `127.0.0.1:8080` の API/WS にプロキシしているので、BOT を docker で起動した状態で `localhost:5173` を開けば iframe 外でも動作確認できます。

### スキーマ追加

`playlist_store.init_schema` 内に `ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...` を追記 + `initdb/Init.sql` (新規 DB 用) を同期。

---

## ライセンス / 連絡先

オリジナル: [akomekagome/SmileMusic](https://github.com/akomekagome/SmileMusic)

問題があればメール / Twitter 等に連絡をください。
