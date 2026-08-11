"""SQLite 数据访问层（基于 aiosqlite，异步）。

存储结构：数据目录下按群分文件，每个群一个独立的 .db 文件：
    <data_dir>/<group_id>.db
不同群的数据物理隔离，便于备份、清理与迁移。
"""
import asyncio
import os
from typing import Dict, List, Optional, Tuple

import aiosqlite


class Database:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = os.path.abspath(data_dir)
        # 确保数据总目录存在
        os.makedirs(self.data_dir, exist_ok=True)
        # 每个群一个连接，懒加载并缓存（key = group_id）
        self._conns: Dict[int, aiosqlite.Connection] = {}
        self._lock: Optional[asyncio.Lock] = None

    def _group_path(self, group_id: int) -> str:
        return os.path.join(self.data_dir, f"{group_id}.db")

    async def init(self) -> None:
        # 目录已在 __init__ 创建；连接按群懒加载，这里仅初始化锁
        self._lock = asyncio.Lock()

    async def _get_conn(self, group_id: int) -> aiosqlite.Connection:
        """获取（按需创建并建表）指定群的数据库连接，带缓存与并发保护。"""
        conn = self._conns.get(group_id)
        if conn is not None:
            return conn
        # 加锁避免并发时同一群重复建连
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            conn = self._conns.get(group_id)
            if conn is not None:
                return conn
            conn = await aiosqlite.connect(self._group_path(group_id))
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id         INTEGER NOT NULL,
                    sender_id        INTEGER NOT NULL,
                    sender_name      TEXT    NOT NULL,
                    target_id        INTEGER NOT NULL,
                    text             TEXT    NOT NULL DEFAULT '',
                    images           TEXT    NOT NULL DEFAULT '[]',
                    created_at       INTEGER NOT NULL,
                    reply_sender_id  INTEGER,
                    reply_sender_name TEXT,
                    reply_content    TEXT
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_records_group_target "
                "ON records (group_id, target_id, created_at)"
            )
            # 兼容旧库：补齐新增列（ALTER 仅在列不存在时执行，不重建表）
            await self._ensure_columns(conn)
            await conn.commit()
            self._conns[group_id] = conn
            return conn

    async def _ensure_columns(self, conn: "aiosqlite.Connection") -> None:
        """为旧版本创建的表补齐新增列（向后兼容，无需重建表）。"""
        async with conn.execute("PRAGMA table_info(records)") as cur:
            existing = {row[1] for row in await cur.fetchall()}
        for col, col_type in (
            ("reply_sender_id", "INTEGER"),
            ("reply_sender_name", "TEXT"),
            ("reply_content", "TEXT"),
        ):
            if col not in existing:
                await conn.execute(
                    f"ALTER TABLE records ADD COLUMN {col} {col_type}"
                )

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
        reply_sender_id: Optional[int] = None,
        reply_sender_name: Optional[str] = None,
        reply_content: Optional[str] = None,
    ) -> None:
        conn = await self._get_conn(group_id)
        await conn.execute(
            "INSERT INTO records "
            "(group_id, sender_id, sender_name, target_id, text, images, created_at, "
            " reply_sender_id, reply_sender_name, reply_content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                group_id,
                sender_id,
                sender_name,
                target_id,
                text,
                images,
                created_at,
                reply_sender_id,
                reply_sender_name,
                reply_content,
            ),
        )
        await conn.commit()

    async def query(
        self, group_id: int, target_id: int, since: int, limit: int = 50
    ) -> List[Tuple]:
        conn = await self._get_conn(group_id)
        async with conn.execute(
            "SELECT sender_name, sender_id, text, images, created_at, "
            "reply_sender_id, reply_sender_name, reply_content "
            "FROM records "
            "WHERE group_id = ? AND target_id = ? AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT ?",
            (group_id, target_id, since, limit),
        ) as cur:
            return await cur.fetchall()

    async def count(self, group_id: int, target_id: int, since: int) -> int:
        """统计时间窗内 @ 了 target 的总条数（用于提示是否触达上限）。"""
        conn = await self._get_conn(group_id)
        async with conn.execute(
            "SELECT COUNT(*) FROM records "
            "WHERE group_id = ? AND target_id = ? AND created_at >= ?",
            (group_id, target_id, since),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def delete_old(self, before: int) -> None:
        """清理所有群文件中超过保留期的记录（遍历数据目录下的 .db 文件）。"""
        for fname in os.listdir(self.data_dir):
            if not fname.endswith(".db"):
                continue
            path = os.path.join(self.data_dir, fname)
            try:
                conn = await aiosqlite.connect(path)
                try:
                    await conn.execute(
                        "DELETE FROM records WHERE created_at < ?", (before,)
                    )
                    await conn.commit()
                finally:
                    await conn.close()
            except Exception as exc:  # noqa: BLE001
                print(f"[whofindme] 清理群文件 {fname} 失败: {exc}")

    async def close(self) -> None:
        for conn in self._conns.values():
            await conn.close()
        self._conns.clear()
