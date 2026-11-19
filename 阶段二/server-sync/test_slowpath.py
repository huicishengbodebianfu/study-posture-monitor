#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""慢通道加固逻辑单元测试（不触发任何网络请求）。

覆盖交付包方案里三处关键改动：
  1. 结果缓存 TTL —— 过期即视为"没有慢通道结果"
  2. fuse() 尊重 should_alert —— 模型说"不该提醒"时不翻案
  3. single_call 模式的 ready 判定 —— 只需要一个 Key

用法（在 /opt/posture 下）：
    ./venv/bin/python test_slowpath.py
退出码 0 = 全部通过。
"""
from __future__ import annotations

import sys
import time

from multimodal import MultimodalDetector, MultimodalResult
from schema import should_alert as schema_should_alert

fails = 0


def check(name: str, got, expect) -> None:
    global fails
    ok = got == expect
    if not ok:
        fails += 1
    print(f"{'OK  ' if ok else 'FAIL'} {name:52s} got={got!r:18s} expect={expect!r}")


def make_detector(**kw) -> MultimodalDetector:
    params = dict(
        enabled=True,
        qwen_api_key="fake-key-for-unit-test",
        single_call=True,
        result_ttl_seconds=90,
        min_confidence=0.6,
    )
    params.update(kw)
    return MultimodalDetector(**params)


def inject(d: MultimodalDetector, upright, state, alert, age=0.0) -> None:
    """直接往缓存里塞一个结果，模拟慢通道已经跑过一次。"""
    d._last_result = MultimodalResult(
        upright=upright, state=state, should_alert=alert,
        source="slow", timestamp=time.time() - age,
    )


print("=" * 78)
print("  慢通道加固逻辑单元测试")
print("=" * 78)

# ---------------- 1. TTL ----------------
print("\n[1] 结果缓存 TTL")
d = make_detector(result_ttl_seconds=90)

inject(d, False, "slouch", True, age=10)
check("10 秒前的缓存未过期 -> 读得到", d.latest() is not None, True)

inject(d, False, "slouch", True, age=200)
check("200 秒前的缓存已过期 -> 读不到", d.latest() is None, True)

d._last_result = None
check("空缓存 -> None", d.latest() is None, True)

# 过期后 fuse 必须回退快速通道行为
inject(d, False, "slouch", True, age=500)
f = d.fuse(None, "no_person")
check("过期缓存的 slouch 不再影响融合", f.upright, None)

# ---------------- 2. fuse 尊重 should_alert ----------------
print("\n[2] fuse() 翻案门控（关键修复）")
d = make_detector()

inject(d, False, "slouch", True, age=5)
check("慢通道 slouch + should_alert=True -> 翻案不端正",
      d.fuse(None, "no_person").upright, False)

inject(d, False, "slouch", False, age=5)
check("慢通道 slouch + should_alert=False -> 不翻案（无法判定）",
      d.fuse(None, "no_person").upright, None)

inject(d, False, "slouch", False, age=5)
check("快速通道明确端正时，慢通道不翻案 -> 端正",
      d.fuse(True, "fast_path_ok").upright, True)

inject(d, True, "upright", False, age=5)
check("慢通道 upright -> 端正", d.fuse(None, "no_person").upright, True)

d._last_result = None
check("无慢通道结果 + 快速通道不端正 -> 不端正", d.fuse(False, "fast_path_bad").upright, False)

inject(d, False, "phone", True, age=5)
r = d.fuse(None, "no_person")
check("慢通道 phone -> 状态透传", r.state, "phone")
check("慢通道 phone -> source=slow", r.source, "slow")

# ---------------- 3. 模式与 ready ----------------
print("\n[3] 单次调用 / 两段式 模式")
d1 = make_detector(single_call=True)
check("single_call=True 只需一个 Key -> ready", d1.ready, True)

d2 = make_detector(single_call=False, deepseek_api_key="")
check("single_call=False 且缺 AI-2 Key -> 未就绪", d2.ready, False)

d3 = make_detector(single_call=False, deepseek_api_key="fake")
check("single_call=False 且两个 Key 都有 -> ready", d3.ready, True)

d4 = make_detector(enabled=False)
check("总开关关闭 -> 未就绪", d4.ready, False)
check("总开关关闭 -> should_run 为假", d4.should_run(), False)

# ---------------- 4. should_alert 统一判定点 ----------------
print("\n[4] schema.should_alert 兜底规则")
check("slouch + conf 0.9 + model_alert -> True",
      schema_should_alert("slouch", False, 0.9, True, 0.6), True)
check("slouch + conf 0.4 < 阈值 -> False",
      schema_should_alert("slouch", False, 0.4, True, 0.6), False)
check("sleep 只记录不吹 -> False",
      schema_should_alert("sleep", False, 0.9, True, 0.6), False)
check("unknown -> False",
      schema_should_alert("unknown", None, 0.9, True, 0.6), False)
check("模型自己说别提醒 -> False",
      schema_should_alert("slouch", False, 0.9, False, 0.6), False)

print("\n" + "=" * 78)
if fails == 0:
    print("  ALL PASS")
    sys.exit(0)
print(f"  {fails} FAILURES")
sys.exit(1)
