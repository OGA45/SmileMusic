// SmileMusic Activity API との WebSocket 通信ラッパ。

export type WsTrack = {
  title: string
  artist: string
  artwork: string
  durationMs: number
  url: string
  source: string
  bitrateKbps: number
}

export type WsPlaylistInfo = {
  id: string
  name: string
  index: number
  loop: boolean
  shuffle: boolean
  nextIndex: number  // 次に再生される予定の index。-1 なら次無し
  loopSingle: boolean
}

export type WsStatePayload = {
  mode: 'queue' | 'playlist'
  guildName: string
  track: WsTrack | null
  positionMs: number
  isPlaying: boolean
  loading: boolean
  // MOD-PERF-04: upnext / musicQueue は state には含めず queue_full で別送する
  playlist: WsPlaylistInfo | null
  queueLoop: boolean
  queueLoopSingle: boolean
  queueNextIndex: number
  normalize: boolean
}

export type WsPlaylistListItem = {
  id: string
  name: string
  trackCount: number
  inLibrary: boolean
  libraryAddedAt: string | null
  coverUrl: string
  tags: string[]
}

export type WsLibraryItem = {
  id: string
  name: string
  ownerId: string
  trackCount: number
  coverUrl: string
  addedByUserId: string
  addedByUsername: string
  addedByAvatarUrl: string
  addedAt: string | null
  tags: string[]
}

export type WsPlaylistDetailTrack = {
  title: string
  url: string
  artwork: string
  durationMs: number
}

export type VisualizerKind =
  | 'off' | 'bars' | 'wave' | 'pulse' | 'mirror' | 'radial' | 'particles'
  | 'wmp' | 'dots' | 'digital'

export type WsProcess = {
  id: string
  kind: string
  sourceUrl: string
  name: string
  status: 'running' | 'success' | 'error'
  progressCurrent: number
  progressTotal: number
  message: string
  startedAt: string
  finishedAt: string
}

export type WsPlaylistDetail = {
  id: string
  name: string
  coverUrl: string
  tracks: WsPlaylistDetailTrack[]
  total: number  // BC-PERF-06: 全曲数 (tracks は読み込み済み分のみ)
  tags: string[]
  isOwner: boolean
}

export type WsMessage =
  | { kind: 'state'; state: WsStatePayload }
  | { kind: 'progress'; positionMs: number }
  | { kind: 'queue'; queue: WsTrack[] }
  | { kind: 'queue_full'; upnext: WsTrack[]; musicQueue: WsTrack[]; musicQueueTotal: number }
  | { kind: 'stopped' }
  | { kind: 'notify'; level: 'info' | 'error' | 'success'; message: string; reqId?: string }
  | {
      kind: 'playlists'
      playlists: WsPlaylistListItem[]
      autoSelectLast: boolean
      lastUsedPlaylistId: string | null
      autoAddToLibrary: boolean
      bgTintEnabled: boolean
      audioVisualizer: VisualizerKind
      themeColor: string | null
      visualizerTintEnabled: boolean
    }
  | { kind: 'library'; items: WsLibraryItem[] }
  | { kind: 'playlist_detail'; detail: WsPlaylistDetail }
  | {
      kind: 'playlist_detail_page'
      id: string
      offset: number
      tracks: WsPlaylistDetailTrack[]
    }
  | { kind: 'audio_features'; rms: number; bands: number[]; beat: boolean }
  | { kind: 'processes'; items: WsProcess[] }
  | {
      kind: 'guild_settings'
      announceChannelId: string | null
      announceChannelName: string
      systemChannelName: string
      canManage: boolean | null
      channels: { id: string; name: string }[] | null
    }

