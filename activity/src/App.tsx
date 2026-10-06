import {
  memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState,
} from 'react'
import { initDiscord, isInDiscord, openExternal, type ActivityContext } from './discord'
import {
  connectActivityWS,
  type VisualizerKind,
  type WsConnection,
  type WsLibraryItem,
  type WsPlaylistDetail,
  type WsPlaylistInfo,
  type WsPlaylistListItem,
  type WsProcess,
  type WsStatePayload,
  type WsTrack,
} from './ws'

type PosBase = { positionMs: number; at: number; isPlaying: boolean }
type WsStatus = 'idle' | 'connecting' | 'open' | 'closed'
type Mode = 'queue' | 'playlist'
type ModalKind = null | 'create' | 'import' | 'confirm' | 'addLibrary' | 'settings' | 'guildSettings' | 'processes'
type Toast = { id: number; level: 'info' | 'success' | 'error'; message: string }
// キューへの曲追加の結果 (キュー画面の入力欄の下に出す)
type QueueAddResult = { level: 'info' | 'success' | 'error'; message: string }

const PLACEHOLDER_ART = 'https://placehold.co/600x600/1db954/ffffff?text=Album'

// 利用規約・プライバシーポリシー (Cloudflare Pages。中身はリポジトリの legal/)。
// Pages は *.html を拡張子なしの URL へ 308 で転送するので、転送先の正規 URL を使う。
const LEGAL_TERMS_URL = 'https://smilemusic3-legal.oga.ninja/terms'
const LEGAL_PRIVACY_URL = 'https://smilemusic3-legal.oga.ninja/privacy'

// FE-MAINT-07: queueNextIndex のセンチネル値を名前付き定数化
// (-1 = 次の曲なし は ws.ts の型コメント参照。-2 = 1曲ループ中で次も現在の曲)
const QUEUE_NEXT_REPEAT_CURRENT = -2

