"""Shared helpers for the OnlyYou offline unittest suite.

Everything here is stdlib only (``unittest`` / ``tempfile`` / ``unittest.mock``) because
the local development machine has **no pytest** and no network access.

Import strategy
---------------
``OnlyYou`` is a *namespace package*: the repository directory ``OnlyYou/`` has no
``__init__.py`` while its parent directory (the checkout parent, e.g.
``.../Project``) is on ``sys.path``.  Importing ``OnlyYou.core.character_card`` therefore works exactly like
inside AstrBot's plugin loader (which also puts the plugin's parent on the path).

The test suite must never instantiate ``OnlyYouPlugin`` (its ``StarTools`` stub would
create an SQLite database inside the repository ``data/`` directory).  ``FakePlugin``
below assembles the *real* business components against a temporary SQLite database
instead.
"""

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

# ---------------------------------------------------------------------------
# sys.path bootstrap (must happen before any `OnlyYou.*` import)
# ---------------------------------------------------------------------------
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(_TESTS_DIR)  # .../Project/OnlyYou
PROJECT_PARENT = os.path.dirname(PROJECT_ROOT)  # .../Project
if PROJECT_PARENT not in sys.path:
    sys.path.insert(0, PROJECT_PARENT)

_MODULES = None


def load_plugin_modules():
    """Import every module under test once and return them in a namespace."""
    global _MODULES
    if _MODULES is not None:
        return _MODULES

    import OnlyYou.core.character_card as character_card
    import OnlyYou.core.context_budget as context_budget
    import OnlyYou.core.memory_manager as memory_manager
    import OnlyYou.core.model_hub as model_hub
    import OnlyYou.core.platform_bridge as platform_bridge
    import OnlyYou.core.provider_factory as provider_factory
    import OnlyYou.core.roguelite_deck as roguelite_deck
    import OnlyYou.core.schedule_mutator as schedule_mutator
    import OnlyYou.core.soul_manager as soul_manager
    import OnlyYou.core.state_machine as state_machine
    import OnlyYou.core.user_relation as user_relation
    import OnlyYou.core.web_api as web_api
    import OnlyYou.prompts.templates as templates
    import OnlyYou.storage.db as db
    import OnlyYou.surfing.pipeline as surfing_pipeline

    _MODULES = SimpleNamespace(
        character_card=character_card,
        context_budget=context_budget,
        memory_manager=memory_manager,
        model_hub=model_hub,
        platform_bridge=platform_bridge,
        provider_factory=provider_factory,
        roguelite_deck=roguelite_deck,
        schedule_mutator=schedule_mutator,
        soul_manager=soul_manager,
        state_machine=state_machine,
        user_relation=user_relation,
        web_api=web_api,
        templates=templates,
        db=db,
        surfing_pipeline=surfing_pipeline,
    )
    return _MODULES


# ---------------------------------------------------------------------------
# Temporary SQLite database
# ---------------------------------------------------------------------------
def temp_db(tmpdir=None):
    """Return an **unopened** :class:`TempDB`; ``await tmp.open()`` to initialize it."""
    return TempDB(tmpdir)


class TempDB:
    """A throw-away SQLite database under ``tempfile.mkdtemp()``."""

    def __init__(self, tmpdir=None):
        self._owns_dir = tmpdir is None
        self.dir = tmpdir or tempfile.mkdtemp(prefix="onlyyou-tests-")
        self.path = os.path.join(self.dir, "only_you.db")
        self.db = None

    async def open(self):
        mods = load_plugin_modules()
        self.db = mods.db.DatabaseManager(self.path)
        await self.db.initialize()
        return self.db

    async def close(self):
        if self.db is not None:
            await self.db.close()
            self.db = None
        if self._owns_dir:
            shutil.rmtree(self.dir, ignore_errors=True)


