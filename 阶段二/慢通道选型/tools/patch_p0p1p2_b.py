"""P0/P1/P2 第二阶段：选路账号化 + 配额过滤 + 早停。

用法： python patch_p0p1p2_b.py <resilience.py 路径>
"""
import sys

path = sys.argv[1]
src = open(path, encoding="utf-8").read()


def sub(old, new, why=""):
    global src
    assert old in src, f"未匹配: {why or old[:60]}"
    src = src.replace(old, new, 1)


# ---------------------------------------------------------------- 1. 选路：账号化 + 配额
sub(
    """        avail = [b for b in self.backends if self.breaker.allow(b.name)]
        if not avail:
            return _fail("all backends unavailable", 0)
        cap = self.parallel_max if self.parallel_max > 0 else len(avail)
        # 主后端必选（它是质量基线）；剩余名额按实测速度补位
        primary = self.backends[0]
        targets = [primary] if primary in avail else []
        rest = [b for b in avail if b is not primary]
        rest.sort(key=lambda b: (b.samples == 0, b.latency_ewma if b.samples else 0.0))

        # 优先挑"不同端点/账号"：多个账号各自有独立配额，落在同一账号上会互相抢限流
        seen = {primary.base_url} if primary in avail else set()
        for b in list(rest):
            if len(targets) >= cap:
                break
            if b.base_url in seen:
                continue
            targets.append(b)
            seen.add(b.base_url)
        # 名额还有剩，再按速度补同账号的
        for b in rest:
            if len(targets) >= cap:
                break
            if b not in targets:
                targets.append(b)""",
    """        # 熔断 + 配额（P2）双重过滤：额度耗尽的账号直接跳过，不浪费一次必然 429 的调用
        avail = [b for b in self.backends if self.breaker.allow(b.name)]
        avail = [b for b in avail if self._quota_ok(b)]
        if not avail:
            return _fail("all backends unavailable", 0)
        cap = self.parallel_max if self.parallel_max > 0 else len(avail)
        # 主后端必选（它是质量基线）；剩余名额按实测速度补位
        primary = self.backends[0]
        targets = [primary] if primary in avail else []
        rest = [b for b in avail if b is not primary]
        rest.sort(key=lambda b: (b.samples == 0, b.latency_ewma if b.samples else 0.0))

        # 优先挑"不同账号"：账号按 key 指纹识别（P1），
        # 同一域名下的不同 key 也视为不同账号，各自有独立配额。
        seen = {self.account_of(primary)} if primary in avail else set()
        for b in list(rest):
            if len(targets) >= cap:
                break
            acct = self.account_of(b)
            if acct in seen:
                continue
            targets.append(b)
            seen.add(acct)
        # 名额还有剩，再按速度补同账号的
        for b in rest:
            if len(targets) >= cap:
                break
            if b not in targets:
                targets.append(b)""",
    "选路账号化",
)

# ---------------------------------------------------------------- 2. 早停（P0）
sub(
    """        t0 = time.time()
        results = []
        # 注意：不能用 with ThreadPoolExecutor，它退出时会 join 所有线程，
        # 慢后端会把整体拖过 deadline（实测 12s deadline 实际跑了 16.2s）。
        ex = ThreadPoolExecutor(max_workers=len(targets))
        try:
            futs = []
            for i, b in enumerate(targets):
                if i:
                    # 错开 150ms 提交，避免同一网关瞬时并发触发 429
                    time.sleep(self.parallel_stagger)
                futs.append(ex.submit(self._call_one, b, data_url, prompt, image_bytes))
            done, not_done = wait(futs, timeout=self.parallel_deadline)
            for f in done:
                try:
                    results.append(f.result())
                except Exception:  # noqa: BLE001
                    pass
            for f in not_done:
                f.cancel()
        finally:
            # wait=False：立刻返回，慢后端在后台线程里自然结束
            ex.shutdown(wait=False, cancel_futures=True)
""",
    """        t0 = time.time()
        results = []
        # 注意：不能用 with ThreadPoolExecutor，它退出时会 join 所有线程，
        # 慢后端会把整体拖过 deadline（实测 12s deadline 实际跑了 16.2s）。
        ex = ThreadPoolExecutor(max_workers=len(targets))
        try:
            futs = []
            for i, b in enumerate(targets):
                if i:
                    # 错开 150ms 提交，避免同一网关瞬时并发触发 429
                    time.sleep(self.parallel_stagger)
                futs.append(ex.submit(self._call_one, b, data_url, prompt, image_bytes))

            # ---- 早停（P0）----
            # 原来 wait() 只在"全部完成或超时"返回，两票早就一致了也得陪跑满 15 秒。
            # 实测中位延迟 11.95s，而 vote2/2 只需两票——白白等第三路。
            # 现在：够票 + 宽限 quorum_grace 就收工。
            deadline = t0 + self.parallel_deadline
            pending = set(futs)
            grace_until = None
            while pending:
                now = time.time()
                limit_ts = deadline if grace_until is None else min(deadline, grace_until)
                left = limit_ts - now
                if left <= 0:
                    break
                done, pending = wait(pending, timeout=min(0.4, left),
                                     return_when=FIRST_COMPLETED)
                for f in done:
                    try:
                        results.append(f.result())
                    except Exception:  # noqa: BLE001
                        pass
                if grace_until is None:
                    valid_now = [r for r in results
                                 if r["ok"] and isinstance(r["json"], dict)]
                    if self._quorum_reached(valid_now):
                        grace_until = time.time() + self.quorum_grace
            for f in pending:
                f.cancel()
        finally:
            # wait=False：立刻返回，慢后端在后台线程里自然结束
            ex.shutdown(wait=False, cancel_futures=True)
""",
    "早停",
)

# ---------------------------------------------------------------- 3. _quorum_reached 助手
sub(
    """    # ---------------------------------------------------------------- 账号""",
    """    def _quorum_reached(self, valid: list) -> bool:
        \"\"\"当前结果是否已经形成 quorum 多数（用于早停）。\"\"\"
        if len(valid) < self.quorum:
            return False
        tally: dict = {}
        for r in valid:
            k = _norm_bool(r["json"].get("upright"))
            tally[k] = tally.get(k, 0) + 1
        return any(v >= self.quorum for v in tally.values())

    # ---------------------------------------------------------------- 账号""",
    "_quorum_reached",
)

open(path, "w", encoding="utf-8").write(src)
print("阶段二完成：选路账号化 + 配额过滤 + 早停")
