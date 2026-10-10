#!/usr/bin/env bash
# OGA_Music のデプロイ。サーバーの SmileMusic ディレクトリ (docker-compose.yml がある所) で使う。
#
# 各インスタンスは、イメージ oga-music:<チャンネル> (alpha / beta / release) で動く。
# コードはイメージに含めるので、python/ を書き換えただけでは動いているコンテナは変わらない。
# ビルドするのは alpha だけで、beta / release には alpha で確かめた版を昇格させる。
#
#   build [--refresh-deps] [ラベル]     alpha をビルドして版タグを付け、alpha を入れ替える
#                                       (--refresh-deps: pip の依存も最新にし直す)
#   promote <beta|release> <版>         版を昇格させ、入れ替える
#   rollback <alpha|beta|release> <版>  指定した版に戻し、入れ替える
#   start <alpha|beta|release>          タグの版でコンテナを作って起動する (初めて動かすとき)
#   restart <alpha|beta|release>        タグの版でコンテナを作り直す (同じ版でも作り直す)
#   status                              各チャンネルの版と、コンテナが動かしている版
#   versions                            残っている版の一覧
#
# 入れ替えの前に、そのインスタンスでボイス接続中や Activity 利用中のサーバーがないかを
# 管理 API で確かめ、使用中なら何も変えずに止まる (-f / --force で無視)。
# promote / rollback / build は、動いていないチャンネルのコンテナは起動しない (タグだけ付ける)。
set -euo pipefail

REPO=oga-music
DIR="${OGA_MUSIC_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
FORCE=0
REFRESH_DEPS=0
VERSION_RE='^[0-9]{8}-[0-9]{6}(-[A-Za-z0-9._-]+)?$'

die() { echo "エラー: $*" >&2; exit 1; }
usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; }
compose() { (cd "$DIR" && docker compose --profile full "$@"); }
image_id() { docker image inspect -f '{{.Id}}' "$REPO:$1" 2>/dev/null || true; }
container_image() { docker inspect -f '{{.Image}}' "oga_music_$1" 2>/dev/null || true; }
# grep -q が先に終わると左側が SIGPIPE で失敗し、pipefail で誤判定になるので || true で吸収する
container_exists() { { docker ps -a --format '{{.Names}}' || true; } | grep -qx "oga_music_$1"; }
container_running() { { docker ps --format '{{.Names}}' || true; } | grep -qx "oga_music_$1"; }
check_channel() { case "${1:-}" in alpha|beta|release) ;; *) die "チャンネルは alpha / beta / release のどれかです: ${1:-}" ;; esac; }
check_version() {
  [[ ${1:-} =~ $VERSION_RE ]] || die "版を指定してください (例: 20261011-153012。一覧は versions)"
  [ -n "$(image_id "$1")" ] || die "版 $1 がありません (一覧は versions)"
}

# イメージ ID を指している版タグ (新しいもの 1 つ)。無ければ空。
version_of_id() {
  local id=$1
  [ -n "$id" ] || return 0
  { docker image ls "$REPO" --no-trunc --format '{{.Tag}} {{.ID}}' || true; } \
    | while read -r tag tid; do
        if [ "$tid" = "$id" ] && [[ $tag =~ $VERSION_RE ]]; then echo "$tag"; fi
      done | sort | tail -n 1
}

