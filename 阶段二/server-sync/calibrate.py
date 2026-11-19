from __future__ import annotations

import argparse
import glob
import os

import numpy as np

from posture import extract_features
from app import preprocess, infer, unletterbox


def collect_features(folder: str):
    files = []
    for ext in ("*.jpg", "*.jpeg", "*.png"):
        files.extend(glob.glob(os.path.join(folder, ext)))
    files.sort()

    if not files:
        print(f"  no images in {folder}")
        return np.array([]), np.array([])

    head_heights = []
    shoulder_levels = []
    skipped = 0

    for f in files:
        with open(f, "rb") as fp:
            data = fp.read()
        try:
            blob, scale, pad_x, pad_y, ow, oh = preprocess(data)
            kp, conf = infer(blob)
            if kp is None:
                skipped += 1
                continue
            kp = unletterbox(kp, scale, pad_x, pad_y)
            feat = extract_features(kp, conf, image_width=ow)
            if not feat.valid:
                skipped += 1
                continue
            head_heights.append(feat.head_height)
            shoulder_levels.append(feat.shoulder_level)
        except Exception as e:
            print(f"  skip {os.path.basename(f)}: {e}")
            skipped += 1

    print(f"  {len(files)} images, {len(head_heights)} valid, {skipped} skipped")
    return np.array(head_heights), np.array(shoulder_levels)


def describe(name: str, arr: np.ndarray):
    if len(arr) == 0:
        print(f"  {name}: no data")
        return
    print(
        f"  {name}: mean={arr.mean():.4f} "
        f"P5={np.percentile(arr, 5):.4f} "
        f"P50={np.percentile(arr, 50):.4f} "
        f"P95={np.percentile(arr, 95):.4f} "
        f"min={arr.min():.4f} max={arr.max():.4f}"
    )


def main():
    parser = argparse.ArgumentParser(description="posture threshold calibration")
    parser.add_argument("--upright", required=True)
    parser.add_argument("--slouch", required=True)
    args = parser.parse_args()

    print("[1/3] upright samples")
    up_hh, up_sl = collect_features(args.upright)
    describe("head_height", up_hh)
    describe("shoulder_level", up_sl)

    print("[2/3] slouch samples")
    sl_hh, sl_sl = collect_features(args.slouch)
    describe("head_height", sl_hh)
    describe("shoulder_level", sl_sl)

    print("[3/3] suggested thresholds")

    if len(up_hh) and len(sl_hh):
        up_p5 = np.percentile(up_hh, 5)
        sl_p95 = np.percentile(sl_hh, 95)
        print(f"  HEAD_HEIGHT_MIN    = {(up_p5 + sl_p95) / 2.0:.4f}")
        print(f"    (upright P5={up_p5:.4f}, slouch P95={sl_p95:.4f})")
        if up_p5 <= sl_p95:
            print("    [warn] distributions overlap, add samples or adjust camera angle")
    else:
        print("  HEAD_HEIGHT_MIN    = insufficient data")

    if len(up_sl) and len(sl_sl):
        up_p95 = np.percentile(up_sl, 95)
        sl_p5 = np.percentile(sl_sl, 5)
        print(f"  SHOULDER_LEVEL_MAX = {(up_p95 + sl_p5) / 2.0:.4f}")
        print(f"    (upright P95={up_p95:.4f}, slouch P5={sl_p5:.4f})")
        if up_p95 >= sl_p5:
            print("    [warn] distributions overlap, add samples or adjust camera angle")
    else:
        print("  SHOULDER_LEVEL_MAX = insufficient data")

    print("write values to .env and restart: sudo systemctl restart posture")


if __name__ == "__main__":
    main()
