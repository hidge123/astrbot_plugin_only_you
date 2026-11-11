"""v2.8 T1 offline tests: MEMORY_MUSE buckets / Tier 1 lifecycle / surfing / vision.

Contract: ``.team/V28_IMPLEMENTATION_CONTRACT.md`` §5 (T1 backend-memory-surf scope).

1. ``MemoryManager.sample_muse_memories`` — independent T1/T2/T3 bucket queries
   (``ORDER BY COALESCE(last_accessed_at, 0) ASC, id ASC LIMIT ?``),
   ``exclude_ids`` filtering and the importance / tier weighted draw.
2. ``MemoryManager.touch_memories_for_muse`` — refresh ``last_accessed_at`` only
   (no ``access_count`` / ``vitality`` / ``memory_tier`` change).
3. ``MemoryManager.add_memory`` Tier 1 behaviour — identical-content dedupe
   (refresh + return the existing id) and FIFO cap from ``max_tier1_memories``
   (default 30); Tier 2 / Tier 3 untouched.
4. ``MemoryManager.cleanup_tier1`` — >48h expiry + per-Soul overflow stats.
5. ``CyberSurfingPipeline.surf_once`` — candidate exhaustion returns ``None``
   (never falls back to ``candidates[0]``); ``is_enabled`` honours the per-Soul
   setting first then the global switch, and gates even manual triggers.
6. ``VideoReviewResult.is_fallback`` — default ``False`` / included in
   ``to_dict()``; vision failure or malformed output -> ``score=60`` +
   ``is_fallback=True`` while normal success is unchanged.

Stdlib ``unittest`` only, fully offline (adapters / vision LLM stubbed), temp DBs.
"""

import os
import random
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    DatabaseAsyncTestCase,
    StubModelHub,
    load_plugin_modules,
    make_plugin_config,
)

mods = load_plugin_modules()
MemoryManager = mods.memory_manager.MemoryManager
MemoryTier = mods.memory_manager.MemoryTier
CyberSurfingPipeline = mods.surfing_pipeline.CyberSurfingPipeline
VideoReviewResult = mods.surfing_pipeline.VideoReviewResult
VideoVisionProcessor = mods.surfing_pipeline.VideoVisionProcessor
VideoItem = mods.surfing_pipeline.VideoItem
SessionBuffer = mods.context_budget.SessionBuffer


def _video(video_id="bv1", title="测试视频", platform="bilibili"):
    return VideoItem(
        platform=platform,
        video_id=video_id,
        title=title,
        author="UP主",
        share_url="https://example.com/%s" % video_id,
    )


def _config_with_tier1_limit(limit):
    config = make_plugin_config()
    config["memory_and_context_config"]["max_tier1_memories"] = limit
    return config


