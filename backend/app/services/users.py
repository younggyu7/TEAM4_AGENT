"""회원·로그인 세션 저장 (서비스 DB가 있으면 PostgreSQL의 users·user_sessions, 없으면 OUTPUT_DIR/service.sqlite).

- 처음 가입한 사람은 관리자·팀장으로 바로 쓸 수 있다 (회원을 승인할 사람이 있어야 하므로).
- 그 뒤 가입은 '승인 대기'이고, 관리자가 역할을 정해 승인해야 로그인된다.
- 관리자가 한 명도 남지 않게 바꾸는 것은 막는다.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import cache

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.auth import ROLES, hash_password, new_token, token_hash, verify_password
from app.core.config import get_settings
from app.core.database import sqlalchemy_url
from app.core.exceptions import BadRequest, Conflict, Forbidden, NotFound, Unauthorized
from app.models.user import User, UserSession

SESSION_DAYS = 7          # '로그인 유지'를 고르면 쿠키·세션을 이만큼 둔다
SESSION_HOURS = 12        # 고르지 않으면 브라우저를 닫을 때까지(쿠키) · 서버는 이 시간까지


@dataclass(frozen=True)
class Member:
    """화면·권한 검사에 쓰는 회원 정보 (비밀번호 해시 없음)"""
    id: int
    email: str
    name: str
    role: str
    requested_role: str
    is_admin: bool
    status: str
    created_at: str

    def to_dict(self) -> dict:
        from app.core.auth import permissions_of
        return {"id": self.id, "email": self.email, "name": self.name, "role": self.role,
                "role_label": ROLES.get(self.role, "역할 없음"), "requested_role": self.requested_role,
                "is_admin": self.is_admin, "status": self.status, "created_at": self.created_at,
                "permissions": permissions_of(self.role) if self.status == "active" else []}


def _member(u: User) -> Member:
    return Member(u.id, u.email, u.name, u.role, u.requested_role, u.is_admin, u.status,
                  u.created_at.isoformat(timespec="seconds") if u.created_at else "")


@cache
def _sessions(url: str) -> sessionmaker[Session]:
    engine = create_engine(sqlalchemy_url(url), pool_pre_ping=True)
    if url.startswith("sqlite"):   # 서비스 DB가 없을 때(로컬·테스트)는 회원 테이블만 바로 만든다. PostgreSQL은 Alembic
        User.metadata.create_all(engine, tables=[User.__table__, UserSession.__table__])
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def _db() -> Iterator[Session]:
    s = get_settings()
    url = s.database_url or f"sqlite:///{(s.output_dir / 'service.sqlite').resolve()}"
    if not s.database_url:
        s.output_dir.mkdir(parents=True, exist_ok=True)
    session = _sessions(url)()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)   # SQLite는 시간대를 지운다


def signup(email: str, name: str, password: str, requested_role: str = "") -> Member:
    email, name = email.strip().lower(), name.strip()
    if "@" not in email or len(email) > 200:
        raise BadRequest("이메일 형식이 아닙니다.")
    if not name or len(name) > 100:
        raise BadRequest("이름을 1~100자로 적어 주세요.")
    if len(password) < 8:
        raise BadRequest("비밀번호는 8자 이상이어야 합니다.")
    if requested_role and requested_role not in ROLES:
        raise BadRequest("희망 역할이 올바르지 않습니다.")
    with _db() as db:
        if db.scalar(select(User).where(User.email == email)):
            raise Conflict("이미 가입한 이메일입니다.")
        first = not db.scalar(select(func.count()).select_from(User))
        user = User(email=email, name=name, password_hash=hash_password(password), requested_role=requested_role,
                    role="lead" if first else "", is_admin=first, status="active" if first else "pending")
        db.add(user)
        db.flush()
        return _member(user)


def create_member(email: str, name: str, password: str, role: str, is_admin: bool = False) -> Member:
    """관리자 페이지: 가입 절차 없이 역할을 정한 계정을 바로 만든다 (승인된 상태)."""
    if role not in ROLES:
        raise BadRequest("역할이 올바르지 않습니다.")
    member = signup(email, name, password)
    return update_member(member.id, role=role, status="active", is_admin=is_admin or member.is_admin)


def set_password(user_id: int, password: str) -> None:
    """관리자 페이지: 비밀번호 초기화. 그 회원의 로그인은 끊는다 (새 비밀번호로 다시 로그인)."""
    if len(password) < 8:
        raise BadRequest("비밀번호는 8자 이상이어야 합니다.")
    with _db() as db:
        user = db.get(User, user_id)
        if user is None:
            raise NotFound("회원을 찾을 수 없습니다.")
        user.password_hash = hash_password(password)
        db.execute(delete(UserSession).where(UserSession.user_id == user.id))


def login(email: str, password: str, remember: bool) -> tuple[Member, str, int | None]:
    """(회원, 쿠키에 줄 토큰, 쿠키 유지 초 — None이면 브라우저를 닫을 때까지)"""
    with _db() as db:
        user = db.scalar(select(User).where(User.email == email.strip().lower()))
        if user is None or not verify_password(password, user.password_hash):
            raise Unauthorized("이메일 또는 비밀번호가 맞지 않습니다.")
        if user.status == "pending":
            raise Forbidden("가입 승인 대기 중입니다. 관리자가 역할을 정해 승인하면 로그인할 수 있습니다.")
        if user.status != "active":
            raise Forbidden("사용이 중지된 계정입니다. 관리자에게 문의하세요.")
        token, hashed = new_token()
        life = timedelta(days=SESSION_DAYS) if remember else timedelta(hours=SESSION_HOURS)
        db.add(UserSession(token_hash=hashed, user_id=user.id, expires_at=_now() + life))
        db.execute(delete(UserSession).where(UserSession.user_id == user.id, UserSession.expires_at < _now()))
        return _member(user), token, int(life.total_seconds()) if remember else None


def member_by_token(token: str) -> Member | None:
    """쿠키 토큰의 회원 (만료·없음·승인 안 된 계정이면 None)"""
    if not token:
        return None
    with _db() as db:
        sess = db.get(UserSession, token_hash(token))
        if sess is None or _aware(sess.expires_at) < _now():
            return None
        user = db.get(User, sess.user_id)
        return _member(user) if user and user.status == "active" else None


def logout(token: str) -> None:
    if token:
        with _db() as db:
            db.execute(delete(UserSession).where(UserSession.token_hash == token_hash(token)))


def list_members() -> list[Member]:
    with _db() as db:
        return [_member(u) for u in db.scalars(select(User).order_by(User.created_at, User.id))]


def update_member(user_id: int, *, role: str | None = None, status: str | None = None,
                  is_admin: bool | None = None) -> Member:
    """관리자 화면: 역할·상태(승인·중지)·관리자 여부를 바꾼다."""
    if role is not None and role not in ROLES:
        raise BadRequest("역할이 올바르지 않습니다.")
    if status is not None and status not in ("active", "disabled", "pending"):
        raise BadRequest("상태가 올바르지 않습니다.")
    with _db() as db:
        user = db.get(User, user_id)
        if user is None:
            raise NotFound("회원을 찾을 수 없습니다.")
        role = user.role if role is None else role
        status = user.status if status is None else status
        if status == "active" and not role:
            raise BadRequest("승인하려면 역할을 먼저 정하세요.")
        admin = user.is_admin if is_admin is None else is_admin
        if user.is_admin and (not admin or status != "active"):
            others = db.scalar(select(func.count()).select_from(User).where(
                User.is_admin.is_(True), User.status == "active", User.id != user.id))
            if not others:
                raise BadRequest("관리자가 한 명은 있어야 합니다.")
        user.role, user.status, user.is_admin = role, status, admin
        if status != "active":   # 중지·대기로 바꾸면 로그인을 끊는다
            db.execute(delete(UserSession).where(UserSession.user_id == user.id))
        return _member(user)


# ---------------------------------------------------------------- [임시] 시연용 계정 (나중에 지운다)
# 계정은 git이 아니라 각 PC의 DB에 있어서, 다른 PC에서도 같은 계정으로 로그인하려고 코드에 둔다.
# 서버가 시작할 때 목록에서 빠진 계정만 만든다 (있는 계정·다른 회원은 건드리지 않음). 끄기: .env SEED_TEST_ACCOUNTS=false
TEST_PASSWORD = "Team4!2026"
TEST_ACCOUNTS = [   # (이메일, 이름, 역할, 관리자)
    ("admin@team4.local", "김관리", "lead", True),
    ("plan.head@team4.local", "이기획", "planner_head", False),
    ("plan1@team4.local", "박기획", "planner", False),
    ("plan2@team4.local", "최기획", "planner", False),
    ("data.head@team4.local", "정데이터", "data_head", False),
    ("data1@team4.local", "강데이터", "data", False),
    ("data2@team4.local", "조데이터", "data", False),
    ("dev.head@team4.local", "한개발", "dev_head", False),
    ("dev1@team4.local", "윤개발", "dev", False),
    ("dev2@team4.local", "임개발", "dev", False),
]


def seed_test_accounts(password: str = TEST_PASSWORD, only_if_empty: bool = True) -> list[str]:
    """시연용 계정을 만들고 만든 이메일을 돌려준다. only_if_empty면 회원이 한 명도 없을 때만, 아니면 없는 계정만."""
    existing = {m.email for m in list_members()}
    if only_if_empty and existing:
        return []
    made = []
    for email, name, role, admin in TEST_ACCOUNTS:
        if email in existing:
            continue
        member = signup(email, name, password) if not list_members() else create_member(email, name, password, role, admin)
        if member.role != role or member.is_admin != admin:
            update_member(member.id, role=role, status="active", is_admin=admin)
        made.append(email)
    return made
