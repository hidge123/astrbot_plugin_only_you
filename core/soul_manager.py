"""Soul (character card) manager: routing, native Persona import & Roguelite deck seeding.

Key concepts after the 2026 refactor:

- A ``SoulProfile`` owns a nested ``card`` dict (see ``core/character_card.py``).
  The four legacy flat fields (``name`` / ``archetype`` / ``speaking_style`` /
  ``surfing_preferences``) plus ``native_persona_prompt`` remain available as
  read/write properties backed by the card, so every existing caller keeps working.
- ``resolve_soul(umo)`` routing is now:
  ``UMO 绑定 -> plugin_settings.default_soul_id -> 最近更新的角色 -> 创建默认角色``.
  It no longer reads ``default_personality`` nor matches ``persona_hash`` and it
  never seeds cards via the LLM on the chat path.
- AstrBot native Personas are only an **import source**: ``list_native_personas``
  reads ``persona_manager.personas_v3`` first, ``list_native_prompts`` exposes the
  raw prompt strings for the Persona-section takeover done by ``ContextBudgetManager``.
- ``generate_card`` asks the **main** model for a whole-card / single-section draft
  (never persisted); ``import_native_persona`` writes the raw native prompt into
  ``card["extra"]["native_persona_prompt"]`` and optionally asks the main model for
  a full AI card, while the four-type action card pool is generated with the
  **decision** model (encounter cards are no longer generated).
"""

import json
import time
from typing import TYPE_CHECKING, Any, Optional

from .character_card import (
    CARD_SCHEMA,
    CARD_SECTION_IDS,
    empty_card,
    migrate_legacy,
    normalize_card,
)
from .roguelite_deck import StateEventCard
from .state_machine import ActionType
from ..prompts.templates import (
    CARD_AND_DECK_SEED_PROMPT,
    CHARACTER_CARD_GENERATE_PROMPT,
    CHARACTER_CARD_SECTION_PROMPT,
)
from ..storage.db import DatabaseManager

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a hard runtime dependency
    from .model_hub import ModelHub

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger("onlyyou.soul_manager")


_CARD_SECTION_BY_ID: dict[str, dict[str, Any]] = {s["id"]: s for s in CARD_SCHEMA}

DEFAULT_SOUL_ID = "default_soul"
_SETTING_DEFAULT_SOUL_ID = "default_soul_id"

_TAGS_SPLIT_CHARS = ",，、/;；|\n\r"

_DEFAULT_SURFING_PREFERENCES = [
    "搞笑整活",
    "萌宠日常",
    "游戏实况",
    "美食探店",
]

# Valid card['rhythm']['chronotype'] values; anything else normalizes to "normal".
_VALID_CHRONOTYPES = ("early", "normal", "night_owl")

# Best-effort occupation / hobby hints used to tailor the offline fallback card pool.
_PERSONA_KEYWORD_HINTS: tuple[str, ...] = (
    "画师",
    "插画",
    "原画",
    "设计师",
    "程序员",
    "代码",
    "开发",
    "作家",
    "写手",
    "小说",
    "编辑",
    "学生",
    "上课",
    "考研",
    "读研",
    "老师",
    "医生",
    "护士",
    "药师",
    "炼丹",
    "修士",
    "剑客",
    "侠客",
    "魔法",
    "主播",
    "UP主",
    "歌手",
    "音乐",
    "钢琴",
    "吉他",
    "舞者",
    "摄影",
    "咖啡",
    "店主",
    "料理",
    "厨师",
    "烘焙",
    "游戏",
    "电竞",
    "铲屎官",
    "旅行",
    "健身",
    "跑步",
    "追星",
    "二次元",
    "coser",
    "会计",
    "律师",
    "记者",
    "翻译",
)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clean_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
        for ch in _TAGS_SPLIT_CHARS:
            items = [piece for item in items for piece in item.split(ch)]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = _clean_text(item)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _compact_title(text: Any, limit: int = 18) -> str:
    """Return a whitespace-trimmed card title capped at ``limit`` characters."""
    clean = _clean_text(text)
    return clean if len(clean) <= limit else clean[:limit]


def _extract_persona_keywords(persona_prompt: Any) -> list[str]:
    """Best-effort extraction of occupation / hobby keywords from raw Persona text."""
    text = _clean_text(persona_prompt)
    if not text:
        return []
    hits: list[tuple[int, str]] = []
    for keyword in _PERSONA_KEYWORD_HINTS:
        index = text.find(keyword)
        if index >= 0:
            hits.append((index, keyword))
    hits.sort(key=lambda item: item[0])
    keywords: list[str] = []
    for _, keyword in hits:
        if keyword not in keywords:
            keywords.append(keyword)
    return keywords[:3]


