"""시연용 로그인 계정과 세션. 계정은 app/accounts.json (스펙의 역할 목록에서 코드가 만든 것).

- 계정은 업무 테이블과 섞이지 않게 app_auth 스키마에 둔다 (MySQL은 스키마가 곧 DB라 앱 DB 안의 app_auth_account 테이블).
- 처음 만드는 계정의 비밀번호는 DEMO_PASSWORD 또는 임의 값이다. 임의 값은 LOCAL_DIR/accounts.txt에만 적고 로그·응답에 남기지 않는다.
- 이미 있는 계정의 비밀번호는 다시 시작해도 바꾸지 않는다 (RESET_PASSWORDS=true면 모두 새로 만든다).
- 세션은 서버 메모리에 둔다. 서버를 다시 시작하면 다시 로그인한다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from sqlalchemy import text

from app.common.db import engine, mysql
from app.common.errors import ApiError
from app.common.user import CurrentUser

log = logging.getLogger(__name__)
ITERATIONS = 120_000
LETTERS = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ACCOUNTS = json.loads((Path(__file__).resolve().parent.parent / "accounts.json").read_text(encoding="utf-8"))
HEAD = "# 시연 계정 (아이디\t역할\t비밀번호). 새로 만든 계정만 적힌다. 이 파일은 공유하지 않는다.\n"

_sessions: dict[str, tuple[CurrentUser, str, float]] = {}   # 토큰 → (사용자, 아이디, 만료 시각)
_lock = threading.Lock()


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)


def hash_password(password: str) -> str:
    """Spring 틀(Accounts.java)과 같은 형식: pbkdf2$반복$소금$해시"""
    salt = secrets.token_bytes(16)
    return f"pbkdf2${ITERATIONS}${salt.hex()}${_derive(password, salt, ITERATIONS).hex()}"


def verify(password: str, stored: str) -> bool:
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2":
        return False
    return hmac.compare_digest(bytes.fromhex(parts[3]), _derive(password, bytes.fromhex(parts[2]), int(parts[1])))


DUMMY = hash_password("not-a-password")


def _table() -> str:
    return "app_auth_account" if mysql() else "app_auth.account"


def _new_password() -> str:
    return "".join(secrets.choice(LETTERS) for _ in range(12))


def setup() -> None:
    """서버를 켤 때: 계정 테이블을 만들고, 없는 계정을 만든다. DB가 뜰 때까지 잠시 기다린다."""
    for attempt in range(30):
        try:
            with engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            break
        except Exception:   # noqa: BLE001 - DB가 아직 뜨지 않음
            if attempt == 29:
                raise
            time.sleep(1)
    demo = os.getenv("DEMO_PASSWORD", "")
    reset = os.getenv("RESET_PASSWORDS", "false").lower() == "true"
    created = []
    table = _table()
    with engine().begin() as conn:
        if not mysql():
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS app_auth"))
        stamp = "datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)" if mysql() else "timestamptz NOT NULL DEFAULT now()"
        conn.execute(text(f"CREATE TABLE IF NOT EXISTS {table} ("
                          "login_id varchar(50) PRIMARY KEY, role varchar(100) NOT NULL, user_id varchar(50) NOT NULL, "
                          f"password_hash varchar(200) NOT NULL, created_at {stamp})"))
        if reset:
            conn.execute(text(f"DELETE FROM {table}"))
        for a in ACCOUNTS:
            found = conn.execute(text(f"UPDATE {table} SET role = :role, user_id = :uid WHERE login_id = :id"),
                                 {"role": a["role"], "uid": a["userId"], "id": a["loginId"]}).rowcount
            if found == 0:
                password = demo or _new_password()
                conn.execute(text(f"INSERT INTO {table} (login_id, role, user_id, password_hash) VALUES (:id, :role, :uid, :hash)"),
                             {"id": a["loginId"], "role": a["role"], "uid": a["userId"], "hash": hash_password(password)})
                created.append(f"{a['loginId']}\t{a['role']}\t{password if not demo else '(DEMO_PASSWORD로 정한 값)'}")
        ids = [a["loginId"] for a in ACCOUNTS]
        for (old,) in conn.execute(text(f"SELECT login_id FROM {table}")).all():
            if old not in ids:   # 스펙에서 사라진 역할의 계정
                conn.execute(text(f"DELETE FROM {table} WHERE login_id = :id"), {"id": old})
    if created:
        folder = Path(os.getenv("LOCAL_DIR", "local"))
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "accounts.txt"
        head = "" if path.exists() and not reset else HEAD
        with path.open("w" if reset else "a", encoding="utf-8", newline="\n") as out:
            out.write(head + "\n".join(created) + "\n")
        log.info("시연 계정 %d개를 만들었습니다. 아이디와 비밀번호는 %s에 있습니다.", len(created), path.resolve())


def login(login_id: str | None, password: str | None) -> dict:
    """로그인: 맞으면 세션 토큰과 사용자 정보"""
    with engine().connect() as conn:
        row = conn.execute(text(f"SELECT role, user_id, password_hash FROM {_table()} WHERE login_id = :id"),
                           {"id": login_id or ""}).first()
    matches = verify(password or "", row.password_hash if row else DUMMY)   # 없는 아이디도 같은 시간이 걸리게 한다
    if row is None or not matches:
        raise ApiError(401, "UNAUTHORIZED", "아이디 또는 비밀번호를 확인하세요.")
    token = secrets.token_urlsafe(32)
    user = CurrentUser(str(row.user_id), row.role)
    hours = max(1, int(os.getenv("SESSION_HOURS", "8")))
    now = time.time()
    with _lock:
        for key in [k for k, s in _sessions.items() if s[2] < now]:
            del _sessions[key]
        _sessions[token] = (user, login_id, now + hours * 3600)
    return {"token": token, "loginId": login_id, "role": user.role, "userId": user.id}


def _session(token: str | None) -> tuple[CurrentUser, str, float] | None:
    s = _sessions.get(token) if token else None
    if s and s[2] < time.time():
        _sessions.pop(token, None)
        return None
    return s


def user(token: str | None) -> CurrentUser | None:
    """토큰의 사용자 (없거나 만료면 None)"""
    s = _session(token)
    return s[0] if s else None


def me(token: str | None) -> dict | None:
    s = _session(token)
    return {"loginId": s[1], "role": s[0].role, "userId": s[0].id} if s else None


def logout(token: str | None) -> None:
    if token:
        _sessions.pop(token, None)
