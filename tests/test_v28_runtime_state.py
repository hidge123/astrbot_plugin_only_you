"""Offline unit tests for v2.8 runtime-state persistence (T2 scope).

Frozen contract: ``.team/V28_HARDENING_BRIEF.md`` §3.

1. ``core/runtime_state.py`` — snapshot schema (``version: 1``), defensive
   ``normalize`` (dirty types / unknown keys), per-Soul caps (shared videos 200,
   mused busy keys 100, mused memory ids 30), ``known_l3_umos`` MRU cap 100 and
   the 7-day cooldown-timestamp trim; ``RuntimeStateStore.load/save`` against a
   real temporary SQLite ``plugin_settings`` table (never raises on failure).
2. ``main.py`` wiring — ``_persist_runtime_state`` / ``_load_runtime_state``
   round-trip with unchanged attribute names/types; ``initialize`` restores after
   ``_ensure_initialized``; ``terminate`` flushes **before** ``db.close()``.
3. Restart safety — a new plugin instance must not re-share the same SURF video,
   must honour the restored surf-share cooldown, and must not museum the same
   settled ``BUSY_TASK`` result again.
4. Unbind semantics — ``clear_platform_memory_state`` syncs the persisted
   snapshot (cleared ★L3 targets / cooldowns cannot resurrect after restart)
   while SQLite ``memories`` rows are kept.

Stdlib ``unittest`` only, fully offline (temp DBs, stubbed model hub / bridge).
"""

import asyncio
import inspect
import os
import sys
import time
import unittest
from types import MethodType, SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import (  # noqa: E402
    DatabaseAsyncTestCase,
    build_fake_plugin,
    load_plugin_modules,
    make_plugin_config,
    make_run,
    make_slot,
    make_soul,
    make_user,
)
from OnlyYou.core.runtime_state import (  # noqa: E402
    COOLDOWN_RETENTION_SECONDS,
    MAX_KNOWN_L3_UMOS,
    MAX_MUSED_BUSY_KEYS_PER_SOUL,
    MAX_MUSED_MEMORY_IDS_PER_SOUL,
    MAX_SHARED_VIDEO_IDS_PER_SOUL,
    RUNTIME_STATE_SETTING_KEY,
    RUNTIME_STATE_VERSION,
    RuntimeStateStore,
    default_snapshot,
    normalize_snapshot,
)
from OnlyYou.main import OnlyYouPlugin  # noqa: E402

mods = load_plugin_modules()
ActionType = mods.state_machine.ActionType
BotState = mods.state_machine.BotState

#: Fixed clock used by the pure ``normalize`` tests (avoids time.time() flakiness).
NOW = 1_700_000_000.0
RECENT = NOW - 3600.0


def bind_real_methods(target, names):
    """Bind *real* unbound ``OnlyYouPlugin`` methods onto a fake ``self``."""
    for name in names:
        raw = inspect.getattr_static(OnlyYouPlugin, name)
        if isinstance(raw, staticmethod):
            setattr(target, name, raw.__func__)
        else:
            setattr(target, name, MethodType(raw, target))
    return target


# Methods needed by the restart/dedupe integration-style tests below.
_RUNTIME_METHODS = (
    "_load_runtime_state",
    "_persist_runtime_state",
    "_idle_roam_cooldown_seconds",
    "_roguelite_setting",
    "_group_lurk_cooldown_seconds",
    "_private_catchup_cooldown_seconds",
    "_unread_private_reply_enabled",
    "_pick_group_unread_candidate",
    "_decide_group_unread_reply",
    "_run_idle_roam_action",
    "_resolve_idle_roam_target",
    "_extract_idle_roam_message",
    "_latest_l3_umo",
    "_maybe_synthesize_proactive",
    "_render_soul_summary",
    "_maybe_share_surfed_video",
    "_pick_memory_muse_material",
    "clear_platform_memory_state",
    "terminate",
)


