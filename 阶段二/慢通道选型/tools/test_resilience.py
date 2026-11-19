"""韧性层单测：6 项能力逐条验证（不发真实请求，全部 mock）。"""
import json
import os
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
# 本地布局是 tools/ 与 proposal/ 平级；部署到服务器后两个文件同在 /opt/posture
for cand in (os.path.join(os.path.dirname(HERE), "proposal"), HERE, "/opt/posture"):
    if os.path.exists(os.path.join(cand, "resilience.py")):
        sys.path.insert(0, cand)
        break

try:
    import requests  # noqa: E402
except ModuleNotFoundError:                     # 本机精简 Python 没有 requests，注入桩
    requests = types.ModuleType("requests")

    class _Timeout(Exception):
        pass

    class _RequestException(Exception):
        pass

    requests.Timeout = _Timeout
    requests.RequestException = _RequestException
    requests.post = None
    requests.get = None
    sys.modules["requests"] = requests

import resilience as R  # noqa: E402

OK_BODY = {"choices": [{"message": {"content": '{"state":"slouch","upright":false,"confidence":0.9}'}}]}
fail_count = 0


def check(name, got, expect):
    global fail_count
    ok = got == expect
    fail_count += 0 if ok else 1
    print(f"{'OK  ' if ok else 'FAIL'} {name:52s} got={got!r} expect={expect!r}")


def mk_backends(n=2, name_prefix="b"):
    return [
        R.Backend(name=f"{name_prefix}{i}", base_url=f"http://{name_prefix}{i}.test/v1",
                  api_key="k", model=f"m{i}")
        for i in range(1, n + 1)
    ]


class Resp:
    def __init__(self, code, body=None, text=""):
        self.status_code = code
        self._body = body
        self.text = text or (json.dumps(body) if body else "")

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


# ---------------------------------------------------------------- 1. 能力探测
calls = {"get": [], "post": []}


def fake_get(url, headers=None, timeout=None):
    calls["get"].append(url)
    if "b1.test" in url:
        return Resp(200, {"data": [{"id": "m1"}, {"id": "m9"}]})
    return Resp(404, None, "not found")


R.requests.get = fake_get
pool = R.VisionPool(mk_backends(2), badcase=R.BadcaseStore(enabled=False))
probe = pool.probe()
check("能力探测：模型存在 -> ok", probe["b1"]["ok"], True)
check("能力探测：模型不存在 -> 标记不支持", pool.backends[1].supports_vision, False)

# ---------------------------------------------------------------- 2/4/5. 退避 / 429 切后端 / 熔断
seq = []


def fake_post_429_then_ok(url, headers=None, json=None, timeout=None):
    seq.append(("post", url))
    if "b1.test" in url:
        return Resp(429, {"error": "rate limit"}, '{"error":"rate limit"}')
    return Resp(200, OK_BODY)


backoff_sleeps = []
R.time.sleep = lambda s: backoff_sleeps.append(round(s, 2))
R.requests.post = fake_post_429_then_ok

pool = R.VisionPool(mk_backends(2), backoff_base=0.8, jitter=0,
                    badcase=R.BadcaseStore(enabled=False))
r = pool.analyze(b"\xff\xd8fake", "prompt")
check("429 自动切后端 -> 第二台成功", r["ok"], True)
check("429 自动切后端 -> backend=b2", r["backend"], "b2")
check("429 自动切后端 -> 用了 2 次尝试", r["attempts"], 2)
check("指数退避 -> 记录下来了一次 sleep", len(backoff_sleeps) >= 1, True)
check("指数退避 -> 首轮延迟≈0.8s", backoff_sleeps[0] if backoff_sleeps else None, 0.8)

# ---------------------------------------------------------------- 3. 超时降级
def fake_post_timeout(url, headers=None, json=None, timeout=None):
    raise requests.Timeout("read timed out")


R.requests.post = fake_post_timeout
backoff_sleeps.clear()
pool = R.VisionPool(mk_backends(2), max_tries=2, jitter=0,
                    badcase=R.BadcaseStore(enabled=False))
r = pool.analyze(b"\xff\xd8fake", "prompt")
check("超时降级 -> 整体失败且不抛异常", r["ok"], False)
check("超时降级 -> error 含 timeout", "timeout" in r["error"], True)

# ---------------------------------------------------------------- 4c. 空 content：必须换后端，不能原地重试
def fake_post_empty_then_ok(url, headers=None, json=None, timeout=None):
    if "b1.test" in url:
        return Resp(200, {"choices": [{"message": {"content": ""}}]})
    return Resp(200, OK_BODY)


R.requests.post = fake_post_empty_then_ok
backoff_sleeps.clear()
pool = R.VisionPool(mk_backends(2), max_tries=2, jitter=0,
                    badcase=R.BadcaseStore(enabled=False))
r = pool.analyze(b"\xff\xd8fake", "prompt")
check("空 content -> 切到备用后端并成功", r["ok"], True)
check("空 content -> backend=b2", r["backend"], "b2")

# ---------------------------------------------------------------- 4b. 永久错误：只熔断该后端，必须继续换后端
def fake_post_401(url, headers=None, json=None, timeout=None):
    if "b1.test" in url:
        return Resp(401, {"error": "bad key"}, '{"error":"bad key"}')
    return Resp(200, OK_BODY)


R.requests.post = fake_post_401
backoff_sleeps.clear()
pool = R.VisionPool(mk_backends(2), max_tries=2, jitter=0,
                    badcase=R.BadcaseStore(enabled=False))
