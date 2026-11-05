"""Detailed character card schema, normalization, migration and prompt rendering.

Single source of truth for the OnlyYou "角色卡" (character card):

1. ``CARD_SCHEMA``  10 sections / 39 fields. It is consumed by:
   - backend: ``normalize_card`` / ``render_persona_prompt`` / ``card_completion``
   - frontend: ``GET card/schema`` （WebAPI 负责人下发，前端据此动态生成表单）
2. ``normalize_card``  is the defensive entry point: it coerces types, splits
   string tags/lists, tolerates several dialog shapes and **keeps unknown keys**
   so that values written by a newer/older frontend are never silently lost.
3. ``migrate_legacy``  upgrades the old flat ``SoulProfile`` dict
   (name / archetype / speaking_style / surfing_preferences / native_persona_prompt)
   into the new nested card structure.
4. ``render_persona_prompt`` builds the Markdown persona block that **replaces the
   AstrBot native Persona section** inside ``req.system_prompt``. It only renders
   non-empty fields, prefers ``identity/speech/personality/social`` and hard-guarantees
   ``len(result) <= max_chars`` by dropping low priority sections first.
5. ``render_card_summary`` builds the short ``{soul_summary}`` snippet injected into
   proactive-chat / atmosphere prompts.

``native_persona_prompt`` (the imported AstrBot Persona raw text) is recorded inside
``extra`` but is deliberately **not** rendered into the persona prompt.
"""

import json
import re
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Select options
# ---------------------------------------------------------------------------

_GENDER_OPTIONS = [
    {"value": "女", "label": "女"},
    {"value": "男", "label": "男"},
    {"value": "其他", "label": "其他"},
]

_MBTI_OPTIONS = [
    {"value": v, "label": v}
    for v in (
        "INTJ",
        "INTP",
        "ENTJ",
        "ENTP",
        "INFJ",
        "INFP",
        "ENFJ",
        "ENFP",
        "ISTJ",
        "ISFJ",
        "ESTJ",
        "ESFJ",
        "ISTP",
        "ISFP",
        "ESTP",
        "ESFP",
    )
]

_REPLY_LENGTH_OPTIONS = [
    {"value": "very_short", "label": "极短（10 字以内）"},
    {"value": "short", "label": "短句（20 字以内）"},
    {"value": "medium", "label": "适中（30~60 字）"},
    {"value": "long", "label": "较长（可展开多句）"},
]

_CHRONOTYPE_OPTIONS = [
    {"value": "early", "label": "早起型"},
    {"value": "normal", "label": "正常作息"},
    {"value": "night_owl", "label": "夜猫子"},
]

# ---------------------------------------------------------------------------
# CARD_SCHEMA (10 sections, field keys strictly follow CONTRACTS.md §3.1)
# ---------------------------------------------------------------------------

