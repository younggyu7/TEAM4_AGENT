/**
 * 공통 DB 처리. 테이블·컬럼·타입 이름은 코드가 만든 상수(Table, Col)에서만 오고, 값은 모두 바인딩한다.
 *
 * 업무 로직에서 직접 쿼리가 필요하면 query·execute를 쓴다 (:이름 바인딩, params는 객체).
 * DB 방언에 따라 다른 SQL은 이 파일에만 둔다: PostgreSQL(DB_URL=postgresql://…, pg)과 MySQL 8(DB_URL=mysql://…, mysql2).
 * MySQL은 연결마다 sql_mode ANSI를 켠다: 이름은 큰따옴표, ||는 문자열 잇기라 PostgreSQL과 같은 SQL 모양을 쓴다.
 */
import { createPool, Pool as MyPool, PoolConnection, ResultSetHeader } from 'mysql2/promise';
import { Pool as PgPool, PoolClient, types } from 'pg';

import { ApiError } from './errors';

export const DEFAULT_SIZE = 20;
export const MAX_SIZE = 200;
/** 값 자리에 넣으면 현재 시각이 들어간다 */
export const NOW = Symbol('NOW');

/** 값 자리에 넣으면 날짜와 임의 문자로 만든 기본 번호가 들어간다. 실제 채번 규칙은 업무 로직에서 정한다. */
export class AutoNo {
  constructor(readonly length: number) {}
}

export function autoNo(length: number): AutoNo {
  return new AutoNo(length);
}

/** 요청·응답 값 묶음 (필드 이름 → 값) */
export type Data = Record<string, any>;

/** API 필드 ↔ DB 컬럼. type은 PostgreSQL 타입 이름이고 코드가 만든 상수에서만 온다. */
export interface Col {
  readonly field: string;
  readonly column: string;
  readonly type: string;
}

export function col(field: string, column: string, type: string): Col {
  return { field, column, type };
}

/** 테이블 정보. created·updated는 입력·수정 때 현재 시각을 넣을 컬럼이고, joins는 조회할 때 붙일 부모 테이블이다. */
export class Table {
  constructor(readonly name: string, readonly pk: string, readonly pkType: string, readonly created: string[] = [],
              readonly updated: string[] = [], readonly joins = '') {}

  withJoins(joins: string): Table {
    return new Table(this.name, this.pk, this.pkType, this.created, this.updated, joins);
  }

  get source(): string {
    return this.joins ? `${this.name} ${this.joins}` : this.name;
  }

  get key(): string {
    return `${this.name}.${this.pk}`;
  }
}

// ------------------------------------------------------------------ 연결 (방언별)
const URL_TEXT = process.env.DB_URL || 'postgresql://app:app@localhost:5433/app';
// ANSI: 이름은 큰따옴표, ||는 문자열 잇기. STRICT: 맞지 않는 값은 잘라 넣지 않고 오류 (Spring 틀의 application.yml과 같다)
const MYSQL_MODE = "SET SESSION sql_mode = 'ANSI,STRICT_TRANS_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO'";

export function mysql(): boolean {
  return URL_TEXT.startsWith('mysql');
}

// PostgreSQL: bigint·count(*)·numeric은 숫자, date는 글자 그대로 (다른 틀의 JSON과 같게)
types.setTypeParser(20, (v) => Number(v));
types.setTypeParser(1700, (v) => Number(v));
types.setTypeParser(1082, (v) => v);

let pg: PgPool | null = null;
let my: MyPool | null = null;

interface Result {
  rows: Data[];
  count: number;
  insertId: unknown;
}

/** 연결 하나: SQL은 :이름 바인딩으로 받아 드라이버의 자리표($1·?)로 바꾼다. */
class Conn {
  constructor(private readonly pgClient: PoolClient | null, private readonly myConn: PoolConnection | null) {}

  async run(sql: string, params: Data = {}): Promise<Result> {
    const [text, values] = positional(sql, params, this.myConn !== null);
    if (this.pgClient) {
      const r = await this.pgClient.query(text, values);
      return { rows: r.rows || [], count: r.rowCount || 0, insertId: null };
    }
    const [r] = await this.myConn!.query(text, values);
    if (Array.isArray(r)) {
      return { rows: r as Data[], count: r.length, insertId: null };
    }
    const head = r as ResultSetHeader;
    return { rows: [], count: head.affectedRows, insertId: head.insertId || null };
  }

