"""SQLite 数据访问层（基于 aiosqlite，异步）。"""
import json
import os
from typing import List, Optional, Tuple

import aiosqlite


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        # 确保数据库所在目录存在
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self.conn: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        await self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS records (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id    INTEGER NOT NULL,
                sender_id   INTEGER NOT NULL,
                sender_name TEXT    NOT NULL,
                target_id   INTEGER NOT NULL,
                text        TEXT    NOT NULL DEFAULT '',
                images      TEXT    NOT NULL DEFAULT '[]',
                created_at  INTEGER NOT NULL
            )
            """
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_records_group_target "
            "ON records (group_id, target_id, created_at)"
        )
        await self.conn.commit()

    async def add(
        self,
        *,
        group_id: int,
        sender_id: int,
        sender_name: str,
        target_id: int,
        text: str,
        images: str,
        created_at: int,
    ) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "INSERT INTO records "
            "(group_id, sender_id, sender_name, target_id, text, images, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (group_id, sender_id, sender_name, target_id, text, images, created_at),
        )
        await self.conn.commit()

    async def query(
        self, group_id: int, target_id: int, since: int, limit: int = 50
    ) -> List[Tuple[str, int, str, str, int]]:
        assert self.conn is not None
        async with self.conn.execute(
            "SELECT sender_name, sender_id, text, images, created_at "
            "FROM records "
            "WHERE group_id = ? AND target_id = ? AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT ?",
            (group_id, target_id, since, limit),
        ) as cur:
            return await cur.fetchall()

    async def count(self, group_id: int, target_id: int, since: int) -> int:
        """统计时间窗内 @ 了 target 的总条数（用于提示是否触达上限）。"""
        assert self.conn is not None
        async with self.conn.execute(
            "SELECT COUNT(*) FROM records "
            "WHERE group_id = ? AND target_id = ? AND created_at >= ?",
            (group_id, target_id, since),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def delete_old(self, before: int) -> None:
        assert self.conn is not None
        await self.conn.execute("DELETE FROM records WHERE created_at < ?", (before,))
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None