class TempDirAsyncTestCase(unittest.IsolatedAsyncioTestCase):
    """Async test case with a temporary directory cleaned up afterwards."""

    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.mkdtemp(prefix="onlyyou-tests-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)


class DatabaseAsyncTestCase(TempDirAsyncTestCase):
    """Async test case with a temporary directory + initialized DatabaseManager."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._temp_db = TempDB(self.tmpdir)
        self.db = await self._temp_db.open()

    async def asyncTearDown(self):
        await self._temp_db.close()
        await super().asyncTearDown()


# ---------------------------------------------------------------------------
# Plugin configs
# ---------------------------------------------------------------------------
def make_plugin_config(**overrides):
    """Return a plugin config dict with the sections used by the modules under test."""
    config = {
        "memory_and_context_config": {
            "enable_tiered_memory": True,
            "sliding_window_size": 5,
            "extract_every_n_messages": 5,
            "max_injected_memories": 6,
            "max_memory_chars_budget": 400,
        },
        "roguelite_schedule_config": {
            "enable_roguelite_schedule": True,
            "max_mutations_per_day": 2,
            "mutation_cooldown_minutes": 120,
            "idle_roam_cooldown_minutes": 45,
            "allow_l3_wake_from_resting": True,
        },
        "relationship_config": {
            "core_user_ids": [],
            "important_user_ids": [],
            "enable_auto_promotion_l2": True,
        },
        "surfing_config": {"enable_bilibili": True, "enable_douyin": False},
        "model_and_tts_config": {},
    }
    config.update(overrides)
    return config


# ---------------------------------------------------------------------------
# Fake AstrBot provider/context layer (see .team/CONTRACTS.md §2.1)
# ---------------------------------------------------------------------------
class FakeProviderMeta:
    """AstrBot ``ProviderMeta``-like object."""

    def __init__(self, provider_id="", model="", type_name="", provider_type="chat_completion"):
        self.id = provider_id
        self.model = model
        self.type = type_name
        self.provider_type = provider_type


class FakeLLMResponse:
    """AstrBot ``LLMResponse``-like object (only ``completion_text`` is consumed)."""

    def __init__(self, completion_text=""):
        self.completion_text = completion_text


class FakeChatProvider:
    """A record-keeping chat provider used by the provider-factory / model-hub tests."""

    instances = []
    default_response = "pong"
    class_get_models = []

    def __init__(self, provider_config, provider_settings=None):
        self.provider_config = dict(provider_config or {})
        self.provider_settings = dict(provider_settings or {})
        self.model_name = str(self.provider_config.get("model") or "")
        self.response_text = FakeChatProvider.default_response
        self.get_models_result = list(FakeChatProvider.class_get_models)
        self.initialized = False
        self.terminated = False
        self.text_chat_calls = []
        self.get_models_calls = 0
        FakeChatProvider.instances.append(self)

    @classmethod
    def reset(cls):
        cls.instances = []
        cls.default_response = "pong"
        cls.class_get_models = []

    async def initialize(self):
        self.initialized = True

    def meta(self):
        return FakeProviderMeta(
            provider_id=str(self.provider_config.get("id") or ""),
            model=self.model_name or str(self.provider_config.get("model") or ""),
            type_name=str(self.provider_config.get("type") or "openai_chat_completion"),
            provider_type=str(self.provider_config.get("provider_type") or "chat_completion"),
        )

    def get_model(self):
        return self.model_name

    async def get_models(self):
        self.get_models_calls += 1
        return list(self.get_models_result)

    async def text_chat(self, prompt="", system_prompt=None, image_urls=None, **kwargs):
        self.text_chat_calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "image_urls": image_urls,
                "kwargs": kwargs,
            }
        )
        return FakeLLMResponse(self.response_text)

    async def terminate(self):
        self.terminated = True


class FakeTTSProvider:
    """A record-keeping TTS provider returning a real temporary wav file path."""

    audio_dir = None
    instances = []

    def __init__(self, provider_config, provider_settings=None):
        self.provider_config = dict(provider_config or {})
        self.provider_settings = dict(provider_settings or {})
        self.initialized = False
        self.terminated = False
        self.get_audio_calls = []
        FakeTTSProvider.instances.append(self)

    @classmethod
    def reset(cls):
        cls.instances = []

    async def initialize(self):
        self.initialized = True

    def meta(self):
        return FakeProviderMeta(
            provider_id=str(self.provider_config.get("id") or ""),
            model=str(self.provider_config.get("model") or ""),
            type_name=str(self.provider_config.get("type") or "openai_tts_api"),
            provider_type="text_to_speech",
        )

    async def get_audio(self, text):
        self.get_audio_calls.append(text)
        directory = FakeTTSProvider.audio_dir
        if not directory:
            directory = tempfile.mkdtemp(prefix="onlyyou-tts-")
            FakeTTSProvider.audio_dir = directory
        path = os.path.join(directory, "tts-%d.wav" % len(self.get_audio_calls))
        with open(path, "wb") as handle:
            handle.write(b"RIFF\x00\x00\x00\x00WAVEfmt ")
        return path

    async def terminate(self):
        self.terminated = True


class FakeProviderMetaData:
    """AstrBot ``ProviderMetaData``-like entry (only ``cls_type`` is used)."""

    def __init__(self, cls_type):
        self.cls_type = cls_type


class FakeProviderManager:
    """Minimal stand-in for ``astrbot.core.provider.manager.ProviderManager``."""

    def __init__(self, sources=None, providers=None, provider_settings=None, audio_dir=None):
        self.provider_sources_config = [dict(item) for item in (sources or [])]
        self.providers_config = [dict(item) for item in (providers or [])]
        self.provider_settings = dict(provider_settings or {})
        self.inst_map = {}
        self.provider_insts = []
        self.tts_provider_insts = []
        self.dynamic_import_calls = []
        self.provider_cls_map = {
            "openai_chat_completion": FakeProviderMetaData(FakeChatProvider),
            "openai_tts_api": FakeProviderMetaData(_make_fake_tts_cls(audio_dir)),
        }

    def get_merged_provider_config(self, provider_config):
        merged = {}
        source_id = (provider_config or {}).get("provider_source_id") or ""
        for source in self.provider_sources_config:
            if source.get("id") == source_id:
                merged.update(copy.deepcopy(source))
                break
        merged.update(copy.deepcopy(provider_config or {}))
        merged["id"] = (provider_config or {}).get("id") or merged.get("id") or ""
        return merged

    def dynamic_import_provider(self, provider_type):
        self.dynamic_import_calls.append(provider_type)

    def add_loaded(self, provider_id, instance):
        """Simulate an already-loaded AstrBot provider instance."""
        self.inst_map[provider_id] = instance
        self.provider_insts.append(instance)
        return instance


def _make_fake_tts_cls(audio_dir):
    class _BoundFakeTTSProvider(FakeTTSProvider):
        pass

    _BoundFakeTTSProvider.audio_dir = audio_dir
    return _BoundFakeTTSProvider


class FakeContext:
    """Minimal stand-in for AstrBot's ``Context`` (only the used attributes)."""

    def __init__(self, provider_manager=None):
        self.provider_manager = provider_manager or FakeProviderManager()
        self.persona_manager = None
        self.config = {}
        self.registered_routes = []
        self.tts_providers = []
        self.providers_by_id = {}
        self.get_config_calls = 0
        self.get_current_chat_provider_id_calls = 0

    def register_web_api(self, route, handler, methods, description=""):
        self.registered_routes.append(
            {
                "route": route,
                "handler": handler,
                "methods": list(methods),
                "description": description,
            }
        )

    def get_all_tts_providers(self):
        return list(self.tts_providers)

    def get_provider_by_id(self, provider_id):
        return self.providers_by_id.get(provider_id)

    def get_config(self):
        self.get_config_calls += 1
        return self.config


class StrictContext(FakeContext):
    """Context whose forbidden model-selection APIs raise ``AssertionError``."""

    def __init__(self, provider_manager=None):
        super().__init__(provider_manager)
        self.forbidden_calls = []

    def get_config(self):
        self.forbidden_calls.append("get_config")
        raise AssertionError("context.get_config() must not be called by ModelHub")

    def get_current_chat_provider_id(self, *args, **kwargs):
        self.forbidden_calls.append("get_current_chat_provider_id")
        raise AssertionError(
            "context.get_current_chat_provider_id() must not be called by ModelHub"
        )


# Default fake AstrBot config used by the Web API tests.
DEFAULT_FAKE_SOURCES = [
    {
        "id": "deepseek",
        "provider": "deepseek",
        "type": "openai_chat_completion",
        "provider_type": "chat_completion",
        "key": "sk-deepseek-secret",
        "api_base": "https://api.deepseek.example/v1",
        "timeout": 120,
        "proxy": "",
        "custom_headers": {},
        "enable": True,
    },
    {
        "id": "ailiyun",
        "provider": "ailiyun",
        "type": "openai_chat_completion",
        "provider_type": "chat_completion",
        "key": "",
        "api_base": "https://api.ailiyun.example/v1",
        "timeout": 60,
        "proxy": "",
        "custom_headers": {},
        "enable": True,
    },
    {
        "id": "edge-tts-source",
        "provider": "edge-tts",
        "type": "openai_tts_api",
        "provider_type": "text_to_speech",
        "key": "sk-tts-secret",
        "api_base": "https://tts.example/v1",
        "timeout": 30,
        "proxy": "",
        "enable": True,
    },
]

DEFAULT_FAKE_PROVIDERS = [
    {
        "id": "deepseek/deepseek-chat",
        "enable": True,
        "provider_source_id": "deepseek",
        "model": "deepseek-chat",
        "modalities": ["text"],
        "custom_extra_body": {},
        "max_context_tokens": 0,
    },
    {
        "id": "ailiyun/kimi-k2.5",
        "enable": False,
        "provider_source_id": "ailiyun",
        "model": "kimi-k2.5",
        "modalities": ["text", "image"],
        "custom_extra_body": {},
        "max_context_tokens": 262144,
    },
    {
        "id": "tts-disabled",
        "enable": False,
        "provider_type": "text_to_speech",
        "type": "openai_tts_api",
        "model": "tts-1",
    },
]


# ---------------------------------------------------------------------------
# Fake plugin (real components + temporary SQLite database; never OnlyYouPlugin)
# ---------------------------------------------------------------------------
class FakePlugin:
    """Assembled stand-in exposing exactly the attributes ``core/web_api.py`` reads.

    It also mirrors the new ``main.py`` runtime attributes/methods that ``web_api``
    lazily calls (``_settle_finished_busy_blocks``) or inspects (``_known_group_umos``,
    ``_last_idle_roam_ts``) so the Web API route tests never hit ``AttributeError``.
    """

    def __init__(self, **components):
        self.__dict__.update(components)
        self._initialized = True
        self._known_l3_umos = {}
        self._last_message_tier = {}
        # 24h dynamic timeline runtime state (same shape as ``OnlyYouPlugin``).
        self._known_group_umos = set()
        self._last_idle_roam_ts = {}
        self._busy_settling = set()

    async def _ensure_initialized(self):
        return None

    async def settle_busy_block(
        self, soul, run, block, progress_pct=100, interrupted_by=""
    ):
        """Stub runtime settler: returns the block's already-known completion result."""
        return str(getattr(block, "completion_result", "") or "")

    async def _settle_finished_busy_blocks(self, soul, run, now_dt=None):
        """Stub lazy settlement (the real ``main.py`` implementation is covered by
        ``test_regressions.TestBusyTaskSettlement``)."""
        return []

    async def _run_idle_roam_action(self, soul, run, current_slot):
        return False


async def build_fake_plugin(
    db,
    tmpdir,
    config=None,
    sources=None,
    providers=None,
    model_config=None,
):
    """Assemble a fake plugin object using the *real* OnlyYou business components."""
    mods = load_plugin_modules()
    config = config or make_plugin_config()
    provider_manager = FakeProviderManager(
        sources=DEFAULT_FAKE_SOURCES if sources is None else sources,
        providers=DEFAULT_FAKE_PROVIDERS if providers is None else providers,
        audio_dir=tmpdir,
    )
    context = FakeContext(provider_manager)
    factory = mods.provider_factory.ProviderFactory(context)
    hub = mods.model_hub.ModelHub(
        context=context, plugin_config=config, db=db, provider_factory=factory
    )
    if model_config:
        await hub.save_model_config(model_config)
    else:
        await hub.load()

    soul_manager = mods.soul_manager.SoulManager(context, db, hub)
    deck_engine = mods.roguelite_deck.RogueliteDeckEngine(db, config)
    schedule_mutator = mods.schedule_mutator.BoundedScheduleMutator(deck_engine, hub, config)
    platform_bridge = mods.platform_bridge.PlatformBridge(context)
    relation = mods.user_relation.UserRelationManager(db, platform_bridge, config)
    memory = mods.memory_manager.MemoryManager(db, hub, config)
    buffer = mods.context_budget.SessionBuffer(config)
    budget = mods.context_budget.ContextBudgetManager(
        memory_manager=memory,
        session_buffer=buffer,
        plugin_config=config,
        soul_manager=soul_manager,
    )
    pipeline = mods.surfing_pipeline.CyberSurfingPipeline(
        model_hub=hub,
        memory_manager=memory,
        session_buffer=buffer,
        schedule_mutator=schedule_mutator,
        plugin_config=config,
    )

    plugin = FakePlugin(
        context=context,
        config=config,
        db=db,
        provider_factory=factory,
        model_hub=hub,
        soul_manager=soul_manager,
        deck_engine=deck_engine,
        schedule_mutator=schedule_mutator,
        platform_bridge=platform_bridge,
        user_relation=relation,
        memory_manager=memory,
        session_buffer=buffer,
        context_budget=budget,
        surfing_pipeline=pipeline,
    )
    api = mods.web_api.OnlyYouWebAPI(plugin)
    plugin.web_api = api
    return plugin, api


# ---------------------------------------------------------------------------
# Card / soul / schedule / user builders
# ---------------------------------------------------------------------------
def card_with(**sections):
    """Build a normalized card from ``{section_id: {field: value}}`` keyword args."""
    mods = load_plugin_modules()
    card = mods.character_card.empty_card()
    for section_id, values in sections.items():
        if not isinstance(values, dict):
            continue
        card.setdefault(section_id, {}).update(values)
    return mods.character_card.normalize_card(card)


def make_soul(soul_id="soul_a", card=None, chronotype=None, **legacy):
    """Build a SoulProfile (defaults to the built-in demo card).

    ``chronotype`` (``early`` / ``normal`` / ``night_owl``) is written into the
    character card's ``rhythm.chronotype`` section so ``SoulProfile.chronotype``
    reports it.
    """
    mods = load_plugin_modules()
    if card is None:
        card = card_with(
            identity={
                "name": "星野",
                "nickname": "小星",
                "tagline": "爱上网冲浪的邻家女孩",
                "avatar": "🌙",
            },
            personality={"personality_tags": ["嘴硬心软", "慢热"]},
            speech={
                "speaking_style": "口语化短句，爱吐槽",
                "reply_length": "short",
                "catchphrases": ["好家伙"],
            },
            interests={"surfing_preferences": ["搞笑整活", "萌宠日常"]},
        )
    card = dict(card or {})
    if chronotype:
        rhythm = dict(card.get("rhythm") or {})
        rhythm["chronotype"] = mods.roguelite_deck.normalize_chronotype(chronotype)
        card["rhythm"] = rhythm
        card = mods.character_card.normalize_card(card)
    return mods.soul_manager.SoulProfile(soul_id=soul_id, card=card, **legacy)


def make_user(uid="user-1", name="阿一", tier=None):
    mods = load_plugin_modules()
    user_tier = mods.state_machine.UserTier.L1_NORMAL if tier is None else tier
    return mods.user_relation.UserProfile(
        global_uid=uid, display_name=name, tier=user_tier, platform_ids=[uid]
    )


def make_slot(
    slot_key="block_0",
    label=None,
    start_hour=9.0,
    end_hour=12.0,
    state=None,
    action_type=None,
    armor=50,
    card_id="evt_test",
    title="专心处理手头正事",
    mutated=False,
    mutation_monologue="",
    delay_minutes=0,
    busy_settled=False,
    completion_result="",
):
    """Build a 24h dynamic timeline block (``SlotInstance``).

    ``action_type`` (str/``ActionType``/``None``) wins over ``state``; when both are
    omitted the action type is derived from the default title (``IDLE_ROAM``).
    ``label=None`` lets ``SlotInstance`` fill the four-action Chinese label itself.
    ``start_hour`` / ``end_hour`` are floats on the 0.5h grid.
    """
    mods = load_plugin_modules()
    bot_state = mods.state_machine.BotState.IDLE if state is None else state
    parsed_action = None
    if action_type is not None:
        parsed_action = mods.roguelite_deck.ActionType.coerce(action_type, None)
    return mods.roguelite_deck.SlotInstance(
        slot_key=slot_key,
        label=label,
        start_hour=float(start_hour),
        end_hour=float(end_hour),
        card_id=card_id,
        title=title,
        state=bot_state,
        action_type=parsed_action,
        armor=armor,
        mutated=mutated,
        mutation_monologue=mutation_monologue,
        delay_minutes=delay_minutes,
        busy_settled=busy_settled,
        completion_result=completion_result,
    )


def make_default_slots():
    """The canonical 8-block 0.0 -> 24.0 default timeline used by ``make_run``.

    Contains all four action types, floats on the 0.5h grid and seamless joints:
    SLEEP 0-7 / IDLE_ROAM 7-9 / BUSY_TASK 9-12 / IDLE_ROAM 12-13 / SURF 13-15 /
    BUSY_TASK 15-18 / IDLE_ROAM 18-23 / SLEEP 23-24.
    """
    mods = load_plugin_modules()
    action = mods.roguelite_deck.ActionType
    bot = mods.state_machine.BotState
    return [
        make_slot(
            slot_key="block_0", start_hour=0.0, end_hour=7.0, state=bot.RESTING,
            action_type=action.SLEEP, card_id="gen_sleep_night", armor=70,
            title="关灯钻进被窝呼呼大睡",
        ),
        make_slot(
            slot_key="block_1", start_hour=7.0, end_hour=9.0, state=bot.IDLE,
            action_type=action.IDLE_ROAM, card_id="gen_roam_commute", armor=35,
            title="叼着面包赶早八通勤",
        ),
        make_slot(
            slot_key="block_2", start_hour=9.0, end_hour=12.0, state=bot.BUSY,
            action_type=action.BUSY_TASK, card_id="gen_busy_focus", armor=60,
            title="沉浸式处理手头正事与学习",
        ),
        make_slot(
            slot_key="block_3", start_hour=12.0, end_hour=13.0, state=bot.IDLE,
            action_type=action.IDLE_ROAM, card_id="gen_roam_slack", armor=30,
            title="午休摸鱼刷手机",
        ),
        make_slot(
            slot_key="block_4", start_hour=13.0, end_hour=15.0, state=bot.IDLE,
            action_type=action.SURF, card_id="gen_surf_evening", armor=25,
            title="躺沙发上快乐刷视频吹水",
        ),
        make_slot(
            slot_key="block_5", start_hour=15.0, end_hour=18.0, state=bot.BUSY,
            action_type=action.BUSY_TASK, card_id="gen_busy_afternoon", armor=55,
            title="跟烦人的任务死磕赶进度",
        ),
        make_slot(
            slot_key="block_6", start_hour=18.0, end_hour=23.0, state=bot.IDLE,
            action_type=action.IDLE_ROAM, card_id="gen_roam_tea", armor=30,
            title="喝下午茶瘫在椅子上逛B站水群",
        ),
        make_slot(
            slot_key="block_7", start_hour=23.0, end_hour=24.0, state=bot.RESTING,
            action_type=action.SLEEP, card_id="gen_sleep_night", armor=65,
            title="关灯准备睡觉",
        ),
    ]


def make_run(
    soul_id="soul_a",
    slots=None,
    encounters=None,
    mutations_used=0,
    last_mutation_ts=0.0,
    chronotype="normal",
    last_mutation_log="",
):
    """Build a ``DailyScheduleRun`` covering the full 0.0 -> 24.0 dynamic timeline."""
    mods = load_plugin_modules()
    if slots is None:
        slots = make_default_slots()
    if encounters is None:
        encounters = []
    return mods.roguelite_deck.DailyScheduleRun(
        soul_id=soul_id,
        date_str=mods.roguelite_deck.RogueliteDeckEngine.today_str(),
        vigor_id="vigor_energetic",
        vigor_title="满血冲浪日",
        vigor_desc="今天精神头超足",
        slots=list(slots),
        encounters=list(encounters),
        mutations_used=mutations_used,
        last_mutation_ts=last_mutation_ts,
        last_mutation_log=last_mutation_log,
        chronotype=mods.roguelite_deck.normalize_chronotype(chronotype),
    )


class StubMemoryManager:
    """Minimal memory manager for context-budget / persona-takeover tests."""

    def __init__(self, memories=None):
        self.memories = list(memories or [])
        self.reinforced = []

    async def retrieve_candidates(self, soul_id=None, user_id=None, limit=40):
        return list(self.memories)

    async def reinforce_memories(self, memory_ids):
        self.reinforced.extend(memory_ids)


class StubModelHub:
    """Minimal ModelHub for schedule-mutator / memory tests."""

    def __init__(self, json_result=None, text_result=""):
        self.json_result = json_result
        self.text_result = text_result
        self.calls = []

    async def call_llm_json(self, prompt="", role="decision", umo=None, system_prompt="",
                            image_urls=None, default=None):
        self.calls.append({"prompt": prompt, "role": role, "umo": umo, "default": default})
        return self.json_result if self.json_result is not None else default

    async def call_llm_text(self, prompt="", role="decision", umo=None, system_prompt="",
                            image_urls=None):
        self.calls.append({"prompt": prompt, "role": role, "umo": umo})
        return self.text_result


class FakeSoulManager:
    """Minimal SoulManager exposing only ``list_native_prompts``."""

    def __init__(self, native_prompts=None):
        self.native_prompts = list(native_prompts or [])

    async def list_native_prompts(self):
        return list(self.native_prompts)


class FakeRequest:
    """Minimal AstrBot ``ProviderRequest`` stand-in."""

    def __init__(self, prompt="", system_prompt="", contexts=None):
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.contexts = list(contexts or [])


class FakeEvent:
    """Minimal AstrBot ``AstrMessageEvent`` stand-in."""

    def __init__(self, message_str="", umo="default:FriendMessage:test", role="member"):
        self.message_str = message_str
        self.unified_msg_origin = umo
        self.role = role

    def is_admin(self):
        return self.role == "admin"


def make_memory(
    id=1,
    soul_id="soul_a",
    content="一条测试记忆",
    keywords="关键词",
    importance=5,
    memory_tier=2,
    vitality=100.0,
    created_at=None,
    user_id=None,
    user_tier=1,
    access_count=0,
):
    """Build a MemoryItem directly (avoids touching SQLite when not needed)."""
    mods = load_plugin_modules()
    import time

    return mods.memory_manager.MemoryItem(
        id=id,
        soul_id=soul_id,
        user_id=user_id,
        user_tier=user_tier,
        memory_tier=memory_tier,
        content=content,
        keywords=keywords,
        importance=importance,
        vitality=vitality,
        access_count=access_count,
        created_at=int(created_at if created_at is not None else time.time()),
        last_accessed_at=None,
    )


def json_dumps(value):
    return json.dumps(value, ensure_ascii=False)
