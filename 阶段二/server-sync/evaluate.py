#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
坐姿判定 · 精度评估脚本

用途：用一批已分类的真实照片，量化当前算法的「误报率 / 漏报率」，
     并给出建议阈值。回答"这个系统到底够不够准"这个问题。

与 calibrate.py 的区别：
  calibrate.py  只输出特征分布和建议阈值
  evaluate.py   输出混淆矩阵、误报率、漏报率、阈值扫描曲线，并单独统计
                被拒绝的帧（no_person / low_kp_conf / shoulder_too_narrow）

用法（在服务器 /opt/posture 下）：
    source venv/bin/activate

    # 基础用法：两个文件夹，各放一批照片
    python evaluate.py --upright samples/upright --slouch samples/slouch

    # 指定当前阈值（默认读环境变量，与线上服务一致）
    python evaluate.py --upright ... --slouch ... \
        --head-height-min 0.37 --shoulder-level-max 0.03

    # 只做阈值扫描，不打印逐张明细
    python evaluate.py --upright ... --slouch ... --quiet

样本要求：
  - 两个文件夹里的照片必须是「同一位置、同一光照」下拍的
  - 每组建议 ≥ 30 张，覆盖你真实的坐姿变化
  - 格式：.jpg / .jpeg / .png
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
from typing import Optional

import numpy as np

from app import preprocess, infer, unletterbox
from posture import (
    MIN_SHOULDER_WIDTH_RATIO,
    KP_CONF_THRESHOLD,
    extract_features,
    Thresholds,
)

IMAGE_EXTS = ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.JPEG", "*.PNG")

# 被拒绝帧的原因说明
REASON_CN = {
    "ok": "正常",
    "no_person": "未检测到人",
    "low_kp_conf": "关键点置信度不足（暗光/遮挡/人太小）",
    "bad_shoulder_width": "双肩重合",
    "shoulder_too_narrow": "肩宽过小（关键点被压在一起）",
    "pose_geometry_unreliable": "姿态几何失真（侧身/透视压缩）",
}


# ---------------------------------------------------------------------------
# 采样
# ---------------------------------------------------------------------------

def collect(folder: str, label: str) -> list[dict]:
    """对文件夹内所有图片推理，返回逐张结果。"""
    if not os.path.isdir(folder):
        print(f"  [错误] 目录不存在: {folder}", file=sys.stderr)
        return []

    files = []
    for ext in IMAGE_EXTS:
        files.extend(glob.glob(os.path.join(folder, ext)))
    files.sort()

    if not files:
        print(f"  [警告] {folder} 里没有图片", file=sys.stderr)
        return []

    rows = []
    for path in files:
        name = os.path.basename(path)
        try:
            with open(path, "rb") as fp:
                data = fp.read()
            blob, scale, pad_x, pad_y, ow, oh = preprocess(data)
            kp, conf = infer(blob)
        except Exception as e:  # noqa: BLE001
            rows.append({"file": name, "label": label, "valid": False,
                         "reason": f"exception: {type(e).__name__}"})
            continue

        if kp is None:
            rows.append({"file": name, "label": label, "valid": False,
                         "reason": "no_person"})
            continue

        kp = unletterbox(kp, scale, pad_x, pad_y)
        feat = extract_features(kp, conf, image_width=ow)

        rows.append({
            "file": name,
            "label": label,
            "valid": feat.valid,
            "reason": feat.reason if feat.reason else "ok",
            "head_height": feat.head_height if feat.valid else None,
            "shoulder_level": feat.shoulder_level if feat.valid else None,
            "shoulder_width": float(abs(kp[5][0] - kp[6][0])),
            "image_width": float(ow),
        })
    return rows


# ---------------------------------------------------------------------------
# 统计工具
# ---------------------------------------------------------------------------

def describe(name: str, arr: np.ndarray) -> None:
    if len(arr) == 0:
        print(f"    {name:<16} 无有效数据")
        return
    print(f"    {name:<16} n={len(arr):<4} "
          f"mean={arr.mean():.4f}  "
          f"P5={np.percentile(arr, 5):.4f}  "
          f"P50={np.percentile(arr, 50):.4f}  "
          f"P95={np.percentile(arr, 95):.4f}  "
          f"min={arr.min():.4f}  max={arr.max():.4f}")


def reason_summary(rows: list[dict]) -> None:
    """统计被拒绝帧的原因分布。"""
    bad = [r for r in rows if not r["valid"]]
    if not bad:
        print("    全部帧有效 ✅")
        return
    counts: dict[str, int] = {}
    for r in bad:
        key = r["reason"].split(":")[0]
        counts[key] = counts.get(key, 0) + 1
    total = len(rows)
    for key, cnt in sorted(counts.items(), key=lambda kv: -kv[1]):
        cn = REASON_CN.get(key, key)
        print(f"    {cn:<32} {cnt:>3} 帧  ({100.0 * cnt / total:.1f}%)")


