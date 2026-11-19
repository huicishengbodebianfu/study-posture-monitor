from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

NOSE = 0
L_SHOULDER = 5
R_SHOULDER = 6

KP_CONF_THRESHOLD = 0.5


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


def extract_features(keypoints: np.ndarray, kp_conf: np.ndarray) -> PostureFeatures:
    needed = (NOSE, L_SHOULDER, R_SHOULDER)
    if any(kp_conf[i] < KP_CONF_THRESHOLD for i in needed):
        return PostureFeatures(0.0, 0.0, valid=False, reason="low_kp_conf")

    ls_x = keypoints[L_SHOULDER][0]
    rs_x = keypoints[R_SHOULDER][0]
    shoulder_width = abs(ls_x - rs_x)
    if shoulder_width < 1e-6:
        return PostureFeatures(0.0, 0.0, valid=False, reason="bad_shoulder_width")

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
    return PostureFeatures(
        head_height=float(head_height / shoulder_width),
        shoulder_level=float(shoulder_level / shoulder_width),
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
    def __init__(self, consecutive_frames: int = 2, dedup_seconds: int = 60):
        self.consecutive_frames = consecutive_frames
        self.dedup_seconds = dedup_seconds
        self._bad_streak = 0
        self._last_blow_ts = 0.0

    def update(self, upright: Optional[bool]) -> str:
        now = time.time()

        if upright is None:
            self._bad_streak = 0
            return "none"

        if upright:
            self._bad_streak = 0
            return "none"

        self._bad_streak += 1
        if self._bad_streak >= self.consecutive_frames:
            if now - self._last_blow_ts >= self.dedup_seconds:
                self._last_blow_ts = now
                self._bad_streak = 0
                return "blow"

        return "none"

    def reset(self):
        self._bad_streak = 0
        self._last_blow_ts = 0.0
