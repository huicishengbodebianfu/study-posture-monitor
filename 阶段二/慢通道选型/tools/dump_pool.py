import sys

sys.path.insert(0, "/opt/posture")
import app

pool = app.vision_pool
st = pool.stats()
print(f"{'后端':<12}{'模型':<42}{'EWMA':>7}{'样本':>6}  账号")
for name, s in st.items():
    be = next((b for b in pool.backends if b.name == name), None)
    if be is None:
        continue
    acct = be.base_url.replace("https://", "").split("/")[0]
    print(f"{name:<12}{be.model:<42}{s['ewma']:>7.2f}{s['samples']:>6}  {acct}")
print()
print("熔断器状态:")
br = pool.breaker
for name in st:
    print(f"  {name:<12} allow={br.allow(name)}")