// FE-MAINT-04: タグ文字列 (カンマ/読点区切り) を配列へ正規化 (重複ロジックを集約)
function parseTags(raw: string): string[] {
  return raw
    .split(/[,、]/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
}

// FE-MAINT-05 / FE-PERF-10: トラック総再生時間を「N 時間 M 分 / M 分」に整形
function formatTotalDuration(tracks: { durationMs: number }[]): string {
  const totalSec = tracks.reduce((acc, t) => acc + Math.floor(t.durationMs / 1000), 0)
  const hh = Math.floor(totalSec / 3600)
  const mm = Math.floor((totalSec % 3600) / 60)
  // 数字と単位の間はノーブレークスペース: 狭い幅で「6」と「分」が別の行に割れないように
  return hh > 0 ? `${hh}\u00a0時間 ${mm}\u00a0分` : `${mm}\u00a0分`
}

// FE-MAINT-08: CSS カスタムプロパティ注入の as-string キャストを 1 箇所に集約
// FE-PERF-03: ウィンドウ仮想化。固定行高 (DETAIL_ROW_H) の前提で、スクロール
// コンテナ (.detail-view = ページスクロール) の可視範囲 + overscan の行だけを
// レンダリングし、上下を spacer <li> で埋めてスクロールバー長を保つ。
// 2000曲規模でも DOM 上の行は数十個に抑えられる。
const DETAIL_ROW_H = 56

function useWindowedRows(count: number, rowH: number, overscan = 8) {
  const scrollRef = useRef<HTMLDivElement | null>(null)  // .detail-view
  const listRef = useRef<HTMLOListElement | null>(null)  // <ol>
  const [range, setRange] = useState<{ start: number; end: number }>(
    () => ({ start: 0, end: Math.min(count, 40) }),
  )
  const recompute = useCallback(() => {
    const sc = scrollRef.current
    const list = listRef.current
    if (!sc || !list) return
    // リストの「コンテンツ先頭からの top」を rect 差分で堅牢に求める
    // (offsetParent の position に依存しない)。
    const listTop = list.getBoundingClientRect().top
      - sc.getBoundingClientRect().top + sc.scrollTop
    const viewTop = sc.scrollTop - listTop
    const viewH = sc.clientHeight
    const start = Math.max(0, Math.floor(viewTop / rowH) - overscan)
    const end = Math.min(count, Math.ceil((viewTop + viewH) / rowH) + overscan)
    setRange((prev) => (prev.start === start && prev.end === end
      ? prev : { start, end }))
  }, [count, rowH, overscan])
  useLayoutEffect(() => {
    recompute()
    const sc = scrollRef.current
    if (!sc) return
    sc.addEventListener('scroll', recompute, { passive: true })
    const ro = new ResizeObserver(recompute)
    ro.observe(sc)
    return () => {
      sc.removeEventListener('scroll', recompute)
      ro.disconnect()
    }
  }, [recompute])
  return { scrollRef, listRef, range }
}

// FE-MAINT-02: アートワークから「背景用 (暗め)」「ビジュアライザ用 (鮮やか)」の
// 2 色を抽出する副作用と state を App から切り出した専用フック。
// App はこれを呼んで { bgTint, visualizerTint } を受け取るだけになり、
// 色抽出ロジックを単独で差し替え/テストできる。
function useArtworkTint(
  artwork: string | undefined,
  bgTintEnabled: boolean,
  visualizerTintEnabled: boolean,
): { bgTint: string | null; visualizerTint: string | null } {
  const [bgTint, setBgTint] = useState<string | null>(null)
  const [visualizerTint, setVisualizerTint] = useState<string | null>(null)
  useEffect(() => {
    if ((!bgTintEnabled && !visualizerTintEnabled) || !artwork) {
      setBgTint(null)
      setVisualizerTint(null)
      return
    }
    let cancelled = false
    extractArtworkColors(artwork).then((colors) => {
      if (cancelled) return
      setBgTint(bgTintEnabled ? (colors?.bg ?? null) : null)
      setVisualizerTint(visualizerTintEnabled ? (colors?.accent ?? null) : null)
    })
    return () => { cancelled = true }
  }, [artwork, bgTintEnabled, visualizerTintEnabled])
  return { bgTint, visualizerTint }
}

// FE/BC-PERF-06 補助: 行がしばらく表示に留まってから初めて画像を読み込む。
// 高速スクロールで通り過ぎるだけの行 (仮想化で即 unmount される) は画像要求を
// 出さないので、最終的に見える行のサムネが大量のフライバイ要求の後ろで待たされない。
const DeferredImg = memo(function DeferredImg({
  src,
  className,
  delay = 150,
}: {
  src: string
  className?: string
  delay?: number
}) {
  const [show, setShow] = useState(false)
  useEffect(() => {
    setShow(false)
    const id = window.setTimeout(() => setShow(true), delay)
    return () => window.clearTimeout(id)
  }, [src, delay])
  if (!show || !src) {
    // 同サイズのプレースホルダ (レイアウトシフト防止)
    return <span className={className} aria-hidden />
  }
  return (
    <img className={className} src={src} alt="" loading="lazy" decoding="async" />
  )
})

function cssVars(vars: Record<string, string>): React.CSSProperties {
  return vars as React.CSSProperties
}

export function App() {
  const [ctx, setCtx] = useState<ActivityContext | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [wsStatus, setWsStatus] = useState<WsStatus>('idle')

  const [mode, setMode] = useState<Mode>('queue')
  const [track, setTrack] = useState<WsTrack | null>(null)
  const [upnext, setUpnext] = useState<WsTrack[]>([])
  const [musicQueue, setMusicQueue] = useState<WsTrack[]>([])
  const [loading, setLoading] = useState(false)
  const [playlist, setPlaylist] = useState<WsPlaylistInfo | null>(null)
  const [posBase, setPosBase] = useState<PosBase>({
    positionMs: 0, at: Date.now(), isPlaying: false,
  })

  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [sidebarContent, setSidebarContent] = useState<'library' | 'queue'>('library')
  // library 並び替え / 検索 / フィルター
  type LibSort = 'added_new' | 'added_old' | 'name_asc' | 'name_desc' | 'count_high' | 'count_low'
  const [librarySearch, setLibrarySearch] = useState('')
  const [librarySort, setLibrarySort] = useState<LibSort>('added_new')
  const [libraryTagFilter, setLibraryTagFilter] = useState<string | null>(null)
  const [libraryAdderFilter, setLibraryAdderFilter] = useState<string | null>(null)
  const [libraryFilterOpen, setLibraryFilterOpen] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [modal, setModal] = useState<ModalKind>(null)
  const [guildName, setGuildName] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const searchRef = useRef<HTMLInputElement>(null)

  const [playlists, setPlaylists] = useState<WsPlaylistListItem[]>([])
  const [libraryItems, setLibraryItems] = useState<WsLibraryItem[]>([])
  const [autoSelectLast, setAutoSelectLast] = useState(false)
  const [autoAddToLibrary, setAutoAddToLibrary] = useState(false)
  const [bgTintEnabled, setBgTintEnabled] = useState(true)
  const [audioVisualizer, setAudioVisualizer] = useState<VisualizerKind>('off')
  const [themeColor, setThemeColor] = useState<string | null>(null)
  const [visualizerTintEnabled, setVisualizerTintEnabled] = useState(false)
  const [lastUsedPlaylistId, setLastUsedPlaylistId] = useState<string | null>(null)
  const [guildSettings, setGuildSettings] = useState<{
    announceChannelId: string | null
    announceChannelName: string
    systemChannelName: string
    canManage: boolean
    channels: { id: string; name: string }[]
  } | null>(null)
  const [queueLoop, setQueueLoop] = useState(false)
  const [queueLoopSingle, setQueueLoopSingle] = useState(false)
  const [queueNextIndex, setQueueNextIndex] = useState(-1)
  const [normalize, setNormalize] = useState(false)
  const artworkRef = useRef<HTMLImageElement | null>(null)
  const [playlistDetail, setPlaylistDetail] = useState<WsPlaylistDetail | null>(null)
  const [detailMode, setDetailMode] = useState<'edit' | 'readonly'>('edit')
  const [queueDetailOpen, setQueueDetailOpen] = useState(false)
  // キューへの曲追加の進行状態。サーバーの返事 (notify) と req_id で突き合わせるので、
  // WS のクロージャからも読めるよう要求は ref に持つ。
  const [queueAddPending, setQueueAddPending] = useState(false)
  // 入力中の URL と直近の結果は App で持つ (取得中にキュー画面を離れても消えないように)
  const [queueAddUrl, setQueueAddUrl] = useState('')
  const [queueAddResult, setQueueAddResult] = useState<QueueAddResult | null>(null)
  const queueAddReqRef = useRef<{ id: string; url: string; timer: number } | null>(null)
  // 切断・時間切れで待つのをやめた要求。返事が遅れて届いたら結果表示に反映する
  const queueAddOrphanRef = useRef<{ id: string; url: string } | null>(null)
  const queueAddSeqRef = useRef(0)
  // music_queue は先頭 200 件だけ届くので、実際の件数を別に持つ
  const [musicQueueTotal, setMusicQueueTotal] = useState(0)
  // playlist_detail メッセージが「ユーザー操作起点(=画面遷移したい)」か
  // 「変更通知(=データだけ更新したい)」かを区別するためのフラグ。
  // ナビ起点なら値が入り、変更通知なら null。応答到着時にまとめて反映する。
  const pendingDetailNavRef = useRef<'edit' | 'readonly' | null>(null)

  const [toasts, setToasts] = useState<Toast[]>([])
  const [processes, setProcesses] = useState<WsProcess[]>([])

  const wsRef = useRef<WsConnection | null>(null)
  // 25Hz で届く audio_features は ref に貯めるだけ。Visualizer の rAF が毎フレーム読みに行く。
  const audioRmsRef = useRef<number>(0)
  const audioBandsRef = useRef<number[]>([])
  // 直近のビート発火時刻 (performance.now()) を保持。Visualizer がフラッシュ減衰に使う。
  const audioBeatAtRef = useRef<number>(0)

  useEffect(() => {
    initDiscord()
      .then(setCtx)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [])

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (modal) setModal(null)
      else if (menuOpen) setMenuOpen(false)
      else if (sidebarOpen) closeSidebar()
      else if (playlistDetail) setPlaylistDetail(null)
      else if (queueDetailOpen) setQueueDetailOpen(false)
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [modal, menuOpen, sidebarOpen, playlistDetail, queueDetailOpen])

  // FE-MAINT-03: 'state' メッセージ (全体状態の反映) を関数に括り出す。
  const applyState = useCallback((state: WsStatePayload) => {
    setMode(state.mode)
    setGuildName(state.guildName)
    setTrack(state.track)
    // MOD-PERF-04: upnext / musicQueue は 'queue_full' メッセージで別途更新する
    setLoading(state.loading)
    setPlaylist(state.playlist)
    setQueueLoop(state.queueLoop)
    setQueueLoopSingle(state.queueLoopSingle)
    setQueueNextIndex(state.queueNextIndex)
    setNormalize(state.normalize)
    setPosBase({
      positionMs: state.positionMs,
      at: Date.now(),
      isPlaying: state.isPlaying,
    })
  }, [])

  useEffect(() => {
    if (!ctx?.guildId || !ctx.accessToken) return
    let mounted = true
    let reconnectTimer: number | null = null
    let retryAttempt = 0
    // 認証/メンバ拒否系のクローズ理由 (これらでは無限再接続させない)
    const FATAL_CLOSE_CODES = new Set([4401, 4403])

    const scheduleReconnect = (code: number) => {
      if (!mounted) return
      if (FATAL_CLOSE_CODES.has(code)) {
        console.warn('ws closed with fatal code, no retry:', code)
        return
      }
      // 指数バックオフ: 1s, 2s, 4s, 8s, 15s (上限)
      const delay = Math.min(15000, 1000 * Math.pow(2, Math.min(retryAttempt, 4)))
      retryAttempt += 1
      reconnectTimer = window.setTimeout(() => {
        if (mounted) connect()
      }, delay)
    }

    const connect = () => {
      if (!mounted || !ctx?.guildId || !ctx.accessToken) return
      setWsStatus('connecting')
      const conn = connectActivityWS({
        guildId: ctx.guildId,
        accessToken: ctx.accessToken,
        inDiscord: isInDiscord,
        onOpen: () => {
          setWsStatus('open')
          retryAttempt = 0
          conn.listPlaylists()
        },
        onClose: ({ code }) => {
          setWsStatus('closed')
          if (wsRef.current === conn) wsRef.current = null
          // 追加待ちは解除する (切断中に送られた返事は届かない)。サーバー側の処理は続くので、
          // 再接続後に返事が届けば結果表示に反映する (queueAddOrphanRef)。
          if (queueAddReqRef.current) {
            releaseQueueAdd(true)
            const m = '接続が切れました。再接続後に結果が届くことがあります。届かない場合はキューを確認してください'
            setQueueAddResult({ level: 'info', message: m })
            pushToast('info', m)
          }
          scheduleReconnect(code)
        },
        onMessage: (msg) => {
          // FE-MAINT-03: kind ごとに switch でディスパッチ (旧: if-else 連鎖)
          switch (msg.kind) {
            case 'state':
              applyState(msg.state)
              break
            case 'progress':
              setPosBase((b) => ({
                positionMs: msg.positionMs,
                at: Date.now(),
                isPlaying: b.isPlaying,
              }))
              break
            case 'queue':
              setUpnext(msg.queue)
              break
            case 'queue_full':
              // MOD-PERF-04: キューが変化したときだけ届く (state とは別送)
              setUpnext(msg.upnext)
              setMusicQueue(msg.musicQueue)
              setMusicQueueTotal(msg.musicQueueTotal)
              break
            case 'stopped':
              setTrack(null)
              setUpnext([])
              setPosBase({ positionMs: 0, at: Date.now(), isPlaying: false })
              break
            case 'notify': {
              pushToast(msg.level, msg.message)
              const req = queueAddReqRef.current
              const orphan = queueAddOrphanRef.current
              if (msg.reqId && req && msg.reqId === req.id) {
                releaseQueueAdd(false)
                applyQueueAddResult(req.url, msg.level, msg.message)
                // 新しい要求に結果が出たら古い要求は忘れる。ただし「前の曲を追加中です」(info) は
                // 古い要求がまだサーバーで動いている印なので、その返事を拾えるよう残す
                if (msg.level !== 'info') queueAddOrphanRef.current = null
              } else if (msg.reqId && !req && orphan && msg.reqId === orphan.id) {
                // 切断・時間切れで待つのをやめた後に届いた返事
                queueAddOrphanRef.current = null
                applyQueueAddResult(orphan.url, msg.level, msg.message)
              }
              break
            }
            case 'playlists':
              setPlaylists(msg.playlists)
              setAutoSelectLast(msg.autoSelectLast)
              setAutoAddToLibrary(msg.autoAddToLibrary)
              setBgTintEnabled(msg.bgTintEnabled)
              setAudioVisualizer(msg.audioVisualizer)
              setThemeColor(msg.themeColor)
              setVisualizerTintEnabled(msg.visualizerTintEnabled)
              setLastUsedPlaylistId(msg.lastUsedPlaylistId)
              break
            case 'library':
              setLibraryItems(msg.items)
              break
            case 'processes':
              setProcesses(msg.items)
              break
            case 'audio_features':
              audioRmsRef.current = msg.rms
              if (msg.bands.length) audioBandsRef.current = msg.bands
              if (msg.beat) audioBeatAtRef.current = performance.now()
              break
            case 'playlist_detail': {
              setPlaylistDetail(msg.detail)
              // ユーザー操作で開いたなら、その瞬間にモード切替 + 他ビューを閉じる。
              // 単なる変更通知 (add/remove/rename 由来) は何も触らずデータだけ反映。
              const pending = pendingDetailNavRef.current
              if (pending) {
                setDetailMode(pending)
                setQueueDetailOpen(false)
                pendingDetailNavRef.current = null
              }
              break
            }
            case 'playlist_detail_page':
              // BC-PERF-06: 追加ページ。現在の詳細に順序整合する位置だけ追記する。
              setPlaylistDetail((cur) => {
                if (!cur || cur.id !== msg.id) return cur
                if (msg.offset !== cur.tracks.length) return cur
                return { ...cur, tracks: [...cur.tracks, ...msg.tracks] }
              })
              break
            case 'guild_settings':
              setGuildSettings((prev) => ({
                announceChannelId: msg.announceChannelId,
                announceChannelName: msg.announceChannelName,
                systemChannelName: msg.systemChannelName,
                canManage: msg.canManage ?? prev?.canManage ?? false,
                channels: msg.channels ?? prev?.channels ?? [],
              }))
              break
          }
        },
      })
      wsRef.current = conn
    }

    connect()

    return () => {
      mounted = false
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer)
        reconnectTimer = null
      }
      wsRef.current?.close()
      wsRef.current = null
    }
  }, [ctx?.guildId, ctx?.accessToken])

  // FE-PERF-07: 検索入力をデバウンス (入力ごとに全件フィルタ/ソートしない)。
  const [debouncedSearch, setDebouncedSearch] = useState('')
  useEffect(() => {
    const id = window.setTimeout(() => setDebouncedSearch(librarySearch), 200)
    return () => window.clearTimeout(id)
  }, [librarySearch])

  // 重い O(n log n) ソートは librarySort / libraryItems のみに依存させ、検索キー入力では
  // 再ソートしない。フィルタ (O(n)) はソート済みリストに対して後段で行う。
  const sortedLibraryItems = useMemo(() => {
    const items = libraryItems.slice()
    items.sort((a, b) => {
      switch (librarySort) {
        case 'name_asc':   return a.name.localeCompare(b.name)
        case 'name_desc':  return b.name.localeCompare(a.name)
        case 'added_new':  return (b.addedAt || '').localeCompare(a.addedAt || '')
        case 'added_old':  return (a.addedAt || '').localeCompare(b.addedAt || '')
        case 'count_high': return b.trackCount - a.trackCount
        case 'count_low':  return a.trackCount - b.trackCount
        default:           return 0
      }
    })
    return items
  }, [libraryItems, librarySort])

  const filteredLibraryItems = useMemo(() => {
    let items = sortedLibraryItems
    const q = debouncedSearch.trim().toLowerCase()
    if (q) {
      items = items.filter((p) =>
        p.name.toLowerCase().includes(q)
        || (p.tags || []).some((t) => t.toLowerCase().includes(q))
        || (p.addedByUsername || '').toLowerCase().includes(q),
      )
    }
    if (libraryTagFilter) {
      items = items.filter((p) =>
        (p.tags || []).some(
          (t) => t.toLowerCase() === libraryTagFilter.toLowerCase(),
        ),
      )
    }
    if (libraryAdderFilter) {
      items = items.filter((p) => p.addedByUserId === libraryAdderFilter)
    }
    return items
  }, [sortedLibraryItems, debouncedSearch, libraryTagFilter, libraryAdderFilter])

  // フィルター候補 (タグ・追加者) は現在のライブラリから抽出
  const availableTags = useMemo(() => {
    const set = new Set<string>()
    for (const p of libraryItems) for (const t of (p.tags || [])) set.add(t)
    return Array.from(set).sort((a, b) => a.localeCompare(b))
  }, [libraryItems])
  const availableAdders = useMemo(() => {
    const map = new Map<string, { name: string; avatar: string }>()
    for (const p of libraryItems) {
      if (p.addedByUserId && !map.has(p.addedByUserId)) {
        map.set(p.addedByUserId, {
          name: p.addedByUsername || p.addedByUserId,
          avatar: p.addedByAvatarUrl || '',
        })
      }
    }
    return Array.from(map.entries())
      .map(([uid, v]) => ({ uid, name: v.name, avatar: v.avatar }))
      .sort((a, b) => a.name.localeCompare(b.name))
  }, [libraryItems])

  const pushToast = (level: Toast['level'], message: string) => {
    const id = Date.now() + Math.random()
    setToasts((ts) => [...ts, { id, level, message }])
    window.setTimeout(() => {
      setToasts((ts) => ts.filter((t) => t.id !== id))
    }, 4000)
  }

  // キューへの曲追加: 以下 2 つは ref と setter しか使わないので、WS のクロージャ
  // (初回 render 時のもの) や useCallback([]) から呼んでも安全。
  // 追加待ちを解除する。orphan=true なら遅れて届く返事を拾えるよう要求を控えておく。
  const releaseQueueAdd = (orphan: boolean) => {
    const req = queueAddReqRef.current
    if (!req) return
    window.clearTimeout(req.timer)
    queueAddReqRef.current = null
    setQueueAddPending(false)
    if (orphan) queueAddOrphanRef.current = { id: req.id, url: req.url }
  }
  // サーバーの返事を入力欄の下 (role=status) にも出す (トーストは 4 秒で消え、読み上げ
  // られず、スマホではキーボードに隠れるため)。成功したら入力欄を空にする (その間に
  // 書き換えていたら残す)。失敗したら URL を残して直せるようにする。
  const applyQueueAddResult = (url: string, level: QueueAddResult['level'], message: string) => {
    setQueueAddResult({ level, message })
    if (level === 'success') setQueueAddUrl((cur) => (cur.trim() === url ? '' : cur))
  }

  // ----- 操作 -----
  // FE-PERF-02: memo 化した子に渡すハンドラは useCallback で参照を安定させる
  // (progress メッセージ等で App が再 render しても子の再 render を防ぐ)。
  const handlePlayPause = useCallback(() => {
    const ws = wsRef.current
    if (!ws) return
    if (posBase.isPlaying) ws.sendPause()
    else ws.sendPlay()
  }, [posBase.isPlaying])
  const handleSkip = useCallback(() => wsRef.current?.sendSkip(), [])
  const handlePrev = useCallback(() => wsRef.current?.sendPrev(), [])
  const handleSeek = useCallback(
    (positionMs: number) => wsRef.current?.sendSeek(positionMs), [])
  const handlePlaylistJump = useCallback(
    (idx: number) => wsRef.current?.playlistJump(idx), [])
  // 3-state ループ: off → all → one → off
  type LoopMode = 'off' | 'all' | 'one'
  const currentLoopMode: LoopMode = (() => {
    if (mode === 'playlist') {
      if (playlist?.loopSingle) return 'one'
      if (playlist?.loop) return 'all'
      return 'off'
    }
    if (queueLoopSingle) return 'one'
    if (queueLoop) return 'all'
    return 'off'
  })()
  const handleToggleLoop = () => {
    const ws = wsRef.current
    if (!ws) return
    const next: LoopMode =
      currentLoopMode === 'off' ? 'all'
      : currentLoopMode === 'all' ? 'one'
      : 'off'
    ws.setLoopMode(next)
  }
  const handleToggleNormalize = () => {
    wsRef.current?.setNormalize(!normalize)
  }
  const handleToggleShuffle = () => {
    const ws = wsRef.current
    if (!ws || !playlist) return
    ws.setShufflePlaylist(!playlist.shuffle)
  }
  const handleRemoveFromLibrary = useCallback((id: string) => {
    wsRef.current?.setLibraryMembership(id, false)
  }, [])
  const handleSelectQueueLibrary = useCallback(() => {
    // プレイリスト同様、即時再生はせず詳細(キュー一覧)を開く。
    // キューは fetch 不要なので同期に切替できる。
    pendingDetailNavRef.current = null
    setPlaylistDetail(null)
    setQueueDetailOpen(true)
  }, [])
  const handleSelectPlaylistLibrary = useCallback((id: string) => {
    // ライブラリクリック → readonly な詳細画面。
    // 同期で state を消すと WS 応答までフォールバック画面がちらつくので、
    // 現在の表示は維持したまま fetch だけ投げ、応答到着時に一気に切替える。
    pendingDetailNavRef.current = 'readonly'
    wsRef.current?.getPlaylistDetail(id)
  }, [])

  // FE-PERF-02: PlaylistDetailView / QueueDetailView 用ハンドラも安定化。
  // playlist 詳細系は対象 playlist id に依存するので id を dep に取る。
  const playlistDetailId = playlistDetail?.id ?? null
  const handleDetailClose = useCallback(() => setPlaylistDetail(null), [])
  const handleDetailAddTrack = useCallback((url: string) => {
    if (playlistDetailId) wsRef.current?.addTrackToPlaylist(playlistDetailId, url)
  }, [playlistDetailId])
  const handleDetailRename = useCallback((newName: string) => {
    if (playlistDetailId) wsRef.current?.renamePlaylist(playlistDetailId, newName)
  }, [playlistDetailId])
  const handleDetailRemoveTrack = useCallback((position: number) => {
    if (playlistDetailId) wsRef.current?.removePlaylistTrack(playlistDetailId, position)
  }, [playlistDetailId])
  const handleDetailReorderTrack = useCallback((from: number, to: number) => {
    if (playlistDetailId) wsRef.current?.reorderPlaylistTrack(playlistDetailId, from, to)
  }, [playlistDetailId])
  const handleDetailSetTags = useCallback((tags: string[]) => {
    if (playlistDetailId) wsRef.current?.setPlaylistTags(playlistDetailId, tags)
  }, [playlistDetailId])
  const handleDetailPlayAll = useCallback(() => {
    if (playlistDetailId) wsRef.current?.librarySelectPlaylist(playlistDetailId, 0)
  }, [playlistDetailId])
  const handleDetailPlayTrack = useCallback((idx: number) => {
    if (playlistDetailId) wsRef.current?.librarySelectPlaylist(playlistDetailId, idx)
  }, [playlistDetailId])
  const handleDetailLoadMore = useCallback((offset: number) => {
    if (playlistDetailId) wsRef.current?.loadPlaylistDetailPage(playlistDetailId, offset)
  }, [playlistDetailId])
  const handleQueueDetailClose = useCallback(() => setQueueDetailOpen(false), [])
  const handlePlayQueue = useCallback(() => wsRef.current?.librarySelectQueue(), [])
  const handleQueueAddTrack = useCallback((url: string) => {
    if (queueAddReqRef.current) return  // 二重送信防止 (ボタンも無効化している)
    setQueueAddResult(null)
    // queueAddOrphanRef はここでは消さない (送れなかった / サーバーが「追加中」と断った
    // 場合でも、前の要求の遅れた返事を拾えるように)
    const id = `qa-${Date.now().toString(36)}-${++queueAddSeqRef.current}`
    if (!wsRef.current?.addTrackToQueue(url, id)) {
      setQueueAddResult({
        level: 'error',
        message: 'サーバーに接続されていません。再接続を待ってからもう一度お試しください',
      })
      return
    }
    // サーバーは解析を 60 秒で諦めて必ず返事をする。それでも来なければ (返事を送る前に
    // サーバーが再起動した等) 待機を解除する。切断時は onClose で解除する。
    const timer = window.setTimeout(() => {
      releaseQueueAdd(true)
      const m = '応答がありませんでした。キューを確認し、必要ならもう一度追加してください'
      setQueueAddResult({ level: 'info', message: m })
      pushToast('info', m)
    }, 75000)
    queueAddReqRef.current = { id, url, timer }
    setQueueAddPending(true)
  }, [])
  const handleQueueAddUrlChange = useCallback((v: string) => {
    setQueueAddUrl(v)
    setQueueAddResult(null)  // 書き換えたら前回の結果表示は消す
  }, [])

  const closeSidebar = () => {
    setSidebarOpen(false)
    setSidebarContent('library')  // 次に開いた時は library に戻す
    setSearchQuery('')
  }
  const openLibrary = () => {
    setSidebarContent('library')
    setSidebarOpen(true)
  }
  const handleShowMore = () => {
    setSidebarContent('queue')
    setSidebarOpen(true)
  }
  const handleBackToLibrary = () => {
    setSidebarContent('library')
    setSearchQuery('')
  }

  // Queue モードに切り替わった直後は search にフォーカス
  useEffect(() => {
    if (sidebarOpen && sidebarContent === 'queue') {
      const id = window.setTimeout(() => searchRef.current?.focus(), 120)
      return () => window.clearTimeout(id)
    }
  }, [sidebarOpen, sidebarContent])

  // FE-MAINT-02: アートワーク色抽出は useArtworkTint フックに分離
  const { bgTint, visualizerTint } = useArtworkTint(
    track?.artwork, bgTintEnabled, visualizerTintEnabled,
  )

  return (
    <div
      className="app"
      style={themeColor ? cssVars({ '--accent': themeColor }) : undefined}
    >
      <StatusBar
        inDiscord={isInDiscord}
        ctx={ctx}
        error={error}
        wsStatus={wsStatus}
      />
      <UserMenuButton
        ctx={ctx}
        open={menuOpen}
        onToggle={() => setMenuOpen((v) => !v)}
        onPick={(k) => {
          setMenuOpen(false)
          setModal(k)
          // confirm / settings は最新の prefs が必要なので playlists を取り直す
          if (k === 'confirm' || k === 'settings') wsRef.current?.listPlaylists()
          if (k === 'guildSettings') wsRef.current?.requestGuildSettings()
        }}
      />
      <div className="layout">
        <div className="library-spacer" aria-hidden />
        <div
          className={`expanded-backdrop${sidebarOpen ? ' open' : ''}`}
          onClick={closeSidebar}
          aria-hidden
        />
        <aside className={`library-overlay${sidebarOpen ? ' expanded' : ''}`}>
          <header className="library-header">
            {sidebarContent === 'library' ? (
              <>
                <button
                  className="library-toggle-btn"
                  onClick={sidebarOpen ? closeSidebar : openLibrary}
                  aria-label={sidebarOpen ? 'collapse library' : 'expand library'}
                  title={sidebarOpen ? '閉じる' : '広げる'}
                >{sidebarOpen ? '‹' : '›'}</button>
                {sidebarOpen && (
                  <>
                    <span className="library-title">
                      {guildName ? `${guildName} LIBRARY` : 'YOUR LIBRARY'}
                    </span>
                    <button
                      className="library-add-btn"
                      onClick={() => {
                        wsRef.current?.listPlaylists()
                        setModal('addLibrary')
                      }}
                      title="プレイリストをライブラリに追加"
                    >＋</button>
                  </>
                )}
              </>
            ) : (
              <>
                <button
                  className="library-toggle-btn"
                  onClick={handleBackToLibrary}
                  aria-label="back to library"
                  title="ライブラリに戻る"
                >←</button>
                <input
                  ref={searchRef}
                  className="library-search queue-search"
                  type="search"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  placeholder={playlist ? `${playlist.name} を検索…` : 'キュー内を検索…'}
                  spellCheck={false}
                  autoCorrect="off"
                  autoCapitalize="off"
                  aria-label="search list"
                />
              </>
            )}
          </header>
          {sidebarContent === 'library' ? (
            <>
              {sidebarOpen && (
                <LibraryControls
                  search={librarySearch}
                  onSearch={setLibrarySearch}
                  sort={librarySort}
                  onSort={setLibrarySort}
                  tagFilter={libraryTagFilter}
                  onTagFilter={setLibraryTagFilter}
                  adderFilter={libraryAdderFilter}
                  onAdderFilter={setLibraryAdderFilter}
                  availableTags={availableTags}
                  availableAdders={availableAdders}
                  filterOpen={libraryFilterOpen}
                  onToggleFilter={() => setLibraryFilterOpen((v) => !v)}
                  totalCount={libraryItems.length}
                  filteredCount={filteredLibraryItems.length}
                />
              )}
              <LibraryItemsList
                mode={mode}
                activePlaylistId={playlist?.id ?? null}
                libraryItems={filteredLibraryItems}
                onSelectQueue={handleSelectQueueLibrary}
                onSelectPlaylist={handleSelectPlaylistLibrary}
                onRemoveFromLibrary={handleRemoveFromLibrary}
              />
            </>
          ) : (
            <QueueItemsList
              mode={mode}
              track={track}
              upnext={upnext}
              playlist={playlist}
              query={searchQuery}
              onJump={handlePlaylistJump}
            />
          )}
        </aside>
        <main
          className={
            'player'
            + (playlistDetail || queueDetailOpen ? ' player-detail-mode' : '')
            + (audioVisualizer !== 'off' && !playlistDetail && !queueDetailOpen
              ? ' player-with-vis'
              : '')
          }
          style={
            bgTint && !playlistDetail && !queueDetailOpen
              ? { background: `linear-gradient(180deg, ${bgTint} 0%, #121212 60%)` }
              : undefined
          }
        >
          {audioVisualizer !== 'off' && !playlistDetail && !queueDetailOpen && (
            <Visualizer
              kind={audioVisualizer}
              isPlaying={posBase.isPlaying}
              themeColor={
                (visualizerTintEnabled && visualizerTint)
                || themeColor
                || '#1db954'
              }
              rmsRef={audioRmsRef}
              bandsRef={audioBandsRef}
              beatAtRef={audioBeatAtRef}
            />
          )}
          {queueDetailOpen ? (
            <QueueDetailView
              currentTrack={mode === 'queue' ? track : null}
              musicQueue={musicQueue}
              isActiveQueue={mode === 'queue'}
              isPlaying={posBase.isPlaying}
              onClose={handleQueueDetailClose}
              onPlayQueue={handlePlayQueue}
              onTogglePlay={handlePlayPause}
              totalCount={musicQueueTotal}
              addUrl={queueAddUrl}
              addPending={queueAddPending}
              addResult={queueAddResult}
              onAddUrlChange={handleQueueAddUrlChange}
              onAddTrack={handleQueueAddTrack}
            />
          ) : playlistDetail ? (
            <PlaylistDetailView
              detail={playlistDetail}
              readOnly={detailMode === 'readonly'}
              isActivePlaylist={mode === 'playlist' && playlist?.id === playlistDetail.id}
              currentPlaylistIndex={
                mode === 'playlist' && playlist?.id === playlistDetail.id
                  ? playlist.index
                  : -1
              }
              isPlaying={posBase.isPlaying}
              onClose={handleDetailClose}
              onAddTrack={handleDetailAddTrack}
              onRename={handleDetailRename}
              onRemoveTrack={handleDetailRemoveTrack}
              onReorderTrack={handleDetailReorderTrack}
              onSetTags={handleDetailSetTags}
              onPlayAll={handleDetailPlayAll}
              onPlayTrack={handleDetailPlayTrack}
              onPause={handlePlayPause}
              onLoadMore={handleDetailLoadMore}
            />
          ) : track ? (
            <>
              <img
                ref={artworkRef}
                className="artwork"
                src={track.artwork || PLACEHOLDER_ART}
                alt=""
                crossOrigin="anonymous"
              />
              <div className="meta">
                <div className="title">
                  <MarqueeText text={track.title} />
                </div>
                <div className="artist">
                  {track.artist && <MarqueeText text={track.artist} />}
                </div>
                {mode === 'playlist' && playlist && (
                  <div className="playlist-banner">
                    <MarqueeText text={`${playlist.name} ・ ${playlist.index + 1}/${Math.max(1, upnext.length)}`} />
                  </div>
                )}
                {(track.source || track.bitrateKbps > 0) && (
                  <div className="source-info">
                    {track.source}
                    {track.source && track.bitrateKbps > 0 ? ' ・ ' : ''}
                    {track.bitrateKbps > 0 ? `${track.bitrateKbps} kbps` : ''}
                  </div>
                )}
              </div>
              <ProgressBar
                posBase={posBase}
                durationMs={track.durationMs}
                loading={loading}
                onSeek={handleSeek}
              />
              <div className="controls">
                <div className="controls-side controls-left">
                  {mode === 'playlist' && (
                    <button
                      className={`secondary-btn${playlist?.shuffle ? ' active' : ''}`}
                      aria-label="toggle shuffle"
                      onClick={handleToggleShuffle}
                      title="シャッフル再生"
                    >
                      <IconShuffle />
                    </button>
                  )}
                  <button
                    className={`secondary-btn${normalize ? ' active' : ''}`}
                    aria-label="toggle normalize"
                    onClick={handleToggleNormalize}
                    title="音量ノーマライズ (loudnorm)"
                  >
                    <IconNormalize />
                  </button>
                </div>
                <div className="controls-center">
                  <button
                    className="step-btn"
                    aria-label="prev"
                    onClick={handlePrev}
                    disabled={mode !== 'playlist'}
                  >⏮</button>
                  <button
                    className="play"
                    aria-label={posBase.isPlaying ? 'pause' : 'play'}
                    onClick={handlePlayPause}
                  >
                    {posBase.isPlaying ? '❚❚' : '▶'}
                  </button>
                  <button
                    className="step-btn"
                    aria-label="next"
                    onClick={handleSkip}
                  >⏭</button>
                </div>
                <div className="controls-side controls-right">
                  <button
                    className={`secondary-btn${currentLoopMode !== 'off' ? ' active' : ''}`}
                    aria-label="toggle loop"
                    onClick={handleToggleLoop}
                    title={
                      currentLoopMode === 'off'
                        ? 'ループなし'
                        : currentLoopMode === 'all'
                        ? (mode === 'playlist' ? 'プレイリスト全体をループ' : 'キューをループ')
                        : '1曲ループ'
                    }
                  >
                    <IconRepeat />
                    {currentLoopMode === 'one' && (
                      <span className="loop-one-badge">1</span>
                    )}
                  </button>
                </div>
              </div>
              <UpNext
                items={upnext}
                mode={mode}
                playlist={playlist}
                queueNextIndex={queueNextIndex}
                currentTrack={track}
                onShowMore={handleShowMore}
              />
            </>
          ) : (
            <EmptyState
              mode={mode}
              wsStatus={wsStatus}
              hasLibraryItems={libraryItems.length > 0}
              onOpenAddLibrary={() => {
                wsRef.current?.listPlaylists()
                setModal('addLibrary')
              }}
              onOpenCreate={() => setModal('create')}
            />
          )}
          {(playlistDetail || queueDetailOpen) && track && (
            <MiniPlayerBar
              track={track}
              posBase={posBase}
              isPlaying={posBase.isPlaying}
              loading={loading}
              mode={mode}
              onPlayPause={handlePlayPause}
              onSkip={handleSkip}
              onPrev={handlePrev}
              onSeek={handleSeek}
            />
          )}
        </main>
      </div>
      {modal === 'create' && (
        <CreatePlaylistModal
          onClose={() => setModal(null)}
          onSubmit={(name, url) => {
            wsRef.current?.createPlaylist(name, url)
            setModal(null)
          }}
        />
      )}
      {modal === 'import' && (
        <ImportPlaylistModal
          onClose={() => setModal(null)}
          onSubmit={(url, name) => {
            wsRef.current?.importPlaylistFromUrl(url, name)
            setModal(null)
          }}
        />
      )}
      {modal === 'confirm' && (
        <ConfirmPlaylistsModal
          playlists={playlists}
          autoSelectLast={autoSelectLast}
          autoAddToLibrary={autoAddToLibrary}
          lastUsedPlaylistId={lastUsedPlaylistId}
          activePlaylistId={playlist?.id ?? null}
          onClose={() => setModal(null)}
          onAddToQueue={(id) => {
            wsRef.current?.addPlaylistToQueue(id)
          }}
          onDelete={(id) => wsRef.current?.deletePlaylist(id)}
          onAddTrack={(id, url) => wsRef.current?.addTrackToPlaylist(id, url)}
          onRename={(id, newName) =>
            wsRef.current?.renamePlaylist(id, newName)
          }
          onSetTags={(id, tags) =>
            wsRef.current?.setPlaylistTags(id, tags)
          }
          onSetAutoSelect={(v) => wsRef.current?.setPref({ autoSelectLast: v })}
          onSetAutoAdd={(v) => wsRef.current?.setPref({ autoAddToLibrary: v })}
          onToggleLibrary={(id, inLib) =>
            wsRef.current?.setLibraryMembership(id, inLib)
          }
          onOpenDetail={(id) => {
            pendingDetailNavRef.current = 'edit'
            wsRef.current?.getPlaylistDetail(id)
            setModal(null)
          }}
        />
      )}
      {modal === 'processes' && (
        <ProcessesModal
          items={processes}
          onClose={() => setModal(null)}
        />
      )}
      {modal === 'settings' && (
        <SettingsModal
          bgTintEnabled={bgTintEnabled}
          audioVisualizer={audioVisualizer}
          themeColor={themeColor}
          visualizerTintEnabled={visualizerTintEnabled}
          onClose={() => setModal(null)}
          onSetBgTint={(v) => wsRef.current?.setPref({ bgTintEnabled: v })}
          onSetVisualizer={(k) => wsRef.current?.setPref({ audioVisualizer: k })}
          onSetThemeColor={(c) => wsRef.current?.setPref({ themeColor: c })}
          onSetVisualizerTint={(v) =>
            wsRef.current?.setPref({ visualizerTintEnabled: v })
          }
        />
      )}
      {modal === 'guildSettings' && (
        <GuildSettingsModal
          guildSettings={guildSettings}
          onClose={() => setModal(null)}
          onSetAnnounceChannel={(id) => wsRef.current?.setGuildPref({ announceChannelId: id })}
        />
      )}
      {modal === 'addLibrary' && (
        <AddToLibraryModal
          playlists={playlists}
          onClose={() => setModal(null)}
          onAdd={(id) => wsRef.current?.setLibraryMembership(id, true)}
        />
      )}
      <ToastStack toasts={toasts} />
    </div>
  )
}

