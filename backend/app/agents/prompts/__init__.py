"""Agent 프롬프트: agents/prompts/<Agent>/<이름>.md 하나에 프롬프트 하나.

프롬프트를 고칠 때는 코드가 아니라 이 폴더의 .md만 고친다. 불러온 문자열이 그대로 LLM 시스템 프롬프트가 된다.
  - 파일 끝의 줄바꿈 하나는 편집기용이라 불러올 때 지운다 (프롬프트 끝에 줄바꿈이 필요하면 빈 줄을 하나 더 둔다).
  - Windows 줄바꿈(\\r\\n)은 \\n으로 읽는다 (운영체제와 관계없이 같은 문자열 → 프롬프트 캐시·manual 응답 재생 키가 같다).
  - {limit} 같은 자리 표시는 쓰는 코드가 format으로 채운다 (semantic_reviewer/common_rules.md).
"""
from __future__ import annotations

from functools import cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent


@cache
def load_prompt(name: str) -> str:
    """프롬프트 하나를 읽는다. name은 'function_designer/system'처럼 이 폴더 기준 경로 (.md 제외)."""
    with open(PROMPTS_DIR / f"{name}.md", encoding="utf-8") as f:
        text = f.read()
    return text[:-1] if text.endswith("\n") else text
