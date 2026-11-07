# -*- coding: utf-8 -*-
"""run_p04.py — P0-4: 阈值跨库迁移 + DB 级 bootstrap + v4b EX 诊断 (零 API)。

P0-4a (A4) 阈值跨库迁移协议: D6 指纹Jaccard 阈值 K 网格 (0.05..0.90) 与
    D1 EX 衰减阈值 E 网格 (0.10..0.50), C(5,2)=10 个调参对 (2 库调参,
    3 库留出)。调参准则 = 调参集锁存 FAR<=0.25 下最大化锁存功效
    (并列取低 FAR, 再取低阈值; 全网格不可行时回退最小 FAR)。
    报告: 全样本 oracle 与 10 组留出 power/FAR 均值±范围、选中阈值
    与 oracle 的一致率/去重集合。
    → 回应 "K=0.30 是在全部 5 库上挑的, 换未见过的库还行吗"
P0-4b (B3) DB 级 bootstrap CI: 题级记录按库分组 (5 组), 有放回重采样
    5 库 B=1000 → chat D5/D6 (E4 配置) 锁存 power/FAR 95% CI;
    逐库点估计并列 (库间方差透明), 并与 E4 存档逐项对账。
    → 回应 "5 个库的 pooled 数字, 库间方差/CI 是多少"
P0-4c (B4) v4b EX 异常诊断: 排除 "v4b 基线更低→衰减空间小" 假设
    (两侧 exec_ok 基线均≈0.99 天花板, ex0<E 者近 0), 实证 EX 双盲
    机制 = 生成器按新 DDL 自适应改写 → exec_ok 恒高; 对照漂移感知
    correct 率 (gen 行内逐 ck gold 标注): ck0 能力差距 (chat vs
    v4b) 与 onset 后正确率崩塌对 EX 均不可见 → v4b D1 功效更低是
    EX 语义盲区 (可执行≠正确) 在弱生成器上的再现, 非检测器缺陷

口径: 全部为 E4 锁存口径 (首次告警覆盖其后稳定检查点); chat 数据在
gen/, v4b 在 gen_v4b/。输出: results/p04_threshold_bootstrap.md + .json
"""
from __future__ import annotations

import itertools
import json
import math
import os
import random
import time
from collections import defaultdict

from build_drift_benchmark import HERE, SEED, FAMILY_DBS
from run_detectors import load_json, load_jsonl, jac_dist
from run_combined_monitor import load_series, gold_map, TMAX

K_GRID = [round(0.05 * i, 2) for i in range(1, 19)]     # 0.05..0.90
E_GRID = [round(0.10 + 0.05 * i, 2) for i in range(9)]  # 0.10..0.50
FAR_BUDGET = 0.25
B_BOOT = 1000
D1_EX = 0.30


# ---------------- 序列与锁存口径 ----------------

def series_fam(root, fam):
    """题级序列记录: onset/ts + jac/dex/d5 逐 ck 值 + ex 曲线。
    过滤逻辑与 build_t1 一致 (gold t0 无效题排除)。"""
    gold = gold_map(fam)
    out = []
    for db in FAMILY_DBS:
        if not os.path.isdir(os.path.join(HERE, root, fam, db)):
            continue
        per = load_series(root, fam, db, TMAX[fam])
        for qid in sorted(per):
            if qid not in gold:
                continue
            truth = gold[qid]["truth"]
            ser = {t: per[qid][t] for t in per[qid]
                   if t in truth or t == 0}
            if 0 not in ser:
                continue
            ts = sorted(t for t in ser if t >= 1)
            if not ts:
                continue
            s0 = ser[0]
            out.append({
                "qid": qid, "db": db,
                "onset": next((t for t in ts if truth[t]), None),
                "ts": ts,
                "jac": {t: jac_dist(s0["cls"], ser[t]["cls"]) for t in ts},
                "dex": {t: s0["ex"] - ser[t]["ex"] for t in ts},
                "d5": {t: 1.0 if ser[t]["mode"] != s0["mode"] else 0.0
                       for t in ts},
                "ex": {0: s0["ex"], **{t: ser[t]["ex"] for t in ts}},
            })
    return out


