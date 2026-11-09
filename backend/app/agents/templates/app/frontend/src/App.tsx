import { useCallback, useEffect, useState } from 'react'
import rawDesign from './generated/screens.json'
import rawLayout from './generated/layout.json'
import { checkLogin, fetchRows, logout, SERVER } from './api'
import { Login } from './Login'
import { applyLayout, applySections, DefaultMenuPage } from './menu'
import type { Layout, MenuInfo } from './menu'
import { menus } from './menus'
import { Cell, statusField, titleField, waiting } from './Screen'
import { pages as built } from './pages'
import { getSession, setSession } from './session'
import type { Session } from './session'
import type { Design, Row, Screen } from './types'

// 앱 틀: 로그인 → 왼쪽 메뉴(업무 홈 · 업무 영역(접고 펼침) → 메뉴 → 그 메뉴의 업무) + 가운데 화면.
// 주소: #home 업무 홈, #m-<메뉴> 메뉴의 업무 카드, #<화면ID> 화면.
// 업무 영역(비슷한 메뉴 묶음)·메뉴 이름·숨김과 홈 칸은 generated/layout.json(화면 구성)을 따르고, 메뉴 화면은 menus/<메뉴>.tsx가 그린다.
const design = rawDesign as unknown as Design
const layout = rawLayout as Layout
const MENU = applyLayout(design.menu, layout)
const SECTIONS = applySections(MENU, layout)
const sectionOf = new Map(SECTIONS.flatMap((s) => s.menus.map((m) => [m.group, s] as const)))
const BRAND = design.company || design.title
const byId = new Map(design.screens.map((s) => [s.screen_id, s]))
const ALL_ROLES = '전체 보기'

interface PageInfo { name: string; screens: string[]; group: string }
const pages: PageInfo[] = MENU.flatMap((m) => m.pages.map((p) => ({ ...p, group: m.group })))
const pageOf = new Map(pages.flatMap((p) => p.screens.map((id) => [id, p] as const)))

