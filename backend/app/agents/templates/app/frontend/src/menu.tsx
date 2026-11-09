import type { ComponentType } from 'react'
import { ScreenPage } from './Screen'
import type { PageProps } from './kit'
import type { Design, Screen } from './types'

// 메뉴 화면의 공통 부분: 화면 구성(generated/layout.json) 적용, 같은 업무 대상 화면 합치기, 기본 메뉴 화면.
// 메뉴마다 menus/<메뉴>.tsx가 이 기본 화면을 쓰고, 화면 작업을 맡은 사람은 그 파일을 고쳐 메뉴 화면을 바꾼다.

export interface MenuPageInfo { name: string; screens: string[] }
export interface MenuInfo { group: string; name: string; short: string; pages: MenuPageInfo[] }
export interface SectionInfo { id: string; name: string; menus: MenuInfo[] }
export interface Layout {
  home?: { todo?: boolean; notices?: boolean; areas?: boolean }
  /** 업무 영역: 비슷한 메뉴를 묶은 대메뉴 (왼쪽 메뉴 영역 → 메뉴, 홈 영역 카드). 메뉴 순서도 이 순서를 따른다 */
  sections?: { id: string; name: string; menus: string[] }[]
  menu?: { group: string; name?: string; hidden?: boolean; pages?: { name?: string; screens: string[]; hidden?: boolean }[] }[]
}

/** 화면 구성을 메뉴에 적용한다: 순서·이름·숨김. 구성에 없는 메뉴·화면(스펙이 바뀌어 새로 생긴 것)은 원래 자리 끝에 붙인다 */
export function applyLayout(menu: Design['menu'], layout: Layout): MenuInfo[] {
  const byGroup = new Map(menu.map((m) => [m.group, m]))
  const placed = new Set<string>()
  const out: MenuInfo[] = []
  for (const item of layout.menu ?? []) {
    const base = byGroup.get(item.group)
    if (!base) continue
    placed.add(item.group)
    if (item.hidden) continue
    const pages = (item.pages ?? base.pages.map((p) => ({ name: p.name, screens: p.screens, hidden: false })))
      .filter((p) => !p.hidden)
      .map((p) => ({ name: p.name || base.pages.find((b) => b.screens.includes(p.screens[0]))?.name || '업무', screens: p.screens }))
    const known = new Set((item.pages ?? []).flatMap((p) => p.screens))
    const fresh = item.pages ? base.pages.filter((p) => !p.screens.some((id) => known.has(id))) : []
    out.push({ group: base.group, name: item.name || base.name, short: item.name || base.short, pages: [...pages, ...fresh] })
  }
  for (const m of menu) if (!placed.has(m.group)) out.push(m)
  return out.filter((m) => m.pages.length > 0)
}

/** 업무 영역을 메뉴에 적용한다. 영역이 없는 예전 화면 구성이면 빈 목록(메뉴를 한 줄로 보인다). 어느 영역에도 없는 메뉴는 '기타'로 */
export function applySections(menus: MenuInfo[], layout: Layout): SectionInfo[] {
  if (!layout.sections?.length) return []
  const byGroup = new Map(menus.map((m) => [m.group, m]))
  const out = layout.sections.map((s) => ({ id: s.id, name: s.name, menus: s.menus.map((g) => byGroup.get(g)).filter((m): m is MenuInfo => Boolean(m)) }))
  const placed = new Set(layout.sections.flatMap((s) => s.menus))
  const rest = menus.filter((m) => !placed.has(m.group))
  if (rest.length) out.push({ id: '_rest', name: '기타', menus: rest })
  return out.filter((s) => s.menus.length > 0)
}

/** 같은 업무 대상(테이블)의 화면들을 하나로: 첫 목록 화면에 다른 화면의 등록·수정·삭제·처리 버튼을 모은다 */
export function mergeScreens(name: string, screens: Screen[]): Screen {
  const lead = screens.find((s) => s.list) ?? screens.find((s) => s.report) ?? screens[0]
  const pick = <K extends 'create' | 'update' | 'delete'>(key: K) => (lead[key] ?? screens.find((s) => s[key])?.[key] ?? null) as Screen[K]
  const actions = [...new Map(screens.flatMap((s) => s.actions).map((a) => [a.operation_id, a])).values()]
  const roles = screens.some((s) => s.roles.length === 0) ? [] : [...new Set(screens.flatMap((s) => s.roles))]
  return {
    ...lead, name, roles, actions, create: pick('create'), update: pick('update'), delete: pick('delete'),
    rules: screens.flatMap((s) => s.rules), ac_count: screens.reduce((n, s) => n + s.ac_count, 0),
  }
}

export interface MenuProps extends PageProps {
  menu: MenuInfo
  page: MenuPageInfo
  screen: Screen               // 주소가 가리키는 화면 (기능 하나)
  screens: Screen[]            // 이 업무 묶음의 화면 (역할이 볼 수 있는 것)
  review: boolean              // 검토 모드: 기능별 화면을 따로 본다
  built: Record<string, ComponentType<PageProps>>   // 개발 Agent가 기능 전체를 구현한 화면 (기능 ID → 화면)
}

/** 기본 메뉴 화면: 같은 업무 대상 화면을 한 화면으로. 검토 모드이거나 AI가 구현한 화면이 있으면 기능별 탭 */
export function DefaultMenuPage({ page, screen, screens, role, notify, review, built }: MenuProps) {
  const separate = review || screens.length === 1 || screens.some((s) => built[s.function_id])
  if (separate) {
    const Built = built[screen.function_id]
    return (
      <>
        {screens.length > 1 && (
          <nav className="tabs" aria-label="화면">
            {screens.map((s) => <a key={s.screen_id} className={s === screen ? 'on' : ''} href={`#${s.screen_id}`}>{s.name}</a>)}
          </nav>
        )}
        {Built
          ? <Built key={screen.screen_id} role={role} notify={notify} />
          : <ScreenPage key={screen.screen_id} screen={screen} role={role} notify={notify} review={review} />}
      </>
    )
  }
  const merged = mergeScreens(page.name, screens)
  return <ScreenPage key={page.screens.join(',')} screen={merged} role={role} notify={notify} review={review} />
}
