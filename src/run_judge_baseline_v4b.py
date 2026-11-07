# -*- coding: utf-8 -*-
"""run_judge_baseline_v4b.py — judge-2: 第二生成器 (gen_v4b) 众数候选复评。
协议与 run_judge_baseline.py 完全一致 (deepseek-chat, temp=0, max_tokens=8);
差异仅: 读 gen_v4b/{fam}/{db}/ck{t}.jsonl, 写 gen_v4b/judge/{fam}/{db}/ck{t}.jsonl,
且只评满单元 (恰 NCAND=10 行, 避免部分单元众数不稳定)。

输入:  gen_v4b/{fam}/{db}/ck{t}.jsonl   (gen_candidates_v4b.py 产出)
输出:  gen_v4b/judge/{fam}/{db}/ck{t}.jsonl 每行:
       {qid, ckpt, sql_used, exec_ok, result_hash, judge_prob[, judge_err]}

众数候选: result_hash 最大类 (EXECFAIL 自成一类), 平局取 cand 最小。
断点续跑: 已有输出的 (fam,db,t,qid) 跳过。API key 只从环境变量读取, 不落盘。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from build_drift_benchmark import CKPTS, FAMILY_DBS, HERE, db_path_of

API = os.environ.get("DRIFTSQL_API_BASE", "https://api.deepseek.com") + "/chat/completions"
KEY = os.environ.get("GATE_LLM_API_KEY", "")
WORKERS = 24
NCAND = 10
GENDIR = "gen_v4b"
LOCK = threading.Lock()

SYS = ("You are a strict SQL grader. Given a SQLite schema, a question and "
       "a candidate SQL, judge whether the SQL correctly answers the question.")
USERT = ("### SQLite schema:\n{ddl}\n\n### Question:\n{q}\n\n"
         "### Candidate SQL:\n```sql\n{sql}\n```\n\n"
         "On a scale of 0-100, how confident are you that the candidate SQL "
         "correctly answers the question? Reply with ONLY one integer 0-100.")


def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def ddl_of(db_path):
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL").fetchall()
    con.close()
    return "\n\n".join(r[0] for r in rows)


def call_judge(ddl, question, sql, retry=3):
    """返回 (text, err); err 非 None 表示重试后仍失败。"""
    body = {"model": "deepseek-chat",
            "messages": [{"role": "system", "content": SYS},
                         {"role": "user", "content": USERT.format(
                             ddl=ddl, q=question, sql=sql)}],
            "temperature": 0.0, "max_tokens": 8}
    last = None
    for i in range(retry):
        try:
            req = urllib.request.Request(
                API, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + KEY})
            r = json.load(urllib.request.urlopen(req, timeout=60))
            return r["choices"][0]["message"]["content"], None
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    return None, str(last)[:120]


def parse_prob(text):
    if not text:
        return None
    m = re.search(r"\b(\d{1,3})\b", text)
    return min(100, max(0, int(m.group(1)))) if m else None


def modal_cand(rs):
    """众数候选: result_hash 最大类 (EXECFAIL 自成一类), 平局取 cand 最小。"""
    def cls(r):
        return r["result_hash"] or "EXECFAIL"
    cnt = Counter(cls(r) for r in rs)
    best = max(cnt.items(), key=lambda kv: (
        kv[1], -min(r["cand"] for r in rs if cls(r) == kv[0])))
    same = [r for r in rs if cls(r) == best[0]]
    return min(same, key=lambda r: r["cand"])


def main():
    assert KEY, "set GATE_LLM_API_KEY env var first"

    qmap = {}  # fam -> qid -> question 记录
    for fam in ("S", "V", "W2", "W1"):
        for q in load_json(os.path.join(HERE, "questions",
                                        f"family_{fam}.json")):
            qmap.setdefault(fam, {})[q["qid"]] = q

    # 任务清单: 每个 (fam,db,t,qid) 一条, 附众数候选; 只取满单元 (恰 NCAND 行)
    jobs = []
    n_partial = 0
    for fam in ("S", "V", "W2"):
        for db in FAMILY_DBS:
            for t in range(0, max(CKPTS[fam]) + 1):
                gp = os.path.join(HERE, GENDIR, fam, db, f"ck{t}.jsonl")
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
                    jobs.append((fam, db, t, qid, modal_cand(rs)))
    w1dir = os.path.join(HERE, GENDIR, "W1")
    if os.path.isdir(w1dir):
        for db in sorted(os.listdir(w1dir)):
            gp = os.path.join(w1dir, db, "ck0.jsonl")
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
                jobs.append(("W1", db, 0, qid, modal_cand(rs)))
    print(f"[judge] units = {len(jobs)} (partial skipped = {n_partial})",
          flush=True)

    # 断点续跑
    def out_path(fam, db, t):
        d = os.path.join(HERE, GENDIR, "judge", fam, db)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"ck{t}.jsonl")

    done = set()
    jroot = os.path.join(HERE, GENDIR, "judge")
    if os.path.isdir(jroot):
        for fam in os.listdir(jroot):
            for db in os.listdir(os.path.join(jroot, fam)):
                for fn in os.listdir(os.path.join(jroot, fam, db)):
                    t = int(fn[2:-6])
                    for ln in open(os.path.join(jroot, fam, db, fn),
                                   encoding="utf-8"):
                        done.add((fam, db, t, json.loads(ln)["qid"]))
    jobs = [j for j in jobs if (j[0], j[1], j[2], j[3]) not in done]
    print(f"[judge] after resume = {len(jobs)}", flush=True)

    ddl_cache = {}

    def get_ddl(fam, db, t):
        p = db_path_of(db) if t == 0 else os.path.join(
            HERE, "dbs", fam, db, f"ck{t}.sqlite")
        if p not in ddl_cache:
            ddl_cache[p] = ddl_of(p)
        return ddl_cache[p]

    def work(job):
        fam, db, t, qid, m = job
        q = qmap[fam][qid]
        ddl = get_ddl(fam, db, t)
        text, err = call_judge(ddl, q["question"], m["sql"] or "(empty)")
        row = {"qid": qid, "ckpt": t,
               "sql_used": (m["sql"] or "")[:200],
               "exec_ok": bool(m["exec_ok"]),
               "result_hash": m["result_hash"],
               "judge_prob": parse_prob(text)}
        if err:
            row["judge_err"] = err
        with LOCK:
            with open(out_path(fam, db, t), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(work, j) for j in jobs]
        for i, fu in enumerate(as_completed(futs), 1):
            fu.result()
            if i % 200 == 0:
                el = time.time() - t0
                print(f"[judge] {i}/{len(jobs)} ({el/60:.1f} min)", flush=True)
    print(f"[judge] done in {(time.time()-t0)/60:.1f} min", flush=True)

    # 简要汇总
    agg = defaultdict(list)
    jroot = os.path.join(HERE, GENDIR, "judge")
    for fam in ("S", "V", "W2", "W1"):
        froot = os.path.join(jroot, fam)
        if not os.path.isdir(froot):
            continue
        for db in os.listdir(froot):
            for fn in os.listdir(os.path.join(froot, db)):
                for ln in open(os.path.join(froot, db, fn),
                               encoding="utf-8"):
                    r = json.loads(ln)
                    if r.get("judge_prob") is not None:
                        agg[fam].append(r["judge_prob"])
    for fam, ps in sorted(agg.items()):
        print(f"[judge] {fam}: n={len(ps)} mean={sum(ps)/len(ps):.1f}",
              flush=True)


if __name__ == "__main__":
    main()

