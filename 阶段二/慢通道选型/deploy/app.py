from __future__ import annotations

import os
import logging

import cv2
import numpy as np
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse

from posture import (  # noqa: E402
    NOSE, L_SHOULDER, R_SHOULDER,
    Thresholds, RuleEngine, extract_features, is_upright,
)
from multimodal import MultimodalDetector

DEVICE_KEY = os.environ.get("DEVICE_KEY", "change-me-to-a-random-secret")
MODEL_DIR = os.environ.get("MODEL_DIR", "yolov8n-pose_openvino_model")
CONF_THRESHOLD = float(os.environ.get("CONF_THRESHOLD", "0.5"))
# 方向可疑阈值：趴桌一般只到 -0.1，倒置的端正帧会到 -0.3 以下
RETRY_HH = float(os.environ.get("RETRY_HH", "-0.25"))
HEAD_HEIGHT_MIN = float(os.environ.get("HEAD_HEIGHT_MIN", "0.15"))
SHOULDER_LEVEL_MAX = float(os.environ.get("SHOULDER_LEVEL_MAX", "0.05"))

# ---- 多态检测 · 慢通道 ----
# 默认关闭。开启后按 MULTIMODAL_INTERVAL 周期在后台线程抽查一帧，
# 结果写进带 TTL 的缓存，/detect 只读缓存（模型永不进请求路径）。
MULTIMODAL_ENABLED = os.environ.get("MULTIMODAL_ENABLED", "false").lower() == "true"
MULTIMODAL_INTERVAL = int(os.environ.get("MULTIMODAL_INTERVAL", "30"))

