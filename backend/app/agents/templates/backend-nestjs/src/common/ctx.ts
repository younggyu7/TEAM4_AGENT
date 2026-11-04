/** 업무 로직에 넘기는 실행 정보: DB와 현재 사용자. */
import type { Db } from './db';
import { ApiError } from './errors';
import type { CurrentUser } from './user';

export class Ctx {
  constructor(readonly db: Db, readonly user: CurrentUser) {}

  fail(status: number, code: string, message: string): ApiError {
    return new ApiError(status, code, message);
  }
}
