import rawData from './generated/mock-data.json'
import type { Field, Page, Row } from './types'

// 서버 없이 화면을 보여 줄 때 쓰는 브라우저 안의 가짜 API. 예시 행은 코드가 만든 값이고, 새로고침하면 처음으로 돌아간다.
type Tables = Record<string, Row[]>
const data = rawData as unknown as { keys: Record<string, string>; labels: Record<string, string>; rows: Tables }
const tables: Tables = JSON.parse(JSON.stringify(data.rows))

const tableOf = (f: Field) => f.column.split('.')[0]
const columnOf = (f: Field) => f.column.split('.')[1]
const pkOf = (table: string) => data.keys[table] ?? `${table}_id`
const idOf = (table: string, row: Row) => row[pkOf(table)] ?? Object.values(row)[0]
const isRow = (table: string, row: Row, id: unknown) => String(idOf(table, row)) === String(id)   // 선택 목록의 값은 글자다

function read(row: Row, main: string, f: Field): unknown {
  if (!f.column) {   // 컬럼 없이 계산하는 값 (통계·검색 결과)
    const n = Number(idOf(main, row)) || 1
    return f.type === 'number' ? n * 12 : f.type === 'boolean' ? n % 2 === 1 : `${f.label} ${n}`
  }
  const table = tableOf(f)
  if (table === main) return row[columnOf(f)]
  // 다른 테이블 컬럼: 이 행의 외래키로 찾고, 없으면 같은 순번의 행을 보여 준다
  const other = tables[table] ?? []
  const id = row[pkOf(table)]
  const hit = other.find((r) => r[pkOf(table)] === id) ?? other[(Number(idOf(main, row)) || 0) % (other.length || 1)]
  return hit?.[columnOf(f)]
}

const toApi = (row: Row, main: string, fields: Field[]): Row =>
  Object.fromEntries(fields.map((f) => [f.name, read(row, main, f)]))

/** 선택 목록: 행의 기본키와 알아볼 이름 (서버의 /api/ui/lookups와 같은 모양) */
export function refOptions(table: string): { value: string; label: string }[] {
  const column = data.labels[table]
  return (tables[table] ?? []).map((r) => ({ value: String(idOf(table, r)), label: String(r[column] ?? idOf(table, r)) }))
}

/** 한 행 전체 (수정 화면의 처음 값) */
export function mockRow(table: string, fields: Field[], id: unknown): Row {
  const hit = (tables[table] ?? []).find((r) => isRow(table, r, id))
  return hit ? toApi(hit, table, fields) : {}
}

export function mockList(table: string, columns: Field[], filters: Field[], query: Row, page: number, size: number): Page {
  let rows = tables[table] ?? []
  for (const f of filters) {
    const want = query[f.name]
    if (want === undefined || want === '' || !f.column) continue
    rows = rows.filter((r) => String(read(r, table, f) ?? '').includes(String(want)))
  }
  const items = rows.slice(page * size, (page + 1) * size).map((r) => ({ ...toApi(r, table, columns), __pk: idOf(table, r) }))
  return { items, page, size, total: rows.length }
}

export function mockSave(table: string, fields: Field[], body: Row, id?: unknown): void {
  const rows = (tables[table] ??= [])
  const values = Object.fromEntries(fields.filter((f) => f.column && tableOf(f) === table && f.name in body).map((f) => [columnOf(f), body[f.name]]))
  const hit = rows.find((r) => isRow(table, r, id))
  if (hit) Object.assign(hit, values)
  else rows.unshift({ ...rows[0], ...values, [pkOf(table)]: Math.max(0, ...rows.map((r) => Number(idOf(table, r)) || 0)) + 1 })
}

export function mockDelete(table: string, id: unknown): void {
  const rows = tables[table] ?? []
  const at = rows.findIndex((r) => isRow(table, r, id))
  if (at >= 0) rows.splice(at, 1)
}

export function mockSet(table: string, column: string, value: string, id: unknown): void {
  const hit = (tables[table] ?? []).find((r) => isRow(table, r, id))
  if (hit) hit[column] = value
}