# 慢通道模型（单次调用方案）。变量名沿用 AI_* ，与交付包方案文档一致。
AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
AI_API_KEY = os.environ.get("AI_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "qwen-vl-plus")
AI_TEMPERATURE = float(os.environ.get("AI_TEMPERATURE", "0.1"))
AI_MAX_TOKENS = int(os.environ.get("AI_MAX_TOKENS", "800"))

# 兼容旧的两段式配置（single_call=false 时才用得到）
QWEN_API_KEY = os.environ.get("QWEN_API_KEY", "") or AI_API_KEY
QWEN_MODEL = os.environ.get("QWEN_MODEL", "") or AI_MODEL
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
AI_SINGLE_CALL = os.environ.get("AI_SINGLE_CALL", "true").lower() == "true"

# 结果缓存有效期（须 >= INTERVAL，建议 3 倍）与翻案最低置信度
MULTIMODAL_TTL = int(os.environ.get("MULTIMODAL_TTL", "90"))
MULTIMODAL_MIN_CONF = float(os.environ.get("MULTIMODAL_MIN_CONF", "0.6"))
# 慢通道否决权门槛：慢通道明确端正且置信度达此值时，可推翻快速通道的不端正
SLOW_OVERRIDE_CONF = float(os.environ.get("SLOW_OVERRIDE_CONF", "0.9"))
# 否决权护栏：快通道连续 N 帧判"不端正"后，禁止慢通道翻案（0=关闭）
SLOW_OVERRIDE_GUARD_FRAMES = int(os.environ.get("SLOW_OVERRIDE_GUARD_FRAMES", "3"))
# 规则引擎坏帧保留窗口（秒）：两帧坏帧之间夹"无法判定"不清零，超过此窗口才过期
RULE_BAD_HOLD_SECONDS = float(os.environ.get("RULE_BAD_HOLD_SECONDS", "60"))
# 慢通道后端"观望期"(秒):连续失败被降级后,间隔这么久允许试一次(0=关闭)
AI_PROBATION_SECONDS = float(os.environ.get("AI_PROBATION_SECONDS", "900"))

# 单次调用硬超时与重试次数。
# 约束：一次抽查的最坏耗时（主后端超时 + 退避 + 备用后端超时）必须小于
# MULTIMODAL_INTERVAL，否则会跨过两个周期（run_async 的 _running 互斥会把下一个挡掉）。
AI_TIMEOUT = float(os.environ.get("AI_TIMEOUT", "12"))
AI_MAX_TRIES = int(os.environ.get("AI_MAX_TRIES", "2"))
AI_BACKOFF_BASE = float(os.environ.get("AI_BACKOFF_BASE", "0.8"))
AI_FALLBACK1_TIMEOUT = float(os.environ.get("AI_FALLBACK1_TIMEOUT", "16"))
AI_FALLBACK2_TIMEOUT = float(os.environ.get("AI_FALLBACK2_TIMEOUT", "10"))
AI_FALLBACK3_TIMEOUT = float(os.environ.get("AI_FALLBACK3_TIMEOUT", "10"))
AI_FALLBACK4_TIMEOUT = float(os.environ.get("AI_FALLBACK4_TIMEOUT", "10"))
AI_FALLBACK5_TIMEOUT = float(os.environ.get("AI_FALLBACK5_TIMEOUT", "10"))
AI_FALLBACK6_TIMEOUT = float(os.environ.get("AI_FALLBACK6_TIMEOUT", "12"))
AI_FALLBACK7_TIMEOUT = float(os.environ.get("AI_FALLBACK7_TIMEOUT", "12"))
AI_FALLBACK8_TIMEOUT = float(os.environ.get("AI_FALLBACK8_TIMEOUT", "12"))

# ---- 并发模式：多个视觉模型同时跑，投票合并 ----
AI_PARALLEL = os.environ.get("AI_PARALLEL", "false").lower() == "true"
AI_PARALLEL_MAX = int(os.environ.get("AI_PARALLEL_MAX", "0"))      # 0 = 全部可用后端
AI_QUORUM = int(os.environ.get("AI_QUORUM", "2"))
AI_PARALLEL_DEADLINE = float(os.environ.get("AI_PARALLEL_DEADLINE", "12"))
# 可以有多个备用；最坏耗时按"最慢的那个备用"算（每轮只挑一个备用）
_FALLBACK_TIMEOUTS = [
    float(os.environ.get(f"AI_FALLBACK{i}_TIMEOUT", "16" if i == 1 else "10"))
    for i in range(1, 21) if os.environ.get(f"AI_FALLBACK{i}_BASE")
]
if AI_PARALLEL:
    # 并发模式：整次分析被 deadline 卡住，与备用数量无关
    AI_WORST_CASE = AI_PARALLEL_DEADLINE
else:
    AI_WORST_CASE = AI_TIMEOUT + (
        (AI_BACKOFF_BASE + max(_FALLBACK_TIMEOUTS)) if _FALLBACK_TIMEOUTS else 0.0
    )
assert AI_WORST_CASE <= MULTIMODAL_INTERVAL, (
    f"最坏耗时 {AI_WORST_CASE:.1f}s（主 {AI_TIMEOUT}s + 退避 {AI_BACKOFF_BASE}s + "
    f"备用 {AI_FALLBACK1_TIMEOUT}s）超过抽查周期 {MULTIMODAL_INTERVAL}s"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("posture")

# ---- 韧性池：指数退避 / 429 切后端 / 熔断 / 坏例落盘 / 能力探测 ----
from resilience import Backend, BadcaseStore, QuotaLedger, VisionPool  # noqa: E402

_BACKENDS = [Backend("primary", AI_BASE_URL, AI_API_KEY, AI_MODEL,
                     AI_TEMPERATURE, AI_MAX_TOKENS)]
for _i in range(1, 21):
    _tmo = float(os.environ.get(f"AI_FALLBACK{_i}_TIMEOUT", "10"))
    _base = os.environ.get(f"AI_FALLBACK{_i}_BASE")
    if not _base:
        continue
    _BACKENDS.append(Backend(
        f"fallback{_i}",
        _base,
        os.environ.get(f"AI_FALLBACK{_i}_KEY", ""),
        os.environ.get(f"AI_FALLBACK{_i}_MODEL", ""),
        AI_TEMPERATURE,
        # 推理型模型（如 OpenRouter 上的 nemotron-3.5-super-vl）思考会吃掉大量 token，
        # 需要单独放大，否则 content 会被截空。
        int(os.environ.get(f"AI_FALLBACK{_i}_MAXTOKENS", AI_MAX_TOKENS)),
        timeout=_tmo,
        account=os.environ.get(f"AI_FALLBACK{_i}_ACCOUNT", ""),
    ))

# ---- 配额台（P2）：按账号记账，落盘，重启不清零 ----
QUOTA_FILE = os.environ.get("AI_QUOTA_FILE", "/var/lib/posture/quota.json")
_quota_limits: dict = {}
for _be in _BACKENDS:
    _tag = _be.name.upper()
    _daily = int(os.environ.get(f"AI_{_tag}_DAILY_LIMIT", "0") or 0)
    _rpm = int(os.environ.get(f"AI_{_tag}_RPM_LIMIT", "0") or 0)
    if _daily or _rpm:
        _quota_limits[VisionPool.account_of(_be)] = {"daily": _daily, "rpm": _rpm}
quota_ledger = QuotaLedger(QUOTA_FILE, _quota_limits)
if _quota_limits:
    log.info("quota limits: %s", _quota_limits)

vision_pool = VisionPool(
    _BACKENDS,
    timeout=AI_TIMEOUT,
    max_tries=AI_MAX_TRIES,
    backoff_base=AI_BACKOFF_BASE,
    breaker_threshold=int(os.environ.get("AI_BREAKER_THRESHOLD", "3")),
    breaker_cooldown=float(os.environ.get("AI_BREAKER_COOLDOWN", "120")),
    badcase=BadcaseStore(os.environ.get("AI_BADCASE_DIR", ""), max_files=500),
    parallel=AI_PARALLEL,
    parallel_max=AI_PARALLEL_MAX,
    quorum=AI_QUORUM,
    parallel_deadline=AI_PARALLEL_DEADLINE,
    quorum_grace=float(os.environ.get("AI_QUORUM_GRACE", "0.15")),
    quota=quota_ledger,
    # P3：主用比最快备用慢这么多倍才取消保送；0 = 关闭降级（永远保送主用）
    primary_demote_ratio=float(os.environ.get("AI_PRIMARY_DEMOTE_RATIO", "1.6")),
    # P0-C 补充:连续失败的后端多久允许"试一次";0 = 关闭观望期
    probation_seconds=AI_PROBATION_SECONDS,
)

app = FastAPI(title="posture-monitor", version="1.1")

thresholds = Thresholds(
    head_height_min=HEAD_HEIGHT_MIN,
    shoulder_level_max=SHOULDER_LEVEL_MAX,
)
rule_engine = RuleEngine(consecutive_frames=2, dedup_seconds=60,
                         bad_hold_seconds=RULE_BAD_HOLD_SECONDS)

multimodal = MultimodalDetector(
    enabled=MULTIMODAL_ENABLED,
    qwen_api_key=QWEN_API_KEY,
    qwen_model=QWEN_MODEL,
    deepseek_api_key=DEEPSEEK_API_KEY,
    deepseek_model=DEEPSEEK_MODEL,
    interval_seconds=MULTIMODAL_INTERVAL,
    base_url=AI_BASE_URL,
    temperature=AI_TEMPERATURE,
    max_tokens=AI_MAX_TOKENS,
    result_ttl_seconds=MULTIMODAL_TTL,
    min_confidence=MULTIMODAL_MIN_CONF,
    slow_override_min_conf=SLOW_OVERRIDE_CONF,
    override_guard_frames=SLOW_OVERRIDE_GUARD_FRAMES,
    timeout=AI_TIMEOUT,
    # 重试已交给 vision_pool（跨后端），这里必须为 1，否则 1×2 = 2 次重试变成 4 次调用
    max_tries=1,
    single_call=AI_SINGLE_CALL,
    pool=vision_pool,
)

_ov_model = None
# 相机方向提示：None=未知, True=倒装(需转180°), False=正装
_flip_hint = None


def load_model():
    global _ov_model
    if _ov_model is not None:
        return _ov_model

    import openvino as ov

    if not os.path.isdir(MODEL_DIR):
        raise RuntimeError(f"model dir not found: {MODEL_DIR}")

    core = ov.Core()
    model = core.read_model(os.path.join(MODEL_DIR, "yolov8n-pose.xml"))
    _ov_model = core.compile_model(model, "CPU")
    log.info("model loaded: %s", MODEL_DIR)
    return _ov_model


def preprocess(image_bytes: bytes, input_size=(640, 640), rotate180: bool = False):
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("decode jpeg failed")
    # 相机装配方向可能上下颠倒（实测 GOOUUU S3-CAM 就是倒的）。
    # 这里只做几何修正，不改变任何相机参数。
    if rotate180:
        img = cv2.rotate(img, cv2.ROTATE_180)

    orig_h, orig_w = img.shape[:2]
    target_w, target_h = input_size

    scale = min(target_w / orig_w, target_h / orig_h)
    new_w, new_h = int(round(orig_w * scale)), int(round(orig_h * scale))
    resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

    canvas = np.full((target_h, target_w, 3), 114, dtype=np.uint8)
    pad_x = (target_w - new_w) // 2
    pad_y = (target_h - new_h) // 2
    canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized

    rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
    blob = rgb.astype(np.float32) / 255.0
    blob = np.transpose(blob, (2, 0, 1))[None, ...]

    # 画面统计：没人时用来区分"空座位"还是"镜头被挡/朝墙/太暗"
    # 边缘密度是最有效的判据：空教室有桌椅纹理(≈0.09)，被挡/朝墙近乎无纹理(≤0.02)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 60, 160)
    # 感知哈希：关键点无效（看不清头）时，用它作为"这一帧"的身份，
    # 让慢通道结果能判断"还是不是刚才那个画面"。
    small = cv2.resize(gray, (16, 16), interpolation=cv2.INTER_AREA)
    ahash = bytes((small > small.mean()).astype(np.uint8).flatten())
    stats = (float(gray.mean()), float(gray.std()), float((edges > 0).mean()), ahash)

    return blob, scale, pad_x, pad_y, orig_w, orig_h, stats


def _raw_hh(keypoints):
    """用 letterbox 坐标粗算 head_height（比值，与缩放无关），用于判断方向是否反了。"""
    if keypoints is None:
        return None
    try:
        sw = abs(float(keypoints[L_SHOULDER][0]) - float(keypoints[R_SHOULDER][0]))
        if sw < 1e-6:
            return None
        mid_y = (float(keypoints[L_SHOULDER][1]) + float(keypoints[R_SHOULDER][1])) / 2.0
        return (mid_y - float(keypoints[NOSE][1])) / sw
    except (TypeError, IndexError, ValueError):
        return None


def classify_scene(stats, person_detected: bool) -> str:
    """三分类：有人 / 空座位 / 看不清。

    快速通道认不到人时无法自证"真的没人"——镜头被挡住、关灯、画面全白
    同样会认不到人。用亮度统计把这两种情况分开，OLED 才能说对。
    """
    if person_detected:
        return "person"
    mean, std, edge, _hash = stats
    if mean < 45:                 # 极暗：关灯 / 镜头被完全遮住
        return "occluded"
    if std < 8:                   # 近乎纯色
        return "occluded"
    if edge < 0.02:               # 近乎无纹理：手挡镜头 / 朝天花板 / 朝墙壁
        return "occluded"
    return "empty"


# 状态 -> OLED 显示码（短码给固件，中文给调试）
DISPLAY_CODES = {
    "upright": ("UP", "端正"),
    "slouch": ("SL", "低头"),
    "lean": ("LEAN", "侧歪"),
    "phone": ("PHONE", "手机"),
    "daze": ("DAZE", "走神"),
    "sleep": ("SLEEP", "趴桌"),
    "away": ("AWAY", "无人"),
    "unknown": ("DIM", "看不清"),
}


def display_for(scene: str, state: str, upright) -> tuple:
    """返回 (code, 中文)。场景优先于状态：没人/看不清时不显示姿态。"""
    if scene == "empty":
        return ("AWAY", "无人")
    if scene == "occluded":
        return ("DIM", "看不清")
    code, cn = DISPLAY_CODES.get(state, ("??", "未知"))
    if upright is True and code in ("??", "DIM"):
        return ("UP", "端正")
    return (code, cn)


def infer(blob):
    model = load_model()
    pred = model([blob])[0][0]  # (56, 8400)

    scores = pred[4, :]
    best_idx = int(np.argmax(scores))
    if float(scores[best_idx]) < CONF_THRESHOLD:
        return None, None

    kp = pred[5:, best_idx].reshape(17, 3)
    return kp[:, :2], kp[:, 2]


def unletterbox(keypoints, scale, pad_x, pad_y):
    kp = keypoints.copy()
    kp[:, 0] = (kp[:, 0] - pad_x) / scale
    kp[:, 1] = (kp[:, 1] - pad_y) / scale
    return kp


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/pool")
def pool_stats(x_device_key: str = Header(default="")):
    """只读：查看慢通道选路状态（需设备密钥，避免对外泄露后端清单）。"""
    if x_device_key != DEVICE_KEY:
        raise HTTPException(status_code=401, detail="invalid device key")
    speeds = vision_pool.speeds()
    rows = []
    for be in vision_pool.backends:
        s = speeds.get(be.name, {})
        rows.append({
            "name": be.name,
            "model": be.model,
            "account": vision_pool.account_of(be),
            "ewma": s.get("ewma", 0.0),
            "samples": s.get("samples", 0),
            "real_ewma": s.get("real_ewma", 0.0),
            "real_samples": s.get("real_samples", 0),
            "fail_streak": s.get("fail_streak", 0),
            "breaker_open": not vision_pool.breaker.allow(be.name),
            "timeout": be.timeout,
            "max_tokens": be.max_tokens,
        })
    quota = quota_ledger.snapshot() if quota_ledger is not None else {}
    return {
        "parallel": AI_PARALLEL,
        "primary_demote_ratio": vision_pool.primary_demote_ratio,
        "parallel_max": AI_PARALLEL_MAX,
        "quorum": AI_QUORUM,
        "deadline": AI_PARALLEL_DEADLINE,
        "backends": rows,
        "quota": quota,
    }


@app.post("/detect")
async def detect(request: Request, x_device_key: str = Header(default="")):
    global _flip_hint
    if x_device_key != DEVICE_KEY:
        log.warning("bad device key: %s", x_device_key)
        raise HTTPException(status_code=401, detail="invalid device key")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty body")

    try:
        # ---- 方向自动纠正 ----
        # 相机可能倒装。判断依据不只是"能否检出人"，还要看几何是否合理：
        # 方向反了时鼻子会跑到肩膀下面（head_height 大幅为负）。
        # 注意：两个方向都检不到人时**不能**锁定方向——旧实现会锁成 False，
        # 之后所有倒置帧都会被错误地按正向处理（实测导致端正帧 hh=-0.32）。
        cur_rot = _flip_hint is True
        blob, scale, pad_x, pad_y, ow, _oh, stats = preprocess(body, rotate180=cur_rot)
        keypoints, kp_conf = infer(blob)
        used_flip = cur_rot
        _hh = _raw_hh(keypoints)

        _suspicious = keypoints is None or (_hh is not None and _hh < RETRY_HH)
        if _suspicious:
            alt_rot = not cur_rot
            b2, s2, px2, py2, ow2, oh2, st2 = preprocess(body, rotate180=alt_rot)
            kp2, cf2 = infer(b2)
            hh2 = _raw_hh(kp2)
            _better = (kp2 is not None) and (hh2 is None or _hh is None or hh2 > _hh)
            if _better:
                blob, scale, pad_x, pad_y, ow, _oh, stats = b2, s2, px2, py2, ow2, oh2, st2  # noqa: F841
                keypoints, kp_conf = kp2, cf2
                used_flip = alt_rot
                _flip_hint = alt_rot
                log.info("auto-rotate: 改用 %s 方向（hh %.2f -> %.2f）",
                         "180°翻转" if alt_rot else "正向", _hh or 0.0, hh2 or 0.0)
            elif keypoints is not None:
                _flip_hint = used_flip
            # 两个方向都认不到人 → 不改变方向提示（旧实现在这里错误地锁成 False）
        else:
            _flip_hint = used_flip
    except Exception as e:
        log.exception("inference failed")
        raise HTTPException(status_code=500, detail=str(e))

    # 慢通道要拿到"和快通道看到的同一方向"的图。
    # 否则同一条帧，快通道按翻转后判定，慢通道却看倒置图，两者结论会打架。
    if used_flip:
        _img = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_COLOR)
        if _img is not None:
            _ok, _buf = cv2.imencode(".jpg", cv2.rotate(_img, cv2.ROTATE_180),
                                     [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            if _ok:
                body = _buf.tobytes()

    if keypoints is None:
        # 快速通道连人都没找到 → 触发慢通道 + 用缓存兜底
        # 这条日志不能省：否则"真没人"和"模型没检出人"在运维侧完全无法区分
        log.info("no_person: keypoints=None flip=%s", used_flip)
        # 没有几何指纹，但有图像指纹：慢通道的结论仍可通过图像比对参与融合
        multimodal.run_async(body, frame_key=None,
                             hint="快速通道未检测到人体关键点。",
                             img_hash=stats[3])
        fused = multimodal.fuse(None, "no_person", frame_key=None, img_hash=stats[3])
        action = rule_engine.update(fused.upright)
        scene = classify_scene(stats, False)
        code, cn = display_for(scene, fused.state, fused.upright)
        return JSONResponse({
            "v": 2,
            "action": action,
            "upright": fused.upright,
            "state": fused.state,
            "source": fused.source,
            "reason": fused.reason,
            # ---- 供 OLED 直接显示 ----
            "person": False,
            "scene": scene,          # empty=空座位 / occluded=遮挡或太暗
            "display": code,         # AWAY / DIM
            "display_cn": cn,        # 无人 / 看不清
            "head_height": None,
            "shoulder_level": None,
        })

    keypoints = unletterbox(keypoints, scale, pad_x, pad_y)
    # 传入原图宽度：肩宽的相对阈值保护，防止比值爆炸（曾出现 head_height=167）
    features = extract_features(keypoints, kp_conf, image_width=ow)
    fast_upright = is_upright(features, thresholds)

    # ---- 慢通道：非阻塞触发，并把"这一帧的特征指纹"一起带进去 ----
    # 帧指纹：特征有效用几何指纹，无效（看不清头）用图像感知哈希
    _phash = stats[3]
    _fkey = ((features.head_height, features.shoulder_level) if features.valid
             else ("img", _phash))
    # 关键点不可靠时的线索：趴桌时鼻子必然不可见，快通道算不出 head_height，
    # 但它仍然检出了人体。把这个事实告诉慢通道，避免它把"头看不见"当成"没人"。
    _hint = ""
    if not features.valid:
        _hint = ("快速通道检测到人体，但头部/肩膀关键点不可靠——可能是低头、趴桌或背对镜头。"
                 "请优先考虑 sleep 或 slouch，不要因为看不到脸就判 away。")
    multimodal.run_async(body, frame_key=_fkey, hint=_hint, img_hash=_phash)

    # ---- 融合快速通道 + 慢速通道 ----
    fused = multimodal.fuse(fast_upright, features.reason,
                            frame_key=_fkey, img_hash=_phash)
    action = rule_engine.update(fused.upright)

    log.info(
        "head_height=%.3f shoulder_level=%.3f valid=%s reason=%s fast=%s fused=%s "
        "state=%s source=%s action=%s",
        features.head_height, features.shoulder_level,
        features.valid, features.reason or "-",
        fast_upright, fused.upright, fused.state, fused.source, action,
    )

    scene = classify_scene(stats, True)
    code, cn = display_for(scene, fused.state, fused.upright)
    return JSONResponse({
        "v": 2,
        "action": action,
        "upright": fused.upright,
        "state": fused.state,
        "source": fused.source,
        "head_height": round(float(features.head_height), 4),
        "shoulder_level": round(float(features.shoulder_level), 4),
        "reason": fused.reason or features.reason,
        # ---- 供 OLED 直接显示 ----
        "person": True,
        "scene": scene,
        "display": code,
        "display_cn": cn,
    })


@app.on_event("startup")
def on_startup():
    log.info("startup, thresholds: head_height_min=%.3f shoulder_level_max=%.3f",
             thresholds.head_height_min, thresholds.shoulder_level_max)
    # 全量生效配置一次性打印：防止 ".env / 代码默认 / 文档" 三方再次漂移
    log.info("startup, effective config: %s", {
        "head_height_min": thresholds.head_height_min,
        "shoulder_level_max": thresholds.shoulder_level_max,
        "conf_threshold": CONF_THRESHOLD,
        "retry_hh": RETRY_HH,
        "multimodal_enabled": MULTIMODAL_ENABLED,
        "multimodal_interval": MULTIMODAL_INTERVAL,
        "multimodal_ttl": MULTIMODAL_TTL,
        "multimodal_min_conf": MULTIMODAL_MIN_CONF,
        "slow_override_conf": SLOW_OVERRIDE_CONF,
        "slow_override_guard_frames": SLOW_OVERRIDE_GUARD_FRAMES,
        "rule_bad_hold_seconds": RULE_BAD_HOLD_SECONDS,
        "ai_probation_seconds": AI_PROBATION_SECONDS,
        "ai_timeout": AI_TIMEOUT,
        "ai_single_call": AI_SINGLE_CALL,
    })
    log.info(
        "slow-path budget: interval=%ds timeout=%.0fs fallback_timeout=%.0fs "
        "worst_case=%.1fs ttl=%ds model=%s enabled=%s",
        MULTIMODAL_INTERVAL, AI_TIMEOUT, AI_FALLBACK1_TIMEOUT,
        AI_WORST_CASE, MULTIMODAL_TTL, QWEN_MODEL, MULTIMODAL_ENABLED,
    )
    # 能力探测：启动时校验每个后端"模型在目录里"（只打 /models，不产生推理费用）
    if MULTIMODAL_ENABLED and vision_pool.enabled:
        try:
            caps = vision_pool.probe(deep=False)
            for name, info in caps.items():
                level = log.info if info["ok"] else log.error
                level("capability %s: ok=%s %s", name, info["ok"], info["detail"])
            if not any(b.supports_vision for b in vision_pool.backends):
                log.error("all backends failed capability probe -> slow channel will degrade")
        except Exception as e:  # noqa: BLE001
            log.warning("capability probe crashed (non-fatal): %s", e)
        # 备用后端测速（后台线程，不阻塞启动）：之后走备用时按实测最快挑
        log.info("backends: %s", [(b.name, b.model, b.timeout) for b in vision_pool.backends])
        vision_pool.seed_speeds(background=True)
    try:
        load_model()
    except Exception as e:
        log.error("model preload failed (will retry on first request): %s", e)
