"""用 OpenAI 官方 SDK 调用魔搭 API-Inference（已实测）。

安装：
    pip install openai

用法：
    set MS_KEY=ms-xxxxxxxx        # Windows
    export MS_KEY=ms-xxxxxxxx     # Linux/macOS
    python test_modelscope_openai.py                 # 文本
    python test_modelscope_openai.py 某张图.jpg      # 文本 + 视觉

关键点：
    1. base_url 必须是 https://api-inference.modelscope.cn/v1
    2. model 必须带组织前缀，如 deepseek-ai/DeepSeek-V4-Pro-0813
    3. 视觉走 image_url + base64 data URL
"""
import base64
import json
import os
import sys

from openai import OpenAI

BASE_URL = "https://api-inference.modelscope.cn/v1"
TEXT_MODEL = "deepseek-ai/DeepSeek-V4-Pro-0813"
VISION_MODEL = "Shanghai_AI_Laboratory/Intern-S2-Preview"

key = os.environ.get("MS_KEY", "")
if not key:
    print("先设置环境变量 MS_KEY")
    sys.exit(1)

client = OpenAI(base_url=BASE_URL, api_key=key)
print(f"base_url = {BASE_URL}")
print(f"key      = {key[:8]}...{key[-4:]}\n")

# ---------------- 1. 文本 ----------------
r = client.chat.completions.create(
    model=TEXT_MODEL,
    messages=[{"role": "user", "content": "只回答两个字：你好"}],
    max_tokens=64,
)
print("[文本]", r.model, "->", repr(r.choices[0].message.content))
print("       usage:", r.usage.prompt_tokens, "in /", r.usage.completion_tokens, "out\n")

# ---------------- 2. 视觉 ----------------
if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
    img = base64.b64encode(open(sys.argv[1], "rb").read()).decode()
    r = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[{"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": "data:image/jpeg;base64," + img}},
            {"type": "text",
             "text": '只输出 JSON：{"state":"upright|slouch","upright":true/false,"confidence":0-1}'},
        ]}],
        max_tokens=200,
    )
    text = (r.choices[0].message.content or "").strip()
    print("[视觉]", r.model, "->", text[:100].replace("\n", " "))
    a, b = text.find("{"), text.rfind("}")
    if a >= 0:
        print("       解析:", json.loads(text[a:b + 1]))
else:
    print("（附一张图可测试视觉：python test_modelscope_openai.py 图.jpg）")
