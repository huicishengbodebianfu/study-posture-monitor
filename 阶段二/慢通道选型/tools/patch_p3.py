"""P3：primary 参与测速 + 软降级。

要点：启动探测用的是 1x1 小请求（max_tokens=8），而真实调用是 638 字符提示词 + 真图，
两者的耗时不可比。所以拆出 real_ewma / real_samples，降级只比较真实数据。
"""
import sys

path = sys.argv[1]
src = open(path, encoding="utf-8").read()


def sub(old, new, why=""):
    global src
    assert old in src, f"未匹配: {why or old[:60]}"
    src = src.replace(old, new, 1)


# ---------- 1. Backend 增加"真实调用"专用统计 ----------
sub(
    """    # 相对统计
    latency_ewma: float = 0.0""",
    """    # 真实调用（带完整提示词与真图）的 EWMA。
    # 启动探测用的是 1x1 小请求，耗时只有真实调用的 1/5，
    # 混在一起比较会把主用误判成"慢"——所以单独记一份。
    real_ewma: float = 0.0
    real_samples: int = 0
    # 相对统计
    latency_ewma: float = 0.0""",
    "Backend.real_ewma",
)

# ---------- 2. _record_latency 支持 real 标记 ----------
sub(
    '''    def _record_latency(self, be: Backend, seconds: float, ok: bool) -> None:
        """记录一次调用耗时（EWMA，alpha=0.3），失败也计入（按超时值惩罚）。"""
        with self._lock:
            alpha = 0.3
            if be.samples == 0:
                be.latency_ewma = seconds
            else:
                be.latency_ewma = alpha * seconds + (1 - alpha) * be.latency_ewma
            be.samples += 1''',
    '''    def _record_latency(self, be: Backend, seconds: float, ok: bool,
                        real: bool = True) -> None:
        """记录一次调用耗时（EWMA，alpha=0.3），失败也计入（按超时值惩罚）。

        real=False 表示这是启动探测（1x1 小请求），只进 latency_ewma，
        不进 real_ewma —— 否则会把"小请求很快"误当成"这个后端很快"。
        """
        with self._lock:
            alpha = 0.3
            if be.samples == 0:
                be.latency_ewma = seconds
            else:
                be.latency_ewma = alpha * seconds + (1 - alpha) * be.latency_ewma
            be.samples += 1
            if real:
                if be.real_samples == 0:
                    be.real_ewma = seconds
                else:
                    be.real_ewma = alpha * seconds + (1 - alpha) * be.real_ewma
                be.real_samples += 1''',
    "_record_latency real",
)

# ---------- 3. 启动探测：标记 real=False，并把 primary 也纳入 ----------
sub(
    """                ok = r.status_code == 200
                dt = time.time() - t0
                self._record_latency(be, dt if ok else (be.timeout or self.timeout), ok)""",
    """                ok = r.status_code == 200
                dt = time.time() - t0
                self._record_latency(be, dt if ok else (be.timeout or self.timeout), ok,
                                     real=False)""",
    "seed real=False",
)
sub(
    """                self._record_latency(be, be.timeout or self.timeout, False)
                log.warning("backup speed probe %s failed: %s (%.1fs)",""",
    """                self._record_latency(be, be.timeout or self.timeout, False, real=False)
                log.warning("backup speed probe %s failed: %s (%.1fs)",""",
    "seed 异常 real=False",
)
sub(
    """            for be in self.backends[1:]:""",
    """            for be in self.backends:""",
    "seed 纳入 primary",
)

# ---------- 4. speeds() 暴露真实速度 ----------
sub(
    """            return {b.name: {"ewma": round(b.latency_ewma, 2), "samples": b.samples,
                             "fail_streak": b.fail_streak} for b in self.backends}""",
    """            return {b.name: {"ewma": round(b.latency_ewma, 2), "samples": b.samples,
                             "real_ewma": round(b.real_ewma, 2),
                             "real_samples": b.real_samples,
                             "fail_streak": b.fail_streak} for b in self.backends}""",
    "speeds real",
)

