import { mockDelete, mockList, mockRow, mockSave, mockSet, refOptions } from './mock'
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

async function send(method: string, target: string, role: string, body?: Row): Promise<Row> {
  const { __pk: _pk, ...fields } = body ?? {}
  const res = await fetch(target, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-User-Id': '1', 'X-User-Role': encodeURIComponent(role) },   // 시연용 사용자 (TASK-003에서 실제 인증으로)
    body: body ? JSON.stringify(fields) : undefined,
  })
  const data = res.status === 204 ? {} : await res.json()
  if (!res.ok) throw new Error((data as { message?: string }).message ?? `요청을 처리하지 못했습니다 (${res.status})`)
  return data as Row
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
