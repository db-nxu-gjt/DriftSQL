# -*- coding: utf-8 -*-
"""build_w3_rewrites.py — W3 改写族构建: 同意图逐级严重度改写 (t1 轻/t2 中/t3 深)。

设计 (对齐 W2 口径): 复用 family_W2.json 的 150 题 (5 轻量库, 自带 gold sql),
每题生成 3 级改写, DB 恒定 → gold 各检查点 = t0 (benign/负控制族, 度量监控在
真实良性负载变化下的误报)。

协议: deepseek-chat 生成改写 (temp=0.7) + deepseek-chat judge 语义等价校验
(temp=0, yes/no); 不等价则重试 1 次; 仍失败则标记 rewrite_failed (评测时剔除)。
断点续跑: family_W3.json 已存在的 qid 跳过。

输出: questions/family_W3.json
  [{qid, db, difficulty, question, sql, base_family: "W2",
    rewrites: {"1","2","3"}, rewrite_judge: {"1": 1/0}}]
API key 只从环境变量 GATE_LLM_API_KEY 读取, 不落盘。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from build_drift_benchmark import HERE

API = os.environ.get("DRIFTSQL_API_BASE", "https://api.deepseek.com") + "/chat/completions"
KEY = os.environ.get("GATE_LLM_API_KEY", "")
MODEL = "deepseek-chat"
WORKERS = 12
LOCK = threading.Lock()

SEV_PROMPT = {
    1: ("Rephrase the question with MINIMAL edits: replace 1-2 words with "
        "synonyms and/or slightly reorder clauses. Keep every entity, filter, "
        "aggregation and the exact same information need."),
    2: ("Rephrase the question SUBSTANTIALLY: use a different sentence "
        "structure and different wording, while keeping every entity, filter, "
        "aggregation and the exact same information need."),
    3: ("Rewrite the question as if a completely different user with a "
        "different vocabulary and phrasing style asked the same thing: you "
        "may change voice, sentence order and wording, but the requested "
        "information and all conditions must be IDENTICAL (same answer)."),
}
REWRITE_SYS = ("You rewrite English natural-language questions about a "
               "database. Preserve meaning EXACTLY. Output ONLY the rewritten "
               "question, no quotes, no explanation.")
JUDGE_USERT = ("Q1: {q1}\n\nQ2: {q2}\n\nDoes Q2 ask for exactly the same "
               "information as Q1 (same entities, conditions, aggregation and "
               "output)? Answer ONLY yes or no.")


def call_api(messages, temp, max_tokens, retry=3):
    body = {"model": MODEL, "messages": messages,
            "temperature": temp, "max_tokens": max_tokens}
    last = None
    for i in range(retry):
        try:
            req = urllib.request.Request(
                API, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + KEY})
            r = json.load(urllib.request.urlopen(req, timeout=60))
            return r["choices"][0]["message"]["content"] or ""
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"api failed: {last}")


def rewrite_one(q, sev):
    text = call_api(
        [{"role": "system", "content": REWRITE_SYS},
         {"role": "user", "content": SEV_PROMPT[sev] +
          "\n\nOriginal question: " + q["question"]}],
        temp=0.7, max_tokens=300)
    return text.strip().strip('"').strip()


def judge_same(q1, q2):
    try:
        a = call_api([{"role": "user",
                       "content": JUDGE_USERT.format(q1=q1, q2=q2)}],
                     temp=0.0, max_tokens=4)
        return 1 if "yes" in a.lower() else 0
    except RuntimeError:
        return -1  # judge api 失败, 视为未校验


def process(q):
    rewrites, judges = {}, {}
    for sev in (1, 2, 3):
        for attempt in (1, 2):
            try:
                r = rewrite_one(q, sev)
            except RuntimeError:
                r = ""
            if not r:
                continue
            j = judge_same(q["question"], r)
            if j == 1:
                rewrites[str(sev)] = r
                judges[str(sev)] = 1
                break
            judges[str(sev)] = j if j == 0 else -1
        else:
            rewrites.setdefault(str(sev), None)
    return {"qid": q["qid"], "db": q["db"],
            "difficulty": q.get("difficulty"),
            "question": q["question"], "sql": q["sql"],
            "base_family": "W2",
            "rewrites": rewrites, "rewrite_judge": judges,
            "rewrite_ok": all(rewrites[str(s)] for s in (1, 2, 3))}


def main():
    assert KEY, "set GATE_LLM_API_KEY"
    src = json.load(open(os.path.join(HERE, "questions", "family_W2.json"),
                         encoding="utf-8"))
    outp = os.path.join(HERE, "questions", "family_W3.json")
    done = {}
    if os.path.exists(outp):
        done = {q["qid"]: q for q in
                json.load(open(outp, encoding="utf-8"))}
    todo = [q for q in src if q["qid"] not in done]
    print(f"[w3] total={len(src)} done={len(done)} todo={len(todo)}",
          flush=True)

    t0 = time.time()
    n_ok = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(process, q): q["qid"] for q in todo}
        for i, fu in enumerate(as_completed(futs), 1):
            rec = fu.result()
            with LOCK:
                done[rec["qid"]] = rec
                n_ok += 1 if rec["rewrite_ok"] else 0
                with open(outp, "w", encoding="utf-8") as f:
                    json.dump(sorted(done.values(),
                                     key=lambda x: int(x["qid"])),
                              f, ensure_ascii=False, indent=1)
            if i % 25 == 0:
                print(f"[w3] {i}/{len(todo)} ({(time.time()-t0)/60:.1f} min) "
                      f"ok={n_ok}", flush=True)

    fails = [q["qid"] for q in done.values() if not q.get("rewrite_ok")]
    print(f"[w3] done in {(time.time()-t0)/60:.1f} min, "
          f"rewrite_ok={len(done)-len(fails)}/{len(done)}", flush=True)
    if fails:
        print(f"[w3] rewrite_failed qids: {fails[:20]}"
              f"{' ...' if len(fails) > 20 else ''}", flush=True)
    print(f"[w3] saved -> {outp}", flush=True)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()

