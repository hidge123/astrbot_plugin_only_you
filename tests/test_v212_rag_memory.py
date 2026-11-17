"""v2.12 长期记忆 RAG 重构回归。

冻结以下契约：

1. ``memories`` 表幂等新增 ``memory_type / event_at / embedding_json /
   superseded_by / last_reinforced_at``，旧库自动迁移且默认值可用；
2. ``ProviderFactory`` 能列出 / 解析 AstrBot 的 embedding 与 rerank 提供商，
   ``ModelHub`` 新增两角色的 ``off | provider | source`` 三档配置与调用封装；
3. 写路径：T1/T2/T3 全层级语义去重（余弦 >= 0.85 / Jaccard >= 0.65）、
   state 版本替代（``superseded_by``）、真实事件时间 ``event_at``；
4. 读路径：三路混合召回（向量 / BM25 / 分层保底）只取未归档记忆；
5. 强化门闸：语义 < 0.55 不计数、6 小时冷却、T3 晋升只看
   profile/relationship + importance >= 8 + 8 次命中 + 至少 1 天；
6. 提炼：单批最多 3 条、默认最高 T2、注入身份归一表与既有记忆。
"""

import os
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace

from _helpers import (
    DatabaseAsyncTestCase,
    FakeContext,
    FakeEvent,
    FakeRequest,
    FakeProviderManager,
    StubModelHub,
    load_plugin_modules,
    make_plugin_config,
    make_run,
    make_slot,
    make_soul,
    make_user,
)

mods = load_plugin_modules()
MemoryManager = mods.memory_manager.MemoryManager
MemoryTier = mods.memory_manager.MemoryTier
ContextBudgetManager = mods.context_budget.ContextBudgetManager
SessionBuffer = mods.context_budget.SessionBuffer
ProviderFactory = mods.provider_factory.ProviderFactory
ModelHub = mods.model_hub.ModelHub
normalize_model_config = mods.model_hub.normalize_model_config
StateMachine = mods.state_machine


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeEmbeddingProvider:
    def __init__(self, vectors=None, provider_id="emb-1", model="bge-m3"):
        self.vectors = dict(vectors or {})
        self.calls = []
        self.provider_config = {
            "id": provider_id,
            "provider_type": "embedding",
            "model": model,
            "type": "openai_embedding",
        }

    async def get_embeddings(self, texts):
        self.calls.append(list(texts))
        return [list(self.vectors.get(str(text), [0.0, 1.0])) for text in texts]

    async def get_embedding(self, text):
        self.calls.append([str(text)])
        return list(self.vectors.get(str(text), [0.0, 1.0]))


class FakeRerankProvider:
    def __init__(self, provider_id="rrk-1", model="bge-reranker-v2-m3"):
        self.calls = []
        self.provider_config = {
            "id": provider_id,
            "provider_type": "rerank",
            "model": model,
            "type": "openai_rerank",
        }

    async def rerank(self, query, documents, top_n=5):
        self.calls.append((query, list(documents), top_n))
        scored = []
        for index, document in enumerate(documents):
            score = 0.1
            text = str(document)
            if "数电" in text or "电子" in text or "考试" in text:
                score = 0.92
            elif "火锅" in text:
                score = 0.45
            scored.append(SimpleNamespace(index=index, relevance_score=score))
        scored.sort(key=lambda item: item.relevance_score, reverse=True)
        return scored[: max(1, int(top_n or 5))]


class VectorStubHub(StubModelHub):
    """ModelHub stub exposing embedding / rerank on top of the LLM stub."""

    def __init__(self, json_result=None, text_result="", vectors=None, rerank=None):
        super().__init__(json_result=json_result, text_result=text_result)
        self.vectors = dict(vectors or {})
        self.rerank_provider = rerank
        self.embedding_calls = []

    async def get_embedding(self, text, soul_id=None):
        self.embedding_calls.append((str(text), soul_id))
        return list(self.vectors.get(str(text), []))

    async def get_embeddings(self, texts, soul_id=None, umo=None):
        self.embedding_calls.append((list(texts), soul_id))
        return [list(self.vectors.get(str(text), [])) for text in texts]

    async def rerank_documents(self, query, documents, top_n=None, soul_id=None, umo=None):
        if self.rerank_provider is None:
            return []
        return await self.rerank_provider.rerank(query, documents, top_n or len(documents))


