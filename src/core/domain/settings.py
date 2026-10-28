from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Setting[T]:
    key: str
    default: T = field(compare=False)
