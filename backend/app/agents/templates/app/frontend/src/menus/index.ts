import type { ComponentType } from 'react'
import type { MenuProps } from '../menu'

// 메뉴 화면 목록 (메뉴 그룹 → 화면 파일). 여기 없는 메뉴는 기본 메뉴 화면(menu.tsx)으로 그린다. 코드가 만든 파일.
export const menus: Record<string, ComponentType<MenuProps>> = {}
