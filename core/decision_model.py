"""OpenRouter Decisions API client (Jev-style System-One judgment models).

The plugin's **decision** role can run in two modes:

* ``chat``  — an ordinary chat model receives the read-the-air prompt and returns
  JSON (``{"decision": "REPLY", "confidence": 0.8, "reason": "..."}``);
* ``judge`` — a typed decision model (``typesafe/jev-1.14`` and the other
  OpenRouter Decisions models) receives a ``state`` object plus a ``choice``
  question and returns probabilities, not generated text.

This module contains only the transport for the second mode. It never exposes
API keys; callers resolve the selected provider source's key themselves.
"""

from typing import Any, Dict, Optional

try:
    import httpx
except ImportError:  # pragma: no cover - offline test environment
    httpx = None


DEFAULT_DECISIONS_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_TIMEOUT_SECONDS = 45.0


class DecisionModelError(Exception):
    """Raised when a Decisions API call cannot be performed or parsed."""


async def post_json(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """POST one JSON payload and return the decoded response object."""
    if httpx is None:
        raise DecisionModelError("httpx 不可用，无法调用判断模型")
    try:
        try:
            client = httpx.AsyncClient(timeout=timeout)
        except Exception:
            # Broken proxy envs (e.g. ``NO_PROXY`` containing bracketed IPv6)
            # make httpx refuse to build the client; fall back to a clean one.
            client = httpx.AsyncClient(timeout=timeout, trust_env=False)
        async with client:
            response = await client.post(str(url), headers=dict(headers), json=payload)
    except Exception as exc:  # noqa: BLE001 - network failures degrade to fallback
        raise DecisionModelError(f"判断模型请求失败: {exc}") from exc

    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        detail = ""
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            body = None
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or error)
            elif error:
                detail = str(error)
            if not detail:
                detail = str(body)[:300]
        if not detail:
            detail = str(getattr(response, "text", "") or "")[:300]
        raise DecisionModelError(f"判断模型 HTTP {status}: {detail}")

    try:
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        raise DecisionModelError("判断模型返回内容不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise DecisionModelError("判断模型返回格式异常")
    return data


def _probability_for(probabilities: Any, choice: str) -> Optional[float]:
    if not isinstance(probabilities, dict):
        return None
    wanted = str(choice or "").strip().upper()
    for key, value in probabilities.items():
        if str(key or "").strip().upper() != wanted:
            continue
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return None
    return None


async def judge_choice(
    *,
    endpoint: str,
    api_key: str,
    model: str,
    state: Dict[str, Any],
    instructions: str,
    criteria: Dict[str, str],
    question_key: str = "decision",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Ask one ``choice`` question and normalize the typed answer.

    Returns ``{decision, confidence, probabilities, model, provider, usage, mode}``
    where ``confidence`` is the probability of the selected option (not the
    provider's distribution-concentration ``confidence`` field).
    """
    key = str(question_key or "decision").strip() or "decision"
    payload: Dict[str, Any] = {
        "model": str(model or "").strip(),
        "state": state if isinstance(state, dict) else {},
        "questions": {
            key: {
                "type": "choice",
                "instructions": str(instructions or "").strip(),
                "criteria": {
                    str(option): str(text)
                    for option, text in (criteria or {}).items()
                    if str(option).strip()
                },
            }
        },
    }
    headers = {
        "Authorization": f"Bearer {str(api_key or '').strip()}",
        "Content-Type": "application/json",
    }
    data = await post_json(endpoint, headers, payload, timeout=timeout)
    answers = data.get("answers")
    answer = answers.get(key) if isinstance(answers, dict) else None
    if not isinstance(answer, dict):
        raise DecisionModelError("判断模型未返回 answers")

    choice = str(answer.get("choice") or "").strip()
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict):
        probabilities = {}
    confidence = _probability_for(probabilities, choice)
    if confidence is None:
        try:
            confidence = float(answer.get("confidence"))
        except (TypeError, ValueError):
            confidence = None
    usage = data.get("usage")
    return {
        "decision": choice.upper(),
        "confidence": confidence,
        "probabilities": {
            str(option): value for option, value in probabilities.items()
        },
        "model": str(data.get("model") or model or ""),
        "provider": str(data.get("provider") or ""),
        "usage": usage if isinstance(usage, dict) else {},
        "mode": "judge",
    }