// ===================================================================
// StatusBar
// ===================================================================

function StatusBar({
  inDiscord,
  ctx,
  error,
  wsStatus,
}: {
  inDiscord: boolean
  ctx: ActivityContext | null
  error: string | null
  wsStatus: WsStatus
}) {
  if (!inDiscord) {
    return <header className="status"><span className="warn">Standalone preview (not inside Discord)</span></header>
  }
  if (error) return <header className="status"><span className="err">SDK error: {error}</span></header>
  if (!ctx) return <header className="status"><span>Connecting to Discord…</span></header>
  if (ctx.authError) {
    return (
      <header className="status">
        <span className="err">Auth failed @ {ctx.authStage}: {ctx.authError}</span>
      </header>
    )
  }
  const who = ctx.user ? `@${ctx.user.username}` : 'unauthenticated'
  const wsLabel =
    wsStatus === 'open' ? 'WS:open'
    : wsStatus === 'connecting' ? 'WS:connecting'
    : wsStatus === 'closed' ? 'WS:closed'
    : 'WS:-'
  return (
    <header className="status">
      <span className={wsStatus === 'open' ? 'ok' : 'warn'}>
        SDK ready · {who} · ch:{ctx.channelId?.slice(0, 6) ?? '-'} · {wsLabel}
      </span>
    </header>
  )
}

// ===================================================================
// Topbar / UserMenu
// ===================================================================

function UserMenuButton({
  ctx,
  open,
  onToggle,
  onPick,
}: {
  ctx: ActivityContext | null
  open: boolean
  onToggle: () => void
  onPick: (kind: 'create' | 'import' | 'confirm' | 'settings' | 'guildSettings' | 'processes') => void
}) {
  const avatarUrl = ctx?.user?.avatarUrl ?? null
  const initial = (ctx?.user?.username?.[0] || '?').toUpperCase()
  return (
    <div className="user-menu">
      <button className="user-avatar" onClick={onToggle} aria-label="open menu">
        {avatarUrl ? (
          <img className="user-avatar-img" src={avatarUrl} alt="" />
        ) : (
          <span className="user-avatar-initial">{initial}</span>
        )}
      </button>
      {open && (
        <ul className="user-menu-dropdown" role="menu">
          <li role="menuitem" onClick={() => onPick('create')}>
            マイプレイリストの新規登録
          </li>
          <li role="menuitem" onClick={() => onPick('import')}>
            マイプレイリストを URL からインポート
          </li>
          <li role="menuitem" onClick={() => onPick('confirm')}>
            マイプレイリストを確認
          </li>
          <li role="menuitem" onClick={() => onPick('settings')}>
            個人設定
          </li>
          <li role="menuitem" onClick={() => onPick('guildSettings')}>
            全体設定
          </li>
          <li role="menuitem" onClick={() => onPick('processes')}>
            プロセス一覧
          </li>
          <li className="user-menu-sep" role="separator" aria-hidden="true"></li>
          <li role="menuitem" onClick={() => openExternal(LEGAL_TERMS_URL)}>
            利用規約
          </li>
          <li role="menuitem" onClick={() => openExternal(LEGAL_PRIVACY_URL)}>
            プライバシーポリシー
          </li>
        </ul>
      )}
    </div>
  )
}

// ===================================================================
// Library Sidebar
// ===================================================================