export type WsConnection = {
  close: () => void
  sendPlay: () => void
  sendPause: () => void
  sendSkip: () => void
  sendPrev: () => void
  sendSeek: (positionMs: number) => void
  setLoopMode: (mode: 'off' | 'all' | 'one') => void
  setNormalize: (enabled: boolean) => void
  setShufflePlaylist: (enabled: boolean) => void
  listPlaylists: () => void
  createPlaylist: (name: string, url: string) => void
  importPlaylistFromUrl: (url: string, name: string) => void
  addTrackToPlaylist: (playlistId: string, url: string) => void
  removePlaylistTrack: (playlistId: string, position: number) => void
  reorderPlaylistTrack: (playlistId: string, from: number, to: number) => void
  addPlaylistToQueue: (playlistId: string) => void
  // 送れたら true (未接続なら false)。結果は同じ reqId 付きの notify で返る
  addTrackToQueue: (url: string, reqId: string) => boolean
  deletePlaylist: (playlistId: string) => void
  renamePlaylist: (playlistId: string, newName: string) => void
  setPlaylistTags: (playlistId: string, tags: string[]) => void
  setLibraryMembership: (playlistId: string, inLibrary: boolean) => void
  librarySelectQueue: () => void
  librarySelectPlaylist: (playlistId: string, startIndex?: number) => void
  playlistJump: (index: number) => void
  getPlaylistDetail: (playlistId: string) => void
  loadPlaylistDetailPage: (playlistId: string, offset: number) => void
  setPref: (prefs: {
    autoSelectLast?: boolean
    autoAddToLibrary?: boolean
    bgTintEnabled?: boolean
    audioVisualizer?: VisualizerKind
    themeColor?: string | null
    visualizerTintEnabled?: boolean
  }) => void
  requestGuildSettings: () => void
  setGuildPref: (p: { announceChannelId: string | null }) => void
}

type ConnectOptions = {
  guildId: string
  accessToken: string
  inDiscord: boolean
  onOpen?: () => void
  onClose?: (info: { code: number; reason: string }) => void
  onMessage: (msg: WsMessage) => void
}