class _RecordingHub:
    """ModelHub stub: records calls and returns configured JSON / text / TTS off."""

    def __init__(self, json_payload=None, text="这个视频太搞笑了，必须分享给你哈哈哈"):
        self.json_payload = json_payload
        self.text = text
        self.json_calls = []
        self.text_calls = []

    async def call_llm_json(
        self, prompt="", role="decision", umo=None, default=None, soul_id=None, **kwargs
    ):
        self.json_calls.append(
            {"prompt": prompt, "role": role, "umo": umo, "soul_id": soul_id}
        )
        return self.json_payload if self.json_payload is not None else default

    async def call_llm_text(
        self, prompt="", role="decision", umo=None, soul_id=None, **kwargs
    ):
        self.text_calls.append(
            {"prompt": prompt, "role": role, "umo": umo, "soul_id": soul_id}
        )
        return self.text

    def get_tts_config(self, soul_id=None):
        return {"mode": "off"}


class _RecordingBridge:
    """PlatformBridge stub recording proactive sends."""

    def __init__(self):
        self.sent = []
        self.ok = True

    async def send_proactive_message(
        self, umo="", text="", session_buffer=None, soul_name="Bot", audio_path=None
    ):
        if not self.ok:
            return False
        self.sent.append((umo, text))
        return True


def make_review(video_id="video-1", score=90, is_fallback=False):
    return SimpleNamespace(
        video=SimpleNamespace(
            video_id=video_id,
            title="猫猫翻车名场面",
            share_url=f"https://www.bilibili.com/video/{video_id}",
        ),
        score=score,
        commentary="笑死我了哈哈哈哈",
        visual_notes="",
        is_fallback=is_fallback,
    )


