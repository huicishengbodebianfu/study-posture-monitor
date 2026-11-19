"""P3 单测：primary 软降级 + real_ewma 与启动探测值分离（不访问网络）。"""
import sys
import time

sys.path.insert(0, "/tmp/p3test")

from resilience import Backend, VisionPool  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print(f"{'OK  ' if cond else 'FAIL'} {name} {extra}")
    if not cond:
        ok = False


def mkpool(ratio=1.6):
    bs = [
        Backend("primary", "https://p.com/v1", "kp", "m"),
        Backend("f1", "https://a.com/v1", "k1", "m"),
        Backend("f2", "https://b.com/v1", "k2", "m"),
        Backend("f3", "https://c.com/v1", "k3", "m"),
    ]
    return VisionPool(bs, quorum=2, parallel=True, parallel_max=3,
                      parallel_deadline=1.0, parallel_stagger=0.0,
                      primary_demote_ratio=ratio)


def set_speed(be, ewma, n, real=True):
    be.latency_ewma = ewma
    be.samples = n
    if real:
        be.real_ewma = ewma
        be.real_samples = n


# ---------- 1. 启动探测值不可作为降级依据 ----------
p = mkpool()
prim = p.backends[0]
# 备用只有"小请求"探测值（real_samples=0），主用有真实值
set_speed(prim, 8.0, 5, real=True)
for b in p.backends[1:]:
    b.latency_ewma = 0.7
    b.samples = 1
    b.real_ewma = 0.0
    b.real_samples = 0          # 关键：没有真实样本
avail = list(p.backends)
check("备用无真实样本 -> 不降级（探测值不可比）",
      not p._should_demote_primary(prim, p.backends[1:], avail))

# ---------- 2. 主用确实慢 -> 降级 ----------
p = mkpool()
prim = p.backends[0]
set_speed(prim, 9.0, 5)
for i, b in enumerate(p.backends[1:]):
    set_speed(b, 1.0 + i * 0.1, 3)      # 真实 1.0~1.2s
check("主用 9s > 1.6 × 1.0s -> 降级",
      p._should_demote_primary(prim, p.backends[1:], list(p.backends)))

# ---------- 3. 主用只是稍慢（未超 1.6 倍）-> 不降级 ----------
p = mkpool()
prim = p.backends[0]
set_speed(prim, 1.5, 5)
for i, b in enumerate(p.backends[1:]):
    set_speed(b, 1.0 + i * 0.1, 3)
check("主用 1.5s < 1.6 × 1.0s -> 不降级",
      not p._should_demote_primary(prim, p.backends[1:], list(p.backends)))

# ---------- 4. 样本不足 -> 不降级 ----------
p = mkpool()
prim = p.backends[0]
set_speed(prim, 9.0, 1)                  # 只有 1 个真实样本
for i, b in enumerate(p.backends[1:]):
    set_speed(b, 1.0, 3)
check("主用真实样本不足(1) -> 不降级",
      not p._should_demote_primary(prim, p.backends[1:], list(p.backends)))

p = mkpool()
prim = p.backends[0]
set_speed(prim, 9.0, 5)
set_speed(p.backends[1], 1.0, 3)
set_speed(p.backends[2], 1.1, 1)         # 只有 1 个健康备用（第3个样本不够）
set_speed(p.backends[3], 1.2, 1)
check("健康备用不足 2 个 -> 不降级",
      not p._should_demote_primary(prim, p.backends[1:], list(p.backends)))

# ---------- 5. 开关关闭 ----------
p = mkpool(ratio=0)
prim = p.backends[0]
set_speed(prim, 99.0, 5)
for i, b in enumerate(p.backends[1:]):
    set_speed(b, 1.0, 3)
check("ratio=0 关闭降级 -> 永远保送",
      not p._should_demote_primary(prim, p.backends[1:], list(p.backends)))

# ---------- 6. 选路实测：降级后 primary 不再白占名额 ----------
p = mkpool()
prim = p.backends[0]
set_speed(prim, 9.0, 5)
set_speed(p.backends[1], 1.0, 3)
set_speed(p.backends[2], 1.1, 3)
set_speed(p.backends[3], 1.2, 3)


called = []


def fake(be, du, pr, im):
    called.append(be.name)
    return {"ok": True, "backend": be.name, "model": be.model, "error": "",
            "latency": 0.1, "json": {"state": "upright", "upright": True,
                                     "confidence": 0.9, "should_alert": False}}


p._call_one = fake
r = p.analyze(b"x", "prompt")
check("降级后：primary 不在目标里", "primary" not in called, f"实际调用={called}")
check("降级后：仍凑满 3 路", len(called) == 3, f"实际调用={called}")

# 不降级时 primary 必须在
called2 = []
p2 = mkpool()
prim2 = p2.backends[0]
set_speed(prim2, 1.0, 5)
for i, b in enumerate(p2.backends[1:]):
    set_speed(b, 1.0 + i * 0.1, 3)


def fake2(be, du, pr, im):
    called2.append(be.name)
    return fake(be, du, pr, im)


p2._call_one = fake2
p2.analyze(b"x", "prompt")
check("不降级时：primary 必在目标里", "primary" in called2, f"实际调用={called2}")
check("不降级时：primary 是第一个", called2 and called2[0] == "primary", f"实际调用={called2}")

print("\n" + ("ALL PASS" if ok else "有失败项"))
sys.exit(0 if ok else 1)