CARD_SCHEMA: list[dict[str, Any]] = [
    {
        "id": "identity",
        "title": "基础身份",
        "fields": [
            {
                "key": "name",
                "label": "名字",
                "type": "text",
                "placeholder": "小星",
                "help": "角色最常用的名字，会出现在人格提示的开头",
            },
            {
                "key": "nickname",
                "label": "自称/网名",
                "type": "text",
                "placeholder": "星星",
                "help": "角色在聊天中的自称或常用网名",
            },
            {
                "key": "tagline",
                "label": "一句话定位",
                "type": "text",
                "placeholder": "住在隔壁、爱上网冲浪的邻家女孩",
                "help": "一句话概括角色在社交网络上的核心人设",
            },
            {
                "key": "gender",
                "label": "性别",
                "type": "select",
                "placeholder": "请选择",
                "help": "",
                "options": _GENDER_OPTIONS,
            },
            {
                "key": "age",
                "label": "年龄",
                "type": "text",
                "placeholder": "21",
                "help": "可写具体年龄或年龄段",
            },
            {
                "key": "birthday",
                "label": "生日",
                "type": "text",
                "placeholder": "3 月 14 日",
                "help": "",
            },
            {
                "key": "occupation",
                "label": "职业/身份",
                "type": "text",
                "placeholder": "大三在读 / 独立插画师",
                "help": "",
            },
            {
                "key": "city",
                "label": "所在城市",
                "type": "text",
                "placeholder": "杭州",
                "help": "",
            },
            {
                "key": "avatar",
                "label": "头像",
                "type": "text",
                "placeholder": "🌙 或 星",
                "help": "emoji 或首字母，用于控制台头像展示",
            },
        ],
    },
    {
        "id": "appearance",
        "title": "外貌形象",
        "fields": [
            {
                "key": "appearance",
                "label": "外貌描述",
                "type": "textarea",
                "placeholder": "黑色短发，右耳戴一枚银色小耳钉，常穿宽松卫衣……",
                "help": "身高、发型、长相、体态等细节",
            },
            {
                "key": "outfit_style",
                "label": "穿衣风格",
                "type": "text",
                "placeholder": "宽松休闲、浅色系",
                "help": "",
            },
        ],
    },
    {
        "id": "personality",
        "title": "性格内核",
        "fields": [
            {
                "key": "personality_tags",
                "label": "性格标签",
                "type": "tags",
                "placeholder": "嘴硬心软、慢热、爱吐槽",
                "help": "逗号 / 顿号 / 斜杠分隔",
            },
            {
                "key": "mbti",
                "label": "MBTI",
                "type": "select",
                "placeholder": "请选择",
                "help": "",
                "options": _MBTI_OPTIONS,
            },
            {
                "key": "strengths",
                "label": "优点",
                "type": "text",
                "placeholder": "共情力强、记性好",
                "help": "",
            },
            {
                "key": "weaknesses",
                "label": "缺点",
                "type": "text",
                "placeholder": "容易拖延、嘴太快",
                "help": "",
            },
            {
                "key": "temperament",
                "label": "情绪与脾气",
                "type": "textarea",
                "placeholder": "平时温和，被放鸽子会生闷气，但很快自己消化……",
                "help": "",
            },
            {
                "key": "values",
                "label": "价值观",
                "type": "text",
                "placeholder": "真诚比体面重要",
                "help": "",
            },
        ],
    },
    {
        "id": "background",
        "title": "背景故事",
        "fields": [
            {
                "key": "growth_story",
                "label": "成长经历",
                "type": "textarea",
                "placeholder": "在小城长大，大学才来到大城市……",
                "help": "长文本，可写几段",
            },
            {
                "key": "family",
                "label": "家庭",
                "type": "text",
                "placeholder": "父母经营一家小面馆",
                "help": "",
            },
            {
                "key": "key_experiences",
                "label": "重要经历",
                "type": "text",
                "placeholder": "高中时组过乐队",
                "help": "",
            },
        ],
    },
    {
        "id": "interests",
        "title": "兴趣喜好",
        "fields": [
            {
                "key": "hobbies",
                "label": "爱好",
                "type": "tags",
                "placeholder": "摄影、citywalk、打游戏",
                "help": "逗号 / 顿号 / 斜杠分隔",
            },
            {
                "key": "likes",
                "label": "喜欢的东西",
                "type": "text",
                "placeholder": "冰美式、雨天、猫",
                "help": "",
            },
            {
                "key": "dislikes",
                "label": "讨厌的东西/雷点",
                "type": "text",
                "placeholder": "香菜、被已读不回",
                "help": "聊天中会回避或反感的内容",
            },
            {
                "key": "surfing_preferences",
                "label": "冲浪偏好",
                "type": "tags",
                "placeholder": "搞笑整活、萌宠日常、美食探店",
                "help": "IDLE 空闲时最爱刷的 B 站 / 抖音内容标签",
            },
        ],
    },
    {
        "id": "speech",
        "title": "说话方式",
        "fields": [
            {
                "key": "speaking_style",
                "label": "整体风格",
                "type": "textarea",
                "placeholder": "口语化短句，像熟人聊天，偶尔冷幽默……",
                "help": "句式长短、语气、幽默感、对话节奏",
            },
            {
                "key": "catchphrases",
                "label": "口头禅",
                "type": "list",
                "placeholder": "好家伙、离谱、笑死",
                "help": "每行一条，也可用逗号分隔",
            },
            {
                "key": "verbal_tics",
                "label": "常用语气词/颜文字",
                "type": "text",
                "placeholder": "啦、诶嘿、(´･ω･`)",
                "help": "",
            },
            {
                "key": "punctuation_habit",
                "label": "标点习惯",
                "type": "text",
                "placeholder": "几乎不用句号，爱用波浪号",
                "help": "",
            },
            {
                "key": "reply_length",
                "label": "回复长度偏好",
                "type": "select",
                "placeholder": "请选择",
                "help": "",
                "options": _REPLY_LENGTH_OPTIONS,
            },
            {
                "key": "banned_expressions",
                "label": "禁用表达",
                "type": "list",
                "placeholder": "不要叫我老师、不要说“作为一个AI”",
                "help": "角色绝不会说出口的表达",
            },
        ],
    },
    {
        "id": "social",
        "title": "社交关系",
        "fields": [
            {
                "key": "attitude_l3",
                "label": "对 ★L3 核心用户的态度与称呼",
                "type": "text",
                "placeholder": "会撒娇、直呼昵称，消息秒回",
                "help": "",
            },
            {
                "key": "attitude_l2",
                "label": "对 ◆L2 重要熟人的态度",
                "type": "text",
                "placeholder": "熟络但不越界，会互相玩梗",
                "help": "",
            },
            {
                "key": "attitude_l1",
                "label": "对 L1 普通群友的态度",
                "type": "text",
                "placeholder": "礼貌但有距离感，主要看心情接话",
                "help": "",
            },
            {
                "key": "group_role",
                "label": "群聊中的角色定位",
                "type": "text",
                "placeholder": "气氛组、偶尔冒泡的潜水党",
                "help": "",
            },
        ],
    },
    {
        "id": "rhythm",
        "title": "生活作息",
        "fields": [
            {
                "key": "chronotype",
                "label": "作息类型",
                "type": "select",
                "placeholder": "请选择",
                "help": "",
                "options": _CHRONOTYPE_OPTIONS,
            },
            {
                "key": "daily_routine",
                "label": "日常安排描述",
                "type": "textarea",
                "placeholder": "早上赖床到最后一刻，白天上课/赶稿，晚上刷手机到一两点……",
                "help": "描述一天大致安排，作为生成行动卡的依据",
            },
        ],
    },
    {
        "id": "examples",
        "title": "示例对话",
        "fields": [
            {
                "key": "example_dialogs",
                "label": "示例对话",
                "type": "dialogs",
                "placeholder": "用户：在干嘛？｜角色：刚下班，瘫着",
                "help": "few-shot 对话对，用于校准说话风格",
            },
        ],
    },
    {
        "id": "extra",
        "title": "补充设定",
        "fields": [
            {
                "key": "extra_notes",
                "label": "自由补充",
                "type": "textarea",
                "placeholder": "任何上面没覆盖到的设定……",
                "help": "",
            },
            {
                "key": "native_persona_prompt",
                "label": "原生人设原文",
                "type": "textarea",
                "placeholder": "从 AstrBot 原生 Persona 导入的原文（只读）",
                "help": "仅记录导入来源，不参与人格 Prompt 渲染",
                "readonly": True,
            },
        ],
    },
]