# ==========================================================================
# 1. Pure snapshot normalization
# ==========================================================================
class TestRuntimeStateNormalize(unittest.TestCase):
    def test_default_snapshot_shape(self):
        snapshot = default_snapshot()
        assert snapshot["version"] == RUNTIME_STATE_VERSION == 1
        for key in (
            "shared_video_ids",
            "last_surf_share_ts",
            "mused_busy_keys",
            "mused_memory_ids",
            "last_idle_roam_ts",
            "last_group_lurk_ts",
            "last_private_catchup_ts",
            "known_l3_umos",
        ):
            assert snapshot[key] == {}, key

    def test_non_dict_input_degrades_to_defaults(self):
        for dirty in (None, "", [], "runtime_state_v1", 42):
            assert normalize_snapshot(dirty) == default_snapshot()
        # non-dict collections nested in an otherwise valid dict are ignored
        out = normalize_snapshot(
            {
                "version": 99,
                "shared_video_ids": ["not", "a", "map"],
                "last_surf_share_ts": "nope",
                "known_l3_umos": 123,
            }
        )
        assert out == default_snapshot()

    def test_valid_snapshot_round_trips_types_and_values(self):
        source = {
            "version": 1,
            "shared_video_ids": {"soul-a": ["v1", "v2"]},
            "last_surf_share_ts": {"soul-a": RECENT},
            "mused_busy_keys": {"soul-a": ["block_2"]},
            "mused_memory_ids": {"soul-a": [1, 2, 3]},
            "last_idle_roam_ts": {"soul-a": RECENT - 10},
            "last_group_lurk_ts": {"aiocqhttp:GroupMessage:1": RECENT - 20},
            "last_private_catchup_ts": {"aiocqhttp:FriendMessage:1": RECENT - 30},
            "known_l3_umos": {"uid-1": "aiocqhttp:FriendMessage:1"},
        }
        out = RuntimeStateStore.normalize(source, now=NOW)
        assert out["shared_video_ids"] == {"soul-a": ["v1", "v2"]}
        assert out["mused_busy_keys"] == {"soul-a": ["block_2"]}
        assert out["mused_memory_ids"] == {"soul-a": [1, 2, 3]}
        assert out["last_surf_share_ts"] == {"soul-a": RECENT}
        assert out["last_idle_roam_ts"] == {"soul-a": RECENT - 10}
        assert out["last_group_lurk_ts"] == {"aiocqhttp:GroupMessage:1": RECENT - 20}
        assert out["last_private_catchup_ts"] == {
            "aiocqhttp:FriendMessage:1": RECENT - 30
        }
        assert out["known_l3_umos"] == {"uid-1": "aiocqhttp:FriendMessage:1"}
        assert out["version"] == RUNTIME_STATE_VERSION

    def test_dirty_entries_are_dropped_not_crashing(self):
        dirty = {
            "version": 7,
            "shared_video_ids": {
                "soul-a": ["v1", 2, "", "v1", None],
                "soul-b": "bare-string-is-not-a-list",
                3: ["non-str-key"],
                "  ": ["empty-key"],
            },
            "mused_busy_keys": {"soul-a": ["k1", True]},
            "mused_memory_ids": {
                "soul-a": [1, "2", 3.0, -4, 5.5, True],
                "soul-b": "nope",
            },
            "last_surf_share_ts": {
                "recent": RECENT,
                "stale": NOW - COOLDOWN_RETENTION_SECONDS - 1,
                "future": NOW + 3600,
                "negative": -1,
                "nan": float("nan"),
                "inf": float("inf"),
                "text": "abc",
                "bool": True,
                4: RECENT,
            },
            "last_idle_roam_ts": {"soul-a": True},
            "last_group_lurk_ts": None,
            "last_private_catchup_ts": [],
            "known_l3_umos": {"uid-1": "umo-1", "uid-2": 123, "": "x", "uid-3": "  "},
            "some_unknown_key": {"ignored": True},
        }
        out = normalize_snapshot(dirty, now=NOW)
        assert out["version"] == RUNTIME_STATE_VERSION
        assert out["shared_video_ids"] == {"soul-a": ["v1"]}
        assert out["mused_busy_keys"] == {"soul-a": ["k1"]}
        assert out["mused_memory_ids"] == {"soul-a": [1, 3]}
        # future timestamps are clamped to "now" (clock skew never freezes a gate)
        assert out["last_surf_share_ts"] == {"recent": RECENT, "future": NOW}
        assert out["last_idle_roam_ts"] == {}
        assert out["last_group_lurk_ts"] == {}
        assert out["last_private_catchup_ts"] == {}
        assert out["known_l3_umos"] == {"uid-1": "umo-1"}
        assert "some_unknown_key" not in out
        # trailing empty maps stay empty dicts, never None
        for key, value in out.items():
            if key in ("version", "known_l3_umos"):
                continue
            assert isinstance(value, dict), key

    def test_caps_and_fifo_trim(self):
        video_ids = [f"v{index}" for index in range(MAX_SHARED_VIDEO_IDS_PER_SOUL + 50)]
        busy_keys = [f"k{index}" for index in range(MAX_MUSED_BUSY_KEYS_PER_SOUL + 20)]
        memory_ids = list(range(1, MAX_MUSED_MEMORY_IDS_PER_SOUL + 11))
        l3 = {f"uid-{index}": f"umo-{index}" for index in range(MAX_KNOWN_L3_UMOS + 50)}
        out = normalize_snapshot(
            {
                "shared_video_ids": {"soul-a": video_ids},
                "mused_busy_keys": {"soul-a": busy_keys},
                "mused_memory_ids": {"soul-a": memory_ids},
                "known_l3_umos": l3,
            },
            now=NOW,
        )
        assert len(out["shared_video_ids"]["soul-a"]) == MAX_SHARED_VIDEO_IDS_PER_SOUL
        # FIFO: the newest entries survive
        assert out["shared_video_ids"]["soul-a"][0] == "v50"
        assert out["shared_video_ids"]["soul-a"][-1] == f"v{len(video_ids) - 1}"
        assert len(out["mused_busy_keys"]["soul-a"]) == MAX_MUSED_BUSY_KEYS_PER_SOUL
        assert out["mused_busy_keys"]["soul-a"][-1] == busy_keys[-1]
        assert len(out["mused_memory_ids"]["soul-a"]) == MAX_MUSED_MEMORY_IDS_PER_SOUL
        assert out["mused_memory_ids"]["soul-a"][-1] == memory_ids[-1]
        assert len(out["known_l3_umos"]) == MAX_KNOWN_L3_UMOS
        # MRU: the newest insertions (tail of the dict) survive
        assert "uid-0" not in out["known_l3_umos"]
        assert f"uid-{MAX_KNOWN_L3_UMOS + 49}" in out["known_l3_umos"]

    def test_cooldown_retention_boundary(self):
        out = normalize_snapshot(
            {
                "last_surf_share_ts": {
                    "exactly-7d": NOW - COOLDOWN_RETENTION_SECONDS,
                    "just-over-7d": NOW - COOLDOWN_RETENTION_SECONDS - 1,
                    "fresh": NOW - 5,
                }
            },
            now=NOW,
        )
        assert set(out["last_surf_share_ts"]) == {"exactly-7d", "fresh"}


