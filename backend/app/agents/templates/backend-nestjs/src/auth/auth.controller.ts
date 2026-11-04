/** 포털 로그인 (서비스 틀). 스펙의 업무 API(/api/v1)와 따로 둔다. */
import { Controller, Get, Header, HttpCode, Post, Req } from '@nestjs/common';
import type { Request } from 'express';

import { ApiError } from '../common/errors';
import * as accounts from './accounts';

@Controller('api/auth')
export class AuthController {
  @Post('login')
  @HttpCode(200)
  @Header('Cache-Control', 'no-store')
  async login(@Req() req: Request) {
    const body = req.body as Record<string, unknown> | undefined;
    const fields = body && typeof body === 'object' && !Array.isArray(body) ? [body.loginId, body.password] : null;
    if (!fields || fields.some((v) => v !== undefined && v !== null && typeof v !== 'string')) {
      throw new ApiError(400, 'VALIDATION_FAILED', '요청 형식이 올바르지 않습니다.');
    }
    return accounts.login((fields[0] as string) ?? null, (fields[1] as string) ?? null);
  }

  @Get('me')
  @Header('Cache-Control', 'no-store')
  me(@Req() req: Request) {
    const found = accounts.me(accounts.token(req));
    if (!found) {
      throw new ApiError(401, 'UNAUTHORIZED', '다시 로그인하세요.');
    }
    return found;
  }

  @Post('logout')
  @HttpCode(204)
  logout(@Req() req: Request): void {
    accounts.logout(accounts.token(req));
  }
}
