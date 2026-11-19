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
    # 行为判定（慢速通道）
    decision: Optional[BehaviorDecision] = None
    # 说明
    reason: str = ""


class MultimodalDetector:
    """
    多态检测编排器。

    线程安全：慢速通道在后台线程执行，避免阻塞 FastAPI 请求。
    主请求线程只读取最近一次慢速结果（可能为 None），保证 HTTP 一问一答低延迟。
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
    ):
        self.enabled = enabled
        self.interval_seconds = interval_seconds

        self.qwen = QwenVLProvider(api_key=qwen_api_key, model=qwen_model)
        self.deepseek = DeepSeekProvider(api_key=deepseek_api_key, model=deepseek_model)

        self._history: deque[str] = deque(maxlen=history_size)
        self._last_run_ts = 0.0
        self._last_result: Optional[MultimodalResult] = None
        self._lock = threading.Lock()
        self._running = False

    @property
    def ready(self) -> bool:
        """两个 AI 是否都已配置。"""
        return self.enabled and self.qwen.enabled and self.deepseek.enabled

    def should_run(self) -> bool:
        """是否到达慢速通道的抽查周期。"""
        if not self.ready:
            return False
        return (time.time() - self._last_run_ts) >= self.interval_seconds

    def run_async(self, image_bytes: bytes) -> None:
        """在后台线程触发一次多态检测（非阻塞）。"""
        if not self.should_run():
            return
        if self._running:
            return
        self._last_run_ts = time.time()
        self._running = True
        t = threading.Thread(target=self._run, args=(image_bytes,), daemon=True)
        t.start()

    def _run(self, image_bytes: bytes) -> None:
        """慢速通道：AI-1 看图 → AI-2 决策。"""
        try:
            # ---- AI-1：图像获取 / 视觉分析 ----
            visual = self.qwen.analyze(image_bytes)
            if not visual.ok:
                log.warning("AI-1 (qwen-vl) failed: %s", visual.error)
                return

            # ---- AI-2：行为分析 / 决策 ----
            decision = self.deepseek.decide(visual, list(self._history))
            if not decision.ok:
                log.warning("AI-2 (deepseek) failed: %s", decision.error)
                return

            self._history.append(decision.state)

            result = MultimodalResult(
                upright=decision.upright,
                state=decision.state,
                should_alert=decision.should_alert,
                source="slow",
                visual=visual,
                decision=decision,
                reason=decision.reason,
            )
            with self._lock:
                self._last_result = result

            log.info(
                "multimodal: state=%s upright=%s alert=%s conf=%.2f reason=%s",
                decision.state, decision.upright, decision.should_alert,
                decision.confidence, decision.reason,
            )
        except Exception:  # noqa: BLE001
            log.exception("multimodal run failed")
        finally:
            self._running = False

    def latest(self) -> Optional[MultimodalResult]:
        """读取最近一次慢速通道结果（可能为 None）。"""
        with self._lock:
            return self._last_result

    def fuse(
        self,
        fast_upright: Optional[bool],
        fast_reason: str = "",
    ) -> MultimodalResult:
        """
        融合快速通道与慢速通道结果。

        规则：
          - 快速通道明确不端正 → 不端正（阶段一核心，优先）
          - 快速通道无法判定，但慢速通道明确不端正 → 不端正
          - 两者都端正 → 端正
          - 其余 → 无法判定
        """
        slow = self.latest()

        if fast_upright is False:
            return MultimodalResult(
                upright=False, state="slouch", should_alert=True,
                source="fast", reason=fast_reason or "fast_path_bad",
            )

        if slow is not None and slow.upright is False:
            return MultimodalResult(
                upright=False, state=slow.state, should_alert=slow.should_alert,
                source="slow", visual=slow.visual, decision=slow.decision,
                reason=slow.reason or "slow_path_bad",
            )

        if fast_upright is True:
            return MultimodalResult(
                upright=True, state="upright", should_alert=False,
                source="fast", reason=fast_reason or "fast_path_ok",
            )

        if slow is not None and slow.upright is True:
            return MultimodalResult(
                upright=True, state=slow.state, should_alert=False,
                source="slow", visual=slow.visual, decision=slow.decision,
                reason=slow.reason or "slow_path_ok",
            )

        return MultimodalResult(
            upright=None, state="unknown", should_alert=False,
            source="fused", reason="cannot_determine",
        )
