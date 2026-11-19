"""追加 4 张补充端正样本到测试集。"""
import json, os
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "images", "raw")
DST = os.path.join(ROOT, "images", "test")

EXTRA = [
    ("upright-13.jpg", "w_upright_desk_top", "坐在书桌前抬头看镜头，上身挺直"),
    ("upright-01.jpg", "w_upright_reading2", "两人并排看书，上身挺直"),
    ("upright-11.jpg", "w_upright_lamp", "台灯下读书，上身挺直"),
    ("night-01.jpg", "w_upright_kid_lamp", "儿童台灯下写字，坐姿端正"),
]

for src, tid, desc in EXTRA:
    im = Image.open(os.path.join(RAW, src)).convert("RGB")
    w = 640
    h = round(im.height * w / im.width)
    im.resize((w, h), Image.LANCZOS).save(os.path.join(DST, tid + ".jpg"), quality=75)

p = os.path.join(DST, "labels.json")
d = json.load(open(p, encoding="utf-8"))
have = {it["id"] for it in d["images"]}
for src, tid, desc in EXTRA:
    if tid in have:
        continue
    d["images"].append({
        "id": tid, "path": tid + ".jpg", "source": "web", "desc": desc,
        "state": "upright", "allowed": ["upright"], "upright": True,
        "alert": False, "count": 1,
    })
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("images:", len(d["images"]))
