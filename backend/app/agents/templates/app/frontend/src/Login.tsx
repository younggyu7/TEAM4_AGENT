import { useState } from 'react'
import type { FormEvent } from 'react'
import { login, SERVER } from './api'
import { setSession } from './session'
import type { Session } from './session'
import type { Design } from './types'

// 로그인 화면: 왼쪽은 회사 이름과 업무 영역, 오른쪽은 로그인. 예시 데이터 모드는 서버가 없으니 역할만 고른다.
export function Login({ design, brand, notice, onDone }: { design: Design; brand: string; notice: string; onDone: (s: Session) => void }) {
  const [loginId, setLoginId] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [sending, setSending] = useState(false)
  const areas = design.menu.map((m) => m.short)
  const MAX_AREAS = 4   // 영역이 많은 문서는 앞쪽만 보이고 개수로 줄인다
  const intro = areas.length > MAX_AREAS ? `${areas.slice(0, MAX_AREAS).join(', ')} 등 ${areas.length}개 영역의` : areas.join(', ')

  const done = (s: Session) => {
    setSession(s)
    onDone(s)
  }
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setSending(true)
    setError('')
    try {
      done(await login(loginId.trim(), password))
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setSending(false)
    }
  }

  return (
    <main className="login-page">
      <section className="cover">
        <div className="brand"><b aria-hidden="true">{brand.slice(0, 1)}</b><span>{brand}<small>업무 포털</small></span></div>
        <div className="cover-copy">
          <span className="eyebrow">{design.company ? design.title : '통합 업무 포털'}</span>
          <h1>흩어진 업무를<br />한곳에서<span>.</span></h1>
          {areas.length > 0 && <p>{intro} 업무를 한 화면에서 처리하세요.</p>}
        </div>
        <svg aria-hidden="true" viewBox="0 0 540 170" fill="none">
          <g stroke="currentColor" strokeWidth="1.5">
            {[0, 11, 22, 33, 44].map((d) => <path key={d} d={`M-30 ${145 - d} 110 ${55 - d}l155 99L435 ${44 - d}l130 84`} />)}
          </g>
        </svg>
      </section>
      <section className="login-box">
        {SERVER ? (
          <>
            <span className="eyebrow">로그인</span>
            <h2>업무 포털 로그인</h2>
            <p className="dim">부여받은 계정으로 접속하세요.</p>
            {notice && <p className="notice" role="status">{notice}</p>}
            <form onSubmit={submit}>
              <label>아이디<input value={loginId} onChange={(e) => setLoginId(e.target.value)} required autoComplete="username" autoCapitalize="none" spellCheck={false} placeholder="계정 아이디" /></label>
              <label>비밀번호<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete="current-password" placeholder="비밀번호 입력" /></label>
              {error && <p className="error" role="alert">{error}</p>}
              <button type="submit" className="submit" disabled={sending}>{sending ? '확인하는 중' : '로그인'} <span aria-hidden="true">→</span></button>
            </form>
            <details className="accounts">
              <summary>시연 계정 보기</summary>
              <table>
                <thead><tr><th>아이디</th><th>역할</th></tr></thead>
                <tbody>{design.accounts.map((a) => <tr key={a.loginId}><td className="num">{a.loginId}</td><td>{a.name}</td></tr>)}</tbody>
              </table>
              <p className="dim">비밀번호는 서비스 폴더의 <code>local/accounts.txt</code>에 있습니다 (처음 실행할 때 만들어집니다).</p>
            </details>
          </>
        ) : (
          <>
            <span className="eyebrow">예시 데이터</span>
            <h2>화면 둘러보기</h2>
            <p className="dim">서버 없이 예시 데이터로 화면을 봅니다. 들어갈 역할을 고르세요.</p>
            <div className="role-list">
              {design.accounts.map((a) => (
                <button key={a.loginId} type="button" className="ghost" onClick={() => done({ token: '', loginId: a.loginId, role: a.role, userId: a.userId })}>
                  <b>{a.name}</b><span aria-hidden="true">→</span>
                </button>
              ))}
            </div>
          </>
        )}
      </section>
    </main>
  )
}
