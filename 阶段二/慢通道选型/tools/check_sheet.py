"""把最终测试集拼成一张核对表（带 id 与真值），用于人工校验标注。"""
import json, os
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DST = os.path.join(ROOT, "images", "test")
labels = json.load(open(os.path.join(DST, "labels.json"), encoding="utf-8"))
CELL = (320, 250)
COLS, ROWS = 4, 5
sheet = Image.new("RGB", (CELL[0] * COLS, (CELL[1] + 26) * ROWS), "white")
d = ImageDraw.Draw(sheet)
for i, it in enumerate(labels["images"]):
    im = Image.open(os.path.join(DST, it["path"])).convert("RGB")
    im.thumbnail((CELL[0], CELL[1]))
    x = (i % COLS) * CELL[0]
    y = (i // COLS) * (CELL[1] + 26)
    sheet.paste(im, (x + (CELL[0] - im.width) // 2, y + (CELL[1] - im.height) // 2))
    d.rectangle([x, y + CELL[1], x + CELL[0], y + CELL[1] + 26], fill="black")
    d.text((x + 3, y + CELL[1] + 2), it["id"], fill="yellow")
    d.text((x + 3, y + CELL[1] + 13), f'{it["state"]} u={it["upright"]}', fill="lime")
sheet.save(os.path.join(ROOT, "images", "test_check.jpg"), quality=88)
print("ok", len(labels["images"]))
