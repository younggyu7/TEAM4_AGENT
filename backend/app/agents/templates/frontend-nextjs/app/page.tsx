'use client'
// 화면은 React 틀과 같은 부품을 ui/에 둔다 (Next.js는 src/pages를 라우트로 보므로 src/ 대신 ui/). 로그인·세션이 브라우저 저장소를 쓰므로 서버 렌더링 없이 브라우저에서만 그린다.
import dynamic from 'next/dynamic'

const App = dynamic(() => import('../ui/App').then((m) => m.App), { ssr: false })

export default function Page() {
  return <App />
}
