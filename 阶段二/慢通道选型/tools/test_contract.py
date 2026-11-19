r"""慢通道输出契约的模糊测试：任何垃圾输入都必须产出合法指令。

用法：
    python test_contract.py                      # 测 阶段二\慢通道选型\proposal\schema.py
    python test_contract.py <schema.py 所在目录>   # 测部署后的 cloud/ 目录
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(HERE), "proposal")
sys.path.insert(0, TARGET)

from schema import normalize, should_alert, VALID_STATES  # noqa: E402

FUZZ = [
    "", None, "not json", "{}",
    '{"state":["writting"]}',                       # 拼写错
    '{"conf":1.5}',                                 # 缺字段 + 越界置信度
    '{"state":["sleeping","phone_use","FAKE"]}',    # 多标签含脏值
    '```json\n{"state":"sleeping","confidence":0.9}\n```',
    '{"states":["sleeping"],"conf":0.9}',           # 别名
    '{"state":"phone","upright":"false","confidence":"0.8","alert":"true"}',
    '{"state":null,"upright":null}',
    '{"state":"slouch","confidence":0.95,"should_alert":false}',
    b"\x02\xff\x0a\x01",                            # 二进制垃圾
    '{"state": "phone_use", "confidence": 2.0}',    # state 别名 + 置信度越界
    '{"state":"head-down","confidence":0.7,"upright":"null"}',
]

fails = 0
for x in FUZZ:
    try:
        r = normalize(x)
    except Exception as e:                          # noqa: BLE001
        print(f"FAIL 抛出异常 {x!r}: {e}")
        fails += 1
        continue
    ok = (r["state"] in VALID_STATES
          and 0.0 <= r["confidence"] <= 1.0
          and isinstance(r["should_alert"], bool))
    fails += 0 if ok else 1
    print(f"{'OK  ' if ok else 'FAIL'} {str(x)[:44]:46s} -> {r['state']:8s} "
          f"up={str(r['upright']):5s} conf={r['confidence']}")

CASES = [
    # (state, upright, conf, model_alert, expect)
    ("slouch", False, 0.9, True,  True),
    ("slouch", False, 0.4, True,  False),   # 置信度不足
    ("sleep",  False, 0.9, True,  False),   # 趴桌只记录不吹
    ("away",   False, 0.9, True,  False),
    ("upright", True, 0.9, True,  False),
    ("phone",  False, 0.9, False, False),   # 模型自保：不提醒
    ("unknown", None, 0.9, True,  False),
]
for st, up, cf, ma, exp in CASES:
    got = should_alert(st, up, cf, ma)
    if got is not exp:
        fails += 1
        print(f"FAIL alert({st},{up},{cf},{ma}) -> {got}, 期望 {exp}")
    else:
        print(f"OK   alert({st:8s} up={str(up):5s} conf={cf} model={ma}) -> {got}")

print("\nALL PASS" if fails == 0 else f"\n{fails} FAILURES")
sys.exit(1 if fails else 0)
