# -*- coding: utf-8 -*-
"""pilot_v4b.py — 方案 A 小试点: 验证 v4-pro 非thinking + MAXTOK=2000 消除空内容伪影。

阶段0 探测: 对最重 schema (thrombosis_prediction ck0) 各发 1 次调用, 尝试多种
        非thinking 参数约定 + effort 基线, 记录 content 非空性 / finish_reason /
        usage 字段 (发现 reasoning token 记账方式), 选出胜出配置。
阶段1 试点: 胜出配置跑 60 调用 (5 个 S 库 ck0 × 2题 × 5候选 + 漂移点 sanity
        thrombosis ck3 / california ck5), 统计空内容率 (成功线 <5%)、exec_ok、
        延迟、token 用量 → 外推 11,000 调用全量成本/时长。

输出: stdout 摘要 + results/pilot_v4b.json
API key 只从环境变量 GATE_LLM_API_KEY 读取, 不落盘。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from build_drift_benchmark import HERE, db_path_of, result_fingerprint, run_sql
from gen_candidates_v4 import extract_sql

API = os.environ.get("DRIFTSQL_API_BASE", "https://api.deepseek.com") + "/chat/completions"
KEY = os.environ.get("GATE_LLM_API_KEY", "")
MODEL = "deepseek-v4-pro"
MAXTOK = 2000
WORKERS = 8
NCAND = 5
LOCK = threading.Lock()

SYS = ("You are an expert text-to-SQL system. Generate ONE SQLite query "
       "for the question. Output ONLY the SQL inside a ```sql code block.")
USERT = "### SQLite schema:\n{ddl}\n\n### Question:\n{q}\n\n" \
        "Write one SQLite query answering the question."

# 阶段0: 候选非thinking调用约定 (name, extra_body)
PROBES = [
    ("default(no-effort)", {}),
    ("effort=minimal", {"effort": "minimal"}),
    ("effort=low(baseline)", {"effort": "low"}),
    ("reasoning_effort=minimal", {"reasoning_effort": "minimal"}),
    ("enable_thinking=false", {"enable_thinking": False}),
    ("thinking=disabled", {"thinking": {"type": "disabled"}}),
]


def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_jsonl(p):
    if not os.path.exists(p):
        return []
    return [json.loads(ln) for ln in open(p, encoding="utf-8")]


def ddl_of(db_path):
    import sqlite3
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL").fetchall()
    con.close()
    return "\n\n".join(r[0] for r in rows)


def call_api(ddl, question, extra, timeout=120):
    """返回 dict: content/finish/usage/latency/err。"""
    body = {"model": MODEL,
            "messages": [{"role": "system", "content": SYS},
                         {"role": "user", "content": USERT.format(
                             ddl=ddl, q=question)}],
            "temperature": 1.0, "top_p": 0.95, "max_tokens": MAXTOK,
            "logprobs": True, "top_logprobs": 1}
    body.update(extra)
    t0 = time.time()
    try:
        req = urllib.request.Request(
            API, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + KEY})
        r = json.load(urllib.request.urlopen(req, timeout=timeout))
        ch = r["choices"][0]
        lp = sum(t["logprob"] for t in
                 (ch.get("logprobs") or {}).get("content", []))
        return {"ok": True, "content": ch["message"].get("content") or "",
                "finish": ch.get("finish_reason"),
                "usage": r.get("usage", {}), "lp": lp,
                "latency": round(time.time() - t0, 1), "err": None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "content": "", "finish": None, "usage": {},
                "lp": None, "latency": round(time.time() - t0, 1),
                "err": str(e)[:160]}


def probe_phase(thr_ddl, thr_q):
    print("=== 阶段0: 非thinking 参数探测 (thrombosis ck0, MAXTOK=%d) ===" % MAXTOK,
          flush=True)
    winner = None
    for name, extra in PROBES:
        r = call_api(thr_ddl, thr_q, extra)
        sql = extract_sql(r["content"]) if r["content"] else ""
        u = r["usage"]
        rk = {k: v for k, v in u.items() if "reason" in k.lower() or
              k in ("prompt_tokens", "completion_tokens", "total_tokens")}
        print(f"[probe] {name:<26} ok={r['ok']} finish={r['finish']} "
              f"content_len={len(r['content'])} sql_len={len(sql)} "
              f"lat={r['latency']}s usage={rk} err={r['err']}", flush=True)
        if r["ok"] and sql and winner is None:
            winner = (name, extra)
            print(f"[probe] -> 胜出配置: {name}", flush=True)
    if winner is None:
        print("[probe] 无配置产出非空 SQL!", flush=True)
    return winner


# 阶段0 探测结论 (2026-09-29 首轮运行): effort=minimal/enable_thinking 被忽略,
# effort=low 在 MAXTOK=2000 下推理仍耗尽预算 (试点空内容率 41.4%);
# thinking={"type":"disabled"} 为真·非thinking: completion≈40 token 纯 SQL。
FORCE_NAME = "thinking=disabled"
FORCE_EXTRA = {"thinking": {"type": "disabled"}}


def main():
    assert KEY, "set GATE_LLM_API_KEY env var first"
    t0 = time.time()

    # thrombosis ck0 DDL + 一道题 (最重 schema, 伪影重灾区)
    thr_ddl = ddl_of(db_path_of("thrombosis_prediction"))
    qs = defaultdict(list)
    for q in load_json(os.path.join(HERE, "questions", "family_S.json")):
        qs[q["db"]].append(q)
    thr_q = sorted(qs["thrombosis_prediction"], key=lambda x: x["qid"])[0]

    wname, wextra = FORCE_NAME, FORCE_EXTRA  # 探测已定, 直接用非thinking
    print(f"\n=== 阶段1: 试点 70 调用, 配置={wname}, MAXTOK={MAXTOK} ===",
          flush=True)

    # 任务: 5 库 ck0 × 2题 × 5候选 + 漂移 sanity (thrombosis ck3, california ck5)
    jobs = []  # (tag, db, t, q, path)
    for db in ("superhero", "student_club", "toxicology",
               "thrombosis_prediction", "california_schools"):
        for q in sorted(qs[db], key=lambda x: x["qid"])[:2]:
            jobs.append(("ck0", db, 0, q, db_path_of(db)))
    for db, t in (("thrombosis_prediction", 3), ("california_schools", 5)):
        p = os.path.join(HERE, "dbs", "S", db, f"ck{t}.sqlite")
        for q in sorted(qs[db], key=lambda x: x["qid"])[:2]:
            jobs.append((f"ck{t}", db, t, q, p))
    jobs = [(tag, db, t, q, p) for tag, db, t, q, p in jobs
            for _ in range(NCAND)]
    print(f"[pilot] calls = {len(jobs)}", flush=True)

    # gold (S 族) 用于 ck0/ck_t correct 标注
    gold = {}
    for r in load_jsonl(os.path.join(HERE, "gold", "gold_S.jsonl")):
        gold[r["qid"]] = r

    results = []

    def work(job):
        tag, db, t, q, path = job
        ddl = ddl_of(path)
        r = call_api(ddl, q["question"], wextra)
        sql = extract_sql(r["content"]) if r["content"] else ""
        ok, errc, rows = (False, None, None)
        fp = None
        correct = None
        if sql:
            ok, errc, rows = run_sql(path, sql, timeout=10)
            if ok:
                h, _n = result_fingerprint(rows)
                fp = h
                g = gold.get(q["qid"], {}).get("checkpoints", {}).get(
                    str(t), {})
                if g.get("exec_ok"):
                    correct = (h == g.get("result_hash"))
        rec = {"tag": tag, "db": db, "ckpt": t, "qid": q["qid"],
               "empty": not bool(r["content"]), "sql_len": len(sql),
               "exec_ok": ok, "correct": correct, "finish": r["finish"],
               "latency": r["latency"], "lp": r["lp"], "err": r["err"],
               "usage": r["usage"]}
        with LOCK:
            results.append(rec)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(work, j) for j in jobs]
        for i, fu in enumerate(as_completed(futs), 1):
            fu.result()
            if i % 20 == 0:
                print(f"[pilot] {i}/{len(jobs)} "
                      f"({(time.time()-t0)/60:.1f} min)", flush=True)

    # ---- 统计 ----
    n = len(results)
    n_empty = sum(1 for r in results if r["empty"])
    fin = Counter(r["finish"] for r in results)
    ok = sum(1 for r in results if r["exec_ok"])
    corr = sum(1 for r in results if r["correct"] is True)
    lats = sorted(r["latency"] for r in results if not r["empty"])
    ptok = sum(r["usage"].get("prompt_tokens", 0) for r in results)
    ctok = sum(r["usage"].get("completion_tokens", 0) for r in results)
    rtok = sum(r["usage"].get("completion_tokens_details", {})
               .get("reasoning_tokens", 0) for r in results)
    print("\n=== 试点结果 ===", flush=True)
    print(f"配置: {wname} extra={wextra}", flush=True)
    print(f"调用: {n}  空内容: {n_empty} ({n_empty/n:.1%})  "
          f"finish={dict(fin)}", flush=True)
    print(f"exec_ok={ok}/{n}  correct={corr}/{n}", flush=True)
    if lats:
        print(f"延迟: 中位 {lats[len(lats)//2]}s / 最大 {lats[-1]}s",
              flush=True)
    print(f"tokens: prompt={ptok} completion={ctok} (reasoning≈{rtok}) "
          f"平均 completion={ctok/max(1,n):.0f}/call", flush=True)
    # 伪影对比 (gen_v4 同库空内容率)
    old = {"superhero": .16, "student_club": .41, "toxicology": .67,
           "thrombosis_prediction": .95, "california_schools": .77}
    print("\n分库空内容率 (旧 gen_v4 → 试点):", flush=True)
    for db in ("superhero", "student_club", "toxicology",
               "thrombosis_prediction", "california_schools"):
        sub = [r for r in results if r["db"] == db]
        e = sum(1 for r in sub if r["empty"])
        print(f"  {db:<24} {old[db]:>4.0%} -> {e}/{len(sub)} "
              f"({e/max(1,len(sub)):.0%})", flush=True)
    # 外推全量 (S+W1 = 1100 单元 x10)
    per_call_lat = (sum(lats) / len(lats)) if lats else 0
    full_lat_min = 11000 * per_call_lat / 60 / WORKERS
    print(f"\n外推全量 (11,000 调用, {WORKERS} 并发): "
          f"≈{full_lat_min/60:.1f} h; tokens≈"
          f"{11000*ptok/n/1e6:.1f}M prompt + {11000*ctok/n/1e6:.1f}M completion",
          flush=True)

    out = {"winner": {"name": wname, "extra": wextra, "model": MODEL,
                      "maxtok": MAXTOK},
           "n_calls": n, "n_empty": n_empty,
           "empty_rate": round(n_empty / n, 4), "finish": dict(fin),
           "exec_ok": ok, "correct": corr,
           "latency_median_s": lats[len(lats)//2] if lats else None,
           "tokens": {"prompt": ptok, "completion": ctok,
                      "reasoning": rtok},
           "per_db": {db: {"n": sum(1 for r in results if r["db"] == db),
                           "empty": sum(1 for r in results
                                        if r["db"] == db and r["empty"]),
                           "exec_ok": sum(1 for r in results
                                          if r["db"] == db and r["exec_ok"])}
                      for db in old},
           "results": results}
    rp = os.path.join(HERE, "results", "pilot_v4b.json")
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n[pilot] saved -> {rp}", flush=True)
    print(f"[pilot] total {(time.time()-t0)/60:.1f} min", flush=True)


if __name__ == "__main__":
    main()

