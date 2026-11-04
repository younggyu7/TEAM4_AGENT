import type { ReactNode } from 'react'
import '../ui/styles.css'
import '../ui/theme.css'

export const metadata = { title: '서비스 화면' }

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="ko">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;500;600;700&display=swap" />
      </head>
      <body>{children}</body>
    </html>
  )
}