# ==========================================================================
# 2. Store load/save against a real (temporary) SQLite KV table
# ==========================================================================
class _BrokenDB:
    def __init__(self, fail_get=False, fail_set=False):
        self.fail_get = fail_get
        self.fail_set = fail_set

    async def get_setting(self, key, default=None):
        raise RuntimeError("db unavailable")

    async def set_setting(self, key, value):
        raise RuntimeError("disk full")


class TestRuntimeStateStoreIO(DatabaseAsyncTestCase):
    async def test_save_then_load_round_trip(self):
        store = RuntimeStateStore(self.db)
        saved = await store.save(
            {
                "version": 1,
                "shared_video_ids": {"soul-a": ["v1", "v2"]},
                "mused_memory_ids": {"soul-a": [7, 8]},
                "known_l3_umos": {"uid-1": "aiocqhttp:FriendMessage:1"},
            }
        )
        assert saved is True
        raw = await self.db.get_setting(RUNTIME_STATE_SETTING_KEY)
        assert raw["version"] == RUNTIME_STATE_VERSION
        # JSON-safe payload: lists (not sets) and string keys only
        assert raw["shared_video_ids"] == {"soul-a": ["v1", "v2"]}
        assert raw["mused_memory_ids"] == {"soul-a": [7, 8]}
        loaded = await RuntimeStateStore(self.db).load()
        assert loaded["shared_video_ids"] == {"soul-a": ["v1", "v2"]}
        assert loaded["known_l3_umos"] == {"uid-1": "aiocqhttp:FriendMessage:1"}
        # untouched sections are still present as empty maps
        assert loaded["last_group_lurk_ts"] == {}

    async def test_save_normalizes_before_persisting(self):
        store = RuntimeStateStore(self.db)
        await store.save(
            {
                "version": 1,
                "shared_video_ids": {"soul-a": [f"v{index}" for index in range(250)]},
                "mused_memory_ids": {"soul-a": list(range(1, 41))},
                "last_surf_share_ts": {"soul-a": time.time() - 40 * 86400},
            }
        )
        raw = await self.db.get_setting(RUNTIME_STATE_SETTING_KEY)
        assert len(raw["shared_video_ids"]["soul-a"]) == MAX_SHARED_VIDEO_IDS_PER_SOUL
        assert len(raw["mused_memory_ids"]["soul-a"]) == MAX_MUSED_MEMORY_IDS_PER_SOUL
        assert raw["last_surf_share_ts"] == {}

    async def test_load_corrupted_payload_returns_defaults(self):
        await self.db.set_setting(RUNTIME_STATE_SETTING_KEY, "not-a-dict")
        loaded = await RuntimeStateStore(self.db).load()
        assert loaded == default_snapshot()
        await self.db.set_setting(RUNTIME_STATE_SETTING_KEY, {"shared_video_ids": []})
        loaded = await RuntimeStateStore(self.db).load()
        assert loaded["shared_video_ids"] == {}

    async def test_load_failure_returns_defaults_and_does_not_raise(self):
        loaded = await RuntimeStateStore(_BrokenDB(fail_get=True)).load()
        assert loaded == default_snapshot()

    async def test_save_failure_returns_false_and_does_not_raise(self):
        assert await RuntimeStateStore(_BrokenDB(fail_set=True)).save({}) is False

    async def test_store_without_db_is_a_safe_noop(self):
        store = RuntimeStateStore(None)
        assert await store.load() == default_snapshot()
        assert await store.save({"shared_video_ids": {"s": ["v"]}}) is False


