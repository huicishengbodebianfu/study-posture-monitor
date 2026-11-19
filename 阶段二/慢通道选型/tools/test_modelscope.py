"""魔搭社区 API-Inference 调用示例 / 自检脚本。

用法：
    set MS_KEY=ms-xxxxxxxx          (Windows)
    export MS_KEY=ms-xxxxxxxx       (Linux/macOS)
    python test_modelscope.py                 # 列表 + 文本调用
    python test_modelscope.py 图片.jpg        # 再加一次视觉调用

为什么单独写这份：
  - 魔搭的 GET /v1/models 不校验鉴权，假 key 也返回 200，不能用来判断 key 有效性
  - 模型 id 必须带组织前缀（如 Shanghai_AI_Laboratory/Intern-S2-Preview）
  - 图片太小时模型会报 "image length and width do not meet the requirements"
"""
import base64
import json
import os
import sys
import time

import requests

BASE = "https://api-inference.modelscope.cn/v1"
KEY = os.environ.get("MS_KEY", "")
TEXT_MODEL = "deepseek-ai/DeepSeek-V4-Pro-0813"
VISION_MODEL = "Shanghai_AI_Laboratory/Intern-S2-Preview"


def headers():
    return {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def list_models():
    r = requests.get(f"{BASE}/models", headers=headers(), timeout=20)
    ids = [m.get("id") for m in (r.json().get("data") or []) if isinstance(m, dict)]
    print(f"[models] HTTP {r.status_code}，共 {len(ids)} 个")
    for i in ids:
        print("   ", i)
    print("   注意：这个接口不校验 key，200 不代表 key 有效\n")
    return ids


def chat(model, content, max_tokens=200):
    t0 = time.time()
    r = requests.post(
        f"{BASE}/chat/completions",
        headers=headers(),
        json={"model": model,
              "messages": [{"role": "user", "content": content}],
              "temperature": 0.1, "max_tokens": max_tokens},
        timeout=60,
    )
    dt = time.time() - t0
    if r.status_code != 200:
        print(f"[chat] {model} -> HTTP {r.status_code} {r.text[:160]}")
        return None
    out = (r.json()["choices"][0]["message"].get("content") or "").strip()
    print(f"[chat] {model} -> HTTP 200 {dt:.2f}s：{out[:80]!r}")
    return out


def main():
    if not KEY:
        print("先设置环境变量 MS_KEY（魔搭 个人中心→设置→访问令牌）")
        return 1

    print(f"base_url = {BASE}")
    print(f"key      = {KEY[:8]}...{KEY[-4:]}（长度 {len(KEY)}）\n")

    list_models()

    # 1) 文本调用 —— 这一步才真正验证 key
    chat(TEXT_MODEL, "只回答两个字：你好", max_tokens=16)

    # 2) 视觉调用
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        img = base64.b64encode(open(sys.argv[1], "rb").read()).decode()
        content = [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + img}},
            {"type": "text", "text": '只输出 JSON：{"state":"upright|slouch","upright":true/false,"confidence":0-1}'},
        ]
        out = chat(VISION_MODEL, content, max_tokens=200)
        if out:
            try:
                print("   解析结果:", json.loads(out[out.find("{"):out.rfind("}") + 1]))
            except Exception:
                print("   （返回不是纯 JSON）")
    else:
        print("\n（想看视觉调用：python test_modelscope.py 某张图.jpg）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
