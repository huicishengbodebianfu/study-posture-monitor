from __future__ import annotations

import os
import logging

import cv2
import numpy as np
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse

from posture import Thresholds, RuleEngine, extract_features, is_upright
from multimodal import MultimodalDetector

DEVICE_KEY = os.environ.get("DEVICE_KEY", "change-me-to-a-random-secret")
MODEL_DIR = os.environ.get("MODEL_DIR", "yolov8n-pose_openvino_model")
CONF_THRESHOLD = float(os.environ.get("CONF_THRESHOLD", "0.5"))
HEAD_HEIGHT_MIN = float(os.environ.get("HEAD_HEIGHT_MIN", "0.15"))
SHOULDER_LEVEL_MAX = float(os.environ.get("SHOULDER_LEVEL_MAX", "0.05"))

# ---- 多态检测（多个 AI API 分开工作）----
# 默认关闭，不影响阶段一验收；开启后启用"Qwen-VL 看图 + DeepSeek 决策"慢速通道
MULTIMODAL_ENABLED = os.environ.get("MULTIMODAL_ENABLED", "false").lower() == "true"
QWEN_API_KEY = os.environ.get("QWEN_API_KEY", "")
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen-vl-plus")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
MULTIMODAL_INTERVAL = int(os.environ.get("MULTIMODAL_INTERVAL", "30"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("posture")

app = FastAPI(title="posture-monitor", version="1.0")

thresholds = Thresholds(
    head_height_min=HEAD_HEIGHT_MIN,
    shoulder_level_max=SHOULDER_LEVEL_MAX,
)
rule_engine = RuleEngine(consecutive_frames=2, dedup_seconds=60)

multimodal = MultimodalDetector(
    enabled=MULTIMODAL_ENABLED,
    qwen_api_key=QWEN_API_KEY,
    qwen_model=QWEN_MODEL,
    deepseek_api_key=DEEPSEEK_API_KEY,
    deepseek_model=DEEPSEEK_MODEL,
    interval_seconds=MULTIMODAL_INTERVAL,
)

_ov_model = None


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


def preprocess(image_bytes: bytes, input_size=(640, 640)):
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("decode jpeg failed")

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

    return blob, scale, pad_x, pad_y, orig_w, orig_h


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


@app.post("/detect")
async def detect(request: Request, x_device_key: str = Header(default="")):
    if x_device_key != DEVICE_KEY:
        log.warning("bad device key: %s", x_device_key)
        raise HTTPException(status_code=401, detail="invalid device key")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty body")

    try:
        blob, scale, pad_x, pad_y, ow, oh = preprocess(body)
        keypoints, kp_conf = infer(blob)
    except Exception as e:
        log.exception("inference failed")
        raise HTTPException(status_code=500, detail=str(e))

    # ---- 慢速通道：多态检测（多个 AI API 分开工作，非阻塞触发）----
    multimodal.run_async(body)

    if keypoints is None:
        # 快速通道无法判定 → 尝试用慢速通道结果兜底
        fused = multimodal.fuse(None, "no_person")
        action = rule_engine.update(fused.upright)
        return JSONResponse({
            "action": action,
            "upright": fused.upright,
            "state": fused.state,
            "source": fused.source,
            "reason": fused.reason,
        })

    keypoints = unletterbox(keypoints, scale, pad_x, pad_y)
    features = extract_features(keypoints, kp_conf)
    fast_upright = is_upright(features, thresholds)

    # ---- 融合快速通道 + 慢速通道 ----
    fused = multimodal.fuse(fast_upright, features.reason)
    action = rule_engine.update(fused.upright)

    log.info(
        "head_height=%.3f shoulder_level=%.3f valid=%s fast=%s fused=%s state=%s source=%s action=%s",
        features.head_height, features.shoulder_level,
        features.valid, fast_upright, fused.upright, fused.state, fused.source, action,
    )

    return JSONResponse({
        "action": action,
        "upright": fused.upright,
        "state": fused.state,
        "source": fused.source,
        "head_height": round(float(features.head_height), 4),
        "shoulder_level": round(float(features.shoulder_level), 4),
        "reason": fused.reason or features.reason,
    })


@app.on_event("startup")
def on_startup():
    log.info("startup, thresholds: head_height_min=%.3f shoulder_level_max=%.3f",
             thresholds.head_height_min, thresholds.shoulder_level_max)
    try:
        load_model()
    except Exception as e:
        log.error("model preload failed (will retry on first request): %s", e)
