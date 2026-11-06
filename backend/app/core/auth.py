"""회원 역할과 권한, 비밀번호 해시 (화면 요청만 검사한다. CLI는 로컬 운영자라 검사하지 않는다).

역할은 PM과 개발자 둘이다 (기획·데이터·개발 파트로 나누지 않는다). 역할별로 할 수 있는 일:
    upload         새 문서 업로드                          PM·개발자
    spec_edit      기능 명세 수정·AI 반영                   PM·개발자
    spec_confirm   기능 명세 확정(내보내기)                 PM (개발자는 '수정 완료 승인 요청'을 보낸다)
    erd_edit       ERD 수정·지적 처리·저장                  PM·개발자
    erd_confirm    ERD 최종 확정                            PM·개발자 (승인 요청 없이)
    final_decide   최종 승인·수정 지시·기술 스택            PM·개발자
    develop        최종 개발하기·합치기·반려·원격 올리기     개발자
    preview_stop   실행 미리보기 끄기                       PM·개발자
    repo_write     최종 개발물 브랜치·고치기·저장하기·합치기 요청  개발자
    app_accounts   앱 시연 계정 보기                        PM·개발자
    assign_members 프로젝트 담당자 바꾸기                   PM
    project_delete·project_rename  프로젝트 삭제·이름 바꾸기  PM
보기는 그 프로젝트의 담당자가 한다 (PM도 고른 PM만, PM 최소 한 명: services/project_access). 회원 승인·역할 지정은 관리자(is_admin)가 한다.
예전 역할(팀장·기획/데이터/개발 파트장·파트원)은 DB 마이그레이션이 바꾸고, 저장된 기록(변경 내역·알림·승인)에 남은 예전 역할 값은
role_of로 새 역할로 읽는다.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

ROLES = {
    "pm": "PM",
    "developer": "개발자",
}
PM, DEVELOPER = "pm", "developer"
# 예전 역할 → 새 역할 (예전 DB 값과 저장된 기록을 읽을 때)
LEGACY_ROLES = {"lead": PM, "planner": DEVELOPER, "planner_head": DEVELOPER, "data": DEVELOPER, "data_head": DEVELOPER,
                "dev": DEVELOPER, "dev_head": DEVELOPER}
_PM, _DEV, _BOTH = {PM}, {DEVELOPER}, {PM, DEVELOPER}

PERMISSIONS: dict[str, tuple[set[str], str]] = {
    "upload": (_BOTH, "새 문서 업로드는 PM·개발자만 할 수 있습니다."),
    "spec_edit": (_BOTH, "기능 명세 수정은 PM·개발자만 할 수 있습니다."),
    "spec_confirm": (_PM, "기능 명세 확정(내보내기)은 PM만 할 수 있습니다. 개발자는 '수정 완료 승인 요청'을 보내 주세요."),
    "erd_edit": (_BOTH, "ERD 수정은 PM·개발자만 할 수 있습니다."),
    "erd_confirm": (_BOTH, "ERD 최종 확정은 PM·개발자만 할 수 있습니다."),
    "final_decide": (_BOTH, "최종 승인·수정 지시와 기술 스택은 PM·개발자만 정할 수 있습니다."),
    "develop": (_DEV, "최종 개발하기·합치기·원격 올리기는 개발자만 할 수 있습니다."),
    "preview_stop": (_BOTH, "실행 미리보기 끄기는 PM·개발자만 할 수 있습니다."),
    "repo_write": (_DEV, "최종 개발물 브랜치·코드 고치기·저장하기·합치기 요청은 개발자만 할 수 있습니다."),
    "app_accounts": (_BOTH, "앱 시연 계정은 PM·개발자만 볼 수 있습니다."),
    "assign_members": (_PM, "담당자는 PM만 바꿀 수 있습니다."),
    "project_delete": (_PM, "프로젝트 삭제는 PM만 할 수 있습니다."),
    "project_rename": (_PM, "프로젝트 이름은 PM만 바꿀 수 있습니다."),
}


def role_of(role: str | None) -> str:
    """예전 역할 값을 새 역할로 (새 역할·빈 값은 그대로)."""
    return LEGACY_ROLES.get(role or "", role or "")


def role_label(role: str | None) -> str:
    return ROLES.get(role_of(role), "역할 없음")


def allowed(role: str, permission: str) -> bool:
    return role_of(role) in PERMISSIONS[permission][0]


def permissions_of(role: str) -> list[str]:
    return [p for p, (roles, _) in PERMISSIONS.items() if role_of(role) in roles]


def denied_message(permission: str) -> str:
    return PERMISSIONS[permission][1]


# 비밀번호: 표준 라이브러리 scrypt (솔트 16바이트). 저장 형식 'scrypt$<솔트 hex>$<해시 hex>'
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, salt, digest = stored.split("$")
    except ValueError:
        return False
    if kind != "scrypt":
        return False
    got = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), **_SCRYPT)
    return hmac.compare_digest(got.hex(), digest)


def new_token() -> tuple[str, str]:
    """(쿠키에 줄 토큰 원문, DB에 둘 해시)"""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
