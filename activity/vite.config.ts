import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// cloudflared などの HTTPS トンネル越しに Discord クライアントから読み込まれる想定のため、
// allowedHosts を開放し、HMR を wss/443 に向けている。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
    allowedHosts: true,
    hmr: {
      clientPort: 443,
    },
  },
  envPrefix: 'VITE_',
})