class SoulProfile:
    """A character (soul): detailed ``card`` + Roguelite card pool data.

    ``card`` is the single source of truth. The legacy flat attributes are
    compatibility properties so that the existing WebUI / API / main.py code
    does not need to change.
    """

    def __init__(
        self,
        soul_id: str = DEFAULT_SOUL_ID,
        card: Optional[dict[str, Any]] = None,
        state_event_cards: Optional[list[Any]] = None,
        encounter_talk_cards: Optional[list[Any]] = None,
        **legacy: Any,
    ) -> None:
        self.soul_id: str = _clean_text(soul_id) or DEFAULT_SOUL_ID
        base: dict[str, Any] = normalize_card(card) if isinstance(card, dict) else empty_card()

        # Legacy keyword arguments (name=..., archetype=..., ...) are folded into
        # the card so old call sites such as `SoulProfile(name=..., archetype=...)`
        # keep working during the transition.
        legacy_map = {
            "name": ("identity", "name"),
            "archetype": ("identity", "tagline"),
            "speaking_style": ("speech", "speaking_style"),
            "native_persona_prompt": ("extra", "native_persona_prompt"),
        }
        for legacy_key, (section_id, field_key) in legacy_map.items():
            if legacy.get(legacy_key) is not None:
                base[section_id][field_key] = legacy[legacy_key]
        if legacy.get("surfing_preferences") is not None:
            base["interests"]["surfing_preferences"] = legacy["surfing_preferences"]
        self.card: dict[str, Any] = normalize_card(base)

        cards: list[StateEventCard] = []
        for item in state_event_cards or []:
            try:
                if isinstance(item, StateEventCard):
                    cards.append(item)
                elif isinstance(item, dict):
                    cards.append(StateEventCard.from_dict(item))
            except Exception as e:  # noqa: BLE001 - never crash on a single bad card
                logger.warning(f"[OnlyYou] Failed to parse state event card: {e}")
        self.state_event_cards: list[StateEventCard] = cards
        self.encounter_talk_cards: list[str] = [
            text for text in (_clean_text(x) for x in (encounter_talk_cards or [])) if text
        ]

    # ---- Legacy flat-field compatibility properties (backed by the card) ----

    @property
    def name(self) -> str:
        return _clean_text((self.card.get("identity") or {}).get("name"))

    @name.setter
    def name(self, value: Any) -> None:
        self.card["identity"]["name"] = value
        self.card = normalize_card(self.card)

    @property
    def archetype(self) -> str:
        return _clean_text((self.card.get("identity") or {}).get("tagline"))

    @archetype.setter
    def archetype(self, value: Any) -> None:
        self.card["identity"]["tagline"] = value
        self.card = normalize_card(self.card)

    @property
    def speaking_style(self) -> str:
        return _clean_text((self.card.get("speech") or {}).get("speaking_style"))

    @speaking_style.setter
    def speaking_style(self, value: Any) -> None:
        self.card["speech"]["speaking_style"] = value
        self.card = normalize_card(self.card)

    @property
    def surfing_preferences(self) -> list[str]:
        return _clean_list((self.card.get("interests") or {}).get("surfing_preferences"))

    @surfing_preferences.setter
    def surfing_preferences(self, value: Any) -> None:
        self.card["interests"]["surfing_preferences"] = value
        self.card = normalize_card(self.card)

    @property
    def chronotype(self) -> str:
        """Rhythm chronotype: ``early`` / ``normal`` / ``night_owl`` (read-only).

        Reads ``card["rhythm"]["chronotype"]``; empty or invalid values fall back
        to ``"normal"`` so schedule generation always has a usable curve.
        """
        raw = _clean_text((self.card.get("rhythm") or {}).get("chronotype")).lower()
        return raw if raw in _VALID_CHRONOTYPES else "normal"

    @property
    def native_persona_prompt(self) -> str:
        return _clean_text((self.card.get("extra") or {}).get("native_persona_prompt"))

    @native_persona_prompt.setter
    def native_persona_prompt(self, value: Any) -> None:
        self.card["extra"]["native_persona_prompt"] = value
        self.card = normalize_card(self.card)

    # ---- Serialization ----

    def to_dict(self) -> dict[str, Any]:
        """Serialize, emitting **both** the new ``card`` and the legacy flat fields."""
        return {
            "soul_id": self.soul_id,
            "card": normalize_card(self.card),
            "name": self.name,
            "archetype": self.archetype,
            "speaking_style": self.speaking_style,
            "native_persona_prompt": self.native_persona_prompt,
            "surfing_preferences": list(self.surfing_preferences),
            "state_event_cards": [card.to_dict() for card in self.state_event_cards],
            "encounter_talk_cards": list(self.encounter_talk_cards),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SoulProfile":
        """Deserialize: prefer ``card``; otherwise migrate the old flat profile."""
        data = data if isinstance(data, dict) else {}
        raw_card = data.get("card")
        if isinstance(raw_card, dict) and raw_card:
            card = normalize_card(raw_card)
        elif any(isinstance(data.get(sid), dict) for sid in CARD_SECTION_IDS):
            # Tolerate a bare card dict being passed straight in.
            card = normalize_card(data)
        else:
            card = migrate_legacy(data)

        cards: list[StateEventCard] = []
        for item in data.get("state_event_cards") or []:
            try:
                if isinstance(item, StateEventCard):
                    state_card = item
                elif isinstance(item, dict):
                    state_card = StateEventCard.from_dict(item)
                else:
                    continue
                cards.append(state_card)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[OnlyYou] Failed to parse stored state event card: {e}")
        # v2.6: armor is a fixed per-action tier now; normalize cards persisted by
        # an older version on load so cards/list & the UI never surface stale values.
        for state_card in cards:
            state_card.armor = state_card.action.fixed_armor

        return cls(
            soul_id=_clean_text(data.get("soul_id")) or DEFAULT_SOUL_ID,
            card=card,
            state_event_cards=cards,
            encounter_talk_cards=[
                text
                for text in (_clean_text(x) for x in (data.get("encounter_talk_cards") or []))
                if text
            ],
        )


class SoulManager:
    """Manages SoulProfile routing, persistence, native Persona import and AI cards."""

    def __init__(self, context: Any, db: DatabaseManager, model_hub: "ModelHub"):
        self.context = context
        self.db = db
        self.model_hub = model_hub
        self._cache: dict[str, tuple[str, SoulProfile]] = {}
        self._umo_bindings: dict[str, str] = {}
        self._bindings_loaded: bool = False
        self._default_soul_id_cache: Optional[str] = None
        self._native_prompts_cache: Optional[list[str]] = None

    # ==================================================================
    # UMO routing bindings
    # ==================================================================
    async def _ensure_bindings_loaded(self) -> None:
        if self._bindings_loaded:
            return
        try:
            rows = await self.db.fetchall("SELECT umo, soul_id FROM umo_soul_bindings")
            for r in rows:
                if r.get("umo") and r.get("soul_id"):
                    self._umo_bindings[str(r["umo"])] = str(r["soul_id"])
            self._bindings_loaded = True
        except Exception:
            pass

    async def get_umo_bindings(self) -> dict[str, str]:
        """Return all explicit `umo -> soul_id` routing bindings."""
        await self._ensure_bindings_loaded()
        return dict(self._umo_bindings)

    async def set_umo_binding(self, umo: str, soul_id: Optional[str]) -> None:
        """Bind or unbind a specific session `umo` to a `soul_id`."""
        await self._ensure_bindings_loaded()
        clean_umo = (umo or "").strip()
        if not clean_umo:
            return
        clean_soul = (soul_id or "").strip()
        if not clean_soul:
            self._umo_bindings.pop(clean_umo, None)
            await self.db.execute("DELETE FROM umo_soul_bindings WHERE umo = ?", (clean_umo,))
        else:
            self._umo_bindings[clean_umo] = clean_soul
            await self.db.execute(
                """
                INSERT INTO umo_soul_bindings (umo, soul_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(umo) DO UPDATE SET
                    soul_id = excluded.soul_id,
                    updated_at = excluded.updated_at
                """,
                (clean_umo, clean_soul, int(time.time())),
            )

    # ==================================================================
    # Plugin-level default soul (plugin_settings.default_soul_id)
    # ==================================================================
    async def get_default_soul_id(self) -> str:
        """Read the plugin-level default soul id (kept in ``plugin_settings``)."""
        if self._default_soul_id_cache:
            return self._default_soul_id_cache
        try:
            value = await self.db.get_setting(_SETTING_DEFAULT_SOUL_ID, "")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[OnlyYou] Failed to read default_soul_id: {e}")
            value = ""
        soul_id = _clean_text(value)
        if soul_id:
            self._default_soul_id_cache = soul_id
        return soul_id

    async def set_default_soul_id(self, soul_id: str) -> None:
        """Persist the plugin-level default soul id."""
        clean = _clean_text(soul_id)
        self._default_soul_id_cache = clean
        try:
            await self.db.set_setting(_SETTING_DEFAULT_SOUL_ID, clean)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[OnlyYou] Failed to save default_soul_id: {e}")

    # ==================================================================
    # Native AstrBot Persona discovery (import source only)
    # ==================================================================
    async def _collect_native_personas(self) -> list[dict[str, str]]:
        """Collect native personas as ``[{persona_name, prompt}]``.

        Priority: ``persona_manager.personas_v3`` (AstrBot v4 dict list) ->
        legacy ``persona_manager.personas`` -> ``context.get_config().persona``.
        """
        result: list[dict[str, str]] = []
        seen: set[str] = set()

        def _add(name: Any, prompt: Any) -> None:
            pid = _clean_text(name)
            if not pid or pid in seen:
                return
            seen.add(pid)
            result.append({"persona_name": pid, "prompt": _clean_text(prompt)})

        if self.context is not None:
            pm = getattr(self.context, "persona_manager", None)
            if pm is not None:
                # 1) AstrBot v4: personas_v3 is a list of dicts (name/prompt/begin_dialogs)
                for p in getattr(pm, "personas_v3", None) or []:
                    if isinstance(p, dict):
                        _add(p.get("name"), p.get("prompt"))
                    else:
                        _add(getattr(p, "name", None), getattr(p, "prompt", None))

                # 2) Legacy in-memory list (Persona ORM objects or dicts)
                legacy_personas = getattr(pm, "personas", None) or []
                if not legacy_personas and callable(getattr(pm, "get_all_personas", None)):
                    try:
                        legacy_personas = await pm.get_all_personas() or []
                    except Exception as e:  # noqa: BLE001
                        logger.debug(f"[OnlyYou] get_all_personas failed: {e}")
                        legacy_personas = []
                for p in legacy_personas:
                    if isinstance(p, dict):
                        _add(
                            p.get("name") or p.get("persona_id"),
                            p.get("prompt") or p.get("system_prompt"),
                        )
                    else:
                        _add(
                            getattr(p, "persona_id", None) or getattr(p, "name", None),
                            getattr(p, "system_prompt", None) or getattr(p, "prompt", None),
                        )

            # 3) Config-file personas (old AstrBot layouts)
            try:
                ab_conf = self.context.get_config() or {}
                for p in ab_conf.get("persona", []) or []:
                    if isinstance(p, dict):
                        _add(p.get("name"), p.get("prompt"))
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[OnlyYou] Failed to list config personas: {e}")

        if not result:
            result.append(
                {
                    "persona_name": DEFAULT_SOUL_ID,
                    "prompt": "角色名：星野。性格鲜明、喜欢上网冲浪与朋友聊天的真实网友。",
                }
            )
        return result

    async def list_native_personas(self) -> list[dict[str, str]]:
        """List AstrBot native Personas (import source), personas_v3 first."""
        return await self._collect_native_personas()

    async def list_native_prompts(self) -> list[str]:
        """Return raw native Persona prompt strings (memory-cached).

        Used by ``ContextBudgetManager._takeover_persona`` to locate and replace the
        exact ``\\n# Persona Instructions\\n\\n{prompt}\\n`` segment AstrBot injected.
        """
        if self._native_prompts_cache is not None:
            return list(self._native_prompts_cache)
        prompts: list[str] = []
        seen: set[str] = set()
        for item in await self._collect_native_personas():
            prompt = _clean_text(item.get("prompt"))
            if prompt and prompt not in seen:
                seen.add(prompt)
                prompts.append(prompt)
        if prompts:
            self._native_prompts_cache = prompts
        return list(prompts)

    def invalidate_native_prompt_cache(self) -> None:
        """Drop the cached native prompt list (e.g. after AstrBot Persona edits)."""
        self._native_prompts_cache = None

    # ==================================================================
    # CRUD
    # ==================================================================
    async def list_souls(self) -> list[SoulProfile]:
        """Return all stored SoulProfiles in SQLite (ensuring at least 1 default soul exists)."""
        rows = await self.db.fetchall(
            "SELECT soul_id, persona_hash, profile_json FROM souls ORDER BY updated_at DESC"
        )
        souls: list[SoulProfile] = []
        for r in rows:
            try:
                profile = SoulProfile.from_dict(json.loads(r["profile_json"]))
                self._cache[profile.soul_id] = (str(r.get("persona_hash") or ""), profile)
                souls.append(profile)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[OnlyYou] Failed to load soul row: {e}")
                continue

        if not souls:
            default_soul = await self.resolve_soul(allow_llm_seed=False)
            souls.append(default_soul)
        return souls

    async def get_soul_by_id(self, soul_id: str) -> Optional[SoulProfile]:
        """Fetch a specific SoulProfile by `soul_id` from cache or SQLite."""
        sid = (soul_id or "").strip()
        if not sid:
            return None
        if sid in self._cache:
            return self._cache[sid][1]
        row = await self.db.fetchone(
            "SELECT persona_hash, profile_json FROM souls WHERE soul_id = ?", (sid,)
        )
        if row and row.get("profile_json"):
            try:
                profile = SoulProfile.from_dict(json.loads(row["profile_json"]))
                self._cache[sid] = (str(row.get("persona_hash") or ""), profile)
                return profile
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[OnlyYou] Failed to parse soul {sid}: {e}")
                return None
        return None

    async def save_soul(self, profile: SoulProfile, persona_hash: Optional[str] = None) -> SoulProfile:
        """Persist a SoulProfile to SQLite and update the in-memory cache."""
        if profile is None:
            raise ValueError("SoulProfile 不能为空")
        profile.card = normalize_card(profile.card)
        if not (profile.soul_id or "").strip():
            profile.soul_id = DEFAULT_SOUL_ID
        p_hash = persona_hash or "webui_custom"
        self._cache[profile.soul_id] = (p_hash, profile)
        await self.db.execute(
            """
            INSERT INTO souls (soul_id, persona_hash, profile_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(soul_id) DO UPDATE SET
                persona_hash = excluded.persona_hash,
                profile_json = excluded.profile_json,
                updated_at = excluded.updated_at
            """,
            (
                profile.soul_id,
                p_hash,
                json.dumps(profile.to_dict(), ensure_ascii=False),
                int(time.time()),
            ),
        )
        return profile

    async def rename_soul(self, old_soul_id: str, new_soul_id: str) -> SoulProfile:
        """
        Rename `old_soul_id` -> `new_soul_id` and cascade update across:
        - `souls`
        - `daily_schedules`
        - `memories`
        - `umo_soul_bindings`
        - `surfing_history`
        """
        old_id = (old_soul_id or "").strip()
        new_id = (new_soul_id or "").strip()
        if not old_id or not new_id:
            raise ValueError("Soul ID 不能为空")
        if old_id == new_id:
            existing = await self.get_soul_by_id(old_id)
            if existing is None:
                raise ValueError(f"未找到角色 {old_id}")
            return existing

        existing_target = await self.get_soul_by_id(new_id)
        if existing_target is not None:
            raise ValueError(f"目标 Soul ID '{new_id}' 已存在")

        profile = await self.get_soul_by_id(old_id)
        if profile is None:
            raise ValueError(f"未找到原角色 '{old_id}'")

        profile.soul_id = new_id
        await self.save_soul(profile)

        # Cascade updates in SQLite tables
        await self.db.execute("DELETE FROM souls WHERE soul_id = ?", (old_id,))
        await self.db.execute(
            "UPDATE OR REPLACE daily_schedules SET soul_id = ? WHERE soul_id = ?",
            (new_id, old_id),
        )
        # Also update `soul_id` inside `daily_schedules.run_json`
        sched_rows = await self.db.fetchall(
            "SELECT date_str, run_json FROM daily_schedules WHERE soul_id = ?", (new_id,)
        )
        for sr in sched_rows:
            try:
                rdata = json.loads(sr["run_json"])
                rdata["soul_id"] = new_id
                await self.db.execute(
                    "UPDATE daily_schedules SET run_json = ? WHERE soul_id = ? AND date_str = ?",
                    (json.dumps(rdata, ensure_ascii=False), new_id, sr["date_str"]),
                )
            except Exception:
                pass

        await self.db.execute(
            "UPDATE memories SET soul_id = ? WHERE soul_id = ?", (new_id, old_id)
        )
        await self.db.execute(
            "UPDATE umo_soul_bindings SET soul_id = ? WHERE soul_id = ?", (new_id, old_id)
        )
        await self.db.execute(
            "UPDATE surfing_history SET soul_id = ? WHERE soul_id = ?", (new_id, old_id)
        )

        self._cache.pop(old_id, None)
        for k, v in list(self._umo_bindings.items()):
            if v == old_id:
                self._umo_bindings[k] = new_id
        if await self.get_default_soul_id() == old_id:
            await self.set_default_soul_id(new_id)
        return profile

    async def delete_soul(self, soul_id: str) -> None:
        """Delete a SoulProfile and its associated schedules, memories, and bindings."""
        sid = (soul_id or "").strip()
        if not sid:
            return
        self._cache.pop(sid, None)
        for k, v in list(self._umo_bindings.items()):
            if v == sid:
                self._umo_bindings.pop(k, None)
        await self.db.execute("DELETE FROM souls WHERE soul_id = ?", (sid,))
        await self.db.execute("DELETE FROM daily_schedules WHERE soul_id = ?", (sid,))
        await self.db.execute("DELETE FROM memories WHERE soul_id = ?", (sid,))
        await self.db.execute("DELETE FROM umo_soul_bindings WHERE soul_id = ?", (sid,))
        await self.db.execute("DELETE FROM surfing_history WHERE soul_id = ?", (sid,))
        if await self.get_default_soul_id() == sid:
            await self.set_default_soul_id("")

    # ==================================================================
    # Default / fallback cards
    # ==================================================================
    @staticmethod
    def _build_default_card() -> dict[str, Any]:
        """Built-in first-run card (no LLM, no AstrBot Persona auto-import)."""
        card = empty_card()
        card["identity"]["name"] = "星野"
        card["identity"]["nickname"] = "小星"
        card["identity"]["tagline"] = "有自己生活作息与喜怒哀乐的真实网友"
        card["personality"]["personality_tags"] = ["自然随性", "有点小幽默", "慢热"]
        card["speech"]["speaking_style"] = "口语化短句，像微信/QQ好友一样自然随性"
        card["speech"]["reply_length"] = "short"
        card["interests"]["surfing_preferences"] = list(_DEFAULT_SURFING_PREFERENCES)
        return normalize_card(card)

    @staticmethod
    def _build_imported_card(
        persona_name: str, native_prompt: str
    ) -> dict[str, Any]:
        """Basic card inferred from the imported native Persona raw text."""
        card = empty_card()
        display_name = persona_name if persona_name and persona_name != DEFAULT_SOUL_ID else "星野"
        card["identity"]["name"] = display_name
        card["identity"]["tagline"] = (
            _clean_text(native_prompt)[:45] if native_prompt else "真实鲜活的数字网友"
        )
        card["speech"]["speaking_style"] = "口语化短句，像熟人好友一样自然随性"
        card["speech"]["reply_length"] = "short"
        card["interests"]["surfing_preferences"] = list(_DEFAULT_SURFING_PREFERENCES)
        card["extra"]["native_persona_prompt"] = _clean_text(native_prompt)
        return normalize_card(card)

    @staticmethod
    def _fill_card_from_seed_data(
        card: dict[str, Any], data: dict[str, Any], persona_name: str
    ) -> dict[str, Any]:
        """Fill still-empty card fields from the legacy deck-seed JSON response."""
        if _is_empty_value(card["identity"].get("name")):
            card["identity"]["name"] = _clean_text(data.get("name")) or persona_name
        if _is_empty_value(card["identity"].get("tagline")):
            card["identity"]["tagline"] = _clean_text(data.get("archetype"))
        if _is_empty_value(card["speech"].get("speaking_style")):
            card["speech"]["speaking_style"] = _clean_text(data.get("speaking_style"))
        if not card["interests"].get("surfing_preferences"):
            card["interests"]["surfing_preferences"] = _clean_list(
                data.get("surfing_preferences")
            ) or list(_DEFAULT_SURFING_PREFERENCES)
        return normalize_card(card)

    # ==================================================================
    # AI card generation (drafts only, never persisted here)
    # ==================================================================
    @staticmethod
    def _extract_card_payload(raw: Any) -> Optional[dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        if isinstance(raw.get("card"), dict):
            return raw["card"]
        if any(section_id in raw for section_id in CARD_SECTION_IDS):
            return raw
        return None

    @staticmethod
    def _extract_section_payload(
        raw: Any, section_id: str
    ) -> Optional[dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        section_raw = raw.get(section_id)
        if isinstance(section_raw, dict):
            return section_raw
        if isinstance(raw.get("fields"), dict):
            return raw["fields"]
        field_keys = {
            f["key"] for f in _CARD_SECTION_BY_ID[section_id]["fields"]
        }
        if any(key in field_keys for key in raw):
            return raw
        return None

    async def generate_card(
        self,
        seed_text: str = "",
        base_card: Optional[dict[str, Any]] = None,
        section: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Generate a whole-card or single-section draft with the **main** model.

        The returned card is a draft only — it is never written to the database.
        Returns ``None`` when the model is unavailable / the reply cannot be parsed.
        """
        if self.model_hub is None:
            return None

        base = normalize_card(base_card) if isinstance(base_card, dict) else empty_card()
        if isinstance(base_card, dict) and not any(
            isinstance(base_card.get(sid), dict) for sid in CARD_SECTION_IDS
        ):
            # Tolerate a legacy flat profile dict being passed as `base_card`.
            legacy_keys = (
                "name",
                "archetype",
                "speaking_style",
                "surfing_preferences",
                "native_persona_prompt",
            )
            if any(k in base_card for k in legacy_keys):
                base = migrate_legacy(base_card)
        seed = _clean_text(seed_text)

        try:
            if section is not None and _clean_text(section):
                section_id = _clean_text(section)
                section_def = _CARD_SECTION_BY_ID.get(section_id)
                if section_def is None:
                    logger.warning(f"[OnlyYou] generate_card: unknown section '{section}'")
                    return None
                other_sections = {
                    key: value
                    for key, value in base.items()
                    if key in _CARD_SECTION_BY_ID and key != section_id
                }
                prompt = CHARACTER_CARD_SECTION_PROMPT.format(
                    section_json=json.dumps(section_def, ensure_ascii=False),
                    other_sections_json=json.dumps(other_sections, ensure_ascii=False),
                    seed_text=seed,
                )
                raw = await self.model_hub.call_llm_json(
                    prompt=prompt, role="main", default=None
                )
                generated = self._extract_section_payload(raw, section_id)
                if generated is None:
                    return None
                merged = normalize_card(base)
                for field_def in section_def["fields"]:
                    key = field_def["key"]
                    value = generated.get(key)
                    if not _is_empty_value(value):
                        merged[section_id][key] = value
                # The imported native prompt is system-maintained, never AI-generated.
                merged["extra"]["native_persona_prompt"] = base["extra"][
                    "native_persona_prompt"
                ]
                return normalize_card(merged)

            prompt = CHARACTER_CARD_GENERATE_PROMPT.format(
                schema_json=json.dumps(CARD_SCHEMA, ensure_ascii=False),
                seed_text=seed,
                existing_card_json=json.dumps(base, ensure_ascii=False),
            )
            raw = await self.model_hub.call_llm_json(
                prompt=prompt, role="main", default=None
            )
            generated_card = self._extract_card_payload(raw)
            if generated_card is None:
                return None
            merged = normalize_card(base)
            for section_id in CARD_SECTION_IDS:
                generated_section = generated_card.get(section_id)
                if not isinstance(generated_section, dict):
                    continue
                for field_def in _CARD_SECTION_BY_ID[section_id]["fields"]:
                    key = field_def["key"]
                    value = generated_section.get(key)
                    if not _is_empty_value(value):
                        merged[section_id][key] = value
            # Never let the AI overwrite the imported native Persona raw text.
            merged["extra"]["native_persona_prompt"] = base["extra"][
                "native_persona_prompt"
            ]
            return normalize_card(merged)
        except Exception as e:  # noqa: BLE001 - AI generation must never crash callers
            logger.warning(f"[OnlyYou] generate_card failed: {e}")
            return None

    # ==================================================================
    # Fallback Roguelite deck (LLM offline)
    # ==================================================================
    @staticmethod
    def build_fallback_custom_deck(
        soul_name: str, persona_prompt: str
    ) -> list[StateEventCard]:
        """Synthesize a tailored ~10-card action pool when the LLM is offline.

        Covers all four action types (``BUSY_TASK`` x3 / ``SURF`` x2 /
        ``IDLE_ROAM`` x3 / ``SLEEP`` x2: one long sleep + one nap) and never
        produces encounter cards. Every card's ``state`` is bound to its
        ``action_type.bound_state``.
        """
        short_name = _clean_text(soul_name)[:6] or "小星"
        keywords = _extract_persona_keywords(persona_prompt)
        focus = keywords[0] if keywords else ""
        hobby = keywords[1] if len(keywords) > 1 else ""

        if focus:
            busy_title_1 = f"{short_name}埋头忙{focus}的活"
            busy_title_2 = f"{short_name}专心搞{focus}相关事"
        else:
            busy_title_1 = f"{short_name}埋头赶手头的正事"
            busy_title_2 = f"{short_name}专心推进今日正事"
        surf_title_2 = (
            f"{short_name}窝着刷{hobby}短视频"
            if hobby
            else f"{short_name}窝在被窝刷短视频"
        )

        # (title, action_type, min_hours, max_hours, base_weight)
        # 护甲不再由卡池配置：卡片自动取 action_type.fixed_armor（v2.6 四档固定护甲）。
        specs: list[tuple[str, ActionType, float, float, int]] = [
            (busy_title_1, ActionType.BUSY_TASK, 1.0, 3.0, 60),
            (busy_title_2, ActionType.BUSY_TASK, 1.5, 3.0, 55),
            (f"{short_name}关起门来专注赶进度", ActionType.BUSY_TASK, 1.0, 2.5, 50),
            (f"{short_name}瘫沙发刷B站视频", ActionType.SURF, 0.5, 2.0, 75),
            (surf_title_2, ActionType.SURF, 1.0, 2.0, 70),
            (f"{short_name}趴着发呆放空", ActionType.IDLE_ROAM, 0.5, 2.0, 70),
            (f"{short_name}翻群记录偷偷吃瓜", ActionType.IDLE_ROAM, 1.0, 2.0, 65),
            (f"{short_name}泡杯茶悠闲摸鱼", ActionType.IDLE_ROAM, 0.5, 1.5, 60),
            (f"{short_name}戴眼罩呼呼大睡", ActionType.SLEEP, 4.0, 7.5, 80),
            (f"{short_name}趴桌上小憩一会儿", ActionType.SLEEP, 1.0, 2.0, 60),
        ]

        cards: list[StateEventCard] = []
        for index, spec in enumerate(specs, start=1):
            title, action_type, min_hours, max_hours, base_weight = spec
            cards.append(
                StateEventCard(
                    card_id=f"evt_fallback_{action_type.value.lower()}_{index}",
                    title=_compact_title(title),
                    state=action_type.bound_state,
                    action_type=action_type,
                    min_hours=min_hours,
                    max_hours=max_hours,
                    valid_slots=[],
                    base_weight=base_weight,
                )
            )
        return cards

    # ==================================================================
    # Native Persona import
    # ==================================================================
    async def import_native_persona(
        self,
        persona_name: str,
        target_soul_id: Optional[str] = None,
        auto_generate_cards: bool = True,
        override_prompt: Optional[str] = None,
        ai_generate_card: bool = False,
        avatar: str = "",
    ) -> SoulProfile:
        """Import an AstrBot native Persona as a SoulProfile.

        - The raw native prompt is always preserved in
          ``card["extra"]["native_persona_prompt"]`` (never rendered).
        - ``ai_generate_card=True``: ask the **main** model for a full card; on
          failure fall back to the basic card inferred from the raw text.
        - ``auto_generate_cards=True``: ask the **decision** model for the four-type
          action card pool (8~12 cards, no encounter cards); on failure or an empty
          result, use ``build_fallback_custom_deck``.
        - ``avatar`` (optional): emoji / short text / image URL / ``data:image/...``
          Base64 avatar; written into ``card["identity"]["avatar"]`` (normalized)
          after the card is inferred or AI-generated, then persisted with the soul.
        - Encounter cards are gone from the system: imported souls always store
          ``encounter_talk_cards = []``.
        """
        pname = _clean_text(persona_name) or DEFAULT_SOUL_ID
        soul_id = _clean_text(target_soul_id or pname) or pname

        native_prompt = _clean_text(override_prompt)
        if not native_prompt:
            native_list = await self.list_native_personas()
            for item in native_list:
                if _clean_text(item.get("persona_name")) == pname and _clean_text(
                    item.get("prompt")
                ):
                    native_prompt = _clean_text(item.get("prompt"))
                    break
            if not native_prompt:
                for item in native_list:
                    if _clean_text(item.get("prompt")):
                        native_prompt = _clean_text(item.get("prompt"))
                        break

        card = self._build_imported_card(pname, native_prompt)

        # Optional: AI-generated full card (main model), fall back to the inferred card.
        if ai_generate_card and self.model_hub is not None:
            draft = await self.generate_card(seed_text=native_prompt, base_card=card)
            if isinstance(draft, dict):
                card = normalize_card(draft)
                card["extra"]["native_persona_prompt"] = native_prompt

        # The user-supplied avatar (uploaded image / emoji / URL) wins over the
        # inferred or AI-generated one; normalize_card then validates it (§5.1).
        clean_avatar = _clean_text(avatar)
        if clean_avatar:
            card["identity"]["avatar"] = clean_avatar
            card = normalize_card(card)

        state_cards: list[StateEventCard] = []

        if auto_generate_cards:
            data: Optional[dict[str, Any]] = None
            if self.model_hub is not None:
                try:
                    prompt = CARD_AND_DECK_SEED_PROMPT.format(
                        persona_name=pname,
                        native_persona_prompt=native_prompt[:1500],
                    )
                    data = await self.model_hub.call_llm_json(
                        prompt=prompt, role="decision", default=None
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[OnlyYou] Deck seeding failed: {e}")
                    data = None

            if isinstance(data, dict):
                for item in data.get("state_event_cards") or []:
                    if not isinstance(item, dict):
                        continue
                    try:
                        state_cards.append(StateEventCard.from_dict(item))
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[OnlyYou] Failed to parse imported card: {e}")
                if not ai_generate_card:
                    card = self._fill_card_from_seed_data(card, data, pname)

            # LLM offline / failure / empty deck -> deterministic tailored fallback
            # (action cards only; encounter cards are gone from the system).
            if not state_cards:
                state_cards = self.build_fallback_custom_deck(pname, native_prompt)
                logger.info(
                    f"[OnlyYou] Used {len(state_cards)} fallback action cards "
                    f"for imported persona '{pname}'."
                )

        profile = SoulProfile(
            soul_id=soul_id,
            card=card,
            state_event_cards=state_cards,
            encounter_talk_cards=[],
        )
        return await self.save_soul(profile)

    # ==================================================================
    # Chat-path routing
    # ==================================================================
    async def _get_latest_soul(self) -> Optional[SoulProfile]:
        row = await self.db.fetchone(
            "SELECT soul_id FROM souls ORDER BY updated_at DESC, soul_id ASC LIMIT 1"
        )
        if row and row.get("soul_id"):
            return await self.get_soul_by_id(str(row["soul_id"]))
        return None

    async def resolve_soul(
        self, umo: Optional[str] = None, allow_llm_seed: bool = True
    ) -> SoulProfile:
        """Resolve the SoulProfile for a session.

        Routing (CONTRACTS.md §3.2):
        ``UMO 绑定 -> plugin_settings.default_soul_id -> 最近更新的角色 -> 创建默认角色``.

        ``allow_llm_seed`` is accepted for backward compatibility and ignored — the
        chat path never seeds cards with the LLM anymore (creation is offline/free).
        """
        del allow_llm_seed  # signature compatibility only
        await self._ensure_bindings_loaded()

        clean_umo = _clean_text(umo)
        if clean_umo and clean_umo in self._umo_bindings:
            soul = await self.get_soul_by_id(self._umo_bindings[clean_umo])
            if soul is not None:
                return soul

        default_soul_id = await self.get_default_soul_id()
        if default_soul_id:
            soul = await self.get_soul_by_id(default_soul_id)
            if soul is not None:
                return soul

        latest = await self._get_latest_soul()
        if latest is not None:
            return latest

        soul = SoulProfile(soul_id=DEFAULT_SOUL_ID, card=self._build_default_card())
        await self.save_soul(soul)
        await self.set_default_soul_id(soul.soul_id)
        logger.info(f"[OnlyYou] Created default soul '{soul.soul_id}' ({soul.name}).")
        return soul
