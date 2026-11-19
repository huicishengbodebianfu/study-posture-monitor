"""对真机采集包做阈值标定分析。

输入：captures/ 目录（内含若干 <时间>-<标签>/ 子目录，每个有 frame-*.jpg + meta.json）
输出：
  1. 每个标签的 head_height / shoulder_level 分位
  2. 用当前阈值判定的混淆情况
  3. 建议阈值（最大化"端正 vs 不端正"的间隔）

在服务器上跑（需要 /opt/posture 的 openvino 环境）：
    /opt/posture/venv/bin/python analyze_captures.py <captures 目录>
"""
import glob
import json
import os
import statistics
import sys

sys.path.insert(0, "/opt/posture")

import app
from posture import extract_features, is_upright

UPRIGHT_LIKE = {"upright"}
BAD_LIKE = {"slouch", "lean", "sleep"}


def analyse(root):
    groups = sorted(d for d in glob.glob(os.path.join(root, "*")) if os.path.isdir(d))
    if not groups:
        print("没找到采集目录")
        return

    rows = []
    for d in groups:
        meta_path = os.path.join(d, "meta.json")
        label = os.path.basename(d).split("-", 2)[-1]
        if os.path.exists(meta_path):
            try:
                label = json.load(open(meta_path, encoding="utf-8")).get("label", label)
            except Exception:  # noqa: BLE001
                pass
        files = sorted(glob.glob(os.path.join(d, "frame-*.jpg")))
        print(f"[{label}] {len(files)} 帧  <- {os.path.basename(d)}")
        for f in files:
            with open(f, "rb") as fp:
                data = fp.read()
            blob, scale, px, py, ow, oh = app.preprocess(data)
            kp, conf = app.infer(blob)
            if kp is None:
                rows.append({"label": label, "file": f, "valid": False, "hh": None, "sl": None})
                continue
            kp = app.unletterbox(kp, scale, px, py)
            feat = extract_features(kp, conf, image_width=ow)
            rows.append({"label": label, "file": f, "valid": feat.valid,
                         "hh": feat.head_height if feat.valid else None,
                         "sl": feat.shoulder_level if feat.valid else None,
                         "fast": is_upright(feat, app.thresholds) if feat.valid else None})

    # ---- 分组统计 ----
    print("\n" + "=" * 78)
    print(f"{'标签':<10}{'帧数':<6}{'有效':<6}{'hh P5':>8}{'hh P50':>8}{'hh P95':>8}"
          f"{'sl P5':>8}{'sl P50':>8}{'sl P95':>8}")
    stats = {}
    for label in sorted({r["label"] for r in rows}):
        rs = [r for r in rows if r["label"] == label]
        v = [r for r in rs if r["valid"]]
        if not v:
            print(f"{label:<10}{len(rs):<6}{0:<6}   (无有效检测)")
            continue
        hh = sorted(r["hh"] for r in v)
        sl = sorted(r["sl"] for r in v)
        def q(a, p):
            return a[min(len(a) - 1, int(len(a) * p))]
        stats[label] = {"hh": hh, "sl": sl, "n": len(v)}
        print(f"{label:<10}{len(rs):<6}{len(v):<6}{q(hh,.05):>8.3f}{q(hh,.5):>8.3f}{q(hh,.95):>8.3f}"
              f"{q(sl,.05):>8.3f}{q(sl,.5):>8.3f}{q(sl,.95):>8.3f}")

    # ---- 建议阈值 ----
    up = [r["hh"] for r in rows if r["valid"] and r["label"] in UPRIGHT_LIKE]
    bad = [r["hh"] for r in rows if r["valid"] and r["label"] in BAD_LIKE]
    if up and bad:
        up_min, bad_max = min(up), max(bad)
        print("\n" + "=" * 78)
        print(f"端正 head_height 最低值 : {up_min:.3f}")
        print(f"不端正 head_height 最高值: {bad_max:.3f}")
        if up_min > bad_max:
            print(f"→ 完全可分，建议 HEAD_HEIGHT_MIN = {(up_min + bad_max) / 2:.3f}"
                  f"（两侧余量各 {(up_min - bad_max) / 2:.3f}）")
        else:
            print(f"→ 有重叠（{bad_max:.3f} ~ {up_min:.3f}），单阈值无法完全分离；"
                  f"建议 HEAD_HEIGHT_MIN = {bad_max + (up_min - bad_max) * 0.5 if up_min > bad_max else up_min:.3f}")

    # ---- 当前阈值下的混淆 ----
    print("\n" + "=" * 78)
    print("当前阈值下的判定：")
    for label in sorted({r["label"] for r in rows}):
        rs = [r for r in rows if r["label"] == label and r["fast"] is not None]
        if not rs:
            continue
        t = sum(1 for r in rs if r["fast"] is True)
        f = sum(1 for r in rs if r["fast"] is False)
        print(f"  {label:<10} fast=True {t:<4} fast=False {f:<4}  (共 {len(rs)} 帧可判)")

    json.dump(rows, open(os.path.join(root, "_analysis.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n明细已写入 {os.path.join(root, '_analysis.json')}")


if __name__ == "__main__":
    analyse(sys.argv[1] if len(sys.argv) > 1 else "./captures")
