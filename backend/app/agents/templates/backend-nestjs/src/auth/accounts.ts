/**
 * 시연용 로그인 계정과 세션. 계정은 src/accounts.json (스펙의 역할 목록에서 코드가 만든 것).
 *
 * - 계정은 업무 테이블과 섞이지 않게 app_auth 스키마에 둔다 (MySQL은 스키마가 곧 DB라 앱 DB 안의 app_auth_account 테이블).
 * - 처음 만드는 계정의 비밀번호는 DEMO_PASSWORD 또는 임의 값이다. 임의 값은 LOCAL_DIR/accounts.txt에만 적고 로그·응답에 남기지 않는다.
 * - 이미 있는 계정의 비밀번호는 다시 시작해도 바꾸지 않는다 (RESET_PASSWORDS=true면 모두 새로 만든다).
 * - 세션은 서버 메모리에 둔다. 서버를 다시 시작하면 다시 로그인한다.
 */
import { pbkdf2Sync, randomBytes, randomInt, timingSafeEqual } from 'crypto';
import type { Request } from 'express';
import { appendFileSync, existsSync, mkdirSync, writeFileSync } from 'fs';
import { join, resolve } from 'path';

import ACCOUNTS from '../accounts.json';
import { connect, mysql, transaction } from '../common/db';
import { ApiError } from '../common/errors';
import { CurrentUser } from '../common/user';

const ITERATIONS = 120_000;
const LETTERS = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';
const HEAD = '# 시연 계정 (아이디\t역할\t비밀번호). 새로 만든 계정만 적힌다. 이 파일은 공유하지 않는다.\n';

interface Session {
  user: CurrentUser;
  loginId: string;
  expires: number;
}

const sessions = new Map<string, Session>();   // 토큰 → 세션

function derive(password: string, salt: Buffer, iterations: number): Buffer {
  return pbkdf2Sync(password, salt, iterations, 32, 'sha256');
}

/** Spring 틀(Accounts.java)과 같은 형식: pbkdf2$반복$소금$해시 */
export function hashPassword(password: string): string {
  const salt = randomBytes(16);
  return `pbkdf2$${ITERATIONS}$${salt.toString('hex')}$${derive(password, salt, ITERATIONS).toString('hex')}`;
}

export function verify(password: string, stored: string): boolean {
  const parts = stored.split('$');
  if (parts.length !== 4 || parts[0] !== 'pbkdf2') {
    return false;
  }
  const want = Buffer.from(parts[3], 'hex');
  const got = derive(password, Buffer.from(parts[2], 'hex'), Number(parts[1]));
  return want.length === got.length && timingSafeEqual(want, got);
}

const DUMMY = hashPassword('not-a-password');

function table(): string {
  return mysql() ? 'app_auth_account' : 'app_auth.account';
}

function newPassword(): string {
  return Array.from({ length: 12 }, () => LETTERS[randomInt(LETTERS.length)]).join('');
}