r = pool.analyze(b"\xff\xd8fake", "prompt")
check("401 主后端 -> 切到备用后端并成功", r["ok"], True)
check("401 主后端 -> backend=b2", r["backend"], "b2")
check("401 主后端 -> 立即熔断 b1", pool.breaker.allow("b1"), False)
check("401 主后端 -> 不空转退避", backoff_sleeps, [])

# ---------------------------------------------------------------- 5. 熔断
def fake_post_500(url, headers=None, json=None, timeout=None):
    return Resp(500, {"error": "boom"}, "boom")


R.requests.post = fake_post_500
backoff_sleeps.clear()
pool = R.VisionPool(mk_backends(2), max_tries=2, jitter=0,
                    breaker_threshold=2, breaker_cooldown=60,
                    badcase=R.BadcaseStore(enabled=False))
for _ in range(2):
    pool.analyze(b"\xff\xd8fake", "prompt")
check("熔断 -> b1 打开", pool.breaker.allow("b1"), False)
check("熔断 -> b2 也打开", pool.breaker.allow("b2"), False)
r = pool.analyze(b"\xff\xd8fake", "prompt")
check("全熔断 -> 直接失败不浪费调用", r["attempts"], 0)

# ---------------------------------------------------------------- 6. 坏例落盘
tmp = tempfile.mkdtemp(prefix="badcase-")
R.requests.post = fake_post_500
pool = R.VisionPool(mk_backends(1), max_tries=1, jitter=0,
                    badcase=R.BadcaseStore(root=tmp, max_files=10))
pool.analyze(b"\xff\xd8IMAGEDATA", "prompt")
day = os.listdir(tmp)[0]
files = sorted(os.listdir(os.path.join(tmp, day)))
check("坏例落盘 -> 生成了 json+jpg 两个文件", len(files), 2)
meta = json.load(open(os.path.join(tmp, day, [f for f in files if f.endswith('.json')][0]), encoding="utf-8"))
check("坏例落盘 -> 记录了 http_code", meta["http_code"], 500)
check("坏例落盘 -> 记录了 backend", meta["backend"], "b1")
check("坏例落盘 -> 记录了 sha1", len(meta["sha1"]), 40)
jpg = [f for f in files if f.endswith(".jpg")][0]
check("坏例落盘 -> 原图字节完整", open(os.path.join(tmp, day, jpg), "rb").read(), b"\xff\xd8IMAGEDATA")

# ---------------------------------------------------------------- 7. 正常路径不受影响
R.requests.post = fake_post_429_then_ok
R.time.sleep = lambda s: None
pool = R.VisionPool(mk_backends(2), jitter=0, badcase=R.BadcaseStore(enabled=False))
r = pool.analyze(b"\xff\xd8fake", "prompt")
check("正常路径 -> 返回模型原文", r["content"][:12], '{"state":"sl')


# ---------------------------------------------------------------- 8. 并发 + 投票
def fake_post_vote(url, headers=None, json=None, timeout=None):
    if "b1.test" in url:      # 端正
        return Resp(200, {"choices": [{"message": {"content":
            '{"state":"upright","upright":true,"confidence":0.9,"should_alert":false}'}}]})
    if "b2.test" in url:      # 低头
        return Resp(200, {"choices": [{"message": {"content":
            '{"state":"slouch","upright":false,"confidence":0.8,"should_alert":true}'}}]})
    return Resp(200, {"choices": [{"message": {"content":
        '{"state":"slouch","upright":false,"confidence":0.7,"should_alert":true}'}}]})  # b3 也低头


R.requests.post = fake_post_vote
pool = R.VisionPool(mk_backends(3), parallel=True, quorum=2, parallel_deadline=5,
                    badcase=R.BadcaseStore(enabled=False))
r = pool.analyze(b"\xff\xd8fake", "prompt")
import json as _json
merged = _json.loads(r["content"])
check("并发：OK", r["ok"], True)
check("并发：多数票 upright=False", merged["upright"], False)
check("并发：state 取胜出阵营", merged["state"], "slouch")
check("并发：backend 标注票数", r["backend"].startswith("vote"), True)
check("并发：3 台都参与", r["attempts"], 3)

# 票数不足时退化为最高置信度
pool2 = R.VisionPool(mk_backends(3), parallel=True, quorum=3, parallel_deadline=5,
                     badcase=R.BadcaseStore(enabled=False))
r2 = pool2.analyze(b"\xff\xd8fake", "prompt")
m2 = _json.loads(r2["content"])
check("并发：票数不足 -> 取最高置信度(upright)", m2["upright"], True)

# 慢后端不拖住整体
import threading as _th
slow_started = _th.Event()


def fake_post_slow(url, headers=None, json=None, timeout=None):
    if "b1.test" in url:
        slow_started.set()
        time.sleep(3)          # 超过 deadline
        return Resp(200, {"choices": [{"message": {"content": '{"state":"upright","upright":true,"confidence":0.99}'}}]})
    return Resp(200, {"choices": [{"message": {"content": '{"state":"slouch","upright":false,"confidence":0.8}'}}]})


R.requests.post = fake_post_slow
pool3 = R.VisionPool(mk_backends(2), parallel=True, quorum=1, parallel_deadline=0.8,
                     badcase=R.BadcaseStore(enabled=False))
t0 = time.time()
r3 = pool3.analyze(b"\xff\xd8fake", "prompt")
elapsed = time.time() - t0
check("并发：慢后端被 deadline 截断(<2s)", elapsed < 2.0, True)
check("并发：仍有有效结果", r3["ok"], True)
print("\nALL PASS" if fail_count == 0 else f"\n{fail_count} FAILURES")
sys.exit(1 if fail_count else 0)


