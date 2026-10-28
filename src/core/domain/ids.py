from __future__ import annotations

from typing import TypeGuard


def is_positive_int(value: object) -> TypeGuard[int]:
    return type(value) is int and value > 0
