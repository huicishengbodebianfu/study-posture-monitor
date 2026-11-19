import glob
import json
import os

files = sorted(glob.glob("/var/log/posture/badcase/*"), key=os.path.getmtime, reverse=True)[:6]
print(f"最近 {len(files)} 个坏例：")
for f in files:
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"  {os.path.basename(f)}  解析失败 {e}")
        continue
    print(f"  {os.path.basename(f)[:24]}  backend={d.get('backend')}  model={d.get('model')}  "
          f"code={d.get('http_code')}  err={d.get('error')}")
    body = (d.get("body") or "")[:160].replace("\n", " ")
    if body:
        print(f"      body: {body}")