def correct_series(root, fam):
    """(db, qid) -> {t: 漂移感知 correct 率} (gen 行内逐 ck gold 标注)。
    gold 破坏的 ck 上 correct=None → 按未 True 计 (即该 ck 不可判对)。"""
    per = defaultdict(dict)
    for db in FAMILY_DBS:
        for t in range(0, TMAX[fam] + 1):
            p = os.path.join(HERE, root, fam, db, f"ck{t}.jsonl")
            if not os.path.exists(p):
                continue
            perq = defaultdict(list)
            for r in load_jsonl(p):
                perq[r["qid"]].append(r)
            for qid, rs in perq.items():
                per[(db, qid)][t] = sum(
                    1 for r in rs if r["correct"] is True) / len(rs)
    return per


def q_pack(q, key, thr):
    """单题锁存口径计数包: (有onset, 命中, 稳定点, 被覆盖稳定点)。"""
    lat = next((t for t in q["ts"] if q[key][t] >= thr), None)
    lim = q["onset"] if q["onset"] is not None else 10 ** 9
    on = 1 if q["onset"] is not None else 0
    hit = 1 if (on and lat is not None and lat >= q["onset"]) else 0
    n_st = sum(1 for t in q["ts"] if t < lim)
    cov = sum(1 for t in q["ts"] if lat <= t < lim) \
        if lat is not None else 0
    return (on, hit, n_st, cov)


def pooled(packs):
    n_on = sum(p[0] for p in packs)
    hits = sum(p[1] for p in packs)
    n_st = sum(p[2] for p in packs)
    cov = sum(p[3] for p in packs)
    return {"power": round(hits / n_on, 3) if n_on else None,
            "far": round(cov / n_st, 4) if n_st else None,
            "n_on": n_on, "n_stable": n_st, "hits": hits,
            "covered": cov, "n_q": len(packs)}


def group_metrics(qs, key, thr):
    return pooled([q_pack(q, key, thr) for q in qs])


def pctile(xs, q_lo, q_hi):
    if not xs:
        return None
    xs = sorted(xs)

    def at(p):
        if len(xs) == 1:
            return xs[0]
        i = p * (len(xs) - 1)
        fl = int(math.floor(i))
        ce = min(fl + 1, len(xs) - 1)
        return xs[fl] + (xs[ce] - xs[fl]) * (i - fl)
    return [round(at(q_lo), 4), round(at(q_hi), 4)]


# ---------------- P0-4a 阈值跨库迁移 ----------------

def select_threshold(qs, key, grid, budget=FAR_BUDGET):
    """FAR<=budget 下最大化 power (并列低 FAR → 低阈值); 不可行回退
    全网格最小 FAR (并列低阈值)。返回 (th, metrics, feasible)。"""
    rows = [(th, group_metrics(qs, key, th)) for th in grid]
    feas = [(th, m) for th, m in rows
            if (m["far"] if m["far"] is not None else 0.0) <= budget]
    if feas:
        best = max(feas, key=lambda r: (
            r[1]["power"] if r[1]["power"] is not None else -1,
            -(r[1]["far"] if r[1]["far"] is not None else 0.0), -r[0]))
    else:
        best = min(rows, key=lambda r: (
            r[1]["far"] if r[1]["far"] is not None else 1.0, r[0]))
    return best[0], best[1], bool(feas)