CARD_SECTION_IDS: list[str] = [section["id"] for section in CARD_SCHEMA]

# Derived lookup tables (kept internal; rebuilt once at import time)
_SECTION_BY_ID: dict[str, dict[str, Any]] = {s["id"]: s for s in CARD_SCHEMA}
_FIELD_BY_KEY: dict[str, dict[str, dict[str, Any]]] = {
    s["id"]: {f["key"]: f for f in s["fields"]} for s in CARD_SCHEMA
}
_LIST_TYPES = ("tags", "list", "dialogs")
_NUMERIC_FIELD_KEYS = ("age",)
_LEGACY_FLAT_KEYS = (
    "name",
    "archetype",
    "speaking_style",
    "surfing_preferences",
    "native_persona_prompt",
)

# 优先级：identity / speech / personality / social 优先保留；
# 超出预算时按 examples > background > appearance > extra/rhythm > interests 顺序丢弃。
_KEEP_FIRST_SECTIONS = ("identity", "speech", "personality", "social")
_DROP_ORDER = ("examples", "background", "appearance", "extra", "rhythm", "interests")
_BASE_DROP_ORDER = ("social", "personality", "speech")  # identity is always kept (opening line)

_TAGS_SPLIT_RE = re.compile(r"[,，、/;；|\n\r]+")


