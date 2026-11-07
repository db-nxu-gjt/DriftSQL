# -*- coding: utf-8 -*-
"""build_gold_w3.py — W3 gold 标签构建 (零 API, 本地执行)。

W3 为同意图改写族: DB 恒定 → 各检查点 (t0..t3) gold = t0 结果 (benign 负控制,
标签全 stable)。对 family_W3.json 中 rewrite_ok 的题, 用题目自带 gold sql 在
t0 快照上执行, 哈希填满 4 个检查点; 并与 gold_W2 的 ck0 哈希交叉核对 (不一致
仅告警不阻断, 因 family_W2.sql 与 gold 构建时点可能不同)。

输出: gold/gold_W3.jsonl  [{qid, db, difficulty, checkpoints: {"0".."3"}}]
"""
from __future__ import annotations

import json
import os

from build_drift_benchmark import HERE, db_path_of, result_fingerprint, run_sql


def main():
    qs = [q for q in json.load(open(os.path.join(
        HERE, "questions", "family_W3.json"), encoding="utf-8"))
        if q.get("rewrite_ok")]
    w2 = {}
    for ln in open(os.path.join(HERE, "gold", "gold_W2.jsonl"),
                   encoding="utf-8"):
        r = json.loads(ln)
        w2[r["qid"]] = r

    out, mism = [], 0
    for q in qs:
        ok, err, rows = run_sql(db_path_of(q["db"]), q["sql"], timeout=30)
        if not ok:
            print(f"[gold] EXEC FAIL qid={q['qid']} db={q['db']}: {err}")
            continue
        h, n = result_fingerprint(rows)
        ck = {str(t): {"exec_ok": True, "n_rows": n, "result_hash": h}
              for t in range(0, 4)}
        g2 = w2.get(q["qid"], {}).get("checkpoints", {}).get("0", {})
        if g2.get("result_hash") and g2["result_hash"] != h:
            mism += 1
            print(f"[gold] HASH MISMATCH vs gold_W2 qid={q['qid']}")
        out.append({"qid": q["qid"], "db": q["db"],
                    "difficulty": q.get("difficulty"), "checkpoints": ck})

    p = os.path.join(HERE, "gold", "gold_W3.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[gold] W3 gold rows={len(out)} (mismatch_vs_W2={mism}) -> {p}")


if __name__ == "__main__":
    main()
