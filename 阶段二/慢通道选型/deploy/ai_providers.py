"""
多态检测 · AI 提供方封装

本模块封装两个独立的外部 AI API，各司其职：

  - QwenVLProvider  : 图像获取 / 视觉分析（看图，输出结构化视觉描述）
  - DeepSeekProvider: 行为分析 / 决策（读描述，输出行为判定与 action）

设计原则（与《阶段一实施方案》保持一致）：
  - 阶段一核心仍是本地 YOLOv8-pose 快速通道，本模块为可选增强层
  - 对应文档 D11：Qwen 相关能力安排在阶段二，此处以"可开关"方式接入，
    默认关闭（MULTIMODAL_ENABLED=false），不影响阶段一验收
  - 对应文档 D9：暗光/无法判定时宁漏报不误报，AI 调用失败一律降级为
    "无法判定"，绝不误触发提醒
  - 对应文档 D10：HTTP 一问一答，无 MQTT、无数据库

两个 Provider 均只依赖标准库 + requests，避免引入重型 SDK。
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

log = logging.getLogger("posture.ai")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class VisualDescription:
    """慢通道单次调用的结构化结果。

    单次调用方案下，同一次请求既产出判定字段（state/upright/confidence/
    should_alert），也产出描述字段（scene/posture_hint/object_hint）。
    描述字段只用于日志与人工复核，不参与触发判定。
    """

    person_present: bool = False
    person_count: int = 0
    # 画面语义描述（自然语言，供人工复核）
    scene: str = ""
    # 姿态相关线索（自然语言）
    posture_hint: str = ""
    # 是否手持/注视手机等物体线索
    object_hint: str = ""

    # ---- 判定字段（来自 schema.normalize，已强校验）----
    state: str = "unknown"
    upright: Optional[bool] = None
    confidence: float = 0.0
    should_alert: bool = False
    needs_review: bool = True

    # 原始返回文本（调试用）
    raw: str = ""
    ok: bool = False
    error: str = ""


@dataclass
class BehaviorDecision:
    """AI-2（DeepSeek）输出的行为判定。"""

    # 多态状态：upright / slouch / lean / phone / daze / away / sleep / unknown
    state: str = "unknown"
    # 是否端正（None 表示无法判定）
    upright: Optional[bool] = None
    # 是否应触发提醒
    should_alert: bool = False
    # 置信度 0~1
    confidence: float = 0.0
    # 判定理由
    reason: str = ""
    raw: str = ""
    ok: bool = False
    error: str = ""


# 多态状态枚举：唯一事实源在 schema.py，此处只做再导出。
# 不要在本文件另建一份枚举——两份枚举迟早漂移（验收清单第 1 条）。
from schema import (  # noqa: E402
    ALERTABLE_STATES,
    VALID_STATES,
    extract_json,
    normalize,
    should_alert,
)


# ---------------------------------------------------------------------------
# AI-1：Qwen-VL 图像获取 / 视觉分析
# ---------------------------------------------------------------------------

class QwenVLProvider:
    """
    通义千问视觉模型（Qwen-VL）客户端。

    职责：接收 JPEG 图像，输出结构化视觉描述。
    对应文档阶段二"发呆走神：人脸区域裁剪 + Qwen-VL 零样本抽查"。
    """

    def __init__(
        self,
        api_key: str,
        model: str = "qwen-vl-plus",
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        timeout: int = 12,
        temperature: float = 0.1,
        max_tokens: int = 800,
        pool=None,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # 传入 VisionPool 时，重试/切后端/熔断/坏例落盘全部由池子负责，
        # 本类退化为"拼 payload + 解析结果"。
        self.pool = pool
        # 温度必须按模型可配：实测 kimi-k3 只接受 1，传 0 直接 400
        self.temperature = temperature
        # 输出上限必须足够：实测推理型模型需 >=2000，256 会让它吐空串
        self.max_tokens = max_tokens

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def analyze(self, image_bytes: bytes, hint: str = "") -> VisualDescription:
        """单次调用：一次请求同时产出判定字段和描述字段。

        相比原「AI-1 看图 + AI-2 决策」两段式，单次调用把两次网络往返压成一次，
        延迟从 ~30s 降到 ~8s，且少一次计费（选型报告 §6.2 第 2 条）。
        """
        if not self.enabled:
            return VisualDescription(ok=False, error="api key not set")

        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:image/jpeg;base64,{b64}"

        prompt = (
            "你是学习状态监测系统的慢通道判定模块。摄像头装在书桌上方，画面是一个学生。"
            "只输出一个 JSON 对象，不要输出 markdown、解释或任何多余文字。"
            "字段与取值必须严格遵守：\n"
            "{\n"
            '  "state": "upright|slouch|lean|phone|daze|away|sleep|unknown",\n'
            '  "upright": true/false/null,\n'
            '  "confidence": 0.0~1.0,\n'
            '  "person_count": 整数,\n'
            '  "scene": "一句话画面内容，不超过20字",\n'
            '  "posture_hint": "头部与肩膀关系，不超过20字",\n'
            '  "object_hint": "手机/书本等物体及注视关系，不超过20字",\n'
            '  "evidence": "判定依据，不超过20字",\n'
            '  "should_alert": true/false,\n'
            '  "needs_review": true/false\n'
            "}\n"
            "判定规则：\n"
            "- 坐直=upright；低头/驼背=slouch；侧歪=lean\n"
            "- 玩手机=phone；发呆走神=daze；画面没人=away；趴桌=sleep\n"
            "- 画面过暗、遮挡、看不清 → state=unknown, upright=null, needs_review=true\n"
            "- 宁漏报不误报：不确定时一律 should_alert=false\n"
            "- 只有明确、持续的不端正才 should_alert=true"
        )
        # 快速通道给出的补充线索（如"检测到人体但头部不可见"）。
        # 弱证据帧（趴桌时看不到鼻子）靠这句能把 away 纠正成 sleep/slouch。
        if hint:
            prompt += "\n- 补充线索：" + hint

        # ---- 走了韧性池：重试/切后端/熔断/坏例落盘都在池子里 ----
        if self.pool is not None:
            r = self.pool.analyze(image_bytes, prompt)
            if not r["ok"]:
                log.warning("slow-path pool failed after %d attempt(s): %s",
                            r["attempts"], r["error"])
                return VisualDescription(ok=False, error=r["error"])
            log.info("slow-path ok backend=%s model=%s tries=%d latency=%ss",
                     r["backend"], r["model"], r["attempts"], r["latency"])
            return self._parse(r["content"])

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"] or ""
            if not content.strip():
                # 推理型模型可能吃光 token 返回空串，明确标记为失败以便重试
                return VisualDescription(ok=False, raw="", error="empty content")
            return self._parse(content)
        except Exception as e:  # noqa: BLE001
            log.warning("slow-path model call failed: %s", e)
            return VisualDescription(ok=False, error=str(e))

    @staticmethod
    def _parse(content: str) -> VisualDescription:
        """用 schema.normalize 清洗 + 校验，任何垃圾输入都不会抛异常。"""
        n = normalize(content)

        # 描述字段不在 normalize 的返回里（它只保证判定字段合法），
        # 所以从同一段文本再取一次。这样不必改动 schema.py 这份契约源。
        obj = extract_json(content) or {}

        try:
            person_count = int(obj.get("person_count") or 0)
        except (TypeError, ValueError):
            person_count = 0

        return VisualDescription(
            person_present=(n["state"] != "away") and (n["upright"] is not None),
            person_count=person_count,
            scene=str(obj.get("scene") or "")[:40],
            posture_hint=str(obj.get("posture_hint") or "")[:40],
            object_hint=str(obj.get("object_hint") or "")[:40],
            state=n["state"],
            upright=n["upright"],
            confidence=n["confidence"],
            should_alert=n["should_alert"],
            needs_review=n["needs_review"],
            raw=content,
            ok=n["ok"],
            error=n["error"],
        )


# ---------------------------------------------------------------------------
# AI-2：DeepSeek 行为分析 / 决策
# ---------------------------------------------------------------------------

class DeepSeekProvider:
    """
    DeepSeek 文本模型客户端。

    职责：接收 AI-1 的视觉描述 + 历史状态，输出行为判定与 action。
    """

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com/v1",
        timeout: int = 30,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def decide(
        self,
        visual: VisualDescription,
        history: list[str],
        thresholds_hint: str = "",
    ) -> BehaviorDecision:
        """基于视觉描述与历史，输出行为判定。"""
        if not self.enabled:
            return BehaviorDecision(ok=False, error="deepseek api key not set")

        history_text = "、".join(history[-5:]) if history else "（无历史）"

        system_prompt = (
            "你是学习状态监测系统的行为分析模块。你会收到视觉模块对书桌前"
            "人像的结构化描述，以及最近几帧的状态历史。请判断当前行为状态，"
            "只输出一个 JSON 对象，不要输出多余文字。字段如下：\n"
            "{\n"
            '  "state": "upright|slouch|lean|phone|daze|away|sleep|unknown",\n'
            '  "upright": true/false/null,   // 是否坐姿端正，无法判定填 null\n'
            '  "should_alert": true/false,   // 是否应触发风扇提醒\n'
            '  "confidence": 0.0~1.0,\n'
            '  "reason": "一句话判定理由"\n'
            "}\n"
            "判定规则：\n"
            "- 阶段一核心只关注坐姿：低头/驼背=slouch，侧歪=lean，端正=upright\n"
            "- 玩手机=phone，发呆=daze，离开=away，趴桌=sleep（阶段二能力）\n"
            "- 画面过暗或看不清 → state=unknown, upright=null, should_alert=false\n"
            "- 宁漏报不误报：不确定时一律 should_alert=false\n"
            "- 只有明确、持续的不端正才 should_alert=true"
        )

        user_prompt = (
            f"【视觉描述】\n"
            f"有人: {visual.person_present}  人数: {visual.person_count}\n"
            f"场景: {visual.scene}\n"
            f"姿态线索: {visual.posture_hint}\n"
            f"物体线索: {visual.object_hint}\n\n"
            f"【最近状态历史】{history_text}\n"
            f"{thresholds_hint}"
        )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }

        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return self._parse(content)
        except Exception as e:  # noqa: BLE001
            log.warning("deepseek call failed: %s", e)
            return BehaviorDecision(ok=False, error=str(e))

    @staticmethod
    def _parse(content: str) -> BehaviorDecision:
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return BehaviorDecision(ok=False, raw=content, error="no json in response")

        try:
            obj = json.loads(text[start:end + 1])
        except json.JSONDecodeError as e:
            return BehaviorDecision(ok=False, raw=content, error=f"json decode: {e}")

        state = str(obj.get("state", "unknown"))
        if state not in VALID_STATES:
            state = "unknown"

        upright = obj.get("upright", None)
        if upright is not None:
            upright = bool(upright)

        return BehaviorDecision(
            state=state,
            upright=upright,
            should_alert=bool(obj.get("should_alert", False)),
            confidence=float(obj.get("confidence", 0.0) or 0.0),
            reason=str(obj.get("reason", "")),
            raw=content,
            ok=True,
        )