# 使用中でなければ 0。使用中・確認できないときは理由を出して 1 (--force なら 0)。
is_idle() {
  local ch=$1 out rc
  container_running "$ch" || return 0
  if out=$(docker exec -i "oga_music_$ch" python - 2>&1 <<'PY'
import json, os, sys, urllib.request
try:
    tok = os.environ.get("SMILEMUSIC3_ADMIN_TOKEN") or ""
    if not tok:
        print("管理トークンが未設定のため、使用状況を確認できません")
        sys.exit(2)
    req = urllib.request.Request("http://127.0.0.1:8080/api/admin/guilds",
                                 headers={"Authorization": "Bearer " + tok})
    rows = json.load(urllib.request.urlopen(req, timeout=10))["guilds"]
except SystemExit:
    raise
except Exception as e:
    print(f"使用状況を確認できません: {type(e).__name__}: {e}")
    sys.exit(2)
busy = [r for r in rows if (r.get("voice") or {}).get("connected") or r.get("ws_connections")]
for r in busy:
    p = r.get("playing") or {}
    print(f"  使用中: {r.get('name')} (ボイス接続={bool((r.get('voice') or {}).get('connected'))}, "
          f"再生中={bool(p.get('is_playing'))}, Activity={r.get('ws_connections') or 0})")
sys.exit(3 if busy else 0)
PY
  ); then rc=0; else rc=$?; fi
  [ -z "$out" ] || echo "$out"
  [ "$rc" = 0 ] && return 0
  if [ "$FORCE" = 1 ]; then
    echo "--force のため、このまま続けます"
    return 0
  fi
  if [ "$rc" = 3 ]; then echo "oga_music_$ch は使用中です。" >&2
  else echo "oga_music_$ch の使用状況を確認できませんでした。" >&2; fi
  return 1
}
require_idle() { is_idle "$1" || die "空いてから実行するか、--force を付けてください (何も変えていません)"; }

wait_ready() {
  local ch=$1 i
  for i in $(seq 1 36); do
    if { docker logs "oga_music_$ch" 2>&1 || true; } | grep -q 'ready!'; then
      echo "oga_music_$ch が起動しました"
      return 0
    fi
    sleep 5
  done
  docker logs --tail 30 "oga_music_$ch" 2>&1 || true
  die "oga_music_$ch が 180 秒以内に起動しませんでした (上のログを確認してください)"
}

# チャンネルのタグを target のイメージに付け替え、動いているコンテナなら入れ替える。
# 使用中なら、タグも含めて何も変えずに止まる。
switch_to() {
  local ch=$1 target=$2 label=$3 target_id
  target_id=$(image_id "$target")
  [ -n "$target_id" ] || die "$REPO:$target がありません"
  if container_running "$ch" && [ "$(container_image "$ch")" != "$target_id" ]; then
    require_idle "$ch"
    docker tag "$REPO:$target" "$REPO:$ch"
    compose up -d --no-deps --force-recreate "oga_music_$ch"
    wait_ready "$ch"
    echo "$ch を $label にしました"
  else
    docker tag "$REPO:$target" "$REPO:$ch"
    if container_running "$ch"; then
      echo "$ch はすでに $label で動いています"
    elif container_exists "$ch"; then
      echo "$ch のタグを $label にしました (コンテナは止まっているので起動していません。起動するなら: restart $ch)"
    else
      echo "$ch のタグを $label にしました (コンテナが無いので起動していません。起動するなら: start $ch)"
    fi
  fi
}

cmd_build() {
  local label=${1:-} ver prev
  [[ -z $label || $label =~ ^[A-Za-z0-9._-]+$ ]] || die "ラベルに使えるのは英数字と . _ - だけです: $label"
  ver="$(TZ=Asia/Tokyo date +%Y%m%d-%H%M%S)${label:+-$label}"
  # 使用中なら、時間のかかるビルドの前に止める
  require_idle alpha
  prev=$(image_id alpha)
  echo "alpha をビルドします (版: $ver)"
  local extra=()
  [ "$REFRESH_DEPS" = 1 ] && extra=(--build-arg "DEPS_REFRESH=$ver")
  OGA_MUSIC_VERSION="$ver" compose build "${extra[@]+"${extra[@]}"}" oga_music_alpha
  # compose build は oga-music:alpha を新しいイメージに付け替えるので、版タグを付けてから
  # いったん元に戻し、入れ替えは switch_to に任せる (使用中なら alpha のタグは元のまま)
  docker tag "$REPO:alpha" "$REPO:$ver"
  [ -n "$prev" ] && docker tag "$prev" "$REPO:alpha"
  echo "版 $ver を作りました"
  if ! is_idle alpha; then
    echo "alpha は入れ替えていません。空いたら: $0 rollback alpha $ver" >&2
    exit 1
  fi
  switch_to alpha "$ver" "$ver"
  echo "確認できたら: $0 promote beta $ver"
}

