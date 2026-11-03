import { useCallback, useEffect, useState } from 'react'
import rawDesign from './generated/screens.json'
import { fetchRows, SERVER } from './api'
import { Cell, ScreenPage, statusField, titleField, waiting } from './Screen'
import type { Design, Row, Screen } from './types'

// 앱 틀: 위 모듈 메뉴, 왼쪽 그 모듈의 메뉴, 가운데 화면. 주소의 #화면ID로 화면을 고른다.
const design = rawDesign as unknown as Design
const byId = new Map(design.screens.map((s) => [s.screen_id, s]))
const ALL_ROLES = '전체 보기'
// 영역이 많은 문서는 위 메뉴가 넘치므로 왼쪽 접이식 메뉴로 보여 준다
const MAX_TOP_MENUS = 6
const FOLDED = design.menu.length > MAX_TOP_MENUS

interface PageInfo { name: string; screens: string[]; group: string }
const pages: PageInfo[] = design.menu.flatMap((m) => m.pages.map((p) => ({ ...p, group: m.group })))
const pageOf = new Map(pages.flatMap((p) => p.screens.map((id) => [id, p] as const)))

function useHash(): string {
  const read = () => window.location.hash.slice(1)
  const [hash, setHash] = useState(read)
  useEffect(() => {
    const on = () => setHash(read())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return hash
}

const allows = (s: Screen, role: string) => role === ALL_ROLES || s.roles.length === 0 || s.roles.includes(role)
const tabsOf = (p: PageInfo, role: string) => p.screens.map((id) => byId.get(id)!).filter((s) => allows(s, role))
const pagesOf = (group: string, role: string) => pages.filter((p) => p.group === group && tabsOf(p, role).length > 0)

interface Waiting { screen: Screen; rows: Row[] }

/** 첫 화면: 지금 처리할 일과 공지, 자주 쓰는 메뉴 */
function Home({ role }: { role: string }) {
  const [todo, setTodo] = useState<Waiting[]>([])
  const [notices, setNotices] = useState<{ screen: Screen; rows: Row[] } | null>(null)

  useEffect(() => {
    let live = true
    const candidates = design.screens.filter((s) => allows(s, role) && s.list && statusField(s) && s.actions.some((a) => a.sets))
    // 같은 테이블을 다루는 화면이 여럿이어도 처리할 일은 한 번만 센다
    const targets = [...new Map(candidates.map((s) => [s.table, s])).values()]
    Promise.all(targets.map((s) => fetchRows(s, {}, 0, 100, role)
      .then((d) => ({ screen: s, rows: d.items.filter((r) => waiting(s, r)) }))
      .catch(() => ({ screen: s, rows: [] }))))
      .then((all) => { if (live) setTodo(all.filter((w) => w.rows.length > 0)) })
    const board = design.screens.find((s) => s.list && /공지|게시/.test(s.table_label + s.name))
    if (board) fetchRows(board, {}, 0, 5, role).then((d) => { if (live) setNotices({ screen: board, rows: d.items }) }).catch(() => undefined)
    return () => { live = false }
  }, [role])

  const items = todo.flatMap((w) => w.rows.map((row) => ({ ...w, row }))).slice(0, 8)
  const total = todo.reduce((n, w) => n + w.rows.length, 0)
  return (
    <div className="home">
      <h1>{role === ALL_ROLES ? '오늘의 업무' : `${role}님의 업무`}</h1>
      <dl className="tiles">
        <div><dt>처리 대기</dt><dd className="num">{total}</dd></div>
        {todo.slice(0, 3).map((w) => (
          <div key={w.screen.screen_id}>
            <dt><a href={`#${w.screen.screen_id}`}>{w.screen.table_label} 대기</a></dt>
            <dd className="num">{w.rows.length}</dd>
          </div>
        ))}
      </dl>
      <div className="home-grid">
        <section className="panel">
          <h2>처리할 일</h2>
          {items.length === 0 ? <p className="dim">대기 중인 업무가 없습니다.</p> : (
            <ul className="todo">
              {items.map(({ screen, row }, i) => {
                const title = titleField(screen)
                const status = statusField(screen)
                return (
                  <li key={`${screen.screen_id}-${i}`}>
                    <a href={`#${screen.screen_id}`}>
                      <span className="dim">{screen.table_label}</span>
                      <b>{title ? <Cell field={title} value={row[title.name]} /> : screen.name}</b>
                      {status && <Cell field={status} value={row[status.name]} />}
                    </a>
                  </li>
                )
              })}
            </ul>
          )}
        </section>
        <div className="home-side">
          {notices && (
            <section className="panel">
              <h2><a href={`#${notices.screen.screen_id}`}>공지사항</a></h2>
              <ul className="notices">
                {notices.rows.map((row, i) => {
                  const title = titleField(notices.screen)
                  return <li key={i}>{title ? <Cell field={title} value={row[title.name]} /> : '-'}</li>
                })}
              </ul>
            </section>
          )}
          <section className="panel">
            <h2>바로가기</h2>
            <ul className="quick">
              {design.menu.flatMap((m) => pagesOf(m.group, role).slice(0, 3)).slice(0, 12).map((p) => (
                <li key={p.screens[0]}><a href={`#${tabsOf(p, role)[0].screen_id}`}>{p.name}</a></li>
              ))}
            </ul>
          </section>
        </div>
      </div>
    </div>
  )
}

export function App() {
  const hash = useHash()
  const [role, setRole] = useState(ALL_ROLES)
  const [review, setReview] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [toast, setToast] = useState('')
  const screen = byId.get(hash)
  const page = screen ? pageOf.get(screen.screen_id) : undefined
  const module = design.menu.find((m) => m.group === page?.group)

  useEffect(() => { document.title = design.title }, [])
  useEffect(() => { setMenuOpen(false); window.scrollTo(0, 0) }, [hash])
  useEffect(() => {
    if (!toast) return
    const t = window.setTimeout(() => setToast(''), 2600)
    return () => window.clearTimeout(t)
  }, [toast])
  const notify = useCallback((text: string) => setToast(text), [])

  return (
    <div className="shell">
      <header className="top">
        <a className="brand" href="#home">{design.title}</a>
        {!FOLDED && (
          <nav className="modules" aria-label="업무 모듈">
            {design.menu.map((m) => {
              const first = pagesOf(m.group, role)[0]
              return first && (
                <a key={m.group} className={m.group === module?.group ? 'on' : ''} title={m.name} href={`#${tabsOf(first, role)[0].screen_id}`}>{m.short}</a>
              )
            })}
          </nav>
        )}
        <div className="top-right">
          {!SERVER && <span className="tag">예시 데이터</span>}
          <button type="button" className={`toggle ${review ? 'on' : ''}`} aria-pressed={review} onClick={() => setReview(!review)}>검토 모드</button>
          <label className="role" htmlFor="role-select">
            <span>역할</span>
            <select id="role-select" value={role} onChange={(e) => setRole(e.target.value)}>
              <option>{ALL_ROLES}</option>
              {design.roles.map((r) => <option key={r.name} value={r.name}>{r.name}</option>)}
            </select>
          </label>
        </div>
      </header>
      <div className={`body ${module || FOLDED ? '' : 'no-side'}`}>
        {(module || FOLDED) && (
          <nav className={`side ${menuOpen ? 'open' : ''}`} aria-label="메뉴">
            <button type="button" className="menu-btn ghost" aria-expanded={menuOpen} onClick={() => setMenuOpen(!menuOpen)}>메뉴</button>
            {FOLDED ? design.menu.map((m) => pagesOf(m.group, role).length > 0 && (
              <details key={m.group} open={m.group === module?.group}>
                <summary>{m.name}</summary>
                {pagesOf(m.group, role).map((p) => (
                  <a key={p.screens[0]} className={p === page ? 'on' : ''} href={`#${tabsOf(p, role)[0].screen_id}`}>{p.name}</a>
                ))}
              </details>
            )) : module && (
              <>
                <p className="side-title">{module.name}</p>
                {pagesOf(module.group, role).map((p) => (
                  <a key={p.screens[0]} className={p === page ? 'on' : ''} href={`#${tabsOf(p, role)[0].screen_id}`}>{p.name}</a>
                ))}
              </>
            )}
          </nav>
        )}
        <main className="main">
          {screen && page ? (
            <>
              <p className="crumb"><a href="#home">홈</a> <span aria-hidden="true">›</span> {module?.short} <span aria-hidden="true">›</span> {page.name}</p>
              <h1>{page.name}</h1>
              {page.screens.length > 1 && (
                <nav className="tabs" aria-label="화면">
                  {tabsOf(page, role).map((s) => <a key={s.screen_id} className={s === screen ? 'on' : ''} href={`#${s.screen_id}`}>{s.name}</a>)}
                </nav>
              )}
              {review && (
                <p className="chips">
                  <span className="chip">{screen.screen_id}</span>
                  <span className="chip">{screen.pattern_label}</span>
                  {screen.roles.map((r) => <span key={r} className="chip">{r}</span>)}
                </p>
              )}
              {allows(screen, role)
                ? <ScreenPage key={screen.screen_id} screen={screen} role={role} notify={notify} review={review} />
                : <p className="error">'{role}' 역할은 이 화면을 쓸 수 없습니다.</p>}
            </>
          ) : <Home role={role} />}
        </main>
      </div>
      {toast && <div className="toast" role="status">{toast}</div>}
    </div>
  )
}
