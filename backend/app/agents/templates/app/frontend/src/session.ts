// 로그인 세션 (이 탭에만 둔다). 서버 모드는 포털 로그인(/api/auth)이 준 토큰, 예시 데이터 모드는 고른 역할만 둔다.
export interface Session { token: string; loginId: string; role: string; userId: string }

const KEY = 'app.session'
let current: Session | null = read()

function read(): Session | null {
  try {
    const raw = window.sessionStorage.getItem(KEY)
    return raw ? (JSON.parse(raw) as Session) : null
  } catch {
    return null
  }
}

export function getSession(): Session | null {
  return current
}

export function setSession(next: Session | null): void {
  current = next
  try {
    if (next) window.sessionStorage.setItem(KEY, JSON.stringify(next))
    else window.sessionStorage.removeItem(KEY)
  } catch {
    // 저장소를 쓸 수 없으면 이 화면에서만 유지한다
  }
}

/** 서버로 보낼 헤더 (로그인 토큰) */
export function authHeaders(): Record<string, string> {
  return current?.token ? { 'Content-Type': 'application/json', Authorization: `Bearer ${current.token}` } : { 'Content-Type': 'application/json' }
}

/** 서버가 로그인이 끝났다고 하면(401) 앱이 로그인 화면으로 돌아가게 알린다 */
export function expired(status: number): void {
  if (status === 401 && current?.token) {
    setSession(null)
    window.dispatchEvent(new Event('session-expired'))
  }
}
