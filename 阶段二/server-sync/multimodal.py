"""
多态检测 · 编排器

将"多个 AI API 分开工作"编排为一条清晰流水线：

    ESP32 JPEG
        │
        ├─▶ [快速通道] 本地 YOLOv8-pose  → 关键点 → 规则引擎（阶段一核心，每帧）
        │
        └─▶ [慢速通道] 多态检测（可开关，每 N 秒抽查一次）
                 │
                 ├─ AI-1  Qwen-VL   : 图像获取 / 视觉分析 → VisualDescription
                 │
                 └─ AI-2  DeepSeek  : 行为分析 / 决策     → BehaviorDecision

融合策略（与文档 D5/D9 一致）：
  - 快速通道负责低延迟的坐姿判定与"连续 2 帧"触发逻辑
  - 慢速通道负责语义级多态判定（玩手机/发呆/离开等阶段二能力）
  - 两者任一判定"不端正"且满足去重条件 → blow
  - 任一通道"无法判定" → 不触发（宁漏报不误报）
  - 慢速通道失败/超时 → 静默降级，仅用快速通道结果
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from ai_providers import (
    BehaviorDecision,
    DeepSeekProvider,
    QwenVLProvider,
    VisualDescription,
)
from schema import should_alert as schema_should_alert

log = logging.getLogger("posture.multimodal")


@dataclass
class MultimodalResult:
    """一次多态检测的融合结果。"""

    # 最终是否端正（None=无法判定）
    upright: Optional[bool] = None
    # 多态状态
    state: str = "unknown"
    # 是否建议提醒
    should_alert: bool = False
    # 来源：fast / slow / fused
    source: str = "fast"
    # 视觉描述（慢速通道）
    visual: Optional[VisualDescription] = None
    # 行为判定（慢速通道，两段式方案才会填）
    decision: Optional[BehaviorDecision] = None
    # 说明
    reason: str = ""
    # 写入缓存的时间戳，供 latest() 判断是否过期
    timestamp: float = 0.0


class MultimodalDetector:
    """
    多态检测编排器。

    线程安全：慢速通道在后台线程执行，避免阻塞 FastAPI 请求。
    主请求线程只读取最近一次慢速结果（可能为 None），保证 HTTP 一问一答低延迟。

    两种工作模式：
      · single_call=True（默认，选型报告推荐）：一次模型调用直出 state/upright，
        不用 AI-2。延迟最低、计费最省。
      · single_call=False：沿用「AI-1 看图 → AI-2 决策」两段式，保留跨帧历史推理能力。
    """

    def __init__(
        self,
        enabled: bool = False,
        qwen_api_key: str = "",
        qwen_model: str = "qwen-vl-plus",
        deepseek_api_key: str = "",
        deepseek_model: str = "deepseek-chat",
        interval_seconds: int = 30,
        history_size: int = 10,
        # ---- 慢通道加固参数（交付包方案）----
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        temperature: float = 0.1,
        max_tokens: int = 800,
        result_ttl_seconds: int = 90,
        min_confidence: float = 0.6,
        max_tries: int = 2,
        single_call: bool = True,
    ):
        self.enabled = enabled
        self.interval_seconds = interval_seconds
        self.single_call = single_call
        # 缓存有效期：过期即视为"没有慢通道结果"，回退快速通道。
        # 必须 >= interval，否则每次抽查前缓存就已失效；建议 3 倍。
        self.result_ttl = result_ttl_seconds
        # 翻案吹风的最低置信度
        self.min_confidence = min_confidence
        # 单次抽查内的重试次数。30s 周期下不能用 3 次，会把后台线程占死。
        self.max_tries = max_tries

        self.qwen = QwenVLProvider(
            api_key=qwen_api_key,
            model=qwen_model,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self.deepseek = DeepSeekProvider(api_key=deepseek_api_key, model=deepseek_model)

        self._history: deque[str] = deque(maxlen=history_size)
        self._last_run_ts = 0.0
        self._last_result: Optional[MultimodalResult] = None
        self._lock = threading.Lock()
        self._running = False

    @property
    def ready(self) -> bool:
        """慢通道是否可用。单次调用方案只需要一个 Key。"""
        if not self.enabled:
            return False
        if self.single_call:
            return self.qwen.enabled
        return self.qwen.enabled and self.deepseek.enabled

    def should_run(self) -> bool:
        """是否到达慢速通道的抽查周期。"""
        if not self.ready:
            return False
        return (time.time() - self._last_run_ts) >= self.interval_seconds

    def run_async(self, image_bytes: bytes) -> None:
        """在后台线程触发一次多态检测（非阻塞）。

        铁律：模型调用永不出现在 /detect 请求路径上。
        固件 HTTP_TIMEOUT_MS=8000，而模型单次要 3~40s，同步等必然超时。
        """
        if not self.should_run():
            return
        if self._running:
            return
        self._last_run_ts = time.time()
        self._running = True
        t = threading.Thread(target=self._run, args=(image_bytes,), daemon=True)
        t.start()

    def _run(self, image_bytes: bytes) -> None:
        """慢速通道主体。失败时不写缓存，让旧结果自然过期（不回退到旧数据）。"""
        try:
            if self.single_call:
                self._run_single(image_bytes)
            else:
                self._run_two_stage(image_bytes)
        except Exception:  # noqa: BLE001
            log.exception("multimodal run failed")
        finally:
            self._running = False

    def _run_single(self, image_bytes: bytes) -> None:
        """单次调用：一次模型请求直出判定。"""
        last_err = ""
        visual = None
        for attempt in range(self.max_tries):
            visual = self.qwen.analyze(image_bytes)
            if visual.ok:
                break
            last_err = visual.error
            log.warning("slow-path attempt %d/%d failed: %s",
                        attempt + 1, self.max_tries, last_err)
        if visual is None or not visual.ok:
            return  # 全部失败：不写缓存

        # 最终"要不要翻案"的唯一判定点（置信度、状态集、模型自保都在这里收口）
        alert = schema_should_alert(
            visual.state, visual.upright, visual.confidence,
            visual.should_alert, self.min_confidence,
        )

        result = MultimodalResult(
            upright=visual.upright,
            state=visual.state,
            should_alert=alert,
            source="slow",
            visual=visual,
            reason=visual.posture_hint or visual.state,
            timestamp=time.time(),
        )
        with self._lock:
            self._last_result = result
        self._history.append(visual.state)

        log.info(
            "multimodal: state=%s upright=%s alert=%s conf=%.2f review=%s evidence=%s",
            visual.state, visual.upright, alert, visual.confidence,
            visual.needs_review, visual.posture_hint or "-",
        )

    def _run_two_stage(self, image_bytes: bytes) -> None:
        """两段式：AI-1 看图 → AI-2 决策（保留跨帧历史推理能力）。"""
        visual = None
        for attempt in range(self.max_tries):
            visual = self.qwen.analyze(image_bytes)
            if visual.ok:
                break
            log.warning("AI-1 attempt %d/%d failed: %s",
                        attempt + 1, self.max_tries, visual.error)
        if visual is None or not visual.ok:
            return

        decision = self.deepseek.decide(visual, list(self._history))
        if not decision.ok:
            log.warning("AI-2 (deepseek) failed: %s", decision.error)
            return

        self._history.append(decision.state)

        result = MultimodalResult(
            upright=decision.upright,
            state=decision.state,
            should_alert=schema_should_alert(
                decision.state, decision.upright, decision.confidence,
                decision.should_alert, self.min_confidence,
            ),
            source="slow",
            visual=visual,
            decision=decision,
            reason=decision.reason,
            timestamp=time.time(),
        )
        with self._lock:
            self._last_result = result

        log.info(
            "multimodal: state=%s upright=%s alert=%s conf=%.2f reason=%s",
            decision.state, decision.upright, result.should_alert,
            decision.confidence, decision.reason,
        )

    def latest(self) -> Optional[MultimodalResult]:
        """读取最近一次慢速通道结果；过期视为"没有结果"。

        TTL 是必需的：没有它，一次误判会永久参与融合，导致
        "坐得再直也一直被翻案吹风"（原实现的真实缺陷）。
        """
        with self._lock:
            r = self._last_result
        if r is None:
            return None
        age = time.time() - getattr(r, "timestamp", 0.0)
        if age > self.result_ttl:
            return None
        return r

    def fuse(
        self,
        fast_upright: Optional[bool],
        fast_reason: str = "",
    ) -> MultimodalResult:
        """
        融合快速通道与慢速通道结果。

        规则（宁漏报不误报）：
          - 快速通道明确不端正 → 不端正（阶段一核心，优先）
          - 快速通道无法判定，但慢速通道明确不端正**且它自己认为该提醒** → 不端正
          - 两者都端正 → 端正
          - 其余 → 无法判定

        关键改动：慢通道翻案必须同时满足 should_alert=True。
        模型判了 slouch 但明确 should_alert=false（"属于正常学习低头"）时不再翻案，
        否则会把"低头看书"当成驼背吹风——这是交付包评审指出的最大真实缺陷。
        """
        slow = self.latest()

        if fast_upright is False:
            return MultimodalResult(
                upright=False, state="slouch", should_alert=True,
                source="fast", reason=fast_reason or "fast_path_bad",
            )

        if slow is not None and slow.upright is False and slow.should_alert:
            return MultimodalResult(
                upright=False, state=slow.state, should_alert=True,
                source="slow", visual=slow.visual, decision=slow.decision,
                reason=slow.reason or "slow_path_bad",
                timestamp=slow.timestamp,
            )

        if fast_upright is True:
            return MultimodalResult(
                upright=True, state="upright", should_alert=False,
                source="fast", reason=fast_reason or "fast_path_ok",
            )

        if slow is not None and slow.upright is False:
            # 慢通道看到不端正，但它判断"不该提醒"（如正常低头看书）
            # → 快速通道也无法判定时，保守给"无法判定"，不触发
            return MultimodalResult(
                upright=None, state=slow.state, should_alert=False,
                source="slow", visual=slow.visual, decision=slow.decision,
                reason=f"slow_no_alert:{slow.reason}",
                timestamp=slow.timestamp,
            )

        if slow is not None and slow.upright is True:
            return MultimodalResult(
                upright=True, state=slow.state, should_alert=False,
                source="slow", visual=slow.visual, decision=slow.decision,
                reason=slow.reason or "slow_path_ok",
                timestamp=slow.timestamp,
            )

        return MultimodalResult(
            upright=None, state="unknown", should_alert=False,
            source="fused", reason="cannot_determine",
        )
