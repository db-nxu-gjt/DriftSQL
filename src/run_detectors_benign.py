# -*- coding: utf-8 -*-
"""run_detectors_benign.py — E10b: V-benign 控制族检测器 FAR 评测 (零 API)。

把 V 族已存候选池 (chat: gen/V, v4b: gen_v4b/V; 生成只依赖 DDL+问题 → 池 iid,
复用合法) 的每条 SQL 在良性快照 dbs_vbenign/V/{db}/b{j}.sqlite 上本地重执行,
以 t0 存档结果为基线计算 D5 (众数迁移) / D6 (指纹Jaccard) / D1 (EX衰减) 的
误报率; D8 (gold) 由 build_v_benign 的 150/150 哈希稳定门槛保证为 0。
D2/D3/D4/D7 是生成侧信号 (logprob/judge/correct), 数据侧良性演化对它们
结构性不可见, 不在本控制族的检验范围。

良性点 b_j 的候选池 = 已存的真实 ck j 池 (iid 重采样噪声结构与真实检测器一致)。
所有 (q, b_j) 均为 stable (已验证良性) → 任何筛查报警都是误报。

输出: results/benign_control.md + results/benign_control.json
      (json 含逐题 series, 供 run_combined_monitor.py 复用)
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict

from build_drift_benchmark import (HERE, FAMILY_DBS, run_sql,
                                   result_fingerprint)
from run_detectors import (DETS, DETN, TH_EX, TH_JAC, group_metrics,
                           load_json, load_jsonl, jac_dist)

SIDES = [("chat", "gen"), ("v4b", "gen_v4b")]
BENDIR = os.path.join(HERE, "dbs_vbenign", "V")
NB = 5


def pool_stats(rows):
    """池统计 (与 run_detectors.build_series 同口径)。"""
    cls = Counter(r["result_hash"] or "EXECFAIL" for r in rows)
    mode, cnt = max(cls.items(), key=lambda kv: (kv[1], kv[0]))
    return {"n": len(rows), "ex": sum(1 for r in rows if r["exec_ok"]) / len(rows),
            "cls": dict(cls), "mode": mode}


def first_alarm(states):
    return next((t for t in sorted(states) if states[t]), None)


def main():
    t0 = time.time()
    with open(os.path.join(HERE, "questions", "family_V.json"),
              encoding="utf-8") as f:
        qs_all = json.load(f)
    gold = {}
    for r in load_jsonl(os.path.join(HERE, "gold", "gold_V.jsonl")):
        g0 = r["checkpoints"].get("0", {})
        gold[r["qid"]] = {"db": r["db"],
                          "valid": bool(g0.get("exec_ok") and g0.get("result_hash"))}
    manifest = load_json(os.path.join(HERE, "transforms",
                                      "manifest_Vbenign.json"))

    res = {"build": {"gate": manifest["gate"],
                    "dbs": {db: {"churn_cols": m["churn_cols"],
                                 "dup_tables": m["dup_tables"],
                                 "dup_survivors": m["dup_ref_tables_survivors"],
                                 "b5_rows_changed": m["row_stats"]["5"]}
                            for db, m in manifest["dbs"].items()},
                    "verification": {db: m["verification_per_ck"]
                                     for db, m in manifest["dbs"].items()}},
           "sides": {}}

    for side, gendir in SIDES:
        evals, series = [], defaultdict(dict)
        skipped = 0
        exec_counts = {"sql": 0, "fail": 0}
        for db in FAMILY_DBS:
            qs = [q for q in qs_all if q["db"] == db
                  and gold.get(q["qid"], {}).get("valid")]
            root = os.path.join(HERE, gendir, "V", db)
            rows0 = load_jsonl(os.path.join(root, "ck0.jsonl"))
            per0 = defaultdict(list)
            for r in rows0:
                if r.get("sql"):
                    per0[r["qid"]].append(r)
            perj = {}                       # j -> {qid: rows} (一次加载)
            for j in range(1, NB + 1):
                pj = defaultdict(list)
                for r in load_jsonl(os.path.join(root, f"ck{j}.jsonl")):
                    if r.get("sql"):
                        pj[r["qid"]].append(r)
                perj[j] = pj
            for q in qs:
                if q["qid"] not in per0:
                    continue
                s0 = pool_stats(per0[q["qid"]])
                series[str(q["qid"])]["0"] = s0
                ts, truth, st_d5, st_d6, st_d1 = [], [], {}, {}, {}
                for j in range(1, NB + 1):
                    rows = perj[j].get(q["qid"])
                    if not rows:
                        skipped += 1
                        continue
                    bpath = os.path.join(BENDIR, db, f"b{j}.sqlite")
                    out = []
                    for r in rows:
                        ok, err, rws = run_sql(bpath, r["sql"])
                        exec_counts["sql"] += 1
                        if ok:
                            h, _ = result_fingerprint(rws)
                            out.append({"exec_ok": True, "result_hash": h})
                        else:
                            exec_counts["fail"] += 1
                            out.append({"exec_ok": False, "result_hash": None})
                    sj = pool_stats(out)
                    series[str(q["qid"])][str(j)] = sj
                    ts.append(j)
                    truth.append(False)
                    st_d5[j] = sj["mode"] != s0["mode"]
                    st_d6[j] = jac_dist(s0["cls"], sj["cls"]) >= TH_JAC
                    st_d1[j] = s0["ex"] - sj["ex"] >= TH_EX
                evals.append({
                    "qid": q["qid"], "db": db, "onset": None, "ts": ts,
                    "truth": truth,
                    "states": {"D5_mode": st_d5, "D6_jac": st_d6,
                               "D1_ex": st_d1},
                    "alarms": {**{d: None for d in DETS},
                               "D5_mode": first_alarm(st_d5),
                               "D6_jac": first_alarm(st_d6),
                               "D1_ex": first_alarm(st_d1)}})
        gm = group_metrics(evals)
        # 分级 (良性体量递增) 与两阶段预览
        by_level, cost = {}, {"confirm_execs": 0, "confirmed": 0}
        for j in range(1, NB + 1):
            ev = [e for e in evals if j in e["states"]["D5_mode"]]
            if ev:
                by_level[j] = {
                    "n": len(ev),
                    "D5": sum(e["states"]["D5_mode"][j] for e in ev) / len(ev),
                    "D6": sum(e["states"]["D6_jac"][j] for e in ev) / len(ev),
                    "OR": sum(e["states"]["D5_mode"][j]
                              or e["states"]["D6_jac"][j]
                              for e in ev) / len(ev)}
            else:
                by_level[j] = {"n": 0, "D5": None, "D6": None, "OR": None}
        for e in evals:
            for j in e["ts"]:
                if e["states"]["D5_mode"][j] or e["states"]["D6_jac"][j]:
                    cost["confirm_execs"] += 1   # gold 确认一次, 结果=拒绝
        # per-DB
        per_db = {}
        for db in FAMILY_DBS:
            ev = [e for e in evals if e["db"] == db]
            pts = sum(len(e["ts"]) for e in ev)
            per_db[db] = {
                "n_q": len(ev), "points": pts,
                "D5": round(sum(sum(e["states"]["D5_mode"].values())
                                for e in ev) / pts, 3) if pts else None,
                "D6": round(sum(sum(e["states"]["D6_jac"].values())
                                for e in ev) / pts, 3) if pts else None}
        res["sides"][side] = {"pooled": gm, "by_level": by_level,
                              "per_db": per_db, "two_stage": cost,
                              "skipped_pools": skipped,
                              "reexec": exec_counts, "series": series}
        print(f"[ben:{side}] n_q={gm['n_questions']} "
              f"points={gm['n_stable_ckpts']} skipped={skipped} "
              f"exec={exec_counts['sql']} fail={exec_counts['fail']}",
              flush=True)
        for d in ("D1_ex", "D5_mode", "D6_jac"):
            print(f"  {d}: far={gm['detectors'][d]['far']}", flush=True)

    resdir = os.path.join(HERE, "results")
    os.makedirs(resdir, exist_ok=True)
    with open(os.path.join(resdir, "benign_control.json"), "w",
              encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)

    # ---- 报告 ----
    b = res["build"]
    L = ["# DriftSQL 良性数据演化控制族 (V-benign, E10)\n",
         f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')} "
         "(build_v_benign.py + run_detectors_benign.py, 零 API)\n",
         "## 1. 构建与验证门槛\n",
         "对 V 族 5 库从原始库施加**分布保持**的良性演化 (churn=未引用列成对"
         "值交换, 精确保持列值多重集; dup=复行插入), 累积 b1..b5 体量递增。\n",
         f"**门槛**: {b['gate']} (build_v_benign 验证, "
         "不通过则操作自动收缩重建)。\n",
         "| db | 题数 | churn 列 | 复行表 (引用表幸存) | b5 累计交换对/复行 |",
         "|---|---|---|---|---|"]
    for db, m in b["dbs"].items():
        st = m["b5_rows_changed"]
        L.append(f"| {db} | "
                 f"{manifest['dbs'][db]['n_questions']} | "
                 f"{len(m['churn_cols'])} | "
                 f"{m['dup_tables']} (幸存 {m['dup_survivors']}) | "
                 f"{st['churn_pairs']} / {st['dup_rows']} |")
    L += ["", "> toxicology: 全部列被 gold 引用或属 PK/FK, 复行插入全部"
             "违反约束 → 零良性变化面 (对该工作负载不存在答案保持的数据演化;"
             "其 (q,b) 点为构造性稳定)。superhero: 引用表 colour 的复行幸存"
             "(其 gold 为 DISTINCT/查表型, 对重复行不敏感) — 引用表增长也可"
             "良性, 但需逐库验证。\n",
          "## 2. 筛查信号误报率 (已存候选池重执行; 全部点 stable)\n",
          "D2/D3/D4/D7 为生成侧信号 (logprob/judge/correct), 数据侧良性演化"
          "结构性不可见, 不在检验范围。\n",
          "| 检测器 | chat FAR | v4b FAR |", "|---|---|---|"]
    for d in ("D1_ex", "D5_mode", "D6_jac"):
        cells = [f"{res['sides'][s]['pooled']['detectors'][d]['far']}"
                 for s, _ in SIDES]
        L.append(f"| {DETN[d]} | " + " | ".join(cells) + " |")
    d8 = "0 (150/150 哈希稳定门槛, build_v_benign 实证)"
    L.append(f"| D8 oracle(gold) | {d8} | {d8} |")
    L += ["", "### 分级误报率 (良性体量递增 b1→b5)\n",
          "| 级 | chat D5 | chat D6 | chat OR | v4b D5 | v4b D6 | v4b OR |",
          "|---|---|---|---|---|---|---|"]
    for j in range(1, NB + 1):
        row = []
        for s, _ in SIDES:
            for k in ("D5", "D6", "OR"):
                v = res["sides"][s]["by_level"][j][k]
                row.append("-" if v is None else f"{v:.3f}")
        L.append(f"| b{j} | " + " | ".join(row) + " |")
    L += ["", "### 分库误报率 (chat, 点级)\n",
          "| db | 题数 | 点数 | D5 | D6 |", "|---|---|---|---|---|"]
    for db, m in res["sides"]["chat"]["per_db"].items():
        L.append(f"| {db} | {m['n_q']} | {m['points']} | {m['D5']} | "
                 f"{m['D6']} |")
    L += ["", "## 3. 两阶段确认 (筛查 D5|D6 → 同题 gold 确认)\n",
          "| 侧 | 筛查报警点 | gold 确认调用 | 确认为真 | 确认后 FAR |",
          "|---|---|---|---|---|"]
    for side, _ in SIDES:
        c = res["sides"][side]["two_stage"]
        L.append(f"| {side} | {c['confirm_execs']} | {c['confirm_execs']} | "
                 f"{c['confirmed']} | 0 |")
    L += ["", "## 4. 对照 W3 改写族 (chat 合并 FAR: D5 0.369 / D6 0.617)\n",
          "良性数据演化与良性表述变化同为部署常态; 两类负控制中筛查信号均大量"
          "误报, 而两级架构 (筛查→gold 确认) 在两者上都把 FAR 压到 0 — gold "
          "确认层是唯一的决定性零误报信号, 且其成本 = 筛查报警点数 (上表)。",
          "", "> 解读: V-benign 的误报来自**候选比 gold 更分散** — 候选引用了 "
          "gold 未引用的列/表 (temp=1.0 多样性), 良性变化改变了候选答案分布但 "
          "不改变 gold 答案。分布信号无法区分这一点; 只有 gold 确认可以。",
          ""]
    rp = os.path.join(resdir, "benign_control.md")
    with open(rp, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[ben] report -> {rp}", flush=True)
    print(f"[ben] done in {(time.time()-t0)/60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
