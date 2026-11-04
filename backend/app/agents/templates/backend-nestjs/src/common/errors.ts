/** API 오류. 응답은 openapi.yaml의 Error 형식 {code, message}. */
export class ApiError extends Error {
  constructor(readonly status: number, readonly code: string, message: string) {
    super(message);
  }

  static notImplemented(what: string): ApiError {
    return new ApiError(501, 'NOT_IMPLEMENTED', '구현 전: ' + what);
  }

  static notFound(): ApiError {
    return new ApiError(404, 'NOT_FOUND', '대상을 찾을 수 없습니다.');
  }
}

/** 기본 동작을 만들 수 없는 API (crud 자리에서 쓴다). */
export async function notImplemented(what: string): Promise<never> {
  throw ApiError.notImplemented(what);
}
