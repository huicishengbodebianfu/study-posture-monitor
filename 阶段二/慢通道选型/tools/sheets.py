"""把 raw 目录候选图拼成带编号的联系表，便于快速筛选。"""
import os
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "images", "raw")
OUT = os.path.join(ROOT, "images", "sheets")
os.makedirs(OUT, exist_ok=True)

CELL = (360, 270)
COLS, ROWS = 3, 4


def make(key):
    files = sorted(f for f in os.listdir(RAW) if f.startswith(key + "-"))
    if not files:
        return
    sheet = Image.new("RGB", (CELL[0] * COLS + 8 * COLS, CELL[1] * ROWS + 8 * ROWS), "white")
    d = ImageDraw.Draw(sheet)
    for i, f in enumerate(files[:COLS * ROWS]):
        try:
            im = Image.open(os.path.join(RAW, f)).convert("RGB")
        except Exception:
            continue
        im.thumbnail((CELL[0], CELL[1]))
        x = (i % COLS) * (CELL[0] + 8)
        y = (i // COLS) * (CELL[1] + 8)
        sheet.paste(im, (x + (CELL[0] - im.width) // 2, y + (CELL[1] - im.height) // 2))
        d.rectangle([x, y, x + 34, y + 22], fill="black")
        d.text((x + 4, y + 4), str(i + 1), fill="yellow")
    sheet.save(os.path.join(OUT, f"{key}.jpg"), quality=85)
    print("sheet", key, len(files))


if __name__ == "__main__":
    for k in ("sleep", "phone", "slouch", "upright", "back", "night", "away", "daze", "lean", "hand"):
        make(k)
