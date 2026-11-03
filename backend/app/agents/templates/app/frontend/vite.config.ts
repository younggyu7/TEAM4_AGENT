import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 기본은 예시 데이터로 화면만 띄운다 (npm run dev). 서버에 붙일 때만 VITE_API=server (백엔드 8080).
export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': 'http://localhost:8080' } },
})
