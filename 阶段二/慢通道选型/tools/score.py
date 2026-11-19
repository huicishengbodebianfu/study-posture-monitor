"""汇总评分：把 raw_*.jsonl 与真值比对，输出每模型指标。"""
import json, os, sys, statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG = os.path.join(ROOT, "images", "test")
OUT = os.path.join(ROOT, "results")
VALID = {"upright", "slouch", "lean", "phone", "daze", "away", "sleep", "unknown"}

PRICING = {  # USD per 1K tokens (prompt, completion); None = 套餐内不计费
    "Agents-A1": None, "Sage-A1.5": None, "intern-s2": None,
    "deepseek-v4.1-flash-1017": (0.001, 0.004),
    "deepseek-v4-flash-vision": (0.001, 0.004),
    "kimi-k2.6": (0.0065, 0.027),
    "qwen3.8-27b": (0.0008, 0.0027),
    "sensenova-6.8-flash-lite": None,
}


def load(prompt):
    p = os.path.join(OUT, f"raw_{prompt}.jsonl")
    recs = [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
    fix = os.path.join(OUT, f"raw_{prompt}_fix.jsonl")
    if os.path.exists(fix):
        recs += [json.loads(l) for l in open(fix, encoding="utf-8") if l.strip()]
    # 同 key 取最后一次（重试成功的覆盖失败的）
    best = {}
    for r in recs:
        k = (r["model"], r["image"])
        best[k] = r
    return list(best.values())


def norm_state(v):
    s = str(v or "unknown").strip().lower()
    return s if s in VALID else "unknown"


def norm_bool(v):
    if v is True or v == "true" or v == "True":
        return True
    if v is False or v == "false" or v == "False":
        return False
    return None


def score(prompt):
    labels = {it["id"]: it for it in json.load(open(os.path.join(IMG, "labels.json"), encoding="utf-8"))["images"]}
    recs = load(prompt)
    by_model = {}
    for r in recs:
        by_model.setdefault(r["model"], []).append(r)

    rows = []
    detail = []
    for model, rs in sorted(by_model.items()):
        n = len(rs)
        js = sum(1 for r in rs if r.get("json_ok"))
        ok = [r for r in rs if r.get("json_ok")]
        strict = acc = up_ok = up_n = 0
        fa = 0      # 误报：真值端正却 should_alert=true
        fs = 0      # 误报（融合层真正生效的）：真值端正却 upright=false → 会被兜底翻案
        miss = 0    # 漏报：真值不端正却 upright=true
        pin = pout = 0
        lat = []
        for r in ok:
            gt = labels[r["image"]]
            j = r["json"]
            st = norm_state(j.get("state"))
            if not gt.get("ambiguous"):
                strict += int(st == gt["state"])
                acc += int(st in gt["allowed"])
            m_up = norm_bool(j.get("upright"))
            if gt["upright"] is not None and not gt.get("ambiguous"):
                up_n += 1
                up_ok += int(m_up == gt["upright"])
                if gt["upright"] is True and m_up is False:
                    fs += 1
                if gt["upright"] is False and m_up is True:
                    miss += 1
            if gt["upright"] is True and norm_bool(j.get("should_alert")) is True:
                fa += 1
            lat.append(r.get("latency_s") or 0)
            u = r.get("usage") or {}
            pin += u.get("prompt_tokens") or 0
            pout += u.get("completion_tokens") or 0
        lat.sort()
        p95 = lat[int(len(lat) * 0.95) - 1] if lat else 0
        pr = PRICING.get(model)
        cost = None
        if pr and pin:
            cost = (pin / 1000 * pr[0] + pout / 1000 * pr[1]) / max(len(ok), 1) * 1000
        rows.append({
            "model": model, "n": n, "json_rate": js / n if n else 0,
            "strict_acc": strict / max(n - sum(1 for r in rs if labels[r["image"]].get("ambiguous")), 1),
            "accept_rate": acc / max(n - sum(1 for r in rs if labels[r["image"]].get("ambiguous")), 1),
            "upright_acc": up_ok / up_n if up_n else 0,
            "false_alarm": fa, "false_slouch": fs, "miss": miss,
            "mean_lat": statistics.mean(lat) if lat else 0, "p95_lat": p95,
            "tok_in": pin / max(len(ok), 1), "tok_out": pout / max(len(ok), 1),
            "cost_1k_calls": cost,
        })
        for r in rs:
            gt = labels[r["image"]]
            j = r.get("json") or {}
            detail.append({
                "model": model, "image": r["image"], "ok": r.get("ok"),
                "json_ok": r.get("json_ok"),
                "state": norm_state(j.get("state")) if r.get("json_ok") else None,
                "upright": norm_bool(j.get("upright")) if r.get("json_ok") else None,
                "alert": norm_bool(j.get("should_alert")) if r.get("json_ok") else None,
                "count": j.get("person_count"),
                "conf": j.get("confidence"),
                "reason": (j.get("reason") or "")[:80],
                "gt_state": gt["state"], "gt_upright": gt["upright"],
                "hit": (norm_state(j.get("state")) in gt["allowed"]) if r.get("json_ok") else False,
                "lat": r.get("latency_s"), "err": (r.get("error") or "")[:120],
            })

    with open(os.path.join(OUT, f"detail_{prompt}.json"), "w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False, indent=1)
    return rows


def fmt(prompt):
    rows = score(prompt)
    rows.sort(key=lambda r: (-r["accept_rate"], r["false_slouch"], r["mean_lat"]))
    print(f"\n### prompt={prompt}  (样本 {rows[0]['n']} 张/模型)\n")
    head = (f"{'model':28s} {'JSON':>6s} {'严格':>6s} {'可接受':>7s} {'姿态':>6s} "
            f"{'端正误判':>8s} {'漏判':>5s} {'误报alert':>9s} {'均延迟':>7s} {'P95':>7s} {'in/out':>10s} {'$/1k':>7s}")
    print(head); print("-" * 122)
    for r in rows:
        c = f"{r['cost_1k_calls']:.2f}" if r["cost_1k_calls"] else "—"
        print(f"{r['model']:28s} {r['json_rate']*100:5.0f}% {r['strict_acc']*100:5.0f}% {r['accept_rate']*100:6.0f}% "
              f"{r['upright_acc']*100:5.0f}% {r['false_slouch']:8d} {r['miss']:5d} {r['false_alarm']:9d} "
              f"{r['mean_lat']:6.1f}s {r['p95_lat']:6.1f}s {r['tok_in']:4.0f}/{r['tok_out']:<5.0f} {c:>7s}")


if __name__ == "__main__":
    for p in (sys.argv[1:] or ["S"]):
        fmt(p)