function LibraryControls({
  search,
  onSearch,
  sort,
  onSort,
  tagFilter,
  onTagFilter,
  adderFilter,
  onAdderFilter,
  availableTags,
  availableAdders,
  filterOpen,
  onToggleFilter,
  totalCount,
  filteredCount,
}: {
  search: string
  onSearch: (v: string) => void
  sort: 'added_new' | 'added_old' | 'name_asc' | 'name_desc' | 'count_high' | 'count_low'
  onSort: (v: 'added_new' | 'added_old' | 'name_asc' | 'name_desc' | 'count_high' | 'count_low') => void
  tagFilter: string | null
  onTagFilter: (v: string | null) => void
  adderFilter: string | null
  onAdderFilter: (v: string | null) => void
  availableTags: string[]
  availableAdders: { uid: string; name: string; avatar: string }[]
  filterOpen: boolean
  onToggleFilter: () => void
  totalCount: number
  filteredCount: number
}) {
  const hasFilter = !!tagFilter || !!adderFilter
  return (
    <div className="library-controls">
      <div className="library-controls-row">
        <input
          className="library-search"
          type="search"
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          placeholder="検索 (名前 / タグ / 追加者)"
          spellCheck={false}
          autoCorrect="off"
          autoCapitalize="off"
          aria-label="search library"
        />
        <select
          className="library-sort"
          value={sort}
          onChange={(e) => onSort(e.target.value as typeof sort)}
          aria-label="sort"
          title="並び替え"
        >
          <option value="added_new">追加日 (新)</option>
          <option value="added_old">追加日 (旧)</option>
          <option value="name_asc">名前 A→Z</option>
          <option value="name_desc">名前 Z→A</option>
          <option value="count_high">曲数 (多)</option>
          <option value="count_low">曲数 (少)</option>
        </select>
        <button
          className={`library-filter-btn${hasFilter ? ' active' : ''}`}
          onClick={onToggleFilter}
          title="フィルター"
          aria-label="toggle filter"
        >▾</button>
      </div>
      {filterOpen && (
        <div className="library-controls-filter">
          <label className="library-filter-row">
            <span>タグ</span>
            <select
              value={tagFilter ?? ''}
              onChange={(e) => onTagFilter(e.target.value || null)}
            >
              <option value="">すべて</option>
              {availableTags.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </label>
          <div className="library-filter-row library-filter-adders">
            <span>追加者</span>
            <div className="library-adder-list">
              <button
                className={`library-adder-chip${adderFilter === null ? ' active' : ''}`}
                onClick={() => onAdderFilter(null)}
              >すべて</button>
              {availableAdders.map((a) => (
                <button
                  key={a.uid}
                  className={`library-adder-chip${adderFilter === a.uid ? ' active' : ''}`}
                  onClick={() => onAdderFilter(a.uid)}
                  title={`@${a.name}`}
                >
                  {a.avatar && (
                    <img
                      className="library-adder-chip-avatar"
                      src={a.avatar}
                      alt=""
                    />
                  )}
                  <span className="library-adder-chip-name">@{a.name}</span>
                </button>
              ))}
            </div>
          </div>
          {hasFilter && (
            <button
              className="library-filter-clear"
              onClick={() => {
                onTagFilter(null)
                onAdderFilter(null)
              }}
            >フィルタークリア</button>
          )}
        </div>
      )}
      <div className="library-count">
        {filteredCount}/{totalCount}
      </div>
    </div>
  )
}

function LibraryItemsListImpl({
  mode,
  activePlaylistId,
  libraryItems,
  onSelectQueue,
  onSelectPlaylist,
  onRemoveFromLibrary,
}: {
  mode: Mode
  activePlaylistId: string | null
  libraryItems: WsLibraryItem[]
  onSelectQueue: () => void
  onSelectPlaylist: (id: string) => void
  onRemoveFromLibrary: (id: string) => void
}) {
  return (
    <ul className="library-items">
      <li className="library-item-row">
        <button
          className={`library-item library-item-queue${mode === 'queue' ? ' active' : ''}`}
          onClick={onSelectQueue}
          title="キュー"
        >
          <span className="library-item-thumb library-item-icon">≡</span>
          <span className="library-item-name-wrap">
            <span className="library-item-name">キュー</span>
          </span>
        </button>
      </li>
      {libraryItems.map((p) => {
        const isActive = mode === 'playlist' && activePlaylistId === p.id
        return (
          <li key={p.id} className="library-item-row">
            <button
              className={`library-item${isActive ? ' active' : ''}`}
              onClick={() => onSelectPlaylist(p.id)}
              title={p.name}
            >
              {p.coverUrl ? (
                <img
                  className="library-item-thumb"
                  src={p.coverUrl}
                  alt=""
                  loading="lazy"
                  decoding="async"
                />
              ) : (
                <span className="library-item-thumb library-item-icon">♫</span>
              )}
              <span className="library-item-name-wrap">
                <span className="library-item-name">
                  <MarqueeText text={p.name} />
                </span>
                {p.addedByUsername && (
                  <small className="library-item-added-by">
                    {p.addedByAvatarUrl && (
                      <img
                        className="library-item-added-by-avatar"
                        src={p.addedByAvatarUrl}
                        alt=""
                        loading="lazy"
                        decoding="async"
                      />
                    )}
                    <span className="library-item-added-by-name">
                      @{p.addedByUsername} 追加
                    </span>
                  </small>
                )}
                {(p.tags || []).length > 0 && (
                  <span className="library-item-tags">
                    {p.tags.slice(0, 3).map((t) => (
                      <span key={t} className="tag-chip tag-chip-mini">{t}</span>
                    ))}
                  </span>
                )}
              </span>
              <small className="library-item-count">{p.trackCount}</small>
            </button>
            <button
              className="library-item-remove"
              onClick={(e) => {
                e.stopPropagation()
                onRemoveFromLibrary(p.id)
              }}
              title="ライブラリから外す"
              aria-label="remove from library"
            >×</button>
          </li>
        )
      })}
      {libraryItems.length === 0 && (
        <li className="library-empty">＋でプレイリストを追加</li>
      )}
    </ul>
  )
}

// ===================================================================
// Player widgets
// ===================================================================

// FE-PERF-01: 再生位置の補間を ProgressBar 内部に閉じ込める。
// 旧実装は App が 500ms ごとに `now` を setState しており、再生中は毎回
// 最大 2000 行のリスト含む全サブツリーが再評価されていた。補間を ProgressBar に
// 移すことで、ティックのたびに再 render するのはこのコンポーネントだけになる。
function livePosition(pb: PosBase, durationMs: number, loading: boolean): number {
  if (loading) return 0
  const base = pb.isPlaying ? pb.positionMs + (Date.now() - pb.at) : pb.positionMs
  return Math.min(base, durationMs)
}

function ProgressBar({
  posBase,
  durationMs,
  loading,
  onSeek,
}: {
  posBase: PosBase
  durationMs: number
  loading?: boolean
  onSeek?: (positionMs: number) => void
}) {
  const [hover, setHover] = useState<{ x: number; ms: number } | null>(null)
  // 再生中だけ 500ms ごとに自分を再 render して補間位置を進める。
  const [, forceTick] = useState(0)
  useEffect(() => {
    if (loading || !posBase.isPlaying) return
    const id = window.setInterval(() => forceTick((n) => n + 1), 500)
    return () => window.clearInterval(id)
  }, [posBase, loading])
  const positionMs = livePosition(posBase, durationMs, !!loading)
  const pct = loading
    ? 0
    : (durationMs > 0 ? Math.min(100, (positionMs / durationMs) * 100) : 0)
  const ratioAt = (e: React.MouseEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect()
    const x = e.clientX - rect.left
    const ratio = Math.max(0, Math.min(1, x / rect.width))
    return { x, ratio, width: rect.width }
  }
  const handleClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (loading || !onSeek || durationMs <= 0) return
    const { ratio } = ratioAt(e)
    onSeek(Math.round(ratio * durationMs))
  }
  const handleMove = (e: React.MouseEvent<HTMLDivElement>) => {
    if (loading || durationMs <= 0) return
    const { x, ratio } = ratioAt(e)
    setHover({ x, ms: Math.round(ratio * durationMs) })
  }
  const handleLeave = () => setHover(null)
  return (
    <div className={`progress${loading ? ' loading' : ''}`}>
      <div
        className="bar"
        onClick={handleClick}
        onMouseMove={handleMove}
        onMouseLeave={handleLeave}
      >
        <div className="fill" style={{ width: `${pct}%` }} />
        {loading && <div className="bar-indeterminate" aria-hidden />}
        {hover && !loading && (
          <div
            className="bar-hover-tooltip"
            style={{ left: `${hover.x}px` }}
          >
            {fmt(hover.ms)}
          </div>
        )}
      </div>
      <div className="times">
        {loading ? (
          <span className="times-loading">読み込み中…</span>
        ) : (
          <>
            <span>{fmt(positionMs)}</span>
            <span>{fmt(durationMs)}</span>
          </>
        )}
      </div>
    </div>
  )
}

function UpNext({
  items,
  mode,
  playlist,
  queueNextIndex,
  currentTrack,
  onShowMore,
}: {
  items: WsTrack[]
  mode: Mode
  playlist: WsPlaylistInfo | null
  queueNextIndex: number
  currentTrack: WsTrack | null
  onShowMore: () => void
}) {
  let next: WsTrack | null = null
  if (mode === 'playlist' && playlist) {
    const ni = playlist.nextIndex
    if (ni >= 0 && ni < items.length) next = items[ni]
  } else if (mode === 'queue') {
    if (queueNextIndex >= 0 && queueNextIndex < items.length) {
      next = items[queueNextIndex]
    } else if (queueNextIndex === QUEUE_NEXT_REPEAT_CURRENT) {
      next = currentTrack
    }
  }
  // Show more はリストが空でなければ常に押せるようにする
  const total = items.length
  if (total === 0 && !next) return null
  return (
    <div className="upnext">
      <div className="upnext-row">
        <div className="upnext-label">Up Next</div>
        <button className="upnext-more" onClick={onShowMore}>
          Show more ({total})
        </button>
      </div>
      <div className="upnext-item">
        {next ? (
          <MarqueeText text={`${next.title}${next.artist ? ` — ${next.artist}` : ''}`} />
        ) : (
          <span className="upnext-empty-label">— 次の曲はありません</span>
        )}
      </div>
    </div>
  )
}

function PlaylistDetailViewImpl({
  detail,
  readOnly,
  isActivePlaylist,
  currentPlaylistIndex,
  isPlaying,
  onClose,
  onAddTrack,
  onRename,
  onRemoveTrack,
  onReorderTrack,
  onSetTags,
  onPlayAll,
  onPlayTrack,
  onPause,
  onLoadMore,
}: {
  detail: WsPlaylistDetail
  readOnly: boolean
  isActivePlaylist: boolean
  currentPlaylistIndex: number
  isPlaying: boolean
  onClose: () => void
  onAddTrack: (url: string) => void
  onRename: (newName: string) => void
  onRemoveTrack: (position: number) => void
  onReorderTrack: (from: number, to: number) => void
  onSetTags: (tags: string[]) => void
  onPlayAll: () => void
  onPlayTrack: (index: number) => void
  onPause: () => void
  onLoadMore: (offset: number) => void
}) {
  const [addUrl, setAddUrl] = useState('')
  const [editingName, setEditingName] = useState(false)
  const [nameInput, setNameInput] = useState(detail.name)
  const [pendingDelete, setPendingDelete] = useState<number | null>(null)
  const [tagsInput, setTagsInput] = useState<string | null>(null)
  const totalLabel = useMemo(
    () => formatTotalDuration(detail.tracks), [detail.tracks],
  )

  useEffect(() => {
    if (!editingName) setNameInput(detail.name)
  }, [detail.name, editingName])

  const commitRename = () => {
    const v = nameInput.trim()
    setEditingName(false)
    if (v && v !== detail.name) onRename(v)
    else setNameInput(detail.name)
  }

  const heroIsPlaying = readOnly && isActivePlaylist && isPlaying

  // FE-PERF-03: トラック一覧をウィンドウ仮想化
  const { scrollRef, listRef, range } = useWindowedRows(
    detail.tracks.length, DETAIL_ROW_H,
  )

  // BC-PERF-06: 可視範囲が読み込み済みの末尾に近づき、まだ未取得の曲が残っていれば
  // 次ページを要求する。同じ offset を二重要求しないよう ref で抑止。
  const reqOffsetRef = useRef(-1)
  const prevLoadedRef = useRef(0)
  useEffect(() => {
    reqOffsetRef.current = -1
    prevLoadedRef.current = 0
  }, [detail.id])
  const loaded = detail.tracks.length
  useEffect(() => {
    // 編集後の offset=0 再 publish で loaded が縮んだら要求 ref をリセットする。
    // これをしないと「直前に要求した offset == 縮んだ loaded」で抑止が効きっぱなしになり、
    // 残りページが取得できなくなる (ページング恒久停止) のを防ぐ。
    if (loaded < prevLoadedRef.current) reqOffsetRef.current = -1
    prevLoadedRef.current = loaded
    if (loaded < detail.total
        && range.end >= loaded - 50
        && reqOffsetRef.current !== loaded) {
      reqOffsetRef.current = loaded
      onLoadMore(loaded)
    }
  }, [range.end, loaded, detail.total, onLoadMore])

  return (
    <div className="detail-view" ref={scrollRef}>
      <header className="detail-header">
        <button
          className="detail-back"
          onClick={onClose}
          aria-label="back"
          title="戻る"
        >←</button>
        <span className="detail-header-title">{detail.name}</span>
      </header>
      <section className="detail-hero">
        <img
          className="detail-cover"
          src={detail.coverUrl || PLACEHOLDER_ART}
          alt=""
        />
        <div className="detail-info">
          <div className="detail-label">PLAYLIST</div>
          {!readOnly && editingName ? (
            <input
              className="detail-name-input"
              value={nameInput}
              autoFocus
              onChange={(e) => setNameInput(e.target.value)}
              onBlur={commitRename}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commitRename()
                else if (e.key === 'Escape') {
                  setEditingName(false)
                  setNameInput(detail.name)
                }
              }}
            />
          ) : (
            <h2
              className={`detail-name${readOnly ? ' readonly' : ''}`}
              title={readOnly ? undefined : 'クリックで編集'}
              onClick={readOnly ? undefined : () => setEditingName(true)}
            >{detail.name}</h2>
          )}
          <div className="detail-stats">
            {detail.tracks.length}{'\u00a0'}曲 ・ {totalLabel}
          </div>
          {(detail.tags || []).length > 0 && (
            <div className="detail-tags">
              {detail.tags.map((t) => (
                <span key={t} className="tag-chip">{t}</span>
              ))}
            </div>
          )}
        </div>
      </section>
      {readOnly && detail.tracks.length > 0 && (
        <section className="detail-play-row">
          <button
            className="detail-play-all"
            onClick={heroIsPlaying ? onPause : onPlayAll}
            aria-label={heroIsPlaying ? 'pause' : 'play'}
            title={heroIsPlaying ? '一時停止' : '再生'}
          >{heroIsPlaying ? '❚❚' : '▶'}</button>
        </section>
      )}
      {!readOnly && (
        <>
          <section className="detail-add">
            <input
              className="modal-input"
              value={addUrl}
              onChange={(e) => setAddUrl(e.target.value)}
              placeholder="曲URLを追加 (https://...)"
            />
            <button
              className="modal-btn modal-btn-primary"
              disabled={!addUrl.trim()}
              onClick={() => {
                onAddTrack(addUrl.trim())
                setAddUrl('')
              }}
            >追加</button>
          </section>
          <section className="detail-add">
            <input
              className="modal-input"
              value={tagsInput !== null ? tagsInput : (detail.tags || []).join(', ')}
              onChange={(e) => setTagsInput(e.target.value)}
              placeholder="タグ (カンマ区切り 例: jpop, anime)"
            />
            <button
              className="modal-btn modal-btn-primary"
              onClick={() => {
                const raw = tagsInput !== null
                  ? tagsInput
                  : (detail.tags || []).join(', ')
                const tags = parseTags(raw)
                onSetTags(tags)
                setTagsInput(null)
              }}
            >保存</button>
          </section>
        </>
      )}
      <ol className="detail-tracks" ref={listRef}>
        {range.start > 0 && (
          <li className="vlist-spacer" style={{ height: range.start * DETAIL_ROW_H }} aria-hidden />
        )}
        {detail.tracks.slice(range.start, range.end).map((t, vi) => {
          const i = range.start + vi
          const isPendingDelete = pendingDelete === i
          const isCurrent = isActivePlaylist && i === currentPlaylistIndex
          return (
            <li
              key={`${t.url}-${i}`}
              className={`detail-track${isCurrent ? ' current' : ''}${readOnly ? ' detail-track-clickable' : ''}`}
              onClick={
                readOnly
                  ? () => {
                      if (isCurrent) onPause()
                      else onPlayTrack(i)
                    }
                  : undefined
              }
              role={readOnly ? 'button' : undefined}
              tabIndex={readOnly ? 0 : undefined}
              onKeyDown={
                readOnly
                  ? (e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault()
                        if (isCurrent) onPause()
                        else onPlayTrack(i)
                      }
                    }
                  : undefined
              }
            >
              <span className="detail-track-no">
                {isCurrent ? (isPlaying ? '♪' : '▶') : i + 1}
              </span>
              <DeferredImg
                className="detail-track-thumb"
                src={t.artwork || PLACEHOLDER_ART}
              />
              <div className="detail-track-meta">
                <div className="detail-track-title">
                  <MarqueeText text={t.title} />
                </div>
              </div>
              <span className="detail-track-duration">
                {fmt(t.durationMs)}
              </span>
              {readOnly ? (
                <div className="detail-track-actions">
                  <button
                    className="detail-track-btn detail-track-play"
                    onClick={(e) => {
                      e.stopPropagation()
                      if (isCurrent) onPause()
                      else onPlayTrack(i)
                    }}
                    title={isCurrent && isPlaying ? '一時停止' : '再生'}
                    aria-label="play track"
                  >{isCurrent && isPlaying ? '❚❚' : '▶'}</button>
                </div>
              ) : (
                <div className="detail-track-actions">
                  <button
                    className="detail-track-btn"
                    onClick={() => onReorderTrack(i, i - 1)}
                    disabled={i === 0}
                    title="上に移動"
                    aria-label="move up"
                  >▲</button>
                  <button
                    className="detail-track-btn"
                    onClick={() => onReorderTrack(i, i + 1)}
                    disabled={i === detail.total - 1}
                    title="下に移動"
                    aria-label="move down"
                  >▼</button>
                  <button
                    className={`detail-track-btn detail-track-del${isPendingDelete ? ' pending' : ''}`}
                    onClick={() => {
                      if (isPendingDelete) {
                        onRemoveTrack(i)
                        setPendingDelete(null)
                      } else {
                        setPendingDelete(i)
                        window.setTimeout(() => {
                          setPendingDelete((cur) => (cur === i ? null : cur))
                        }, 3000)
                      }
                    }}
                    title={isPendingDelete ? 'もう一度押すと削除' : '削除'}
                    aria-label="delete track"
                  >{isPendingDelete ? '!' : '×'}</button>
                </div>
              )}
            </li>
          )
        })}
        {range.end < detail.tracks.length && (
          <li
            className="vlist-spacer"
            style={{ height: (detail.tracks.length - range.end) * DETAIL_ROW_H }}
            aria-hidden
          />
        )}
        {detail.tracks.length === 0 && (
          <li className="detail-empty">曲がまだありません</li>
        )}
      </ol>
    </div>
  )
}


