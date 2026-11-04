/** 요청 값 검사: 스펙의 필수 값·허용값을 코드가 만든 API가 부른다. */
import type { Data } from './db';
import { ApiError } from './errors';

function invalid(): ApiError {
  return new ApiError(400, 'VALIDATION_FAILED', '요청 형식이 올바르지 않습니다.');
}

/** 요청 본문을 고칠 수 있는 입력 묶음으로 바꾼다 (업무 로직이 값을 채울 수 있다). 객체가 아니면 400. */
export function inputOf(values: unknown): Data {
  if (values === undefined || values === null) {
    return {};
  }
  if (typeof values !== 'object' || Array.isArray(values)) {
    throw invalid();
  }
  return { ...(values as Data) };
}

/** 쿼리 값 (같은 이름이 여러 번이면 마지막 값). */
export function queryOf(values: unknown): Data {
  const out: Data = {};
  for (const [name, value] of Object.entries(inputOf(values))) {
    out[name] = Array.isArray(value) ? value[value.length - 1] : value;
  }
  return out;
}

export function required(data: Data, ...names: string[]): void {
  for (const name of names) {
    if (data[name] === undefined || data[name] === null || data[name] === '') {
      throw new ApiError(400, 'VALIDATION_FAILED', '필수 값이 없습니다: ' + name);
    }
  }
}

export function oneOf(data: Data, name: string, ...allowed: string[]): void {
  const value = data[name];
  if (value !== undefined && value !== null && !allowed.includes(String(value))) {
    throw new ApiError(400, 'VALIDATION_FAILED', '허용되지 않은 값입니다: ' + name);
  }
}
