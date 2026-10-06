/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_DISCORD_CLIENT_ID: string
  readonly VITE_TOKEN_ENDPOINT?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