  async begin(): Promise<void> {
    if (this.pgClient) {
      await this.pgClient.query('BEGIN');
    } else {
      await this.myConn!.beginTransaction();
    }
  }

  async commit(): Promise<void> {
    if (this.pgClient) {
      await this.pgClient.query('COMMIT');
    } else {
      await this.myConn!.commit();
    }
  }

  async rollback(): Promise<void> {
    if (this.pgClient) {
      await this.pgClient.query('ROLLBACK');
    } else {
      await this.myConn!.rollback();
    }
  }

  release(): void {
    this.pgClient?.release();
    this.myConn?.release();
  }
}

export async function connect(): Promise<Conn> {
  if (mysql()) {
    if (!my) {
      const url = new URL(URL_TEXT);
      my = createPool({
        host: url.hostname, port: Number(url.port || 3306), user: decodeURIComponent(url.username),
        password: decodeURIComponent(url.password), database: url.pathname.replace(/^\//, ''), charset: 'utf8mb4',
        timezone: 'Z', dateStrings: ['DATE'], decimalNumbers: true, connectionLimit: 10,
      });
      my.pool.on('connection', (conn) => {
        conn.query(MYSQL_MODE);
      });
    }
    return new Conn(null, await my.getConnection());
  }
  if (!pg) {
    pg = new PgPool({ connectionString: URL_TEXT, max: 10 });
  }
  return new Conn(await pg.connect(), null);
}

/** :이름 → $1(PostgreSQL)·?(MySQL). 따옴표 안 글자와 ::타입 변환은 그대로 둔다. 없는 이름이면 오류다. */
export function positional(sql: string, params: Data, forMysql: boolean): [string, unknown[]] {
  const values: unknown[] = [];
  const index = new Map<string, number>();
  let out = '';
  for (let i = 0; i < sql.length; i++) {
    const ch = sql[i];
    if (ch === "'" || ch === '"') {
      let j = i + 1;
      while (j < sql.length && !(sql[j] === ch && sql[j + 1] !== ch)) {
        j += sql[j] === ch ? 2 : 1;
      }
      out += sql.slice(i, j + 1);
      i = j;
      continue;
    }
    const name = ch === ':' && !/[:\w\\]/.test(sql[i - 1] || '') ? /^[A-Za-z_]\w*/.exec(sql.slice(i + 1))?.[0] : undefined;
    if (!name || sql[i + 1 + name.length] === ':') {
      out += ch;
      continue;
    }
    if (!(name in params)) {
      throw new Error('값이 없는 SQL 파라미터: ' + name);
    }
    const value = params[name] === undefined ? null : params[name];
    if (forMysql) {
      values.push(value);
      out += '?';
    } else {
      if (!index.has(name)) {
        values.push(value);
        index.set(name, values.length);
      }
      out += '$' + index.get(name);
    }
    i += name.length;
  }
  return [out, values];
}

/** 요청 하나 = 트랜잭션 하나: 업무 로직·자동 처리까지 한 묶음, 예외가 나면 모두 되돌린다. */
export async function transaction<T>(work: (db: Db) => Promise<T>): Promise<T> {
  const conn = await connect();
  try {
    await conn.begin();
    const out = await work(new Db(conn));
    await conn.commit();
    return out;
  } catch (e) {
    await conn.rollback().catch(() => undefined);
    throw e;
  } finally {
    conn.release();
  }
}

/** 값을 글자로 읽는 SQL (MySQL의 CAST 타입 이름은 char). */
export function asText(expr: string): string {
  return `CAST(${expr} AS ${mysql() ? 'char' : 'text'})`;
}

/** PostgreSQL은 컬럼 타입으로 바꿔 넣는다. MySQL은 값을 컬럼 타입에 맞춰 넣고 비교하므로 JSON만 감싼다. */
function bind(name: string, kind: string): string {
  if (mysql()) {
    return kind === 'jsonb' ? `CAST(:${name} AS JSON)` : `:${name}`;
  }
  return `CAST(:${name} AS ${kind === 'yn' ? 'varchar' : kind})`;
}

/** 응답 컬럼 목록. 응답에 컬럼이 하나도 없으면 기본키만 읽는다 (빈 SELECT 목록은 SQL 오류다). */
function selectList(t: Table, out: Col[]): string {
  const parts = out.map((c) => {
    const column = c.type === 'jsonb' ? asText(c.column) : c.type === 'yn' ? `(${c.column} = 'Y')` : c.column;
    return `${column} AS "${c.field}"`;
  });
  return parts.length ? parts.join(', ') : `${t.key} AS "id"`;
}

/** 화면이 비워 보낸 값은 NULL로 저장한다. 묶음 값은 JSON 글자로. */
function clean(value: unknown): unknown {
  if (value !== null && typeof value === 'object') {
    return JSON.stringify(value);
  }
  return value === '' || value === undefined ? null : value;
}

/** 화면에서 글자로 입력한 값도 jsonb에 들어가게 한다: JSON이 아니면 JSON 문자열로 저장한다. */
function jsonText(value: string): string {
  try {
    JSON.parse(value);
    return value;
  } catch {
    return JSON.stringify(value);
  }
}

/** 표준이 boolean을 Y/N 글자로 바꾼 컬럼: API는 참·거짓, DB는 Y/N이다. */
function toColumn(c: Col, value: unknown): unknown {
  const cleaned = clean(value);
  if (c.type === 'yn' && cleaned !== null) {
    return String(cleaned).toLowerCase() === 'true' || cleaned === 'Y' ? 'Y' : 'N';
  }
  if (c.type === 'boolean' && typeof cleaned === 'string' && mysql()) {   // 조회 조건 ?done=true: MySQL은 'true'를 0으로 본다
    return ['true', '1', 'y'].includes(cleaned.toLowerCase());
  }
  if (c.type === 'jsonb' && typeof cleaned === 'string') {
    return jsonText(cleaned);
  }
  return cleaned;
}

/** MySQL은 참·거짓을 0·1로 돌려준다: API는 참·거짓이다 (PostgreSQL은 그대로). */
function bools(rows: Data[], out: Col[]): Data[] {
  const names = out.filter((c) => c.type === 'boolean' || c.type === 'yn').map((c) => c.field);
  if (names.length && mysql()) {
    for (const row of rows) {
      for (const name of names) {
        if (row[name] !== null && row[name] !== undefined) {
          row[name] = Boolean(Number(row[name]));
        }
      }
    }
  }
  return rows;
}

function intOf(value: unknown, fallback: number): number {
  if (value === undefined || value === null) {
    return fallback;
  }
  const text = String(value).trim();
  if (!/^[+-]?\d+$/.test(text)) {
    throw new ApiError(400, 'VALIDATION_FAILED', `숫자가 아닙니다: ${value}`);
  }
  return Number(text);
}

function first(row: Data | undefined): unknown {
  return row ? Object.values(row)[0] : undefined;
}

export class Db {
  private n = 0;

  constructor(private readonly conn: Conn) {}

  /** 값 자리 이름 (필드 이름은 바인딩 이름으로 쓰지 않는다). */
  private param(params: Data, value: unknown): string {
    this.n += 1;
    const name = `p${this.n}`;
    params[name] = value;
    return name;
  }

  private async rows(sql: string, params: Data): Promise<Data[]> {
    return (await this.conn.run(sql, params)).rows;
  }

  private where(filters: Col[], data: Data, params: Data): string {
    const parts: string[] = [];
    for (const c of filters) {
      const value = data[c.field];
      if (value === undefined || value === null || value === '') {
        continue;
      }
      const name = this.param(params, toColumn(c, value));
      parts.push(c.type === 'varchar' || c.type === 'text' ? `${c.column} LIKE '%' || :${name} || '%'` : `${c.column} = ${bind(name, c.type)}`);
    }
    return parts.length ? ' WHERE ' + parts.join(' AND ') : '';
  }

  /** 목록 조회: 응답 {items, page, size, total} */
  async page(t: Table, out: Col[], filters: Col[], data: Data): Promise<Data> {
    const page = Math.max(intOf(data.page, 0), 0);
    const size = Math.min(Math.max(intOf(data.size, DEFAULT_SIZE), 1), MAX_SIZE);
    const params: Data = {};
    const where = this.where(filters, data, params);
    const total = Number(first((await this.rows(`SELECT count(*) FROM ${t.source}${where}`, params))[0]));
    params.__limit = size;
    params.__offset = page * size;
    const items = bools(await this.rows(`SELECT ${t.key} AS "__pk", ${selectList(t, out)} FROM ${t.source}${where} ORDER BY ${t.key}`
      + ' LIMIT :__limit OFFSET :__offset', params), out);
    return { items, page, size, total };
  }

  /** 조건 조회 한 건 (요약 화면) */
  async first(t: Table, out: Col[], filters: Col[], data: Data): Promise<Data> {
    const params: Data = {};
    const where = this.where(filters, data, params);
    const rows = bools(await this.rows(`SELECT ${selectList(t, out)} FROM ${t.source}${where} ORDER BY ${t.key} LIMIT 1`, params), out);
    return rows[0] || {};
  }

  async one(t: Table, out: Col[], id: unknown): Promise<Data> {
    const rows = bools(await this.rows(`SELECT ${selectList(t, out)} FROM ${t.source} WHERE ${t.key} = ${bind('__id', t.pkType)}`, { __id: id }), out);
    if (!rows.length) {
      throw ApiError.notFound();
    }
    return rows[0];
  }

  /** needed: 코드로 값을 정할 수 없는 필수 컬럼. 업무 로직이 채우지 않으면 구현 전이다 (DB 제약 오류로 보여 주지 않는다). */
  async insert(t: Table, cols: Col[], out: Col[], data: Data, defaults: Data, operation = '', ...needed: string[]): Promise<Data> {
    for (const name of needed) {
      if (clean(data[name]) === null) {
        throw ApiError.notImplemented(`${operation} (${name})`);
      }
    }
    const columns: string[] = [];
    const marks: string[] = [];
    const params: Data = {};
    const given: Data = {};
    for (const c of cols) {
      if (c.field in data) {
        columns.push(c.column);
        given[c.column] = toColumn(c, data[c.field]);
        marks.push(bind(this.param(params, given[c.column]), c.type));
      }
    }
    for (const [column, value] of Object.entries(defaults)) {
      if (columns.includes(column)) {
        continue;
      }
      columns.push(column);
      if (value === NOW) {
        marks.push('now()');
      } else if (value instanceof AutoNo && mysql()) {
        marks.push(`left(concat(date_format(now(), '%y%m%d'), '-', substr(md5(rand()), 1, 6)), ${value.length})`);
      } else if (value instanceof AutoNo) {
        marks.push(`left(to_char(now(), 'YYMMDD') || '-' || substr(md5(CAST(random() AS text)), 1, 6), ${value.length})`);
      } else {
        marks.push(':' + this.param(params, value));
      }
    }
    for (const column of t.created) {
      columns.push(column);
      marks.push('now()');
    }
    const sql = `INSERT INTO ${t.name} (${columns.join(', ')}) VALUES (${marks.join(', ')})`;
    if (!mysql()) {
      return this.one(t, out, first((await this.rows(sql + ` RETURNING ${t.pk}`, params))[0]));
    }
    // MySQL에는 RETURNING이 없다: 자동 번호 키는 insertId, 키를 요청 값·기본값으로 정한 테이블은 그 값으로 다시 읽는다
    let id = (await this.conn.run(sql, params)).insertId;
    if (id === null || id === undefined) {
      id = t.pk in given ? given[t.pk] : defaults[t.pk];
    }
    return this.one(t, out, id);
  }

  /** 함께 남기는 값의 SET 조각 (처리자·처리 시각). vals는 필드 이름 → 값, NOW면 현재 시각이다. */
  private stamps(cols: Col[], vals: Data, params: Data): string[] {
    return cols.map((c) => vals[c.field] === NOW ? `${c.column} = now()` : `${c.column} = ${bind(this.param(params, toColumn(c, vals[c.field])), c.type)}`);
  }

  /** 수정 (+ 함께 남기는 값: 처리자·처리 시각) */
  async update(t: Table, cols: Col[], out: Col[], id: unknown, data: Data, stamp: Col[] = [], vals: Data = {}): Promise<Data> {
    const params: Data = { __id: id };
    const sets = cols.filter((c) => c.field in data).map((c) => `${c.column} = ${bind(this.param(params, toColumn(c, data[c.field])), c.type)}`);
    sets.push(...this.stamps(stamp, vals, params), ...t.updated.map((column) => `${column} = now()`));
    if (sets.length) {
      const count = (await this.conn.run(`UPDATE ${t.name} SET ${sets.join(', ')} WHERE ${t.pk} = ${bind('__id', t.pkType)}`, params)).count;
      if (count === 0) {
        throw ApiError.notFound();
      }
    }
    return this.one(t, out, id);
  }

  /** 상태를 바꾸는 SET 절: 상태, 함께 남기는 값, 수정 시각 */
  private stateSets(t: Table, state: string, stamp: Col[], vals: Data, params: Data): string {
    return [state, ...this.stamps(stamp, vals, params), ...t.updated.map((column) => `${column} = now()`)].join(', ');
  }

  /** 상태 값 바꾸기 (승인·반려 같은 처리의 기본 동작) + 처리자·처리 시각 */
  async set(t: Table, column: string, value: string, id: unknown, out: Col[], stamp: Col[] = [], vals: Data = {}): Promise<Data> {
    const params: Data = { __id: id };
    const state = `${column} = :${this.param(params, value)}`;
    const count = (await this.conn.run(`UPDATE ${t.name} SET ${this.stateSets(t, state, stamp, vals, params)}`
      + ` WHERE ${t.pk} = ${bind('__id', t.pkType)}`, params)).count;
    if (count === 0) {
      throw ApiError.notFound();
    }
    return this.one(t, out, id);
  }

  /** 상태 전환: 허용된 현재 상태에서만 바꾼다 (BEHAVIOR.md 1절). 다른 상태면 409로 거절하고 값을 바꾸지 않는다. */
  async move(t: Table, column: string, from: string[], to: string, id: unknown, out: Col[], stamp: Col[] = [], vals: Data = {}): Promise<Data> {
    const params: Data = { __id: id };
    const state = `${column} = :${this.param(params, to)}`;
    const allowed = from.map((v) => ':' + this.param(params, v)).join(', ');
    const count = (await this.conn.run(`UPDATE ${t.name} SET ${this.stateSets(t, state, stamp, vals, params)}`
      + ` WHERE ${t.pk} = ${bind('__id', t.pkType)} AND ${column} IN (${allowed})`, params)).count;
    if (count === 0) {
      await this.one(t, out, id);   // 대상이 없으면 404
      throw new ApiError(409, 'INVALID_STATE', '지금 상태에서는 처리할 수 없습니다.');
    }
    return this.one(t, out, id);
  }

  /** API 하나가 여러 상태로 보내는 전환: 요청 값이 목표 상태이고, allowed는 목표 → 허용된 현재 상태다. 표에 없는 목표는 409다. */
  async moveTo(t: Table, column: string, allowed: Record<string, string[]>, to: unknown, id: unknown, out: Col[], stamp: Col[] = [],
               vals: Data = {}): Promise<Data> {
    const from = Object.prototype.hasOwnProperty.call(allowed, String(to)) ? allowed[String(to)] : undefined;
    if (!from) {
      throw new ApiError(409, 'INVALID_STATE', '지금 상태에서는 처리할 수 없습니다.');
    }
    return this.move(t, column, from, String(to), id, out, stamp, vals);
  }

  async delete(t: Table, id: unknown): Promise<Data> {
    const count = (await this.conn.run(`DELETE FROM ${t.name} WHERE ${t.pk} = ${bind('__id', t.pkType)}`, { __id: id })).count;
    if (count === 0) {
      throw ApiError.notFound();
    }
    return {};
  }

  async query(sql: string, params: Data = {}): Promise<Data[]> {
    return this.rows(sql, params);
  }

  async execute(sql: string, params: Data = {}): Promise<number> {
    return (await this.conn.run(sql, params)).count;
  }
}
