import { DiscordSDK } from '@discord/embedded-app-sdk'

const CLIENT_ID = import.meta.env.VITE_DISCORD_CLIENT_ID
const TOKEN_ENDPOINT = import.meta.env.VITE_TOKEN_ENDPOINT ?? '/.proxy/api/token'

// Discord クライアントの iframe 内で開かれた時は frame_id が URL に付く。
// ブラウザで直接開いた時（単体プレビュー）とで挙動を分けるためのフラグ。
export const isInDiscord = new URLSearchParams(window.location.search).has('frame_id')

const sdk = isInDiscord && CLIENT_ID ? new DiscordSDK(CLIENT_ID) : null
export const discordSdk = sdk

// 外部 URL を開く。Discord Activity は iframe 内なので通常の遷移は弾かれるため、
// SDK の openExternalLink を使う (確認ダイアログ経由で外部ブラウザに開く)。
// ブラウザ単体プレビュー時は通常の新規タブで開く。
export function openExternal(url: string): void {
  if (sdk) {
    void sdk.commands.openExternalLink({ url }).catch((e) => {
      console.warn('[discord] openExternalLink failed', e)
    })
  } else {
    window.open(url, '_blank', 'noopener,noreferrer')
  }
}

// FE-SEC-03: 識別子やトークン関連のログは開発時のみ。本番ビルドでは黙る。
const dlog = (...args: unknown[]) => {
  if (import.meta.env.DEV) console.log(...args)
}

// FE-SEC-02: CSRF/リプレイ対策のランダム state を生成する。
function randomState(): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

export type ActivityContext = {
  instanceId: string | null
  channelId: string | null
  guildId: string | null
  user: {
    id: string
    username: string
    avatarUrl: string | null
  } | null
  accessToken: string | null
  authError: string | null
  authStage: 'authorize' | 'token' | 'authenticate' | 'ok' | 'sdk-disabled'
}

function buildAvatarUrl(userId: string, avatarHash: string | null): string | null {
  // Discord CDN は iframe CSP で直接読めないので /api/image 越しにする
  let raw: string
  if (avatarHash) {
    raw = `https://cdn.discordapp.com/avatars/${userId}/${avatarHash}.png?size=64`
  } else {
    try {
      const idBig = BigInt(userId)
      const idx = Number((idBig >> 22n) % 6n)
      raw = `https://cdn.discordapp.com/embed/avatars/${idx}.png`
    } catch {
      return null
    }
  }
  return isInDiscord ? `/api/image?url=${encodeURIComponent(raw)}` : raw
}

export async function initDiscord(): Promise<ActivityContext | null> {
  if (!sdk || !CLIENT_ID) {
    console.warn('[discord] sdk disabled', {
      hasSdk: Boolean(sdk),
      hasClientId: Boolean(CLIENT_ID),
    })
    return null
  }

  dlog('[discord] sdk.ready() …')
  await sdk.ready()
  dlog('[discord] sdk.ready ok', {
    instanceId: sdk.instanceId,
    channelId: sdk.channelId,
    guildId: sdk.guildId,
  })

  let user: ActivityContext['user'] = null
  let accessToken: string | null = null
  let authError: string | null = null
  let authStage: ActivityContext['authStage'] = 'authorize'

  try {
    // Activity の RPC OAuth2 フローは redirect_uri をパラメータでは受け付けない。
    // Discord Developer Portal の OAuth2 → Redirects に登録した URI を
    // Discord 側が暗黙に使う。登録漏れだと "Missing redirect_uri" が返る。
    dlog('[discord] authorize …')
    const { code } = await sdk.commands.authorize({
      client_id: CLIENT_ID,
      response_type: 'code',
      state: randomState(),
      prompt: 'none',
      scope: ['identify', 'guilds'],
    })
    dlog('[discord] authorize ok, code length =', code?.length)

    authStage = 'token'
    dlog('[discord] POST', TOKEN_ENDPOINT)
    const res = await fetch(TOKEN_ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    })
    dlog('[discord] token endpoint status =', res.status)
    if (!res.ok) {
      const body = await res.text().catch(() => '<no body>')
      throw new Error(`token endpoint ${res.status}: ${body.slice(0, 200)}`)
    }
    // FE-SEC-04: 応答を信頼せず access_token が非空文字列か実行時検証する
    const tokenJson = (await res.json()) as { access_token?: unknown }
    if (typeof tokenJson.access_token !== 'string' || !tokenJson.access_token) {
      throw new Error('token endpoint returned no valid access_token')
    }
    const access_token = tokenJson.access_token
    accessToken = access_token

    authStage = 'authenticate'
    dlog('[discord] sdk.authenticate …')
    const auth = await sdk.commands.authenticate({ access_token })
    const authUser = auth.user as {
      id: string
      username: string
      avatar?: string | null
    }
    user = {
      id: authUser.id,
      username: authUser.username,
      avatarUrl: buildAvatarUrl(authUser.id, authUser.avatar ?? null),
    }
    authStage = 'ok'
    dlog('[discord] authenticate ok, user =', user.username)
  } catch (e) {
    authError = describeError(e)
    console.error(`[discord] auth failed at stage=${authStage}:`, e)
  }

  return {
    instanceId: sdk.instanceId,
    channelId: sdk.channelId,
    guildId: sdk.guildId,
    user,
    accessToken,
    authError,
    authStage,
  }
}

function describeError(e: unknown): string {
  if (e instanceof Error) return e.message
  if (typeof e === 'string') return e
  if (e && typeof e === 'object') {
    // Discord SDK は `{code, message}` のようなプレーンオブジェクトを投げてくる
    const o = e as Record<string, unknown>
    if (typeof o.message === 'string') {
      const code = o.code !== undefined ? ` (code=${String(o.code)})` : ''
      return `${o.message}${code}`
    }
    try {
      return JSON.stringify(e)
    } catch {
      return String(e)
    }
  }
  return String(e)
}