function useHash(): string {
  const read = () => decodeURIComponent(window.location.hash.slice(1))
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
const pageLink = (p: PageInfo, role: string) => `#${tabsOf(p, role)[0].screen_id}`
const today = () => new Intl.DateTimeFormat('ko-KR', { dateStyle: 'long' }).format(new Date())

/** 업무 카드: 영역 / 번호, 업무 이름, 설명 */
function Cards({ group, role }: { group: string; role: string }) {
  const module = MENU.find((m) => m.group === group)!
  return (
    <div className="cards">
      {pagesOf(group, role).map((p, i) => {
        const first = tabsOf(p, role)[0]
        return (
          <a key={p.screens[0]} className="card" href={pageLink(p, role)}>
            <small>{module.short} / {String(i + 1).padStart(2, '0')}</small>
            <strong>{p.name}</strong>
            {first.description && <span className="card-note">{first.description}</span>}
            <span className="card-go">업무 열기 <span aria-hidden="true">→</span></span>
          </a>
        )
      })}
    </div>
  )
}

interface Waiting { screen: Screen; rows: Row[] }

/** 첫 화면: 지금 처리할 일과 공지, 업무 메뉴 */
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
    const board = design.screens.find((s) => allows(s, role) && s.list && /공지|게시/.test(s.table_label + s.name))
    if (board) fetchRows(board, {}, 0, 5, role).then((d) => { if (live) setNotices({ screen: board, rows: d.items }) }).catch(() => undefined)
    return () => { live = false }
  }, [role])

  const items = todo.flatMap((w) => w.rows.map((row) => ({ ...w, row }))).slice(0, 8)
  const total = todo.reduce((n, w) => n + w.rows.length, 0)
  const modules = MENU.filter((m) => pagesOf(m.group, role).length > 0)
  const count = modules.reduce((n, m) => n + pagesOf(m.group, role).length, 0)
  const show = { todo: layout.home?.todo !== false, notices: layout.home?.notices !== false, areas: layout.home?.areas !== false }
  return (
    <div className="home">
      <div className="hero">
        <span className="eyebrow">업무 홈</span>
        <h1>{role === ALL_ROLES ? '오늘의 업무' : `${role}님의 업무`}</h1>
        <p>{BRAND}에서 쓸 수 있는 업무 {count}개입니다.</p>
      </div>
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
        {show.todo && <section className="panel">
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
        </section>}
        {show.notices && notices && (
          <section className="panel">
            <h2><a href={`#${notices.screen.screen_id}`}>공지사항</a></h2>
            {notices.rows.length === 0 ? <p className="dim">공지가 없습니다.</p> : (
              <ul className="notices">
                {notices.rows.map((row, i) => {
                  const title = titleField(notices.screen)
                  return <li key={i}>{title ? <Cell field={title} value={row[title.name]} /> : '-'}</li>
                })}
              </ul>
            )}
          </section>
        )}
      </div>
      {show.areas && modules.length > 0 && SECTIONS.length > 0 && (
        <section className="panel areas">
          <h2>업무 영역</h2>
          <ul className="area-cards">
            {SECTIONS.map((s) => ({ ...s, menus: s.menus.filter((m) => pagesOf(m.group, role).length > 0) })).filter((s) => s.menus.length > 0).map((s) => (
              <li key={s.id}>
                <h3>{s.name}<span className="dim num">메뉴 {s.menus.length} · 업무 {s.menus.reduce((n, m) => n + pagesOf(m.group, role).length, 0)}</span></h3>
                <ul>
                  {s.menus.map((m) => (
                    <li key={m.group}><a href={`#m-${encodeURIComponent(m.group)}`}>{m.name}<span className="dim num">{pagesOf(m.group, role).length}</span></a></li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </section>
      )}
      {show.areas && modules.length > 0 && SECTIONS.length === 0 && (
        <section className="panel areas">
          <h2>업무 영역</h2>
          <ul className="area-list">
            {modules.map((m) => (
              <li key={m.group}>
                <a href={`#m-${encodeURIComponent(m.group)}`}><b>{m.name}</b><span className="dim num">업무 {pagesOf(m.group, role).length}</span></a>
              </li>
            ))}
          </ul>
        </section>
      )}
      {count === 0 && <p className="dim">아직 쓸 수 있는 업무가 없습니다.</p>}
    </div>
  )
}

export function App() {
  const hash = useHash()
  const [session, setCurrent] = useState<Session | null>(getSession)
  const [notice, setNotice] = useState('')
  const [review, setReview] = useState(false)
  const [toast, setToast] = useState('')
  const [opened, setOpened] = useState<Set<string>>(new Set())   // 사람이 펼친 업무 영역 (지금 보는 메뉴의 영역은 늘 펼침)
  const role = session?.role ?? ''
  const screen = byId.get(hash)
  const page = screen ? pageOf.get(screen.screen_id) : undefined
  const area = hash.startsWith('m-') ? hash.slice(2) : page?.group
  const module = MENU.find((m) => m.group === area)
  const MenuView = (module && menus[module.group]) || DefaultMenuPage   // 메뉴 화면 (menus/<메뉴>.tsx)

  useEffect(() => { document.title = design.company ? `${design.company} 업무 포털` : design.title }, [])
  useEffect(() => { window.scrollTo(0, 0) }, [hash])
  useEffect(() => {
    const out = () => { setCurrent(null); setNotice('로그인이 끝났습니다. 다시 로그인하세요.') }
    window.addEventListener('session-expired', out)
    return () => window.removeEventListener('session-expired', out)
  }, [])
  useEffect(() => {
    if (SERVER && session?.token) checkLogin().then((ok) => { if (!ok) { setSession(null); setCurrent(null) } })
  }, [session?.token])
  useEffect(() => {
    if (!toast) return
    const t = window.setTimeout(() => setToast(''), 2600)
    return () => window.clearTimeout(t)
  }, [toast])
  const notify = useCallback((text: string) => setToast(text), [])
  const signOut = async () => {
    await logout()
    setSession(null)
    setCurrent(null)
    setNotice('')
    window.location.hash = 'home'
  }

  if (!session) return <Login design={design} brand={BRAND} notice={notice} onDone={(s) => { setCurrent(s); setNotice('') }} />

  const modules = MENU.filter((m) => pagesOf(m.group, role).length > 0)
  const section = module ? sectionOf.get(module.group) : undefined
  const menuLink = (m: MenuInfo) => (
    <div key={m.group}>
      <a className={m === module ? 'on' : ''} href={`#m-${encodeURIComponent(m.group)}`} title={m.name}>{m.name}</a>
      {m === module && (
        <div className="sub">
          {pagesOf(m.group, role).map((p) => <a key={p.screens[0]} className={p === page ? 'on' : ''} href={pageLink(p, role)}>{p.name}</a>)}
        </div>
      )}
    </div>
  )
  return (
    <div className="app">
      <aside className="side">
        <a className="brand" href="#home"><b aria-hidden="true">{BRAND.slice(0, 1)}</b><span>{BRAND}<small>업무 포털</small></span></a>
        <nav aria-label="메뉴">
          <a className={!module ? 'on' : ''} href="#home">업무 홈</a>
          <p className="side-cap">업무 영역</p>
          {SECTIONS.length === 0 ? modules.map(menuLink) : SECTIONS.map((s) => {
            const shown = s.menus.filter((m) => modules.includes(m))
            if (shown.length === 0) return null
            if (shown.length === 1) return menuLink(shown[0])   // 메뉴가 하나뿐인 영역은 메뉴를 바로 보인다
            const isOpen = opened.has(s.id) || s === section
            return (
              <div key={s.id} className="side-group">
                <button type="button" className={s === section ? 'on' : ''} aria-expanded={isOpen}
                  onClick={() => setOpened((o) => { const n = new Set(o); if (n.has(s.id)) n.delete(s.id); else n.add(s.id); return n })}>
                  <span>{s.name}</span><small className="num">{shown.length}</small><i aria-hidden="true">{isOpen ? '−' : '+'}</i>
                </button>
                {isOpen && <div className="side-menus">{shown.map(menuLink)}</div>}
              </div>
            )
          })}
        </nav>
        <footer>
          <strong>{role}</strong>
          <span className="num">{session.loginId}</span>
          <button type="button" className="link" onClick={signOut}>로그아웃 <span aria-hidden="true">↗</span></button>
        </footer>
      </aside>
      <section className="content">
        <header className="top">
          <span className="crumb">
            <a href="#home">업무 공간</a>
            {section && section.menus.length > 1 && <><i aria-hidden="true">/</i><span>{section.name}</span></>}
            {module && <><i aria-hidden="true">/</i><a href={`#m-${encodeURIComponent(module.group)}`}>{module.short}</a></>}
            {page && <><i aria-hidden="true">/</i><strong>{page.name}</strong></>}
          </span>
          <span className="top-right">
            {!SERVER && <span className="tag">예시 데이터</span>}
            <button type="button" className={`toggle ${review ? 'on' : ''}`} aria-pressed={review} onClick={() => setReview(!review)}>검토 모드</button>
            <time>{today()}</time>
          </span>
        </header>
        {screen && page ? (
          <>
            <div className="hero">
              <span className="eyebrow">{module?.name}</span>
              <h1>{page.name}</h1>
              {screen.description && <p>{screen.description}</p>}
            </div>
            {review && (
              <p className="chips">
                <span className="chip">{screen.screen_id}</span>
                <span className="chip">{screen.pattern_label}</span>
                {built[screen.function_id] && <span className="chip">LLM 구현</span>}
                {screen.roles.map((r) => <span key={r} className="chip">{r}</span>)}
              </p>
            )}
            {!allows(screen, role)
              ? <p className="error">'{role}' 역할은 이 화면을 쓸 수 없습니다.</p>
              : <MenuView menu={module!} page={page} screen={screen} screens={tabsOf(page, role)} role={role} notify={notify} review={review} built={built} />}
          </>
        ) : module ? (
          <div className="home">
            <div className="hero">
              <span className="eyebrow">{section && section.menus.length > 1 ? section.name : '업무 영역'}</span>
              <h1>{module.name}</h1>
              <p>업무 {pagesOf(module.group, role).length}개. 업무를 골라 조회하거나 처리하세요.</p>
            </div>
            <Cards group={module.group} role={role} />
          </div>
        ) : <Home role={role} />}
      </section>
      {toast && <div className="toast" role="status">{toast}</div>}
    </div>
  )
}
