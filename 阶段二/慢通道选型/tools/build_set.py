"""构建测试集：把联网素材统一到 ESP32-CAM 的 640x480 条件，并与项目实拍帧合并。

慢通道真实输入就是 VGA 帧，所以统一缩放到 640 宽是"公平且贴近部署"的条件。
"""
import json, os, shutil
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "images", "raw")
DST = os.path.join(ROOT, "images", "test")
os.makedirs(DST, exist_ok=True)

PICK = [
    # (源文件, 目标 id)
    ("sleep-03.jpg", "w_sleep_class"),
    ("sleep-06.jpg", "w_sleep_close"),
    ("phone-06.jpg", "w_phone_side"),
    ("phone-10.jpg", "w_phone_two"),
    ("slouch-01.jpg", "w_study_hunch"),
    ("slouch-08.jpg", "w_study_hunch_multi"),
    ("upright-05.jpg", "w_upright_lib"),
    ("upright-10.jpg", "w_upright_write"),
    ("night-07.jpg", "w_night_back"),
    ("night-12.jpg", "w_night_dark"),
    ("away-03.jpg", "w_empty_room"),
    ("daze-01.jpg", "w_daze_chin"),
]

PROJ = os.path.join(os.path.dirname(os.path.dirname(ROOT)), "项目", "测试记录")
PROJ_PICK = [
    ("20261008-state-check.jpg", "p_occlude_hand"),
    ("20261007-抓图测试-233534.jpg", "p_ceiling"),
    ("20261008-连测1.jpg", "p_side_lookup1"),
    ("20261008-连测2.jpg", "p_side_lookup2"),
    ("20261008-上传测试-100838.jpg", "p_side_lookup3"),
    ("20261008-烧录验证-抓图.jpg", "p_underdesk"),
    ("20261007-上传测试-235218.jpg", "p_black_frame"),
    ("20260919-M1-单帧测试.jpg", "p_qvga_side"),
]


def web():
    for src, tid in PICK:
        p = os.path.join(RAW, src)
        if not os.path.exists(p):
            print("MISS", p); continue
        im = Image.open(p).convert("RGB")
        w = 640
        h = round(im.height * w / im.width)
        im = im.resize((w, h), Image.LANCZOS)
        out = os.path.join(DST, tid + ".jpg")
        im.save(out, quality=75)
        print(tid, im.size, os.path.getsize(out))


def proj():
    for src, tid in PROJ_PICK:
        p = os.path.join(PROJ, src)
        if not os.path.exists(p):
            print("MISS", p); continue
        shutil.copy2(p, os.path.join(DST, tid + ".jpg"))
        print(tid, os.path.getsize(p))


if __name__ == "__main__":
    web(); proj()


