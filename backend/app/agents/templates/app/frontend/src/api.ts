import { mockDelete, mockList, mockRow, mockSave, mockSet, refOptions } from './mock'
import { authHeaders, expired } from './session'
import type { Action, FormView, Option, Page, Row, Screen } from './types'

// 화면이 쓰는 API 한곳. 기본은 예시 데이터(mock.ts), VITE_API=server면 백엔드를 부른다.
export const SERVER = import.meta.env.VITE_API === 'server'
const BASE = '/api/v1'

function url(path: string, values: Row, query?: Row): string {
  const filled = path.replace(/\{(\w+)\}/g, (_, k: string) => encodeURIComponent(String(values[k] ?? values.__pk ?? '')))
  const pairs = Object.entries(query ?? {}).filter(([, v]) => v !== '' && v !== undefined).map(([k, v]) => [k, String(v)])
  const search = new URLSearchParams(pairs).toString()
  return BASE + filled + (search ? `?${search}` : '')
}

// role은 화면이 보이는 역할이다. 서버는 로그인 토큰으로 사용자와 역할을 정하고 이 값을 믿지 않는다.
async function send(method: string, target: string, _role: string, body?: Row): Promise<Row> {
  const { __pk: _pk, ...fields } = body ?? {}
  const res = await fetch(target, { method, headers: authHeaders(), body: body ? JSON.stringify(fields) : undefined })
  const data = res.status === 204 ? {} : await res.json().catch(() => ({}))
  if (!res.ok) {
    expired(res.status)
    throw new Error((data as { message?: string }).message ?? `요청을 처리하지 못했습니다 (${res.status})`)
  }
  return data as Row
}

/** 포털 로그인 (서버 모드) */
export async function login(loginId: string, password: string): Promise<{ token: string; loginId: string; role: string; userId: string }> {
  const res = await fetch('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ loginId, password }) })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error((data as { message?: string }).message ?? '로그인하지 못했습니다.')
  return data as { token: string; loginId: string; role: string; userId: string }
}

/** 저장된 토큰이 아직 쓸 수 있는지 (서버를 다시 시작하면 다시 로그인한다) */
export async function checkLogin(): Promise<boolean> {
  const res = await fetch('/api/auth/me', { headers: authHeaders() }).catch(() => null)
  return !!res && res.ok
}

export async function logout(): Promise<void> {
  if (SERVER) await fetch('/api/auth/logout', { method: 'POST', headers: authHeaders() }).catch(() => undefined)
}

/** 경로에 필요한데 아직 고르지 않은 조회 조건 (서버는 값이 없으면 호출할 수 없다) */
export function missingParams(s: Screen, query: Row): string[] {
  const view = s.list ?? s.report
  if (!SERVER || !view) return []
  const names = [...view.path.matchAll(/\{(\w+)\}/g)].map((m) => m[1])
  return view.filters.filter((f) => names.includes(f.name) && (query[f.name] === undefined || query[f.name] === '')).map((f) => f.label)
}

export async function fetchRows(s: Screen, query: Row, page: number, size: number, role: string): Promise<Page> {
  const view = s.list ?? s.report
  if (!view || missingParams(s, query).length) return { items: [], page, size, total: 0 }
  if (!SERVER) return mockList(s.table, view.columns, view.filters, query, page, size)
  const data = await send('GET', url(view.path, query, { ...query, page, size }), role)
  if (Array.isArray(data)) return { items: data as Row[], page: 0, size: data.length, total: data.length }   // 업무 로직이 목록을 그대로 돌려준 경우
  if (!s.list) return { items: [data], page: 0, size: 1, total: 1 }
  const got = data as unknown as Page
  return { ...got, items: got.items ?? [] }
}

/** 다른 테이블의 선택 목록 (ID 대신 이름을 보여 줄 때도 쓴다) */
export async function fetchLookup(table: string, role: string): Promise<Option[]> {
  if (!SERVER) return refOptions(table)
  return (await send('GET', `/api/ui/lookups/${encodeURIComponent(table)}`, role)) as unknown as Option[]
}

/** 수정 화면의 처음 값: 목록에 없는 칸까지 채운다 */
export async function loadRow(s: Screen, form: FormView, row: Row, role: string): Promise<Row> {
  if (!SERVER) return { ...row, ...mockRow(s.table, form.fields, row.__pk) }
  return { ...row, ...(await send('GET', `/api/ui/rows/${s.screen_id}/${encodeURIComponent(String(row.__pk))}`, role)) }
}

export async function saveRow(s: Screen, form: FormView, body: Row, row: Row | null, role: string): Promise<void> {
  if (!SERVER) return mockSave(s.table, form.fields, body, row?.__pk)
  await send(form.method, url(form.path, { ...row, ...body }), role, body)
}

export async function deleteRow(s: Screen, row: Row, role: string): Promise<void> {
  if (!SERVER) return mockDelete(s.table, row.__pk)
  if (s.delete) await send('DELETE', url(s.delete.path, row), role)
}

export async function runAction(s: Screen, action: Action, row: Row | null, body: Row, role: string): Promise<void> {
  if (!SERVER) {
    if (action.sets) mockSet(s.table, action.sets.column, action.sets.value, row?.__pk)
    const first = action.fields[0]
    const target = first?.column.split('.')[0]
    if (action.kind === 'add' && target) mockSave(target, action.fields, body)
    if (action.kind === 'update' && target) mockSave(target, action.fields, body, body[first.name])
    if (action.kind === 'delete' && target) mockDelete(target, body[first.name])
    return
  }
  await send(action.method, url(action.path, { ...row, ...body }), role, body)
}
