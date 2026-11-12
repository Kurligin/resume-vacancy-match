"""Вход по ролям: учётки, подписанная cookie сессии.

Полноценной авторизации нет (решение 11.11): учётки демо заданы здесь, пароли — в README."""

import hashlib
import hmac
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


# логин → (пользователь, пароль); руководителю назначены все открытые вакансии
USERS: dict[str, tuple[User, str]] = {
    "anna": (User(login="anna", name="Анна Смирнова", role="recruiter"), "anna-demo"),
    "oleg": (User(login="oleg", name="Олег Волков", role="manager"), "oleg-demo"),
}


def check_password(login: str, password: str) -> User | None:
    user, expected = USERS.get(login, (None, ""))
    ok = hmac.compare_digest(password.encode(), expected.encode())
    return user if user and ok else None


def demo_user(role: Role) -> User:
    return next(u for u, _ in USERS.values() if u.role == role)


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
    return USERS[login][0] if login in USERS else None