function MiniPlayerBar({
  track,
  posBase,
  isPlaying,
  loading,
  mode,
  onPlayPause,
  onSkip,
  onPrev,
  onSeek,
}: {
  track: WsTrack
  posBase: PosBase
  isPlaying: boolean
  loading: boolean
  mode: Mode
  onPlayPause: () => void
  onSkip: () => void
  onPrev: () => void
  onSeek: (positionMs: number) => void
}) {
  return (
    <div className="mini-player">
      <img
        className="mini-player-thumb"
        src={track.artwork || PLACEHOLDER_ART}
        alt=""
      />
      <div className="mini-player-meta">
        <div className="mini-player-title">{track.title}</div>
        <div className="mini-player-artist">{track.artist}</div>
      </div>
      <div className="mini-player-progress">
        <ProgressBar
          posBase={posBase}
          durationMs={track.durationMs}
          loading={loading}
          onSeek={onSeek}
        />
      </div>
      <div className="mini-player-controls">
        <button
          className="mini-step-btn"
          aria-label="prev"
          onClick={onPrev}
          disabled={mode !== 'playlist'}
        >⏮</button>
        <button
          className="mini-play-btn"
          aria-label={isPlaying ? 'pause' : 'play'}
          onClick={onPlayPause}
        >{isPlaying ? '❚❚' : '▶'}</button>
        <button
          className="mini-step-btn"
          aria-label="next"
          onClick={onSkip}
        >⏭</button>
      </div>
    </div>
  )
}


function EmptyState({
  mode,
  wsStatus,
  hasLibraryItems,
  onOpenAddLibrary,
  onOpenCreate,
}: {
  mode: Mode
  wsStatus: WsStatus
  hasLibraryItems: boolean
  onOpenAddLibrary: () => void
  onOpenCreate: () => void
}) {
  if (wsStatus !== 'open') {
    return (
      <div className="empty">
        <div className="empty-art-placeholder" />
        <div className="empty-text-muted">
          {wsStatus === 'closed' ? '切断されました' : '接続中…'}
        </div>
      </div>
    )
  }
  if (mode === 'playlist') {
    return (
      <div className="empty empty-playlist">
        <div className="empty-text-strong">
          {hasLibraryItems
            ? 'ライブラリからプレイリストを選択してください'
            : 'ライブラリにプレイリストがありません'}
        </div>
        <div className="empty-actions">
          <button
            className="empty-cta"
            onClick={hasLibraryItems ? onOpenAddLibrary : onOpenCreate}
          >
            {hasLibraryItems ? 'ライブラリに追加' : 'プレイリストを作成'}
          </button>
        </div>
      </div>
    )
  }
  return (
    <div className="empty">
      <div className="empty-art-placeholder" />
      <div className="empty-text-muted">再生していません</div>
    </div>
  )
}

// ===================================================================
// Queue Drawer (search + list)
// ===================================================================

function QueueItemsListImpl({
  mode,
  track,
  upnext,
  playlist,
  query,
  onJump,
}: {
  mode: Mode
  track: WsTrack | null
  upnext: WsTrack[]
  playlist: WsPlaylistInfo | null
  query: string
  onJump: (index: number) => void
}) {
  const q = query.trim().toLowerCase()
  const matches = (t: WsTrack) =>
    !q || t.title.toLowerCase().includes(q) || t.artist.toLowerCase().includes(q)

  const currentIdx = mode === 'playlist' && playlist ? playlist.index : 0
  const items = upnext.map((t, originalIndex) => ({ t, originalIndex }))
  const filtered = items.filter(({ t }) => matches(t))

  return (
    <div className="queue-list">
      {mode === 'queue' && track && matches(track) && (
        <div className="queue-item queue-item-current">
          <span className="queue-item-index">▶</span>
          <img
            className="queue-item-thumb"
            src={track.artwork || PLACEHOLDER_ART}
            alt=""
            loading="lazy"
            decoding="async"
          />
          <div className="queue-item-meta">
            <div className="queue-item-title">
              <MarqueeText text={track.title} />
            </div>
            <div className="queue-item-artist">
              Now Playing{track.artist ? ` · ${track.artist}` : ''}
            </div>
          </div>
        </div>
      )}
      {filtered.map(({ t, originalIndex }) => {
        const isCurrent = mode === 'playlist' && originalIndex === currentIdx
        const itemIndex =
          mode === 'queue' ? originalIndex + 2 : originalIndex + 1
        const clickable = mode === 'playlist' && !isCurrent
        return (
          <div
            key={`${t.url}-${originalIndex}`}
            className={`queue-item${isCurrent ? ' queue-item-current' : ''}${clickable ? ' queue-item-clickable' : ''}`}
            onClick={clickable ? () => onJump(originalIndex) : undefined}
            role={clickable ? 'button' : undefined}
            tabIndex={clickable ? 0 : undefined}
            onKeyDown={
              clickable
                ? (e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      onJump(originalIndex)
                    }
                  }
                : undefined
            }
          >
            <span className="queue-item-index">
              {isCurrent ? '▶' : itemIndex}
            </span>
            <img
              className="queue-item-thumb"
              src={t.artwork || PLACEHOLDER_ART}
              alt=""
              loading="lazy"
              decoding="async"
            />
            <div className="queue-item-meta">
              <div className="queue-item-title">
                <MarqueeText text={t.title} />
              </div>
              <div className="queue-item-artist">
                {isCurrent
                  ? `Now Playing${t.artist ? ` · ${t.artist}` : ''}`
                  : t.artist}
              </div>
            </div>
          </div>
        )
      })}
      {filtered.length === 0
        && !(mode === 'queue' && track && matches(track)) && (
        <div className="queue-empty">
          {q ? `「${query.trim()}」に一致する曲はありません` : 'リストは空です'}
        </div>
      )}
    </div>
  )
}

// ===================================================================
// Modal: 新規登録 / インポート / 確認 / ライブラリ追加
// ===================================================================

function CreatePlaylistModal({
  onClose,
  onSubmit,
}: {
  onClose: () => void
  onSubmit: (name: string, url: string) => void
}) {
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  return (
    <ModalShell onClose={onClose} title="マイプレイリストの新規登録">
      <label className="modal-label">プレイリスト名</label>
      <input
        className="modal-input"
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="例: お気に入り"
        autoFocus
      />
      <label className="modal-label">初期登録する楽曲 URL</label>
      <input
        className="modal-input"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        placeholder="https://www.youtube.com/watch?v=..."
      />
      <div className="modal-hint">
        単曲 URL のほか、YouTube 再生リスト / Bandcamp アルバム URL を渡すと
        全曲を一括で登録します
      </div>
      <div className="modal-actions">
        <button className="modal-btn" onClick={onClose}>キャンセル</button>
        <button
          className="modal-btn modal-btn-primary"
          onClick={() => onSubmit(name.trim(), url.trim())}
          disabled={!name.trim() || !url.trim()}
        >登録</button>
      </div>
    </ModalShell>
  )
}

function ImportPlaylistModal({
  onClose,
  onSubmit,
}: {
  onClose: () => void
  onSubmit: (url: string, name: string) => void
}) {
  const [url, setUrl] = useState('')
  const [name, setName] = useState('')
  return (
    <ModalShell onClose={onClose} title="URL からインポート">
      <label className="modal-label">プレイリスト / アルバムの URL</label>
      <input
        className="modal-input"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        placeholder="https://www.youtube.com/playlist?list=..."
        autoFocus
      />
      <div className="modal-hint">
        対応:
        <br />・ YouTube 再生リスト (<code>youtube.com/playlist?list=...</code>)
        <br />・ Bandcamp アルバム (<code>artist.bandcamp.com/album/...</code>)
        <br />・ niconico マイリスト (<code>nicovideo.jp/mylist/...</code>)
        <br />・ Jellyfin アルバム / プレイリスト (詳細ページの URL)
      </div>
      <label className="modal-label">プレイリスト名 (任意)</label>
      <input
        className="modal-input"
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="空欄ならインポート元のタイトルを使用"
      />
      <div className="modal-actions">
        <button className="modal-btn" onClick={onClose}>キャンセル</button>
        <button
          className="modal-btn modal-btn-primary"
          onClick={() => onSubmit(url.trim(), name.trim())}
          disabled={!url.trim()}
        >取り込み</button>
      </div>
    </ModalShell>
  )
}