/** 서버를 켤 때: 계정 테이블을 만들고, 없는 계정을 만든다. DB가 뜰 때까지 잠시 기다린다. */
export async function setup(): Promise<void> {
  for (let attempt = 0; ; attempt++) {
    try {
      const conn = await connect();
      try {
        await conn.run('SELECT 1');
      } finally {
        conn.release();
      }
      break;
    } catch (e) {   // DB가 아직 뜨지 않음
      if (attempt === 29) {
        throw e;
      }
      await new Promise((done) => setTimeout(done, 1000));
    }
  }
  const demo = process.env.DEMO_PASSWORD || '';
  const reset = (process.env.RESET_PASSWORDS || 'false').toLowerCase() === 'true';
  const created: string[] = [];
  const name = table();
  await transaction(async (db) => {
    if (!mysql()) {
      await db.execute('CREATE SCHEMA IF NOT EXISTS app_auth');
    }
    const stamp = mysql() ? 'datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)' : 'timestamptz NOT NULL DEFAULT now()';
    await db.execute(`CREATE TABLE IF NOT EXISTS ${name} (`
      + 'login_id varchar(50) PRIMARY KEY, role varchar(100) NOT NULL, user_id varchar(50) NOT NULL, '
      + `password_hash varchar(200) NOT NULL, created_at ${stamp})`);
    if (reset) {
      await db.execute(`DELETE FROM ${name}`);
    }
    for (const a of ACCOUNTS) {
      const found = (await db.query(`SELECT login_id FROM ${name} WHERE login_id = :id`, { id: a.loginId })).length;
      if (found) {
        await db.execute(`UPDATE ${name} SET role = :role, user_id = :uid WHERE login_id = :id`, { role: a.role, uid: a.userId, id: a.loginId });
        continue;
      }
      const password = demo || newPassword();
      await db.execute(`INSERT INTO ${name} (login_id, role, user_id, password_hash) VALUES (:id, :role, :uid, :hash)`,
        { id: a.loginId, role: a.role, uid: a.userId, hash: hashPassword(password) });
      created.push(`${a.loginId}\t${a.role}\t${demo ? '(DEMO_PASSWORD로 정한 값)' : password}`);
    }
    const ids = ACCOUNTS.map((a) => a.loginId);
    for (const row of await db.query(`SELECT login_id FROM ${name}`)) {
      if (!ids.includes(String(row.login_id))) {   // 스펙에서 사라진 역할의 계정
        await db.execute(`DELETE FROM ${name} WHERE login_id = :id`, { id: row.login_id });
      }
    }
  });
  if (created.length) {
    const folder = process.env.LOCAL_DIR || 'local';
    mkdirSync(folder, { recursive: true });
    const path = join(folder, 'accounts.txt');
    const head = existsSync(path) && !reset ? '' : HEAD;
    (reset ? writeFileSync : appendFileSync)(path, head + created.join('\n') + '\n', 'utf8');
    console.log(`시연 계정 ${created.length}개를 만들었습니다. 아이디와 비밀번호는 ${resolve(path)}에 있습니다.`);
  }
}

/** Authorization: Bearer 토큰 */
export function token(req: Request): string | null {
  const header = req.headers.authorization;
  return header && header.startsWith('Bearer ') ? header.slice(7).trim() : null;
}

/** 로그인: 맞으면 세션 토큰과 사용자 정보 */
export async function login(loginId: string | null, password: string | null): Promise<Record<string, string>> {
  const rows = await transaction((db) => db.query(`SELECT role, user_id, password_hash FROM ${table()} WHERE login_id = :id`, { id: loginId || '' }));
  const row = rows[0];
  const matches = verify(password || '', row ? String(row.password_hash) : DUMMY);   // 없는 아이디도 같은 시간이 걸리게 한다
  if (!row || !matches) {
    throw new ApiError(401, 'UNAUTHORIZED', '아이디 또는 비밀번호를 확인하세요.');
  }
  const user = new CurrentUser(String(row.user_id), String(row.role));
  const hours = Math.max(1, Number(process.env.SESSION_HOURS || '8') || 8);
  const now = Date.now();
  for (const [key, s] of sessions) {
    if (s.expires < now) {
      sessions.delete(key);
    }
  }
  const key = randomBytes(32).toString('base64url');
  sessions.set(key, { user, loginId: loginId as string, expires: now + hours * 3600_000 });
  return { token: key, loginId: loginId as string, role: user.role, userId: user.id as string };
}

function session(key: string | null): Session | null {
  const s = key ? sessions.get(key) : undefined;
  if (s && s.expires < Date.now()) {
    sessions.delete(key as string);
    return null;
  }
  return s || null;
}

/** 토큰의 사용자 (없거나 만료면 null) */
export function user(key: string | null): CurrentUser | null {
  return session(key)?.user || null;
}

export function me(key: string | null): Record<string, string | null> | null {
  const s = session(key);
  return s ? { loginId: s.loginId, role: s.user.role, userId: s.user.id } : null;
}

export function logout(key: string | null): void {
  if (key) {
    sessions.delete(key);
  }
}
