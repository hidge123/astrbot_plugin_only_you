"""v2.12 Tier 1 保留期修复回归。

冻结契约：

1. 新建 T1 以 ``created_at`` 为保留期起点；
2. T2/T3 **降级**为 T1 时，``tier1_since`` 重置为降级时刻，保留期重新计算 48h，
   不会因旧 ``created_at`` 立即过期；
3. 旧库 T1（``tier1_since = 0``）仍以 ``created_at`` 回退，过期即由 GC 删除；
4. 已是 T1 的编辑不重置保留期；
5. T1 上限淘汰按 ``tier1_since`` 排序，降级来的新 T1 不会先被裁掉；
6. ``memories/list`` 返回 ``tier1_expires_at / remaining / expired``，并在列表面板
   刷新前顺带回收过期 T1。
"""

import time
import unittest

from _helpers import (
    DatabaseAsyncTestCase,
    StubModelHub,
    load_plugin_modules,
    make_plugin_config,
)

mods = load_plugin_modules()
MemoryManager = mods.memory_manager.MemoryManager
MemoryTier = mods.memory_manager.MemoryTier
TIER1_TTL_SECONDS = mods.memory_manager.TIER1_TTL_SECONDS


class Tier1RetentionTest(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.manager = MemoryManager(self.db, StubModelHub(), self.config)

    async def _add(self, content, **kwargs):
        kwargs.setdefault("soul_id", "soul-a")
        return await self.manager.add_memory(content=content, **kwargs)

    async def _item(self, memory_id):
        items = await self.manager.list_memories(soul_id="soul-a")
        return next(item for item in items if item.id == memory_id)

    async def test_new_tier1_anchors_retention_to_creation(self):
        now = int(time.time()) - 10 * 3600
        memory_id = await self._add(
            "新瞬时记忆", memory_tier=MemoryTier.EPHEMERAL, created_at=now
        )
        row = await self.db.fetchone(
            "SELECT created_at, tier1_since FROM memories WHERE id = ?", (memory_id,)
        )
        assert row["tier1_since"] == row["created_at"] == now
        item = await self._item(memory_id)
        assert item.tier1_expires_at == now + TIER1_TTL_SECONDS
        payload = item.to_dict()
        assert payload["tier1_expires_at"] == now + TIER1_TTL_SECONDS
        assert payload["tier1_remaining_seconds"] > 0
        assert payload["tier1_expired"] is False

    async def test_downgrade_to_tier1_restarts_the_48h_window(self):
        now = int(time.time())
        memory_id = await self._add(
            "十二天前形成的近期记忆",
            memory_tier=MemoryTier.RECENT,
            created_at=now - 12 * 86400,
        )
        updated = await self.manager.update_memory(
            memory_id, memory_tier=MemoryTier.EPHEMERAL, now_override=now
        )
        assert updated is not None
        assert updated.tier1_since == now
        assert updated.tier1_expires_at == now + TIER1_TTL_SECONDS

        # 47h later: still alive (the old created_at must not expire it instantly)
        stats = await self.manager.cleanup_tier1(now_override=now + 47 * 3600)
        assert stats["deleted_expired"] == 0
        assert await self.db.fetchone(
            "SELECT id FROM memories WHERE id = ?", (memory_id,)
        )

        # 49h later: normal expiry
        stats = await self.manager.cleanup_tier1(now_override=now + 49 * 3600)
        assert stats["deleted_expired"] == 1
        assert await self.db.fetchone(
            "SELECT id FROM memories WHERE id = ?", (memory_id,)
        ) is None

    async def test_legacy_tier1_falls_back_to_created_at(self):
        now = int(time.time())
        memory_id = await self._add(
            "旧库瞬时记忆", memory_tier=MemoryTier.EPHEMERAL, created_at=now - 60 * 3600
        )
        # Simulate a pre-v2.12 row: no tier1_since anchor.
        await self.db.execute(
            "UPDATE memories SET tier1_since = 0 WHERE id = ?", (memory_id,)
        )
        stats = await self.manager.cleanup_tier1(now_override=now)
        assert stats["deleted_expired"] == 1
        assert await self.db.fetchone(
            "SELECT id FROM memories WHERE id = ?", (memory_id,)
        ) is None

    async def test_editing_an_existing_tier1_does_not_restart_retention(self):
        now = int(time.time()) - 6 * 3600
        memory_id = await self._add(
            "正在编辑的瞬时记忆", memory_tier=MemoryTier.EPHEMERAL, created_at=now
        )
        updated = await self.manager.update_memory(
            memory_id,
            content="编辑后的瞬时记忆",
            now_override=now + 3600,
        )
        assert updated is not None
        assert updated.tier1_since == now
        # Leaving T1 clears the anchor; pinning back to T1 starts a fresh window.
        promoted = await self.manager.update_memory(
            memory_id, memory_tier=MemoryTier.CORE, now_override=now + 7200
        )
        assert promoted is not None and promoted.tier1_since == 0
        back = await self.manager.update_memory(
            memory_id, memory_tier=MemoryTier.EPHEMERAL, now_override=now + 9000
        )
        assert back is not None and back.tier1_since == now + 9000

    async def test_tier1_cap_evicts_by_retention_start(self):
        self.config["memory_and_context_config"]["max_tier1_memories"] = 1
        now = int(time.time())
        old_id = await self._add(
            "更早的瞬时记忆", memory_tier=MemoryTier.EPHEMERAL, created_at=now - 10 * 3600
        )
        downgraded_id = await self._add(
            "十二天前形成但刚降级的记忆",
            memory_tier=MemoryTier.RECENT,
            created_at=now - 12 * 86400,
        )
        await self.manager.update_memory(
            downgraded_id, memory_tier=MemoryTier.EPHEMERAL, now_override=now
        )
        stats = await self.manager.cleanup_tier1(now_override=now)
        assert stats["deleted_overflow"] == 1
        assert await self.db.fetchone(
            "SELECT id FROM memories WHERE id = ?", (old_id,)
        ) is None
        assert await self.db.fetchone(
            "SELECT id FROM memories WHERE id = ?", (downgraded_id,)
        ) is not None


if __name__ == "__main__":
    unittest.main()
