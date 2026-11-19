"""给 resilience.py 打 P0+P1+P2 补丁：早停、账号化、配额台账。

用法： python patch_p0p1p2.py <resilience.py 路径>
"""
import sys

path = sys.argv[1]
src = open(path, encoding="utf-8").read()


def sub(old, new, why=""):
    global src
    assert old in src, f"未匹配: {why or old[:60]}"
    src = src.replace(old, new, 1)


# ---------------------------------------------------------------- 0. imports
sub(
    "from concurrent.futures import ThreadPoolExecutor, wait",
    "import hashlib\nfrom concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait",
    "imports",
)

# ---------------------------------------------------------------- 1. Backend.account (P1)
sub(
    """    # 测速：指数滑动平均延迟（秒）与样本数，用于"备用里挑最快"
    latency_ewma: float = 0.0""",
    """    # 账号标识（P1）。空 = 用 "域名::key指纹" 自动推导。
    # 同一域名下的不同 key 通常配额独立，按域名去重会浪费掉可用额度。
    account: str = ""
    # 相对统计
    latency_ewma: float = 0.0""",
    "Backend.account",
)


# ---------------------------------------------------------------- 2. QuotaLedger (P2)
LEDGER = '''

class QuotaLedger:
    """按"账号"记账的配额台（P2）。

    两个维度：
      - daily：每自然日上限（免费档 50 次/天这类）
      - rpm  ：每分钟上限（20 次/分这类）

    关键点：**计数落盘**，重启不清零——重启很频繁，内存计数等于没有。
    """

    def __init__(self, path: str = "", limits: Optional[dict] = None):
        self.path = path
        self.limits = limits or {}          # {account: {"daily": n, "rpm": n}}
        self._lock = threading.Lock()
        self.day = time.strftime("%Y%m%d")
        self.daily: dict = {}               # {account: 当日已用}
        self.minute: dict = {}              # {account: [分钟戳, 该分钟内已用]}
        self.skipped: dict = {}             # {account: 因配额跳过的次数}
        self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("day") == self.day:
                self.daily = d.get("daily") or {}
                self.minute = d.get("minute") or {}
                self.skipped = d.get("skipped") or {}
        except Exception:  # noqa: BLE001
            pass

    def _save(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"day": self.day, "daily": self.daily,
                           "minute": self.minute, "skipped": self.skipped}, f)
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001
            pass

    def _rollover(self) -> None:
        today = time.strftime("%Y%m%d")
        if today != self.day:
            self.day = today
            self.daily = {}
            self.minute = {}
            self.skipped = {}

    # ---- 查询 ----
    def limits_of(self, account: str) -> tuple:
        lim = self.limits.get(account) or {}
        return int(lim.get("daily") or 0), int(lim.get("rpm") or 0)

    def allow(self, account: str) -> bool:
        with self._lock:
            self._rollover()
            daily_lim, rpm_lim = self.limits_of(account)
            if daily_lim and self.daily.get(account, 0) >= daily_lim:
                return False
            if rpm_lim:
                stamp, cnt = self.minute.get(account, [0, 0])
                now_min = int(time.time() // 60)
                if stamp == now_min and cnt >= rpm_lim:
                    return False
            return True

    def skip(self, account: str) -> None:
        with self._lock:
            self.skipped[account] = self.skipped.get(account, 0) + 1

    def record(self, account: str) -> None:
        with self._lock:
            self._rollover()
            self.daily[account] = self.daily.get(account, 0) + 1
            now_min = int(time.time() // 60)
            stamp, cnt = self.minute.get(account, [0, 0])
            self.minute[account] = [now_min, cnt + 1] if stamp == now_min else [now_min, 1]
            self._save()

    def snapshot(self) -> dict:
        with self._lock:
            out = {}
            for acct, val in self.daily.items():
                daily_lim, rpm_lim = self.limits_of(acct)
                out[acct] = {
                    "used_today": val,
                    "daily_limit": daily_lim,
                    "remaining": (daily_lim - val) if daily_lim else None,
                    "rpm_limit": rpm_lim,
                    "quota_skipped": self.skipped.get(acct, 0),
                }
            return out
'''

sub("\nclass VisionPool:", LEDGER + "\nclass VisionPool:", "QuotaLedger 插入")

# ---------------------------------------------------------------- 3. 构造函数
sub(
    """        parallel_stagger: float = 0.15,    # 提交间隔，避免同网关瞬时 429
    ):""",
    """        parallel_stagger: float = 0.15,    # 提交间隔，避免同网关瞬时 429
        quorum_grace: float = 0.15,        # 够票后再等这么久收尾（P0）
        quota: Optional[QuotaLedger] = None,   # 配额台（P2）
    ):""",
    "__init__ 参数",
)
sub(
    """        self.parallel_stagger = parallel_stagger
        self._cursor = 0""",
    """        self.parallel_stagger = parallel_stagger
        self.quorum_grace = quorum_grace
        self.quota = quota
        self._cursor = 0""",
    "__init__ 赋值",
)

# ---------------------------------------------------------------- 4. 账号标识 + 配额过滤（P1/P2）
HELPER = '''
    # ---------------------------------------------------------------- 账号
    @staticmethod
    def account_of(be: "Backend") -> str:
        """账号标识：显式配置优先，否则 "域名::key指纹"。

        同一域名下的不同 key 往往配额独立，按域名去重会把 9 个账号压成 5 个用。
        """
        if be.account:
            return be.account
        host = be.base_url.replace("https://", "").replace("http://", "").split("/")[0]
        fp = hashlib.sha1((be.api_key or "").encode("utf-8")).hexdigest()[:6]
        return f"{host}::{fp}"

    def _quota_ok(self, be: "Backend") -> bool:
        if self.quota is None:
            return True
        acct = self.account_of(be)
        if self.quota.allow(acct):
            return True
        self.quota.skip(acct)
        return False

    @property
    def _cursor_unused(self) -> int:  # 占位，避免误删
        return self._cursor
'''

sub(
    """    # ---------------------------------------------------------------- 能力探测""",
    HELPER + """
    # ---------------------------------------------------------------- 能力探测""",
    "账号助手插入",
)

# ---------------------------------------------------------------- 5. _pick 串行：加配额过滤
sub(
    """        with self._lock:
            avail = [b for b in self.backends
                     if b.name not in tried and self.breaker.allow(b.name)]""",
    """        with self._lock:
            avail = [b for b in self.backends
                     if b.name not in tried and self.breaker.allow(b.name)]
        avail = [b for b in avail if self._quota_ok(b)]""",
    "_pick 配额过滤",
)

open(path, "w", encoding="utf-8").write(src)
print("阶段一完成：imports / Backend.account / QuotaLedger / __init__ / account_of / _pick")
