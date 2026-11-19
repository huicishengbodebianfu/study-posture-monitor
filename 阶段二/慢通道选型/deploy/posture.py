from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

NOSE = 0
L_SHOULDER = 5
R_SHOULDER = 6

KP_CONF_THRESHOLD = 0.5

# 肩宽占图宽的最小比例。低于此值视为检测异常（判为"无法判定"，不触发提醒）。
#
# 为什么需要这个保护：head_height 和 shoulder_level 都是「距离 / 肩宽」的比值，
# 一旦肩宽被检测得极小，除数趋近 0，比值会爆炸。实测中曾出现 head_height=167
# 这种数值（肩宽被检测成约 1 像素）。
# 原来的 `shoulder_width < 1e-6` 检查拦不住 1 像素这种量级，因此改用相对阈值。
#
# 取值依据：正常正面人像中肩宽通常占图宽 15%~95%；远端小人（如 bus.jpg 中
# 肩宽 72.9px / 图宽 810px = 9%）仍应被接受。2% 是一个足够宽松的下限，
# 只拦截明显退化的检测。
MIN_SHOULDER_WIDTH_RATIO = 0.02

# head_height 的合理上限。超过它说明姿态几何不可信（人侧身 / 肩膀被透视压缩 /
# 关键点错位），此时比值没有物理意义，应判为"无法判定"而不是硬给一个数值。
#
# 取值依据：真实人像实测范围约 -1.0 ~ 1.8
#   · 坐直        0.68
#   · 低头驼背    0.21（可为负，头低于肩线时说明极度驼背，属正常情况，不拦）
#   · 侧身照      1.69 ~ 1.78（几何已失真，但仍可能正确判为不端正）
#   · COCO 样本   0.65 ~ 1.04
# 3.0 留了足够余量，只拦截明显崩坏的帧（如肩宽被压缩到临界值时产生的 5.0）。
MAX_HEAD_HEIGHT_RATIO = 3.0


@dataclass
class PostureFeatures:
    head_height: float
    shoulder_level: float
    valid: bool
    reason: str = ""


@dataclass
class Thresholds:
    head_height_min: float = 0.15
    shoulder_level_max: float = 0.05


def extract_features(
    keypoints: np.ndarray,
    kp_conf: np.ndarray,
    image_width: Optional[float] = None,
) -> PostureFeatures:
    needed = (NOSE, L_SHOULDER, R_SHOULDER)
    if any(kp_conf[i] < KP_CONF_THRESHOLD for i in needed):
        return PostureFeatures(0.0, 0.0, valid=False, reason="low_kp_conf")

    ls_x = keypoints[L_SHOULDER][0]
    rs_x = keypoints[R_SHOULDER][0]
    shoulder_width = abs(ls_x - rs_x)
    if shoulder_width < 1e-6:
        return PostureFeatures(0.0, 0.0, valid=False, reason="bad_shoulder_width")

    # 相对阈值保护：肩宽太小说明关键点被压在一起，比值不可信
    if image_width and image_width > 0:
        if shoulder_width < MIN_SHOULDER_WIDTH_RATIO * float(image_width):
            return PostureFeatures(
                0.0, 0.0, valid=False, reason="shoulder_too_narrow"
            )

    nose_y = keypoints[NOSE][1]
    ls_y = keypoints[L_SHOULDER][1]
    rs_y = keypoints[R_SHOULDER][1]

    shoulder_mid_y = (ls_y + rs_y) / 2.0
    head_height = shoulder_mid_y - nose_y
    shoulder_level = abs(ls_y - rs_y)

    # 注意：必须显式转成 Python 原生 float。
    # 关键点数组来自 OpenVINO 推理输出，是 numpy 数组；上面的算术结果
    # 因此是 numpy.float32。round() 对 np.float32 仍返回 np.float32，
    # 而 json.dumps 无法序列化 numpy 标量 → FastAPI 返回 500：
    #   TypeError: Object of type float32 is not JSON serializable
    hh = float(head_height / shoulder_width)
    sl = float(shoulder_level / shoulder_width)

    # 输出合理性检查：比值过大说明几何崩坏（侧身/透视压缩/关键点错位）
    # 一律判为"无法判定"，宁漏报不误报（文档 D9）
    if hh > MAX_HEAD_HEIGHT_RATIO:
        return PostureFeatures(0.0, 0.0, valid=False, reason="pose_geometry_unreliable")

    return PostureFeatures(
        head_height=hh,
        shoulder_level=sl,
        valid=True,
    )


def is_upright(features: PostureFeatures, th: Thresholds) -> Optional[bool]:
    if not features.valid:
        return None
    if features.head_height < th.head_height_min:
        return False
    if features.shoulder_level > th.shoulder_level_max:
        return False
    return True


class RuleEngine:
    def __init__(self, consecutive_frames: int = 2, dedup_seconds: int = 60,
                 bad_hold_seconds: float = 60.0):
        self.consecutive_frames = consecutive_frames
        self.dedup_seconds = dedup_seconds
        # 坏帧计数的保留窗口：两次坏帧之间允许夹若干"无法判定"的帧，
        # 但超过这个窗口都没有新的坏帧，就视为已经恢复。
        self.bad_hold_seconds = bad_hold_seconds
        self._bad_streak = 0
        self._last_blow_ts = 0.0
        self._last_bad_ts = 0.0

    def update(self, upright: Optional[bool]) -> str:
        now = time.time()

        if upright is None:
            # 【R3】**"无法判定"不等于"已经坐正"。**
            # 旧实现在这里直接把 _bad_streak 清零，后果是：
            #   帧1 判不端正（streak=1）→ 帧2 因 low_kp_conf 无法判定（streak 被清零）
            #   → 帧3 判不端正（streak=1）……
            # 只要中间夹一帧看不清，真实驼背就永远凑不满 consecutive_frames，
            # 永远不吹风。这正好是"往漏报方向"的洞，比误报更危险（用户以为在监测）。
            # 现在改为"保持"，只在超过保留窗口后才过期清零。
            if self._bad_streak and now - self._last_bad_ts > self.bad_hold_seconds:
                self._bad_streak = 0
            return "none"

        if upright:
            # 真正的"恢复"才清零
            self._bad_streak = 0
            return "none"

        # 距上一个坏帧太久 → 不构成"连续"
        if self._bad_streak and now - self._last_bad_ts > self.bad_hold_seconds:
            self._bad_streak = 0
        self._bad_streak += 1
        self._last_bad_ts = now
        if self._bad_streak >= self.consecutive_frames:
            if now - self._last_blow_ts >= self.dedup_seconds:
                self._last_blow_ts = now
                self._bad_streak = 0
                return "blow"

        return "none"

    def reset(self):
        self._bad_streak = 0
        self._last_blow_ts = 0.0
        self._last_bad_ts = 0.0
