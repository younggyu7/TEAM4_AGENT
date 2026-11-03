import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { deleteRow, fetchLookup, fetchRows, loadRow, missingParams, runAction, saveRow } from './api'
import type { Action, Field, FormView, Option, Page, Row, Rule, Screen } from './types'

// 화면 한 장. 모든 화면이 이 틀 하나로 그려지므로 화면끼리 모양과 동작이 같다. 화면마다 다른 것은 screens.json의 값뿐이다.
const PAGE_SIZE = 10
const MAX_COLUMNS = 7

const TONES: [RegExp, string][] = [
  [/APPROVED|CONFIRMED|COMPLETED|DONE|PAID|ACTIVE|SUCCESS|PUBLISHED|승인|확정|완료/i, 'ok'],
  [/REJECTED|FAILED|CANCEL|반려|실패|취소/i, 'bad'],
  [/REVIEW|PENDING|REQUESTED|SUBMITTED|검토|대기|요청/i, 'warn'],
]
const toneOf = (value: string) => TONES.find(([re]) => re.test(value))?.[1] ?? 'plain'
const viewOf = (s: Screen) => s.list ?? s.report
// 다른 테이블을 가리키는 칸의 이름 목록 (테이블 → 선택지). 화면을 열 때와 저장한 뒤 다시 읽어 새로 등록한 행도 보인다.
type Lookups = Record<string, Option[]>
const LookupContext = createContext<Lookups>({})

function allFields(s: Screen): Field[] {
  const view = viewOf(s)
  return [...(view?.filters ?? []), ...(view?.columns ?? []), ...(s.create?.fields ?? []), ...(s.update?.fields ?? []), ...s.actions.flatMap((a) => a.fields)]
}

/** 상태 칸 */
export const statusField = (s: Screen) => viewOf(s)?.columns.find((c) => c.status)
/** 행 제목으로 쓸 칸: 제목·이름, 없으면 첫 글자 칸 */
export const titleField = (s: Screen) => {
  const columns = viewOf(s)?.columns ?? []
  return columns.find((c) => /title|subject|제목/i.test(c.name + c.label))
    ?? columns.find((c) => /name|명$|번호$/i.test(c.name) || /명$|번호$/.test(c.label))
    ?? columns.find((c) => c.type === 'text')
}
/** 처리를 기다리는 행인가 */
export function waiting(s: Screen, row: Row): boolean {
  const field = statusField(s)
  return Boolean(field) && toneOf(String(row[field!.name] ?? '')) === 'warn'
}

export function Cell({ field, value }: { field: Field; value: unknown }) {
  const lookups = useContext(LookupContext)
  if (value === undefined || value === null || value === '') return <span className="dim">-</span>
  if (field.options) {
    const label = field.options.find((o) => o.value === value)?.label ?? String(value)
    return <span className={`badge ${toneOf(String(value))}`}>{label}</span>
  }
  if (field.ref) return <>{lookups[field.ref]?.find((o) => o.value === String(value))?.label ?? String(value)}</>
  if (field.type === 'number') return <span className="num">{Number(value).toLocaleString('ko-KR')}</span>
  if (field.type === 'boolean') return <>{value ? '예' : '아니오'}</>
  if (field.type === 'datetime') return <span className="num">{String(value).slice(0, 16).replace('T', ' ')}</span>
  if (field.type === 'date') return <span className="num">{String(value).slice(0, 10)}</span>
  return <>{String(value)}</>
}

