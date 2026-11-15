"""Ролі користувачів і їхній ранг: user < admin < owner.

Owner має всі права admin і ще керує ролями. Модерувати контент і блокувати можна лише
тих, чия роль нижча (свій контент модератор може змінювати завжди).
"""

from typing import Literal

Role = Literal["user", "admin", "owner"]

RANK: dict[str, int] = {"user": 0, "admin": 1, "owner": 2}


def rank(role: str) -> int:
    return RANK.get(str(role), 0)


def is_staff(role: str) -> bool:
    """admin або owner: доступ до модерації."""
    return rank(role) >= RANK["admin"]


def cast_role(role: str) -> Role:
    value = str(role)
    return "owner" if value == "owner" else "admin" if value == "admin" else "user"


def outranks(actor: str, target: str) -> bool:
    return rank(actor) > rank(target)