# ==========================================================================
# 3. main.py wiring: persist / restore round-trip
# ==========================================================================
class TestPluginRuntimeStateRoundTrip(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()

    async def _new_plugin(self):
        plugin, _api = await build_fake_plugin(self.db, self.tmpdir, self.config)
        plugin.runtime_state_store = RuntimeStateStore(self.db)
        bind_real_methods(plugin, ["_load_runtime_state", "_persist_runtime_state"])
        return plugin

    async def test_persist_then_restore_keeps_attributes_and_types(self):
        now = time.time()
        plugin = await self._new_plugin()
        plugin._shared_video_ids = {"soul-a": {"v1", "v2"}}
        plugin._last_surf_share_ts = {"soul-a": now - 120.0}
        plugin._mused_busy_keys = {"soul-a": {"block_2"}}
        plugin._mused_memory_ids = {"soul-a": [1, 2, 3]}
        plugin._last_idle_roam_ts = {"soul-a": now - 60.0}
        plugin._last_group_lurk_ts = {"aiocqhttp:GroupMessage:1": now - 30.0}
        plugin._last_private_catchup_ts = {"aiocqhttp:FriendMessage:1": now - 20.0}
        plugin._known_l3_umos = {"uid-1": "aiocqhttp:FriendMessage:1"}
        await plugin._persist_runtime_state()

        restarted = await self._new_plugin()
        assert restarted._shared_video_ids == {}
        await restarted._load_runtime_state()

        assert restarted._shared_video_ids == {"soul-a": {"v1", "v2"}}
        assert isinstance(restarted._shared_video_ids["soul-a"], set)
        assert restarted._mused_busy_keys == {"soul-a": {"block_2"}}
        assert isinstance(restarted._mused_busy_keys["soul-a"], set)
        assert restarted._mused_memory_ids == {"soul-a": [1, 2, 3]}
        assert isinstance(restarted._mused_memory_ids["soul-a"], list)
        assert isinstance(restarted._mused_memory_ids["soul-a"][0], int)
        assert restarted._last_surf_share_ts["soul-a"] == now - 120.0
        assert isinstance(restarted._last_surf_share_ts["soul-a"], float)
        assert restarted._last_idle_roam_ts["soul-a"] == now - 60.0
        assert restarted._last_group_lurk_ts["aiocqhttp:GroupMessage:1"] == now - 30.0
        assert restarted._last_private_catchup_ts["aiocqhttp:FriendMessage:1"] == now - 20.0
        assert restarted._known_l3_umos == {"uid-1": "aiocqhttp:FriendMessage:1"}
        # the unread backlog is *not* part of the snapshot (restart clears it)
        assert restarted.session_buffer.get_unread_total() == 0

    async def test_load_ignores_dirty_snapshot_without_crashing(self):
        await self.db.set_setting(
            RUNTIME_STATE_SETTING_KEY,
            {
                "shared_video_ids": {"soul-a": "bad-shape"},
                "mused_memory_ids": {"soul-a": ["x", None]},
                "known_l3_umos": ["bad-shape"],
                "last_group_lurk_ts": {"umo": "not-a-number"},
            },
        )
        plugin = await self._new_plugin()
        await plugin._load_runtime_state()  # must not raise
        assert plugin._shared_video_ids == {}
        assert plugin._mused_memory_ids == {}
        assert plugin._known_l3_umos == {}
        assert plugin._last_group_lurk_ts == {}

    async def test_persist_swallows_db_failure(self):
        plugin = await self._new_plugin()
        plugin.runtime_state_store = RuntimeStateStore(_BrokenDB(fail_set=True))
        plugin._shared_video_ids = {"soul-a": {"v1"}}
        assert await plugin._persist_runtime_state() is None  # never raises

    async def test_persist_without_store_is_a_noop(self):
        plugin = await self._new_plugin()
        plugin.runtime_state_store = None
        assert await plugin._persist_runtime_state() is None


# ==========================================================================
# 4. Restart safety: no duplicate share / restored cooldown / no duplicate muse
# ==========================================================================
class TestRestartProactiveDedupe(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        # proactive-action cooldown must not interfere with the muse assertions
        self.config["roguelite_schedule_config"]["idle_roam_cooldown_minutes"] = 0

    async def _new_plugin(self, hub=None, bridge=None, methods=None):
        plugin, _api = await build_fake_plugin(self.db, self.tmpdir, self.config)
        plugin.model_hub = hub or _RecordingHub()
        plugin.platform_bridge = bridge or _RecordingBridge()
        plugin.runtime_state_store = RuntimeStateStore(self.db)
        plugin._known_l3_umos = {"uid-1": "aiocqhttp:FriendMessage:1"}
        bind_real_methods(plugin, methods or _RUNTIME_METHODS)
        return plugin

    async def test_restart_does_not_reshare_the_same_video(self):
        soul = make_soul("soul-share")
        slot = make_slot(action_type=ActionType.SURF, title="刷视频")
        bridge1 = _RecordingBridge()
        plugin1 = await self._new_plugin(bridge=bridge1)
        assert (
            await plugin1._maybe_share_surfed_video(
                soul, None, slot, make_review("v-1")
            )
            is True
        )
        assert plugin1._shared_video_ids[soul.soul_id] == {"v-1"}
        raw = await self.db.get_setting(RUNTIME_STATE_SETTING_KEY)
        assert raw["shared_video_ids"][soul.soul_id] == ["v-1"]

        # simulate "later": the surf-share cooldown has elapsed before the restart
        plugin1._last_surf_share_ts[soul.soul_id] = time.time() - 91 * 60
        await plugin1._persist_runtime_state()

        bridge2 = _RecordingBridge()
        plugin2 = await self._new_plugin(bridge=bridge2)
        plugin2._known_l3_umos = {}  # must come from the persisted snapshot
        await plugin2._load_runtime_state()
        assert plugin2._known_l3_umos == {"uid-1": "aiocqhttp:FriendMessage:1"}

        # same video id -> deduped before the L3/LLM path, even past the cooldown
        assert (
            await plugin2._maybe_share_surfed_video(
                soul, None, slot, make_review("v-1")
            )
            is False
        )
        assert bridge2.sent == []
        # a genuinely new video still goes through (proves the gates are open)
        assert (
            await plugin2._maybe_share_surfed_video(
                soul, None, slot, make_review("v-2")
            )
            is True
        )
        assert len(bridge2.sent) == 1

    async def test_restart_restores_surf_share_cooldown(self):
        soul = make_soul("soul-cd")
        slot = make_slot(action_type=ActionType.SURF, title="刷视频")
        plugin1 = await self._new_plugin()
        assert (
            await plugin1._maybe_share_surfed_video(soul, None, slot, make_review("v-c1"))
            is True
        )
        persisted = await self.db.get_setting(RUNTIME_STATE_SETTING_KEY)
        assert persisted["last_surf_share_ts"][soul.soul_id] > 0

        hub2 = _RecordingHub()
        bridge2 = _RecordingBridge()
        plugin2 = await self._new_plugin(hub=hub2, bridge=bridge2)
        await plugin2._load_runtime_state()
        assert (
            await plugin2._maybe_share_surfed_video(
                soul, None, slot, make_review("v-c2")
            )
            is False
        )
        assert bridge2.sent == []
        assert hub2.text_calls == []

    async def test_restart_does_not_remuse_the_same_busy_result(self):
        soul = make_soul("soul-muse")
        current_slot = make_slot(
            state=BotState.IDLE,
            action_type=ActionType.IDLE_ROAM,
            title="趴在窗边发呆",
        )
        run = make_run(soul_id=soul.soul_id)
        busy_slot = run.slots[2]
        assert busy_slot.action == ActionType.BUSY_TASK
        busy_slot.busy_settled = True
        busy_slot.completion_result = "今天把回春丹炼成了，差点炸炉"

        hub1 = _RecordingHub(
            json_payload={
                "target": "PRIVATE_L3",
                "message": "突然想起今天炼丹差点炸炉，笑死",
            }
        )
        bridge1 = _RecordingBridge()
        plugin1 = await self._new_plugin(hub=hub1, bridge=bridge1)
        sent = await plugin1._run_idle_roam_action(soul, run, current_slot)
        assert sent is True
        assert plugin1._mused_busy_keys[soul.soul_id] == {busy_slot.slot_key}
        assert "回春丹" in hub1.json_calls[0]["prompt"]
        assert len(bridge1.sent) == 1
        raw = await self.db.get_setting(RUNTIME_STATE_SETTING_KEY)
        assert raw["mused_busy_keys"][soul.soul_id] == [busy_slot.slot_key]

        # restart: the busy result was already mused -> no LLM call, no send
        hub2 = _RecordingHub(json_payload={"target": "PRIVATE_L3", "message": "重复"})
        bridge2 = _RecordingBridge()
        plugin2 = await self._new_plugin(hub=hub2, bridge=bridge2)
        await plugin2._load_runtime_state()
        assert plugin2._mused_busy_keys[soul.soul_id] == {busy_slot.slot_key}
        assert await plugin2._run_idle_roam_action(soul, run, current_slot) is False
        assert hub2.json_calls == []
        assert bridge2.sent == []


# ==========================================================================
# 5. Unbind / clear semantics: persisted snapshot follows the memory wipe
# ==========================================================================
class TestClearPlatformMemoryPersists(DatabaseAsyncTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config = make_plugin_config()
        self.plugin, _api = await build_fake_plugin(
            self.db, self.tmpdir, self.config
        )
        self.plugin.runtime_state_store = RuntimeStateStore(self.db)
        bind_real_methods(
            self.plugin,
            [
                "clear_platform_memory_state",
                "_load_runtime_state",
                "_persist_runtime_state",
            ],
        )
        now = time.time()
        for umo in ("aiocqhttp:GroupMessage:1", "telegram:GroupMessage:2"):
            message = self.plugin.session_buffer.record_user_message(
                umo, make_user("u-1", "阿一"), "在吗"
            )
            self.plugin.session_buffer.mark_unread(
                umo, message, is_private="FriendMessage" in umo
            )
        self.plugin._known_group_umos = {
            "aiocqhttp:GroupMessage:1",
            "telegram:GroupMessage:2",
        }
        self.plugin._known_l3_umos = {
            "uid-1": "aiocqhttp:FriendMessage:1",
            "uid-2": "telegram:FriendMessage:2",
        }
        self.plugin._last_group_lurk_ts = {
            "aiocqhttp:GroupMessage:1": now - 10,
            "telegram:GroupMessage:2": now - 20,
        }
        self.plugin._last_private_catchup_ts = {
            "aiocqhttp:FriendMessage:1": now - 30,
            "telegram:FriendMessage:2": now - 40,
        }
        await self.plugin._persist_runtime_state()
        self.memory_id = await self.plugin.memory_manager.add_memory(
            soul_id="soul-a",
            content="解绑平台后仍然保留的持久记忆",
            keywords="记忆",
            memory_tier=2,
        )

    async def test_clear_syncs_snapshot_and_never_resurrects_after_restart(self):
        stats = await self.plugin.clear_platform_memory_state("aiocqhttp")
        assert stats["l3"] == 1
        assert stats["groups"] == 1

        raw = await self.db.get_setting(RUNTIME_STATE_SETTING_KEY)
        assert "uid-1" not in raw["known_l3_umos"]
        assert "aiocqhttp:FriendMessage:1" not in raw["last_private_catchup_ts"]
        assert "aiocqhttp:GroupMessage:1" not in raw["last_group_lurk_ts"]
        # the other platform is untouched in the persisted snapshot
        assert raw["known_l3_umos"]["uid-2"] == "telegram:FriendMessage:2"
        assert "telegram:FriendMessage:2" in raw["last_private_catchup_ts"]

        restarted, _api = await build_fake_plugin(
            self.db, self.tmpdir, self.config
        )
        restarted.runtime_state_store = RuntimeStateStore(self.db)
        bind_real_methods(restarted, ["_load_runtime_state"])
        await restarted._load_runtime_state()
        assert restarted._known_l3_umos == {"uid-2": "telegram:FriendMessage:2"}
        assert restarted._last_private_catchup_ts == {
            "telegram:FriendMessage:2": self.plugin._last_private_catchup_ts[
                "telegram:FriendMessage:2"
            ]
        }
        # persistent memories are NOT deleted by the in-memory wipe
        memories = await restarted.memory_manager.list_memories(soul_id="soul-a")
        assert any(item.id == self.memory_id for item in memories)

    async def test_clear_empty_platform_is_a_noop_and_still_safe(self):
        stats = await self.plugin.clear_platform_memory_state("")
        assert stats["windows"] == 0
        assert await self.plugin.clear_platform_memory_state("unknown") == {
            "windows": 0,
            "evicted": 0,
            "unread": 0,
            "messages": 0,
            "groups": 0,
            "l3": 0,
            "caches": 0,
        }


# ==========================================================================
# 6. Lifecycle: initialize restore order + terminate flush-before-close
# ==========================================================================
class _RecordingDB:
    """Tiny DB stub recording the save/close order (and optionally failing writes)."""

    def __init__(self, fail_set=False):
        self.data = {}
        self.fail_set = fail_set
        self.events = []

    async def get_setting(self, key, default=None):
        return self.data.get(key, default)

    async def set_setting(self, key, value):
        if self.fail_set:
            raise RuntimeError("disk full")
        self.data[key] = value
        self.events.append("save")

    async def close(self):
        self.events.append("close")


class _DisposeHub:
    async def dispose(self):
        return None


class TestLifecycleWiring(DatabaseAsyncTestCase):
    async def test_initialize_loads_runtime_state_after_ensure_initialized(self):
        events = []

        async def ensure_initialized():
            events.append("ensure")

        async def load_runtime_state():
            events.append("load")

        async def migrate():
            events.append("migrate")

        async def scheduled_gc():
            events.append("gc")
            return {}

        async def heartbeat():
            events.append("heartbeat")

        async def daily_gc():
            events.append("daily")

        fake = SimpleNamespace(
            _bg_tasks=[],
            _ensure_initialized=ensure_initialized,
            _load_runtime_state=load_runtime_state,
            soul_manager=SimpleNamespace(migrate_legacy_umo_bindings=migrate),
            _maybe_run_scheduled_gc=scheduled_gc,
            web_api=SimpleNamespace(register_routes=lambda: events.append("routes")),
            _heartbeat_loop=heartbeat,
            _daily_gc_loop=daily_gc,
        )
        bind_real_methods(fake, ["initialize"])
        await fake.initialize()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert events[0] == "ensure"
        assert events.index("load") == events.index("ensure") + 1
        assert events.index("load") < events.index("migrate")
        assert "routes" in events
        assert all(task.done() for task in fake._bg_tasks), "background loops did not finish"

    async def test_terminate_flushes_snapshot_before_closing_db(self):
        db = _RecordingDB()
        fake = SimpleNamespace(
            _bg_tasks=[],
            model_hub=_DisposeHub(),
            db=db,
            runtime_state_store=RuntimeStateStore(db),
        )
        fake._shared_video_ids = {"soul-a": {"v1"}}
        fake._known_l3_umos = {"uid-1": "aiocqhttp:FriendMessage:1"}
        bind_real_methods(fake, ["terminate", "_persist_runtime_state"])
        await fake.terminate()
        assert db.events == ["save", "close"]
        snapshot = db.data[RUNTIME_STATE_SETTING_KEY]
        assert snapshot["shared_video_ids"] == {"soul-a": ["v1"]}
        assert snapshot["known_l3_umos"] == {"uid-1": "aiocqhttp:FriendMessage:1"}

    async def test_terminate_swallows_persist_failure(self):
        db = _RecordingDB(fail_set=True)
        fake = SimpleNamespace(
            _bg_tasks=[],
            model_hub=_DisposeHub(),
            db=db,
            runtime_state_store=RuntimeStateStore(db),
        )
        fake._shared_video_ids = {"soul-a": {"v1"}}
        bind_real_methods(fake, ["terminate", "_persist_runtime_state"])
        await fake.terminate()  # must not raise even though the KV write failed
        assert db.events == ["close"]

    async def test_terminate_without_persister_method_is_defensive(self):
        db = _RecordingDB()
        fake = SimpleNamespace(_bg_tasks=[], model_hub=_DisposeHub(), db=db)
        bind_real_methods(fake, ["terminate"])  # no _persist_runtime_state attribute
        await fake.terminate()
        assert db.events == ["close"]


if __name__ == "__main__":
    unittest.main()
