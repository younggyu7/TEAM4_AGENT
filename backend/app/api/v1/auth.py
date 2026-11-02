"""회원가입·로그인·회원 관리.

  POST /auth/signup     회원가입 (처음 가입자는 관리자·팀장, 그 뒤는 승인 대기)
  POST /auth/login      로그인 → httpOnly 쿠키 세션
  POST /auth/logout     로그아웃
  GET  /auth/me         로그인한 회원·권한
  GET  /users           회원 목록 (관리자)
  POST /users/update    역할·승인·중지·관리자 지정 (관리자)
  POST /users/create    계정 바로 만들기 (관리자, 역할을 정해 승인된 상태)
  POST /users/password  비밀번호 초기화 (관리자)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from app.api.v1.deps import SESSION_COOKIE, admin_user, current_user
from app.core.auth import ROLES
from app.services import users

router = APIRouter(tags=["auth"])


class SignupBody(BaseModel):
    email: str
    name: str
    password: str
    requested_role: str = ""   # 희망 역할 (관리자 참고용, 바로 주지 않음)


class LoginBody(BaseModel):
    email: str
    password: str
    remember: bool = True


class MemberCreate(BaseModel):
    email: str
    name: str
    password: str
    role: str
    is_admin: bool = False


class PasswordReset(BaseModel):
    id: int
    password: str


class MemberUpdate(BaseModel):
    id: int
    role: str | None = None
    status: str | None = None     # active(승인) · disabled(중지) · pending
    is_admin: bool | None = None


@router.post("/auth/signup", summary="회원가입")
def signup(body: SignupBody):
    member = users.signup(body.email, body.name, body.password, body.requested_role)
    return {"user": member.to_dict(), "pending": member.status != "active"}


@router.post("/auth/login", summary="로그인 (httpOnly 쿠키 세션)")
def login(body: LoginBody, response: Response):
    member, token, max_age = users.login(body.email, body.password, body.remember)
    # SameSite=Strict: 다른 사이트에서 보낸 폼(업로드 등)에는 쿠키가 실리지 않는다. 개발 서버는 Vite proxy라 같은 출처
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True, samesite="strict", path="/")
    return {"user": member.to_dict()}


@router.post("/auth/logout", summary="로그아웃")
def logout(request: Request, response: Response):
    users.logout(request.cookies.get(SESSION_COOKIE, ""))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me", summary="로그인한 회원")
def me(member: users.Member = Depends(current_user)):
    return {"user": member.to_dict()}


@router.get("/auth/roles", summary="역할 목록")
def roles():
    return {"roles": [{"id": k, "label": v} for k, v in ROLES.items()]}


@router.get("/users", summary="회원 목록 (관리자)")
def list_users(_: users.Member = Depends(admin_user)):
    return {"users": [m.to_dict() for m in users.list_members()]}


@router.post("/users/update", summary="회원 역할·승인·중지 (관리자)")
def update_user(body: MemberUpdate, _: users.Member = Depends(admin_user)):
    return {"user": users.update_member(body.id, role=body.role, status=body.status, is_admin=body.is_admin).to_dict()}


@router.post("/users/create", summary="계정 바로 만들기 (관리자)")
def create_user(body: MemberCreate, _: users.Member = Depends(admin_user)):
    return {"user": users.create_member(body.email, body.name, body.password, body.role, body.is_admin).to_dict()}


@router.post("/users/password", summary="비밀번호 초기화 (관리자)")
def reset_password(body: PasswordReset, _: users.Member = Depends(admin_user)):
    users.set_password(body.id, body.password)
    return {"ok": True}
