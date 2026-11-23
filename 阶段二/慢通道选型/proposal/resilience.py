"""慢通道调用韧性层（Resilience Layer）。

一次性补齐 6 项能力：

  1. 能力探测   probe()            启动时 GET /models 校验模型存在，可选发 1 张 1x1 图验证视觉能力
  2. 指数退避   指数退避 + 抖动     失败后 0.8s / 1.6s / 3.2s，避免把限流打得更死
  3. 超时降级   单次硬超时          超时直接判失败，不写缓存，交给上层 TTL 过期后回退快速通道
  4. 429 切后端 状态码分流          429/5xx 立即换下一个后端，不原地重试
  5. 故障切换   熔断器             连续失败 N 次熔断 T 秒，全熔断时 fail-open 回退到未熔断者
  6. 坏例落盘   坏例落盘 JSON+JPG   最终失败时把「元信息 + 原图」写到 badcase 目录，供离线复盘

只依赖 requests，可脱离 FastAPI 单独单测（见 tools/test_resilience.py）。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger("posture.resilience")

# 不可重试：配置/鉴权/参数错误，重试只会浪费额度
PERMANENT_CODES = {400, 401, 403, 404, 405, 413, 415, 422}
# 可重试且应换后端：限流、上游故障
SWITCH_CODES = {429, 500, 502, 503, 504}


@dataclass
class Backend:
    """一个可调用的模型后端。"""

    name: str
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.1
    max_tokens: int = 800
    # 单后端超时覆盖。None = 用池子的默认值。
    # 用途：备用后端（如 intern-s2，实测 P95 19s）比主后端慢，不能共用同一个 12s。
    timeout: Optional[float] = None
    # 能力探测结果（None=未探测）
    supports_vision: Optional[bool] = None
    probe_error: str = ""
    # 测速：指数滑动平均延迟（秒）与样本数，用于"备用里挑最快"
    latency_ewma: float = 0.0
    samples: int = 0
    last_ok_ts: float = 0.0
    fail_streak: int = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def __str__(self) -> str:
        return f"{self.name}({self.model})"


@dataclass
class _BreakerState:
    fails: int = 0
    open_until: float = 0.0

    @property
    def is_open(self) -> bool:
        return time.time() < self.open_until


class CircuitBreaker:
    """极简熔断器：连续 fail_threshold 次失败 → 熔断 cooldown 秒。"""

    def __init__(self, threshold: int = 3, cooldown: float = 120.0):
        self.threshold = threshold
        self.cooldown = cooldown
        self._states = {}
        self._lock = threading.Lock()

    def allow(self, name: str) -> bool:
        with self._lock:
            st = self._states.get(name)
            return True if st is None else not st.is_open

    def success(self, name: str) -> None:
        with self._lock:
            self._states[name] = _BreakerState()

    def fail(self, name: str) -> bool:
        """记录一次失败；返回是否刚刚触发熔断。"""
        with self._lock:
            st = self._states.setdefault(name, _BreakerState())
            st.fails += 1
            if st.fails >= self.threshold and not st.is_open:
                st.open_until = time.time() + self.cooldown
                st.fails = 0
                return True
            return False

    def open_now(self, name: str) -> None:
        """立即熔断（用于鉴权失败这类"再打也没用"的配置级错误）。"""
        with self._lock:
            st = self._states.setdefault(name, _BreakerState())
            st.fails = 0
            st.open_until = time.time() + self.cooldown

    def snapshot(self) -> dict:
        with self._lock:
            return {k: {"fails": v.fails, "open_until": v.open_until} for k, v in self._states.items()}


class BadcaseStore:
    """坏例落盘：<dir>/YYYYMMDD/<ts>-<sha1>.json + .jpg。

    只保存失败样本，按天分目录，超过 max_files 自动删最旧，避免撑爆磁盘。
    """

    def __init__(self, root: str = "", max_files: int = 500, enabled: bool = True):
        self.root = root
        self.max_files = max_files
        self.enabled = bool(enabled and root)

    def save(self, image_bytes: bytes, meta: dict) -> str:
        if not self.enabled:
            return ""
        try:
            day = time.strftime("%Y%m%d")
            d = os.path.join(self.root, day)
            os.makedirs(d, exist_ok=True)
            h = hashlib_sha1(image_bytes)
            base = f"{int(time.time() * 1000)}-{h[:12]}"
            with open(os.path.join(d, base + ".jpg"), "wb") as f:
                f.write(image_bytes)
            with open(os.path.join(d, base + ".json"), "w", encoding="utf-8") as f:
                json.dump({**meta, "sha1": h, "ts": time.time(), "bytes": len(image_bytes)},
                          f, ensure_ascii=False, indent=1)
            self._trim(d)
            return os.path.join(d, base + ".json")
        except Exception as e:  # noqa: BLE001
            log.warning("badcase save failed: %s", e)
            return ""

    def _trim(self, d: str) -> None:
        try:
            files = sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".json"))
            for p in files[: max(0, len(files) - self.max_files)]:
                for ext in (".json", ".jpg"):
                    q = p[:-5] + ext
                    if os.path.exists(q):
                        os.remove(q)
        except Exception:  # noqa: BLE001
            pass


def hashlib_sha1(data: bytes) -> str:
    import hashlib
    return hashlib.sha1(data).hexdigest()


class VisionPool:
    """多后端视觉调用池：轮转 + 退避 + 熔断 + 坏例落盘。"""

    def __init__(
        self,
        backends: list[Backend],
        timeout: float = 20.0,
        max_tries: int = 3,
        backoff_base: float = 0.8,
        backoff_cap: float = 8.0,
        jitter: float = 0.2,
        breaker_threshold: int = 3,
        breaker_cooldown: float = 120.0,
        badcase: Optional[BadcaseStore] = None,
        # ---- 并发模式：多个模型同时跑，投票合并 ----
        parallel: bool = False,
        parallel_max: int = 0,        # 0 = 全部可用后端一起跑
        quorum: int = 2,              # 形成多数所需的最少同向票数
        parallel_deadline: float = 12.0,   # 并发总截止时间（秒）
        parallel_stagger: float = 0.15,    # 提交间隔，避免同网关瞬时 429
    ):
        self.backends = [b for b in backends if b.enabled]
        self.timeout = timeout
        self.max_tries = max_tries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.jitter = jitter
        self.breaker = CircuitBreaker(breaker_threshold, breaker_cooldown)
        self.badcase = badcase or BadcaseStore()
        self.parallel = parallel
        self.parallel_max = parallel_max
        self.quorum = quorum
        self.parallel_deadline = parallel_deadline
        self.parallel_stagger = parallel_stagger
        self._cursor = 0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.backends)

    # ---------------------------------------------------------------- 能力探测
    def probe(self, deep: bool = False) -> dict:
        """探测每个后端：模型是否存在（GET /models），可选验证视觉能力。

        返回 {backend_name: {"ok": bool, "models": [...], "detail": str}}
        """
        result = {}
        for be in self.backends:
            info = {"ok": False, "models": [], "detail": ""}
            try:
                r = requests.get(
                    f"{be.base_url.rstrip('/')}/models",
                    headers={"Authorization": f"Bearer {be.api_key}"},
                    timeout=min(self.timeout, 15),
                )
                if r.status_code != 200:
                    info["detail"] = f"HTTP {r.status_code}"
                    be.supports_vision = False
                else:
                    ids = [m.get("id") for m in (r.json().get("data") or []) if isinstance(m, dict)]
                    info["models"] = ids
                    if ids and be.model not in ids:
                        info["detail"] = f"model {be.model!r} not in catalog"
                        be.supports_vision = False
                    else:
                        info["ok"] = True
                        info["detail"] = f"{len(ids)} models" if ids else "catalog empty"
                        # 浅探：模型在目录里就算"可用"；深探会进一步验证收不收图
                        be.supports_vision = True
            except Exception as e:  # noqa: BLE001
                info["detail"] = f"{type(e).__name__}: {e}"
                be.supports_vision = False

            if deep and info["ok"]:
                ok_v, detail = self._probe_vision(be)
                info["ok"] = ok_v
                info["detail"] += f"; vision={'ok' if ok_v else detail}"
                be.supports_vision = ok_v
            be.probe_error = "" if info["ok"] else info["detail"]
            result[be.name] = info
        return result

    def _probe_vision(self, be: Backend) -> tuple[bool, str]:
        """发一张 1x1 白图，确认后端真的收得下 image_url。"""
        try:
            r = requests.post(
                f"{be.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {be.api_key}", "Content-Type": "application/json"},
                json={
                    "model": be.model,
                    "messages": [{"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": TINY_JPEG}},
                        {"type": "text", "text": "只回答 ok"},
                    ]}],
                    "temperature": 0,
                    "max_tokens": 8,
                },
                timeout=self.timeout,
            )
            if r.status_code == 200:
                return True, ""
            return False, f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            return False, type(e).__name__

    # ---------------------------------------------------------------- 选择后端
    def _pick(self, tried: set) -> Optional[Backend]:
        """选后端：backends[0] 是主用，其余是备用。

        - 主用可用 → 永远用主用（主备语义，不做轮转）
        - 主用不可用/已试过 → 在备用里挑**实测最快**的（EWMA 延迟最小）
        - 从未测过速度的备用排在最后，但仍可用（避免冷启动全部不可用）
        """
        with self._lock:
            avail = [b for b in self.backends
                     if b.name not in tried and self.breaker.allow(b.name)]
        if not avail:
            return None
        primary = self.backends[0] if self.backends else None
        if primary is not None and primary in avail:
            return primary
        # 备用：有测速数据的排前面，按 EWMA 升序；无数据的按定义顺序垫底
        avail.sort(key=lambda b: (b.samples == 0, b.latency_ewma if b.samples else 0.0))
        return avail[0]

    def _record_latency(self, be: Backend, seconds: float, ok: bool) -> None:
        """记录一次调用耗时（EWMA，alpha=0.3），失败也计入（按超时值惩罚）。"""
        with self._lock:
            alpha = 0.3
            if be.samples == 0:
                be.latency_ewma = seconds
            else:
                be.latency_ewma = alpha * seconds + (1 - alpha) * be.latency_ewma
            be.samples += 1
            if ok:
                be.last_ok_ts = time.time()
                be.fail_streak = 0
            else:
                be.fail_streak += 1

    def speeds(self) -> dict:
        """返回每个后端的测速快照，供日志/接口查看。"""
        with self._lock:
            return {b.name: {"ewma": round(b.latency_ewma, 2), "samples": b.samples,
                             "fail_streak": b.fail_streak} for b in self.backends}

    def seed_speeds(self, background: bool = True) -> None:
        """给每个备用后端测一次速度（各发一张 1x1 图），写入 EWMA。

        - 并发发出（串行会让启动后 40s 内全是"未测速"状态）
        - 只测备用；主用每次真实调用都会刷新自己的 EWMA
        - join 超时很短，绝不拖住启动
        """

        def _one(be):
            t0 = time.time()
            try:
                r = requests.post(
                    f"{be.base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {be.api_key}",
                             "Content-Type": "application/json"},
                    json={"model": be.model,
                          "messages": [{"role": "user", "content": [
                              {"type": "image_url", "image_url": {"url": TINY_JPEG}},
                              {"type": "text", "text": "只回答ok"}]}],
                          "temperature": 0, "max_tokens": 8},
                    timeout=be.timeout or self.timeout,
                )
                ok = r.status_code == 200
                dt = time.time() - t0
                self._record_latency(be, dt if ok else (be.timeout or self.timeout), ok)
                log.info("backup speed probe %s: %s %.2fs",
                         be.name, "ok" if ok else "HTTP %d" % r.status_code, dt)
            except Exception as e:  # noqa: BLE001
                self._record_latency(be, be.timeout or self.timeout, False)
                log.warning("backup speed probe %s failed: %s (%.1fs)",
                            be.name, type(e).__name__, time.time() - t0)

        def _run():
            ths = []
            for be in self.backends[1:]:
                th = threading.Thread(target=_one, args=(be,), daemon=True)
                th.start()
                ths.append(th)
                time.sleep(0.1)     # 错开，减少同网关瞬时 429
            for th in ths:
                th.join(timeout=1.5)

        if background:
            threading.Thread(target=_run, daemon=True).start()
        else:
            _run()

    def analyze(self, image_bytes: bytes, prompt: str) -> dict:
        """返回 {"ok", "content", "backend", "model", "attempts", "error", "latency", "badcase"}。

        parallel=True 时走"多模型并发 + 投票合并"，否则串行主备切换。
        """
        if self.parallel and len(self.backends) > 1:
            return self._analyze_parallel(image_bytes, prompt)
        return self._analyze_serial(image_bytes, prompt)

    def _analyze_serial(self, image_bytes: bytes, prompt: str) -> dict:
        if not self.enabled:
            return _fail("no backend configured", 0)

        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:image/jpeg;base64,{b64}"
        tried: set = set()
        attempts = 0
        last_err = ""
        t_start = time.time()

        for round_i in range(self.max_tries):
            be = self._pick(tried)
            if be is None:
                break
            tried.add(be.name)
            attempts += 1
            tb = time.time()

            payload = {
                "model": be.model,
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt},
                ]}],
                "temperature": be.temperature,
                "max_tokens": be.max_tokens,
            }

            try:
                be_timeout = be.timeout or self.timeout
                r = requests.post(
                    f"{be.base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {be.api_key}",
                             "Content-Type": "application/json"},
                    json=payload,
                    timeout=be_timeout,
                )
            except requests.Timeout:
                last_err = f"{be.name}: timeout>{be_timeout}s"
                self._record_latency(be, be_timeout, False)
                self._fail_backend(be, "timeout", image_bytes, prompt)
                log.warning("slow-path %s timeout (%.0fs)", be, be_timeout)
                self._backoff(round_i)
                continue
            except requests.RequestException as e:
                last_err = f"{be.name}: {type(e).__name__}"
                self._record_latency(be, time.time() - tb, False)
                self._fail_backend(be, f"{type(e).__name__}: {e}"[:200], image_bytes, prompt)
                self._backoff(round_i)
                continue

            code = r.status_code
            if code == 200:
                try:
                    msg = r.json()["choices"][0]["message"]
                    content = (msg.get("content") or "").strip()
                except (KeyError, IndexError, TypeError, ValueError) as e:
                    last_err = f"{be.name}: bad response shape ({type(e).__name__})"
                    self.breaker.fail(be.name)
                    self._record_latency(be, time.time() - tb, False)
                    self._fail_backend(be, last_err, image_bytes, prompt, code=code,
                                       body=r.text[:500])
                    continue
                if not content:
                    last_err = f"{be.name}: empty content"
                    # 空内容多半是推理吃光 token，换后端比原地重试更划算
                    self.breaker.fail(be.name)
                    self._record_latency(be, time.time() - tb, False)
                    self._fail_backend(be, last_err, image_bytes, prompt, code=code,
                                       body=r.text[:200])
                    self._backoff(round_i)
                    continue
                self.breaker.success(be.name)
                self._record_latency(be, time.time() - tb, True)
                return {
                    "ok": True, "content": content, "backend": be.name, "model": be.model,
                    "attempts": attempts, "error": "", "badcase": "",
                    "latency": round(time.time() - t_start, 2),
                }

            if code in PERMANENT_CODES:
                # 配置/鉴权错：**这台**再打也没用，立即熔断它，但必须继续试下一台
                # （典型场景：主后端 Key 被吊销，备用后端还能干活）
                last_err = f"{be.name}: HTTP {code} (permanent)"
                self.breaker.open_now(be.name)
                self._record_latency(be, time.time() - tb, False)
                self._fail_backend(be, last_err, image_bytes, prompt, code=code,
                                   body=r.text[:500])
                log.error("slow-path permanent failure on %s: HTTP %d -> switching backend",
                          be, code)
                continue

            # 429 / 5xx：换后端
            last_err = f"{be.name}: HTTP {code}"
            tripped = self.breaker.fail(be.name)
            self._record_latency(be, time.time() - tb, False)
            self._fail_backend(be, last_err, image_bytes, prompt, code=code, body=r.text[:500])
            log.warning("slow-path %s -> HTTP %d, switching backend%s",
                        be, code, " (breaker open)" if tripped else "")
            self._backoff(round_i)

        path = "" if attempts == 0 else ""
        return {
            "ok": False, "content": "", "backend": "", "model": "",
            "attempts": attempts, "error": last_err or "all backends unavailable",
            "badcase": path, "latency": round(time.time() - t_start, 2),
        }

    # ---------------------------------------------------------------- 并发模式
    def _call_one(self, be: "Backend", data_url: str, prompt: str, image_bytes: bytes) -> dict:
        """对单个后端发一次请求，返回结构化结果（不抛异常）。"""
        payload = {
            "model": be.model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": prompt},
            ]}],
            "temperature": be.temperature,
            "max_tokens": be.max_tokens,
        }
        be_timeout = be.timeout or self.timeout
        tb = time.time()
        try:
            r = requests.post(
                f"{be.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {be.api_key}",
                         "Content-Type": "application/json"},
                json=payload, timeout=be_timeout,
            )
        except Exception as e:  # noqa: BLE001
            self._record_latency(be, time.time() - tb, False)
            self.breaker.fail(be.name)
            self._fail_backend(be, f"{type(e).__name__}", image_bytes, prompt)
            return {"ok": False, "backend": be.name, "model": be.model,
                    "error": type(e).__name__, "latency": round(time.time() - tb, 2),
                    "json": None}

        if r.status_code != 200:
            self._record_latency(be, time.time() - tb, False)
            if r.status_code in PERMANENT_CODES:
                self.breaker.open_now(be.name)
            else:
                self.breaker.fail(be.name)
            self._fail_backend(be, f"HTTP {r.status_code}", image_bytes, prompt,
                               code=r.status_code, body=r.text[:300])
            return {"ok": False, "backend": be.name, "model": be.model,
                    "error": f"HTTP {r.status_code}", "latency": round(time.time() - tb, 2),
                    "json": None}

        try:
            content = (r.json()["choices"][0]["message"].get("content") or "").strip()
        except Exception:  # noqa: BLE001
            content = ""
        if not content:
            self._record_latency(be, time.time() - tb, False)
            self.breaker.fail(be.name)
            self._fail_backend(be, "empty content", image_bytes, prompt,
                               code=200, body=r.text[:200])
            return {"ok": False, "backend": be.name, "model": be.model,
                    "error": "empty content", "latency": round(time.time() - tb, 2),
                    "json": None}

        self.breaker.success(be.name)
        self._record_latency(be, time.time() - tb, True)
        return {"ok": True, "backend": be.name, "model": be.model, "error": "",
                "latency": round(time.time() - tb, 2), "content": content,
                "json": _extract_json(content)}

    def _analyze_parallel(self, image_bytes: bytes, prompt: str) -> dict:
        """多模型并发 + 投票。

        - 健康后端按 EWMA 从快到慢取前 N 个（parallel_max，0=全部）
        - 并发发出，最多等 parallel_deadline 秒
        - 对 upright 做多数票；票数达到 quorum 才算多数
        - 状态取"胜出阵营里置信度最高"的那个，输出仍是标准 JSON 文本
        """
        if not self.enabled:
            return _fail("no backend configured", 0)

        data_url = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")
        avail = [b for b in self.backends if self.breaker.allow(b.name)]
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
                targets.append(b)

        t0 = time.time()
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

        valid = [r for r in results if r["ok"] and isinstance(r["json"], dict)]
        if not valid:
            err = results[0]["error"] if results else "no result"
            return {"ok": False, "content": "", "backend": "", "model": "",
                    "attempts": len(results), "error": err, "badcase": "",
                    "latency": round(time.time() - t0, 2)}

        # ---- 投票：upright ----
        votes: dict = {}
        for r in valid:
            up = _norm_bool(r["json"].get("upright"))
            votes.setdefault(up, []).append(r)
        best_up, best_group = max(votes.items(), key=lambda kv: len(kv[1]))
        if len(best_group) < self.quorum:
            # 票数不够：退化为"置信度最高的单条"
            best_group = [max(valid, key=lambda r: _conf(r["json"]))]
            best_up = _norm_bool(best_group[0]["json"].get("upright"))

        winner = max(best_group, key=lambda r: _conf(r["json"]))
        merged = dict(winner["json"])
        merged["upright"] = best_up
        merged.setdefault("state", "unknown")
        # 投票情况写进 evidence，便于日志溯源
        detail = "|".join(f'{r["backend"]}:{r["json"].get("state")}/{r["json"].get("upright")}'
                          for r in valid)
        merged["evidence"] = detail[:60]

        return {
            "ok": True, "content": json.dumps(merged, ensure_ascii=False),
            "backend": f'vote{len(best_group)}/{len(valid)}',
            "model": ",".join(r["model"] for r in best_group),
            "attempts": len(results), "error": "", "badcase": "",
            "latency": round(time.time() - t0, 2),
        }

    # ---------------------------------------------------------------- 内部
    def _backoff(self, round_i: int) -> None:
        delay = min(self.backoff_cap, self.backoff_base * (2 ** round_i))
        delay += random.uniform(0, self.jitter)
        time.sleep(delay)

    def _fail_backend(self, be: Backend, err: str, image_bytes: bytes, prompt: str,
                      code: int = 0, body: str = "") -> None:
        meta = {
            "backend": be.name, "model": be.model, "base_url": be.base_url,
            "http_code": code, "error": err, "prompt_len": len(prompt),
            "body": body,
        }
        self.badcase.save(image_bytes, meta)


def _fail(err: str, attempts: int) -> dict:
    return {"ok": False, "content": "", "backend": "", "model": "",
            "attempts": attempts, "error": err, "badcase": "", "latency": 0.0}


# 1x1 白色 JPEG，用于能力探测
TINY_JPEG = (
    "data:image/jpeg;base64,"
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)


# ---------------------------------------------------------------- 并发投票辅助
def _extract_json(text: str):
    if not isinstance(text, str):
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s[:4].lower() == "json":
            s = s[4:]
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b <= a:
        return None
    try:
        obj = json.loads(s[a:b + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _norm_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "1"):
            return True
        if s in ("false", "no", "0"):
            return False
    return None


def _conf(obj: dict) -> float:
    try:
        return float(obj.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


