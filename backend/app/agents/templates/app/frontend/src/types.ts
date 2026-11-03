// 화면 설계(generated/screens.json)의 모양. 코드가 만든 값이고 이 앱은 그 값대로 화면을 그린다.
export interface Option { value: string; label: string }

export interface Field {
  name: string
  label: string
  type: 'text' | 'textarea' | 'number' | 'date' | 'datetime' | 'boolean' | 'select' | 'ref'
  required: boolean
  column: string
  options?: Option[]
  status?: boolean
  ref?: string
}

export interface View { operation_id: string; path: string; filters: Field[]; columns: Field[] }
export interface FormView { operation_id: string; path: string; method: string; fields: Field[] }
export interface DeleteView { operation_id: string; path: string }

export interface Action {
  operation_id: string
  label: string
  kind: 'action' | 'add' | 'update' | 'delete'
  method: string
  path: string
  fields: Field[]
  sets?: { column: string; value: string; label: string; from?: string[] }
}

export interface Rule { kind: '확정' | '가정' | '보류' | '공통'; text: string }

export interface Screen {
  screen_id: string
  function_id: string
  name: string
  group: string
  description: string
  task: string
  rules: Rule[]
  ac_count: number
  pattern: 'crud' | 'list' | 'report' | 'form' | 'action'
  pattern_label: string
  roles: string[]
  table: string
  table_label: string
  pk: string
  list: View | null
  report: View | null
  create: FormView | null
  update: FormView | null
  delete: DeleteView | null
  actions: Action[]
}

export interface Auto {
  screen_id: string
  function_id: string
  name: string
  group: string
  description: string
  reason: string
  task: string
  rules: Rule[]
}

export interface Design {
  project: string
  title: string
  version: string
  stack: string
  menu: { group: string; name: string; short: string; pages: { name: string; screens: string[] }[] }[]
  roles: { name: string; scope: string }[]
  screens: Screen[]
  autos: Auto[]
}

export type Row = Record<string, unknown>
export interface Page { items: Row[]; page: number; size: number; total: number }