# ---------------------------------------------------------------------------
# Empty card / type coercion helpers
# ---------------------------------------------------------------------------


def _default_for_type(field_type: str) -> Any:
    """Return the canonical empty value for a field type."""
    if field_type in _LIST_TYPES:
        return []
    return ""


def empty_card() -> dict[str, Any]:
    """Return a fully-shaped empty card (every section / every field)."""
    card: dict[str, Any] = {}
    for section in CARD_SCHEMA:
        section_data: dict[str, Any] = {}
        for field_def in section["fields"]:
            section_data[field_def["key"]] = _default_for_type(field_def["type"])
        card[section["id"]] = section_data
    return card


def _as_text(value: Any) -> str:
    """Coerce any scalar to a clean string (numbers -> str, as required by the contract)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "是" if value else ""
    if isinstance(value, (int, float)):
        try:
            if isinstance(value, float) and value.is_integer():
                return str(int(value))
        except (OverflowError, ValueError):
            pass
        return str(value)
    if isinstance(value, (list, tuple)):
        return "、".join(item for item in (_as_text(v) for v in value) if item)
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value).strip()


def _as_list(value: Any) -> list[str]:
    """Coerce tags / list fields: accept arrays or comma/顿号/slash separated strings."""
    if value is None:
        return []
    if isinstance(value, str):
        items = _TAGS_SPLIT_RE.split(value)
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = _as_text(item)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _dialog_pair_from_dict(item: dict[str, Any]) -> Optional[dict[str, str]]:
    """Normalize a {user, character} / {role, content} style dialog dict."""
    user_text = _as_text(
        item.get("user")
        if item.get("user") is not None
        else item.get("user_text")
        if item.get("user_text") is not None
        else item.get("question")
    )
    character_text = ""
    for key in ("character", "assistant", "bot", "reply", "answer", "character_text"):
        if item.get(key) is not None:
            character_text = _as_text(item.get(key))
            break
    if not user_text and not character_text:
        return None
    return {"user": user_text, "character": character_text}


def _as_dialogs(value: Any) -> list[dict[str, str]]:
    """Tolerantly normalize dialog lists into ``[{"user": ..., "character": ...}]``."""
    if isinstance(value, dict):
        pair = _dialog_pair_from_dict(value)
        return [pair] if pair else []
    if not isinstance(value, (list, tuple)):
        return []

    result: list[dict[str, str]] = []
    pending_user = ""
    for item in value:
        if isinstance(item, dict):
            if "user" in item or "character" in item:
                pair = _dialog_pair_from_dict(item)
                if pair:
                    result.append(pair)
                continue
            if "role" in item or "content" in item or "speaker" in item:
                role = _as_text(item.get("role") or item.get("speaker")).lower()
                content = _as_text(
                    item.get("content")
                    if item.get("content") is not None
                    else item.get("text")
                    if item.get("text") is not None
                    else item.get("message")
                )
                if not content:
                    continue
                is_user = role in ("user", "human", "question", "u") or role.startswith("user")
                if is_user:
                    pending_user = content
                else:
                    result.append(
                        {"user": pending_user, "character": content}
                    )
                    pending_user = ""
                continue
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            result.append(
                {"user": _as_text(item[0]), "character": _as_text(item[1])}
            )
            continue
        text = _as_text(item)
        if text:
            if pending_user:
                result.append({"user": pending_user, "character": text})
                pending_user = ""
            else:
                pending_user = text
    if pending_user:
        result.append({"user": pending_user, "character": ""})
    return [pair for pair in result if pair["user"] or pair["character"]]


def _coerce_field_value(section_id: str, field_key: str, field_type: str, value: Any) -> Any:
    """Coerce one field value according to its schema type."""
    if field_type == "tags":
        return _as_list(value)
    if field_type == "list":
        return _as_list(value)
    if field_type == "dialogs":
        return _as_dialogs(value)
    if field_type == "select":
        return _as_text(value)
    # text / textarea: numbers become strings; lists are flattened for tolerance
    return _as_text(value)


def _type_of(section_id: str, field_key: str) -> Optional[str]:
    field_def = _FIELD_BY_KEY.get(section_id, {}).get(field_key)
    return field_def["type"] if field_def else None


# ---------------------------------------------------------------------------
# Normalization / migration
# ---------------------------------------------------------------------------


def normalize_card(raw: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Defensively normalize a card dict.

    - Fills every missing section / field with its canonical empty value.
    - Coerces ``tags`` / ``list`` from comma separated strings, ``dialogs`` from
      ``{user, character}`` or ``{role, content}`` shapes, numbers to strings.
    - **Keeps unknown keys**: unknown top-level keys, unknown keys inside known
      sections and fields added by a newer frontend are preserved untouched.
    """
    card = empty_card()

    # Tolerate a JSON string (e.g. pasted from the WebUI) and a {"card": {...}} wrapper.
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = None
    if not isinstance(raw, dict):
        return card
    if not any(isinstance(raw.get(sid), dict) for sid in CARD_SECTION_IDS):
        wrapped = raw.get("card")
        if isinstance(wrapped, dict):
            raw = wrapped

    for section in CARD_SCHEMA:
        section_id = section["id"]
        raw_section = raw.get(section_id)
        if not isinstance(raw_section, dict):
            continue
        target = card[section_id]
        for key, value in raw_section.items():
            field_type = _type_of(section_id, key)
            if field_type is None:
                # Unknown field key inside a known section -> preserve verbatim.
                target[key] = value
            else:
                target[key] = _coerce_field_value(section_id, key, field_type, value)

    # Unknown top-level keys are preserved verbatim (future schema / UI metadata).
    for key, value in raw.items():
        if key not in _SECTION_BY_ID:
            card[key] = value
    return card


