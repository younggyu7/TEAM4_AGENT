/**
 * 현재 사용자: 포털 로그인(auth)의 세션에서 읽는다 (Authorization: Bearer 토큰). 로그인하지 않았으면 역할이 빈 값.
 * id는 시연 계정에 연결한 사용자 ID다 (accounts.json). 역할 헤더 같은 요청 값은 믿지 않는다.
 */
import { timingSafeEqual } from 'crypto';
import type { Request } from 'express';

import { ApiError } from './errors';

export const ALL = '전체 보기';

export class CurrentUser {
  /** system: X-Api-Key가 SYSTEM_API_KEY와 같다 */
  constructor(readonly id: string | null, readonly role: string, readonly system = false) {}

  isSystem(): boolean {
    return this.system;
  }

  requireSystem(): void {
    if (!this.system) {
      throw new ApiError(401, 'UNAUTHORIZED', '외부 시스템 인증이 필요합니다.');
    }
  }

  requireLogin(): void {
    if (!this.role) {
      throw new ApiError(401, 'UNAUTHORIZED', '인증이 필요합니다.');
    }
  }

  require(...roles: string[]): void {
    this.requireLogin();
    if (this.role !== ALL && !roles.includes(this.role)) {
      throw new ApiError(403, 'FORBIDDEN', '이 역할은 쓸 수 없습니다: ' + this.role);
    }
  }
}

/** 외부 시스템의 호출인가: X-Api-Key가 환경 변수 SYSTEM_API_KEY와 같아야 한다. 키를 정하지 않으면 아무도 통과하지 못한다. */
export function systemCall(req: Request): boolean {
  const key = Buffer.from(process.env.SYSTEM_API_KEY || '', 'utf8');
  const header = req.headers['x-api-key'];
  if (!key.length || typeof header !== 'string') {
    return false;
  }
  const given = Buffer.from(header, 'utf8');
  return given.length === key.length && timingSafeEqual(given, key);
}

/** 로그인 미들웨어(main.ts)가 붙인 사용자. */
export function currentUser(req: Request): CurrentUser {
  const found = (req as { appUser?: CurrentUser }).appUser;
  return found || new CurrentUser(null, '', systemCall(req));
}
