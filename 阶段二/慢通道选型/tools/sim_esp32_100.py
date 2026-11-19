"""模拟 ESP32-S3-CAM 每 10 秒上传一张，共 100 张，记录服务器每次的返回。

图片按真实使用分布混合（多数端正，少量低头/玩手机/趴桌/故障帧）。
结果写 JSONL：时间戳、图片、HTTP 耗时、action、upright、state、source、reason。
"""
import json
import random
import time

import requests

URL = "http://127.0.0.1:8000/detect"
OUT = "/tmp/sim100.jsonl"
INTERVAL = 10.0

KEY = ""
for line in open("/opt/posture/.env", encoding="utf-8"):
    line = line.strip()
    if line.startswith("DEVICE_KEY="):
        KEY = line.split("=", 1)[1]

# 真实分布：55 端正 / 20 低头 / 10 玩手机 / 5 趴桌 / 3 发呆 / 2 离开 / 5 故障
PLAN = (
    ["w_upright_lib", "w_upright_desk_top", "w_upright_reading2",
     "w_upright_lamp", "w_upright_write", "w_upright_kid_lamp"] * 10   # 60
    + ["w_study_hunch", "w_study_hunch_multi", "w_night_dark", "w_night_back"] * 5   # 20
    + ["w_phone_side", "w_phone_two"] * 5                                            # 10
    + ["w_sleep_class", "w_sleep_close"] * 2 + ["w_sleep_close"]                     # 5
    + ["w_daze_chin"] * 2 + ["w_empty_room"]                                         # 3
    + ["p_black_frame", "p_occlude_hand"] * 2 + ["p_black_frame"]                    # 5
)  # 共 105，取前 100
random.seed(20261102)
PLAN = PLAN[:100]
random.shuffle(PLAN)

rows = []
open(OUT, "w").close()
t_start = time.time()
for i, name in enumerate(PLAN, 1):
    path = f"/tmp/t24/{name}.jpg"
    with open(path, "rb") as f:
        data = f.read()
    t0 = time.time()
    rec = {"i": i, "image": name}
    try:
        r = requests.post(URL, data=data, timeout=20,
                          headers={"X-Device-Key": KEY, "Content-Type": "image/jpeg"})
        rec["http"] = r.status_code
        rec["http_time"] = round(time.time() - t0, 3)
        if r.status_code == 200:
            d = r.json()
            rec.update(action=d.get("action"), upright=d.get("upright"),
                       state=d.get("state"), source=d.get("source"),
                       reason=(d.get("reason") or "")[:40])
    except Exception as e:  # noqa: BLE001
        rec.update(http=0, error=type(e).__name__, http_time=round(time.time() - t0, 3))
    rec["t"] = round(time.time() - t_start, 1)
    rows.append(rec)
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if i % 10 == 0:
        print(f"  {i}/100  elapsed={time.time()-t_start:.0f}s", flush=True)

    # 补足到 10 秒一个周期
    time.sleep(max(0.0, INTERVAL - (time.time() - t0)))

print("done", len(rows), "->", OUT)
