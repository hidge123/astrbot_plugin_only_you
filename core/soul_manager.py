"""Soul (Character Persona) manager: inherits AstrBot native Persona & expands Roguelite deck."""

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .model_hub import ModelHub
from .roguelite_deck import StateEventCard
from ..prompts.templates import CARD_AND_DECK_SEED_PROMPT
from ..storage.db import DatabaseManager

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("onlyyou.soul_manager")


@dataclass
class SoulProfile:
    """Expanded character card & custom Roguelite deck seeded from AstrBot Persona."""

    soul_id: str
    name: str
    archetype: str
    speaking_style: str
    native_persona_prompt: str
    surfing_preferences: list[str] = field(default_factory=list)
    state_event_cards: list[StateEventCard] = field(default_factory=list)
    encounter_talk_cards: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "soul_id": self.soul_id,
            "name": self.name,
            "archetype": self.archetype,
            "speaking_style": self.speaking_style,
            "native_persona_prompt": self.native_persona_prompt,
            "surfing_preferences": list(self.surfing_preferences),
            "state_event_cards": [c.to_dict() for c in self.state_event_cards],
            "encounter_talk_cards": list(self.encounter_talk_cards),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SoulProfile":
        return cls(
            soul_id=str(data.get("soul_id", "default_soul")),
            name=str(data.get("name", "小星")),
            archetype=str(data.get("archetype", "年轻有活力的冲浪网友")),
            speaking_style=str(data.get("speaking_style", "口语化短句，自然随性")),
            native_persona_prompt=str(data.get("native_persona_prompt", "")),
            surfing_preferences=list(
                data.get("surfing_preferences") or ["搞笑动物", "游戏整活", "美食探店", "日常吐槽"]
            ),
            state_event_cards=[
                StateEventCard.from_dict(c) for c in data.get("state_event_cards", [])
            ],
            encounter_talk_cards=list(data.get("encounter_talk_cards") or []),
        )


