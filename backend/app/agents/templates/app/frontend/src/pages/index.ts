import type { ComponentType } from 'react'
import type { PageProps } from '../kit'

// 개발 Agent(LLM)가 기능 전체를 구현한 화면 (기능 ID → 화면). 여기 없는 기능은 공통 화면 틀(Screen.tsx)로 그린다. 코드가 만든 파일.
export const pages: Record<string, ComponentType<PageProps>> = {}
