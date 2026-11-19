"""P0/P1/P2 的单测：早停、账号化、配额台（不访问网络）。"""
import sys
import time

sys.path.insert(0, "/tmp/p0test")

from resilience import Backend, QuotaLedger, VisionPool  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print(f"{'OK  ' if cond else 'FAIL'} {name} {extra}")
    if not cond:
        ok = False


# ---------------- P1：同域名不同 key 必须是不同账号 ----------------
b1 = Backend("a", "https://x.com/v1", "key1", "m")
b2 = Backend("b", "https://x.com/v1", "key2", "m")
b3 = Backend("c", "https://y.com/v1", "key1", "m")
check("同域名不同 key → 不同账号", VisionPool.account_of(b1) != VisionPool.account_of(b2),
      f"{VisionPool.account_of(b1)} vs {VisionPool.account_of(b2)}")
check("不同域名 → 不同账号", VisionPool.account_of(b1) != VisionPool.account_of(b3))
check("显式 account 优先", VisionPool.account_of(Backend("d", "https://z.com", "k", "m", account="my-acct")) == "my-acct")

# ---------------- P2：配额台账 ----------------
qpath = "/tmp/p0test/q.json"
import os
if os.path.exists(qpath):
    os.remove(qpath)
acct = VisionPool.account_of(b1)
led = QuotaLedger(qpath, {acct: {"daily": 2, "rpm": 0}})
check("初始可用", led.allow(acct))
led.record(acct)
led.record(acct)
check("用完 2 次后拒绝", not led.allow(acct))
led2 = QuotaLedger(qpath, {acct: {"daily": 2, "rpm": 0}})
check("重载后仍拒绝（重启不清零）", not led2.allow(acct))
check("快照含剩余额度", led2.snapshot()[acct]["remaining"] == 0, str(led2.snapshot()[acct]))
# rpm
led3 = QuotaLedger("", {acct: {"daily": 0, "rpm": 2}})
led3.record(acct); led3.record(acct)
check("rpm 限流生效", not led3.allow(acct))

# ---------------- P0：早停 ----------------
def make_pool(delays, results):
    pool = VisionPool(
        [Backend(f"p{i+1}", f"https://h{i}.com/v1", f"k{i}", "m") for i in range(len(delays))],
        quorum=2, parallel=True, parallel_max=len(delays),
        parallel_deadline=10.0, parallel_stagger=0.0, quorum_grace=0.15,
    )

    def fake(be, data_url, prompt, image_bytes):
        time.sleep(delays[be.name])
        return {"ok": True, "backend": be.name, "model": be.model, "error": "",
                "latency": delays[be.name], "json": results[be.name]}

    pool._call_one = fake
    return pool


agree = {"state": "upright", "upright": True, "confidence": 0.9, "should_alert": False}
pool = make_pool({"p1": 0.2, "p2": 0.3, "p3": 5.0}, {"p1": agree, "p2": agree, "p3": agree})
t0 = time.time()
r = pool.analyze(b"x", "p")
dt = time.time() - t0
check("早停：两票一致后立即返回（p3 需 5s）", dt < 1.5, f"耗时 {dt:.2f}s backend={r['backend']}")
check("早停：结果仍是 vote2/2 以上", r["backend"].startswith("vote2") or r["backend"].startswith("vote3"), r["backend"])

# 无一致票时不应早停，要等 deadline（1.5s）
disagree = {"state": "slouch", "upright": False, "confidence": 0.9, "should_alert": True}
none_ = {"state": "unknown", "upright": None, "confidence": 0.5, "should_alert": False}
pool2 = VisionPool(
    [Backend(f"q{i+1}", f"https://k{i}.com/v1", f"kk{i}", "m") for i in range(3)],
    quorum=2, parallel=True, parallel_max=3,
    parallel_deadline=1.5, parallel_stagger=0.0, quorum_grace=0.15,
)
res2 = {"q1": agree, "q2": disagree, "q3": none_}
pool2._call_one = lambda be, du, pr, im: (time.sleep(0.1), {"ok": True, "backend": be.name,
    "model": be.model, "error": "", "latency": 0.1, "json": res2[be.name]})[1]
t0 = time.time()
r2 = pool2.analyze(b"x", "p")
dt2 = time.time() - t0
check("无 quorum 时退化取最高置信度", r2["ok"] and r2["backend"].startswith("vote1"), f"{r2['backend']} {dt2:.2f}s")

print("\n" + ("ALL PASS" if ok else "有失败项"))
sys.exit(0 if ok else 1)