def confusion(upright_hh: np.ndarray, slouch_hh: np.ndarray,
              tp_th: float, sl_th: float,
              upright_sl: np.ndarray, slouch_sl: np.ndarray) -> dict:
    """
    按当前阈值计算混淆矩阵。
    正类 = 不端正（应该触发提醒）
    """
    up_pred_bad = ((upright_hh < tp_th) | (upright_sl > sl_th)).sum()
    sl_pred_bad = ((slouch_hh < tp_th) | (slouch_sl > sl_th)).sum()

    up_total = len(upright_hh)
    sl_total = len(slouch_hh)

    fp = int(up_pred_bad)          # 坐直却被判不端正
    tp = int(sl_pred_bad)          # 驼背被判不端正
    fn = sl_total - tp             # 驼背却被判端正
    tn = up_total - fp             # 坐直被判端正

    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "up_total": up_total, "sl_total": sl_total,
        "fpr": (fp / up_total) if up_total else float("nan"),
        "fnr": (fn / sl_total) if sl_total else float("nan"),
        "acc": ((tp + tn) / (up_total + sl_total))
               if (up_total + sl_total) else float("nan"),
    }


def sweep_threshold(upright_hh: np.ndarray, slouch_hh: np.ndarray) -> Optional[dict]:
    """扫描 HEAD_HEIGHT_MIN，找误报率+漏报率最小的点。"""
    if len(upright_hh) == 0 or len(slouch_hh) == 0:
        return None

    lo = float(min(upright_hh.min(), slouch_hh.min()))
    hi = float(max(upright_hh.max(), slouch_hh.max()))
    if hi <= lo:
        return None

    best = None
    for th in np.linspace(lo, hi, 300):
        fpr = float((upright_hh < th).mean())
        fnr = float((slouch_hh >= th).mean())
        score = fpr + fnr
        if best is None or score < best["score"]:
            best = {"th": float(th), "fpr": fpr, "fnr": fnr, "score": score}

    # 误报率 = 漏报率 的平衡点
    balance = None
    for th in np.linspace(lo, hi, 300):
        fpr = float((upright_hh < th).mean())
        fnr = float((slouch_hh >= th).mean())
        if balance is None or abs(fpr - fnr) < abs(balance["fpr"] - balance["fnr"]):
            balance = {"th": float(th), "fpr": fpr, "fnr": fnr}

    return {"best": best, "balance": balance}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="坐姿判定精度评估",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--upright", required=True, help="端正样本目录")
    ap.add_argument("--slouch", required=True, help="不端正样本目录")
    ap.add_argument("--head-height-min", type=float,
                    default=float(os.environ.get("HEAD_HEIGHT_MIN", "0.15")),
                    help="当前头高阈值（默认读环境变量，否则 0.15）")
    ap.add_argument("--shoulder-level-max", type=float,
                    default=float(os.environ.get("SHOULDER_LEVEL_MAX", "0.05")),
                    help="当前肩平差阈值（默认读环境变量，否则 0.05）")
    ap.add_argument("--quiet", action="store_true", help="不打印逐张明细")
    args = ap.parse_args()

    th = Thresholds(head_height_min=args.head_height_min,
                    shoulder_level_max=args.shoulder_level_max)

    print("=" * 68)
    print("  坐姿判定 · 精度评估")
    print("=" * 68)
    print(f"  端正样本目录:   {args.upright}")
    print(f"  不端正样本目录: {args.slouch}")
    print(f"  当前阈值:       HEAD_HEIGHT_MIN={th.head_height_min}  "
          f"SHOULDER_LEVEL_MAX={th.shoulder_level_max}")
    print(f"  判定参数:       关键点置信度 >= {KP_CONF_THRESHOLD}  "
          f"肩宽 >= 图宽 {MIN_SHOULDER_WIDTH_RATIO:.0%}")

    # ---------- 推理 ----------
    print("\n[1/4] 推理中……")
    up_rows = collect(args.upright, "upright")
    sl_rows = collect(args.slouch, "slouch")
    if not up_rows and not sl_rows:
        print("  没有可用的样本，退出。", file=sys.stderr)
        return 2

    print(f"  端正样本   {len(up_rows)} 张")
    print(f"  不端正样本 {len(sl_rows)} 张")

    if not args.quiet:
        print("\n  --- 逐张明细（仅列被拒绝的帧）---")
        rejected = [r for r in (up_rows + sl_rows) if not r["valid"]]
        if not rejected:
            print("    无")
        for r in rejected:
            print(f"    [{r['label']:<8}] {r['file']:<32} {r['reason']}")

    # ---------- 有效帧统计 ----------
    print("\n[2/4] 有效帧统计")
    print("  端正组:")
    reason_summary(up_rows)
    print("  不端正组:")
    reason_summary(sl_rows)

    up_valid = [r for r in up_rows if r["valid"]]
    sl_valid = [r for r in sl_rows if r["valid"]]

    # 样本量不足时明确警告，避免拿几张图的数字当结论
    if len(up_valid) < 10 or len(sl_valid) < 10:
        print()
        print("  " + "!" * 62)
        print(f"  ⚠️  样本量不足：端正 {len(up_valid)} 张 / 不端正 {len(sl_valid)} 张")
        print("     统计结果仅供参考，不能作为定论。")
        print("     建议每组至少 30 张，且覆盖你真实的坐姿变化。")
        print("  " + "!" * 62)

    up_hh = np.array([r["head_height"] for r in up_valid], dtype=float)
    up_sl = np.array([r["shoulder_level"] for r in up_valid], dtype=float)
    sl_hh = np.array([r["head_height"] for r in sl_valid], dtype=float)
    sl_sl = np.array([r["shoulder_level"] for r in sl_valid], dtype=float)

    print("\n[3/4] 特征分布")
    print("  头高 head_height（越大越挺直）")
    describe("端正", up_hh)
    describe("不端正", sl_hh)
    if len(up_hh) and len(sl_hh) and sl_hh.mean() > 0:
        ratio = up_hh.mean() / sl_hh.mean()
        verdict = "区分度良好" if ratio >= 1.5 else "区分度不足"
        print(f"    区分度（端正/不端正）: {ratio:.2f}x   → {verdict}")

    print("\n  肩平差 shoulder_level（越小越平）")
    describe("端正", up_sl)
    describe("不端正", sl_sl)
    if len(up_sl) and len(sl_sl) and up_sl.mean() > 0:
        ratio = sl_sl.mean() / up_sl.mean()
        verdict = "区分度良好" if ratio >= 1.5 else "区分度不足（侧歪检测偏弱）"
        print(f"    区分度（不端正/端正）: {ratio:.2f}x   → {verdict}")

    if len(up_hh) and len(sl_hh):
        overlap = float(up_hh.min()) <= float(sl_hh.max())
        print(f"\n  头高分布是否重叠: {'是 ⚠️' if overlap else '否 ✅'}"
              f"  （端正最低 {up_hh.min():.4f} vs 不端正最高 {sl_hh.max():.4f}）")

    # ---------- 混淆矩阵 ----------
    print("\n[4/4] 当前阈值下的表现")
    if len(up_hh) == 0 or len(sl_hh) == 0:
        print("  某一类没有有效样本，无法计算混淆矩阵。")
        print("  请检查样本目录，或改善拍摄条件后重拍。")
        return 1

    cm = confusion(up_hh, sl_hh, th.head_height_min, th.shoulder_level_max,
                   up_sl, sl_sl)
    print(f"    真正例 TP（驼背→吹）  {cm['tp']:>3} / {cm['sl_total']}")
    print(f"    假正例 FP（坐直→吹）  {cm['fp']:>3} / {cm['up_total']}")
    print(f"    真负例 TN（坐直→不吹）{cm['tn']:>3} / {cm['up_total']}")
    print(f"    假负例 FN（驼背→不吹）{cm['fn']:>3} / {cm['sl_total']}")
    print()
    print(f"    误报率 FPR = {cm['fpr']:.1%}   （坐直却被吹，最影响体验）")
    print(f"    漏报率 FNR = {cm['fnr']:.1%}   （驼背没吹，影响监督效果）")
    print(f"    准确率 ACC = {cm['acc']:.1%}")

    # ---------- 阈值建议 ----------
    print("\n[建议] 阈值扫描（只扫 HEAD_HEIGHT_MIN，肩平差保持不动）")
    sw = sweep_threshold(up_hh, sl_hh)
    if not sw:
        print("  数据不足，无法给出建议。")
        return 0

    b = sw["balance"]
    best = sw["best"]
    print(f"  误报=漏报 平衡点:  HEAD_HEIGHT_MIN = {b['th']:.3f}"
          f"   (FPR={b['fpr']:.1%}, FNR={b['fnr']:.1%})")
    print(f"  总错误最小点:      HEAD_HEIGHT_MIN = {best['th']:.3f}"
          f"   (FPR={best['fpr']:.1%}, FNR={best['fnr']:.1%})")
    print()
    print("  选择原则（文档 D6 决策：先松后紧）：")
    print("    · 初期宁可漏报，也别频繁误报 → 取偏低的阈值（更严格才吹）")
    print("    · 即 threshold 取「总错误最小点」和「平衡点」中较小的那个")
    safer = min(b["th"], best["th"])
    print(f"    · 建议值: HEAD_HEIGHT_MIN = {safer:.3f}")

    cur_fpr = cm["fpr"]
    if cur_fpr > 0.10:
        print(f"\n  ⚠️ 当前阈值误报率 {cur_fpr:.1%} 偏高，建议按上面的数值调整 .env")
    elif cur_fpr > 0.03:
        print(f"\n  当前阈值误报率 {cur_fpr:.1%}，可接受，仍有优化空间")
    else:
        print(f"\n  ✅ 当前阈值误报率 {cur_fpr:.1%}，表现良好")

    print("\n  应用方法：")
    print(f"    sed -i 's/^HEAD_HEIGHT_MIN=.*/HEAD_HEIGHT_MIN={safer:.3f}/' .env")
    print("    sudo systemctl restart posture")
    print("\n" + "=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
