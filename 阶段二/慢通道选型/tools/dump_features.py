"""用线上 YOLOv8-pose 跑一遍带真值的图，导出每张的 head_height / shoulder_level。

用于阈值标定：只有在"我们自己的相机坐标系"里算出来的特征才有可比性。
"""
import json
import os
import sys

sys.path.insert(0, "/opt/posture")

import numpy as np

import app
from posture import extract_features, is_upright

GT = {
    "w_upright_lib": "upright", "w_upright_desk_top": "upright",
    "w_upright_reading2": "upright", "w_upright_lamp": "upright",
    "w_upright_write": "upright", "w_upright_kid_lamp": "upright",
    "w_study_hunch": "slouch", "w_study_hunch_multi": "slouch",
    "w_night_dark": "slouch", "w_night_back": "slouch",
    "w_phone_side": "phone", "w_phone_two": "phone",
    "w_sleep_class": "sleep", "w_sleep_close": "sleep",
    "w_daze_chin": "daze", "w_empty_room": "away",
    "p_black_frame": "unknown", "p_occlude_hand": "unknown",
    "p_ceiling": "unknown", "p_underdesk": "unknown",
    "p_qvga_side": "lean", "p_side_lookup1": "lean",
    "p_side_lookup2": "lean", "p_side_lookup3": "lean",
}

rows = []
for name, gt in GT.items():
    path = f"/tmp/t24/{name}.jpg"
    if not os.path.exists(path):
        path = f"/tmp/t24/{name}.jpg"
    with open(path, "rb") as f:
        data = f.read()
    blob, scale, pad_x, pad_y, ow, oh = app.preprocess(data)
    kp, conf = app.infer(blob)
    if kp is None:
        rows.append({"image": name, "gt": gt, "valid": False, "reason": "no_person"})
        print(f"{name:24s} {gt:8s} 无检测")
        continue
    kp = app.unletterbox(kp, scale, pad_x, pad_y)
    feat = extract_features(kp, conf, image_width=ow)
    up = is_upright(feat, app.thresholds)
    rows.append({"image": name, "gt": gt, "valid": feat.valid,
                 "head_height": round(feat.head_height, 4),
                 "shoulder_level": round(feat.shoulder_level, 4),
                 "fast_upright": up, "reason": feat.reason})
    print(f"{name:24s} {gt:8s} hh={feat.head_height:+.3f} sl={feat.shoulder_level:.3f} "
          f"valid={feat.valid} fast={up}")

with open("/tmp/features.json", "w", encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False, indent=1)
print("\n-> /tmp/features.json")