// FE-SEC-05: サーバ由来の画像 URL を http(s) / 自オリジンの proxy 相対パスのみ許可。
// data:/javascript: 等の想定外スキームを弾く。
export function safeUrl(v: unknown): string {
  if (typeof v !== 'string' || !v) return ''
  if (v.startsWith('/api/image') || v.startsWith('/')) return v
  if (/^https?:\/\//i.test(v)) return v
  return ''
}

// FE-SEC-06: サーバ由来のテーマカラーを色トークン (#rgb/#rrggbb/rgb()/rgba()) に限定。
// CSS 変数 --accent は background ショートハンドに展開され url() を受けるため、
// 不正値は破棄して既定にフォールバックさせる。
export function sanitizeColor(v: unknown): string | null {
  if (typeof v !== 'string') return null
  const s = v.trim()
  if (/^#[0-9a-fA-F]{3}$/.test(s) || /^#[0-9a-fA-F]{6}$/.test(s)) return s
  if (/^rgba?\(\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*(,\s*[\d.]+\s*)?\)$/.test(s)) return s
  return null
}

function mapTrack(t: unknown): WsTrack {
  const o = (t ?? {}) as Record<string, unknown>
  return {
    title: typeof o.title === 'string' ? o.title : '',
    artist: typeof o.artist === 'string' ? o.artist : '',
    artwork: safeUrl(o.artwork),
    durationMs: Number(o.duration_ms ?? 0),
    url: typeof o.url === 'string' ? o.url : '',
    source: typeof o.source === 'string' ? o.source : '',
    bitrateKbps: Number(o.bitrate_kbps ?? 0),
  }
}

function mapPlaylistInfo(p: unknown): WsPlaylistInfo | null {
  if (!p || typeof p !== 'object') return null
  const o = p as Record<string, unknown>
  return {
    id: typeof o.id === 'string' ? o.id : '',
    name: typeof o.name === 'string' ? o.name : '',
    index: Number(o.index ?? 0),
    loop: Boolean(o.loop),
    shuffle: Boolean(o.shuffle),
    nextIndex: Number(o.next_index ?? -1),
    loopSingle: Boolean(o.loop_single),
  }
}

function mapDetailTracks(raw: unknown): WsPlaylistDetailTrack[] {
  if (!Array.isArray(raw)) return []
  return raw.map((t) => {
    const o = (t ?? {}) as Record<string, unknown>
    return {
      title: typeof o.title === 'string' ? o.title : '',
      url: typeof o.url === 'string' ? o.url : '',
      artwork: safeUrl(o.artwork),
      durationMs: Number(o.duration_ms ?? 0),
    }
  })
}

function fromServer(msg: unknown): WsMessage | null {
  if (!msg || typeof msg !== 'object') return null
  const m = msg as Record<string, unknown>
  switch (m.type) {
    case 'state':
      return {
        kind: 'state',
        state: {
          mode: m.mode === 'playlist' ? 'playlist' : 'queue',
          guildName: typeof m.guild_name === 'string' ? m.guild_name : '',
          track: m.track ? mapTrack(m.track) : null,
          positionMs: Number(m.position_ms ?? 0),
          isPlaying: Boolean(m.is_playing),
          loading: Boolean(m.loading),
          playlist: mapPlaylistInfo(m.playlist),
          queueLoop: Boolean(m.queue_loop),
          queueLoopSingle: Boolean(m.queue_loop_single),
          queueNextIndex: Number(m.queue_next_index ?? -1),
          normalize: Boolean(m.normalize),
        },
      }
    case 'progress':
      return { kind: 'progress', positionMs: Number(m.position_ms ?? 0) }
    case 'queue':
      return { kind: 'queue', queue: Array.isArray(m.queue) ? m.queue.map(mapTrack) : [] }
    case 'queue_full': {
      const musicQueue = Array.isArray(m.music_queue) ? m.music_queue.map(mapTrack) : []
      return {
        kind: 'queue_full',
        upnext: Array.isArray(m.upnext) ? m.upnext.map(mapTrack) : [],
        musicQueue,
        // music_queue は先頭 200 件だけ届く。実際の件数 (古いサーバーなら一覧の長さ)
        musicQueueTotal:
          typeof m.music_queue_total === 'number' ? m.music_queue_total : musicQueue.length,
      }
    }
    case 'stopped':
      return { kind: 'stopped' }
    case 'playlist_detail': {
      const tracks = mapDetailTracks(m.tracks)
      return {
        kind: 'playlist_detail',
        detail: {
          id: typeof m.id === 'string' ? m.id : '',
          name: typeof m.name === 'string' ? m.name : '',
          coverUrl: safeUrl(m.cover_url),
          tracks,
          total: Number(m.total ?? tracks.length),
          tags: Array.isArray(m.tags)
            ? m.tags.filter((x): x is string => typeof x === 'string')
            : [],
          isOwner: m.is_owner === undefined ? true : Boolean(m.is_owner),
        },
      }
    }
    case 'playlist_detail_page':
      return {
        kind: 'playlist_detail_page',
        id: typeof m.id === 'string' ? m.id : '',
        offset: Number(m.offset ?? 0),
        tracks: mapDetailTracks(m.tracks),
      }
    case 'library':
      return {
        kind: 'library',
        items: Array.isArray(m.items)
          ? m.items.map((it) => {
              const o = (it ?? {}) as Record<string, unknown>
              return {
                id: typeof o.id === 'string' ? o.id : '',
                name: typeof o.name === 'string' ? o.name : '',
                ownerId: typeof o.owner_id === 'string' ? o.owner_id : '',
                trackCount: Number(o.track_count ?? 0),
                coverUrl: safeUrl(o.cover_url),
                addedByUserId:
                  typeof o.added_by_userid === 'string' ? o.added_by_userid : '',
                addedByUsername:
                  typeof o.added_by_username === 'string' ? o.added_by_username : '',
                addedByAvatarUrl: safeUrl(o.added_by_avatar_url),
                addedAt: typeof o.added_at === 'string' ? o.added_at : null,
                tags: Array.isArray(o.tags)
                  ? o.tags.filter((x): x is string => typeof x === 'string')
                  : [],
              }
            })
          : [],
      }
    case 'audio_features':
      return {
        kind: 'audio_features',
        rms: typeof m.rms === 'number' ? m.rms : 0,
        bands: Array.isArray(m.bands)
          ? m.bands.map((v) => (typeof v === 'number' ? v : 0))
          : [],
        beat: Boolean(m.beat),
      }
    case 'processes':
      return {
        kind: 'processes',
        items: Array.isArray(m.items)
          ? m.items.map((it) => {
              const o = (it ?? {}) as Record<string, unknown>
              const status =
                o.status === 'success' || o.status === 'error' ? o.status : 'running'
              return {
                id: typeof o.id === 'string' ? o.id : '',
                kind: typeof o.kind === 'string' ? o.kind : '',
                sourceUrl: typeof o.source_url === 'string' ? o.source_url : '',
                name: typeof o.name === 'string' ? o.name : '',
                status: status as 'running' | 'success' | 'error',
                progressCurrent: Number(o.progress_current ?? 0),
                progressTotal: Number(o.progress_total ?? 0),
                message: typeof o.message === 'string' ? o.message : '',
                startedAt: typeof o.started_at === 'string' ? o.started_at : '',
                finishedAt: typeof o.finished_at === 'string' ? o.finished_at : '',
              }
            })
          : [],
      }
    case 'notify':
      return {
        kind: 'notify',
        level:
          m.level === 'error' || m.level === 'success' || m.level === 'info'
            ? m.level
            : 'info',
        message: typeof m.message === 'string' ? m.message : '',
        reqId: typeof m.req_id === 'string' ? m.req_id : undefined,
      }
    case 'playlists':
      return {
        kind: 'playlists',
        playlists: Array.isArray(m.playlists)
          ? m.playlists.map((p) => {
              const po = (p ?? {}) as Record<string, unknown>
              return {
                id: typeof po.id === 'string' ? po.id : '',
                name: typeof po.name === 'string' ? po.name : '',
                trackCount: Number(po.track_count ?? 0),
                inLibrary: Boolean(po.in_library),
                libraryAddedAt:
                  typeof po.library_added_at === 'string' ? po.library_added_at : null,
                coverUrl: safeUrl(po.cover_url),
                tags: Array.isArray(po.tags)
                  ? po.tags.filter((x): x is string => typeof x === 'string')
                  : [],
              }
            })
          : [],
        autoSelectLast: Boolean(m.auto_select_last),
        lastUsedPlaylistId:
          typeof m.last_used_playlist_id === 'string' ? m.last_used_playlist_id : null,
        autoAddToLibrary: Boolean(m.auto_add_to_library),
        bgTintEnabled: m.bg_tint_enabled === undefined ? true : Boolean(m.bg_tint_enabled),
        audioVisualizer: (() => {
          const v = m.audio_visualizer
          if (
            v === 'bars' || v === 'wave' || v === 'pulse'
            || v === 'mirror' || v === 'radial' || v === 'particles'
            || v === 'wmp' || v === 'dots' || v === 'digital'
          ) return v
          return 'off'
        })(),
        themeColor: sanitizeColor(m.theme_color),
        visualizerTintEnabled: Boolean(m.visualizer_tint_enabled),
      }
    case 'guild_settings':
      return {
        kind: 'guild_settings',
        announceChannelId: typeof m.announce_channel_id === 'string' ? m.announce_channel_id : null,
        announceChannelName: typeof m.announce_channel_name === 'string' ? m.announce_channel_name : '',
        systemChannelName: typeof m.system_channel_name === 'string' ? m.system_channel_name : '',
        canManage: typeof m.can_manage === 'boolean' ? m.can_manage : null,
        channels: Array.isArray(m.channels)
          ? m.channels.map((c) => {
              const o = (c ?? {}) as Record<string, unknown>
              return { id: String(o.id ?? ''), name: String(o.name ?? '') }
            })
          : null,
      }
    default:
      return null
  }
}

export function connectActivityWS(opts: ConnectOptions): WsConnection {
  // FE-SEC-01: トークンを URL クエリに載せない (ログ/履歴/Referer 漏洩防止)。
  // 接続後の最初のフレームで {type:'auth', token} として送る。
  const url = opts.inDiscord
    ? `wss://${window.location.host}/.proxy/ws/${opts.guildId}`
    : `ws://localhost:8080/ws/${opts.guildId}`

  const ws = new WebSocket(url)
  ws.addEventListener('open', () => {
    try {
      ws.send(JSON.stringify({ type: 'auth', token: opts.accessToken }))
    } catch (err) {
      console.error('ws auth send failed', err)
    }
    opts.onOpen?.()
  })
  ws.addEventListener('message', (e) => {
    try {
      const data = JSON.parse(typeof e.data === 'string' ? e.data : '')
      const msg = fromServer(data)
      if (msg) opts.onMessage(msg)
    } catch (err) {
      console.error('ws parse error', err)
    }
  })
  ws.addEventListener('close', (e) => {
    opts.onClose?.({ code: e.code, reason: e.reason || '' })
  })

  const send = (payload: unknown): boolean => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload))
      return true
    }
    console.warn('ws not open, dropping payload', payload)
    return false
  }

  return {
    close: () => ws.close(),
    sendPlay: () => send({ type: 'play' }),
    sendPause: () => send({ type: 'pause' }),
    sendSkip: () => send({ type: 'skip' }),
    sendPrev: () => send({ type: 'prev' }),
    sendSeek: (positionMs) => send({ type: 'seek', position_ms: positionMs }),
    setLoopMode: (mode) => send({ type: 'set_loop_mode', mode }),
    setNormalize: (enabled) => send({ type: 'set_normalize', enabled }),
    setShufflePlaylist: (enabled) => send({ type: 'set_shuffle_playlist', enabled }),
    listPlaylists: () => send({ type: 'list_playlists' }),
    createPlaylist: (name, url) => send({ type: 'create_playlist', name, url }),
    importPlaylistFromUrl: (url, name) =>
      send({ type: 'import_playlist_url', url, name }),
    addTrackToPlaylist: (playlistId, url) =>
      send({ type: 'add_track_to_playlist', playlist_id: playlistId, url }),
    removePlaylistTrack: (playlistId, position) =>
      send({ type: 'remove_playlist_track', playlist_id: playlistId, position }),
    reorderPlaylistTrack: (playlistId, from, to) =>
      send({ type: 'reorder_playlist_track', playlist_id: playlistId, from, to }),
    addPlaylistToQueue: (playlistId) =>
      send({ type: 'add_playlist_to_queue', playlist_id: playlistId }),
    addTrackToQueue: (url, reqId) =>
      send({ type: 'add_track_to_queue', url, req_id: reqId }),
    deletePlaylist: (playlistId) => send({ type: 'delete_playlist', playlist_id: playlistId }),
    renamePlaylist: (playlistId, newName) =>
      send({ type: 'rename_playlist', playlist_id: playlistId, new_name: newName }),
    setPlaylistTags: (playlistId, tags) =>
      send({ type: 'set_playlist_tags', playlist_id: playlistId, tags }),
    setLibraryMembership: (playlistId, inLibrary) =>
      send({ type: 'set_library_membership', playlist_id: playlistId, in_library: inLibrary }),
    librarySelectQueue: () => send({ type: 'library_select', kind: 'queue' }),
    librarySelectPlaylist: (playlistId, startIndex) =>
      send({
        type: 'library_select',
        kind: 'playlist',
        playlist_id: playlistId,
        ...(typeof startIndex === 'number' ? { start_index: startIndex } : {}),
      }),
    playlistJump: (index) => send({ type: 'playlist_jump', index }),
    getPlaylistDetail: (playlistId) =>
      send({ type: 'get_playlist_detail', playlist_id: playlistId }),
    loadPlaylistDetailPage: (playlistId, offset) =>
      send({ type: 'load_playlist_detail_page', playlist_id: playlistId, offset }),
    setPref: (prefs) => {
      const payload: Record<string, unknown> = { type: 'set_pref' }
      if (typeof prefs.autoSelectLast === 'boolean') {
        payload.auto_select_last = prefs.autoSelectLast
      }
      if (typeof prefs.autoAddToLibrary === 'boolean') {
        payload.auto_add_to_library = prefs.autoAddToLibrary
      }
      if (typeof prefs.bgTintEnabled === 'boolean') {
        payload.bg_tint_enabled = prefs.bgTintEnabled
      }
      if (typeof prefs.audioVisualizer === 'string') {
        payload.audio_visualizer = prefs.audioVisualizer
      }
      if ('themeColor' in prefs) {
        payload.theme_color = prefs.themeColor
      }
      if (typeof prefs.visualizerTintEnabled === 'boolean') {
        payload.visualizer_tint_enabled = prefs.visualizerTintEnabled
      }
      send(payload)
    },
    requestGuildSettings: () => send({ type: 'get_guild_settings' }),
    setGuildPref: (p) => send({ type: 'set_guild_pref', announce_channel_id: p.announceChannelId }),
  }
}
