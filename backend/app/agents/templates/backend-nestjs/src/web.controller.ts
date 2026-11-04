/** 빌드한 화면 (STATIC_DIR, 기본 ./static). 없는 파일은 index.html (화면 안의 주소). /api 아래는 404 (경로는 있고 메서드가 다르면 405). */
import { All, Controller, Req, Res } from '@nestjs/common';
import type { Request, Response } from 'express';
import { statSync } from 'fs';
import { join, resolve, sep } from 'path';

import { ApiError } from './common/errors';

const STATIC = resolve(process.env.STATIC_DIR || join(__dirname, '..', 'static'));

function isFile(path: string): boolean {
  try {
    return statSync(path).isFile();
  } catch {
    return false;
  }
}

interface Layer {
  route?: { path: string };
  match(path: string): boolean;
}

@Controller()
export class WebController {
  @All('*')
  web(@Req() req: Request, @Res() res: Response): void {
    let path: string;
    try {
      path = decodeURIComponent(req.path).replace(/^\/+/, '');
    } catch {
      throw ApiError.notFound();
    }
    if (path === 'api' || path.startsWith('api/')) {
      const stack: Layer[] = (req.app as unknown as { _router: { stack: Layer[] } })._router.stack;
      if (stack.some((layer) => layer.route && !layer.route.path.includes('*') && layer.match(req.path))) {
        throw new ApiError(405, 'METHOD_NOT_ALLOWED', '허용되지 않은 메서드입니다.');
      }
      throw ApiError.notFound();
    }
    if (req.method !== 'GET' && req.method !== 'HEAD') {
      throw new ApiError(405, 'METHOD_NOT_ALLOWED', '허용되지 않은 메서드입니다.');
    }
    const target = resolve(STATIC, path);
    if (path && target.startsWith(STATIC + sep) && isFile(target)) {
      res.sendFile(target);
    } else if (isFile(join(STATIC, 'index.html'))) {
      res.sendFile(join(STATIC, 'index.html'));
    } else {
      throw ApiError.notFound();
    }
  }
}
