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
    # 慢通道判定置信度（0~1）；fast 来源时为 0
    confidence: float = 0.0
    # 这帧的特征指纹 (head_height, shoulder_level)，用于判断"缓存是否还对应同一场景"
    frame_key: Optional[tuple] = None
    # 同一帧的 256bit 图像感知哈希。关键点有效时 frame_key 是几何元组、无效时是
    # ("img", hash)，**类型会在相邻帧之间翻转**；只靠 frame_key 会导致
    # "快通道一失效，慢通道的正确结论就永远匹配不上"（_same_scene 直接返回 False）。
    # 因此额外保存一份图像指纹，作为跨类型比较的兜底。
    img_hash: Optional[tuple] = None
    # 写入缓存的时间戳，供 latest() 判断是否过期
    timestamp: float = 0.0


# 几何能直接判的姿态状态：慢通道在这些状态上与几何冲突时，采信几何
POSTURAL_STATES = {"slouch", "lean", "sleep"}


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
        slow_override_min_conf: float = 0.9,
        # 慢通道否决权的护栏：快通道连续 N 帧判"不端正"时，不再允许慢通道翻案。
        # 0 = 关闭护栏（回到旧行为）。
        override_guard_frames: int = 3,
        max_tries: int = 2,
        single_call: bool = True,
        timeout: float = 12.0,
        pool=None,
    ):
        self.enabled = enabled
        self.interval_seconds = interval_seconds
        self.single_call = single_call
        # 缓存有效期：过期即视为"没有慢通道结果"，回退快速通道。
        # 必须 >= interval，否则每次抽查前缓存就已失效；建议 3 倍。
        self.result_ttl = result_ttl_seconds
        # 翻案吹风的最低置信度
        self.min_confidence = min_confidence
        # 慢通道"否决权"门槛：慢通道明确端正且置信度达到此值时，可以推翻快速通道的不端正
        self.slow_override_min_conf = slow_override_min_conf
        # 否决权护栏：快通道连续判坏达到该帧数时，禁止慢通道翻案
        self.override_guard_frames = max(0, int(override_guard_frames))
        self._fast_bad_streak = 0
        # 单次抽查内的重试次数。30s 周期下不能用 3 次，会把后台线程占死。
        self.max_tries = max_tries

        self.qwen = QwenVLProvider(
            api_key=qwen_api_key,
            model=qwen_model,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            pool=pool,
        )
        self.deepseek = DeepSeekProvider(api_key=deepseek_api_key, model=deepseek_model,
                                         timeout=timeout)

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

    def run_async(self, image_bytes: bytes, frame_key=None, hint: str = "",
                  img_hash=None) -> None:
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
        t = threading.Thread(target=self._run,
                             args=(image_bytes, frame_key, hint, img_hash), daemon=True)
        t.start()

    def _run(self, image_bytes: bytes, frame_key=None, hint: str = "",
             img_hash=None) -> None:
        """慢速通道主体。失败时不写缓存，让旧结果自然过期（不回退到旧数据）。"""
        try:
            if self.single_call:
                self._run_single(image_bytes, frame_key, hint, img_hash)
            else:
                self._run_two_stage(image_bytes, frame_key, hint, img_hash)
        except Exception:  # noqa: BLE001
            log.exception("multimodal run failed")
        finally:
            self._running = False

    def _run_single(self, image_bytes: bytes, frame_key=None, hint: str = "",
                    img_hash=None) -> None:
        """单次调用：一次模型请求直出判定。"""
        last_err = ""
        visual = None
        for attempt in range(self.max_tries):
            visual = self.qwen.analyze(image_bytes, hint)
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
            confidence=visual.confidence,
            frame_key=frame_key,
            img_hash=img_hash,
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

    def _run_two_stage(self, image_bytes: bytes, frame_key=None, hint: str = "",
                       img_hash=None) -> None:
        """两段式：AI-1 看图 → AI-2 决策（保留跨帧历史推理能力）。"""
        visual = None
        for attempt in range(self.max_tries):
            visual = self.qwen.analyze(image_bytes, hint)
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
            confidence=decision.confidence,
            frame_key=frame_key,
            img_hash=img_hash,
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

    @staticmethod
    def _hash_same(a, b) -> bool:
        """两个 256bit 感知哈希是否属于同一场景（汉明距离 ≤32）。"""
        if not a or not b:
            return False
        try:
            if len(a) != len(b):
                return False
            return sum(bin(x ^ y).count("1") for x, y in zip(a, b)) <= 32
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _same_scene(cached_key, cur_key, cached_img=None, cur_img=None) -> bool:
        """判断缓存结果是否还对应"同一场景"。

        慢通道每 30~90s 才抽查一帧，而设备每 10s 传一帧；
        若拿 40s 前的判定去推翻当前帧，就会把"正在低头"误判成"端正"。
        这里用特征指纹做近似匹配：两端都有指纹且差异过大 → 视为换了场景。

        P0-3：指纹有两种形态（几何元组 / ("img", 哈希)），而**形态会在相邻帧之间翻转**
        —— 关键点一失效，frame_key 就从几何变成图像哈希。旧实现遇到类型不一致直接
        返回 False，导致"快通道一失效，慢通道的正确结论就永远匹配不上"。
        现在类型不一致时退回**图像指纹**比较（两张图本身像不像），不再一票否决。
        几何 ↔ 几何的比较逻辑保持不变（仍然是 0.12 双阈值），不放松原有严格性。
        """
        # 缺几何指纹：只剩图像指纹可比
        if cached_key is None or cur_key is None:
            return MultimodalDetector._hash_same(cached_img, cur_img)

        cached_is_img = (isinstance(cached_key, tuple) and bool(cached_key)
                         and cached_key[0] == "img")
        cur_is_img = (isinstance(cur_key, tuple) and bool(cur_key)
                      and cur_key[0] == "img")

        # 形态 1：两边都是图像哈希
        if cached_is_img and cur_is_img:
            return MultimodalDetector._hash_same(cached_key[1], cur_key[1])

        # 形态 2：两边都是几何元组（保持原有严格判定）
        if not cached_is_img and not cur_is_img:
            try:
                dh = abs(float(cached_key[0]) - float(cur_key[0]))
                ds = abs(float(cached_key[1]) - float(cur_key[1]))
            except (TypeError, ValueError, IndexError):
                return MultimodalDetector._hash_same(cached_img, cur_img)
            return dh <= 0.12 and ds <= 0.12

        # 形态 3：类型不一致 → 退回图像指纹
        return MultimodalDetector._hash_same(cached_img, cur_img)

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
        frame_key=None,
        img_hash=None,
    ) -> MultimodalResult:
        """
        融合快速通道与慢速通道结果。

        规则（宁漏报不误报）：
          - 快速通道明确不端正 → 不端正（阶段一核心，优先）
          - 快速通道无法判定，但慢速通道明确不端正**且它自己认为该提醒** → 不端正
          - 两者都端正 → 端正
          - 其余 → 无法判定

        关键改动：
        1. 慢通道翻案必须同时满足 should_alert=True。
           模型判了 slouch 但明确 should_alert=false（"属于正常学习低头"）时不再翻案，
           否则会把"低头看书"当成驼背吹风——这是交付包评审指出的最大真实缺陷。
        2. P1-1 护栏：慢通道否决权（slow_override）不再是"无条件"的。
           快通道**连续 override_guard_frames 帧**判"不端正"时，几何证据已稳定，
           不再允许慢通道用一句"端正"把它整体否掉。
           理由：慢通道是 30s 一次的抽样，实测对同一坐姿会给出 away / unknown /
           upright / slouch 四种互相矛盾的结论；没有护栏时，它的**一次**"端正"
           就能让真正驼背的学生长时间收不到提醒（漏报）。
        3. P0-3：同场景判定改用 _same_scene(..., img_hash)，
           关键点有效/无效导致的指纹类型翻转不再一票否决。
        """
        # 快通道"连续判坏"计数（跨 /detect 调用累积）。
        # None（判不了）保持原值，不打断也不累加。
        if fast_upright is False:
            self._fast_bad_streak += 1
        elif fast_upright is True:
            self._fast_bad_streak = 0

        slow = self.latest()
        same = (slow is not None
                and self._same_scene(slow.frame_key, frame_key,
                                     slow.img_hash, img_hash))

        # ---- 慢通道否决权（最高优先级，带护栏）----
        # 慢通道明确"端正"且置信度足够时，可以推翻快速通道的"不端正"。
        # 依据：快速通道只测 head_height/shoulder_level 两个几何量，机位一变就系统性误判；
        #      慢通道看的是整幅画面，在端正/不端正这种语义级判断上更可靠。
        if same and slow.upright is True and slow.confidence >= self.slow_override_min_conf:
            guarded = (self.override_guard_frames > 0
                       and self._fast_bad_streak >= self.override_guard_frames)
            if not guarded:
                return MultimodalResult(
                    upright=True, state=slow.state, should_alert=False,
                    source="slow_override", visual=slow.visual, decision=slow.decision,
                    confidence=slow.confidence,
                    reason=f"slow_override(conf={slow.confidence:.2f}):{slow.reason}",
                    timestamp=slow.timestamp,
                )
            log.info(
                "slow_override blocked: fast_bad_streak=%d >= %d "
                "(slow=upright conf=%.2f evidence=%s)",
                self._fast_bad_streak, self.override_guard_frames,
                slow.confidence, slow.reason,
            )

        if fast_upright is False:
            return MultimodalResult(
                upright=False, state="slouch", should_alert=True,
                source="fast", reason=fast_reason or "fast_path_bad",
            )

        if fast_upright is True:
            # 几何判"端正"，但慢通道说"不端正且该提醒"：
            #   - 慢通道说的是 手机/走神/离开 等**语义状态** → 采信慢通道（几何看不见这些）
            #   - 慢通道说的是 低头/侧歪/趴桌 等**姿态状态** → 采信几何
            #     （几何就是量这个的；实测出现过端正帧被慢通道误判 slouch 而误吹）
            if (same and slow.upright is False and slow.should_alert
                    and slow.state not in POSTURAL_STATES):
                return MultimodalResult(
                    upright=False, state=slow.state, should_alert=True,
                    source="slow", visual=slow.visual, decision=slow.decision,
                    reason=f"slow_semantic:{slow.reason}",
                    timestamp=slow.timestamp,
                )
            return MultimodalResult(
                upright=True, state="upright", should_alert=False,
                source="fast", reason=fast_reason or "fast_path_ok",
            )

        # 快速通道判不了 → 只有"同一场景"的慢通道结果才可信
        trusted = same

        if trusted and slow.upright is False and slow.should_alert:
            return MultimodalResult(
                upright=False, state=slow.state, should_alert=True,
                source="slow", visual=slow.visual, decision=slow.decision,
                reason=slow.reason or "slow_path_bad",
                timestamp=slow.timestamp,
            )

        if trusted and slow.upright is False:
            # 慢通道看到不端正，但它判断"不该提醒"（如正常低头看书）
            # → 快速通道也无法判定时，保守给"无法判定"，不触发
            return MultimodalResult(
                upright=None, state=slow.state, should_alert=False,
                source="slow", visual=slow.visual, decision=slow.decision,
                reason=f"slow_no_alert:{slow.reason}",
                timestamp=slow.timestamp,
            )

        if trusted and slow.upright is True:
            # 【R3】低置信度的"端正"不足以宣告恢复。
            # 旧实现这里无条件返回 upright=True，于是一次 conf=0.3 的慢通道
            # "端正"就能把快速通道已经攒到 1 的坏帧计数清零 → 真实驼背不吹。
            # 现在低于 min_confidence 一律降级为"无法判定"（配合 RuleEngine
            # 对 None 的"保持"语义，坏帧计数不再被抹掉）。
            if slow.confidence < self.min_confidence:
                return MultimodalResult(
                    upright=None, state=slow.state, should_alert=False,
                    source="slow", visual=slow.visual, decision=slow.decision,
                    reason=f"slow_low_conf(conf={slow.confidence:.2f}<{self.min_confidence:.2f}):"
                           f"{slow.reason}",
                    timestamp=slow.timestamp,
                )
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
