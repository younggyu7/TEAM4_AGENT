/**
 * 서버 시작점: 업무 API(/api/v1)·화면 보조 API(/api/ui)·로그인(/api/auth)과 화면(static, SPA).
 *
 * node dist/main.js (포트 8080). 환경 변수: DB_URL, DEMO_PASSWORD, RESET_PASSWORDS, SYSTEM_API_KEY, LOCAL_DIR,
 * ACCESS_LOG_DIR(요청 한 줄씩 기록, 실행 미리보기가 쓰는 중인지 본다), STATIC_DIR(빌드한 화면, 기본 ./static).
 */
import 'reflect-metadata';

import { NestFactory } from '@nestjs/core';
import { json, NextFunction, Request, Response } from 'express';
import { appendFileSync, mkdirSync } from 'fs';
import { join } from 'path';

import { AppModule } from './app.module';
import * as accounts from './auth/accounts';
import { ErrorFilter } from './common/filter';
import { CurrentUser, systemCall } from './common/user';

/** ACCESS_LOG_DIR이 있으면 요청 한 줄을 남긴다. 기록하지 못해도 요청은 처리한다. */
function access(req: Request, status: number): void {
  const folder = process.env.ACCESS_LOG_DIR;
  if (!folder) {
    return;
  }
  const d = new Date();
  const two = (n: number) => String(n).padStart(2, '0');
  const stamp = `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())}T${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
  try {
    mkdirSync(folder, { recursive: true });
    appendFileSync(join(folder, 'access.log'), `${stamp} ${req.method} ${req.originalUrl.split('?')[0]} ${status}\n`, 'utf8');
  } catch {
    // 기록하지 못해도 된다
  }
}

/** 같은 모양의 경로는 글자 경로를 변수 경로보다 먼저 (Spring처럼 /orders/summary가 /orders/:id보다 먼저). 화면(*)은 맨 뒤. */
function specific(path: string): number[] {
  if (path.includes('*')) {
    return [1];
  }
  return [0, ...path.replace(/^\/+|\/+$/g, '').split('/').map((part) => (part.startsWith(':') ? 1 : 0))];
}

function compare(a: number[], b: number[]): number {
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    if (a[i] !== b[i]) {
      return a[i] - b[i];
    }
  }
  return a.length - b.length;
}

function sortRoutes(stack: { route?: { path: string } }[]): void {
  const slots = stack.map((layer, i) => (layer.route ? i : -1)).filter((i) => i >= 0);
  const routes = slots.map((i) => stack[i]).sort((a, b) => compare(specific(a.route!.path), specific(b.route!.path)));
  slots.forEach((slot, n) => {
    stack[slot] = routes[n];
  });
}

async function main(): Promise<void> {
  await accounts.setup();
  const app = await NestFactory.create(AppModule, { bodyParser: false, logger: ['error', 'warn'] });
  const http = app.getHttpAdapter().getInstance();
  http.disable('x-powered-by');
  http.set('query parser', 'simple');
  app.use(json({ limit: '5mb' }));
  app.use((err: { type?: string }, _req: Request, res: Response, next: NextFunction) => {
    if (err && err.type === 'entity.parse.failed') {
      res.status(400).json({ code: 'VALIDATION_FAILED', message: '요청 형식이 올바르지 않습니다.' });
      return;
    }
    next(err);
  });
  // 요청의 로그인 사용자를 붙인다. 막는 일은 하지 않는다: 각 API가 user.require…로 확인한다 (외부 시스템은 X-Api-Key)
  app.use((req: Request, res: Response, next: NextFunction) => {
    const found = accounts.user(accounts.token(req));
    const system = systemCall(req);
    (req as { appUser?: CurrentUser }).appUser = found ? new CurrentUser(found.id, found.role, system) : new CurrentUser(null, '', system);
    res.on('finish', () => access(req, res.statusCode));
    next();
  });
  app.useGlobalFilters(new ErrorFilter());
  await app.init();
  sortRoutes(http._router.stack);
  const port = Number(process.env.PORT || 8080);
  await app.listen(port, '0.0.0.0');
  console.log(`서버가 ${port} 포트에서 시작했습니다.`);
}

main().catch((e) => {
  console.error('서버를 시작하지 못했습니다', e);
  process.exit(1);
});
