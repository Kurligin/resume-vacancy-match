"""Вход по ролям: учётки, подписанная cookie сессии.

Полноценной авторизации нет (решение 11.11). Учётки заданы здесь, пароли — только хэши scrypt
из окружения: RVM_PASSWORD_<ЛОГИН> (без переменной вход по паролю закрыт). Хэш:
uv run python -c "from rvm.api.auth import hash_password; print(hash_password('пароль'))"
"""

import hashlib
import hmac
import os
import secrets
import time
from typing import Literal

from pydantic import BaseModel

Role = Literal["recruiter", "manager"]
ROLE_NAMES: dict[Role, str] = {"recruiter": "рекрутер", "manager": "руководитель"}
COOKIE = "rvm_session"
MAX_AGE = 12 * 3600  # сессия — рабочий день


class User(BaseModel):
    login: str
    name: str
    role: Role

    @property
    def who(self) -> str:
        """Подпись в журнале: имя и роль."""
        return f"{self.name} ({ROLE_NAMES[self.role]})"


# руководителю назначены все открытые вакансии
USERS: dict[str, User] = {
    "anna": User(login="anna", name="Анна Смирнова", role="recruiter"),
    "oleg": User(login="oleg", name="Олег Волков", role="manager"),
}
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt:{salt.hex()}:{key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, salt, key = stored.split(":")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
        return hmac.compare_digest(got, bytes.fromhex(key))
    except ValueError:
        return False


def check_password(login: str, password: str) -> User | None:
    stored = os.environ.get(f"RVM_PASSWORD_{login.upper()}", "")
    if login not in USERS or not stored:
        return None
    return USERS[login] if verify_password(password, stored) else None


def demo_user(role: Role) -> User:
    return next(u for u in USERS.values() if u.role == role)


def _sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


def issue(secret: bytes, user: User) -> str:
    payload = f"{user.login}.{int(time.time())}"
    return f"{payload}.{_sign(secret, payload)}"


def read(secret: bytes, token: str | None) -> User | None:
    """Пользователь из cookie или None: подпись не сходится, срок вышел, учётки нет."""
    parts = (token or "").rsplit(".", 2)
    if len(parts) != 3 or not parts[1].isdigit():
        return None
    login, ts, sig = parts
    if not hmac.compare_digest(sig, _sign(secret, f"{login}.{ts}")):
        return None
    if time.time() - int(ts) > MAX_AGE:
        return None
    return USERS.get(login)
