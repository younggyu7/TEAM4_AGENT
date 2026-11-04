/** 오류를 openapi.yaml의 Error 형식 {code, message}로 바꾼다. 내부 메시지는 응답에 넣지 않는다. */
import { ArgumentsHost, Catch, ExceptionFilter, HttpException } from '@nestjs/common';
import type { Response } from 'express';

import { ApiError } from './errors';

// MySQL: 필수 값(1048)·중복(1062)·참조(1451·1452·1216·1217), 값 조건(CHECK 3819)·맞지 않는 값(1366·1292·1265)·너무 긴 값(1406)·범위(1264)
const MYSQL_VALUE_ERRORS = new Set([1048, 1062, 1451, 1452, 1216, 1217, 3819, 1366, 1292, 1265, 1406, 1264]);

function describe(e: unknown): [number, string, string] {
  if (e instanceof ApiError) {
    return [e.status, e.code, e.message];
  }
  if (e instanceof HttpException) {
    const status = e.getStatus();
    const known: Record<number, [string, string]> = { 404: ['NOT_FOUND', '대상을 찾을 수 없습니다.'], 405: ['METHOD_NOT_ALLOWED', '허용되지 않은 메서드입니다.'] };
    const [code, message] = known[status] || ['HTTP_ERROR', e.message];
    return [status, code, message];
  }
  const db = e as { code?: unknown; errno?: unknown; sqlState?: unknown };
  // PostgreSQL: 23… 무결성(필수·중복·참조·CHECK), 22… 값 오류(형식·길이·범위)
  const pgValue = typeof db.code === 'string' && /^2[23][0-9A-Z]{3}$/.test(db.code) && db.sqlState === undefined;
  if (pgValue || (typeof db.errno === 'number' && db.sqlState !== undefined && MYSQL_VALUE_ERRORS.has(db.errno))) {
    return [400, 'VALIDATION_FAILED', '필수 값이나 값의 조건이 맞지 않습니다.'];
  }
  return [500, 'INTERNAL_ERROR', '서버 오류가 발생했습니다.'];
}

@Catch()
export class ErrorFilter implements ExceptionFilter {
  catch(e: unknown, host: ArgumentsHost): void {
    const [status, code, message] = describe(e);
    if (status === 500) {
      console.error('처리하지 못한 오류', e);
    }
    const res = host.switchToHttp().getResponse<Response>();
    if (!res.headersSent) {
      res.status(status).json({ code, message });
    }
  }
}
