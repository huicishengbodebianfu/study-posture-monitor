"""用线上同一份代码/提示词，串行跑一批图，落盘 JSONL。

与生产差异（测试专用）：
  - 跳过 run_async 的 30s 调度门闩
  - 每次调用前清空熔断器状态：批量 100 张必然触发限流，
    熔断器会挡住后续请求，导致测不到模型质量。生产里 30s 一次不会撞限流。
  - 串行 + 固定间隔，避免把限流打得更死
"""
import json
import os
import sys
import time

sys.path.insert(0, "/opt/posture")

import app  # noqa: E402
from ai_providers import QwenVLProvider  # noqa: E402

DIR = sys.argv[1] if len(sys.argv) > 1 else "/tmp/raw100"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/raw100_results.jsonl"
DELAY = float(sys.argv[3]) if len(sys.argv) > 3 else 1.2

provider: QwenVLProvider = app.multimodal.qwen
files = sorted(f for f in os.listdir(DIR) if f.lower().endswith(".jpg"))
print(f"images={len(files)} delay={DELAY}s", flush=True)

open(OUT, "w").close()
for i, name in enumerate(files, 1):
    with open(os.path.join(DIR, name), "rb") as f:
        data = f.read()

    rec = {"image": name, "ok": False, "error": ""}
    for attempt in range(3):
        # 测试专用：绕过熔断器，否则一次限流会挡住后续几十张
        provider.pool.breaker._states.clear()
        t0 = time.time()
        d = provider.analyze(data)
        rec = {
            "image": name, "ok": bool(d.ok), "state": d.state, "upright": d.upright,
            "confidence": d.confidence, "should_alert": d.should_alert,
            "needs_review": d.needs_review, "latency": round(time.time() - t0, 2),
            "error": d.error, "raw": (d.raw or "")[:200], "attempt": attempt + 1,
        }
        if d.ok:
            break
        if "429" in (d.error or ""):
            time.sleep(8)
        else:
            time.sleep(2)

    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if i % 10 == 0:
        print(f"  {i}/{len(files)}", flush=True)
    time.sleep(DELAY)

print("done", len(files), "->", OUT)