function ConfirmPlaylistsModal({
  playlists,
  autoSelectLast,
  autoAddToLibrary,
  lastUsedPlaylistId,
  activePlaylistId,
  onClose,
  onAddToQueue,
  onDelete,
  onAddTrack,
  onRename,
  onSetTags,
  onSetAutoSelect,
  onSetAutoAdd,
  onToggleLibrary,
  onOpenDetail,
}: {
  playlists: WsPlaylistListItem[]
  autoSelectLast: boolean
  autoAddToLibrary: boolean
  lastUsedPlaylistId: string | null
  activePlaylistId: string | null
  onClose: () => void
  onAddToQueue: (id: string) => void
  onDelete: (id: string) => void
  onAddTrack: (id: string, url: string) => void
  onRename: (id: string, newName: string) => void
  onSetTags: (id: string, tags: string[]) => void
  onSetAutoSelect: (v: boolean) => void
  onSetAutoAdd: (v: boolean) => void
  onToggleLibrary: (id: string, inLibrary: boolean) => void
  onOpenDetail: (id: string) => void
}) {
  const [expanded, setExpanded] = useState<string | null>(null)
  const [addUrl, setAddUrl] = useState<{ [k: string]: string }>({})
  const [renameInput, setRenameInput] = useState<{ [k: string]: string }>({})
  const [tagInput, setTagInput] = useState<{ [k: string]: string }>({})
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)

  return (
    <ModalShell onClose={onClose} title="マイプレイリストを確認">
      <div className="confirm-prefs">
        <label className="confirm-pref">
          <input
            type="checkbox"
            checked={autoSelectLast}
            onChange={(e) => onSetAutoSelect(e.target.checked)}
          />
          <span>最後に使ったプレイリストを自動選択</span>
        </label>
        <label className="confirm-pref">
          <input
            type="checkbox"
            checked={autoAddToLibrary}
            onChange={(e) => onSetAutoAdd(e.target.checked)}
          />
          <span>作成時に自動でライブラリへ追加</span>
        </label>
      </div>
      {playlists.length === 0 && (
        <div className="confirm-empty">プレイリストがありません</div>
      )}
      <ul className="confirm-list">
        {playlists.map((p) => {
          const isActive = p.id === activePlaylistId
          const isLast = p.id === lastUsedPlaylistId
          const isExpanded = expanded === p.id
          const isPendingDelete = pendingDelete === p.id
          return (
            <li key={p.id} className={`confirm-item${isActive ? ' active' : ''}`}>
              <div className="confirm-item-head">
                <button
                  className="confirm-item-name"
                  onClick={() => {
                    setExpanded(isExpanded ? null : p.id)
                    if (!isExpanded) {
                      setRenameInput((s) => ({ ...s, [p.id]: p.name }))
                    }
                  }}
                  aria-label="toggle"
                >
                  <span>{p.name}</span>
                  <small>
                    {p.trackCount} 曲
                    {isLast ? ' · 直近' : ''}
                    {p.inLibrary ? ' · ライブラリ' : ''}
                  </small>
                </button>
                <button
                  className="confirm-item-detail"
                  onClick={() => onOpenDetail(p.id)}
                  title="詳細を確認"
                  aria-label="open detail"
                >ⓘ</button>
                <button
                  className={`confirm-item-lib${p.inLibrary ? ' active' : ''}`}
                  onClick={() => onToggleLibrary(p.id, !p.inLibrary)}
                  title={p.inLibrary ? 'ライブラリから外す' : 'ライブラリに追加'}
                >
                  {p.inLibrary ? '✓' : '＋'}
                </button>
                <button
                  className="confirm-item-play"
                  onClick={() => onAddToQueue(p.id)}
                  title="このプレイリストをキューに追加"
                  aria-label="add to queue"
                >＋Q</button>
                <button
                  className={`confirm-item-delete${isPendingDelete ? ' pending' : ''}`}
                  onClick={() => {
                    if (isPendingDelete) {
                      onDelete(p.id)
                      setPendingDelete(null)
                    } else {
                      setPendingDelete(p.id)
                      window.setTimeout(() => {
                        setPendingDelete((cur) => (cur === p.id ? null : cur))
                      }, 3000)
                    }
                  }}
                  title={isPendingDelete ? 'もう一度押すと削除' : '削除'}
                >{isPendingDelete ? '!' : '×'}</button>
              </div>
              {isExpanded && (
                <div className="confirm-item-body">
                  <div className="confirm-item-row">
                    <input
                      className="modal-input"
                      placeholder="新しいプレイリスト名"
                      value={renameInput[p.id] ?? p.name}
                      onChange={(e) =>
                        setRenameInput((s) => ({ ...s, [p.id]: e.target.value }))
                      }
                    />
                    <button
                      className="modal-btn modal-btn-primary"
                      disabled={
                        !(renameInput[p.id] ?? '').trim()
                        || (renameInput[p.id] ?? '').trim() === p.name
                      }
                      onClick={() => {
                        onRename(p.id, (renameInput[p.id] || '').trim())
                      }}
                    >改名</button>
                  </div>
                  <div className="confirm-item-row">
                    <input
                      className="modal-input"
                      placeholder="曲URLを追加 (https://...)"
                      value={addUrl[p.id] ?? ''}
                      onChange={(e) =>
                        setAddUrl((s) => ({ ...s, [p.id]: e.target.value }))
                      }
                    />
                    <button
                      className="modal-btn modal-btn-primary"
                      disabled={!(addUrl[p.id] || '').trim()}
                      onClick={() => {
                        onAddTrack(p.id, (addUrl[p.id] || '').trim())
                        setAddUrl((s) => ({ ...s, [p.id]: '' }))
                      }}
                    >追加</button>
                  </div>
                  <div className="confirm-item-row">
                    <input
                      className="modal-input"
                      placeholder="タグ (カンマ区切り 例: jpop, anime)"
                      value={
                        tagInput[p.id] !== undefined
                          ? tagInput[p.id]
                          : (p.tags || []).join(', ')
                      }
                      onChange={(e) =>
                        setTagInput((s) => ({ ...s, [p.id]: e.target.value }))
                      }
                    />
                    <button
                      className="modal-btn modal-btn-primary"
                      onClick={() => {
                        const raw = tagInput[p.id] !== undefined
                          ? tagInput[p.id]
                          : (p.tags || []).join(', ')
                        const tags = parseTags(raw)
                        onSetTags(p.id, tags)
                        setTagInput((s) => {
                          const { [p.id]: _drop, ...rest } = s
                          return rest
                        })
                      }}
                    >保存</button>
                  </div>
                  {(p.tags || []).length > 0 && (
                    <div className="confirm-item-tags">
                      {p.tags.map((t) => (
                        <span key={t} className="tag-chip">{t}</span>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </li>
          )
        })}
      </ul>
      <div className="modal-actions">
        <button className="modal-btn" onClick={onClose}>閉じる</button>
      </div>
    </ModalShell>
  )
}

function AddToLibraryModal({
  playlists,
  onClose,
  onAdd,
}: {
  playlists: WsPlaylistListItem[]
  onClose: () => void
  onAdd: (id: string) => void
}) {
  const candidates = playlists.filter((p) => !p.inLibrary)
  return (
    <ModalShell onClose={onClose} title="ライブラリに追加">
      {candidates.length === 0 ? (
        <div className="confirm-empty">
          追加できるプレイリストがありません。
          <br />
          メニューから「新規登録」または「YouTube インポート」で
          プレイリストを作成してください。
        </div>
      ) : (
        <ul className="confirm-list">
          {candidates.map((p) => (
            <li key={p.id} className="confirm-item">
              <div className="confirm-item-head">
                <div className="confirm-item-name">
                  <span>{p.name}</span>
                  <small>{p.trackCount} 曲</small>
                </div>
                <button
                  className="modal-btn modal-btn-primary"
                  onClick={() => onAdd(p.id)}
                >追加</button>
              </div>
            </li>
          ))}
        </ul>
      )}
      <div className="modal-actions">
        <button className="modal-btn" onClick={onClose}>閉じる</button>
      </div>
    </ModalShell>
  )
}

function ProcessesModal({
  items,
  onClose,
}: {
  items: WsProcess[]
  onClose: () => void
}) {
  const sorted = useMemo(
    () => items.slice().sort((a, b) => b.startedAt.localeCompare(a.startedAt)),
    [items],
  )
  const fmtTime = (iso: string) => {
    if (!iso) return '—'
    try {
      const d = new Date(iso)
      return d.toLocaleString('ja-JP', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      })
    } catch {
      return iso
    }
  }
  const fmtDuration = (a: string, b: string) => {
    if (!a || !b) return '—'
    try {
      const t = new Date(b).getTime() - new Date(a).getTime()
      if (t < 0) return '—'
      const sec = Math.floor(t / 1000)
      if (sec < 60) return `${sec} 秒`
      return `${Math.floor(sec / 60)} 分 ${sec % 60} 秒`
    } catch {
      return '—'
    }
  }
  return (
    <ModalShell onClose={onClose} title="プロセス一覧">
      {sorted.length === 0 ? (
        <div className="confirm-empty">
          このセッションで実行したプロセスはまだありません。
        </div>
      ) : (
        <ul className="proc-list">
          {sorted.map((p) => {
            const pct = p.progressTotal > 0
              ? Math.min(100, (p.progressCurrent / p.progressTotal) * 100)
              : 0
            return (
              <li key={p.id} className={`proc-item proc-${p.status}`}>
                <div className="proc-head">
                  <span className={`proc-status proc-status-${p.status}`}>
                    {p.status === 'running' ? '実行中'
                      : p.status === 'success' ? '完了'
                      : 'エラー'}
                  </span>
                  <span className="proc-name" title={p.name}>{p.name}</span>
                </div>
                {p.message && (
                  <div className="proc-message">{p.message}</div>
                )}
                {p.status === 'running' && p.progressTotal > 0 && (
                  <div className="proc-progress">
                    <div className="proc-progress-bar">
                      <div
                        className="proc-progress-fill"
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                    <span className="proc-progress-label">
                      {p.progressCurrent}/{p.progressTotal}
                    </span>
                  </div>
                )}
                <div className="proc-url" title={p.sourceUrl}>
                  {p.sourceUrl}
                </div>
                <div className="proc-meta">
                  <span>開始: {fmtTime(p.startedAt)}</span>
                  {p.finishedAt && (
                    <>
                      <span>完了: {fmtTime(p.finishedAt)}</span>
                      <span>所要: {fmtDuration(p.startedAt, p.finishedAt)}</span>
                    </>
                  )}
                </div>
              </li>
            )
          })}
        </ul>
      )}
      <div className="modal-actions">
        <button className="modal-btn" onClick={onClose}>閉じる</button>
      </div>
    </ModalShell>
  )
}

function SettingsModal({
  bgTintEnabled,
  audioVisualizer,
  themeColor,
  visualizerTintEnabled,
  onClose,
  onSetBgTint,
  onSetVisualizer,
  onSetThemeColor,
  onSetVisualizerTint,
}: {
  bgTintEnabled: boolean
  audioVisualizer: VisualizerKind
  themeColor: string | null
  visualizerTintEnabled: boolean
  onClose: () => void
  onSetBgTint: (v: boolean) => void
  onSetVisualizer: (k: VisualizerKind) => void
  onSetThemeColor: (c: string | null) => void
  onSetVisualizerTint: (v: boolean) => void
}) {
  const DEFAULT_ACCENT = '#1db954'
  const swatches = [
    DEFAULT_ACCENT, '#ff5e57', '#ffa502', '#ffd166', '#06d6a0',
    '#118ab2', '#9b5de5', '#f15bb5', '#3a86ff', '#fb5607',
  ]
  const current = themeColor ?? DEFAULT_ACCENT
  const visualizers: { kind: VisualizerKind; label: string; desc: string }[] = [
    { kind: 'off',       label: 'オフ',         desc: '何も表示しない' },
    { kind: 'bars',      label: 'バー',         desc: '周波数帯ごとの縦バー' },
    { kind: 'mirror',    label: 'ミラー',       desc: '中央線から上下にミラー表示' },
    { kind: 'wmp',       label: 'WMP風',        desc: '緑→黄→赤グラデにピークホールド (WMP/XP風)' },
    { kind: 'dots',      label: 'ドット',       desc: '8bit 風 LED ドットマトリクス' },
    { kind: 'digital',   label: 'デジタル',     desc: '車載 HU 風の長方形 LED セグメント' },
    { kind: 'wave',      label: '波',           desc: '帯域に追従する波形' },
    { kind: 'radial',    label: 'ラジアル',     desc: '円形に放射するバンドバー' },
    { kind: 'pulse',     label: 'パルス',       desc: 'ビートに合わせて広がるリング' },
    { kind: 'particles', label: 'パーティクル', desc: 'ビートで弾ける粒子' },
  ]
  return (
    <ModalShell onClose={onClose} title="個人設定">
      <section className="settings-section">
        <div className="settings-section-title">表示</div>
        <label className="confirm-pref">
          <input
            type="checkbox"
            checked={bgTintEnabled}
            onChange={(e) => onSetBgTint(e.target.checked)}
          />
          <span>再生中の曲のサムネに合わせて背景色を変える</span>
        </label>
      </section>
      <section className="settings-section">
        <div className="settings-section-title">オーディオビジュアライザー</div>
        <label className="confirm-pref">
          <input
            type="checkbox"
            checked={visualizerTintEnabled}
            onChange={(e) => onSetVisualizerTint(e.target.checked)}
          />
          <span>ビジュアライザーの色を再生中の曲のサムネに合わせる</span>
        </label>
        <div className="settings-vis-list">
          {visualizers.map((v) => (
            <label
              key={v.kind}
              className={`settings-vis-item${audioVisualizer === v.kind ? ' active' : ''}`}
            >
              <input
                type="radio"
                name="audio-visualizer"
                checked={audioVisualizer === v.kind}
                onChange={() => onSetVisualizer(v.kind)}
              />
              <div className="settings-vis-meta">
                <div className="settings-vis-label">{v.label}</div>
                <div className="settings-vis-desc">{v.desc}</div>
              </div>
            </label>
          ))}
        </div>
      </section>
      <section className="settings-section">
        <div className="settings-section-title">テーマカラー</div>
        <div className="settings-color-row">
          <input
            className="settings-color-picker"
            type="color"
            value={current}
            onChange={(e) => onSetThemeColor(e.target.value)}
            aria-label="theme color"
          />
          <input
            className="modal-input settings-color-hex"
            type="text"
            value={current}
            onChange={(e) => {
              const v = e.target.value.trim()
              if (/^#[0-9a-fA-F]{6}$/.test(v)) onSetThemeColor(v.toLowerCase())
            }}
            aria-label="theme color hex"
          />
          <button
            className="modal-btn"
            onClick={() => onSetThemeColor(null)}
            title="既定値に戻す"
          >既定値</button>
        </div>
        <div className="settings-swatches">
          {swatches.map((s) => (
            <button
              key={s}
              className={`settings-swatch${current.toLowerCase() === s.toLowerCase() ? ' active' : ''}`}
              style={{ background: s }}
              onClick={() => onSetThemeColor(s)}
              aria-label={`color ${s}`}
            />
          ))}
        </div>
      </section>
      <div className="modal-actions">
        <button className="modal-btn" onClick={onClose}>閉じる</button>
      </div>
    </ModalShell>
  )
}

function GuildSettingsModal({
  guildSettings,
  onClose,
  onSetAnnounceChannel,
}: {
  guildSettings: {
    announceChannelId: string | null
    announceChannelName: string
    systemChannelName: string
    canManage: boolean
    channels: { id: string; name: string }[]
  } | null
  onClose: () => void
  onSetAnnounceChannel: (id: string | null) => void
}) {
  return (
    <ModalShell onClose={onClose} title="全体設定">
      <section className="settings-section">
        <div className="settings-section-title">サーバー設定</div>
        {guildSettings === null ? (
          <div className="modal-hint">読み込み中…</div>
        ) : guildSettings.canManage ? (
          <>
            <div className="modal-hint" style={{ marginBottom: 6 }}>
              BOT からのお知らせが届くチャンネル (サーバー全体の設定)
            </div>
            <select
              className="settings-select"
              value={guildSettings.announceChannelId ?? ''}
              onChange={(e) => onSetAnnounceChannel(e.target.value === '' ? null : e.target.value)}
            >
              <option value="">未設定 (システムチャンネルに届きます)</option>
              {guildSettings.announceChannelId !== null &&
                !guildSettings.channels.some((c) => c.id === guildSettings.announceChannelId) && (
                  <option value={guildSettings.announceChannelId} disabled>
                    #{guildSettings.announceChannelName || '不明'} (現在の設定)
                  </option>
                )}
              {guildSettings.channels.map((c) => (
                <option key={c.id} value={c.id}>#{c.name}</option>
              ))}
            </select>
          </>
        ) : (
          <>
            <div style={{ fontSize: 13, marginBottom: 4 }}>
              {guildSettings.announceChannelId
                ? `#${guildSettings.announceChannelName}`
                : `未設定 (システムチャンネル${guildSettings.systemChannelName ? ' #' + guildSettings.systemChannelName : ''} に届きます)`}
            </div>
            <div className="modal-hint">変更にはサーバー管理権限が必要です</div>
          </>
        )}
      </section>
      <div className="modal-actions">
        <button className="modal-btn" onClick={onClose}>閉じる</button>
      </div>
    </ModalShell>
  )
}

// ===================================================================
// Reusables
// ===================================================================

function ModalShell({
  title,
  onClose,
  children,
}: {
  title: string
  onClose: () => void
  children: React.ReactNode
}) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-header">
          <span className="modal-title">{title}</span>
          <button className="modal-close" onClick={onClose} aria-label="close">×</button>
        </header>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  )
}

function ToastStack({ toasts }: { toasts: Toast[] }) {
  if (!toasts.length) return null
  return (
    <div className="toast-stack">
      {toasts.map((t) => (
        <div key={t.id} className={`toast toast-${t.level}`}>{t.message}</div>
      ))}
    </div>
  )
}

function QueueDetailViewImpl({
  currentTrack,
  musicQueue,
  isActiveQueue,
  isPlaying,
  onClose,
  onPlayQueue,
  onTogglePlay,
  totalCount,
  addUrl,
  addPending,
  addResult,
  onAddUrlChange,
  onAddTrack,
}: {
  currentTrack: WsTrack | null
  musicQueue: WsTrack[]
  isActiveQueue: boolean
  isPlaying: boolean
  onClose: () => void
  onPlayQueue: () => void
  onTogglePlay: () => void
  totalCount: number
  addUrl: string
  addPending: boolean
  addResult: QueueAddResult | null
  onAddUrlChange: (url: string) => void
  onAddTrack: (url: string) => void
}) {
  const submitAdd = () => {
    const v = addUrl.trim()
    if (v && !addPending) onAddTrack(v)
  }
  // 表示順: アクティブキューなら currentTrack(=music_queue[0]) を先頭にハイライトし、
  // そうでなければ単純に music_queue を一覧する。
  const allTracks: WsTrack[] = (() => {
    if (isActiveQueue && currentTrack) {
      const rest = musicQueue.slice(1)
      return [currentTrack, ...rest]
    }
    return musicQueue
  })()
  const totalLabel = formatTotalDuration(allTracks)
  // サーバーは先頭 200 曲までしか送らない。残りの曲数 (一覧に出ない分)
  const truncatedCount = Math.max(0, totalCount - allTracks.length)
  const heroIsPlaying = isActiveQueue && isPlaying
  // FE-PERF-03: キュー一覧をウィンドウ仮想化
  const { scrollRef, listRef, range } = useWindowedRows(
    allTracks.length, DETAIL_ROW_H,
  )
  return (
    <div className="detail-view" ref={scrollRef}>
      <header className="detail-header">
        <button
          className="detail-back"
          onClick={onClose}
          aria-label="back"
          title="戻る"
        >←</button>
        <span className="detail-header-title">キュー</span>
      </header>
      <section className="detail-hero">
        <div className="detail-cover detail-cover-queue" aria-hidden>≡</div>
        <div className="detail-info">
          <div className="detail-label">QUEUE</div>
          <h2 className="detail-name readonly">キュー</h2>
          <div className="detail-stats">
            {truncatedCount > 0 ? totalCount : allTracks.length}{'\u00a0'}曲 ・ {totalLabel}
            {truncatedCount > 0 ? '以上' : ''}
          </div>
        </div>
      </section>
      {allTracks.length > 0 && (
        <section className="detail-play-row">
          <button
            className="detail-play-all"
            onClick={heroIsPlaying ? onTogglePlay : onPlayQueue}
            aria-label={heroIsPlaying ? 'pause' : 'play'}
            title={heroIsPlaying ? '一時停止' : '再生'}
          >{heroIsPlaying ? '❚❚' : '▶'}</button>
        </section>
      )}
      <form
        className="detail-add detail-add-form"
        noValidate
        onSubmit={(e) => {
          e.preventDefault()
          submitAdd()
        }}
      >
        <input
          className="modal-input"
          type="text"
          inputMode="url"
          enterKeyHint="send"
          maxLength={2000}
          autoComplete="off"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          value={addUrl}
          onChange={(e) => onAddUrlChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key !== 'Escape') return
            // 変換中の Esc は変換の取り消し。画面を閉じる window の Esc まで届かせない
            if (e.nativeEvent.isComposing) {
              e.stopPropagation()
              return
            }
            // 入力がある間の Esc は欄を空にするだけ (空なら従来どおり画面を閉じる)
            if (addUrl) {
              e.stopPropagation()
              onAddUrlChange('')
            }
          }}
          placeholder="曲URLを追加 (https://...)"
          aria-label="キューに追加する曲の URL"
        />
        <button
          type="submit"
          className="modal-btn modal-btn-primary"
          disabled={!addUrl.trim() || addPending}
        >追加</button>
        {!isActiveQueue && (
          <span className="detail-add-hint">
            プレイリストモードです。追加した曲は ▶ でキュー再生に切り替えると流れます
          </span>
        )}
        {/* 成功は 1 行に省略 (よくある長い曲名で一覧が下にずれないように。全文はトーストと
            title 属性で読める)。ただし「(BOT がボイスチャンネルにいないため…)」のような
            末尾の注意書き付きは切れると困るので、エラー・案内と同じく折り返して全文を出す */}
        <span
          className={`detail-add-status${!addPending && addResult ? ` is-${addResult.level}` : ''}${
            !addPending && addResult?.level === 'success' && !addResult.message.endsWith(')')
              ? ' is-oneline' : ''}`}
          role="status"
          title={!addPending && addResult?.level === 'success' ? addResult.message : undefined}
        >
          {addPending ? '曲情報を取得中…' : (addResult?.message ?? '')}
        </span>
      </form>
      <ol className="detail-tracks" ref={listRef}>
        {range.start > 0 && (
          <li className="vlist-spacer" style={{ height: range.start * DETAIL_ROW_H }} aria-hidden />
        )}
        {allTracks.slice(range.start, range.end).map((t, vi) => {
          const i = range.start + vi
          const isCurrent = isActiveQueue && i === 0
          return (
            <li
              key={`${t.url}-${i}`}
              className={`detail-track${isCurrent ? ' current' : ''}`}
            >
              <span className="detail-track-no">
                {isCurrent ? (isPlaying ? '♪' : '▶') : i + 1}
              </span>
              <DeferredImg
                className="detail-track-thumb"
                src={t.artwork || PLACEHOLDER_ART}
              />
              <div className="detail-track-meta">
                <div className="detail-track-title">
                  <MarqueeText text={t.title} />
                </div>
                {t.artist && (
                  <div className="detail-track-artist">{t.artist}</div>
                )}
              </div>
              <span className="detail-track-duration">
                {fmt(t.durationMs)}
              </span>
            </li>
          )
        })}
        {range.end < allTracks.length && (
          <li
            className="vlist-spacer"
            style={{ height: (allTracks.length - range.end) * DETAIL_ROW_H }}
            aria-hidden
          />
        )}
        {truncatedCount > 0 && (
          <li className="detail-empty">ほか {truncatedCount} 曲 (一覧は先頭 200 曲まで表示しています)</li>
        )}
        {allTracks.length === 0 && (
          <li className="detail-empty">キューが空です。上の入力欄から曲の URL を追加できます</li>
        )}
      </ol>
    </div>
  )
}

type Particle = {
  x: number
  y: number
  vx: number
  vy: number
  life: number
  size: number
}

function VisualizerImpl({
  kind,
  isPlaying,
  themeColor,
  rmsRef,
  bandsRef,
  beatAtRef,
}: {
  kind: VisualizerKind
  isPlaying: boolean
  themeColor: string
  rmsRef: React.MutableRefObject<number>
  bandsRef: React.MutableRefObject<number[]>
  beatAtRef: React.MutableRefObject<number>
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  // FE-PERF-06: isPlaying/themeColor は ref 経由でループに渡し、変更で rAF を
  // 再生成しない (envelope/peaks/particles 等の内部状態がリセットされるのを防ぐ)。
  const isPlayingRef = useRef(isPlaying)
  isPlayingRef.current = isPlaying
  const themeColorRef = useRef(themeColor)
  themeColorRef.current = themeColor
  useEffect(() => {
    if (kind === 'off') return
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    let raf = 0
    const start = performance.now()
    let lastTime = start
    let accum = 0
    let envelope = 0
    let smoothBands: number[] = []
    let lastBeatProcessed = 0
    // wmp / dots 用の peak-hold バッファ (バンドごとに直近最大値を保持し、ゆっくり減衰)
    let peaks: number[] = []
    // particles 用ステート
    const particles: Particle[] = []
    // radial 用の回転角 (時間で徐々に回す)
    let rotation = 0
    // FE-PERF-05: wmp の縦グラデは全バー・全フレームで同一 (色固定・高さ固定)。
    // 高さ (h) が変わったときだけ作り直してキャッシュする。
    let wmpGrad: CanvasGradient | null = null
    let wmpGradH = -1

    const resize = () => {
      const dpr = window.devicePixelRatio || 1
      canvas.width = canvas.clientWidth * dpr
      canvas.height = canvas.clientHeight * dpr
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    }
    resize()
    const ro = new ResizeObserver(resize)
    ro.observe(canvas)

    const draw = (now: number) => {
      // 毎フレーム最新値を ref から読む (props 変更で effect を再起動しないため)
      const isPlaying = isPlayingRef.current
      const themeColor = themeColorRef.current
      const dt = now - lastTime
      lastTime = now
      const dts = dt / 1000
      if (isPlaying) accum += dt
      const t = accum / 1000

      // RMS envelope
      const target = isPlaying ? rmsRef.current : 0
      const k = target > envelope ? 0.25 : 0.06
      envelope += (target - envelope) * k
      const lvl = Math.min(1, envelope * 3.0)

      // バンドスムージング
      const rawBands = bandsRef.current
      if (rawBands.length && smoothBands.length !== rawBands.length) {
        smoothBands = rawBands.slice()
        peaks = new Array(rawBands.length).fill(0)
      }
      if (rawBands.length) {
        for (let i = 0; i < rawBands.length; i++) {
          const v = isPlaying ? rawBands[i] : 0
          const kb = v > smoothBands[i] ? 0.45 : 0.12
          smoothBands[i] += (v - smoothBands[i]) * kb
          // peak hold: 即時 attack、ゆっくり release
          if (smoothBands[i] > peaks[i]) peaks[i] = smoothBands[i]
          else peaks[i] = Math.max(0, peaks[i] - dts * 0.45)
        }
      }

      // 新しいビートを検知
      const isNewBeat =
        beatAtRef.current > 0 && beatAtRef.current !== lastBeatProcessed
      let beatFlash = 0
      if (beatAtRef.current > 0) {
        const since = now - beatAtRef.current
        if (since >= 0 && since < 250) beatFlash = 1 - since / 250
      }

      const w = canvas.clientWidth
      const h = canvas.clientHeight
      ctx.clearRect(0, 0, w, h)
      ctx.fillStyle = themeColor
      ctx.strokeStyle = themeColor

      // ---------- bars: 縦バー + ビートで全体パンチ ----------
      if (kind === 'bars') {
        const N = smoothBands.length || 16
        const gap = 4
        const bw = (w - gap * (N + 1)) / N
        const punch = 1 + 0.08 * beatFlash
        // FE-PERF-05: withAlpha (正規表現) はフレーム内で themeColor 不変なので
        // ループ外で一度だけ計算する。
        const cTop = withAlpha(themeColor, 0.85)
        const cBot = withAlpha(themeColor, 0.35)
        for (let i = 0; i < N; i++) {
          const v = (smoothBands[i] ?? 0) * punch
          const h2 = (0.04 + 0.92 * v) * h * 0.85
          // 縦方向グラデーション (下: themeColor, 上: ライト)。高さが可変なので
          // gradient 自体はバーごとに作るが、色文字列は使い回す。
          const grad = ctx.createLinearGradient(0, h - h2, 0, h)
          grad.addColorStop(0, cTop)
          grad.addColorStop(1, cBot)
          ctx.fillStyle = grad
          ctx.globalAlpha = 0.85 + 0.15 * beatFlash
          roundRect(ctx, gap + i * (bw + gap), h - h2, bw, h2, Math.min(bw / 3, 6))
          ctx.fill()
        }
        ctx.globalAlpha = 1
      }

      // ---------- mirror: 中央線から上下対称 ----------
      else if (kind === 'mirror') {
        const N = smoothBands.length || 16
        const gap = 3
        const bw = (w - gap * (N + 1)) / N
        const mid = h / 2
        const maxHalf = h * 0.42
        const punch = 1 + 0.1 * beatFlash
        // FE-PERF-05: 色文字列 (withAlpha 正規表現) をループ外で一度だけ計算
        const cStrong = withAlpha(themeColor, 0.95)
        const cWeak = withAlpha(themeColor, 0.25)
        for (let i = 0; i < N; i++) {
          const v = (smoothBands[i] ?? 0) * punch
          const half = (0.05 + 0.92 * v) * maxHalf
          const x = gap + i * (bw + gap)
          // 上向き
          const gUp = ctx.createLinearGradient(0, mid - half, 0, mid)
          gUp.addColorStop(0, cStrong)
          gUp.addColorStop(1, cWeak)
          ctx.fillStyle = gUp
          roundRect(ctx, x, mid - half, bw, half, Math.min(bw / 3, 5))
          ctx.fill()
          // 下向き (ミラー)
          const gDn = ctx.createLinearGradient(0, mid, 0, mid + half)
          gDn.addColorStop(0, cWeak)
          gDn.addColorStop(1, cStrong)
          ctx.fillStyle = gDn
          roundRect(ctx, x, mid, bw, half, Math.min(bw / 3, 5))
          ctx.fill()
        }
        // 中央ライン
        ctx.globalAlpha = 0.18 + 0.4 * beatFlash
        ctx.fillStyle = themeColor
        ctx.fillRect(0, mid - 1, w, 2)
        ctx.globalAlpha = 1
      }

      // ---------- wmp: クラシック VU グラデ (緑→黄→赤) + ピークホールド ----------
      else if (kind === 'wmp') {
        const N = smoothBands.length || 16
        const gap = 3
        const bw = (w - gap * (N + 1)) / N
        const maxH = h * 0.88
        const punch = 1 + 0.06 * beatFlash
        for (let i = 0; i < N; i++) {
          const v = Math.min(1, (smoothBands[i] ?? 0) * punch)
          const barH = v * maxH
          const x = gap + i * (bw + gap)
          const yTop = h - barH
          // クラシック WMP の縦グラデ (緑→黄→オレンジ→赤、ピーク帯で白)。
          // FE-PERF-05: 色固定・高さ固定なので全バー/全フレームで使い回す。
          if (!wmpGrad || wmpGradH !== h) {
            const g = ctx.createLinearGradient(0, h, 0, h - maxH)
            g.addColorStop(0.00, '#00d05a')
            g.addColorStop(0.45, '#a8e600')
            g.addColorStop(0.65, '#ffd000')
            g.addColorStop(0.85, '#ff5a1f')
            g.addColorStop(1.00, '#ff2a2a')
            wmpGrad = g
            wmpGradH = h
          }
          ctx.fillStyle = wmpGrad
          ctx.globalAlpha = 0.92
          // バー本体 (角丸トップ)
          roundRect(ctx, x, yTop, bw, barH, Math.min(bw / 3, 4))
          ctx.fill()
          // 内側ハイライト (3D 感)
          ctx.globalAlpha = 0.22
          ctx.fillStyle = '#ffffff'
          ctx.fillRect(x + 1, yTop, Math.max(1, bw * 0.25), barH)
          // ピークホールドの白いキャップ
          const peakV = peaks[i] ?? 0
          const peakY = h - peakV * maxH
          ctx.globalAlpha = 0.95
          // themeColor を使ってキャップを着色 (アクセント)
          ctx.fillStyle = themeColor
          ctx.fillRect(x, Math.max(0, peakY - 3), bw, 2)
        }
        ctx.globalAlpha = 1
        // 床のリフレクション (微かな床面影)
        const floor = ctx.createLinearGradient(0, h - 4, 0, h)
        floor.addColorStop(0, 'rgba(255,255,255,0)')
        floor.addColorStop(1, 'rgba(255,255,255,0.07)')
        ctx.fillStyle = floor
        ctx.fillRect(0, h - 4, w, 4)
      }

      // ---------- digital: 車載 HU 風の長方形 LED セグメント ----------
      else if (kind === 'digital') {
        const N = smoothBands.length || 16
        const cols = N
        const rows = 28
        const gapX = 2
        const gapY = 2
        const cellW = (w - gapX * (cols + 1)) / cols
        const cellH = (h - gapY * (rows + 1)) / rows
        // 背景の "OFF" セグメント (常時うっすら見える格子)
        ctx.fillStyle = withAlpha(themeColor, 0.05)
        for (let i = 0; i < cols; i++) {
          const colX = gapX + i * (cellW + gapX)
          for (let r = 0; r < rows; r++) {
            const rowY = h - gapY - r * (cellH + gapY) - cellH
            ctx.fillRect(colX, rowY, cellW, cellH)
          }
        }
        // 点灯セグメント (各帯のレベルぶんだけ下から塗る)
        for (let i = 0; i < cols; i++) {
          const v = smoothBands[i] ?? 0
          const litRows = Math.min(rows, Math.round(v * rows))
          const peakRow = Math.min(
            rows - 1, Math.round((peaks[i] ?? 0) * (rows - 1)),
          )
          const colX = gapX + i * (cellW + gapX)
          for (let r = 0; r < litRows; r++) {
            const rowY = h - gapY - r * (cellH + gapY) - cellH
            const lvl01 = r / Math.max(1, rows - 1)
            ctx.fillStyle = ledColor(themeColor, lvl01, 1)
            // 上端 (1段分) だけ明るく抜く -> セグメント感を強調
            ctx.fillRect(colX, rowY, cellW, cellH)
            ctx.fillStyle = ledColor(themeColor, lvl01, 0.55)
            ctx.fillRect(colX, rowY + cellH * 0.55, cellW, cellH * 0.45)
          }
          // ピークホールドのキャップ: 白の独立した 1 セグメント
          if (peakRow >= litRows && peakRow < rows) {
            const rowY = h - gapY - peakRow * (cellH + gapY) - cellH
            ctx.fillStyle = '#ffffff'
            ctx.shadowBlur = 6 + 6 * beatFlash
            ctx.shadowColor = themeColor
            ctx.fillRect(colX, rowY, cellW, cellH)
            ctx.shadowBlur = 0
          }
        }
        // ビート時の全体グロー (上端ラインがフラッシュ)
        if (beatFlash > 0) {
          ctx.fillStyle = `rgba(255, 255, 255, ${0.08 * beatFlash})`
          ctx.fillRect(0, 0, w, gapY + cellH)
        }
      }

      // ---------- dots: 8bit 風 LED ドットマトリクス ----------
      else if (kind === 'dots') {
        const N = smoothBands.length || 16
        const cols = N
        const rows = 28
        const gapX = 3
        const gapY = 2
        const cellW = (w - gapX * (cols + 1)) / cols
        const cellH = (h - gapY * (rows + 1)) / rows
        const dotR = Math.max(1.5, Math.min(cellW, cellH) * 0.42)
        for (let i = 0; i < cols; i++) {
          const v = smoothBands[i] ?? 0
          const litRows = Math.min(rows, Math.round(v * rows))
          const peakRow = Math.min(rows - 1, Math.round((peaks[i] ?? 0) * (rows - 1)))
          const colX = gapX + i * (cellW + gapX) + cellW / 2
          for (let r = 0; r < rows; r++) {
            const rowY = h - gapY - r * (cellH + gapY) - cellH / 2
            const lit = r < litRows
            const isPeak = r === peakRow && peakRow >= litRows
            const lvl01 = r / rows
            if (lit) {
              // 高度ごとに themeColor → 警告色へ滑らかに遷移
              ctx.fillStyle = ledColor(themeColor, lvl01, 1)
              ctx.shadowBlur = 6 + 4 * beatFlash
              ctx.shadowColor = themeColor
            } else if (isPeak) {
              ctx.fillStyle = '#ffffff'
              ctx.shadowBlur = 8
              ctx.shadowColor = themeColor
            } else {
              // 未点灯はうっすら格子状に
              ctx.fillStyle = withAlpha(themeColor, 0.08)
              ctx.shadowBlur = 0
            }
            ctx.beginPath()
            ctx.arc(colX, rowY, dotR, 0, Math.PI * 2)
            ctx.fill()
          }
        }
        ctx.shadowBlur = 0
      }

      // ---------- wave: 折れ線 (バンドで局所変調 + ビートで太さ脈動) ----------
      else if (kind === 'wave') {
        ctx.globalAlpha = 0.75
        ctx.lineWidth = 2 + 3 * beatFlash
        ctx.shadowBlur = 12 * beatFlash
        ctx.shadowColor = themeColor
        ctx.beginPath()
        const N = smoothBands.length || 16
        const amp = h * (0.16 + 0.28 * lvl)
        for (let x = 0; x <= w; x += 3) {
          const k2 = x / w
          const bi = Math.min(N - 1, Math.floor(k2 * N))
          const bv = smoothBands[bi] ?? 0
          const y = h / 2
            + Math.sin(k2 * 8 + t * 3) * amp * (0.3 + bv)
            + Math.sin(k2 * 4 - t * 2) * amp * 0.4 * (0.3 + bv)
          if (x === 0) ctx.moveTo(x, y)
          else ctx.lineTo(x, y)
        }
        ctx.stroke()
        ctx.shadowBlur = 0
        ctx.globalAlpha = 1
      }

      // ---------- pulse: 同心円 + ビート時の特大リング ----------
      else if (kind === 'pulse') {
        const cx = w / 2
        const cy = h / 2
        const base = Math.min(w, h) * 0.18
        for (let i = 0; i < 4; i++) {
          const pulse = (t * 0.6 + i * 0.25) % 1
          const r = base * (1 + 0.4 * lvl)
            + pulse * Math.min(w, h) * (0.32 + 0.25 * lvl)
          ctx.globalAlpha = (1 - pulse) * (0.15 + 0.5 * lvl)
          ctx.lineWidth = 2
          ctx.beginPath()
          ctx.arc(cx, cy, r, 0, Math.PI * 2)
          ctx.stroke()
        }
        if (beatFlash > 0) {
          const r = base * (1.2 + 0.6 * lvl) + (1 - beatFlash) * Math.min(w, h) * 0.18
          ctx.globalAlpha = 0.4 * beatFlash
          ctx.lineWidth = 4 + 6 * beatFlash
          ctx.beginPath()
          ctx.arc(cx, cy, r, 0, Math.PI * 2)
          ctx.stroke()
        }
        ctx.globalAlpha = 1
      }

      // ---------- radial: 中央から放射するバンドバー ----------
      else if (kind === 'radial') {
        const cx = w / 2
        const cy = h / 2
        const N = smoothBands.length || 16
        const innerR = Math.min(w, h) * (0.12 + 0.04 * lvl)
        const maxOut = Math.min(w, h) * 0.32
        rotation += dts * 0.08  // ゆっくり回転
        for (let i = 0; i < N; i++) {
          const v = smoothBands[i] ?? 0
          // 半分のバーで一周にする (左右対称感)
          const angle =
            rotation + (i / N) * Math.PI * 2 + (beatFlash * 0.08)
          const len = innerR + (0.05 + 0.95 * v) * maxOut
          const x1 = cx + Math.cos(angle) * innerR
          const y1 = cy + Math.sin(angle) * innerR
          const x2 = cx + Math.cos(angle) * len
          const y2 = cy + Math.sin(angle) * len
          ctx.globalAlpha = 0.4 + 0.5 * v
          ctx.lineWidth = 3
          ctx.lineCap = 'round'
          ctx.beginPath()
          ctx.moveTo(x1, y1)
          ctx.lineTo(x2, y2)
          ctx.stroke()
        }
        // 中央のコア (RMS で大きさ)
        const coreR = innerR * (0.45 + 0.4 * lvl + 0.3 * beatFlash)
        const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreR)
        grad.addColorStop(0, withAlpha(themeColor, 0.5 + 0.3 * beatFlash))
        grad.addColorStop(1, withAlpha(themeColor, 0))
        ctx.fillStyle = grad
        ctx.globalAlpha = 1
        ctx.beginPath()
        ctx.arc(cx, cy, coreR, 0, Math.PI * 2)
        ctx.fill()
        ctx.globalAlpha = 1
      }

      // ---------- particles: ビートで弾ける粒子 ----------
      else if (kind === 'particles') {
        const cx = w / 2
        const cy = h / 2
        // ビート発生時に粒子を放出
        if (isNewBeat) {
          lastBeatProcessed = beatAtRef.current
          const burst = 18 + Math.floor(28 * lvl)
          for (let i = 0; i < burst; i++) {
            const a = Math.random() * Math.PI * 2
            const s = 80 + Math.random() * 200 * (0.5 + lvl)
            particles.push({
              x: cx,
              y: cy,
              vx: Math.cos(a) * s,
              vy: Math.sin(a) * s,
              life: 1.0,
              size: 2 + Math.random() * 3,
            })
          }
        }
        // 中央パルス (RMS)
        const haloR = Math.min(w, h) * (0.06 + 0.14 * lvl + 0.1 * beatFlash)
        const halo = ctx.createRadialGradient(cx, cy, 0, cx, cy, haloR)
        halo.addColorStop(0, withAlpha(themeColor, 0.45))
        halo.addColorStop(1, withAlpha(themeColor, 0))
        ctx.fillStyle = halo
        ctx.beginPath()
        ctx.arc(cx, cy, haloR, 0, Math.PI * 2)
        ctx.fill()
        // 粒子更新 + 描画
        for (let i = particles.length - 1; i >= 0; i--) {
          const p = particles[i]
          p.x += p.vx * dts
          p.y += p.vy * dts
          p.vx *= 0.985
          p.vy *= 0.985
          p.life -= dts * 0.85  // ~1.15秒で消える
          if (p.life <= 0) {
            particles.splice(i, 1)
            continue
          }
          ctx.globalAlpha = Math.max(0, p.life) * 0.85
          ctx.fillStyle = themeColor
          ctx.beginPath()
          ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2)
          ctx.fill()
        }
        ctx.globalAlpha = 1
        // 暴走防止
        if (particles.length > 400) particles.splice(0, particles.length - 400)
      }

      raf = requestAnimationFrame(draw)
    }
    raf = requestAnimationFrame(draw)
    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
    }
  }, [kind, rmsRef, bandsRef, beatAtRef])
  if (kind === 'off') return null
  return <canvas ref={canvasRef} className={`visualizer visualizer-${kind}`} aria-hidden />
}

