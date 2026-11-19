"""导出给服务器同事的表格证据（指标汇总 + 逐图判定明细）。"""
import csv, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import score  # noqa: E402

ROOT = score.ROOT
OUT = os.path.join(ROOT, "export")
os.makedirs(OUT, exist_ok=True)

rows = score.score("S")
rows.sort(key=lambda r: (-r["accept_rate"], r["false_slouch"], r["mean_lat"]))

with open(os.path.join(OUT, "01-各模型指标.csv"), "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["模型", "样本数", "JSON可解析率", "严格准确率", "可接受率", "upright一致率",
                "端正误判(误报)", "漏判", "alert误报", "平均延迟s", "P95延迟s",
                "输入token", "输出token", "每千次成本USD"])
    for r in rows:
        w.writerow([
            r["model"], r["n"],
            f'{r["json_rate"]:.0%}', f'{r["strict_acc"]:.0%}', f'{r["accept_rate"]:.0%}',
            f'{r["upright_acc"]:.0%}', r["false_slouch"], r["miss"], r["false_alarm"],
            f'{r["mean_lat"]:.1f}', f'{r["p95_lat"]:.1f}',
            round(r["tok_in"]), round(r["tok_out"]),
            f'{r["cost_1k_calls"]:.2f}' if r["cost_1k_calls"] else "套餐内",
        ])

labels = {it["id"]: it for it in json.load(
    open(os.path.join(ROOT, "images", "test", "labels.json"), encoding="utf-8"))["images"]}
detail = json.load(open(os.path.join(ROOT, "results", "detail_S.json"), encoding="utf-8"))
detail.sort(key=lambda d: (d["model"], d["image"]))

with open(os.path.join(OUT, "02-逐图判定明细.csv"), "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["模型", "图片", "图片真值state", "真值upright", "是否计入严格统计",
                "模型state", "模型upright", "模型confidence", "是否命中", "耗时s", "模型理由"])
    for d in detail:
        gt = labels[d["image"]]
        w.writerow([
            d["model"], d["image"], gt["state"], str(gt["upright"]),
            "否(争议)" if gt.get("ambiguous") else "是",
            d["state"] or "(空)", str(d["upright"]), d.get("conf", ""), "是" if d["hit"] else "否",
            d["lat"], d["reason"],
        ])

print("exported:", os.listdir(OUT))

