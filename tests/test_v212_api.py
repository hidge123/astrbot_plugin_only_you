"""v2.12 Web API / 控制台数据契约回归（Embedding/Rerank、记忆分类与归档）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import FakeContext, load_plugin_modules  # noqa: E402
from test_web_api import WebApiTestBase  # noqa: E402

mods = load_plugin_modules()
MemoryTier = mods.memory_manager.MemoryTier


class FakeEmbeddingProvider:
    def __init__(self):
        self.provider_config = {
            "id": "emb-1",
            "provider_type": "embedding",
            "model": "bge-m3",
        }

    async def get_embeddings(self, texts):
        return [[1.0, 0.0] for _ in texts]


class FakeRerankProvider:
    def __init__(self):
        self.provider_config = {
            "id": "rrk-1",
            "provider_type": "rerank",
            "model": "bge-reranker",
        }

    async def rerank(self, query, documents, top_n=5):
        from types import SimpleNamespace

        return [
            SimpleNamespace(index=index, relevance_score=0.8 - index * 0.1)
            for index in range(min(len(documents), int(top_n or 5)))
        ]


class TestV212ModelRoutes(WebApiTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        manager = self.plugin.context.provider_manager
        manager.providers_config.extend(
            [
                {
                    "id": "emb-1",
                    "provider_source_id": "emb-src",
                    "provider_type": "embedding",
                    "model": "bge-m3",
                    "enable": True,
                },
                {
                    "id": "rrk-1",
                    "provider_source_id": "rrk-src",
                    "provider_type": "rerank",
                    "model": "bge-reranker",
                    "enable": True,
                },
            ]
        )
        self.embedding = manager.inst_map["emb-1"] = FakeEmbeddingProvider()
        self.rerank = manager.inst_map["rrk-1"] = FakeRerankProvider()
        self.plugin.context.providers_by_id = {"emb-1": self.embedding, "rrk-1": self.rerank}

    async def test_sources_expose_rag_providers(self):
        response = await self.call("GET", self.api.api_models_sources)
        data = self.assert_ok(response)
        assert {item["id"] for item in data["embedding_providers"]} >= {"emb-1"}
        assert {item["id"] for item in data["rerank_providers"]} >= {"rrk-1"}

    async def test_models_config_roundtrip_and_status(self):
        soul = await self.first_soul()
        payload = {
            "soul_id": soul.soul_id,
            "model_config": {
                "main": {"source_id": "deepseek", "model": "deepseek-chat"},
                "embedding": {"mode": "provider", "provider_id": "emb-1"},
                "rerank": {"mode": "provider", "provider_id": "rrk-1"},
            },
        }
        response = await self.call("POST", self.api.api_models_config, body=payload)
        data = self.assert_ok(response)
        assert data["model_config"]["embedding"]["mode"] == "provider"
        assert data["status"]["embedding"]["configured"] is True
        assert data["status"]["rerank"]["ok"] is True

        response = await self.call(
            "GET",
            self.api.api_models_config,
            query={"soul_id": soul.soul_id},
        )
        data = self.assert_ok(response)
        assert data["model_config"]["embedding"]["provider_id"] == "emb-1"
        assert data["status"]["embedding"]["role"] == "embedding"

    async def test_models_test_supports_embedding_and_rerank(self):
        soul = await self.first_soul()
        await self.call(
            "POST",
            self.api.api_models_config,
            body={
                "soul_id": soul.soul_id,
                "model_config": {
                    "embedding": {"mode": "provider", "provider_id": "emb-1"},
                    "rerank": {"mode": "provider", "provider_id": "rrk-1"},
                },
            },
        )
        embedding = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_models_test,
                body={"role": "embedding", "soul_id": soul.soul_id},
            )
        )
        assert embedding["status"] == "ok"
        assert embedding["dimension"] == 2
        rerank = self.assert_ok(
            await self.call(
                "POST",
                self.api.api_models_test,
                body={"role": "rerank", "soul_id": soul.soul_id},
            )
        )
        assert rerank["status"] == "ok"
        assert rerank["results"]


class TestV212MemoryRoutes(WebApiTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.soul = await self.first_soul()

    async def _add(self, content, memory_type="episode", **kwargs):
        manager = self.plugin.memory_manager
        return await manager.add_memory(
            soul_id=self.soul.soul_id,
            content=content,
            keywords=kwargs.pop("keywords", ""),
            importance=kwargs.pop("importance", 6),
            memory_tier=kwargs.pop("memory_tier", MemoryTier.RECENT),
            memory_type=memory_type,
            **kwargs,
        )

    async def test_memories_list_stats_and_filters(self):
        profile_id = await self._add("阿泽严重海鲜过敏", memory_type="profile", importance=9)
        state_id = await self._add("阿泽在备考数电", memory_type="state")
        archived_id = await self._add("旧的备考推测", memory_type="state", embedding=[1.0, 0.0])
        new_id = await self._add(
            "确认在备考数电", memory_type="state", embedding=[0.8, 0.6],
            supersedes_id=archived_id,
        )
        _ = (profile_id, state_id, new_id)

        response = await self.call(
            "GET", self.api.api_memories_list, query={"soul_id": self.soul.soul_id}
        )
        data = self.assert_ok(response)
        stats = data["stats"]
        assert stats["memory_type_counts"]["profile"] == 1
        assert stats["memory_type_counts"]["state"] == 3
        assert stats["superseded_count"] == 1
        assert stats["embedding_count"] == 2
        archived = [item for item in data["memories"] if item["id"] == archived_id][0]
        assert archived["superseded"] is True
        assert archived["superseded_by"] == new_id

        response = await self.call(
            "GET",
            self.api.api_memories_list,
            query={"soul_id": self.soul.soul_id, "include_superseded": "false"},
        )
        data = self.assert_ok(response)
        assert archived_id not in {item["id"] for item in data["memories"]}

        response = await self.call(
            "GET",
            self.api.api_memories_list,
            query={"soul_id": self.soul.soul_id, "memory_type": "profile"},
        )
        data = self.assert_ok(response)
        assert [item["memory_type"] for item in data["memories"]] == ["profile"]

    async def test_downgrade_via_api_restarts_tier1_retention(self):
        import time

        now = int(time.time())
        memory_id = await self._add(
            "十二天前形成的近期记忆",
            memory_tier=MemoryTier.RECENT,
        )
        await self.db.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            (now - 12 * 86400, memory_id),
        )
        response = await self.call(
            "POST",
            self.api.api_memories_mutate,
            body={
                "action": "edit",
                "soul_id": self.soul.soul_id,
                "id": memory_id,
                "memory_tier": 1,
            },
        )
        data = self.assert_ok(response)
        memory = data["memory"]
        assert memory["memory_tier"] == 1
        assert memory["tier1_since"] >= now - 5
        assert memory["tier1_expires_at"] >= now + 48 * 3600 - 5
        assert memory["tier1_expired"] is False
        # And the refreshed list keeps it (it must not look instantly expired).
        listed = self.assert_ok(
            await self.call(
                "GET",
                self.api.api_memories_list,
                query={"soul_id": self.soul.soul_id},
            )
        )
        row = next(item for item in listed["memories"] if item["id"] == memory_id)
        assert row["tier1_expired"] is False

    async def test_memories_list_reclaims_overdue_tier1(self):
        import time

        now = int(time.time())
        expired_id = await self._add(
            "已经超过 48h 的瞬时记忆",
            memory_tier=MemoryTier.EPHEMERAL,
            created_at=now - 49 * 3600,
        )
        fresh_id = await self._add(
            "仍然有效的近期记忆", memory_tier=MemoryTier.RECENT
        )
        data = self.assert_ok(
            await self.call(
                "GET",
                self.api.api_memories_list,
                query={"soul_id": self.soul.soul_id},
            )
        )
        ids = {item["id"] for item in data["memories"]}
        assert expired_id not in ids
        assert fresh_id in ids
        assert await self.db.fetchone(
            "SELECT id FROM memories WHERE id = ?", (expired_id,)
        ) is None

    async def test_mutate_add_and_edit_memory_type(self):
        response = await self.call(
            "POST",
            self.api.api_memories_mutate,
            body={
                "action": "add",
                "soul_id": self.soul.soul_id,
                "content": "阿泽确认在备考数电",
                "keywords": "数电,备考",
                "importance": 7,
                "memory_tier": 2,
                "memory_type": "state",
                "event_at": 1700000000,
            },
        )
        data = self.assert_ok(response)
        new_id = data["new_id"]
        assert new_id > 0
        response = await self.call(
            "POST",
            self.api.api_memories_mutate,
            body={
                "action": "edit",
                "soul_id": self.soul.soul_id,
                "id": new_id,
                "memory_type": "profile",
            },
        )
        data = self.assert_ok(response)
        assert data["memory"]["memory_type"] == "profile"
        assert data["memory"]["memory_type_label"] == "人物画像"
        assert data["memory"]["event_at"] == 1700000000


if __name__ == "__main__":
    unittest.main()