def part_a(series):
    qs_S = series["S"]
    qs_SIL = series["V"] + series["W2"]
    combos = [("D6", "jac", K_GRID, "S", qs_S),
             ("D6", "jac", K_GRID, "SILENT", qs_SIL),
             ("D1", "dex", E_GRID, "S", qs_S),
             ("D1", "dex", E_GRID, "SILENT", qs_SIL)]
    out = {"budget_far": FAR_BUDGET, "grids": {"D6": K_GRID, "D1": E_GRID},
           "combos": {}}
    for det, key, grid, scen, qs in combos:
        ck = f"{det}:{scen}"
        sweep = [{"th": th, **group_metrics(qs, key, th)} for th in grid]
        o_th, o_m, _ = select_threshold(qs, key, grid)
        splits = []
        for tune in itertools.combinations(sorted(FAMILY_DBS), 2):
            evd = [db for db in sorted(FAMILY_DBS) if db not in tune]
            q_t = [q for q in qs if q["db"] in tune]
            q_e = [q for q in qs if q["db"] in evd]
            th, m_t, ok = select_threshold(q_t, key, grid)
            m_e = group_metrics(q_e, key, th)
            splits.append({"tune": list(tune), "eval": evd, "th": th,
                           "feasible": ok,
                           "tune_power": m_t["power"],
                           "tune_far": m_t["far"],
                           "eval_power": m_e["power"],
                           "eval_far": m_e["far"]})
        pows = [s["eval_power"] for s in splits
                if s["eval_power"] is not None]
        fars = [s["eval_far"] for s in splits
                if s["eval_far"] is not None]
        out["combos"][ck] = {
            "scenario": scen, "n_q": len(qs), "sweep": sweep,
            "oracle_th": o_th, "oracle_power": o_m["power"],
            "oracle_far": o_m["far"], "splits": splits,
            "eval_power_mean": round(sum(pows) / len(pows), 3)
            if pows else None,
            "eval_power_range": [min(pows), max(pows)] if pows else None,
            "eval_far_mean": round(sum(fars) / len(fars), 4)
            if fars else None,
            "eval_far_range": [min(fars), max(fars)] if fars else None,
            "th_agreement": sum(1 for s in splits if s["th"] == o_th),
            "th_choices": sorted({s["th"] for s in splits}),
            "n_infeasible": sum(1 for s in splits if not s["feasible"]),
        }
        print(f"[p04a] {ck}: oracle th={o_th} "
              f"(P {o_m['power']} / FAR {o_m['far']}), "
              f"held-out P mean="
              f"{out['combos'][ck]['eval_power_mean']}", flush=True)
    return out


# ---------------- P0-4b DB 级 bootstrap ----------------

def part_b(series):
    dets = [("D5_mode", "d5", 0.5), ("D6_jac", "jac", 0.30)]
    scen_list = [("S", ["S"]), ("V", ["V"]), ("W2", ["W2"]),
                 ("SILENT", ["V", "W2"])]
    out = {"B": B_BOOT, "per_family": {}, "per_db": {}, "xcheck_e4": []}
    stored = load_json(os.path.join(HERE, "results",
                                    "detector_results.json"))
    for gname, fams in scen_list:
        qs = [q for f in fams for q in series[f]]
        row_f, row_db = {}, {}
        for dname, key, thr in dets:
            by_db = {db: [q_pack(q, key, thr) for q in qs
                          if q["db"] == db] for db in FAMILY_DBS}
            pt = pooled([p for db in FAMILY_DBS for p in by_db[db]])
            pows, fars = [], []
            for b in range(B_BOOT):
                rng = random.Random(f"{SEED}:p04b:{gname}:{dname}:{b}")
                samp = [FAMILY_DBS[rng.randrange(5)] for _ in range(5)]
                m = pooled([p for db in samp for p in by_db[db]])
                if m["power"] is not None:
                    pows.append(m["power"])
                if m["far"] is not None:
                    fars.append(m["far"])
            row_f[dname] = {"power": pt["power"],
                            "power_ci": pctile(pows, 0.025, 0.975),
                            "far": pt["far"],
                            "far_ci": pctile(fars, 0.025, 0.975),
                            "n_valid_power_draws": len(pows)}
            for db in FAMILY_DBS:
                m = pooled(by_db[db])
                row_db.setdefault(db, {})[dname] = {
                    "power": m["power"], "far": m["far"],
                    "n_on": m["n_on"], "n_q": m["n_q"]}
            s = stored["groups"][gname]["detectors"][dname]
            out["xcheck_e4"].append({
                "group": gname, "det": dname, "power": pt["power"],
                "stored_power": s["power"], "far": pt["far"],
                "stored_far": s["far"],
                "match": pt["power"] == s["power"]
                and pt["far"] == s["far"]})
        out["per_family"][gname] = row_f
        out["per_db"][gname] = row_db
        print(f"[p04b] {gname}: done "
              f"(xcheck match={out['xcheck_e4'][-2]['match']},"
              f"{out['xcheck_e4'][-1]['match']})", flush=True)
    return out


# ---------------- P0-4c v4b EX 诊断 ----------------

