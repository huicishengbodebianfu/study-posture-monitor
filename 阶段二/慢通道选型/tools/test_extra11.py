"""对新增 11 张（未参与阈值标定）做端到端验证：每 10 秒一张。"""
import json
import time

import requests

URL = "http://127.0.0.1:8000/detect"
GT = {
    "phone-03": "slouch", "slouch-02": "slouch", "phone-05": "phone",
    "upright-14": "upright", "sleep-04": "sleep", "slouch-10": "slouch",
    "daze-07": "daze", "daze-06": "upright", "upright-06": "upright",
    "phone-07": "sleep", "slouch-11": "slouch",
}
KEY = ""
for line in open("/opt/posture/.env", encoding="utf-8"):
    if line.startswith("DEVICE_KEY="):
        KEY = line.strip().split("=", 1)[1]

rows = []
for name, gt in GT.items():
    with open(f"/tmp/t_extra/{name}.jpg", "rb") as f:
        data = f.read()
    t0 = time.time()
    try:
        r = requests.post(URL, data=data, timeout=20,
                          headers={"X-Device-Key": KEY, "Content-Type": "image/jpeg"})
        d = r.json() if r.status_code == 200 else {}
    except Exception as e:  # noqa: BLE001
        d = {"error": type(e).__name__}
    rec = {"image": name, "gt": gt, "http_time": round(time.time() - t0, 3),
           "action": d.get("action"), "upright": d.get("upright"),
           "state": d.get("state"), "source": d.get("source"),
           "reason": (d.get("reason") or "")[:44]}
    rows.append(rec)
    print(f"{name:12s} GT={gt:8s} upright={str(rec['upright']):5s} "
          f"state={str(rec['state']):8s} source={rec['source']:14s} "
          f"action={rec['action']:5s} {rec['http_time']*1000:.0f}ms  {rec['reason']}", flush=True)
    time.sleep(max(0.0, 10.0 - (time.time() - t0)))

json.dump(rows, open("/tmp/extra_results.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