function FieldInput({ id, field, value, onChange, filter }: {
  id: string
  field: Field
  value: unknown
  onChange: (v: unknown) => void
  filter?: boolean
}) {
  const lookups = useContext(LookupContext)
  const text = value === undefined || value === null ? '' : String(value)
  const set = (v: string) => onChange(field.type === 'number' && v !== '' ? Number(v) : v)
  const options = field.options ?? (field.ref ? lookups[field.ref] : undefined)
  if (options) {
    return (
      <select id={id} value={text} onChange={(e) => set(e.target.value)}>
        <option value="">{filter ? '전체' : '선택'}</option>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    )
  }
  if (field.type === 'boolean') {
    return (
      <select id={id} value={text} onChange={(e) => onChange(e.target.value === '' ? '' : e.target.value === 'true')}>
        <option value="">{filter ? '전체' : '선택'}</option>
        <option value="true">예</option>
        <option value="false">아니오</option>
      </select>
    )
  }
  if (field.type === 'textarea' && !filter) return <textarea id={id} rows={3} value={text} onChange={(e) => set(e.target.value)} />
  const type = { number: 'number', date: 'date', datetime: 'datetime-local' }[field.type as string] ?? 'text'
  return <input id={id} type={type} value={type === 'datetime-local' ? text.slice(0, 16) : text} onChange={(e) => set(e.target.value)} />
}