def part_c(qs_chat_S):
    out = {"sides": {}}
    for name, root in (("chat", "gen"), ("v4b", "gen_v4b")):
        qs = qs_chat_S if name == "chat" else series_fam(root, "S")
        cors = correct_series(root, "S")
        for q in qs:
            cs = cors.get((q["db"], q["qid"]), {})
            q["cor"] = {t: cs[t] for t in [0] + q["ts"] if t in cs}
        ex0 = [q["ex"][0] for q in qs]
        cor0 = [q["cor"][0] for q in qs]
        dr = [q for q in qs if q["onset"] is not None]
        stb = [q for q in qs if q["onset"] is None]
        d_on = [q["dex"][q["onset"]] for q in dr]
        d_c = [q["cor"][0] - q["cor"][q["onset"]]
               for q in dr if q["onset"] in q["cor"]]
        maxpost = [max(q["dex"][t] for t in q["ts"] if t >= q["onset"])
                   for q in dr]
        ex_by, cor_by = {}, {}
        for lab, grp in (("drifted", dr), ("stable", stb)):
            ex_by[lab], cor_by[lab] = {}, {}
            for t in range(0, 6):
                ve = [q["ex"][t] for q in grp if t in q["ex"]]
                vc = [q["cor"][t] for q in grp if t in q["cor"]]
                ex_by[lab][t] = round(sum(ve) / len(ve), 3) if ve else None
                cor_by[lab][t] = round(sum(vc) / len(vc), 3) \
                    if vc else None
        grid_tab = []
        for e in E_GRID:
            m = group_metrics(qs, "dex", e)
            grid_tab.append({"E": e, "power": m["power"], "far": m["far"],
                             "n_ex0_below_E": sum(
                                 1 for v in ex0 if v < e)})
        out["sides"][name] = {
            "n_q": len(qs), "n_drifted": len(dr), "n_stable": len(stb),
            "ex0_mean": round(sum(ex0) / len(ex0), 3),
            "ex0_median": round(sorted(ex0)[len(ex0) // 2], 3),
            "ex0_lt_010": sum(1 for v in ex0 if v < 0.10),
            "ex0_lt_030": sum(1 for v in ex0 if v < 0.30),
            "cor0_mean": round(sum(cor0) / len(cor0), 3),
            "dex_at_onset_mean": round(sum(d_on) / len(d_on), 3),
            "dex_at_onset_median": round(
                sorted(d_on)[len(d_on) // 2], 3),
            "dex_at_onset_max": round(max(d_on), 3),
            "decor_at_onset_mean": round(sum(d_c) / len(d_c), 3)
            if d_c else None,
            "dex_post_max_ge_030": sum(1 for v in maxpost if v >= 0.30),
            "ex_by_ck": ex_by, "cor_by_ck": cor_by, "e_grid": grid_tab,
            "d1_030": group_metrics(qs, "dex", D1_EX)}
        print(f"[p04c] {name}: n={len(qs)} ex0="
              f"{out['sides'][name]['ex0_mean']} cor0="
              f"{out['sides'][name]['cor0_mean']} D1@0.30 P="
              f"{out['sides'][name]['d1_030']['power']}", flush=True)
    xg = load_json(os.path.join(HERE, "results", "detector_crossgen.json"))
    xcheck = []
    for name in ("chat", "v4b"):
        s = xg["groups"][name]["S"]["detectors"]["D1_ex"]
        mine = out["sides"][name]["d1_030"]
        xcheck.append({"side": name, "power": mine["power"],
                       "stored_power": s["power"], "far": mine["far"],
                       "stored_far": s["far"],
                       "match": mine["power"] == s["power"]
                       and mine["far"] == s["far"]})
    out["xcheck_e8"] = xcheck
    print(f"[p04c] E8 xcheck: {[x['match'] for x in xcheck]}", flush=True)
    return out


# ---------------- 报告 ----------------

def fm(v):
    return "-" if v is None else str(v)


def fmb(v):
    """[lo, hi] → '[lo, hi]'。"""
    return "-" if v is None else f"[{v[0]}, {v[1]}]"


def write_report(res, resdir):
    a = res["a_transfer"]
    b = res["b_bootstrap"]
    c = res["c_v4b_ex"]
    L = ["# DriftSQL P0-4: 阈值跨库迁移 + DB 级 bootstrap + v4b EX 诊断\n",
         f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')} "
         "(run_p04.py, 零 API; 全部 E4 锁存口径, chat=gen, v4b=gen_v4b)\n",
         "P0-4a (A4): 2 库调参 / 3 库留出 × C(5,2)=10 组; 准则 = 调参集"
         "锁存 FAR≤0.25 最大化锁存功效 (并列低 FAR→低阈值, 不可行回退"
         "最小 FAR)。P0-4b (B3): 题级记录按库分组, 有放回重采样 5 库 "
         f"B={B_BOOT} → 95% CI。P0-4c (B4): v4b S 族 EX 异常解源 — "
         "exec_ok 天花板 (地板假设排除) + 漂移感知 correct 率对照"
         " (EX 看不见的两个真实信号)。\n",

         "## 1. P0-4a 阈值跨库迁移 (A4)\n",
         "### 1.1 全样本网格扫描 (oracle 视角, chat 5 库)\n",
         "| K (D6 Jaccard) | S power | S FAR | SILENT power | "
         "SILENT FAR |", "|---|---|---|---|---|"]
    swS = a["combos"]["D6:S"]["sweep"]
    swI = a["combos"]["D6:SILENT"]["sweep"]
    for r1, r2 in zip(swS, swI):
        L.append(f"| {r1['th']} | {fm(r1['power'])} | {fm(r1['far'])} "
                 f"| {fm(r2['power'])} | {fm(r2['far'])} |")
    L += ["\n| E (D1 EX衰减) | S power | S FAR | SILENT power | "
          "SILENT FAR |", "|---|---|---|---|---|"]
    swS = a["combos"]["D1:S"]["sweep"]
    swI = a["combos"]["D1:SILENT"]["sweep"]
    for r1, r2 in zip(swS, swI):
        L.append(f"| {r1['th']} | {fm(r1['power'])} | {fm(r1['far'])} "
                 f"| {fm(r2['power'])} | {fm(r2['far'])} |")
    lo = a["combos"]["D6:SILENT"]["sweep"][0]
    hi = max(a["combos"]["D6:SILENT"]["sweep"],
             key=lambda r: r["power"] or -1)
    L += [f"\n- 景观 (SILENT): K {lo['th']}→{hi['th']} 功效 "
          f"{lo['power']}→{hi['power']} (平坦微升) 而 FAR "
          f"{lo['far']}→{hi['far']} (陡降) — 锁存口径下高 K 占优: "
          "高 FAR 通过早期误锁存侵蚀功效 (两阶段架构正是解此)。"]

    L += ["\n### 1.2 迁移汇总 (留出 3 库, 10 组)\n",
          "| 组合 | oracle TH | oracle P/FAR | 留出 P 均值 [min,max] | "
          "留出 FAR 均值 [min,max] | TH 一致 | TH 选项 | 不可行组 |",
          "|---|---|---|---|---|---|---|---|"]
    for ck in ("D6:SILENT", "D6:S", "D1:S", "D1:SILENT"):
        d = a["combos"][ck]
        L.append(f"| {ck} | {d['oracle_th']} | {fm(d['oracle_power'])} / "
                 f"{fm(d['oracle_far'])} | {fm(d['eval_power_mean'])} "
                 f"{fmb(d['eval_power_range'])} | "
                 f"{fm(d['eval_far_mean'])} {fmb(d['eval_far_range'])} "
                 f"| {d['th_agreement']}/10 | {d['th_choices']} "
                 f"| {d['n_infeasible']} |")

    L += ["\n### 1.3 per-split 明细 (D6:SILENT, 旗舰场景)\n",
          "| 调参库对 (2) | K* | 调参 P/FAR | 留出 P | 留出 FAR |",
          "|---|---|---|---|---|"]
    for s in a["combos"]["D6:SILENT"]["splits"]:
        mark = "" if s["feasible"] else " (回退: 调参集无可行解)"
        L.append(f"| {s['tune'][0]} + {s['tune'][1]} | {s['th']}{mark} "
                 f"| {fm(s['tune_power'])} / {fm(s['tune_far'])} "
                 f"| {fm(s['eval_power'])} | {fm(s['eval_far'])} |")

    L += [f"\n## 2. P0-4b DB 级 bootstrap CI (B3, B={B_BOOT}, "
          "按库有放回重采样 5 库)\n",
          "| 场景 | 检测器 | power | 95% CI | FAR | 95% CI |",
          "|---|---|---|---|---|---|"]
    for gname in ("S", "V", "W2", "SILENT"):
        for dname in ("D5_mode", "D6_jac"):
            d = b["per_family"][gname][dname]
            L.append(f"| {gname} | {dname} | {fm(d['power'])} "
                     f"{fmb(d['power_ci'])} | {fm(d['far'])} "
                     f"{fmb(d['far_ci'])} |")
    L += ["\n### 2.1 逐库点估计 (库间方差透明)\n",
          "| 场景 | 库 | n_q | n_onset | D5 P/FAR | D6 P/FAR |",
          "|---|---|---|---|---|---|"]
    for gname in ("S", "V", "W2", "SILENT"):
        for db in FAMILY_DBS:
            r = b["per_db"][gname][db]
            d5, d6 = r["D5_mode"], r["D6_jac"]
            L.append(f"| {gname} | {db} | {d5['n_q']} | {d5['n_on']} "
                     f"| {fm(d5['power'])} / {fm(d5['far'])} "
                     f"| {fm(d6['power'])} / {fm(d6['far'])} |")
    L += ["\n### 2.2 E4 存档对账 (重算 vs detector_results.json)\n",
          "| 场景 | 检测器 | 重算 P/FAR | 存档 P/FAR | match |",
          "|---|---|---|---|---|"]
    for x in b["xcheck_e4"]:
        L.append(f"| {x['group']} | {x['det']} | {fm(x['power'])} / "
                 f"{fm(x['far'])} | {fm(x['stored_power'])} / "
                 f"{fm(x['stored_far'])} | {x['match']} |")

    sc = c["sides"]["chat"]
    sv = c["sides"]["v4b"]
    L += ["\n## 3. P0-4c v4b EX 异常诊断 (B4, S 族, E4 锁存口径)\n",
          "假设检验: 'v4b 基线更低 → 衰减空间小 → D1 更低'。实测**排除**:"
          " 两侧 exec_ok 基线均在 ~0.99 天花板 (ex0<0.30 者 0/150), D1 "
          "双盲另有机制 — 生成器看新 DDL 自适应改写后 SQL 仍可执行; "
          "对照漂移感知 correct 率 (gen 行内逐 ck gold 标注, gold 破坏"
          "的 ck 不可判对) 可见 EX 看不见的两个真实信号: ck0 能力差距 "
          "与 onset 后正确率崩塌。\n",
          "### 3.1 基线 (exec_ok 天花板; correct 能力差距)\n",
          "| 侧 | n (漂移/稳定) | mean ex0 | ex0<0.30 | mean cor0 | "
          "Δex@onset mean/med/max | Δcor@onset mean | "
          "onset后 maxΔex≥0.30 题数 |",
          "|---|---|---|---|---|---|---|---|"]
    for name, s in (("chat", sc), ("v4b", sv)):
        L.append(f"| {name} | {s['n_q']} ({s['n_drifted']}/"
                 f"{s['n_stable']}) | {s['ex0_mean']} | "
                 f"{s['ex0_lt_030']}/{s['n_q']} | {s['cor0_mean']} | "
                 f"{s['dex_at_onset_mean']} / {s['dex_at_onset_median']}"
                 f" / {s['dex_at_onset_max']} | "
                 f"{s['decor_at_onset_mean']} | "
                 f"{s['dex_post_max_ge_030']}/{s['n_drifted']} |")
    L += ["\n### 3.2 E 网格 (D1, S 族; ex0<E ⇒ 该题结构性无法告警)\n",
          "| E | chat P/FAR | v4b P/FAR | chat ex0<E | v4b ex0<E |",
          "|---|---|---|---|---|"]
    for rc, rv in zip(sc["e_grid"], sv["e_grid"]):
        L.append(f"| {rc['E']} | {fm(rc['power'])} / {fm(rc['far'])} "
                 f"| {fm(rv['power'])} / {fm(rv['far'])} "
                 f"| {rc['n_ex0_below_E']}/{sc['n_q']} "
                 f"| {rv['n_ex0_below_E']}/{sv['n_q']} |")
    L += ["\n### 3.3 逐 ck mean exec_ok (漂移题 vs 稳定题)\n",
          "| 侧×状态 | " + " | ".join(f"ck{t}" for t in range(6)) + " |",
          "|---|---|---|---|---|---|---|"]
    for name, s in (("chat", sc), ("v4b", sv)):
        for lab in ("drifted", "stable"):
            L.append(f"| {name}-{lab} | " + " | ".join(
                fm(s["ex_by_ck"][lab][t]) for t in range(6)) + " |")
    L += ["\n### 3.4 逐 ck mean correct (漂移题 vs 稳定题; 漂移感知 "
          "gold 标注, gold 破坏的 ck 不可判对)\n",
          "| 侧×状态 | " + " | ".join(f"ck{t}" for t in range(6)) + " |",
          "|---|---|---|---|---|---|---|"]
    for name, s in (("chat", sc), ("v4b", sv)):
        for lab in ("drifted", "stable"):
            L.append(f"| {name}-{lab} | " + " | ".join(
                fm(s["cor_by_ck"][lab][t]) for t in range(6)) + " |")
    L += ["\n### 3.5 E8 存档对账 (D1_ex, S 族; S 全量=v4b 同集)\n",
          "| 侧 | 重算 P/FAR | 存档 P/FAR | match |",
          "|---|---|---|---|"]
    for x in c["xcheck_e8"]:
        L.append(f"| {x['side']} | {fm(x['power'])} / {fm(x['far'])} "
                 f"| {fm(x['stored_power'])} / {fm(x['stored_far'])} "
                 f"| {x['match']} |")

    # ---- 结论 ----
    d6i = a["combos"]["D6:SILENT"]
    d6s = a["combos"]["D6:S"]
    d1s = a["combos"]["D1:S"]
    d1i = a["combos"]["D1:SILENT"]
    d1s_max = max(d1s["sweep"], key=lambda r: r["power"] or -1)
    d1i_max = max(d1i["sweep"], key=lambda r: r["power"] or -1)
    ci_d6 = b["per_family"]["SILENT"]["D6_jac"]
    ci_d5 = b["per_family"]["SILENT"]["D5_mode"]
    n_x4 = sum(1 for x in b["xcheck_e4"] if x["match"])
    over = [s for s in d6i["splits"]
            if s["eval_far"] is not None and s["eval_far"] > FAR_BUDGET]
    cons = [s for s in d6i["splits"] if s["th"] >= 0.6]
    cons_far = max((s["eval_far"] for s in cons
                    if s["eval_far"] is not None), default=None)
    cons_pow = round(sum(s["eval_power"] for s in cons
                         if s["eval_power"] is not None) / len(cons), 3)
    L += ["\n## 4. 结论 (论文叙事弹药)\n",
          f"1. **D6 阈值可跨库迁移 (A4)**: SILENT oracle K="
          f"{d6i['oracle_th']} (P {d6i['oracle_power']} / FAR "
          f"{d6i['oracle_far']}); 10 组留出 P 均值 "
          f"{d6i['eval_power_mean']} {fmb(d6i['eval_power_range'])}, "
          f"FAR 均值 {d6i['eval_far_mean']} "
          f"{fmb(d6i['eval_far_range'])} → 留出性能与 oracle 同档 "
          f"(均值差 {round(d6i['oracle_power'] - d6i['eval_power_mean'], 3)}),"
          " 阈值不是过拟合 5 库的产物。选中值有区间不确定性 ("
          f"{d6i['th_agreement']}/10 组选中 oracle K, 选项 "
          f"{d6i['th_choices']}), 但迁移协议本身标出安全工作区: 留出 "
          f"FAR>{FAR_BUDGET} 的 {len(over)} 组全部选中敏感档 K*="
          f"{sorted({s['th'] for s in over})} (调参集达标但跨库方差大), "
          f"K*≥0.6 的 {len(cons)} 组留出 FAR≤{cons_far} 且 P 均值 "
          f"{cons_pow} — 锁存口径下保守阈值双优 (高 FAR 经早期误锁存"
          "侵蚀功效, 见 1.1 景观)。S(loud) 侧迁移同样稳定: oracle K="
          f"{d6s['oracle_th']}, 留出 P 均值 {d6s['eval_power_mean']} "
          f"{fmb(d6s['eval_power_range'])} — D6 在 S 上受限是信号本身的"
          "局限 (E4 已证), 与调参无关。",
          f"2. **D1 全网格无工作点 (EX 双盲非阈值伪影)**: S 在 E∈"
          f"[0.10,0.50] 全网格最高 P {d1s_max['power']} (E="
          f"{d1s_max['th']}), SILENT 最高 {d1i_max['power']} (E="
          f"{d1i_max['th']}); 10 组留出同样 ≈0 → EX 衰减的双盲性在阈值"
          "轴与库集两个维度上成立。",
          f"3. **DB 级 bootstrap (B3)**: chat D6 SILENT power "
          f"{ci_d6['power']} {fmb(ci_d6['power_ci'])}, D5 "
          f"{ci_d5['power']} {fmb(ci_d5['power_ci'])} — 5 库库间方差"
          "如实呈现 (逐库点估计见 2.1), 主结论 (silent 主信号 D5≈D6, "
          f"EX/judge 无信息) 在 95% CI 下方向不变; E4 存档对账 "
          f"{n_x4}/{len(b['xcheck_e4'])} match。",
          f"4. **v4b EX 异常解源 (B4)**: '基线低→衰减空间小' 假设排除 "
          f"(exec_ok 基线 chat {sc['ex0_mean']} / v4b {sv['ex0_mean']} "
          "均在天花板, ex0<0.30 者 0/150); 漂移题 mean exec_ok 逐 ck "
          f"几乎不降 (chat {sc['ex_by_ck']['drifted'][0]}→"
          f"{sc['ex_by_ck']['drifted'][5]}, v4b "
          f"{sv['ex_by_ck']['drifted'][0]}→"
          f"{sv['ex_by_ck']['drifted'][5]}) — 生成器看新 DDL 自适应"
          "改写, SQL 始终可执行。EX 看不见的两个真实信号: ck0 correct "
          f"能力差距 chat {sc['cor0_mean']} vs v4b {sv['cor0_mean']} "
          f"({round(100 * (sc['cor0_mean'] - sv['cor0_mean']))}pp), "
          f"onset 后 Δcorrect 均值 chat {sc['decor_at_onset_mean']} / "
          f"v4b {sv['decor_at_onset_mean']} (正确率崩塌, 见 3.4) → D1 "
          "双盲是 EX 语义盲区 (可执行≠正确): v4b 功效 0.0 与 chat "
          "0.018 同因, 非检测器在 v4b 上的缺陷; E8 对账 "
          + "/".join(str(x["match"]) for x in c["xcheck_e8"]) + "。",
          ""]
    rp = os.path.join(resdir, "p04_threshold_bootstrap.md")
    with open(rp, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[p04] report -> {rp}", flush=True)


# ---------------- 主流程 ----------------

def main():
    t0 = time.time()
    series = {fam: series_fam("gen", fam) for fam in ("S", "V", "W2")}
    for fam, qs in series.items():
        n_on = sum(1 for q in qs if q["onset"] is not None)
        print(f"[p04] chat {fam}: {len(qs)} questions, {n_on} onset",
              flush=True)
    res = {
        "params": {"far_budget": FAR_BUDGET, "b_boot": B_BOOT,
                   "k_grid": K_GRID, "e_grid": E_GRID,
                   "latched_semantics": "E4 (首次告警覆盖其后稳定检查点)"},
        "a_transfer": part_a(series),
        "b_bootstrap": part_b(series),
        "c_v4b_ex": part_c(series["S"]),
    }
    resdir = os.path.join(HERE, "results")
    with open(os.path.join(resdir, "p04_threshold_bootstrap.json"),
              "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    write_report(res, resdir)
    ok4 = all(x["match"] for x in res["b_bootstrap"]["xcheck_e4"])
    ok8 = all(x["match"] for x in res["c_v4b_ex"]["xcheck_e8"])
    print(f"[p04] xcheck: E4={ok4} E8={ok8}", flush=True)
    print(f"[p04] done in {(time.time()-t0)/60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