function roundRect(
  ctx: CanvasRenderingContext2D,
  x: number, y: number, w: number, h: number, r: number,
) {
  if (w <= 0 || h <= 0) return
  const rr = Math.min(r, w / 2, h / 2)
  ctx.beginPath()
  ctx.moveTo(x + rr, y)
  ctx.arcTo(x + w, y, x + w, y + h, rr)
  ctx.arcTo(x + w, y + h, x, y + h, rr)
  ctx.arcTo(x, y + h, x, y, rr)
  ctx.arcTo(x, y, x + w, y, rr)
  ctx.closePath()
}

function withAlpha(hex: string, a: number): string {
  // 簡易 hex (#RRGGBB) → rgba()
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim())
  if (!m) return hex
  const v = parseInt(m[1], 16)
  const r = (v >> 16) & 255
  const g = (v >> 8) & 255
  const b = v & 255
  return `rgba(${r}, ${g}, ${b}, ${a})`
}

function ledColor(baseHex: string, lvl: number, alpha: number): string {
  // 高度 (0..1) を themeColor → 黄 → 赤 に混色した LED 風配色を返す
  const m = /^#?([0-9a-f]{6})$/i.exec(baseHex.trim())
  if (!m) return baseHex
  const v = parseInt(m[1], 16)
  const br = (v >> 16) & 255
  const bg = (v >> 8) & 255
  const bb = v & 255
  // 中間: 黄 (255, 200, 40), 上端: 赤 (255, 60, 40)
  const my = { r: 255, g: 200, b: 40 }
  const mr = { r: 255, g: 60,  b: 40 }
  let r: number, g: number, b: number
  if (lvl < 0.65) {
    const k = lvl / 0.65
    r = br + (my.r - br) * k
    g = bg + (my.g - bg) * k
    b = bb + (my.b - bb) * k
  } else {
    const k = (lvl - 0.65) / 0.35
    r = my.r + (mr.r - my.r) * k
    g = my.g + (mr.g - my.g) * k
    b = my.b + (mr.b - my.b) * k
  }
  return `rgba(${Math.round(r)}, ${Math.round(g)}, ${Math.round(b)}, ${alpha})`
}

