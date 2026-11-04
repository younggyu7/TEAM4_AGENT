/** 서버 모듈: 로그인(/api/auth) + 화면 보조 API(/api/ui)·기능별 API(/api/v1, src/api/index.ts) + 화면(맨 뒤). */
import { Module } from '@nestjs/common';

import { CONTROLLERS } from './api';
import { AuthController } from './auth/auth.controller';
import { WebController } from './web.controller';

@Module({ controllers: [AuthController, ...CONTROLLERS, WebController] })
export class AppModule {}
