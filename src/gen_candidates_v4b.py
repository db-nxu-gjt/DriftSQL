# -*- coding: utf-8 -*-
# gen_candidates_v4b.py — 第二生成器变体 B: deepseek-v4-pro, thinking=disabled。
# 修复 gen_v4 伪影 (effort=low 下 reasoning 耗尽 max_tokens → 59% 空内容 SQL):
# 2026-09-29 试点 (pilot_v4b.py) 验证 thinking=disabled + MAXTOK=2000
# 空内容率 0/70, completion≈69 tok/call, 延迟中位 10.7s。
# 输出 gen_v4b/, 其余协议 (NCAND=10, temp=1.0/top_p=0.95, logprobs) 不变。
# -*- coding: utf-8 -*-
"""gen_candidates.py — DriftSQL 各检查点候选生成 + 严格标注（API 阶段）。

对每个 (family, db, checkpoint, question) 生成 10 个候选（deepseek-chat,
n=1 x 10 次调用, temp=1.0/top_p=0.95, 带 token logprobs），在对应漂移快照上
执行并与漂移感知 gold 指纹比对标注。支持断点续跑（每题满 10 行即跳过）。

输出:
  gen/{family}/{db}/ck{t}.jsonl   每行一个候选标注
  gen/gen_summary.json            每个 (family,db,ckpt) 的聚合统计
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from build_drift_benchmark import (  # noqa: E402
    CKPTS, FAMILY_DBS, HERE, db_path_of, result_fingerprint, run_sql)

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


def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def ddl_of(db_path):
    con_db = __import__("sqlite3").connect(db_path)
    rows = con_db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL").fetchall()
    con_db.close()
    return "\n\n".join(r[0] for r in rows)


def extract_sql(text):
    m = re.search(r"```sql\s*(.*?)```", text, re.S | re.I)
    if not m:
        m = re.search(r"```\s*(.*?)```", text, re.S)
    s = (m.group(1) if m else text).strip()
    return s.rstrip(";").strip()


def call_api(ddl, question, retry=3):
    body = {"model": "deepseek-v4-pro",
            "messages": [{"role": "system", "content": SYS},
                         {"role": "user", "content": USERT.format(
                             ddl=ddl, q=question)}],
            "thinking": {"type": "disabled"}, "temperature": 1.0,
            "top_p": 0.95, "max_tokens": MAXTOK,
            "logprobs": True, "top_logprobs": 1}
    last = None
    for i in range(retry):
        try:
            req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                         headers={
                                             "Content-Type": "application/json",
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
    assert KEY, "set GATE_LLM_API_KEY"
    gendir = os.path.join(HERE, "gen_v4b")
    os.makedirs(gendir, exist_ok=True)

    # gold: fam -> {qid: {"db":..., "checkpoints": {t: gold}}}
    gold = {}
    for fam in ("S", "V", "W2", "W1"):
        for line in open(os.path.join(HERE, "gold", f"gold_{fam}.jsonl"),
                         encoding="utf-8"):
            e = json.loads(line)
            gold.setdefault(fam, {})[e["qid"]] = e

    ddl_cache = {}

    def get_ddl(path):
        if path not in ddl_cache:
            ddl_cache[path] = ddl_of(path)
        return ddl_cache[path]

    # 任务清单
    jobs = []
    for fam in ("S", "V", "W2"):
        qs = load_json(os.path.join(HERE, "questions", f"family_{fam}.json"))
        by_db = defaultdict(list)
        for q in qs:
            by_db[q["db"]].append(q)
        for db in sorted(by_db):
            for t in range(0, max(CKPTS[fam]) + 1):
                p = (db_path_of(db) if t == 0 else os.path.join(
                    HERE, "dbs", fam, db, f"ck{t}.sqlite"))
                if t > 0 and not os.path.exists(p):
                    continue
                for q in by_db[db]:
                    jobs.append((fam, db, t, q, p))
    qs_w1 = load_json(os.path.join(HERE, "questions", "family_W1.json"))
    for q in qs_w1:
        jobs.append(("W1", q["db"], 0, q, db_path_of(q["db"])))
    print(f"[jobs] total question-checkpoint units = {len(jobs)} "
          f"(x{NCAND} calls)", flush=True)

    # 断点续跑: 已满 NCAND 行的 (fam,db,t,qid) 跳过
    done = set()

    def out_path(fam, db, t):
        d = os.path.join(gendir, fam, db)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"ck{t}.jsonl")

    cnt = defaultdict(int)
    for fam, db, t, q, _ in jobs:
        p = out_path(fam, db, t)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                cnt[(fam, db, t, q["qid"])] = sum(
                    1 for ln in f if json.loads(ln)["qid"] == q["qid"])
    jobs = [j for j in jobs
            if cnt.get((j[0], j[1], j[2], j[3]["qid"]), 0) < NCAND]
    print(f"[jobs] after resume check = {len(jobs)}", flush=True)

    stats = defaultdict(lambda: {"n": 0, "ok": 0, "correct": 0,
                                 "gold_broken": 0, "lp": 0.0})

    def work(job):
        fam, db, t, q, path = job
        p = out_path(fam, db, t)
        have = cnt.get((fam, db, t, q["qid"]), 0)
        need = NCAND - have
        if need <= 0:
            return
        ddl = get_ddl(path)
        g = gold[fam][q["qid"]]["checkpoints"].get(
            str(t) if fam != "W1" else "0", {})
        gold_ok = bool(g.get("exec_ok"))
        gold_h = g.get("result_hash")
        results = []
        for i in range(need):
            try:
                text, lp = call_api(ddl, q["question"])
                sql = extract_sql(text)
            except RuntimeError as e:
                sql, lp = "", None
                err = str(e)[:80]
                results.append({"qid": q["qid"], "db": db, "ckpt": t,
                                "cand": have + i, "sql": "", "exec_ok": False,
                                "error": "api:" + err, "n_rows": None,
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
            key = (fam, db, t)
            s = stats[key]
            for r in results:
                s["n"] += 1
                s["ok"] += 1 if r["exec_ok"] else 0
                s["correct"] += 1 if r["correct"] is True else 0
                s["gold_broken"] += 1 if r["gold_broken"] else 0
                s["lp"] += r["token_logprob"] or 0.0

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(work, j) for j in jobs]
        for i, fu in enumerate(as_completed(futs), 1):
            fu.result()
            if i % 50 == 0:
                el = time.time() - t0
                print(f"[gen] {i}/{len(jobs)} units "
                      f"({el/60:.1f} min)", flush=True)
    print(f"[gen] done in {(time.time()-t0)/60:.1f} min", flush=True)

    # 聚合统计（含 share = 同指纹占比, execfail 自成一类）
    summary = {}
    for fam in ("S", "V", "W2", "W1"):
        for db in FAMILY_DBS + sorted({j[1] for j in jobs if j[0] == "W1"}):
            for t in ([0] if fam == "W1" else range(0, max(CKPTS[fam]) + 1)):
                p = out_path(fam, db, t)
                if not os.path.exists(p):
                    continue
                per_q = defaultdict(list)
                for ln in open(p, encoding="utf-8"):
                    r = json.loads(ln)
                    per_q[r["qid"]].append(r)
                n = ok = corr = gb = 0
                lp_sum = share_sum = 0.0
                nq = 0
                for qid, rs in per_q.items():
                    n += len(rs)
                    ok += sum(1 for r in rs if r["exec_ok"])
                    corr += sum(1 for r in rs if r["correct"] is True)
                    gb += sum(1 for r in rs if r["gold_broken"])
                    lp_sum += sum(r["token_logprob"] or 0.0 for r in rs)
                    classes = defaultdict(int)
                    for r in rs:
                        classes[r["result_hash"] or "EXECFAIL"] += 1
                    share_sum += max(classes.values()) / len(rs)
                    nq += 1
                if n == 0:
                    continue
                summary[f"{fam}/{db}/ck{t}"] = {
                    "questions": nq, "candidates": n,
                    "ex_rate": round(corr / max(1, n - gb), 4),
                    "exec_ok_rate": round(ok / n, 4),
                    "gold_broken_rate": round(gb / n, 4),
                    "mean_share": round(share_sum / max(1, nq), 4),
                    "mean_logprob": round(lp_sum / n, 4)}
    with open(os.path.join(gendir, "gen_summary.json"), "w",
              encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    for k in sorted(summary):
        s = summary[k]
        print(f"[sum] {k}: ex={s['ex_rate']} ok={s['exec_ok_rate']} "
              f"share={s['mean_share']} goldbrk={s['gold_broken_rate']}",
              flush=True)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()

