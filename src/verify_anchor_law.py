"""Verify hypergeometric anchor-coverage law at m>1 (E11-T2 semantics).

Event = database with >=1 ever-drifted question. K = ever-drifted questions
in the db, n = db pool size. P_hit(m) = 1 - C(n-K,m)/C(n,m), m_eff=min(m,n).
Predicted family power = mean over event dbs. Compare with combined_monitor.
"""
import json
import os
from math import comb

HERE = os.path.dirname(os.path.abspath(__file__))
MEASURED = {"S":  {1: 0.761, 3: 0.971, 5: 0.995, 10: 1.0, 25: 1.0},
            "V":  {1: 0.455, 3: 0.825, 5: 0.93, 10: 0.994, 25: 1.0},
            "W2": {1: 0.218, 3: 0.503, 5: 0.625, 10: 0.813, 25: 0.965}}

for fam in ["S", "V", "W2"]:
    path = os.path.join(HERE, "gold", f"gold_{fam}.jsonl")
    by_db = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            g = json.loads(line)
            ck = g["checkpoints"]
            c0 = ck.get("0")
            if not c0 or not c0.get("exec_ok"):
                continue  # no valid baseline: excluded from pools, matches E11
            h0 = c0["result_hash"]
            drifted = any(
                (not ck[str(t)].get("exec_ok"))
                or ck[str(t)].get("result_hash") != h0
                for t in range(1, 6) if str(t) in ck)
            d = by_db.setdefault(g["db"], {"n": 0, "K": 0})
            d["n"] += 1
            if drifted:
                d["K"] += 1
    events = {db: v for db, v in by_db.items() if v["K"] > 0}
    row = []
    for m in [1, 3, 5, 10, 25]:
        pred = sum(1 - (comb(v["n"] - v["K"], min(m, v["n"]))
                       / comb(v["n"], min(m, v["n"])))
                   for v in events.values()) / len(events)
        row.append((m, pred, MEASURED[fam][m]))
    kn = sum(v["K"] / v["n"] for v in events.values()) / len(events)
    print(fam, f"event_dbs={len(events)} meanK/n={kn:.3f}",
          "  ".join(f"m={m}: pred={p:.3f} meas={mm:.3f}" for m, p, mm in row))