function Drawer({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
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

function FormBody({ note, fields, initial, submit, tone, onDone }: {
  note?: string
  fields: Field[]
  initial: Row
  submit: string
  tone?: string
  onDone: (values: Row) => Promise<void>
}) {
  const [values, setValues] = useState<Row>(initial)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const send = async (e: FormEvent) => {
    e.preventDefault()
    const missing = fields.filter((f) => f.required && (values[f.name] === undefined || values[f.name] === ''))
    if (missing.length) return setError(`${missing.map((f) => f.label).join(', ')}을(를) 입력해 주세요.`)
    setBusy(true)
    try {
      await onDone(values)
    } catch (err) {
      setError((err as Error).message)
      setBusy(false)
    }
  }
  return (
    <form className="drawer-form" onSubmit={send} noValidate>
      {note && <p>{note}</p>}
      <div className="fields">
        {fields.map((f) => (
          <label key={f.name} htmlFor={`form-${f.name}`} className={f.type === 'textarea' ? 'wide' : ''}>
            <span>{f.label}{f.required && <em aria-label="필수"> *</em>}</span>
            <FieldInput id={`form-${f.name}`} field={f} value={values[f.name]} onChange={(v) => setValues({ ...values, [f.name]: v })} />
          </label>
        ))}
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="acts"><button type="submit" className={tone ?? ''} disabled={busy}>{submit}</button></div>
    </form>
  )
}

const RULE_GROUPS: { kind: Rule['kind']; title: string; tone: string }[] = [
  { kind: '보류', title: '고객 확인 필요', tone: 'bad' },
  { kind: '가정', title: '가정', tone: 'warn' },
  { kind: '확정', title: '확정 규칙', tone: 'ok' },
  { kind: '공통', title: '공통 규칙', tone: 'plain' },
]

function RuleNotes({ screen }: { screen: Screen }) {
  return (
    <aside className="rules" aria-label="이 화면의 업무 규칙">
      <h2>업무 규칙 검토</h2>
      <p className="dim">{screen.function_id} · {screen.task || '작업 없음'} · 수용 기준 {screen.ac_count}개</p>
      {RULE_GROUPS.map((g) => {
        const rules = screen.rules.filter((r) => r.kind === g.kind)
        return rules.length > 0 && (
          <details key={g.kind} open={g.kind === '보류' || g.kind === '가정'}>
            <summary><span className={`badge ${g.tone}`}>{g.title}</span> <span className="num">{rules.length}</span></summary>
            <ul>{rules.map((r) => <li key={r.text}>{r.text}</li>)}</ul>
          </details>
        )
      })}
    </aside>
  )
}

type Panel =
  | { mode: 'detail'; row: Row }
  | { mode: 'form'; form: FormView; row: Row | null }
  | { mode: 'action'; action: Action; row: Row | null }
  | { mode: 'delete'; row: Row }
  | null

export function ScreenPage({ screen, role, notify, review }: { screen: Screen; role: string; notify: (text: string) => void; review: boolean }) {
  const view = viewOf(screen)
  const [query, setQuery] = useState<Row>({})
  const [applied, setApplied] = useState<Row>({})
  const [page, setPage] = useState(0)
  const [data, setData] = useState<Page | null>(null)
  const [error, setError] = useState('')
  const [panel, setPanel] = useState<Panel>(null)
  const [tick, setTick] = useState(0)
  const [lookups, setLookups] = useState<Lookups>({})
  const reload = useCallback(() => setTick((t) => t + 1), [])
  const refTables = useMemo(() => [...new Set(allFields(screen).flatMap((f) => (f.ref ? [f.ref] : [])))], [screen])

  useEffect(() => {
    let live = true
    setError('')
    fetchRows(screen, applied, page, PAGE_SIZE, role)
      .then((d) => { if (live) setData(d) })
      .catch((e: Error) => { if (live) setError(e.message) })
    return () => { live = false }
  }, [screen, applied, page, role, tick])

  useEffect(() => {
    let live = true
    Promise.all(refTables.map((t) => fetchLookup(t, role).then((o) => [t, o] as const).catch(() => [t, undefined] as const)))
      .then((pairs) => { if (live) setLookups(Object.fromEntries(pairs.filter(([, o]) => o)) as Lookups) })
    return () => { live = false }
  }, [refTables, role, tick])

  const close = () => setPanel(null)
  const done = (text: string) => { close(); reload(); notify(text) }
  const columns = (view?.columns ?? []).filter((c) => c.column !== `${screen.table}.${screen.pk}`).slice(0, MAX_COLUMNS)
  const filters = view?.filters ?? []
  const missing = missingParams(screen, applied)
  const rows = data?.items ?? []
  const lastPage = data ? Math.max(0, Math.ceil(data.total / PAGE_SIZE) - 1) : 0
  const subject = screen.table_label || screen.name
  // 스펙의 상태 전환표가 허용한 상태에서만 처리 버튼을 보인다 (표가 없으면 이미 그 상태인 행만 뺀다) (이미 승인된 행에 '승인' 없음)
  const actionsFor = (row: Row) => screen.actions.filter((a) => a.path.includes('{')).filter((a) => {
    const field = view?.columns.find((c) => c.column === `${screen.table}.${a.sets?.column}`)
    if (!a.sets || !field) return true
    const now = String(row[field.name] ?? '')
    return a.sets.from ? a.sets.from.includes(now) : now !== a.sets.value
  })
  const pageActions = screen.list ? screen.actions.filter((a) => !a.path.includes('{')) : screen.actions   // 일괄 처리는 행을 고르지 않는다

  const edit = async (form: FormView, row: Row) => {
    try {
      setPanel({ mode: 'form', form, row: await loadRow(screen, form, row, role) })
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <LookupContext.Provider value={lookups}>
    <div className={review ? 'page-body with-rules' : 'page-body'}>
      <div className="work">
        {view && filters.length > 0 && (
          <form className="filters" onSubmit={(e) => { e.preventDefault(); setPage(0); setApplied(query) }}>
            {filters.map((f) => (
              <label key={f.name} htmlFor={`filter-${f.name}`}>
                <span>{f.label}</span>
                <FieldInput id={`filter-${f.name}`} field={f} value={query[f.name]} filter onChange={(v) => setQuery({ ...query, [f.name]: v })} />
              </label>
            ))}
            <div className="filter-acts">
              <button type="button" className="ghost" onClick={() => { setQuery({}); setApplied({}); setPage(0) }}>초기화</button>
              <button type="submit">조회</button>
            </div>
          </form>
        )}
        {error && <p className="error" role="alert">{error}</p>}
        {missing.length > 0 && <p className="dim">{missing.join(', ')}을(를) 고른 뒤 조회하세요.</p>}
        {screen.report && (
          <dl className="tiles">
            {screen.report.columns.map((c) => (
              <div key={c.name}><dt>{c.label}</dt><dd><Cell field={c} value={rows[0]?.[c.name]} /></dd></div>
            ))}
          </dl>
        )}
        {(screen.list || screen.create || pageActions.length > 0) && (
          <div className="band">
            <span className="dim">{screen.list ? <>총 <b className="num">{(data?.total ?? 0).toLocaleString('ko-KR')}</b>건</> : null}</span>
            <span className="band-acts">
              {pageActions.map((a) => <button key={a.operation_id} type="button" className="ghost" onClick={() => setPanel({ mode: 'action', action: a, row: null })}>{a.label}</button>)}
              {screen.create && <button type="button" onClick={() => setPanel({ mode: 'form', form: screen.create!, row: null })}>{subject} 등록</button>}
            </span>
          </div>
        )}
        {screen.list && (
          <>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>{columns.map((c) => <th key={c.name} scope="col">{c.label}</th>)}</tr>
                </thead>
                <tbody>
                  {rows.map((row, i) => (
                    <tr
                      key={String(row.__pk ?? i)}
                      tabIndex={0}
                      onClick={() => setPanel({ mode: 'detail', row })}
                      onKeyDown={(e) => { if (e.key === 'Enter') setPanel({ mode: 'detail', row }) }}
                    >
                      {columns.map((c) => <td key={c.name}><Cell field={c} value={row[c.name]} /></td>)}
                    </tr>
                  ))}
                  {rows.length === 0 && <tr className="none"><td colSpan={columns.length} className="empty">{missing.length ? '조회 조건을 고르세요.' : '조건에 맞는 자료가 없습니다.'}</td></tr>}
                </tbody>
              </table>
            </div>
            {lastPage > 0 && (
              <div className="pager">
                <button type="button" className="ghost" disabled={page === 0} onClick={() => setPage(page - 1)}>이전</button>
                <span className="num">{page + 1} / {lastPage + 1}</span>
                <button type="button" className="ghost" disabled={page >= lastPage} onClick={() => setPage(page + 1)}>다음</button>
              </div>
            )}
          </>
        )}
      </div>
      {review && <RuleNotes screen={screen} />}

      {panel?.mode === 'detail' && (
        <Drawer title={`${subject} 상세`} onClose={close}>
          <dl className="pairs">
            {(view?.columns ?? []).map((f) => <div key={f.name}><dt>{f.label}</dt><dd><Cell field={f} value={panel.row[f.name]} /></dd></div>)}
          </dl>
          <div className="acts">
            {screen.delete && <button type="button" className="ghost danger" onClick={() => setPanel({ mode: 'delete', row: panel.row })}>삭제</button>}
            {screen.update && <button type="button" className="ghost" onClick={() => edit(screen.update!, panel.row)}>수정</button>}
            {actionsFor(panel.row).map((a) => <button key={a.operation_id} type="button" onClick={() => setPanel({ mode: 'action', action: a, row: panel.row })}>{a.label}</button>)}
          </div>
        </Drawer>
      )}
      {panel?.mode === 'form' && (
        <Drawer title={`${subject} ${panel.row ? '수정' : '등록'}`} onClose={close}>
          <FormBody
            fields={panel.form.fields}
            initial={panel.row ?? {}}
            submit={panel.row ? '저장' : '등록'}
            onDone={async (values) => {
              await saveRow(screen, panel.form, values, panel.row, role)
              done(panel.row ? '수정했습니다.' : '등록했습니다.')
            }}
          />
        </Drawer>
      )}
      {panel?.mode === 'action' && (
        <Drawer title={panel.action.label} onClose={close}>
          <FormBody
            note={panel.action.sets ? `상태를 '${panel.action.sets.label}'(으)로 바꿉니다.` : undefined}
            fields={panel.action.fields}
            initial={{}}
            submit={panel.action.label}
            onDone={async (values) => {
              await runAction(screen, panel.action, panel.row, values, role)
              done(`${panel.action.label}을(를) 처리했습니다.`)
            }}
          />
        </Drawer>
      )}
      {panel?.mode === 'delete' && (
        <Drawer title="삭제할까요?" onClose={close}>
          <FormBody
            note="삭제하면 되돌릴 수 없습니다."
            fields={[]}
            initial={{}}
            submit="삭제"
            tone="danger-fill"
            onDone={async () => {
              await deleteRow(screen, panel.row, role)
              done('삭제했습니다.')
            }}
          />
        </Drawer>
      )}
    </div>
    </LookupContext.Provider>
  )
}