# ===========================================================================
# 1. sample_muse_memories — per-bucket query, exclude, weighted sampling
# ===========================================================================
class MemoryTestBase(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.manager = MemoryManager(self.db, StubModelHub(), self.config)

    async def _add(
        self,
        content,
        tier,
        importance=5,
        soul_id="soul-a",
        created_at=None,
        last_accessed_at=None,
        **kwargs,
    ):
        memory_id = await self.manager.add_memory(
            soul_id=soul_id,
            content=content,
            importance=importance,
            memory_tier=tier,
            created_at=created_at,
            **kwargs,
        )
        assert memory_id > 0, "add_memory returned no id"
        if last_accessed_at is not None:
            await self.db.execute(
                "UPDATE memories SET last_accessed_at = ? WHERE id = ?",
                (int(last_accessed_at), memory_id),
            )
        return memory_id

    async def _pick(self, **kwargs):
        kwargs.setdefault("soul_id", "soul-a")
        return await self.manager.sample_muse_memories(**kwargs)


class TestSampleMuseMemories(MemoryTestBase):
    async def test_buckets_queried_independently_oldest_first(self):
        t1 = await self._add("T1 最老", MemoryTier.EPHEMERAL, importance=4, last_accessed_at=10)
        t2_a = await self._add("T2 A", MemoryTier.RECENT, importance=5, last_accessed_at=100)
        await self._add("T2 B", MemoryTier.RECENT, importance=5, last_accessed_at=200)
        await self._add("T2 C", MemoryTier.RECENT, importance=5, last_accessed_at=300)
        t3_a = await self._add(
            "T3 A", MemoryTier.CORE, importance=9, user_tier=3, last_accessed_at=100
        )
        await self._add(
            "T3 B", MemoryTier.CORE, importance=9, user_tier=3, last_accessed_at=200
        )

        picked = await self._pick(per_bucket_limit=1, pick=10)
        assert len(picked) == 3
        assert {item.memory_tier for item in picked} == {
            MemoryTier.EPHEMERAL,
            MemoryTier.RECENT,
            MemoryTier.CORE,
        }
        by_tier = {item.memory_tier: item.id for item in picked}
        assert by_tier[MemoryTier.EPHEMERAL] == t1
        assert by_tier[MemoryTier.RECENT] == t2_a
        assert by_tier[MemoryTier.CORE] == t3_a

    async def test_never_accessed_rows_come_first(self):
        explicit_old = await self._add(
            "显式较老", MemoryTier.EPHEMERAL, importance=3, last_accessed_at=100
        )
        never = await self._add("从未被访问", MemoryTier.EPHEMERAL, importance=3)
        await self.db.execute(
            "UPDATE memories SET last_accessed_at = NULL WHERE id = ?", (never,)
        )
        newest = await self._add(
            "最近被访问", MemoryTier.EPHEMERAL, importance=3, last_accessed_at=9999
        )

        picked = await self._pick(per_bucket_limit=2, pick=10)
        assert {item.id for item in picked} == {never, explicit_old}
        assert newest not in {item.id for item in picked}

    async def test_exclude_ids_filtered_after_bucket_limit(self):
        a = await self._add("记忆 A", MemoryTier.RECENT, importance=5)
        b = await self._add("记忆 B", MemoryTier.RECENT, importance=5)
        c = await self._add("记忆 C", MemoryTier.RECENT, importance=5)

        picked = await self._pick(exclude_ids=[a, c], per_bucket_limit=15, pick=10)
        assert [item.id for item in picked] == [b]

        picked = await self._pick(exclude_ids=[a, b, c], per_bucket_limit=15, pick=10)
        assert picked == []

    async def test_only_requested_soul_is_queried(self):
        await self._add("别的角色的记忆", MemoryTier.RECENT, soul_id="soul-b")
        mine = await self._add("本角色的记忆", MemoryTier.RECENT, soul_id="soul-a")

        picked = await self._pick(per_bucket_limit=15, pick=10)
        assert [item.id for item in picked] == [mine]

    async def test_weighted_draw_prefers_high_importance(self):
        high = await self._add("高重要度记忆", MemoryTier.RECENT, importance=10)
        for index in range(5):
            await self._add("低重要度记忆%d" % index, MemoryTier.RECENT, importance=1)
        await self._add("T1 低层级", MemoryTier.EPHEMERAL, importance=1)

        random.seed(2024)
        high_count = 0
        for _ in range(300):
            picked = await self._pick(per_bucket_limit=15, pick=1)
            assert len(picked) == 1
            if picked[0].id == high:
                high_count += 1
        # weight 10 vs 5x1 -> P(high) ~= 2/3
        assert 150 <= high_count <= 250, high_count

    async def test_weighted_draw_biases_core_tier(self):
        t1 = await self._add("T1 同分", MemoryTier.EPHEMERAL, importance=5)
        t3 = await self._add(
            "T3 同分", MemoryTier.CORE, importance=5, user_tier=3
        )

        random.seed(7)
        t3_count = 0
        for _ in range(300):
            picked = await self._pick(per_bucket_limit=15, pick=1)
            assert len(picked) == 1
            if picked[0].id == t3:
                t3_count += 1
        assert t3_count > 150, t3_count

    async def test_pick_limits_returned_items_without_duplicates(self):
        ids = [
            await self._add("候选记忆%d" % index, MemoryTier.RECENT, importance=5)
            for index in range(3)
        ]
        picked = await self._pick(per_bucket_limit=15, pick=2)
        assert len(picked) == 2
        assert len({item.id for item in picked}) == 2
        assert {item.id for item in picked} <= set(ids)

    async def test_per_bucket_limit_defaults_to_15(self):
        for index in range(20):
            await self._add("批量记忆%d" % index, MemoryTier.RECENT, importance=5)
        picked = await self._pick(pick=20)
        assert len(picked) == 15

    async def test_pick_zero_and_disabled_memory_return_empty(self):
        await self._add("候选记忆", MemoryTier.RECENT, importance=5)
        assert await self._pick(pick=0) == []
        assert await self._pick(pick=-3) == []

        disabled_config = make_plugin_config(
            memory_and_context_config={"enable_tiered_memory": False}
        )
        disabled = MemoryManager(self.db, StubModelHub(), disabled_config)
        assert await disabled.sample_muse_memories("soul-a") == []

    async def test_returns_memory_items(self):
        await self._add("记忆条目", MemoryTier.RECENT, importance=5)
        picked = await self._pick(per_bucket_limit=15, pick=1)
        assert isinstance(picked[0], mods.memory_manager.MemoryItem)


# ===========================================================================
# 2. touch_memories_for_muse — last_accessed_at only
# ===========================================================================
class TestTouchMemoriesForMuse(MemoryTestBase):
    async def test_only_last_accessed_at_is_updated(self):
        memory_id = await self._add(
            "炼丹日常", MemoryTier.RECENT, importance=6, keywords="炼丹"
        )
        await self.db.execute(
            """
            UPDATE memories
            SET access_count = 3, vitality = 42.5, memory_tier = 2, last_accessed_at = 111
            WHERE id = ?
            """,
            (memory_id,),
        )
        before = int(time.time())
        await self.manager.touch_memories_for_muse([memory_id])

        row = await self.db.fetchone("SELECT * FROM memories WHERE id = ?", (memory_id,))
        assert row["last_accessed_at"] >= before
        assert row["access_count"] == 3
        assert abs(float(row["vitality"]) - 42.5) < 1e-9
        assert row["memory_tier"] == MemoryTier.RECENT
        assert row["content"] == "炼丹日常"

    async def test_multiple_ids_are_touched(self):
        first = await self._add("记忆甲", MemoryTier.RECENT)
        second = await self._add("记忆乙", MemoryTier.EPHEMERAL, created_at=1)
        before = int(time.time())
        await self.manager.touch_memories_for_muse([first, second, first])

        for memory_id in (first, second):
            row = await self.db.fetchone(
                "SELECT last_accessed_at FROM memories WHERE id = ?", (memory_id,)
            )
            assert row["last_accessed_at"] >= before

    async def test_empty_unknown_and_invalid_ids_are_noops(self):
        await self.manager.touch_memories_for_muse([])
        await self.manager.touch_memories_for_muse([999999])
        await self.manager.touch_memories_for_muse([None, "not-an-int"])  # type: ignore[list-item]


# ===========================================================================
# 3. add_memory Tier 1 dedupe + FIFO cap
# ===========================================================================
class TestTier1DedupeAndCap(MemoryTestBase):
    async def test_identical_tier1_content_is_deduped(self):
        first = await self._add(
            "阿泽爱吃火锅", MemoryTier.EPHEMERAL, importance=3, keywords="火锅"
        )
        await self.db.execute("UPDATE memories SET last_accessed_at = 111 WHERE id = ?", (first,))
        before = int(time.time())

        second = await self._add(
            "阿泽爱吃火锅", MemoryTier.EPHEMERAL, importance=9, keywords="另一个关键词"
        )
        assert second == first
        rows = await self.db.fetchall(
            "SELECT * FROM memories WHERE soul_id = ? AND memory_tier = 1", ("soul-a",)
        )
        assert len(rows) == 1
        assert rows[0]["last_accessed_at"] >= before
        # refresh only: original keywords / importance are kept as-is.
        assert rows[0]["keywords"] == "火锅"
        assert rows[0]["importance"] == 3

    async def test_dedupe_is_scoped_per_soul(self):
        first = await self._add("同名记忆", MemoryTier.EPHEMERAL, soul_id="soul-a")
        second = await self._add("同名记忆", MemoryTier.EPHEMERAL, soul_id="soul-b")
        assert second != first
        count = await self.db.fetchone("SELECT COUNT(*) AS n FROM memories")
        assert count["n"] == 2

    async def test_dedupe_only_applies_to_tier1(self):
        first = await self._add("跨层级内容", MemoryTier.EPHEMERAL)
        second = await self._add("跨层级内容", MemoryTier.RECENT, importance=6)
        third = await self._add(
            "跨层级内容", MemoryTier.CORE, importance=9, user_tier=3
        )
        assert len({first, second, third}) == 3

    async def test_default_cap_is_30_fifo(self):
        ids = []
        base = int(time.time()) - 500
        for index in range(31):
            ids.append(
                await self._add(
                    "默认上限记忆%d" % index,
                    MemoryTier.EPHEMERAL,
                    importance=2,
                    created_at=base + index,
                )
            )
        rows = await self.db.fetchall(
            "SELECT id FROM memories WHERE soul_id = ? AND memory_tier = 1"
            " ORDER BY created_at ASC, id ASC",
            ("soul-a",),
        )
        assert len(rows) == 30
        remaining = {int(row["id"]) for row in rows}
        assert ids[0] not in remaining  # oldest evicted first
        assert ids[-1] in remaining

    async def test_configured_cap_trims_oldest_rows(self):
        self.config["memory_and_context_config"]["max_tier1_memories"] = 3
        base = int(time.time()) - 500
        ids = [
            await self._add(
                "小上限记忆%d" % index,
                MemoryTier.EPHEMERAL,
                importance=2,
                created_at=base + index,
            )
            for index in range(5)
        ]
        rows = await self.db.fetchall(
            "SELECT id FROM memories WHERE soul_id = ? AND memory_tier = 1"
            " ORDER BY created_at ASC, id ASC",
            ("soul-a",),
        )
        assert [int(row["id"]) for row in rows] == ids[2:]

    async def test_cap_is_per_soul(self):
        self.config["memory_and_context_config"]["max_tier1_memories"] = 2
        base = int(time.time()) - 500
        for soul_id in ("soul-a", "soul-b"):
            for index in range(3):
                await self._add(
                    "%s 记忆%d" % (soul_id, index),
                    MemoryTier.EPHEMERAL,
                    importance=2,
                    soul_id=soul_id,
                    created_at=base + index,
                )
        rows = await self.db.fetchall(
            "SELECT soul_id, COUNT(*) AS n FROM memories"
            " WHERE memory_tier = 1 GROUP BY soul_id"
        )
        assert {row["soul_id"]: row["n"] for row in rows} == {
            "soul-a": 2,
            "soul-b": 2,
        }

    async def test_cap_does_not_touch_tier2_tier3(self):
        self.config["memory_and_context_config"]["max_tier1_memories"] = 2
        for index in range(5):
            await self._add("T2 记忆%d" % index, MemoryTier.RECENT, importance=6)
        for index in range(3):
            await self._add(
                "T3 记忆%d" % index,
                MemoryTier.CORE,
                importance=9,
                user_tier=3,
            )
        for index in range(4):
            await self._add("T1 记忆%d" % index, MemoryTier.EPHEMERAL, importance=2)

        counts = await self.db.fetchall(
            "SELECT memory_tier, COUNT(*) AS n FROM memories GROUP BY memory_tier"
        )
        by_tier = {int(row["memory_tier"]): row["n"] for row in counts}
        assert by_tier == {1: 2, 2: 5, 3: 3}


# ===========================================================================
# 4. cleanup_tier1 — 48h expiry + per-Soul overflow
# ===========================================================================
class TestCleanupTier1(MemoryTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.now = int(time.time())

    async def test_expired_boundary_and_fresh_rows(self):
        expired = await self._add(
            "超过48小时", MemoryTier.EPHEMERAL, created_at=self.now - 172801
        )
        boundary = await self._add(
            "刚好48小时", MemoryTier.EPHEMERAL, created_at=self.now - 172800
        )
        fresh = await self._add(
            "刚刚写入", MemoryTier.EPHEMERAL, created_at=self.now - 100
        )

        stats = await self.manager.cleanup_tier1(now_override=self.now)
        assert stats == {"deleted_expired": 1, "deleted_overflow": 0}
        assert await self.db.fetchone("SELECT id FROM memories WHERE id = ?", (expired,)) is None
        assert await self.db.fetchone("SELECT id FROM memories WHERE id = ?", (boundary,)) is not None
        assert await self.db.fetchone("SELECT id FROM memories WHERE id = ?", (fresh,)) is not None

    async def test_default_clock_is_used_without_override(self):
        expired = await self._add(
            "真实时间过期", MemoryTier.EPHEMERAL, created_at=int(time.time()) - 200000
        )
        stats = await self.manager.cleanup_tier1()
        assert stats["deleted_expired"] == 1
        assert await self.db.fetchone("SELECT id FROM memories WHERE id = ?", (expired,)) is None

    async def test_overflow_after_limit_is_lowered(self):
        base = self.now - 1000
        ids = [
            await self._add(
                "溢出记忆%d" % index,
                MemoryTier.EPHEMERAL,
                importance=2,
                created_at=base + index,
            )
            for index in range(5)
        ]
        self.config["memory_and_context_config"]["max_tier1_memories"] = 2

        stats = await self.manager.cleanup_tier1(now_override=self.now)
        assert stats["deleted_overflow"] == 3
        rows = await self.db.fetchall(
            "SELECT id FROM memories WHERE soul_id = ? AND memory_tier = 1"
            " ORDER BY created_at ASC, id ASC",
            ("soul-a",),
        )
        assert [int(row["id"]) for row in rows] == ids[3:]

    async def test_expired_and_overflow_are_counted_separately(self):
        for index in range(4):
            await self._add(
                "过期记忆%d" % index,
                MemoryTier.EPHEMERAL,
                created_at=self.now - 200000 - index,
            )
        for index in range(4):
            await self._add(
                "新鲜记忆%d" % index,
                MemoryTier.EPHEMERAL,
                created_at=self.now - 1000 + index,
            )
        self.config["memory_and_context_config"]["max_tier1_memories"] = 2

        stats = await self.manager.cleanup_tier1(now_override=self.now)
        assert stats == {"deleted_expired": 4, "deleted_overflow": 2}
        remaining = await self.db.fetchall("SELECT id FROM memories WHERE memory_tier = 1")
        assert len(remaining) == 2

    async def test_overflow_is_per_soul(self):
        for soul_id in ("soul-a", "soul-b"):
            for index in range(3):
                await self._add(
                    "%s 记忆%d" % (soul_id, index),
                    MemoryTier.EPHEMERAL,
                    soul_id=soul_id,
                    created_at=self.now - 1000 + index,
                )
        self.config["memory_and_context_config"]["max_tier1_memories"] = 2

        stats = await self.manager.cleanup_tier1(now_override=self.now)
        assert stats == {"deleted_expired": 0, "deleted_overflow": 2}
        counts = await self.db.fetchall(
            "SELECT soul_id, COUNT(*) AS n FROM memories WHERE memory_tier = 1 GROUP BY soul_id"
        )
        assert {row["soul_id"]: row["n"] for row in counts} == {
            "soul-a": 2,
            "soul-b": 2,
        }

    async def test_tier2_tier3_are_never_deleted(self):
        t2 = await self._add("T2 永久候选", MemoryTier.RECENT, importance=6)
        t3 = await self._add("T3 核心", MemoryTier.CORE, importance=9, user_tier=3)
        for index in range(5):
            await self._add(
                "T1 记忆%d" % index,
                MemoryTier.EPHEMERAL,
                created_at=self.now - 300000 - index,
            )
        self.config["memory_and_context_config"]["max_tier1_memories"] = 1

        stats = await self.manager.cleanup_tier1(now_override=self.now)
        assert stats["deleted_expired"] == 5
        for memory_id in (t2, t3):
            assert await self.db.fetchone(
                "SELECT id FROM memories WHERE id = ?", (memory_id,)
            ) is not None
        count = await self.db.fetchone("SELECT COUNT(*) AS n FROM memories")
        assert count["n"] == 2

    async def test_empty_database_returns_zero_stats(self):
        stats = await self.manager.cleanup_tier1(now_override=self.now)
        assert stats == {"deleted_expired": 0, "deleted_overflow": 0}


# ===========================================================================
# 5. CyberSurfingPipeline.surf_once / is_enabled
# ===========================================================================
class _RecordingMutator:
    def __init__(self):
        self.calls = []

    async def on_video_input(self, **kwargs):
        self.calls.append(kwargs)


class _FakeVisionProcessor:
    def __init__(self, score=90):
        self.score = score
        self.calls = []

    async def watch_and_evaluate(
        self, video, soul_name, soul_archetype, surfing_preferences, umo=None
    ):
        self.calls.append(
            {
                "video": video,
                "soul_name": soul_name,
                "soul_archetype": soul_archetype,
                "surfing_preferences": list(surfing_preferences or []),
                "umo": umo,
            }
        )
        return VideoReviewResult(
            video=video,
            score=self.score,
            commentary="笑死我了hhh",
            visual_notes="画面笔记",
        )


class SurfingPipelineTestBase(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.memory = MemoryManager(self.db, StubModelHub(), self.config)
        self.session_buffer = SessionBuffer(self.config)
        self.mutator = _RecordingMutator()
        self.pipeline = CyberSurfingPipeline(
            model_hub=StubModelHub(),
            memory_manager=self.memory,
            session_buffer=self.session_buffer,
            schedule_mutator=self.mutator,
            plugin_config=self.config,
        )
        self.bili_feed = []
        self.douyin_feed = []
        self.adapter_calls = {"bilibili": 0, "douyin": 0}
        self.vision = _FakeVisionProcessor()
        self.pipeline.vision_processor = self.vision
        self._patch_adapters()
        self.soul = SimpleNamespace(
            soul_id="soul-surf",
            name="星野",
            archetype="爱冲浪的测试角色",
            surfing_preferences=["搞笑整活"],
            settings={"surfing_enabled": True},
        )

    def _patch_adapters(self):
        test = self
        pipeline_module = mods.surfing_pipeline

        class _FakeBilibiliAdapter:
            def __init__(self, cookie=""):
                self.cookie = cookie

            async def fetch_recommend_feed(self, preferences=None, limit=5):
                test.adapter_calls["bilibili"] += 1
                return list(test.bili_feed)

        class _FakeDouyinAdapter:
            def __init__(self, api_base=""):
                self.api_base = api_base

            async def fetch_recommend_feed(self, preferences=None, limit=5):
                test.adapter_calls["douyin"] += 1
                return list(test.douyin_feed)

        for patcher in (
            mock.patch.object(pipeline_module, "BilibiliAdapter", _FakeBilibiliAdapter),
            mock.patch.object(pipeline_module, "DouyinAdapter", _FakeDouyinAdapter),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _tier1_rows(self):
        return await self.db.fetchall(
            "SELECT * FROM memories WHERE soul_id = ? AND memory_tier = 1",
            (self.soul.soul_id,),
        )

    async def _history_rows(self):
        return await self.db.fetchall("SELECT * FROM surfing_history")


class TestSurfingEnabledSwitch(SurfingPipelineTestBase):
    async def test_defaults_to_enabled(self):
        assert self.pipeline.is_enabled(self.soul) is True
        assert self.pipeline.is_enabled(None) is True
        assert self.pipeline.is_enabled(SimpleNamespace(settings={})) is True

    async def test_soul_setting_wins_over_global(self):
        self.config["surfing_config"]["surfing_enabled"] = False
        assert self.pipeline.is_enabled(SimpleNamespace(settings={})) is False
        assert (
            self.pipeline.is_enabled(SimpleNamespace(settings={"surfing_enabled": True}))
            is True
        )
        assert (
            self.pipeline.is_enabled(SimpleNamespace(settings={"surfing_enabled": False}))
            is False
        )
        # Legacy duck-typed Soul exposing the flag directly (no settings dict).
        assert self.pipeline.is_enabled(SimpleNamespace(surfing_enabled=False)) is False

    async def test_soul_switch_can_reenable_global_off(self):
        self.config["surfing_config"]["surfing_enabled"] = False
        self.config["surfing_config"]["enable_bili_surfing"] = True
        self.bili_feed = [_video("bv-soul-on")]
        review = await self.pipeline.surf_once(
            self.soul, mods.state_machine.BotState.IDLE
        )
        assert review is not None
        assert review.video.video_id == "bv-soul-on"

    async def test_disabled_soul_returns_none_even_for_manual_trigger(self):
        self.soul.settings = {"surfing_enabled": False}
        self.bili_feed = [_video("bv-disabled")]
        review = await self.pipeline.surf_once(
            self.soul,
            mods.state_machine.BotState.IDLE,
            ignore_state_gate=True,
        )
        assert review is None
        assert self.adapter_calls == {"bilibili": 0, "douyin": 0}
        assert self.vision.calls == []
        assert await self._tier1_rows() == []
        assert await self._history_rows() == []

    async def test_global_switch_off_returns_none_when_soul_unset(self):
        self.config["surfing_config"]["surfing_enabled"] = False
        self.soul.settings = {}
        self.bili_feed = [_video("bv-global-off")]
        assert self.pipeline.is_enabled(self.soul) is False
        review = await self.pipeline.surf_once(
            self.soul, mods.state_machine.BotState.IDLE, ignore_state_gate=True
        )
        assert review is None
        assert self.adapter_calls == {"bilibili": 0, "douyin": 0}

    async def test_state_gate_still_applies_when_enabled(self):
        self.bili_feed = [_video("bv-resting")]
        review = await self.pipeline.surf_once(
            self.soul, mods.state_machine.BotState.RESTING
        )
        assert review is None
        assert self.adapter_calls == {"bilibili": 0, "douyin": 0}


class TestSurfOnceCycle(SurfingPipelineTestBase):
    async def test_successful_cycle_records_video(self):
        video = _video("bv-success", title="超搞笑的翻车现场")
        self.bili_feed = [video]

        review = await self.pipeline.surf_once(
            self.soul, mods.state_machine.BotState.IDLE, umo="aiocqhttp:FriendMessage:l3"
        )
        assert review is not None
        assert review.video.video_id == "bv-success"
        assert review.score == 90
        assert review.is_fallback is False
        assert review.video.title == "超搞笑的翻车现场"
        assert self.pipeline._watched_ids == {"bv-success"}
        assert self.adapter_calls["bilibili"] == 1
        assert self.adapter_calls["douyin"] == 0
        assert len(self.vision.calls) == 1
        assert self.vision.calls[0]["umo"] == "aiocqhttp:FriendMessage:l3"

        # Tier 1 ephemeral memory + surfing_history persistence
        rows = await self._tier1_rows()
        assert len(rows) == 1
        assert "超搞笑的翻车现场" in rows[0]["content"]
        history = await self._history_rows()
        assert len(history) == 1
        assert history[0]["video_id"] == "bv-success"
        assert history[0]["score"] == 90
        assert history[0]["worth_sharing"] == 1

        # Session buffer + bounded mutation callback
        watched_text = self.session_buffer.get_recent_watched_video_text()
        assert "超搞笑的翻车现场" in watched_text
        assert len(self.mutator.calls) == 1
        assert self.mutator.calls[0]["video_score"] == 90

    async def test_candidate_exhaustion_returns_none_without_rewatch(self):
        self.bili_feed = [_video("bv-only", title="唯一候选视频")]

        first = await self.pipeline.surf_once(
            self.soul, mods.state_machine.BotState.IDLE
        )
        assert first is not None
        assert first.video.video_id == "bv-only"

        second = await self.pipeline.surf_once(
            self.soul, mods.state_machine.BotState.IDLE
        )
        assert second is None
        third = await self.pipeline.surf_once(
            self.soul,
            mods.state_machine.BotState.IDLE,
            ignore_state_gate=True,
        )
        assert third is None

        assert len(self.vision.calls) == 1  # never re-evaluated / no candidates[0] fallback
        assert len(await self._history_rows()) == 1
        assert len(await self._tier1_rows()) == 1

    async def test_exhaustion_across_platform_fallback(self):
        # Bilibili empty -> Douyin candidate; second cycle exhausts both sources.
        self.bili_feed = []
        self.douyin_feed = [_video("dy-only", title="抖音唯一候选", platform="douyin")]

        first = await self.pipeline.surf_once(
            self.soul, mods.state_machine.BotState.IDLE
        )
        assert first is not None
        assert first.video.video_id == "dy-only"

        second = await self.pipeline.surf_once(
            self.soul, mods.state_machine.BotState.IDLE
        )
        assert second is None
        assert self.adapter_calls["bilibili"] == 2
        assert self.adapter_calls["douyin"] == 2
        assert len(self.vision.calls) == 1

    async def test_new_candidate_is_picked_after_exhaustion(self):
        self.bili_feed = [_video("bv-1"), _video("bv-2")]
        first = await self.pipeline.surf_once(
            self.soul, mods.state_machine.BotState.IDLE
        )
        second = await self.pipeline.surf_once(
            self.soul, mods.state_machine.BotState.IDLE
        )
        assert first is not None and second is not None
        assert {first.video.video_id, second.video.video_id} == {"bv-1", "bv-2"}


# ===========================================================================
# 6. VideoReviewResult.is_fallback / vision fallback
# ===========================================================================
class _RaisingModelHub:
    async def call_llm_json(self, **kwargs):
        raise RuntimeError("vision provider down")


class TestVideoVisionFallback(unittest.IsolatedAsyncioTestCase):
    def _processor(self, json_result=None, raising=False):
        hub = _RaisingModelHub() if raising else StubModelHub(json_result=json_result)
        return VideoVisionProcessor(model_hub=hub)

    async def _evaluate(self, json_result=None, raising=False):
        processor = self._processor(json_result=json_result, raising=raising)
        return await processor.watch_and_evaluate(
            video=_video(),
            soul_name="星野",
            soul_archetype="爱冲浪的测试角色",
            surfing_preferences=["搞笑整活"],
        )

    def test_review_result_default_is_not_fallback(self):
        result = VideoReviewResult(video=_video(), score=77, commentary="好看")
        assert result.is_fallback is False
        payload = result.to_dict()
        assert "is_fallback" in payload
        assert payload["is_fallback"] is False

    async def test_normal_success_keeps_original_behaviour(self):
        result = await self._evaluate(
            {"score": 90, "commentary": "笑死我了", "visual_notes": "翻车名场面"}
        )
        assert result.score == 90
        assert result.commentary == "笑死我了"
        assert result.visual_notes == "翻车名场面"
        assert result.is_fallback is False
        assert result.to_dict()["is_fallback"] is False

    async def test_normal_success_without_visual_notes_uses_default(self):
        result = await self._evaluate({"score": 88, "commentary": "哈哈哈哈"})
        assert result.score == 88
        assert result.is_fallback is False
        assert result.visual_notes  # default notes filled

    async def test_fallback_when_llm_returns_none(self):
        result = await self._evaluate(None)
        assert result.score == 60
        assert result.is_fallback is True
        assert result.commentary
        assert result.to_dict()["is_fallback"] is True
        assert result.to_dict()["score"] == 60

    async def test_fallback_when_llm_output_is_not_a_dict(self):
        result = await self._evaluate("这不是 JSON")
        assert result.score == 60
        assert result.is_fallback is True

    async def test_fallback_when_commentary_missing(self):
        result = await self._evaluate({"score": 93, "visual_notes": "只有笔记"})
        assert result.score == 60
        assert result.is_fallback is True

    async def test_fallback_when_score_unparseable(self):
        result = await self._evaluate({"score": "非常高", "commentary": "哈哈哈哈"})
        assert result.score == 60
        assert result.is_fallback is True

    async def test_fallback_when_llm_raises(self):
        result = await self._evaluate(raising=True)
        assert result.score == 60
        assert result.is_fallback is True
        assert result.score != 80  # old 80-score fallback branch is gone


if __name__ == "__main__":
    unittest.main()
