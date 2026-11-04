import { createRoot } from 'react-dom/client'
import { App } from './App'
import './styles.css'
import './theme.css'   // 고른 화면 스타일 (styles.css의 색 토큰만 바꾼다)

createRoot(document.getElementById('root')!).render(<App />)
