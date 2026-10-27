from __future__ import annotations

import dataclasses
import json
import sqlite3

_COLUMNS = "message_id, channel_id, host_id, game, size, note, members, expires_at, cheer"


@dataclasses.dataclass
class LfgCard:
    message_id: int
    channel_id: int
    host_id: int
    game: str
    size: int
    note: str | None
    members: list[int]
    expires_at: int
    cheer: str | None = None


class LfgStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def _add(self, card: LfgCard) -> None:
        self.db.execute(
            f"INSERT INTO lfg_cards ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (card.message_id, card.channel_id, card.host_id, card.game, card.size, card.note,
             json.dumps(card.members), card.expires_at, card.cheer),
        )

    def _set_members(self, message_id: int, members: list[int]) -> None:
        self.db.execute("UPDATE lfg_cards SET members = ? WHERE message_id = ?", (json.dumps(members), message_id))

    def _set_cheer(self, message_id: int, line: str) -> None:
        self.db.execute("UPDATE lfg_cards SET cheer = ? WHERE message_id = ?", (line, message_id))

    def _cards(self, where: str = "", args: tuple = ()) -> list[LfgCard]:
        rows = self.db.execute(f"SELECT {_COLUMNS} FROM lfg_cards {where}", args).fetchall()
        return [LfgCard(*row[:6], json.loads(row[6]), *row[7:]) for row in rows]

    def get(self, message_id: int) -> LfgCard | None:
        return next(iter(self._cards("WHERE message_id = ?", (message_id,))), None)

    def cards(self) -> list[LfgCard]:
        return self._cards()

    def _finish(self, message_id: int) -> bool:
        return self.db.execute("DELETE FROM lfg_cards WHERE message_id = ?", (message_id,)).rowcount == 1