# ---------- 5. __init__ 增加降级比例 ----------
sub(
    """        quota: Optional[QuotaLedger] = None,   # 配额台（P2）
    ):""",
    """        quota: Optional[QuotaLedger] = None,   # 配额台（P2）
        primary_demote_ratio: float = 1.6,  # P3：主用比最快备用慢这么多倍才降级；0=关闭
    ):""",
    "init 参数",
)
sub(
    """        self.quota = quota
        self._cursor = 0""",
    """        self.quota = quota
        self.primary_demote_ratio = primary_demote_ratio
        self._cursor = 0""",
    "init 赋值",
)

# ---------- 6. 降级判定助手 ----------
sub(
    """    def _quorum_reached(self, valid: list) -> bool:""",
    '''    @staticmethod
    def _fastest_real(backends: list, min_samples: int = 2) -> float:
        """这些后端里"真实调用"最快的一个（秒）；样本不足返回 0。"""
        vals = [b.real_ewma for b in backends
                if b.real_samples >= min_samples and b.real_ewma > 0]
        return min(vals) if vals else 0.0

    def _should_demote_primary(self, primary: "Backend", rest: list, avail: list) -> bool:
        """主用是否该"软降级"（不再保送名额，改为按速度竞争）。

        保守设计，四条同时满足才降级：
          1. 开关打开（primary_demote_ratio > 0）
          2. 主用可用，且**真实调用**样本 ≥2
          3. 至少 2 个备用也有真实样本（避免拿启动探测值当基准）
          4. 主用真实耗时 > 最快备用真实耗时 × ratio
        """
        if self.primary_demote_ratio <= 0 or primary not in avail:
            return False
        if primary.real_samples < 2 or primary.real_ewma <= 0:
            return False
        healthy = [b for b in rest if b.real_samples >= 2 and b.real_ewma > 0]
        if len(healthy) < 2:
            return False
        best = self._fastest_real(healthy)
        if best <= 0:
            return False
        return primary.real_ewma > best * self.primary_demote_ratio

    def _quorum_reached(self, valid: list) -> bool:''',
    "降级助手",
)

# ---------- 7. 选路：软降级 ----------
sub(
    """        cap = self.parallel_max if self.parallel_max > 0 else len(avail)
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
    """        cap = self.parallel_max if self.parallel_max > 0 else len(avail)
        primary = self.backends[0]
        rest = [b for b in avail if b is not primary]
        rest.sort(key=lambda b: (b.samples == 0, b.latency_ewma if b.samples else 0.0))

        # 主后端默认保送（它是质量基线：实测 0 端正误判）。
        # P3：只有它"真实调用"明显慢于最快备用（默认 1.6 倍）时，才取消保送、按速度竞争。
        demote = self._should_demote_primary(primary, rest, avail)
        if demote:
            log.warning("primary demoted: real_ewma=%.2fs > %.2f x best_backup=%.2fs",
                        primary.real_ewma, self.primary_demote_ratio,
                        self._fastest_real(rest))
            fill = ([primary] + rest) if primary in avail else list(rest)
            fill.sort(key=lambda b: (b.samples == 0, b.latency_ewma if b.samples else 0.0))
            targets, seen = [], set()
        else:
            targets = [primary] if primary in avail else []
            # 优先挑"不同账号"：账号按 key 指纹识别（P1），
            # 同一域名下的不同 key 也视为不同账号，各自有独立配额。
            seen = {self.account_of(primary)} if primary in avail else set()
            fill = rest

        for b in list(fill):
            if len(targets) >= cap:
                break
            acct = self.account_of(b)
            if acct in seen:
                continue
            targets.append(b)
            seen.add(acct)
        # 名额还有剩，再按速度补同账号的
        for b in fill:
            if len(targets) >= cap:
                break
            if b not in targets:
                targets.append(b)""",
    "选路降级",
)

open(path, "w", encoding="utf-8").write(src)
print("P3 已打入：real_ewma 拆分 / 启动探测纳入主用 / 软降级选路")
