# -*- coding: utf-8 -*-
"""gen_candidates_w3.py — W3 改写族候选生成, 双生成器。

用法: python gen_candidates_w3.py chat|v4b
  chat -> gendir gen_w3\\,  model deepseek-chat     (temperature=1.0)
  v4b  -> gendir gen_w3b\\, model deepseek-v4-pro, thinking=disabled

协议与 E2/E6 完全一致: NCAND=10, temp=1.0/top_p=0.95, logprobs=True,
max_tokens=2000, 断点续跑 (每单元满 10 行跳过)。
W3 特性: DB 恒定 (t0 快照), 每检查点 question 不同
(t0 原题, t1/t2/t3 = 轻/中/深改写); gold = gold_W3.jsonl (各 ck 同 t0)。

输出: {gendir}\\W3\\{db}\\ck{t}.jsonl + gen_summary.json
API key 只从环境变量 GATE_LLM_API_KEY 读取, 不落盘。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from build_drift_benchmark import (CKPTS, HERE, db_path_of,
                                   result_fingerprint, run_sql)


def ddl_of(db_path):
    import sqlite3
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL").fetchall()
    con.close()
    return "\n\n".join(r[0] for r in rows)


API = os.environ.get("DRIFTSQL_API_BASE", "https://api.deepseek.com") + "/chat/completions"
KEY = os.environ.get("GATE_LLM_API_KEY", "")
NCAND = 10
WORKERS = 24
MAXTOK = 2000
LOCK = threading.Lock()

SYS = ("You are an expert text-to-SQL system. Generate ONE SQLite query "
       "for the question. Output ONLY the SQL inside a ```sql code block.")
USERT = "### SQLite schema:\n{ddl}\n\n### Question:\n{q}\n\n" \
        "Write one SQLite query answering the question."

GENS = {
    "chat": {"model": "deepseek-chat", "extra": {}, "gendir": "gen_w3"},
    "v4b": {"model": "deepseek-v4-pro",
            "extra": {"thinking": {"type": "disabled"}}, "gendir": "gen_w3b"},
}


def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def extract_sql(text):
    import re
    m = re.search(r"```sql\s*(.*?)```", text, re.S | re.I)
    if not m:
        m = re.search(r"```\s*(.*?)```", text, re.S)
    s = (m.group(1) if m else text).strip()
    return s.rstrip(";").strip()


def call_api(ddl, question, model, extra, retry=3):
    body = {"model": model,
            "messages": [{"role": "system", "content": SYS},
                         {"role": "user", "content": USERT.format(
                             ddl=ddl, q=question)}],
            "temperature": 1.0, "top_p": 0.95, "max_tokens": MAXTOK,
            "logprobs": True, "top_logprobs": 1}
    body.update(extra)
    last = None
    for i in range(retry):
        try:
            req = urllib.request.Request(
                API, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + KEY})
            r = json.load(urllib.request.urlopen(req, timeout=90))
            ch = r["choices"][0]
            lp = sum(t["logprob"] for t in
                     (ch.get("logprobs") or {}).get("content", []))
            return ch["message"]["content"], lp
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"api failed: {last}")


def main():
    gen = sys.argv[1] if len(sys.argv) > 1 else "chat"
    cfg = GENS[gen]
    model, extra = cfg["model"], cfg["extra"]
    gendir = os.path.join(HERE, cfg["gendir"])
    os.makedirs(gendir, exist_ok=True)
    print(f"[gen] generator={gen} model={model} gendir={cfg['gendir']}",
          flush=True)

    gold = {}
    for ln in open(os.path.join(HERE, "gold", "gold_W3.jsonl"),
                   encoding="utf-8"):
        e = json.loads(ln)
        gold[e["qid"]] = e

    qs = [q for q in load_json(os.path.join(
        HERE, "questions", "family_W3.json")) if q.get("rewrite_ok")]

    ddl_cache = {}

    def get_ddl(path):
        if path not in ddl_cache:
            ddl_cache[path] = ddl_of(path)
        return ddl_cache[path]

    # 任务: 150 题 x t0..t3 (DB 恒定); W3 无漂移库快照, 统一用 t0 路径
    jobs = []
    for q in qs:
        for t in range(0, max(CKPTS["W2"]) + 1):  # W3 对齐 W2: 0..3
            jobs.append((q["db"], t, q, db_path_of(q["db"])))
    print(f"[jobs] units = {len(jobs)} (x{NCAND} calls)", flush=True)

    def out_path(db, t):
        d = os.path.join(gendir, "W3", db)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"ck{t}.jsonl")

    # 断点续跑
    done = defaultdict(int)
    for db, t, q, _ in jobs:
        p = out_path(db, t)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                done[(db, t, q["qid"])] = sum(
                    1 for ln in f if json.loads(ln)["qid"] == q["qid"])
    jobs = [j for j in jobs
            if done.get((j[0], j[1], j[2]["qid"]), 0) < NCAND]
    print(f"[jobs] after resume check = {len(jobs)}", flush=True)

    stats = defaultdict(lambda: {"n": 0, "ok": 0, "correct": 0, "lp": 0.0})

    def work(job):
        db, t, q, path = job
        p = out_path(db, t)
        have = done.get((db, t, q["qid"]), 0)
        need = NCAND - have
        if need <= 0:
            return
        ddl = get_ddl(path)
        question = q["question"] if t == 0 else q["rewrites"][str(t)]
        g = gold[q["qid"]]["checkpoints"].get(str(t), {})
        gold_ok = bool(g.get("exec_ok"))
        gold_h = g.get("result_hash")
        results = []
        for i in range(need):
            try:
                text, lp = call_api(ddl, question, model, extra)
                sql = extract_sql(text)
            except RuntimeError as e:
                results.append({"qid": q["qid"], "db": db, "ckpt": t,
                                "cand": have + i, "sql": "",
                                "exec_ok": False,
                                "error": str(e)[:80], "n_rows": None,
                                "result_hash": None, "correct": None,
                                "gold_broken": not gold_ok,
                                "token_logprob": None})
                continue
            ok, errc, rows = run_sql(path, sql, timeout=10)
            if ok:
                h, n = result_fingerprint(rows)
            else:
                h, n = None, None
            correct = (h == gold_h) if (ok and gold_ok) else \
                (None if not gold_ok else False)
            results.append({"qid": q["qid"], "db": db, "ckpt": t,
                            "cand": have + i, "sql": sql, "exec_ok": ok,
                            "error": errc if not ok else None,
                            "n_rows": n, "result_hash": h,
                            "correct": correct, "gold_broken": not gold_ok,
                            "token_logprob": lp})
        with LOCK:
            with open(p, "a", encoding="utf-8") as f:
                for r in results:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            s = stats[(db, t)]
            for r in results:
                s["n"] += 1
                s["ok"] += 1 if r["exec_ok"] else 0
                s["correct"] += 1 if r["correct"] is True else 0
                s["lp"] += r["token_logprob"] or 0.0

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(work, j) for j in jobs]
        for i, fu in enumerate(as_completed(futs), 1):
            fu.result()
            if i % 50 == 0:
                print(f"[gen] {i}/{len(jobs)} units "
                      f"({(time.time()-t0)/60:.1f} min)", flush=True)
    print(f"[gen] done in {(time.time()-t0)/60:.1f} min", flush=True)

    # 聚合
    summary = {}
    for db in sorted({j[0] for j in jobs} |
                     {d for d in os.listdir(os.path.join(gendir, "W3"))
                      if os.path.isdir(os.path.join(gendir, "W3", d))}):
        for t in range(0, max(CKPTS["W2"]) + 1):
            p = out_path(db, t)
            if not os.path.exists(p):
                continue
            per_q = defaultdict(list)
            for ln in open(p, encoding="utf-8"):
                r = json.loads(ln)
                per_q[r["qid"]].append(r)
            n = ok = corr = 0
            lp_sum = share_sum = 0.0
            nq = 0
            for qid, rs in per_q.items():
                n += len(rs)
                ok += sum(1 for r in rs if r["exec_ok"])
                corr += sum(1 for r in rs if r["correct"] is True)
                lp_sum += sum(r["token_logprob"] or 0.0 for r in rs)
                classes = defaultdict(int)
                for r in rs:
                    classes[r["result_hash"] or "EXECFAIL"] += 1
                share_sum += max(classes.values()) / len(rs)
                nq += 1
            if n == 0:
                continue
            summary[f"W3/{db}/ck{t}"] = {
                "questions": nq, "candidates": n,
                "ex_rate": round(corr / n, 4),
                "exec_ok_rate": round(ok / n, 4),
                "mean_share": round(share_sum / max(1, nq), 4),
                "mean_logprob": round(lp_sum / n, 4)}
    with open(os.path.join(gendir, "gen_summary.json"), "w",
              encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    for k in sorted(summary):
        s = summary[k]
        print(f"[sum] {k}: ex={s['ex_rate']} ok={s['exec_ok_rate']} "
              f"share={s['mean_share']}", flush=True)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()

