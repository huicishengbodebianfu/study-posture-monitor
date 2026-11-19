"""从真机采集图算阈值：支持 180° 倒置自动纠正。

用法（服务器）：
    /opt/posture/venv/bin/python analyze_real_captures.py <目录> <组名>=<标签> ...
例：
    analyze_real_captures.py /tmp/calib120 20261108-173751=upright 20261108-173834=upright 20261108-173914=sleep
"""
import glob
import json
import os
import sys

import cv2

sys.path.insert(0, "/opt/posture")

import app
from posture import extract_features, is_upright


def infer_with_flip(data, force_flip=False):
    """返回 (features, flipped_used)。先按原方向，没人再转 180°。"""
    img = cv2.imdecode(app.np.frombuffer(data, app.np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None, False
    variants = [(img, False)]
    if force_flip:
        variants = [(cv2.rotate(img, cv2.ROTATE_180), True)]
    else:
        variants.append((cv2.rotate(img, cv2.ROTATE_180), True))

    for mat, flipped in variants:
        ok, buf = cv2.imencode(".jpg", mat, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        if not ok:
            continue
        blob, scale, px, py, ow, oh = app.preprocess(buf.tobytes())
        kp, conf = app.infer(blob)
        if kp is None:
            continue
        kp = app.unletterbox(kp, scale, px, py)
        feat = extract_features(kp, conf, image_width=ow)
        if feat.valid:
            return feat, flipped
    return None, False


def main():
    root = sys.argv[1]
    labels = {}
    for arg in sys.argv[2:]:
        g, lab = arg.split("=")
        labels[g] = lab

    rows = []
    for group in sorted(os.listdir(root)):
        d = os.path.join(root, group)
        if not os.path.isdir(d):
            continue
        lab = labels.get(group, "unknown")
        files = sorted(glob.glob(os.path.join(d, "frame-*.jpg")))
        stat = {"n": 0, "ok": 0, "flip": 0, "hh": [], "sl": []}
        for f in files:
            with open(f, "rb") as fp:
                data = fp.read()
            feat, flipped = infer_with_flip(data)
            stat["n"] += 1
            if flipped:
                stat["flip"] += 1
            if feat is None:
                continue
            stat["ok"] += 1
            stat["hh"].append(feat.head_height)
            stat["sl"].append(feat.shoulder_level)
            rows.append({"group": group, "label": lab, "file": os.path.basename(f),
                         "flipped": flipped, "hh": round(feat.head_height, 4),
                         "sl": round(feat.shoulder_level, 4),
                         "fast": is_upright(feat, app.thresholds)})
        print(f"{group} [{lab}]  帧={stat['n']} 检出={stat['ok']} 需翻转={stat['flip']}")

    print("\n" + "=" * 76)
    print(f"{'标签':<10}{'组':<22}{'检出':<6}{'hh min':>8}{'hh P5':>8}{'hh P50':>8}{'hh P95':>8}{'hh max':>8}{'sl P50':>8}")
    for group in sorted({r['group'] for r in rows}):
        rs = [r for r in rows if r["group"] == group]
        hh = sorted(r["hh"] for r in rs)
        sl = sorted(r["sl"] for r in rs)
        if not hh:
            continue
        q = lambda a, p: a[min(len(a) - 1, int(len(a) * p))]
        print(f"{rs[0]['label']:<10}{group:<22}{len(hh):<6}{hh[0]:>8.3f}{q(hh,.05):>8.3f}"
              f"{q(hh,.5):>8.3f}{q(hh,.95):>8.3f}{hh[-1]:>8.3f}{q(sl,.5):>8.3f}")

    # 分组汇总
    print("\n按标签汇总：")
    for lab in sorted({r['label'] for r in rows}):
        rs = [r for r in rows if r["label"] == lab]
        hh = sorted(r["hh"] for r in rs)
        if not hh:
            continue
        q = lambda a, p: a[min(len(a) - 1, int(len(a) * p))]
        print(f"  {lab:<10} n={len(hh):<4} hh: min={hh[0]:.3f} p5={q(hh,.05):.3f} "
              f"p50={q(hh,.5):.3f} p95={q(hh,.95):.3f} max={hh[-1]:.3f}")

    up = [r["hh"] for r in rows if r["label"] == "upright"]
    bad = [r["hh"] for r in rows if r["label"] in ("sleep", "slouch", "lean")]
    if up and bad:
        print("\n" + "=" * 76)
        print(f"端正 hh 最低 = {min(up):.3f}   （P5 = {sorted(up)[max(0,int(len(up)*0.05))]:.3f}）")
        print(f"趴下 hh 最高 = {max(bad):.3f}   （P95 = {sorted(bad)[min(len(bad)-1,int(len(bad)*0.95))]:.3f}）")
        gap_lo, gap_hi = max(bad), min(up)
        if gap_hi > gap_lo:
            print(f"→ 完全可分，空档 {gap_lo:.3f} ~ {gap_hi:.3f}")
            print(f"→ 建议 HEAD_HEIGHT_MIN = {(gap_lo + gap_hi) / 2:.3f}")
        else:
            print("→ 有重叠，需要再看分布（可能要用 shoulder_level 或置信度辅助）")

    json.dump(rows, open(os.path.join(root, "_features.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n明细 -> {os.path.join(root, '_features.json')}")


if __name__ == "__main__":
    main()
