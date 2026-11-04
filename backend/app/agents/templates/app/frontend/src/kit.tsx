import { useCallback, useEffect, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { SERVER } from './api'
import { authHeaders, expired } from './session'

// 기능 화면 부품: 개발 Agent(LLM)가 쓴 기능 화면(pages/*.tsx)은 이 부품만 써서 다른 화면과 모양·동작을 맞춘다.
// 서버 모드(VITE_API=server)에서만 동작한다 (예시 데이터 모드에서는 안내만 보인다).

/** 기능 화면이 받는 값 */
export interface PageProps { role: string; notify: (text: string) => void }

export type Row = Record<string, unknown>

/** API 오류: status는 HTTP 상태, code는 openapi의 오류 코드, message는 사용자에게 보일 문장 */
export class ApiFail extends Error {
  constructor(public status: number, public code: string, message: string) { super(message) }
}


/** API 호출. path는 /api/v1 뒤 경로 (예: '/budget-masters/3'). query의 빈 값은 보내지 않는다. 목록 응답은 {items, page, size, total} */
export async function call<T = Row>(method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE', path: string, role: string, body?: Row, query?: Row): Promise<T> {
  if (!SERVER) throw new ApiFail(0, 'MOCK', '이 화면은 서버 모드(VITE_API=server)에서 동작합니다.')
  const pairs = Object.entries(query ?? {}).filter(([, v]) => v !== '' && v !== undefined && v !== null).map(([k, v]) => [k, String(v)])
  const search = new URLSearchParams(pairs).toString()
  void role   // 서버는 로그인 토큰으로 사용자와 역할을 정한다
  const res = await fetch(`/api/v1${path}${search ? `?${search}` : ''}`, { method, headers: authHeaders(), body: body ? JSON.stringify(body) : undefined })
  const data = res.status === 204 ? {} : await res.json().catch(() => ({}))
  if (method !== 'GET') lookupCache.clear()
  if (!res.ok) {
    expired(res.status)
    const e = data as { code?: string; message?: string }
    throw new ApiFail(res.status, e.code ?? 'ERROR', e.message ?? `요청을 처리하지 못했습니다 (${res.status})`)
  }
  return data as T
}

/** 목록 응답 */
export interface Page<R = Row> { items: R[]; page: number; size: number; total: number }

/** 불러오기: deps가 바뀌거나 reload()하면 다시 부른다 */
export function useData<T>(load: () => Promise<T>, deps: unknown[]): { data: T | undefined; error: string; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T>()
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [tick, setTick] = useState(0)
  const reload = useCallback(() => setTick((t) => t + 1), [])
  useEffect(() => {
    let live = true
    setLoading(true)
    setError('')
    load().then((d) => { if (live) setData(d) }).catch((e: Error) => { if (live) setError(e.message) }).finally(() => { if (live) setLoading(false) })
    return () => { live = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick])
  return { data, error, loading, reload }
}

const TONES: [RegExp, string][] = [
  [/APPROVED|CONFIRMED|COMPLETED|DONE|ACTIVE|승인|확정|완료/i, 'ok'],
  [/REJECTED|FAILED|CANCEL|반려|실패|취소/i, 'bad'],
  [/REVIEW|PENDING|REQUESTED|진행|검토|대기|요청/i, 'warn'],
]

/** 상태 표시 (승인·완료는 초록, 반려·실패는 빨강, 진행·대기는 노랑) */
export function Badge({ value }: { value: unknown }) {
  const text = value === undefined || value === null ? '' : String(value)
  if (!text) return <span className="dim">-</span>
  return <span className={`badge ${TONES.find(([re]) => re.test(text))?.[1] ?? 'plain'}`}>{text}</span>
}

/** 금액·수량 (천 단위 쉼표) */
export function Num({ value }: { value: unknown }) {
  if (value === undefined || value === null || value === '') return <span className="dim">-</span>
  return <span className="num">{Number(value).toLocaleString('ko-KR')}</span>
}

/** 날짜·시각 (YYYY-MM-DD 또는 YYYY-MM-DD HH:mm) */
export function DateText({ value, time }: { value: unknown; time?: boolean }) {
  if (!value) return <span className="dim">-</span>
  return <span className="num">{time ? String(value).slice(0, 16).replace('T', ' ') : String(value).slice(0, 10)}</span>
}

/** 조회 조건 줄: 안에 Field들을 두고, 조회·초기화 버튼이 붙는다 */
export function Filters({ onSearch, onReset, children }: { onSearch: () => void; onReset?: () => void; children: ReactNode }) {
  return (
    <form className="filters" onSubmit={(e) => { e.preventDefault(); onSearch() }}>
      {children}
      <div className="filter-acts">
        {onReset && <button type="button" className="ghost" onClick={onReset}>초기화</button>}
        <button type="submit">조회</button>
      </div>
    </form>
  )
}

/** 목록 위 줄: 왼쪽 건수, 오른쪽 버튼들 */
export function Band({ total, children }: { total?: number; children?: ReactNode }) {
  return (
    <div className="band">
      <span className="dim">{total !== undefined && <>총 <b className="num">{total.toLocaleString('ko-KR')}</b>건</>}</span>
      <span className="band-acts">{children}</span>
    </div>
  )
}

/** 표의 칸: key는 행의 필드 이름, render가 있으면 그 내용으로 그린다 */
export interface Column<R = Row> { key: string; label: string; render?: (row: R) => ReactNode }

/** 목록 표. 행을 누르면 onRowClick */
export function DataTable<R extends Row = Row>({ columns, rows, onRowClick, empty }: { columns: Column<R>[]; rows: R[]; onRowClick?: (row: R) => void; empty?: string }) {
  return (
    <div className="table-wrap">
      <table>
        <thead><tr>{columns.map((c) => <th key={c.key} scope="col">{c.label}</th>)}</tr></thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} tabIndex={onRowClick ? 0 : undefined} onClick={() => onRowClick?.(row)} onKeyDown={(e) => { if (e.key === 'Enter') onRowClick?.(row) }}>
              {columns.map((c) => <td key={c.key}>{c.render ? c.render(row) : row[c.key] === undefined || row[c.key] === null || row[c.key] === '' ? <span className="dim">-</span> : String(row[c.key])}</td>)}
            </tr>
          ))}
          {rows.length === 0 && <tr className="none"><td colSpan={columns.length} className="empty">{empty ?? '조건에 맞는 자료가 없습니다.'}</td></tr>}
        </tbody>
      </table>
    </div>
  )
}