# ---------------------------------------------------------------------------
# 1. Schema migration
# ---------------------------------------------------------------------------
class TestMemorySchemaMigration(unittest.TestCase):
    def test_legacy_database_gets_new_columns_with_defaults(self):
        import asyncio

        tmpdir = tempfile.mkdtemp(prefix="onlyyou-v212-")
        path = os.path.join(tmpdir, "legacy.db")
        connection = sqlite3.connect(path)
        connection.execute(
            """
            CREATE TABLE memories (
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
        connection.execute(
            "INSERT INTO memories (soul_id, memory_tier, content, keywords, importance,"
            " created_at) VALUES ('s', 2, '旧记忆', '旧', 5, 1);"
        )
        connection.commit()
        connection.close()

        async def _run():
            db = mods.db.DatabaseManager(path)
            await db.initialize()
            columns = {
                row["name"] for row in await db.fetchall("PRAGMA table_info(memories)")
            }
            row = await db.fetchone("SELECT * FROM memories")
            await db.close()
            return columns, row

        loop = asyncio.new_event_loop()
        try:
            columns, row = loop.run_until_complete(_run())
        finally:
            loop.close()
        assert {
            "memory_type",
            "event_at",
            "embedding_json",
            "superseded_by",
            "last_reinforced_at",
        } <= columns
        assert row["memory_type"] == "episode"
        assert row["event_at"] == 0
        assert row["embedding_json"] == ""
        assert row["superseded_by"] is None
        assert row["last_reinforced_at"] == 0


# ---------------------------------------------------------------------------
# 2. ProviderFactory + ModelHub RAG roles
# ---------------------------------------------------------------------------
class TestVectorProviders(unittest.TestCase):
    def _factory(self):
        manager = FakeProviderManager(
            sources=[
                {
                    "id": "emb-src",
                    "type": "openai_embedding",
                    "provider_type": "embedding",
                    "api_base": "https://example/v1",
                    "key": "k",
                    "enable": True,
                },
                {
                    "id": "rrk-src",
                    "type": "openai_rerank",
                    "provider_type": "rerank",
                    "api_base": "https://example/v1",
                    "key": "k",
                    "enable": True,
                },
            ],
            providers=[
                {"id": "emb-1", "provider_source_id": "emb-src", "provider_type": "embedding",
                 "model": "bge-m3", "enable": True},
                {"id": "rrk-1", "provider_source_id": "rrk-src", "provider_type": "rerank",
                 "model": "bge-reranker", "enable": True},
            ],
        )
        embedding = FakeEmbeddingProvider()
        rerank = FakeRerankProvider()
        manager.embedding_provider_insts = [embedding]
        manager.rerank_provider_insts = [rerank]
        manager.inst_map = {"emb-1": embedding, "rrk-1": rerank}
        context = FakeContext(manager)
        context.providers_by_id = {"emb-1": embedding, "rrk-1": rerank}
        return ProviderFactory(context), embedding, rerank

    def test_lists_embedding_and_rerank_providers(self):
        factory, _emb, _rrk = self._factory()
        embedding_ids = {item["id"] for item in factory.list_embedding_providers()}
        rerank_ids = {item["id"] for item in factory.list_rerank_providers()}
        assert "emb-1" in embedding_ids
        assert "rrk-1" in rerank_ids
        assert not embedding_ids & rerank_ids

    def test_resolves_provider_mode_instances(self):
        import asyncio

        factory, embedding, rerank = self._factory()

        async def _run():
            emb = await factory.get_embedding_provider(
                {"mode": "provider", "provider_id": "emb-1"}
            )
            rrk = await factory.get_rerank_provider(
                {"mode": "provider", "provider_id": "rrk-1"}
            )
            missing = await factory.get_embedding_provider(
                {"mode": "provider", "provider_id": "nope"}
            )
            wrong_kind = await factory.get_embedding_provider(
                {"mode": "provider", "provider_id": "rrk-1"}
            )
            off = await factory.get_rerank_provider({"mode": "off"})
            return emb, rrk, missing, wrong_kind, off

        loop = asyncio.new_event_loop()
        try:
            emb, rrk, missing, wrong_kind, off = loop.run_until_complete(_run())
        finally:
            loop.close()
        assert emb is embedding
        assert rrk is rerank
        assert missing is None
        assert wrong_kind is None
        assert off is None


class TestVectorSourceMode(unittest.TestCase):
    def test_source_mode_builds_plugin_owned_instance(self):
        import asyncio

        class _EmbeddingCls:
            def __init__(self, config, settings=None):
                self.provider_config = dict(config or {})
                self.settings = settings or {}

            async def get_embeddings(self, texts):
                return [[0.5, 0.5] for _ in texts]

        manager = FakeProviderManager(
            sources=[
                {
                    "id": "emb-src",
                    "type": "openai_embedding",
                    "provider_type": "embedding",
                    "enable": True,
                }
            ],
            providers=[],
        )
        manager.provider_cls_map["openai_embedding"] = SimpleNamespace(
            cls_type=_EmbeddingCls
        )
        factory = ProviderFactory(FakeContext(manager))

        async def _run():
            return await factory.get_embedding_provider(
                {"mode": "source", "source_id": "emb-src", "model": "bge-m3"}
            )

        loop = asyncio.new_event_loop()
        try:
            instance = loop.run_until_complete(_run())
        finally:
            loop.close()
        assert isinstance(instance, _EmbeddingCls)
        assert instance.provider_config["model"] == "bge-m3"
        assert instance.provider_config["provider_type"] == "embedding"
        assert "openai_embedding" in manager.dynamic_import_calls
        # the plugin-owned instance is cached by (mode, source, model)
        loop = asyncio.new_event_loop()
        try:
            again = loop.run_until_complete(_run())
        finally:
            loop.close()
        assert again is instance


class TestModelHubRagRoles(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.manager = FakeProviderManager(
            sources=[
                {
                    "id": "emb-src",
                    "type": "openai_embedding",
                    "provider_type": "embedding",
                    "enable": True,
                }
            ],
            providers=[
                {"id": "emb-1", "provider_source_id": "emb-src",
                 "provider_type": "embedding", "model": "bge-m3", "enable": True}
            ],
        )
        self.embedding = FakeEmbeddingProvider(vectors={"数电": [1.0, 0.0]})
        self.rerank = FakeRerankProvider()
        self.manager.inst_map = {"emb-1": self.embedding, "rrk-1": self.rerank}
        self.manager.embedding_provider_insts = [self.embedding]
        self.manager.rerank_provider_insts = [self.rerank]
        self.context = FakeContext(self.manager)
        self.context.providers_by_id = {"emb-1": self.embedding, "rrk-1": self.rerank}
        self.factory = ProviderFactory(self.context)
        self.hub = ModelHub(
            context=self.context,
            plugin_config=make_plugin_config(),
            db=self.db,
            provider_factory=self.factory,
        )
        await self.hub.load()

    def test_defaults_are_off(self):
        cfg = normalize_model_config(None)
        assert cfg["embedding"] == {
            "mode": "off", "provider_id": "", "source_id": "", "model": ""
        }
        assert cfg["rerank"]["mode"] == "off"

    async def test_save_and_status_roundtrip(self):
        saved = await self.hub.save_model_config(
            {
                "embedding": {"mode": "provider", "provider_id": "emb-1"},
                "rerank": {"mode": "off"},
            }
        )
        assert saved["embedding"]["mode"] == "provider"
        status = await self.hub.get_status()
        assert status["embedding"]["configured"] is True
        assert status["embedding"]["ok"] is True
        assert status["rerank"]["mode"] == "off"
        assert status["rerank"]["configured"] is False

    async def test_get_embeddings_and_rerank_documents(self):
        await self.hub.save_model_config(
            {
                "embedding": {"mode": "provider", "provider_id": "emb-1"},
                "rerank": {"mode": "provider", "provider_id": "rrk-1"},
            }
        )
        vectors = await self.hub.get_embeddings(["数电", "别的"])
        assert vectors[0] == [1.0, 0.0]
        assert len(vectors) == 2
        assert await self.hub.get_embedding("数电") == [1.0, 0.0]

        rows = await self.hub.rerank_documents(
            "数电备考", ["阿泽爱吃火锅", "阿泽在备考数电"], top_n=2
        )
        assert rows[0]["index"] == 1
        assert rows[0]["score"] > rows[1]["score"]

    async def test_test_role_reports_dimension_and_scores(self):
        await self.hub.save_model_config(
            {
                "embedding": {"mode": "provider", "provider_id": "emb-1"},
                "rerank": {"mode": "provider", "provider_id": "rrk-1"},
            }
        )
        embedding_result = await self.hub.test_role("embedding")
        assert embedding_result["status"] == "ok"
        assert embedding_result["dimension"] == 2
        rerank_result = await self.hub.test_role("rerank")
        assert rerank_result["status"] == "ok"
        assert rerank_result["results"][0]["index"] == 0

    async def test_off_role_degrades_to_empty(self):
        assert await self.hub.get_embeddings(["数电"]) == []
        assert await self.hub.rerank_documents("q", ["a", "b"]) == []
        status = await self.hub.test_role("embedding")
        assert status["status"] == "error"


# ---------------------------------------------------------------------------
# 3. Memory write path: dedupe / supersede / event_at
# ---------------------------------------------------------------------------
class MemoryRagTestBase(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.hub = VectorStubHub()
        self.manager = MemoryManager(self.db, self.hub, self.config)

    async def _add(self, content, **kwargs):
        kwargs.setdefault("soul_id", "soul-a")
        return await self.manager.add_memory(content=content, **kwargs)


class TestMemoryWritePath(MemoryRagTestBase):
    async def test_jaccard_duplicate_is_not_inserted(self):
        first = await self._add("阿泽爱吃川渝火锅", keywords="火锅", importance=3,
                                memory_tier=MemoryTier.EPHEMERAL)
        second = await self._add("阿泽爱吃川渝火锅", keywords="口味", importance=9,
                                 memory_tier=MemoryTier.RECENT)
        assert second == first
        assert len(await self.db.fetchall("SELECT id FROM memories")) == 1
        row = await self.db.fetchone("SELECT * FROM memories WHERE id = ?", (first,))
        assert set(row["keywords"].split(",")) == {"火锅", "口味"}
        assert row["importance"] == 9

    async def test_vector_duplicate_uses_cosine_threshold(self):
        first = await self._add("阿泽在备考数电", embedding=[1.0, 0.0])
        duplicate = await self._add("数电备考中", embedding=[0.9, 0.4359])
        different = await self._add("阿泽爱吃火锅", embedding=[0.0, 1.0])
        assert duplicate == first
        assert different != first
        assert len(await self.db.fetchall("SELECT id FROM memories")) == 2

    async def test_state_memory_is_superseded_by_new_state(self):
        old = await self._add(
            "阿泽可能在备考数电", memory_type="state", embedding=[1.0, 0.0]
        )
        new = await self._add(
            "确认阿泽正在备考数电", memory_type="state", embedding=[0.8, 0.6]
        )
        assert new != old
        old_row = await self.db.fetchone("SELECT * FROM memories WHERE id = ?", (old,))
        assert old_row["superseded_by"] == new
        active = await self.manager.list_memories(
            soul_id="soul-a", include_superseded=False
        )
        assert [item.id for item in active] == [new]

    async def test_state_supersede_works_without_embeddings(self):
        old = await self._add("阿泽在备考数电", memory_type="state")
        new = await self._add("阿泽确认在备考数电", memory_type="state")
        assert new != old
        row = await self.db.fetchone("SELECT superseded_by FROM memories WHERE id = ?", (old,))
        assert row["superseded_by"] == new

    async def test_explicit_supersedes_id_wins(self):
        old = await self._add("旧事实", memory_type="profile", embedding=[1.0, 0.0])
        new = await self._add(
            "新事实", memory_type="profile", embedding=[0.0, 1.0], supersedes_id=old
        )
        old_row = await self.db.fetchone("SELECT superseded_by FROM memories WHERE id = ?", (old,))
        assert old_row["superseded_by"] == new

    async def test_event_at_is_stored_and_used_as_anchor(self):
        memory_id = await self._add(
            "阿泽上周去考科目二", event_at=1700000000, created_at=1700100000
        )
        item = (
            await self.manager.list_memories(soul_id="soul-a")
        )[0]
        assert item.event_at == 1700000000
        assert item.effective_event_at == 1700000000
        assert item.to_dict()["event_at"] == 1700000000
        assert item.to_dict()["memory_type"] == "episode"
        _ = memory_id


# ---------------------------------------------------------------------------
# 4. Hybrid retrieval
# ---------------------------------------------------------------------------
class TestHybridRetrieval(MemoryRagTestBase):
    async def test_tier_bucket_guarantee_keeps_relevant_t1(self):
        for index in range(12):
            await self._add(
                self._distinct("闲聊", index),
                memory_tier=MemoryTier.CORE,
                user_tier=3,
                importance=9,
                embedding=[0.0, 1.0],
            )
        relevant = await self._add(
            "阿泽在备考数电", memory_tier=MemoryTier.EPHEMERAL, embedding=[1.0, 0.0]
        )
        pool = await self.manager.retrieve_hybrid_candidates(
            soul_id="soul-a", query_text="数电", query_embedding=[1.0, 0.0], limit=6
        )
        ids = {item.id for item in pool}
        assert relevant in ids
        assert len(pool) <= 6

    async def test_archived_rows_never_enter_candidates(self):
        old = await self._add("旧状态", memory_type="state", embedding=[1.0, 0.0])
        new = await self._add("新状态", memory_type="state", embedding=[0.8, 0.6])
        assert new != old
        pool = await self.manager.retrieve_hybrid_candidates(
            soul_id="soul-a", query_text="状态", query_embedding=[1.0, 0.0], limit=10
        )
        assert old not in {item.id for item in pool}
        assert await self.manager.sample_muse_memories(
            "soul-a", exclude_ids=[], pick=5
        )
        assert old not in {
            item.id
            for item in await self.manager.sample_muse_memories(
                "soul-a", exclude_ids=[], pick=5
            )
        }

    @classmethod
    def _distinct(cls, prefix, index):
        alphabet = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥"
        return f"{prefix}{alphabet[index % 20]}{alphabet[(index * 7 + 3) % 20]}{index}"


# ---------------------------------------------------------------------------
# 5. Reinforcement gates
# ---------------------------------------------------------------------------
class TestReinforcementGates(MemoryRagTestBase):
    async def test_low_semantic_score_does_not_count(self):
        mid = await self._add("数电备考", memory_type="profile", importance=8,
                              memory_tier=MemoryTier.RECENT)
        now = int(time.time())
        assert await self.manager.reinforce_memories(
            [mid], sem_scores={mid: 0.3}, now_override=now
        ) == 0
        row = await self.db.fetchone("SELECT access_count FROM memories WHERE id = ?", (mid,))
        assert row["access_count"] == 0

    async def test_six_hour_cooldown_blocks_repeated_bursts(self):
        mid = await self._add("数电备考", memory_type="profile", importance=8,
                              memory_tier=MemoryTier.RECENT)
        now = int(time.time())
        assert await self.manager.reinforce_memories(
            [mid], sem_scores={mid: 0.9}, now_override=now
        ) == 1
        assert await self.manager.reinforce_memories(
            [mid], sem_scores={mid: 0.9}, now_override=now + 3600
        ) == 0
        assert await self.manager.reinforce_memories(
            [mid], sem_scores={mid: 0.9}, now_override=now + 7 * 3600
        ) == 1

    async def test_legacy_memory_gets_embedding_backfilled_on_recall(self):
        self.hub.vectors["旧记忆补向量"] = [1.0, 0.0]
        memory_id = await self._add("旧记忆补向量", memory_type="profile", importance=8,
                                    memory_tier=MemoryTier.RECENT)
        await self.db.execute(
            "UPDATE memories SET embedding_json = '' WHERE id = ?", (memory_id,)
        )
        now = int(time.time())
        assert await self.manager.reinforce_memories(
            [memory_id], sem_scores={memory_id: 0.9}, now_override=now
        ) == 1
        for _ in range(5):
            await __import__("asyncio").sleep(0)
        row = await self.db.fetchone(
            "SELECT embedding_json FROM memories WHERE id = ?", (memory_id,)
        )
        assert row["embedding_json"]

    async def test_only_profile_relationship_promote_and_only_after_a_day(self):
        episode = await self._add("短期情节", memory_type="episode", importance=10,
                                  memory_tier=MemoryTier.RECENT)
        await self.db.execute("UPDATE memories SET access_count = 7 WHERE id = ?", (episode,))
        now = int(time.time())
        await self.manager.reinforce_memories(
            [episode], sem_scores={episode: 0.9}, now_override=now
        )
        row = await self.db.fetchone("SELECT memory_tier FROM memories WHERE id = ?", (episode,))
        assert row["memory_tier"] == MemoryTier.RECENT

        profile = await self._add("阿泽严重海鲜过敏", memory_type="profile",
                                  importance=8, memory_tier=MemoryTier.RECENT,
                                  created_at=now - 2 * 86400)
        await self.db.execute("UPDATE memories SET access_count = 7 WHERE id = ?", (profile,))
        await self.manager.reinforce_memories(
            [profile], sem_scores={profile: 0.9}, now_override=now
        )
        row = await self.db.fetchone("SELECT memory_tier FROM memories WHERE id = ?", (profile,))
        assert row["memory_tier"] == MemoryTier.CORE


# ---------------------------------------------------------------------------
# 6. Extraction prompt + limits
# ---------------------------------------------------------------------------
class TestMemoryExtraction(MemoryRagTestBase):
    async def test_batch_is_capped_and_t3_is_guarded(self):
        self.hub.json_result = [
            {"content": "候选一", "memory_type": "episode", "memory_tier": 3,
             "importance": 10, "user_tier": 3, "keywords": "a"},
            {"content": "候选二", "memory_type": "profile", "memory_tier": 3,
             "importance": 9, "user_tier": 3, "keywords": "b"},
            {"content": "候选三", "memory_type": "state", "memory_tier": 2,
             "importance": 6, "user_tier": 2, "keywords": "c"},
            {"content": "候选四", "memory_type": "episode", "memory_tier": 1,
             "importance": 2, "user_tier": 1, "keywords": "d"},
        ]
        count = await self.manager.extract_memories_from_transcript(
            soul_id="soul-a",
            soul_name="星野",
            transcript_text="[2026-11-16 21:10] [L1 阿一(ID:u-1)]: 随便聊聊",
        )
        assert count == 3
        rows = await self.db.fetchall(
            "SELECT content, memory_tier, memory_type FROM memories ORDER BY id"
        )
        assert [row["content"] for row in rows] == ["候选一", "候选二", "候选三"]
        assert rows[0]["memory_tier"] == MemoryTier.RECENT  # episode cannot directly be T3
        assert rows[1]["memory_tier"] == MemoryTier.CORE    # profile + 9 can
        assert rows[2]["memory_type"] == "state"

    async def test_prompt_contains_identity_block_and_existing_memories(self):
        await self._add("阿泽严重海鲜过敏", memory_type="profile", importance=9,
                        memory_tier=MemoryTier.CORE, user_tier=3)
        relation_manager = mods.user_relation.UserRelationManager(
            self.db, _NoopBridge(), make_plugin_config()
        )
        profile = await relation_manager.get_or_create_user(
            "u-1", "胡翔宇（考研版）", soul_id="soul-a"
        )
        profile.nickname = "hidge"
        profile.aliases = ["胡翔宇"]
        await relation_manager.save_user(profile)
        self.manager.attach_user_relation(relation_manager)
        self.hub.json_result = []

        await self.manager.extract_memories_from_transcript(
            soul_id="soul-a",
            soul_name="星野",
            transcript_text="[2026-11-16 21:10] [L3 胡翔宇(ID:u-1)]: 我在备考数电",
        )
        prompt = self.hub.calls[0]["prompt"]
        assert "参与者身份归一表" in prompt
        assert "标准称呼：hidge" in prompt
        assert "胡翔宇" in prompt
        assert "已有长期记忆" in prompt
        assert "阿泽严重海鲜过敏" in prompt
        assert "memory_type" in prompt


class _NoopBridge:
    def get_platform_admins(self, umo=None):
        return []


# ---------------------------------------------------------------------------
# 7. ContextBudget relevance gate / label budget
# ---------------------------------------------------------------------------
class TestRagInjectionGate(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.hub = VectorStubHub()
        self.manager = MemoryManager(self.db, self.hub, self.config)
        self.soul = make_soul("soul-a")
        self.user = make_user("user-1", "阿一")
        self.buffer = SessionBuffer(self.config)
        self.budget = ContextBudgetManager(
            memory_manager=self.manager,
            session_buffer=self.buffer,
            plugin_config=self.config,
            soul_manager=None,
        )

    async def _apply(self, message, query_vector):
        self.hub.vectors[message] = query_vector
        req = FakeRequest(prompt=message, system_prompt="BASE")
        result = await self.budget.apply_context_and_memory_budget(
            event=FakeEvent(message_str=message, umo="umo-1"),
            req=req,
            soul=self.soul,
            user=self.user,
            run=make_run(),
            current_slot=make_slot(state=StateMachine.BotState.IDLE),
        )
        return req, result

    async def test_irrelevant_memory_is_never_injected(self):
        await self.manager.add_memory(
            soul_id="soul-a", content="完全不相关的内容", keywords="天气",
            embedding=[0.0, 1.0], importance=10,
        )
        req, result = await self._apply("数电备考", [1.0, 0.0])
        assert result["recalled_ids"] == []
        assert "脑海中的相关记忆" not in req.system_prompt

    async def test_relevant_memory_is_injected_with_type_and_anchor(self):
        mid = await self.manager.add_memory(
            soul_id="soul-a", content="阿泽在备考数电", keywords="数电",
            embedding=[1.0, 0.0], importance=8, memory_type="state",
        )
        req, result = await self._apply("数电备考", [1.0, 0.0])
        assert result["recalled_ids"] == [mid]
        assert "- [近期记忆 · 近期状态 · 形成于 " in req.system_prompt
        assert "阿泽在备考数电" in req.system_prompt
        assert result["semantic_scores"][mid] >= 0.25

    async def test_budget_skips_long_memory_and_keeps_short_one(self):
        self.config["memory_and_context_config"]["max_memory_chars_budget"] = 50
        long_id = await self.manager.add_memory(
            soul_id="soul-a", content="数电" * 25, keywords="数电",
            importance=10,
        )
        short_id = await self.manager.add_memory(
            soul_id="soul-a", content="数电备考", keywords="数电",
            embedding=[0.99, 0.05], importance=5,
        )
        _req, result = await self._apply("数电", [1.0, 0.0])
        assert long_id not in result["recalled_ids"]
        assert short_id in result["recalled_ids"]

    async def test_rerank_provider_can_rescue_a_lexically_weak_memory(self):
        rerank = FakeRerankProvider()
        self.hub.rerank_provider = rerank
        mid = await self.manager.add_memory(
            soul_id="soul-a", content="阿泽在准备电子技术考试", keywords="考试",
            importance=8,
        )
        _req, result = await self._apply("数电", [1.0, 0.0])
        assert mid in result["recalled_ids"]
        assert result["semantic_scores"][mid] > 0.8


class TestRagEndToEnd(DatabaseAsyncTestCase):
    """ContextBudget -> ModelHub -> ProviderFactory -> SQLite 的完整 RAG 链路。"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.manager_config = FakeProviderManager(
            sources=[
                {"id": "emb-src", "type": "openai_embedding", "provider_type": "embedding", "enable": True},
                {"id": "rrk-src", "type": "openai_rerank", "provider_type": "rerank", "enable": True},
            ],
            providers=[
                {"id": "emb-1", "provider_source_id": "emb-src", "provider_type": "embedding",
                 "model": "bge-m3", "enable": True},
                {"id": "rrk-1", "provider_source_id": "rrk-src", "provider_type": "rerank",
                 "model": "bge-reranker", "enable": True},
            ],
        )
        self.embedding = FakeEmbeddingProvider(
            vectors={
                "阿泽在备考数电": [1.0, 0.0],
                "还记得数电的事吗": [1.0, 0.0],
            }
        )
        self.rerank = FakeRerankProvider()
        self.manager_config.inst_map = {"emb-1": self.embedding, "rrk-1": self.rerank}
        self.manager_config.embedding_provider_insts = [self.embedding]
        self.manager_config.rerank_provider_insts = [self.rerank]
        self.context = FakeContext(self.manager_config)
        self.context.providers_by_id = {"emb-1": self.embedding, "rrk-1": self.rerank}
        self.factory = ProviderFactory(self.context)
        self.hub = ModelHub(
            context=self.context,
            plugin_config=self.config,
            db=self.db,
            provider_factory=self.factory,
        )
        await self.hub.load()
        await self.hub.save_model_config(
            {
                "embedding": {"mode": "provider", "provider_id": "emb-1"},
                "rerank": {"mode": "provider", "provider_id": "rrk-1"},
            }
        )
        self.memory = MemoryManager(self.db, self.hub, self.config)

    async def test_write_caches_vector_and_recall_injects_it(self):
        memory_id = await self.memory.add_memory(
            soul_id="soul-a",
            content="阿泽在备考数电",
            keywords="数电,备考",
            importance=8,
            memory_type="state",
        )
        row = await self.db.fetchone(
            "SELECT embedding_json FROM memories WHERE id = ?", (memory_id,)
        )
        assert row["embedding_json"]
        assert await self.hub.get_embedding("阿泽在备考数电") == [1.0, 0.0]

        budget = ContextBudgetManager(
            memory_manager=self.memory,
            session_buffer=SessionBuffer(self.config),
            plugin_config=self.config,
            soul_manager=None,
        )
        req = FakeRequest(prompt="还记得数电的事吗", system_prompt="BASE")
        result = await budget.apply_context_and_memory_budget(
            event=FakeEvent(message_str="还记得数电的事吗", umo="umo-1"),
            req=req,
            soul=make_soul("soul-a"),
            user=make_user("user-1", "阿一"),
            run=make_run(),
            current_slot=make_slot(state=StateMachine.BotState.IDLE),
        )
        assert memory_id in result["recalled_ids"]
        assert result["semantic_scores"][memory_id] >= 0.9
        assert "阿泽在备考数电" in req.system_prompt
        assert self.embedding.calls  # query was embedded through ModelHub


if __name__ == "__main__":
    unittest.main()
