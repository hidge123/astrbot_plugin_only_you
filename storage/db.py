"""SQLite persistence layer based on aiosqlite (with stdlib sqlite3 fallback) for astrbot_plugin_only_you."""

import json
import os
import sqlite3
import time
from typing import Any, Optional

try:
    import aiosqlite
except ImportError:
    aiosqlite = None


class DatabaseManager:
    """Async SQLite manager for memories, souls, daily schedules, and user relations."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Any = None
        self._use_aiosqlite: bool = aiosqlite is not None

    async def initialize(self) -> None:
        """Open connection and initialize schema tables."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        if self._use_aiosqlite:
            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row
        else:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row

        await self.execute("PRAGMA journal_mode=WAL;")
        await self.execute("PRAGMA synchronous=NORMAL;")

        # 1. Memories table (exact schema from docs/ARCHITECTURE.md Section 4.1)
        await self.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                soul_id TEXT NOT NULL,
                user_id TEXT,
                user_tier INTEGER DEFAULT 1,
                memory_tier INTEGER NOT NULL,
                content TEXT NOT NULL,
                keywords TEXT NOT NULL,
                importance INTEGER DEFAULT 5,
                vitality REAL DEFAULT 100.0,
                access_count INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL,
                last_accessed_at INTEGER
            );
            """
        )
        await self.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_soul_user ON memories(soul_id, user_id);"
        )
        await self.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_tier ON memories(memory_tier);"
        )

        # 2. Souls table (caches expanded Persona / custom Roguelite card pools)
        await self.execute(
            """
            CREATE TABLE IF NOT EXISTS souls (
                soul_id TEXT PRIMARY KEY,
                persona_hash TEXT NOT NULL,
                profile_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )

        # 3. Daily schedules table (persists DailyScheduleRun per soul_id + date_str)
        await self.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_schedules (
                soul_id TEXT NOT NULL,
                date_str TEXT NOT NULL,
                run_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (soul_id, date_str)
            );
            """
        )

        # 4. Users table (tracks three-tier relationships L1/L2/L3 and interaction stats)
        await self.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                global_uid TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                nickname TEXT NOT NULL DEFAULT '',
                tier INTEGER NOT NULL DEFAULT 1,
                interaction_count INTEGER NOT NULL DEFAULT 0,
                affinity REAL NOT NULL DEFAULT 0.0,
                platform_ids_json TEXT NOT NULL DEFAULT '[]',
                updated_at INTEGER NOT NULL
            );
            """
        )
        # Safe migration for existing databases without `nickname` column
        try:
            await self.execute("ALTER TABLE users ADD COLUMN nickname TEXT NOT NULL DEFAULT '';")
        except Exception:
            pass

        # 5. UMO -> Soul ID explicit routing bindings table (for WebUI Console Module 1)
        await self.execute(
            """
            CREATE TABLE IF NOT EXISTS umo_soul_bindings (
                umo TEXT PRIMARY KEY,
                soul_id TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )

        # 6. Generic plugin settings KV store (plugin-autonomous model config,
        #    default soul routing, migration flags...). Kept out of `AstrBotConfig`
        #    so the platform schema integrity check never deletes undeclared keys.
        await self.execute(
            """
            CREATE TABLE IF NOT EXISTS plugin_settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )

        # 7. Surfing video history pool (for WebUI Console Module 5)
        await self.execute(
            """
            CREATE TABLE IF NOT EXISTS surfing_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                soul_id TEXT NOT NULL,
                video_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                title TEXT NOT NULL,
                author TEXT NOT NULL,
                cover_url TEXT NOT NULL DEFAULT '',
                share_url TEXT NOT NULL DEFAULT '',
                tags_json TEXT NOT NULL DEFAULT '[]',
                hot_comments_json TEXT NOT NULL DEFAULT '[]',
                visual_notes TEXT NOT NULL DEFAULT '',
                score INTEGER NOT NULL DEFAULT 75,
                commentary TEXT NOT NULL DEFAULT '',
                worth_sharing INTEGER NOT NULL DEFAULT 1,
                watched_at INTEGER NOT NULL
            );
            """
        )

    async def get_setting(self, key: str, default: Any = None) -> Any:
        """Read a JSON value from the generic `plugin_settings` KV table."""
        if not key:
            return default
        row = await self.fetchone(
            "SELECT value_json FROM plugin_settings WHERE key = ?", (str(key),)
        )
        if not row or row.get("value_json") is None:
            return default
        try:
            return json.loads(row["value_json"])
        except Exception:
            return default

    async def set_setting(self, key: str, value: Any) -> None:
        """Upsert a JSON-serialisable value into the generic `plugin_settings` table."""
        if not key:
            return
        payload = json.dumps(value, ensure_ascii=False)
        await self.execute(
            """
            INSERT INTO plugin_settings (key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (str(key), payload, int(time.time())),
        )

    async def close(self) -> None:
        """Close the SQLite connection."""
        if self._conn is not None:
            if self._use_aiosqlite:
                await self._conn.close()
            else:
                self._conn.close()
            self._conn = None

    async def execute(self, sql: str, params: tuple = ()) -> int:
        """Execute a write statement and return lastrowid."""
        assert self._conn is not None, "DatabaseManager is not initialized"
        if self._use_aiosqlite:
            async with self._conn.execute(sql, params) as cursor:
                last_id = cursor.lastrowid
            await self._conn.commit()
            return last_id or 0
        else:
            cursor = self._conn.execute(sql, params)
            last_id = cursor.lastrowid
            self._conn.commit()
            return last_id or 0

    async def fetchone(self, sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
        """Fetch a single row as a dict."""
        assert self._conn is not None, "DatabaseManager is not initialized"
        if self._use_aiosqlite:
            async with self._conn.execute(sql, params) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row is not None else None
        else:
            cursor = self._conn.execute(sql, params)
            row = cursor.fetchone()
            return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Fetch all matching rows as a list of dicts."""
        assert self._conn is not None, "DatabaseManager is not initialized"
        if self._use_aiosqlite:
            async with self._conn.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
        else:
            cursor = self._conn.execute(sql, params)
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
