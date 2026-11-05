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

    async def _get_native_persona_info(self, umo: Optional[str] = None) -> tuple[str, str]:
        """Return `(persona_id, persona_prompt)` configured for `umo` in AstrBot."""
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
            if cached_hash == p_hash:
                return cached_profile

        row = await self.db.fetchone(
            "SELECT persona_hash, profile_json FROM souls WHERE soul_id = ?",
            (persona_id,),
        )
        if row and row.get("persona_hash") == p_hash and row.get("profile_json"):
            try:
                profile = SoulProfile.from_dict(json.loads(row["profile_json"]))
                self._cache[persona_id] = (p_hash, profile)
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
