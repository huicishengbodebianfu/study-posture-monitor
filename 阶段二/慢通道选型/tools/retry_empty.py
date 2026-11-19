"""对"内容为空（推理吃光 token 预算）"的记录，用更大预算重跑一次。"""
import json, os, sys, threading
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe  # noqa: E402

ROOT = probe.ROOT
OUT = probe.OUT
IMG = probe.IMG
lock = threading.Lock()


def rerun(rec, max_tokens=8000):
    labels = {it["path"]: it for it in json.load(open(os.path.join(IMG, "labels.json"), encoding="utf-8"))["images"]}
    path = os.path.join(IMG, rec["image"] + ".jpg")
    payload = {
        "model": rec["model"],
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + probe.b64(path)}},
            {"type": "text", "text": probe.PROMPTS[rec["prompt"]]},
        ]}],
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    out = dict(rec); out["retried"] = True; out["max_tokens"] = max_tokens
    try:
        import time
        t0 = time.time()
        resp = probe.post(rec["base"], probe.PROVIDERS[rec["base"]]["key"], payload, timeout=300)
        msg = resp["choices"][0]["message"]
        out.update(ok=True, latency_s=round(time.time() - t0, 2), content=msg.get("content") or "",
                   reasoning=(msg.get("reasoning_content") or "")[:1500], usage=resp.get("usage", {}))
        out["json"] = probe.extract_json(out["content"])
        out["json_ok"] = out["json"] is not None
    except urllib.error.HTTPError as e:
        out.update(ok=False, error=f"HTTP {e.code}")
    except Exception as e:
        out.update(ok=False, error=repr(e)[:300])
    return out


def main(prompt="S"):
    p = os.path.join(OUT, f"raw_{prompt}.jsonl")
    rows = [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
    todo = [r for r in rows if r.get("ok") and not r.get("json_ok") and not (r.get("content") or "").strip()]
    print("to retry:", len(todo))
    with open(os.path.join(OUT, f"raw_{prompt}_fix.jsonl"), "w", encoding="utf-8") as f, ThreadPoolExecutor(max_workers=4) as ex:
        for fut in as_completed([ex.submit(rerun, r) for r in todo]):
            rec = fut.result()
            with lock:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n"); f.flush()
            print(rec["model"], rec["image"], "json_ok=", rec.get("json_ok"))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "S")
