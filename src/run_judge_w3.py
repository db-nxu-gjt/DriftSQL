# -*- coding: utf-8 -*-
"""run_judge_w3.py — W3 族 judge (双生成器通用)。

用法: python run_judge_w3.py gen_w3|gen_w3b
协议与 run_judge_baseline*.py 一致 (deepseek-chat, temp=0, max_tokens=8);
读 {gendir}/W3/{db}/ck{t}.jsonl (只评满 10 行单元), 写 {gendir}/judge/W3/...
断点续跑; API key 只从环境变量 GATE_LLM_API_KEY 读取, 不落盘。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from build_drift_benchmark import CKPTS, HERE, db_path_of, result_fingerprint, run_sql

API = os.environ.get("DRIFTSQL_API_BASE", "https://api.deepseek.com") + "/chat/completions"
KEY = os.environ.get("GATE_LLM_API_KEY", "")
WORKERS = 24
NCAND = 10
MAXTOK = 8
LOCK = threading.Lock()

JT = ("You are given an SQLite schema, a question, and a candidate SQL "
      "query.\n### Question:\n{q}\n\n### Candidate SQL:\n```sql\n{sql}\n```\n"
      "\nRate your confidence that the candidate SQL returns the correct "
      "answer to the question (0 = certainly wrong, 100 = certainly "
      "correct). Respond with ONLY a number 0-100.")


def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def modal_cand(rows):
    cls = Counter(r["result_hash"] or "EXECFAIL" for r in rows)
    mode, _ = max(cls.items(), key=lambda kv: (kv[1], kv[0]))
    for r in sorted(rows, key=lambda r: r["cand"]):
        if (r["result_hash"] or "EXECFAIL") == mode:
            return r
    return rows[0]


def call_judge(sql, question, retry=3):
    body = {"model": "deepseek-chat",
            "messages": [{"role": "user", "content": JT.format(
                q=question, sql=sql)}],
            "temperature": 0.0, "max_tokens": MAXTOK}
    last = None
    for i in range(retry):
        try:
            req = urllib.request.Request(
                API, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + KEY})
            r = json.load(urllib.request.urlopen(req, timeout=60))
            txt = (r["choices"][0]["message"]["content"] or "").strip()
            digits = "".join(ch for ch in txt if ch.isdigit())
            return min(100, int(digits[:3])) if digits else None
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"api failed: {last}")


def main():
    gendir = sys.argv[1] if len(sys.argv) > 1 else "gen_w3"
    assert KEY, "set GATE_LLM_API_KEY"
    qmap = {q["qid"]: q for q in load_json(
        os.path.join(HERE, "questions", "family_W3.json"))}
    jroot = os.path.join(HERE, gendir, "judge", "W3")

    jobs = []
    n_partial = 0
    for db in sorted(os.listdir(os.path.join(HERE, gendir, "W3"))):
        d = os.path.join(HERE, gendir, "W3", db)
        if not os.path.isdir(d):
            continue
        for t in range(0, max(CKPTS["W2"]) + 1):
            gp = os.path.join(d, f"ck{t}.jsonl")
            if not os.path.exists(gp):
                continue
            per_q = defaultdict(list)
            for ln in open(gp, encoding="utf-8"):
                r = json.loads(ln)
                per_q[r["qid"]].append(r)
            for qid, rs in per_q.items():
                if len(rs) != NCAND:
                    n_partial += 1
                    continue
                jobs.append((db, t, qid, modal_cand(rs)))
    print(f"[judge] {gendir} units = {len(jobs)} (partial={n_partial})",
          flush=True)

    os.makedirs(jroot, exist_ok=True)
    done = set()
    for db in os.listdir(jroot):
        for fn in os.listdir(os.path.join(jroot, db)):
            for ln in open(os.path.join(jroot, db, fn), encoding="utf-8"):
                r = json.loads(ln)
                done.add((db, r["ckpt"], r["qid"]))
    jobs = [j for j in jobs if (j[0], j[1], j[2]) not in done]
    print(f"[judge] after resume = {len(jobs)}", flush=True)

    def work(job):
        db, t, qid, mc = job
        q = qmap[qid]
        question = q["question"] if t == 0 else (q["rewrites"].get(str(t)) or
                                                 q["question"])
        try:
            prob = call_judge(mc.get("sql") or "", question)
            err = None
        except RuntimeError as e:
            prob, err = None, str(e)[:80]
        if mc.get("exec_ok"):
            ok, _e, rows = run_sql(db_path_of(db), mc["sql"], timeout=10)
            h, _n = result_fingerprint(rows) if ok else (None, None)
        else:
            ok, h = False, None
        rec = {"qid": qid, "ckpt": t, "sql_used": mc.get("sql"),
               "exec_ok": ok, "result_hash": h,
               "judge_prob": prob, **({"judge_err": err} if err else {})}
        with LOCK:
            os.makedirs(os.path.join(jroot, db), exist_ok=True)
            with open(os.path.join(jroot, db, f"ck{t}.jsonl"), "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(work, j) for j in jobs]
        for i, fu in enumerate(as_completed(futs), 1):
            fu.result()
            if i % 200 == 0:
                print(f"[judge] {i}/{len(jobs)} "
                      f"({(time.time()-t0)/60:.1f} min)", flush=True)

    probs = []
    for db in os.listdir(jroot):
        for fn in os.listdir(os.path.join(jroot, db)):
            for ln in open(os.path.join(jroot, db, fn), encoding="utf-8"):
                r = json.loads(ln)
                if r.get("judge_prob") is not None:
                    probs.append(r["judge_prob"])
    print(f"[judge] done in {(time.time()-t0)/60:.1f} min; "
          f"n={len(probs)} mean={sum(probs)/max(1,len(probs)):.1f}",
          flush=True)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()