function MarqueeText({ text }: { text: string }) {
  const innerRef = useRef<HTMLSpanElement | null>(null)
  const [shift, setShift] = useState<string | null>(null)
  // FE-PERF-04: 行ごとに常時 ResizeObserver を張ると、非仮想化時は曲数分
  // (最大2000) の Observer が生きてしまう。代わりに「マウント時に一度だけ計測」
  // + 「hover で再計測 (リサイズ追従)」にする。仮想化 (FE-PERF-03) でマウント
  // される行は可視範囲分だけに抑えられるので、Observer 撤去で十分軽い。
  const measure = useCallback(() => {
    const el = innerRef.current
    const parent = el?.parentElement
    if (!el || !parent) return
    // 子は本来の長さ (scrollWidth) を持つ。差分だけ左にスライドさせれば右端が見える。
    const diff = el.scrollWidth - parent.clientWidth
    setShift(diff > 1 ? `${-diff - 8}px` : null)
  }, [])
  // マウント時 / text 変化時に一度計測しておく。これにより行のどこをホバーしても
  // (タイトル span 上を通らなくても) CSS の :hover でマーキーが動く。
  useLayoutEffect(() => { measure() }, [text, measure])
  return (
    <span
      className={`marquee${shift ? ' marquee-overflow' : ''}`}
      onMouseEnter={measure}
    >
      <span
        ref={innerRef}
        className="marquee-inner"
        style={shift ? cssVars({ '--marquee-shift': shift }) : undefined}
      >{text}</span>
    </span>
  )
}

function IconRepeat({ size = 16 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <polyline points="17 1 21 5 17 9" />
      <path d="M3 11V9a4 4 0 0 1 4-4h14" />
      <polyline points="7 23 3 19 7 15" />
      <path d="M21 13v2a4 4 0 0 1-4 4H3" />
    </svg>
  )
}

function IconNormalize({ size = 16 }: { size?: number }) {
  // 上下が揃った音量バーで「ノーマライズ」を表現
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="currentColor"
      stroke="currentColor"
      strokeWidth="1.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <rect x="3" y="9" width="3" height="6" rx="0.8" />
      <rect x="8" y="7" width="3" height="10" rx="0.8" />
      <rect x="13" y="8" width="3" height="8" rx="0.8" />
      <rect x="18" y="10" width="3" height="4" rx="0.8" />
    </svg>
  )
}

function IconShuffle({ size = 16 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <polyline points="16 3 21 3 21 8" />
      <line x1="4" y1="20" x2="21" y2="3" />
      <polyline points="21 16 21 21 16 21" />
      <line x1="15" y1="15" x2="21" y2="21" />
      <line x1="4" y1="4" x2="9" y2="9" />
    </svg>
  )
}

// FE-PERF-08: URL→抽出色のキャッシュ。曲送り連打や再表示で同一 URL を
// 何度もデコードし直さない (上限 64 件の素朴な Map)。
const _artworkColorCache = new Map<string, { bg: string; accent: string } | null>()

async function extractArtworkColors(
  imageSrc: string,
): Promise<{ bg: string; accent: string } | null> {
  if (_artworkColorCache.has(imageSrc)) {
    return _artworkColorCache.get(imageSrc) ?? null
  }
  const result = await extractArtworkColorsUncached(imageSrc)
  // 失敗 (null) はキャッシュしない (一時的なネットワーク失敗の再試行を許す)
  if (result) {
    if (_artworkColorCache.size >= 64) {
      const first = _artworkColorCache.keys().next().value
      if (first !== undefined) _artworkColorCache.delete(first)
    }
    _artworkColorCache.set(imageSrc, result)
  }
  return result
}

async function extractArtworkColorsUncached(
  imageSrc: string,
): Promise<{ bg: string; accent: string } | null> {
  return new Promise((resolve) => {
    const img = new Image()
    img.crossOrigin = 'anonymous'
    img.onload = () => {
      try {
        const size = 16
        const canvas = document.createElement('canvas')
        canvas.width = size
        canvas.height = size
        const ctx = canvas.getContext('2d')
        if (!ctx) { resolve(null); return }
        ctx.drawImage(img, 0, 0, size, size)
        const data = ctx.getImageData(0, 0, size, size).data
        let r = 0, g = 0, b = 0
        const count = data.length / 4
        for (let i = 0; i < data.length; i += 4) {
          r += data[i]; g += data[i + 1]; b += data[i + 2]
        }
        r = r / count
        g = g / count
        b = b / count
        // 背景用: 暗めに落として周囲と馴染ませる
        const bgR = Math.round(r * 0.45)
        const bgG = Math.round(g * 0.45)
        const bgB = Math.round(b * 0.45)
        // ビジュアライザ用: 最大チャンネルを 200 まで引き上げて鮮やかに
        // (暗いサムネでも視覚的に映える明度を確保)
        const m = Math.max(r, g, b, 1)
        const k = Math.min(2.5, 200 / m)
        const ar = Math.min(255, Math.round(r * k))
        const ag = Math.min(255, Math.round(g * k))
        const ab = Math.min(255, Math.round(b * k))
        const hex =
          '#' + [ar, ag, ab].map((v) => v.toString(16).padStart(2, '0')).join('')
        resolve({
          bg: `rgb(${bgR}, ${bgG}, ${bgB})`,
          accent: hex,
        })
      } catch (err) {
        console.warn('color extraction failed', err)
        resolve(null)
      }
    }
    img.onerror = () => resolve(null)
    img.src = imageSrc
  })
}

function fmt(ms: number) {
  const s = Math.max(0, Math.floor(ms / 1000))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

// FE-PERF-02: 重い子コンポーネントを React.memo でラップし、props が変わらない
// 限り (progress メッセージ等による App の再 render では) 再 render しないようにする。
// 渡すハンドラは App 側で useCallback 済み、配列/オブジェクト props も状態更新時のみ変化する。
const LibraryItemsList = memo(LibraryItemsListImpl)
const QueueItemsList = memo(QueueItemsListImpl)
const PlaylistDetailView = memo(PlaylistDetailViewImpl)
const QueueDetailView = memo(QueueDetailViewImpl)
const Visualizer = memo(VisualizerImpl)