class SoulManager:
    """Manages SoulProfile resolution from AstrBot native Persona with SQLite caching."""

    def __init__(self, context: Any, db: DatabaseManager, model_hub: ModelHub):
        self.context = context
        self.db = db
        self.model_hub = model_hub
        self._cache: dict[str, tuple[str, SoulProfile]] = {}
        self._umo_bindings: dict[str, str] = {}
        self._bindings_loaded: bool = False

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

    async def list_native_personas(self) -> list[dict[str, str]]:
        """Inspect AstrBot `persona_manager` and config to list all native Personas."""
        result: list[dict[str, str]] = []
        seen: set[str] = set()

        if self.context is not None:
            try:
                pm = getattr(self.context, "persona_manager", None)
                if pm is not None:
                    raw_list = getattr(pm, "personas", None)
                    if callable(getattr(pm, "get_all_personas", None)):
                        try:
                            raw_list = await pm.get_all_personas()
                        except Exception:
                            raw_list = getattr(pm, "personas", None)
                    for p in raw_list or []:
                        if isinstance(p, dict):
                            pid = str(p.get("name") or p.get("persona_id") or "").strip()
                            prompt = str(p.get("prompt") or "").strip()
                        else:
                            pid = str(
                                getattr(p, "persona_id", None) or getattr(p, "name", None) or ""
                            ).strip()
                            prompt = str(getattr(p, "prompt", "") or "").strip()
                        if pid and pid not in seen:
                            seen.add(pid)
                            result.append({"persona_name": pid, "prompt": prompt})
            except Exception as e:
                logger.debug(f"[OnlyYou] Failed to list persona_manager personas: {e}")

            try:
                ab_conf = self.context.get_config() or {}
                for p in ab_conf.get("persona", []) or []:
                    if isinstance(p, dict):
                        pid = str(p.get("name") or "").strip()
                        prompt = str(p.get("prompt") or "").strip()
                        if pid and pid not in seen:
                            seen.add(pid)
                            result.append({"persona_name": pid, "prompt": prompt})
            except Exception as e:
                logger.debug(f"[OnlyYou] Failed to list config personas: {e}")

        if not result:
            result.append(
                {
                    "persona_name": "default_soul",
                    "prompt": "角色名：星野。性格鲜明、喜欢上网冲浪与朋友聊天的真实网友。",
                }
            )
        return result

    async def _get_native_persona_info(self, umo: Optional[str] = None) -> tuple[str, str]:
        """Return `(persona_id, persona_prompt)` configured for `umo` in AstrBot."""
        await self._ensure_bindings_loaded()
        if umo and umo in self._umo_bindings:
            bound_id = self._umo_bindings[umo]
            existing = await self.get_soul_by_id(bound_id)
            if existing is not None:
                return existing.soul_id, existing.native_persona_prompt

        if self.context is None:
            return "default_soul", "你是一个真实、自然的年轻网友。"

        persona_id = "default_soul"
        persona_prompt = ""

        try:
            ab_conf = self.context.get_config(umo=umo) or {}
            ps = ab_conf.get("provider_settings", {}) or {}
            raw_pid = str(ps.get("default_personality") or "").strip()
            if raw_pid and raw_pid != "default":
                persona_id = raw_pid

            # Try PersonaManager in AstrBot v4
            pm = getattr(self.context, "persona_manager", None)
            if pm is not None:
                if hasattr(pm, "get_persona"):
                    p_obj = await pm.get_persona(persona_id)
                    if p_obj and getattr(p_obj, "prompt", None):
                        persona_prompt = str(p_obj.prompt)
                if not persona_prompt and hasattr(pm, "personas"):
                    for p in getattr(pm, "personas", []):
                        if isinstance(p, dict) and p.get("name") == persona_id:
                            persona_prompt = str(p.get("prompt", ""))
                            break
                        elif getattr(p, "persona_id", None) == persona_id:
                            persona_prompt = str(getattr(p, "prompt", ""))
                            break

            # Fallback to `persona` list in ab_conf
            if not persona_prompt:
                for p in ab_conf.get("persona", []) or []:
                    if isinstance(p, dict) and p.get("name") == persona_id:
                        persona_prompt = str(p.get("prompt", ""))
                        break
        except Exception as e:
            logger.debug(f"[OnlyYou] Failed to inspect native persona: {e}")

        if not persona_prompt:
            persona_prompt = f"角色名：{persona_id}。性格鲜明、喜欢上网冲浪与朋友聊天的真实网友。"

        return persona_id, persona_prompt

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
            except Exception:
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
            except Exception:
                return None
        return None

    async def save_soul(self, profile: SoulProfile, persona_hash: Optional[str] = None) -> SoulProfile:
        """Persist a SoulProfile to SQLite and update in-memory cache."""
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

    @staticmethod
    def build_fallback_custom_deck(
        soul_name: str, persona_prompt: str
    ) -> tuple[list[StateEventCard], list[str]]:
        """Synthesize a tailored set of 6 action cards and 3 encounter cards when LLM is offline."""
        from .state_machine import BotState

        short_name = (soul_name or "小星").strip()[:10]
        cards = [
            StateEventCard(
                card_id=f"evt_{int(time.time())}_1",
                title=f"{short_name}赖在被窝里补觉充能",
                state=BotState.RESTING,
                valid_slots=["morning", "noon"],
                base_weight=55,
                armor=65,
            ),
            StateEventCard(
                card_id=f"evt_{int(time.time())}_2",
                title=f"专心处理今日正事与学习计划",
                state=BotState.BUSY,
                valid_slots=["forenoon", "afternoon"],
                base_weight=65,
                armor=70,
            ),
            StateEventCard(
                card_id=f"evt_{int(time.time())}_3",
                title=f"边吃外卖边刷手机看沙雕视频",
                state=BotState.IDLE,
                valid_slots=["noon", "evening"],
                base_weight=75,
                armor=25,
            ),
            StateEventCard(
                card_id=f"evt_{int(time.time())}_4",
                title=f"瘫在沙发上摸鱼水群找人唠嗑",
                state=BotState.IDLE,
                valid_slots=["afternoon", "evening"],
                base_weight=70,
                armor=30,
            ),
            StateEventCard(
                card_id=f"evt_{int(time.time())}_5",
                title=f"戴上耳机沉浸式赶进度",
                state=BotState.BUSY,
                valid_slots=["afternoon", "evening"],
                base_weight=50,
                armor=60,
            ),
            StateEventCard(
                card_id=f"evt_{int(time.time())}_6",
                title=f"关灯缩进被窝开启免打扰睡眠",
                state=BotState.RESTING,
                valid_slots=["night"],
                base_weight=80,
                armor=75,
            ),
        ]
        encounters = [
            f"今天点外卖的时候老板居然多送了一份小食，心情瞬间起飞！",
            f"刚才出门在路边看到一只超粘人的小猫，蹲着撸了好久才走",
            f"今天刷到一个离谱又好笑的整活神评，差点笑出鹅叫",
        ]
        return cards, encounters

    async def import_native_persona(
        self,
        persona_name: str,
        target_soul_id: Optional[str] = None,
        auto_generate_cards: bool = True,
        override_prompt: Optional[str] = None,
    ) -> SoulProfile:
        """
        Import an AstrBot native Persona as a SoulProfile:
        - Mode A (`auto_generate_cards=False`): direct inheritance with general card pool
        - Mode B (`auto_generate_cards=True`): LLM expansion + custom 3-state action card & encounter deck
        """
        pname = (persona_name or "default_soul").strip()
        soul_id = (target_soul_id or pname).strip() or pname

        native_prompt = (override_prompt or "").strip()
        if not native_prompt:
            native_list = await self.list_native_personas()
            for item in native_list:
                if item["persona_name"] == pname:
                    native_prompt = item["prompt"]
                    break
        if not native_prompt:
            _, native_prompt = await self._get_native_persona_info()

        profile: Optional[SoulProfile] = None
        if auto_generate_cards:
            prompt = CARD_AND_DECK_SEED_PROMPT.format(
                persona_name=pname,
                native_persona_prompt=native_prompt[:1500],
            )
            data = await self.model_hub.call_llm_json(
                prompt=prompt, role="small", default=None
            )
            if isinstance(data, dict) and data.get("name"):
                try:
                    cards = [
                        StateEventCard.from_dict(c)
                        for c in (data.get("state_event_cards") or [])
                        if isinstance(c, dict)
                    ]
                    encounters = [
                        str(x).strip()
                        for x in (data.get("encounter_talk_cards") or [])
                        if str(x).strip()
                    ]
                    profile = SoulProfile(
                        soul_id=soul_id,
                        name=str(data.get("name") or pname),
                        archetype=str(data.get("archetype") or "真实鲜活的数字网友"),
                        speaking_style=str(data.get("speaking_style") or "自然口语化短句"),
                        native_persona_prompt=native_prompt,
                        surfing_preferences=list(
                            data.get("surfing_preferences")
                            or ["搞笑整活", "萌宠日常", "二次元", "美食"]
                        ),
                        state_event_cards=cards,
                        encounter_talk_cards=encounters,
                    )
                except Exception as e:
                    logger.warning(f"[OnlyYou] Failed to parse imported persona JSON: {e}")

            if profile is None:
                cards, encounters = self.build_fallback_custom_deck(pname, native_prompt)
                profile = SoulProfile(
                    soul_id=soul_id,
                    name=pname if pname != "default_soul" else "星野",
                    archetype=native_prompt[:45] if native_prompt else "真实鲜活的数字网友",
                    speaking_style="口语化短句，像熟人好友一样自然随性",
                    native_persona_prompt=native_prompt,
                    surfing_preferences=["搞笑整活", "萌宠日常", "游戏实况", "美食探店"],
                    state_event_cards=cards,
                    encounter_talk_cards=encounters,
                )
        else:
            profile = SoulProfile(
                soul_id=soul_id,
                name=pname if pname != "default_soul" else "星野",
                archetype=native_prompt[:45] if native_prompt else "有自己生活作息的真实网友",
                speaking_style="口语化短句，自然随性",
                native_persona_prompt=native_prompt,
                surfing_preferences=["搞笑动物", "游戏整活", "美食探店", "日常吐槽"],
                state_event_cards=[],
                encounter_talk_cards=[],
            )

        return await self.save_soul(profile)

    async def resolve_soul(
        self, umo: Optional[str] = None, allow_llm_seed: bool = True
    ) -> SoulProfile:
        """
        Resolve SoulProfile for `umo`:
        1. Check memory cache / SQLite by `(soul_id, persona_hash)`
        2. Optionally seed custom Roguelite cards via `CARD_AND_DECK_SEED_PROMPT`
        3. Fallback to default SoulProfile if LLM is unavailable
        """
        persona_id, persona_prompt = await self._get_native_persona_info(umo=umo)
        p_hash = hashlib.md5(f"{persona_id}:{persona_prompt}".encode("utf-8")).hexdigest()[:16]

        if persona_id in self._cache:
            cached_hash, cached_profile = self._cache[persona_id]
            if cached_hash in (p_hash, "webui_custom"):
                return cached_profile

        row = await self.db.fetchone(
            "SELECT persona_hash, profile_json FROM souls WHERE soul_id = ?",
            (persona_id,),
        )
        if row and row.get("persona_hash") in (p_hash, "webui_custom") and row.get("profile_json"):
            try:
                profile = SoulProfile.from_dict(json.loads(row["profile_json"]))
                self._cache[persona_id] = (str(row["persona_hash"]), profile)
                return profile
            except Exception:
                pass

        # Seed via LLM if allowed
        profile: Optional[SoulProfile] = None
        if allow_llm_seed and self.context is not None:
            prompt = CARD_AND_DECK_SEED_PROMPT.format(
                persona_name=persona_id,
                native_persona_prompt=persona_prompt[:1500],
            )
            data = await self.model_hub.call_llm_json(
                prompt=prompt, role="small", umo=umo, default=None
            )
            if isinstance(data, dict) and data.get("name"):
                try:
                    cards = [
                        StateEventCard.from_dict(c)
                        for c in (data.get("state_event_cards") or [])
                        if isinstance(c, dict)
                    ]
                    encounters = [
                        str(x).strip()
                        for x in (data.get("encounter_talk_cards") or [])
                        if str(x).strip()
                    ]
                    profile = SoulProfile(
                        soul_id=persona_id,
                        name=str(data.get("name") or persona_id),
                        archetype=str(data.get("archetype") or "真实鲜活的数字网友"),
                        speaking_style=str(data.get("speaking_style") or "自然口语化短句"),
                        native_persona_prompt=persona_prompt,
                        surfing_preferences=list(
                            data.get("surfing_preferences")
                            or ["搞笑整活", "萌宠日常", "二次元", "美食"]
                        ),
                        state_event_cards=cards,
                        encounter_talk_cards=encounters,
                    )
                except Exception as e:
                    logger.warning(f"[OnlyYou] Failed to parse seeded soul JSON: {e}")

        if profile is None:
            profile = SoulProfile(
                soul_id=persona_id,
                name=persona_id if persona_id != "default_soul" else "星野",
                archetype="有自己生活作息与喜怒哀乐的真实网友",
                speaking_style="口语化短句，像微信/QQ好友一样自然随性",
                native_persona_prompt=persona_prompt,
            )

        self._cache[persona_id] = (p_hash, profile)
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
                persona_id,
                p_hash,
                json.dumps(profile.to_dict(), ensure_ascii=False),
                int(time.time()),
            ),
        )
        return profile