cmd_promote() {
  local ch=${1:-} ver=${2:-}
  [[ $ch == beta || $ch == release ]] || die "昇格先は beta か release です"
  check_version "$ver"
  if [ "$ch" = release ] && [ "$(image_id beta)" != "$(image_id "$ver")" ]; then
    echo "注意: 版 $ver は beta の版ではありません (beta で確かめずに release に出します)"
  fi
  switch_to "$ch" "$ver" "$ver"
}

cmd_rollback() {
  local ch=${1:-} ver=${2:-}
  check_channel "$ch"
  check_version "$ver"
  switch_to "$ch" "$ver" "$ver"
}

cmd_start() {
  local ch=${1:-}
  check_channel "$ch"
  [ -n "$(image_id "$ch")" ] || die "$REPO:$ch がありません (先に promote $ch <版>)"
  if container_running "$ch"; then echo "oga_music_$ch はすでに動いています"; return 0; fi
  compose up -d --no-deps "oga_music_$ch"
  wait_ready "$ch"
}

cmd_restart() {
  local ch=${1:-}
  check_channel "$ch"
  [ -n "$(image_id "$ch")" ] || die "$REPO:$ch がありません"
  container_exists "$ch" || die "oga_music_$ch のコンテナがありません (初めて動かすなら: start $ch)"
  require_idle "$ch"
  compose up -d --no-deps --force-recreate "oga_music_$ch"
  wait_ready "$ch"
}

cmd_status() {
  local ch id ver cid cver state
  for ch in alpha beta release; do
    id=$(image_id "$ch")
    ver=$(version_of_id "$id")
    if [ -z "$id" ]; then ver="(イメージなし)"; elif [ -z "$ver" ]; then ver="(版タグなし)"; fi
    if container_exists "$ch"; then
      cid=$(container_image "$ch")
      state=$(docker inspect -f '{{.State.Status}}' "oga_music_$ch")
      if [ "$cid" = "$id" ]; then
        state="$state、この版で動作"
      else
        cver=$(version_of_id "$cid")
        state="$state、別の版で動作: ${cver:-版タグなし}"
      fi
    else
      state="コンテナなし"
    fi
    echo "$ch: $ver / $state"
  done
}

cmd_versions() {
  local a b r
  a=$(image_id alpha); b=$(image_id beta); r=$(image_id release)
  { docker image ls "$REPO" --no-trunc --format '{{.Tag}} {{.ID}} {{.CreatedSince}}' || true; } \
    | while read -r tag tid since; do
        [[ $tag =~ $VERSION_RE ]] || continue
        on=""
        [ "$tid" = "$a" ] && on+=" alpha"
        [ "$tid" = "$b" ] && on+=" beta"
        [ "$tid" = "$r" ] && on+=" release"
        echo "$tag  ($since)${on:+  ←$on}"
      done | sort -r
}

args=()
for a in "$@"; do
  case "$a" in
    -f|--force) FORCE=1 ;;
    --refresh-deps) REFRESH_DEPS=1 ;;
    -h|--help) usage; exit 0 ;;
    *) args+=("$a") ;;
  esac
done
set -- "${args[@]+"${args[@]}"}"

cmd=${1:-}
shift || true
case "$cmd" in
  build) cmd_build "$@" ;;
  promote) cmd_promote "$@" ;;
  rollback) cmd_rollback "$@" ;;
  start) cmd_start "$@" ;;
  restart) cmd_restart "$@" ;;
  status) cmd_status ;;
  versions) cmd_versions ;;
  *) usage; exit 1 ;;
esac