def migrate_legacy(profile: dict[str, Any]) -> dict[str, Any]:
    """Migrate an old flat ``SoulProfile`` dict into the new nested card."""
    card = empty_card()
    if not isinstance(profile, dict):
        return card
    if isinstance(profile.get("card"), dict) and profile.get("card"):
        return normalize_card(profile["card"])

    card["identity"]["name"] = _as_text(profile.get("name"))
    card["identity"]["tagline"] = _as_text(profile.get("archetype"))
    card["speech"]["speaking_style"] = _as_text(profile.get("speaking_style"))
    card["interests"]["surfing_preferences"] = _as_list(
        profile.get("surfing_preferences")
    )
    card["extra"]["native_persona_prompt"] = _as_text(
        profile.get("native_persona_prompt")
    )
    return normalize_card(card)


def _looks_like_legacy_profile(raw: Any) -> bool:
    """True when ``raw`` looks like an old flat profile rather than a nested card."""
    if not isinstance(raw, dict):
        return False
    if any(isinstance(raw.get(section_id), dict) for section_id in CARD_SECTION_IDS):
        return False
    return any(key in raw for key in _LEGACY_FLAT_KEYS)


def _coerce_card(raw: Any) -> dict[str, Any]:
    """Accept a card, a legacy flat profile or a ``{"card": {...}}`` wrapper."""
    if _looks_like_legacy_profile(raw):
        return migrate_legacy(raw)
    return normalize_card(raw)


# ---------------------------------------------------------------------------
# Completion statistics (for the section nav dots in the WebUI)
# ---------------------------------------------------------------------------


def _is_filled(section_id: str, field_key: str, field_type: str, value: Any) -> bool:
    if field_type == "tags" or field_type == "list":
        return bool(_as_list(value))
    if field_type == "dialogs":
        return bool(_as_dialogs(value))
    return bool(_as_text(value))