/** 쪽 넘기기 (page는 0부터) */
export function Pager({ page, size, total, onPage }: { page: number; size: number; total: number; onPage: (page: number) => void }) {
  const last = Math.max(0, Math.ceil(total / size) - 1)
  if (last === 0) return null
  return (
    <div className="pager">
      <button type="button" className="ghost" disabled={page === 0} onClick={() => onPage(page - 1)}>이전</button>
      <span className="num">{page + 1} / {last + 1}</span>
      <button type="button" className="ghost" disabled={page >= last} onClick={() => onPage(page + 1)}>다음</button>
    </div>
  )
}

/** 오른쪽에서 열리는 패널 (상세·등록·수정·처리) */
export function Drawer({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <div className="veil" onClick={onClose}>
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={title} onClick={(e) => e.stopPropagation()}>
        <header>
          <h2>{title}</h2>
          <button type="button" className="ghost" onClick={onClose}>닫기</button>
        </header>
        {children}
      </aside>
    </div>
  )
}

/** 상세 보기: [이름, 내용] 목록 */
export function Pairs({ items }: { items: [string, ReactNode][] }) {
  return <dl className="pairs">{items.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
}

/** 요약 숫자 칸들 */
export function Tiles({ items }: { items: [string, ReactNode][] }) {
  return <dl className="tiles">{items.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
}

/** 입력 폼: onSubmit이 예외를 던지면 그 문장을 폼 아래에 보인다. 처리 중에는 버튼을 막는다 */
export function Form({ onSubmit, submit, danger, note, children }: { onSubmit: () => Promise<void>; submit: string; danger?: boolean; note?: ReactNode; children?: ReactNode }) {
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const send = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      await onSubmit()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <form className="drawer-form" onSubmit={send} noValidate>
      {note && <p>{note}</p>}
      {children && <div className="fields">{children}</div>}
      {error && <p className="error" role="alert">{error}</p>}
      <div className="acts"><button type="submit" className={danger ? 'danger-fill' : ''} disabled={busy}>{submit}</button></div>
    </form>
  )
}

/** 입력 칸 하나 (이름표 + 입력). wide면 한 줄 전체 */
export function Field({ label, required, wide, hint, children }: { label: string; required?: boolean; wide?: boolean; hint?: string; children: ReactNode }) {
  return (
    <label className={wide ? 'wide' : ''}>
      <span>{label}{required && <em aria-label="필수"> *</em>}</span>
      {children}
      {hint && <small className="dim">{hint}</small>}
    </label>
  )
}

/** 글자 입력 (multiline이면 여러 줄) */
export function TextInput({ value, onChange, multiline, placeholder }: { value: unknown; onChange: (v: string) => void; multiline?: boolean; placeholder?: string }) {
  const text = value === undefined || value === null ? '' : String(value)
  return multiline
    ? <textarea rows={3} value={text} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
    : <input type="text" value={text} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
}

/** 숫자 입력: 비우면 '' (숫자가 아니면 그대로 글자로 둔다 — 서버가 검사한다) */
export function NumberInput({ value, onChange }: { value: unknown; onChange: (v: number | string) => void }) {
  const text = value === undefined || value === null ? '' : String(value)
  return <input type="number" value={text} onChange={(e) => onChange(e.target.value === '' || Number.isNaN(Number(e.target.value)) ? e.target.value : Number(e.target.value))} />
}

/** 날짜 입력 (YYYY-MM-DD) */
export function DateInput({ value, onChange }: { value: unknown; onChange: (v: string) => void }) {
  return <input type="date" value={value ? String(value).slice(0, 10) : ''} onChange={(e) => onChange(e.target.value)} />
}

/** 고르기: options는 [값, 이름] 목록. 빈 칸 이름은 placeholder (기본 '선택') */
export function Select({ value, onChange, options, placeholder }: { value: unknown; onChange: (v: string) => void; options: [string, string][]; placeholder?: string }) {
  return (
    <select value={value === undefined || value === null ? '' : String(value)} onChange={(e) => onChange(e.target.value)}>
      <option value="">{placeholder ?? '선택'}</option>
      {options.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
    </select>
  )
}

// 선택 목록은 테이블마다 한 번 읽어 같이 쓴다 (표의 칸마다 부르지 않게). 값을 바꾸는 API를 부르면 비운다 (새로 등록한 행이 보이게)
const lookupCache = new Map<string, Promise<{ value: string; label: string }[]>>()

function lookups(table: string, role: string): Promise<{ value: string; label: string }[]> {
  if (!SERVER) return Promise.resolve([])
  const key = `${table}|${role}`
  if (!lookupCache.has(key)) {
    lookupCache.set(key, fetch(`/api/ui/lookups/${encodeURIComponent(table)}`, { headers: authHeaders() }).then((r) => (r.ok ? r.json() : [])).catch(() => []))
  }
  return lookupCache.get(key)!
}

/** 다른 테이블의 행 고르기 (ID 대신 이름이 보인다). table은 schema.sql 테이블 이름. 고른 값은 숫자 ID면 숫자로 준다 */
export function LookupSelect({ table, role, value, onChange, placeholder }: { table: string; role: string; value: unknown; onChange: (v: number | string) => void; placeholder?: string }) {
  const { data } = useData(() => lookups(table, role), [table, role])
  return <Select value={value} placeholder={placeholder} options={(data ?? []).map((o) => [o.value, o.label])} onChange={(v) => onChange(v !== '' && /^\d+$/.test(v) ? Number(v) : v)} />
}

/** 다른 테이블의 ID를 이름으로 보인다 (목록·상세의 칸). table은 schema.sql 테이블 이름 */
export function RefName({ table, role, value }: { table: string; role: string; value: unknown }) {
  const { data } = useData(() => lookups(table, role), [table, role])
  if (value === undefined || value === null || value === '') return <span className="dim">-</span>
  return <>{data?.find((o) => o.value === String(value))?.label ?? String(value)}</>
}

/** 오류 문장 */
export function ErrorText({ text }: { text: string }) {
  return text ? <p className="error" role="alert">{text}</p> : null
}
