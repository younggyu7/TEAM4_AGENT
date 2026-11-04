"""회원 역할과 권한, 비밀번호 해시 (화면 요청만 검사한다. CLI는 로컬 운영자라 검사하지 않는다).

역할별로 할 수 있는 일 (회사 예: 기획 파트가 기능 명세, 데이터 파트가 ERD, 팀장이 최종 산출물):
    upload        새 문서 업로드        기획 파트원·기획 파트장·팀장
    spec_edit     기능 명세 수정·AI 반영 기획 파트원·기획 파트장·팀장
    spec_confirm  기능 명세 확정(내보내기) 기획 파트장·팀장
    erd_edit      ERD 수정·지적 처리·저장 데이터 파트원·데이터 파트장·팀장
    erd_confirm   ERD 최종 확정          데이터 파트장·팀장
    final_decide  최종 승인·수정 지시·기술 스택 팀장
    develop       최종 개발하기·실행·git 올리기 개발 파트장·팀장 (개발 파트원은 알림·미리보기)
보기는 승인된 회원 누구나 할 수 있다. 회원 승인·역할 지정은 관리자(is_admin)가 한다.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

ROLES = {
    "planner": "기획 파트원",
    "planner_head": "기획 파트장",
    "data": "데이터 파트원",
    "data_head": "데이터 파트장",
    "dev": "개발 파트원",
    "dev_head": "개발 파트장",
    "lead": "팀장",
}

PERMISSIONS: dict[str, tuple[set[str], str]] = {
    "upload": ({"planner", "planner_head", "lead"}, "새 문서 업로드는 기획 파트원·기획 파트장·팀장만 할 수 있습니다."),
    "spec_edit": ({"planner", "planner_head", "lead"}, "기능 명세 수정은 기획 파트원·기획 파트장·팀장만 할 수 있습니다."),
    "spec_confirm": ({"planner_head", "lead"}, "기능 명세 확정(내보내기)은 기획 파트장·팀장만 할 수 있습니다."),
    "erd_edit": ({"data", "data_head", "lead"}, "ERD 수정은 데이터 파트원·데이터 파트장·팀장만 할 수 있습니다."),
    "erd_confirm": ({"data_head", "lead"}, "ERD 최종 확정은 데이터 파트장·팀장만 할 수 있습니다."),
    "final_decide": ({"lead"}, "최종 승인·수정 지시와 기술 스택은 팀장만 정할 수 있습니다."),
    "develop": ({"dev_head", "lead"}, "최종 개발하기·실행·git 올리기는 개발 파트장·팀장만 할 수 있습니다."),
    "code_edit": ({"dev", "dev_head", "lead"}, "최종 개발물 코드 고치기는 개발 파트원·개발 파트장·팀장만 할 수 있습니다."),
}


def allowed(role: str, permission: str) -> bool:
    return role in PERMISSIONS[permission][0]


def permissions_of(role: str) -> list[str]:
    return [p for p, (roles, _) in PERMISSIONS.items() if role in roles]


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
