"""慢通道输出契约：枚举单一事实源 + 强校验 + 安全兜底。

设计约束（与文档 D9 一致）：
  - 任何异常输入都必须产出合法结果，绝不抛给调用方
  - 无法判定一律 unknown / upright=None，绝不猜测
  - 不依赖 jsonschema 等第三方库（服务器只装了 requests）
"""
from __future__ import annotations

import json
from typing import Any, Optional

# ---------------------------------------------------------------- 枚举（唯一事实源）
VALID_STATES = ("upright", "slouch", "lean", "phone", "daze", "away", "sleep", "unknown")

# 允许翻案吹风的状态（sleep/away 只记录，见阶段二文档 §8.4）
ALERTABLE_STATES = frozenset({"slouch", "lean", "phone", "daze"})

# 模型爱犯的错 → 项目枚举
_STATE_ALIASES = {
    "writing": "upright", "listening": "upright", "normal": "upright",
    "straight": "upright", "sitting": "upright", "sitting_upright": "upright",
    "head_down": "slouch", "distracted_head_down": "slouch", "hunch": "slouch",
    "hunched": "slouch", "slouching": "slouch", "bowed": "slouch",
    "leaning": "lean", "tilted": "lean", "side_lean": "lean", "leaning_side": "lean",
    "phone_use": "phone", "using_phone": "phone", "playing_phone": "phone",
    "smartphone": "phone", "mobile": "phone",
    "distracted": "daze", "zoning_out": "daze", "daydreaming": "daze",
    "absent": "away", "absent_seat": "away", "left": "away", "empty": "away",
    "sleeping": "sleep", "sleepy": "sleep", "drowsy": "sleep", "lying": "sleep",
    "uncertain": "unknown", "none": "unknown", "": "unknown",
}

# 字段别名（模型偶尔缩写/复数）
_FIELD_ALIASES = {
    "states": "state", "posture": "state", "status": "state", "label": "state",
    "conf": "confidence", "score": "confidence", "prob": "confidence",
    "detail": "evidence", "explain": "evidence",
    "review": "needs_review", "need_human_review": "needs_review",
    "human_review": "needs_review",
    "alert": "should_alert", "need_alert": "should_alert", "warn": "should_alert",
}

SAFE_DEFAULT = {
    "state": "unknown", "upright": None, "confidence": 0.0,
    "evidence": "", "needs_review": True, "should_alert": False,
    "ok": False, "error": "",
}


def norm_state(value: Any) -> str:
    """任意输入 → 合法枚举。多标签取最严重的一个。"""
    if isinstance(value, (list, tuple, set)):
        picked = {norm_state(v) for v in value}
        for s in ("sleep", "away", "phone", "daze", "slouch", "lean", "upright"):
            if s in picked:
                return s
        return "unknown"
    if value is None:
        return "unknown"
    s = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if s in VALID_STATES:
        return s
    return _STATE_ALIASES.get(s, "unknown")


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("true", "yes", "y", "1", "是"):
        return True
    if s in ("false", "no", "n", "0", "否"):
        return False
    return None    # "null"/"unknown"/乱码 一律视为无法判定


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def extract_json(text: Any) -> Optional[dict]:
    """从模型返回（可能带 markdown 包裹/前后废话）中抠出 JSON 对象。"""
    if isinstance(text, dict):
        return text
    if not isinstance(text, str) or not text.strip():
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s[:4].lower() == "json":
            s = s[4:]
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b == -1 or b <= a:
        return None
    try:
        obj = json.loads(s[a:b + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def normalize(raw: Any) -> dict:
    """清洗 + 校验 + 归一化。任何情况都返回合法 dict，绝不抛异常。"""
    obj = extract_json(raw)
    if not isinstance(obj, dict):
        return dict(SAFE_DEFAULT, error="no_json")

    # 字段别名（先收集，避免覆盖已有正名）
    for k, v in list(obj.items()):
        if not isinstance(k, str):
            continue
        alias = _FIELD_ALIASES.get(k.strip().lower())
        if alias and alias not in obj:
            obj[alias] = v

    state = norm_state(obj.get("state"))
    upright = _as_bool(obj.get("upright"))
    if upright is None and state != "unknown":
        # 模型只给了 state 没给 upright → 用状态集补，而不是猜
        if state == "upright":
            upright = True
        elif state in ALERTABLE_STATES or state in ("away", "sleep"):
            upright = False

    confidence = max(0.0, min(1.0, _as_float(obj.get("confidence"), 0.0)))
    alert = _as_bool(obj.get("should_alert"))
    if alert is None:
        alert = False          # 缺省保守：不提醒
    review = _as_bool(obj.get("needs_review")) or False

    evidence = str(obj.get("evidence") or "").strip()[:60]

    return {
        "state": state, "upright": upright, "confidence": round(confidence, 2),
        "evidence": evidence, "needs_review": bool(review),
        "should_alert": bool(alert), "ok": True, "error": "",
    }


def should_alert(state: str, upright: Optional[bool], confidence: float,
                 model_alert: bool, min_conf: float = 0.6) -> bool:
    """最终"要不要翻案吹风"的唯一判定点。

    规则（宁漏报不误报）：
      - 模型明确说不提醒 → 不提醒（尊重模型的自保判断）
      - 状态不在可提醒集合（unknown/away/sleep/upright）→ 不提醒
      - upright 不是 False → 不提醒
      - 置信度不足 → 不提醒
    """
    if not model_alert:
        return False
    if state not in ALERTABLE_STATES:
        return False
    if upright is not False:
        return False
    return confidence >= min_conf