def card_completion(card: Optional[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Return ``{section_id: {"filled": n, "total": m}}`` for the section nav dots."""
    normalized = _coerce_card(card)
    result: dict[str, dict[str, int]] = {}
    for section in CARD_SCHEMA:
        section_id = section["id"]
        data = normalized.get(section_id) or {}
        filled = 0
        for field_def in section["fields"]:
            value = data.get(field_def["key"])
            if _is_filled(section_id, field_def["key"], field_def["type"], value):
                filled += 1
        result[section_id] = {"filled": filled, "total": len(section["fields"])}
    return result


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def _select_label(field_def: dict[str, Any], value: str) -> str:
    """Map a select enum value to its human label (fallback: the raw value)."""
    for option in field_def.get("options") or []:
        if isinstance(option, dict) and _as_text(option.get("value")) == value:
            return _as_text(option.get("label")) or value
    return value


def _render_field_line(
    field_def: dict[str, Any],
    data: dict[str, Any],
    character_name: str,
) -> str:
    """Render a single non-empty field into a Markdown bullet line."""
    field_type = field_def["type"]
    label = field_def["label"]
    raw_value = data.get(field_def["key"])

    if field_type in ("tags", "list"):
        items = _as_list(raw_value)
        if not items:
            return ""
        return f"- {label}：{'、'.join(items)}"

    if field_type == "dialogs":
        dialogs = _as_dialogs(raw_value)
        if not dialogs:
            return ""
        speaker = character_name or "角色"
        lines = ["- 示例对话："]
        for dialog in dialogs:
            if dialog["user"]:
                lines.append(f"  - 用户：{dialog['user']}")
            if dialog["character"]:
                lines.append(f"  - {speaker}：{dialog['character']}")
        return "\n".join(lines)

    text = _as_text(raw_value)
    if not text:
        return ""
    if field_type == "select":
        text = _select_label(field_def, text)
    if "\n" in text:
        text = text.replace("\n", "\n  ")
    return f"- {label}：{text}"


def _render_section(
    section: dict[str, Any],
    card: dict[str, Any],
    character_name: str,
) -> str:
    """Render one section as a Markdown block (empty sections return ``""``)."""
    data = card.get(section["id"])
    if not isinstance(data, dict):
        return ""
    lines: list[str] = []
    for field_def in section["fields"]:
        # native_persona_prompt is the import source only: never rendered.
        if field_def["key"] == "native_persona_prompt":
            continue
        line = _render_field_line(field_def, data, character_name)
        if line:
            lines.append(line)
    if not lines:
        return ""
    return f"## {section['title']}\n" + "\n".join(lines)


def _assemble_prompt(
    header: str,
    blocks: dict[str, str],
    kept: set[str],
) -> str:
    ordered = [blocks[sid] for sid in CARD_SECTION_IDS if sid in kept and sid in blocks]
    if not ordered:
        return header
    return header + "\n\n" + "\n\n".join(ordered)


def render_persona_prompt(card: Optional[dict[str, Any]], max_chars: int = 2000) -> str:
    """Render the character card as the persona prompt replacing AstrBot's Persona block.

    Rules (CONTRACTS.md §3.1):
    - Only non-empty fields are rendered, as Markdown sections.
    - First line is always ``你正在扮演「{name}」。``.
    - ``native_persona_prompt`` never participates in rendering.
    - ``identity/speech/personality/social`` are kept first; when over budget the
      sections are dropped in the order ``examples > background > appearance >
      extra/rhythm > interests``.
    - Hard guarantee: ``len(result) <= max_chars``.
    """
    try:
        budget = int(max_chars)
    except (TypeError, ValueError):
        budget = 2000
    if budget <= 0:
        return ""

    normalized = _coerce_card(card)
    identity = normalized.get("identity") or {}
    character_name = (
        _as_text(identity.get("name"))
        or _as_text(identity.get("nickname"))
        or "一个真实网友"
    )
    header = f"你正在扮演「{character_name}」。"

    blocks: dict[str, str] = {}
    for section in CARD_SCHEMA:
        block = _render_section(section, normalized, character_name)
        if block:
            blocks[section["id"]] = block

    kept: set[str] = set(blocks.keys())
    result = _assemble_prompt(header, blocks, kept)

    # 1) Drop low-priority sections in the contract's order.
    for section_id in _DROP_ORDER:
        if len(result) <= budget:
            break
        if section_id in kept:
            kept.discard(section_id)
            result = _assemble_prompt(header, blocks, kept)

    # 2) Extremely long "core" cards: progressively drop the lower-priority core
    #    sections too. `identity` is always kept; a pathological remaining overflow
    #    is handled by the final hard cut below.
    for section_id in _BASE_DROP_ORDER:
        if len(result) <= budget:
            break
        if section_id in kept:
            kept.discard(section_id)
            result = _assemble_prompt(header, blocks, kept)

    # 3) Final hard guarantee (e.g. absurdly long name or a tiny max_chars).
    if len(result) > budget:
        result = result[:budget]
    return result


def render_card_summary(card: Optional[dict[str, Any]], max_chars: int = 200) -> str:
    """Render a compact ``{soul_summary}`` snippet: 定位 + 性格标签 + 说话方式 + 回复长度.

    All four components are kept whenever possible by trimming the longest textual
    component first (the reply-length label is never trimmed first), and the result
    is hard-capped at ``max_chars``.
    """
    try:
        budget = int(max_chars)
    except (TypeError, ValueError):
        budget = 200
    if budget <= 0:
        return ""

    normalized = _coerce_card(card)
    identity = normalized.get("identity") or {}
    personality = normalized.get("personality") or {}
    speech = normalized.get("speech") or {}

    tagline = _as_text(identity.get("tagline"))
    tags_text = "、".join(_as_list(personality.get("personality_tags"))[:6])
    speaking_style = _as_text(speech.get("speaking_style"))
    reply_length_raw = _as_text(speech.get("reply_length"))
    reply_length = ""
    if reply_length_raw:
        reply_length = _select_label(_FIELD_BY_KEY["speech"]["reply_length"], reply_length_raw)

    entries: list[list[str]] = []  # [label, text]
    if tagline:
        entries.append(["定位", tagline])
    if tags_text:
        entries.append(["性格", tags_text])
    if speaking_style:
        entries.append(["说话方式", speaking_style])
    if reply_length:
        entries.append(["回复长度", reply_length])

    def _build() -> str:
        return "；".join(f"{label}：{text}" for label, text in entries if text)

    summary = _build()
    # Trim the longest trimmable component first so every part stays represented.
    #
    # Termination: ``text[:keep] + "…"`` is strictly shorter than ``text`` as long
    # as ``keep <= len(text) - 2`` (the ellipsis replaces fewer characters than are
    # cut), and the candidate filter stops once every component is <= 6 chars.
    # The iteration cap is a belt-and-braces guard only; the final ``summary[:budget]``
    # cut below always enforces the hard cap, so even a pathological budget returns.
    max_iterations = max(16, len(entries) * 32)
    for _ in range(max_iterations):
        if len(summary) <= budget:
            break
        candidates = [
            (index, len(item[1]))
            for index, item in enumerate(entries)
            if item[0] != "回复长度" and len(item[1]) > 6
        ]
        if not candidates:
            break
        index = max(candidates, key=lambda pair: pair[1])[0]
        label, text = entries[index]
        # Guarantee strict length reduction: len(text[:keep] + "…") == keep + 1
        # must be < len(text), i.e. keep <= len(text) - 2.
        keep = min(len(text) - 2, max(6, len(text) // 2))
        if keep < 1:
            break
        entries[index] = [label, text[:keep] + "…"]
        summary = _build()

    # 2) Tiny budgets: when the trimmed components still do not fit, drop whole
    #    low-priority components (trailing ones first) so the reserved reply-length
    #    label survives as long as possible. Dropping at most ``len(entries)``
    #    components keeps this loop finite; the final slice below still guarantees
    #    the hard cap for budgets smaller than a single remaining component.
    for index in range(len(entries) - 1, -1, -1):
        if len(summary) <= budget:
            break
        label, text = entries[index]
        if label == "回复长度" or not text:
            continue
        has_reply_length = any(
            item[0] == "回复长度" and item[1] for item in entries
        )
        remaining_non_reply = [
            item for item in entries if item[0] != "回复长度" and item[1]
        ]
        # Keep at least one component when there is no reply-length label to reserve,
        # so a tiny budget still yields a meaningful prefix instead of "".
        if not has_reply_length and len(remaining_non_reply) <= 1:
            break
        entries[index] = [label, ""]
        summary = _build()

    if not summary:
        fallback_name = _as_text(identity.get("name")) or _as_text(identity.get("nickname"))
        summary = f"角色名：{fallback_name}" if fallback_name else ""
    if len(summary) > budget:
        summary = summary[:budget]
    return summary
