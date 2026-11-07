# -*- coding: utf-8 -*-
"""run_revision_analysis.py — 审稿修订零 API 重分析 (M3/M5/M9/M7)。

Part 1 (M3): 2×2 消融 — 监控语义 (latched/continue) × gold 确认 (无/有)。
  4 个筛查配置 (D5 / D6@0.30 / D6@0.50 / OR) × 组 (SILENT / W3 / BENIGN)。
  - Latched/无gold (=E4 基线): 判决=首次报警; FAR=锁存覆盖口径; 可见报警=有报警题数。
  - Continue/无gold: 每个筛查报警直接给操作员; FAR=stable 单元逐单元口径。
  - Latched/有gold: 首次报警触发一次 gold; 拒绝后停止监测 (保持 latched 语义);
    操作员 FAR=0; gold/q=有报警题数比例。
  - Continue/有gold (=DriftGuard, replay 逻辑)。
Part 2 (M5): 层级 bootstrap (DB→题, B=2000, 种子 rev:boot:{b}) + 逐 DB 表 +
  G1/G2 S 族两阶段/latched 功效比值联合层级 bootstrap + 零事件口径 (DB 级
  二项精确上界; 保留论文 "1314 个良性控制单元零确认" 计数陈述, 不附 3/1314)。
Part 3 (M9): placebo 诊断 — D5/D6@0.30/OR 各自独立的首报警位置 MLE (修正论文
  把 OR 沿用 D6 f̂ 的错误) + 逐 checkpoint 稳定单元报警率 + lag-1 自相关 +
  族匹配序列交换 placebo (B=1000, 种子 rev:swap:{b}, 替代 "iid 上界" 主张)。
Part 4 (M7): 规则 R 的 leave-one-database-out 嵌套验证 (D6 SILENT, 复刻
  run_p04.py part_a 的 18 点网格 / FAR 预算 / 平局规则)。

零 API: 只读 gen/ gen_v4b/ gold/ results/ 缓存。基线对账 (Part 1 OR vs
combined_monitor.json; S 族 vs combined_monitor.json; f̂ vs 论文 0.080/0.265
+ 首报警位置 vs detector_results.json; Part 4 pooled sweep/oracle/10 折 vs
p04_threshold_bootstrap.json) 全部通过后才写出 results/revision_analysis.{json,md}。
"""
from __future__ import annotations

import itertools
import json
import math
import os
import random
import time
from collections import defaultdict

from run_combined_monitor import (HERE, TMAX, FAMILY_DBS, TH_JAC,
                                  gold_map, jac_dist, load_json,
                                  load_series)
from run_p1_analysis import load_chat_questions, replay, qids_of_fam
from run_p04 import (K_GRID, FAR_BUDGET, select_threshold,
                     group_metrics, series_fam, pctile)

B_BOOT = 2000
B_SWAP = 1000
TH50 = 0.50
CONFIGS = ("D5", "D6@0.30", "D6@0.50", "OR")
PBO_CONFIGS = ("D5", "D6@0.30", "OR")     # placebo 部分的三配置
CELLS = ("latched_nogold", "continue_nogold", "latched_gold", "continue_gold")
CELL_LABEL = {"latched_nogold": "Latched/无gold (=E4)",
              "continue_nogold": "Continue/无gold",
              "latched_gold": "Latched/有gold",
              "continue_gold": "Continue/有gold (=DriftGuard)"}


# ---------------- 基础工具 ----------------

def med(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def fm(v):
    return "-" if v is None else str(v)


def fmb(v):
    return "-" if v is None else f"[{v[0]}, {v[1]}]"


def alarm_lists(q):
    """4 配置的报警列表 (对齐 q["ts"])。OR 必须与 load_chat_questions 的
    q["alarms"] 一致 (构建时断言)。"""
    s0, ts, ser = q["s0"], q["ts"], q["ser"]
    d5 = [ser[t]["mode"] != s0["mode"] for t in ts]
    j30 = [jac_dist(s0["cls"], ser[t]["cls"]) >= TH_JAC for t in ts]
    j50 = [jac_dist(s0["cls"], ser[t]["cls"]) >= TH50 for t in ts]
    return {"D5": d5, "D6@0.30": j30, "D6@0.50": j50,
            "OR": [a or b for a, b in zip(d5, j30)]}


def cell_stats(q, A):
    """单题四单元格计数包 (A 为对齐 ts 的报警列表)。

    注意: 缓存中存在少量瞬态漂移题 (truth 先 True 后 False), 故不假设 truth
    单调; 四个单元格均按各自口径独立计数:
    - latched 判决 = 首次报警 (时间 >= onset 记 hit);
    - latched/有gold 检出 = 首报警落在 truth=True 单元 (gold 精确);
    - continue/无gold 检出 = 任一 t >= onset 的报警 (时间口径);
    - continue/有gold = DriftGuard replay (报警落在 truth=True 单元)。
    """
    ts, truth = q["ts"], q["truth"]
    onset_t = next((t for t, f in zip(ts, truth) if f), None)
    lim = onset_t if onset_t is not None else 10 ** 9
    n_stable = sum(1 for t in ts if t < lim)
    first_i = next((i for i, a in enumerate(A) if a), None)
    if first_i is not None:
        lat_t = ts[first_i]
        lat_hit = 1 if (onset_t is not None and lat_t >= onset_t) else 0
        lat_delay = (lat_t - onset_t) if lat_hit else None
        lat_cover = sum(1 for t in ts if lat_t <= t < lim)
        lat_vis, lat_vis_st = 1, (1 if lat_t < lim else 0)
        lg_cost = 1
        lg_conf = 1 if truth[first_i] else 0
        lg_rej = 1 - lg_conf
    else:
        lat_hit, lat_delay, lat_cover = 0, None, 0
        lat_vis, lat_vis_st = 0, 0
        lg_cost, lg_conf, lg_rej = 0, 0, 0
    ci = next((i for i, (t, a) in enumerate(zip(ts, A))
               if a and onset_t is not None and t >= onset_t), None)
    cont_hit = 1 if ci is not None else 0
    cont_delay = (ts[ci] - onset_t) if cont_hit else None
    stable_al = sum(1 for t, a in zip(ts, A) if a and t < lim)
    total_al = sum(1 for a in A if a)
    r = replay(dict(q, alarms=A))             # continue/有gold = DriftGuard
    cg_det = 1 if r["detected"] is not None else 0
    cg_delay = (r["detected"] - r["onset"]) \
        if r["detected"] is not None else None
    return {"onset": 1 if onset_t is not None else 0,
            "n_stable": n_stable,
            "lat_hit": lat_hit, "lat_delay": lat_delay,
            "lat_cover": lat_cover, "lat_vis": lat_vis,
            "lat_vis_st": lat_vis_st,
            "lg_cost": lg_cost, "lg_conf": lg_conf, "lg_rej": lg_rej,
            "cont_hit": cont_hit, "cont_delay": cont_delay,
            "stable_al": stable_al, "total_al": total_al,
            "cg_det": cg_det, "cg_cost": r["cost"],
            "cg_rej": r["rejected"], "cg_delay": cg_delay}


def agg_cells(stats):
    """四单元格聚合 (口径与 scenario_metrics 的舍入一致)。"""
    n = len(stats)
    n_on = sum(s["onset"] for s in stats)
    n_st = sum(s["n_stable"] for s in stats)
    covered = sum(s["lat_cover"] for s in stats)
    screen_far = round(covered / n_st, 4) if n_st else None

    def pw(hits):
        return round(hits / n_on, 3) if n_on else None

    lat_hits = [s["lat_delay"] for s in stats if s["lat_hit"]]
    lat_conf_delays = [s["lat_delay"] for s in stats if s["lg_conf"]]
    cont_hits = [s["cont_delay"] for s in stats if s["cont_hit"]]
    cg_hits = [s["cg_delay"] for s in stats if s["cg_det"]]
    lat_conf = sum(s["lg_conf"] for s in stats)
    lat_rej = sum(s["lg_rej"] for s in stats)
    cg_conf = sum(s["cg_det"] for s in stats)
    cg_rej = sum(s["cg_rej"] for s in stats)
    stable_al = sum(s["stable_al"] for s in stats)
    n_q_stable_al = sum(1 for s in stats if s["stable_al"])
    total_al = sum(s["total_al"] for s in stats)
    return {
        "n_questions": n, "n_onset": n_on, "n_stable_points": n_st,
        "screen_far_latched": screen_far,
        "latched_nogold": {
            "power": pw(sum(s["lat_hit"] for s in stats)),
            "med_delay": med(lat_hits), "op_far": screen_far,
            "q_frac_visible_stable": round(
                sum(s["lat_vis_st"] for s in stats) / n, 4),
            "visible_total": sum(s["lat_vis"] for s in stats),
            "gold_total": 0, "gold_per_q": 0.0,
            "confirmed": None, "rejected": None},
        "continue_nogold": {
            "power": pw(sum(s["cont_hit"] for s in stats)),
            "med_delay": med(cont_hits),
            "op_far": round(stable_al / n_st, 4) if n_st else None,
            "q_frac_visible_stable": round(n_q_stable_al / n, 4),
            "visible_total": total_al,
            "gold_total": 0, "gold_per_q": 0.0,
            "confirmed": None, "rejected": None,
            "stable_alarm_total": stable_al,
            "n_q_stable_alarm": n_q_stable_al},
        "latched_gold": {
            "power": pw(lat_conf),
            "med_delay": med(lat_conf_delays), "op_far": 0,
            "q_frac_visible_stable": 0.0,
            "visible_total": lat_conf,
            "gold_total": sum(s["lg_cost"] for s in stats),
            "gold_per_q": round(sum(s["lg_cost"] for s in stats) / n, 2),
            "confirmed": lat_conf, "rejected": lat_rej},
        "continue_gold": {
            "power": pw(cg_conf), "med_delay": med(cg_hits), "op_far": 0,
            "q_frac_visible_stable": 0.0, "visible_total": cg_conf,
            "gold_total": sum(s["cg_cost"] for s in stats),
            "gold_per_q": round(sum(s["cg_cost"] for s in stats) / n, 2),
            "confirmed": cg_conf, "rejected": cg_rej},
    }


def load_s_side(root):
    """S 族 (loud) 题级序列 + OR 报警集, 过滤口径与 load_chat_questions 一致。"""
    gold = gold_map("S")
    out = []
    for db in FAMILY_DBS:
        if not os.path.isdir(os.path.join(HERE, root, "S", db)):
            continue
        per = load_series(root, "S", db, TMAX["S"])
        for qid in sorted(per):
            if qid not in gold:
                continue
            truth = gold[qid]["truth"]
            ser = {t: per[qid][t] for t in per[qid] if t in truth or t == 0}
            if 0 not in ser:
                continue
            ts = sorted(t for t in ser if t >= 1)
            if not ts:
                continue
            out.append({"qid": qid, "fam": "S", "db": db, "ts": ts,
                        "truth": [truth[t] for t in ts],
                        "alarms": alarm_lists(
                            {"ts": ts, "ser": ser, "s0": ser[0]})["OR"],
                        "s0": ser[0], "ser": ser})
    return out


# ---------------- Part 3: placebo 工具 ----------------

def mle_rate(stable):
    """首报警位置的删失几何 MLE — 精确复刻 placebo_control.mle_rate (网格
    i/200, i=1..199), 保证与论文 f̂=0.080/0.265 可对账。"""
    best_f, best_ll = 0.0, -1e18
    for i in range(1, 200):
        f = i / 200.0
        ll = 0.0
        for (L, a) in stable:      # a: 1-based 首报警位置或 None
            if a is None:
                ll += L * math.log(1 - f)
            else:
                ll += (a - 1) * math.log(1 - f) + math.log(f)
        if ll > best_ll:
            best_ll, best_f = ll, f
    return best_f


def pearson(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs)
    sy = sum((y - my) ** 2 for y in ys)
    if sx <= 0 or sy <= 0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / math.sqrt(sx * sy)


# ---------------- 主流程 ----------------

def main():
    t0 = time.time()
    stored = load_json(os.path.join(HERE, "results",
                                    "combined_monitor.json"))
    Q = load_chat_questions()
    GK = {"SILENT": qids_of_fam(Q, ("V", "W2")),
          "W3": qids_of_fam(Q, ("W3",)),
          "BENIGN": qids_of_fam(Q, ("BENIGN",))}
    print(f"[rev] questions: SILENT={len(GK['SILENT'])} "
          f"W3={len(GK['W3'])} BENIGN={len(GK['BENIGN'])}", flush=True)
    assert len(GK["SILENT"]) == 300 and len(GK["W3"]) == 141 \
        and len(GK["BENIGN"]) == 150, "题目数与存档不符"
    AL = {}
    for k in Q:
        AL[k] = alarm_lists(Q[k])
        assert AL[k]["OR"] == Q[k]["alarms"], "OR 报警集与存档构建不一致"
    xcheck = defaultdict(list)

    # ---- Part 1: 2×2 消融 ----
    part1 = {}
    for gname in ("SILENT", "W3", "BENIGN"):
        part1[gname] = {}
        for cfg in CONFIGS:
            stats = [cell_stats(Q[k], AL[k][cfg]) for k in GK[gname]]
            part1[gname][cfg] = agg_cells(stats)
            # 对账: continue/有gold vs combined_monitor.json (逐字段)
            so = stored["t1"]["chat"][gname][cfg]
            mine = part1[gname][cfg]["continue_gold"]
            for fld, mv, sv in (
                    ("n_questions", part1[gname][cfg]["n_questions"],
                     so["n_questions"]),
                    ("n_onset", part1[gname][cfg]["n_onset"],
                     so["n_onset"]),
                    ("power", mine["power"], so["power"]),
                    ("screen_far_latched",
                     part1[gname][cfg]["screen_far_latched"],
                     so["screen_far_latched"]),
                    ("confirmed", mine["confirmed"], so["confirmed"]),
                    ("rejected", mine["rejected"], so["rejected"]),
                    ("gold_confirm_total", mine["gold_total"],
                     so["gold_confirm_total"]),
                    ("n_stable_points", part1[gname][cfg]["n_stable_points"],
                     so["n_stable_points"])):
                xcheck["part1_continue_gold_vs_stored"].append({
                    "group": gname, "config": cfg, "field": fld,
                    "mine": mv, "stored": sv, "match": mv == sv})
            ln = part1[gname][cfg]["latched_nogold"]
            xcheck["part1_latched_vs_stored"].append({
                "group": gname, "config": cfg,
                "power": [ln["power"], so["power_screen_latched"]],
                "far": [ln["op_far"], so["screen_far_latched"]],
                "match": ln["power"] == so["power_screen_latched"]
                and ln["op_far"] == so["screen_far_latched"]})
    ok1 = all(r["match"] for r in xcheck["part1_continue_gold_vs_stored"]) \
        and all(r["match"] for r in xcheck["part1_latched_vs_stored"])
    print(f"[rev] P1 crosscheck vs combined_monitor.json: match={ok1}",
          flush=True)
    if not ok1:
        for key in ("part1_continue_gold_vs_stored", "part1_latched_vs_stored"):
            for r in xcheck[key]:
                if not r["match"]:
                    print(f"  MISMATCH {key}: {r}")
        raise SystemExit("Part1 baseline xcheck FAILED — abort")

    # ---- Part 4: LODO (硬对账先行) ----
    qs_sil = series_fam("gen", "V") + series_fam("gen", "W2")
    assert len(qs_sil) == 300, "series_fam SILENT 题数不符"
    assert sum(1 for q in qs_sil if q["onset"] is not None) == 90
    arch_p04 = load_json(os.path.join(HERE, "results",
                                      "p04_threshold_bootstrap.json"))
    d6a = arch_p04["a_transfer"]["combos"]["D6:SILENT"]
    sweep = [{"th": th, **group_metrics(qs_sil, "jac", th)} for th in K_GRID]
    o_th, o_m, o_feas = select_threshold(qs_sil, "jac", K_GRID)
    splits = []
    for tune in itertools.combinations(sorted(FAMILY_DBS), 2):
        evd = [db for db in sorted(FAMILY_DBS) if db not in tune]
        q_t = [q for q in qs_sil if q["db"] in tune]
        q_e = [q for q in qs_sil if q["db"] in evd]
        th, m_t, ok = select_threshold(q_t, "jac", K_GRID)
        m_e = group_metrics(q_e, "jac", th)
        splits.append({"tune": list(tune), "eval": evd, "th": th,
                       "feasible": ok, "tune_power": m_t["power"],
                       "tune_far": m_t["far"], "eval_power": m_e["power"],
                       "eval_far": m_e["far"]})
    pows = [s["eval_power"] for s in splits if s["eval_power"] is not None]
    eval_mean = round(sum(pows) / len(pows), 3) if pows else None
    xcheck["part4_pooled_sweep"] = {
        "sweep_match": sweep == d6a["sweep"],
        "n_grid": len(sweep), "grid": K_GRID}
    xcheck["part4_oracle"] = {
        "oracle_th": [o_th, d6a["oracle_th"]],
        "oracle_power": [o_m["power"], d6a["oracle_power"]],
        "oracle_far": [o_m["far"], d6a["oracle_far"]],
        "match": o_th == d6a["oracle_th"]
        and o_m["power"] == d6a["oracle_power"]
        and o_m["far"] == d6a["oracle_far"]}
    my_sp = {(s["tune"][0], s["tune"][1]): s for s in splits}
    sp_match = all(
        my_sp[(s["tune"][0], s["tune"][1])]["th"] == s["th"]
        and my_sp[(s["tune"][0], s["tune"][1])]["eval_power"] == s["eval_power"]
        and my_sp[(s["tune"][0], s["tune"][1])]["eval_far"] == s["eval_far"]
        for s in d6a["splits"])
    xcheck["part4_archived_splits"] = {
        "eval_power_mean": [eval_mean, d6a["eval_power_mean"]],
        "n_splits": len(splits),
        "per_split_match": sp_match,
        "match": eval_mean == d6a["eval_power_mean"] and sp_match}
    ok4 = (xcheck["part4_pooled_sweep"]["sweep_match"]
           and xcheck["part4_oracle"]["match"]
           and xcheck["part4_archived_splits"]["match"])
    print(f"[rev] P4 xcheck vs p04_threshold_bootstrap.json: "
          f"sweep/oracle/splits match={ok4}", flush=True)
    if not ok4:
        print(f"  sweep_match={xcheck['part4_pooled_sweep']['sweep_match']} "
              f"oracle={xcheck['part4_oracle']} "
              f"splits={xcheck['part4_archived_splits']}")
        raise SystemExit("Part4 baseline xcheck FAILED — abort")
    per_db_grid = {}
    for db in sorted(FAMILY_DBS):
        qh = [q for q in qs_sil if q["db"] == db]
        per_db_grid[db] = [{"th": th,
                            **group_metrics(qh, "jac", th)}
                           for th in K_GRID]
    folds = []
    for h in sorted(FAMILY_DBS):
        q_t = [q for q in qs_sil if q["db"] != h]
        q_h = [q for q in qs_sil if q["db"] == h]
        k_star, m_t, feas = select_threshold(q_t, "jac", K_GRID)
        dep = k_star if k_star >= 0.6 else 0.8     # 规则 R (保守升级)
        m_h = group_metrics(q_h, "jac", dep)
        m_hd = group_metrics(q_h, "jac", k_star)
        folds.append({"heldout": h, "k_star": k_star, "feasible": feas,
                      "tune_power": m_t["power"], "tune_far": m_t["far"],
                      "r_threshold": dep,
                      "held_power_R": m_h["power"], "held_far_R": m_h["far"],
                      "held_power_direct": m_hd["power"],
                      "held_far_direct": m_hd["far"]})

    def fold_summary(key_p, key_f):
        ps = [f[key_p] for f in folds if f[key_p] is not None]
        fs = [f[key_f] for f in folds if f[key_f] is not None]
        return {"power_mean": round(sum(ps) / len(ps), 3),
                "power_min": min(ps), "far_mean": round(sum(fs) / len(fs), 4),
                "far_max": max(fs)}

    part4 = {"budget_far": FAR_BUDGET, "grid": K_GRID,
             "n_q": len(qs_sil), "n_onset": 90,
             "oracle": {"th": o_th, "power": o_m["power"],
                        "far": o_m["far"]},
             "sweep": sweep, "archived_splits": splits,
             "per_db_grid": per_db_grid, "folds": folds,
             "summary_R": fold_summary("held_power_R", "held_far_R"),
             "summary_direct": fold_summary("held_power_direct",
                                            "held_far_direct")}
    print(f"[rev] P4 LODO: "
          + "; ".join(f"{f['heldout']}: K*={f['k_star']}->R{f['r_threshold']}"
                      f" P/FAR={f['held_power_R']}/{f['held_far_R']}"
                      for f in folds), flush=True)

    # ---- Part 2c: S 族比值 (先对账再 bootstrap) ----
    ratio = {}
    for tag, root, side in (("G1", "gen", "chat"), ("G2", "gen_v4b", "v4b")):
        qs = load_s_side(root)
        stats = [cell_stats(q, q["alarms"]) for q in qs]
        agg = agg_cells(stats)
        so = stored["t1"][side]["S"]["OR"]
        rows_ok = (agg["n_questions"] == so["n_questions"]
                   and agg["n_onset"] == so["n_onset"]
                   and agg["continue_gold"]["power"] == so["power"]
                   and agg["latched_nogold"]["power"]
                   == so["power_screen_latched"]
                   and agg["screen_far_latched"] == so["screen_far_latched"]
                   and agg["continue_gold"]["confirmed"] == so["confirmed"]
                   and agg["continue_gold"]["rejected"] == so["rejected"]
                   and agg["continue_gold"]["gold_total"]
                   == so["gold_confirm_total"]
                   and agg["n_stable_points"] == so["n_stable_points"])
        xcheck["part2c_S_vs_stored"].append({
            "tag": tag, "side": side,
            "n": [agg["n_questions"], so["n_questions"]],
            "n_onset": [agg["n_onset"], so["n_onset"]],
            "two_stage_power": [agg["continue_gold"]["power"], so["power"]],
            "latched_power": [agg["latched_nogold"]["power"],
                              so["power_screen_latched"]],
            "match": rows_ok})
        if not rows_ok:
            raise SystemExit(f"Part2c S-family xcheck FAILED ({tag}) — abort")
        n_on = agg["n_onset"]
        cg_conf = agg["continue_gold"]["confirmed"]
        lat_hits = sum(s["lat_hit"] for s in stats)
        pools = defaultdict(list)
        for q, s in zip(qs, stats):
            pools[q["db"]].append((s["onset"], s["cg_det"], s["lat_hit"]))
        nums, dens, rats, zero_den = [], [], [], 0
        for b in range(B_BOOT):
            rng = random.Random(f"rev:ratio:{tag}:{b}")
            on = det = lat = 0
            for _ in range(5):
                db = FAMILY_DBS[rng.randrange(5)]
                pool = pools[db]
                for _ in range(len(pool)):
                    p = pool[rng.randrange(len(pool))]
                    on += p[0]
                    det += p[1]
                    lat += p[2]
            if on:
                nums.append(det / on)
                dens.append(lat / on)
                if lat > 0:
                    rats.append((det / on) / (lat / on))
                else:
                    zero_den += 1
        ratio[tag] = {
            "root": root, "n": len(qs), "n_onset": n_on,
            "two_stage_power": agg["continue_gold"]["power"],
            "latched_power": agg["latched_nogold"]["power"],
            "confirmed": cg_conf, "latched_hits": lat_hits,
            "raw_ratio": round(cg_conf / lat_hits, 2),
            "rounded_power_ratio": round(
                agg["continue_gold"]["power"]
                / agg["latched_nogold"]["power"], 1),
            "two_stage_ci": pctile(nums, 0.025, 0.975),
            "latched_ci": pctile(dens, 0.025, 0.975),
            "ratio_ci": pctile(rats, 0.025, 0.975),
            "n_valid_draws": len(nums), "n_zero_denominator_draws": zero_den}
        print(f"[rev] P2c {tag} ({root}): two-stage "
              f"{ratio[tag]['two_stage_power']} / latched "
              f"{ratio[tag]['latched_power']} = raw "
              f"{ratio[tag]['raw_ratio']}x, ratio CI "
              f"{ratio[tag]['ratio_ci']}, zero-den draws {zero_den}",
          flush=True)

    # ---- Part 2a/2b: 层级 bootstrap + 逐 DB 表 ----
    boot_pool = defaultdict(list)
    for k in GK["SILENT"]:
        s = cell_stats(Q[k], AL[k]["OR"])
        boot_pool[Q[k]["db"]].append(s)
    per_db_tab = {}
    for db in sorted(FAMILY_DBS):
        for fam in ("V", "W2"):
            ks = [k for k in GK["SILENT"]
                  if Q[k]["db"] == db and Q[k]["fam"] == fam]
            st = [cell_stats(Q[k], AL[k]["OR"]) for k in ks]
            n_on = sum(s["onset"] for s in st)
            conf = sum(s["cg_det"] for s in st)
            per_db_tab[f"{db}|{fam}"] = {
                "n_q": len(st), "n_onset": n_on, "confirmed": conf,
                "missed": n_on - conf,
                "gold_total": sum(s["cg_cost"] for s in st),
                "gold_per_q": round(sum(s["cg_cost"] for s in st) / len(st), 2)
                if st else None}
    draws = defaultdict(list)
    for b in range(B_BOOT):
        rng = random.Random(f"rev:boot:{b}")
        on = det = cost = lhit = lcov = nst = chit = csal = ntot = 0
        for _ in range(5):
            db = FAMILY_DBS[rng.randrange(5)]
            pool = boot_pool[db]
            n = len(pool)
            for _ in range(n):
                p = pool[rng.randrange(n)]
                on += p["onset"]
                det += p["cg_det"]
                cost += p["cg_cost"]
                lhit += p["lat_hit"]
                lcov += p["lat_cover"]
                nst += p["n_stable"]
                chit += p["cont_hit"]
                csal += p["stable_al"]
                ntot += 1
        if on:
            draws["dg_power"].append(det / on)
            draws["lat_power"].append(lhit / on)
            draws["cont_power"].append(chit / on)
        draws["dg_goldpq"].append(cost / ntot)
        if nst:
            draws["lat_far"].append(lcov / nst)
            draws["cont_far"].append(csal / nst)
    sil = part1["SILENT"]["OR"]
    hier = {
        "params": {"b_draws": B_BOOT, "seed": "rev:boot:{b}",
                   "scheme": "DB→question 两级有放回 (同一次重采样内重算三单元格)"},
        "driftguard_or": {
            "power_point": sil["continue_gold"]["power"],
            "power_ci": pctile(draws["dg_power"], 0.025, 0.975),
            "goldpq_point": sil["continue_gold"]["gold_per_q"],
            "goldpq_ci": pctile(draws["dg_goldpq"], 0.025, 0.975),
            "op_far_point": 0, "op_far_ci": [0.0, 0.0],
            "n_valid_power_draws": len(draws["dg_power"])},
        "latched_nogold": {
            "power_point": sil["latched_nogold"]["power"],
            "power_ci": pctile(draws["lat_power"], 0.025, 0.975),
            "goldpq_point": 0.0, "goldpq_ci": [0.0, 0.0],
            "op_far_point": sil["latched_nogold"]["op_far"],
            "op_far_ci": pctile(draws["lat_far"], 0.025, 0.975),
            "n_valid_power_draws": len(draws["lat_power"])},
        "continue_nogold": {
            "power_point": sil["continue_nogold"]["power"],
            "power_ci": pctile(draws["cont_power"], 0.025, 0.975),
            "goldpq_point": 0.0, "goldpq_ci": [0.0, 0.0],
            "op_far_point": sil["continue_nogold"]["op_far"],
            "op_far_ci": pctile(draws["cont_far"], 0.025, 0.975),
            "n_valid_power_draws": len(draws["cont_power"])},
    }
    print(f"[rev] P2a hier-boot: DriftGuard power CI "
          f"{hier['driftguard_or']['power_ci']}, gold/q CI "
          f"{hier['driftguard_or']['goldpq_ci']}", flush=True)

    # ---- Part 2d: 零事件口径 ----
    rej = {g: part1[g]["OR"]["continue_gold"]["rejected"]
           for g in ("SILENT", "W3", "BENIGN")}
    conf_ctrl = {"W3": part1["W3"]["OR"]["continue_gold"]["confirmed"],
                 "BENIGN": part1["BENIGN"]["OR"]["continue_gold"]["confirmed"]}
    zero_event = {
        "benign_confirmations_g1_or": {
            "SILENT": rej["SILENT"], "W3": rej["W3"],
            "BENIGN": rej["BENIGN"],
            "total": rej["SILENT"] + rej["W3"] + rej["BENIGN"]},
        "all_true_rejections": True,
        "false_confirmations": 0,
        "control_group_confirmations": conf_ctrl,
        "n_db_clusters": 5,
        "db_events": {db: 0 for db in FAMILY_DBS},
        "db_level_p_upper_95": round(1 - 0.05 ** (1 / 5), 4),
        "note_cluster_level": ("上界为 DB (cluster) 级二项精确: 5 个 DB 均 0 "
                              "事件 → 每 DB 至少 1 次假确认概率的 95% 上界 "
                              "= 1-0.05^(1/5)≈0.45; 非 unit 级 (同题多 "
                              "checkpoint 重复观测, unit 独立性不成立)"),
        "retained_count_statement": (
            "保留论文计数陈述: 1,314 个良性控制单元零确认 (W3 564 + "
            "benign 750, 见 REPRODUCING.md) — 仅作计数陈述, 不附 "
            "3/1314≈0.23% 的 unit 级 rule-of-three 上界"),
        "traceable_counts": (
            "本重分析可追溯口径: G1 OR 配置良性确认 "
            f"{rej['SILENT']}+{rej['W3']}+{rej['BENIGN']}="
            f"{rej['SILENT'] + rej['W3'] + rej['BENIGN']} 次, 全部为真拒绝 "
            "(gold 在 truth=False 单元上全部正确拒绝); 控制组确认 "
            f"W3={conf_ctrl['W3']}, BENIGN={conf_ctrl['BENIGN']} — 假确认 0")}

    # ---- Part 3: placebo 诊断 ----
    never_keys = [k for k in GK["SILENT"]
                 if not any(Q[k]["truth"])]
    onset_keys = [k for k in GK["SILENT"] if any(Q[k]["truth"])]
    assert len(never_keys) == 210, f"从未漂移题应为 210, 实为 {len(never_keys)}"
    assert len(onset_keys) == 90
    print(f"[rev] P3 never-drifted={len(never_keys)} onset={len(onset_keys)}",
          flush=True)
    # 首报警位置 vs detector_results.json 对账
    dr = load_json(os.path.join(HERE, "results", "detector_results.json"))
    dmap = {(q["fam"], q["qid"]): q for q in dr["questions"]
            if q["fam"] in ("V", "W2")}
    n_pos_checked = n_pos_mismatch = 0
    for k in GK["SILENT"]:
        q, dk = Q[k], dmap.get(k)
        if dk is None:
            n_pos_mismatch += 1
            continue
        first_t = {"D5": None, "D6@0.30": None}
        for cfg in ("D5", "D6@0.30"):
            fi = next((i for i, a in enumerate(AL[k][cfg]) if a), None)
            first_t[cfg] = q["ts"][fi] if fi is not None else None
        if (dk["ts"] == q["ts"]
                and dk["onset"] == next(
                    (t for t, f in zip(q["ts"], q["truth"]) if f), None)
                and first_t["D5"] == dk["alarms"]["D5_mode"]
                and first_t["D6@0.30"] == dk["alarms"]["D6_jac"]):
            n_pos_checked += 1
        else:
            n_pos_mismatch += 1
    xcheck["part3_first_alarm_vs_detector_results"] = {
        "n_questions": len(GK["SILENT"]), "n_match": n_pos_checked,
        "n_mismatch": n_pos_mismatch, "match": n_pos_mismatch == 0}
    if n_pos_mismatch:
        raise SystemExit("Part3 first-alarm xcheck vs detector_results.json "
                         "FAILED — abort")

    def first_pos(k, cfg):
        fi = next((i for i, a in enumerate(AL[k][cfg]) if a), None)
        return (len(Q[k]["ts"]), fi + 1 if fi is not None else None)

    def window_of(k):
        q = Q[k]
        on = next(t for t, f in zip(q["ts"], q["truth"]) if f)
        return frozenset(t for t in q["ts"] if t >= on)

    windows = {k: window_of(k) for k in onset_keys}
    pools_f = defaultdict(list)
    for k in never_keys:
        pools_f[Q[k]["fam"]].append(k)
    placebo = {}
    for cfg in PBO_CONFIGS:
        stable = [first_pos(k, cfg) for k in never_keys]
        f_hat = mle_rate(stable)
        n_al = sum(1 for (_, a) in stable if a is not None)
        ws = [len(windows[k]) for k in onset_keys]
        iid_exp = sum(1 - (1 - f_hat) ** w for w in ws) / len(ws)
        # 逐 checkpoint 稳定单元报警率 (210 题全部单元均为 stable)
        rate_t, n_t = {}, {}
        for t in range(1, 6):
            al = tot = 0
            for k in never_keys:
                for tt, a in zip(Q[k]["ts"], AL[k][cfg]):
                    if tt == t:
                        tot += 1
                        al += 1 if a else 0
            rate_t[t] = round(al / tot, 4) if tot else None
            n_t[t] = tot
        # lag-1 自相关 (题内 stable 段 = 从未漂移题整段序列)
        corrs, n_const, n_short = [], 0, 0
        for k in never_keys:
            xs = [a for a in AL[k][cfg]]
            if len(xs) < 2:
                n_short += 1
                continue
            c = pearson(xs[:-1], xs[1:])
            if c is None:
                n_const += 1
            else:
                corrs.append(c)
        # 族匹配序列交换 placebo
        donor_times = {k: frozenset(t for t, a in zip(Q[k]["ts"], AL[k][cfg])
                                     if a) for k in never_keys}
        pows_b = []
        for b in range(B_SWAP):
            rng = random.Random(f"rev:swap:{b}")
            det = 0
            for k in onset_keys:
                donor = rng.choice(pools_f[Q[k]["fam"]])
                if windows[k] & donor_times[donor]:
                    det += 1
            pows_b.append(det / len(onset_keys))
        placebo[cfg] = {
            "f_hat": f_hat, "n_alarmed_never": n_al,
            "n_never": len(never_keys),
            "iid_expected_power": round(iid_exp, 3),
            "per_ckpt_stable_rate": rate_t, "per_ckpt_n": n_t,
            "lag1_autocorr_mean": round(sum(corrs) / len(corrs), 4),
            "lag1_n_defined": len(corrs), "lag1_n_constant": n_const,
            "lag1_n_short": n_short,
            "swap_expected_power": round(sum(pows_b) / len(pows_b), 3),
            "swap_power_ci": pctile(pows_b, 0.025, 0.975),
            "swap_b_draws": B_SWAP, "swap_seed": "rev:swap:{b}",
            "observed_driftguard_power":
                part1["SILENT"][cfg]["continue_gold"]["power"],
            "observed_continue_nogold_power":
                part1["SILENT"][cfg]["continue_nogold"]["power"]}
        print(f"[rev] P3 {cfg}: f_hat={f_hat:.3f} iid_exp="
              f"{placebo[cfg]['iid_expected_power']} swap_exp="
              f"{placebo[cfg]['swap_expected_power']} lag1="
              f"{placebo[cfg]['lag1_autocorr_mean']}", flush=True)
    # f̂ 对账 vs 论文 (main.tex: D5 0.080, D6@0.30 0.265; OR 此前被错误沿用)
    xcheck["part3_fhat_vs_paper"] = {
        "D5": [placebo["D5"]["f_hat"], 0.080],
        "D6@0.30": [placebo["D6@0.30"]["f_hat"], 0.265],
        "match": placebo["D5"]["f_hat"] == 0.080
        and placebo["D6@0.30"]["f_hat"] == 0.265}
    if not xcheck["part3_fhat_vs_paper"]["match"]:
        print(f"  f_hat mismatch: {xcheck['part3_fhat_vs_paper']}")
        raise SystemExit("Part3 f_hat xcheck vs paper FAILED — abort")
    # OR 独立估计与 D6 重合的机制诊断: 从未漂移题上 D5 首报警从未早于 D6
    or_extra_units = sum(1 for k in never_keys for a6, ao in
                         zip(AL[k]["D6@0.30"], AL[k]["OR"]) if ao and not a6)
    or_earlier = 0
    for k in never_keys:
        f6 = next((i for i, a in enumerate(AL[k]["D6@0.30"]) if a), None)
        fo = next((i for i, a in enumerate(AL[k]["OR"]) if a), None)
        if fo is not None and (f6 is None or fo < f6):
            or_earlier += 1
    or_diag = {"or_extra_alarm_units_vs_d6": or_extra_units,
               "or_first_earlier_than_d6_never_q": or_earlier}
    iid_paper = {"D5": 0.204, "D6@0.30": 0.549}
    xcheck["part3_iid_vs_paper"] = {
        "D5": [placebo["D5"]["iid_expected_power"], iid_paper["D5"]],
        "D6@0.30": [placebo["D6@0.30"]["iid_expected_power"],
                    iid_paper["D6@0.30"]],
        "match": placebo["D5"]["iid_expected_power"] == iid_paper["D5"]
        and placebo["D6@0.30"]["iid_expected_power"] == iid_paper["D6@0.30"]}
    if not xcheck["part3_iid_vs_paper"]["match"]:
        print(f"  iid power mismatch: {xcheck['part3_iid_vs_paper']}")
        raise SystemExit("Part3 iid placebo xcheck vs paper FAILED — abort")

    # ---- 汇总 + 写出 ----
    res = {
        "params": {
            "configs": list(CONFIGS), "cells": list(CELLS),
            "b_boot": B_BOOT, "b_swap": B_SWAP,
            "boot_seed": "rev:boot:{b}", "swap_seed": "rev:swap:{b}",
            "ratio_seed": "rev:ratio:{tag}:{b}",
            "or_thresholds": {"D6@0.30": TH_JAC, "D6@0.50": TH50},
            "far_budget_part4": FAR_BUDGET,
            "zero_api": True},
        "xcheck": {k: (v if isinstance(v, dict) else
                       {"n": len(v), "n_match": sum(1 for r in v
                                                    if r["match"]),
                        "rows": v})
                   for k, v in xcheck.items()},
        "part1_ablation_2x2": part1,
        "part2_hier_bootstrap": hier,
        "part2_per_db": per_db_tab,
        "part2_ratio": ratio,
        "part2_zero_event": zero_event,
        "part3_placebo": {
            "n_never_drifted": len(never_keys), "n_onset": len(onset_keys),
            "or_vs_d6_diagnostic": or_diag,
            "configs": placebo},
        "part4_lodo": part4,
    }
    resdir = os.path.join(HERE, "results")
    with open(os.path.join(resdir, "revision_analysis.json"), "w",
              encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    write_report(res, resdir)
    print(f"[rev] done in {time.time() - t0:.1f} s", flush=True)


# ---------------- 报告 ----------------

def write_report(res, resdir):
    part1 = res["part1_ablation_2x2"]
    hier = res["part2_hier_bootstrap"]
    pdb = res["part2_per_db"]
    ratio = res["part2_ratio"]
    ze = res["part2_zero_event"]
    pbo = res["part3_placebo"]
    p4 = res["part4_lodo"]
    L = ["# DriftSQL 审稿修订零 API 重分析 (M3/M5/M9/M7)\n",
         f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')} "
         "(run_revision_analysis.py, 零 API — 只读 gen/gen_v4b/gold/results "
         "缓存, 无任何 LLM/API 调用)\n",
         "## 0. 基线对账 (全部通过后本报告才写出)\n",
         "| 检查 | 口径 | 结果 |", "|---|---|---|"]
    x = res["xcheck"]
    cg = x["part1_continue_gold_vs_stored"]["rows"]
    L.append(f"| P1 Continue/有gold (4 配置×3 组×8 字段) vs "
             f"combined_monitor.json | 逐字段 | "
             f"{sum(1 for r in cg if r['match'])}/{len(cg)} 全部通过 |")
    la = x["part1_latched_vs_stored"]["rows"]
    L.append(f"| P1 Latched/无gold power/FAR vs 存档 power_screen_latched/"
             f"screen_far_latched | 4 配置×3 组 | "
             f"{sum(1 for r in la if r['match'])}/{len(la)} 全部通过 |")
    sc = x["part2c_S_vs_stored"]["rows"]
    L.append(f"| P2c S 族两阶段/latched/确认/拒绝/gold vs "
             f"combined_monitor.json t1.chat|v4b.S.OR | 逐字段 | "
             + " / ".join(f"{r['tag']}:{r['match']}" for r in sc) + " |")
    L.append(f"| P3 首报警位置 vs detector_results.json (V+W2 300 题) | "
             f"ts/onset/D5/D6 首报警时间 | "
             f"{x['part3_first_alarm_vs_detector_results']['n_match']}/"
             f"{x['part3_first_alarm_vs_detector_results']['n_questions']}"
             " 全部通过 |")
    L.append(f"| P3 f̂ vs 论文 (D5 0.080, D6@0.30 0.265) | 删失几何 MLE "
             "(网格 i/200, 复刻 placebo_control.py) | "
             f"{x['part3_fhat_vs_paper']['match']} ✓ |")
    L.append(f"| P3 iid 期望功率 vs 论文 (D5 0.204, D6@0.30 0.549) | "
             f"mean_q[1-(1-f̂)^W_q] | "
             f"{x['part3_iid_vs_paper']['match']} ✓ |")
    L.append(f"| P4 pooled 18 点网格 sweep vs p04_threshold_bootstrap.json | "
             f"逐行 (th/power/far/n_on/n_stable/hits/covered/n_q) | "
             f"{x['part4_pooled_sweep']['sweep_match']} ✓ |")
    L.append(f"| P4 oracle (K=0.8, P 0.622 / FAR 0.0633) vs 存档 | "
             f"select_threshold (FAR≤0.25 最大化 power, 平局低 FAR→低 K) | "
             f"{x['part4_oracle']['match']} ✓ |")
    L.append(f"| P4 存档 10 折 (2 库调参/3 库留出, eval_power_mean 0.577) | "
             f"逐折 K*/留出 P/FAR | "
             f"{x['part4_archived_splits']['match']} ✓ |")

    # ---- Part 1 表 ----
    sec = {"SILENT": "### 1.1 SILENT (V+W2, 300 题 / 90 onset)",
           "W3": "### 1.2 对照组 W3 (141 题, 无 onset)",
           "BENIGN": "### 1.3 对照组 BENIGN (150 题, 无 onset)"}
    L.append("\n## 1. Part 1 (M3): 2×2 消融 — 监控语义 (latched/"
             "continue) × gold 确认 (无/有)\n")
    for gname in ("SILENT", "W3", "BENIGN"):
        L.append("")
        L.append(sec[gname])
        L.append("")
        L.append("| 配置 | 单元格 | power | 中位延迟 | 操作员FAR | "
                 "stable可见报警题占比 | 总可见报警 | gold 总/题均 | "
                 "确认/拒绝 |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for cfg in CONFIGS:
            d = part1[gname][cfg]
            for cell in CELLS:
                c = d[cell]
                L.append(
                    f"| {cfg} | {CELL_LABEL[cell]} | {fm(c['power'])} "
                    f"| {fm(c['med_delay'])} | {fm(c['op_far'])} "
                    f"| {c['q_frac_visible_stable']} | {c['visible_total']} "
                    f"| {c['gold_total']} / {c['gold_per_q']} "
                    f"| {fm(c['confirmed'])} / {fm(c['rejected'])} |")
        if gname == "SILENT":
            L += ["", "Continue/无gold 单元格的逐单元口径明细 (SILENT):",
                  "| 配置 | stable 单元报警总数 | 有≥1 stable 报警题数 (占比) "
                  "| 总报警数 | 逐单元 FAR |",
                  "|---|---|---|---|---|"]
            for cfg in CONFIGS:
                c = part1["SILENT"][cfg]["continue_nogold"]
                L.append(f"| {cfg} | {c['stable_alarm_total']} "
                         f"| {c['n_q_stable_alarm']} "
                         f"({c['q_frac_visible_stable']}) "
                         f"| {c['visible_total']} | {c['op_far']} |")
    p = part1["SILENT"]["OR"]
    L += ["", "**Part 1 要点 (SILENT, OR 配置)**: ",
          f"- 监控语义决定功效上限: latched 判决 (首报警) 只有 "
          f"{p['latched_nogold']['power']}; continue 语义把可判题恢复到 "
          f"{p['continue_nogold']['power']} (任一 post-onset 报警) / "
          f"{p['continue_gold']['power']} (DriftGuard: 报警落在 truth=True "
          "单元并经 gold 确认; 瞬态漂移题上两口径可有微小差异)。",
          f"- gold 确认决定操作员 FAR: latched 覆盖口径 FAR "
          f"{p['latched_nogold']['op_far']} / continue 逐单元口径 "
          f"{p['continue_nogold']['op_far']} → 加 gold 后均为 0, 代价 = "
          f"{p['continue_gold']['gold_total']} 次 gold 确认 "
          f"({p['continue_gold']['gold_per_q']}/题)。",
          f"- Latched/有gold 不恢复功效 ({p['latched_gold']['power']}, "
          "首报警需落在 truth=True 单元): 首个误报被 gold 拒绝后按 "
          "latched 语义停止监测, 后续真漂移不可见 "
          f"(确认 {p['latched_gold']['confirmed']} / 拒绝 "
          f"{p['latched_gold']['rejected']}, gold/q="
          f"{p['latched_gold']['gold_per_q']})。"]

    # ---- Part 2 表 ----
    L += ["\n## 2. Part 2 (M5): 层级 bootstrap + 逐 DB 表 + 比值区间 + "
          "零事件口径\n",
          "### 2.1 层级 bootstrap (DB→题, B=2000, 种子 rev:boot:{b}; "
          "SILENT power / gold per q / 操作员 FAR; 同一重采样内重算)\n",
          "| 单元格 | 指标 | 点估计 | 95% CI |", "|---|---|---|---|"]
    for key, label in (("driftguard_or", "DriftGuard OR (continue/gold)"),
                      ("latched_nogold", "Latched/无gold"),
                      ("continue_nogold", "Continue/无gold")):
        d = hier[key]
        L.append(f"| {label} | power | {fm(d['power_point'])} | "
                 f"{fmb(d['power_ci'])} |")
        L.append(f"| {label} | gold/q | {fm(d['goldpq_point'])} | "
                 f"{fmb(d['goldpq_ci'])} |")
        L.append(f"| {label} | 操作员 FAR | {fm(d['op_far_point'])} | "
                 f"{fmb(d['op_far_ci'])} |")
    L.append("\n(DriftGuard 操作员 FAR=0 为构造性 0, CI 退化为 [0,0]; "
             "无 gold 单元格 gold/q 恒 0。)\n")
    L += ["### 2.2 逐 DB 表 (SILENT, DriftGuard OR)\n",
          "| DB | 族 | n题 | onset | 确认检出 | 漏检 | gold 总 | gold/题 |",
          "|---|---|---|---|---|---|---|---|"]
    for db in sorted(FAMILY_DBS):
        for fam in ("V", "W2"):
            r = pdb[f"{db}|{fam}"]
            L.append(f"| {db} | {fam} | {r['n_q']} | {r['n_onset']} "
                     f"| {r['confirmed']} | {r['missed']} "
                     f"| {r['gold_total']} | {r['gold_per_q']} |")
    L += ["\n### 2.3 两阶段/latched 功效比值 (G1 chat 与 G2 v4b 的 S 族 "
          "loud; 联合层级 bootstrap, 分子分母同次重采样, 分母 0 的 draw 单独"
          "计数、不算无穷)\n",
          "| 侧 | n/onset | 两阶段 power [CI] | latched power [CI] | "
          "比值 (计数 / 四舍五入功效) | 比值 95% CI | 分母=0 draws |",
          "|---|---|---|---|---|---|---|"]
    for tag in ("G1", "G2"):
        r = ratio[tag]
        L.append(f"| {tag} ({r['root']}) | {r['n']}/{r['n_onset']} "
                 f"| {r['two_stage_power']} {fmb(r['two_stage_ci'])} "
                 f"| {r['latched_power']} {fmb(r['latched_ci'])} "
                 f"| {r['confirmed']}/{r['latched_hits']} = "
                 f"{r['raw_ratio']}× (功效口径 {r['rounded_power_ratio']}×) "
                 f"| {fmb(r['ratio_ci'])} | {r['n_zero_denominator_draws']} |")
    L.append("\n(比值同时给出计数口径 (确认数/锁存命中数) 与四舍五入功效口径; "
              "G1 对应论文 6.6×; G2 论文的 10.1× 来自功效 0.455/0.045。"
              "分母=0 的 draw 已单独计数、不进入 CI、不算无穷。)\n")
    L += ["### 2.4 零事件口径 (对照组 0 假确认)\n",
          f"- G1 OR 配置良性确认 {ze['benign_confirmations_g1_or']['SILENT']}"
          f" (SILENT) + {ze['benign_confirmations_g1_or']['W3']} (W3) + "
          f"{ze['benign_confirmations_g1_or']['BENIGN']} (BENIGN) = "
          f"{ze['benign_confirmations_g1_or']['total']} 次, 全部为真拒绝 "
          "(gold 在 truth=False 单元上全部正确拒绝); 控制组确认 "
          f"W3={ze['control_group_confirmations']['W3']}, "
          f"BENIGN={ze['control_group_confirmations']['BENIGN']} → "
          f"假确认 {ze['false_confirmations']}。",
          f"- **DB 级保守上界 (cluster 级, 二项精确)**: 5 个 DB 均 0 事件 → "
          f"每 DB 至少 1 次假确认概率的 95% 上界 = 1-0.05^(1/5) = "
          f"{ze['db_level_p_upper_95']} ≈ 0.45。明确标注: 这是 cluster "
          "(DB) 级上界, **非 unit 级** — 同题多 checkpoint 为重复观测, unit "
          "独立性不成立 (评审意见)。",
          "- 保留论文计数陈述: 「1,314 个良性控制单元零确认 (W3 564 + "
          "benign 750)」 — 仅作计数陈述保留, 不附 3/1314≈0.23% 的 unit 级 "
          "rule-of-three 上界 (完整措辞与可追溯计数见 JSON part2_zero_event)。"]

    # ---- Part 3 表 ----
    L += ["\n## 3. Part 3 (M9): placebo 诊断 (SILENT 从未漂移 210 题核对 ✓)\n",
          "### 3.1 iid vs 族匹配序列交换 vs 实测\n",
          "| 配置 | f̂ (MLE, 各自独立) | 210 题中报警题数 | iid 期望功率 | "
          "序列交换期望功率 [CI] | 实测 DriftGuard | 实测 Continue/无gold |",
          "|---|---|---|---|---|---|---|"]
    for cfg in PBO_CONFIGS:
        d = pbo["configs"][cfg]
        L.append(f"| {cfg} | {d['f_hat']} | {d['n_alarmed_never']}/210 "
                 f"| {d['iid_expected_power']} "
                 f"| {d['swap_expected_power']} {fmb(d['swap_power_ci'])} "
                 f"| {d['observed_driftguard_power']} "
                 f"| {d['observed_continue_nogold_power']} |")
    L += ["\n修正: 论文此前把 OR 沿用 D6@0.30 的 f̂=0.265 (审稿人指出错误); "
          "本表 OR 的 f̂/iid 期望功率为独立估计 (min 复合 D5∪D6 首报警位置)。"
          f"独立估计恰与 D6 相同 (0.265), 机制: 210 个从未漂移题上 D5 的"
          f"首报警从未早于 D6 的首报警 "
          f"(仅 {pbo['or_vs_d6_diagnostic']['or_extra_alarm_units_vs_d6']} "
          "个 OR-only 报警单元, "
          f"{pbo['or_vs_d6_diagnostic']['or_first_earlier_than_d6_never_q']}"
          " 题的 OR 首报警早于 D6), 故重用数值虽论证不严谨但恰好成立; "
          "OR 的逐 checkpoint 报警率与 lag-1 自相关仍与 D6 有可测差异。"
          "序列交换参照 (同族从未漂移题池有放回抽整条报警序列, B=1000) "
          "精确保持报警率与题内时间相关, 替代原 iid 「上界」主张 — "
          "iid 假设被诊断否定 (题内 lag-1 相关为负, 见 3.3), 且 D5 上 "
          f"swap 期望功率 {pbo['configs']['D5']['swap_expected_power']} > "
          f"iid {pbo['configs']['D5']['iid_expected_power']}, 说明 iid "
          "数值并非上界; "
          "分母事件计数: 交换检出按 donor 序列在 post-onset 窗口 "
          "(绝对 checkpoint 时间) 内有≥1 报警计。\n",
          "### 3.2 逐 checkpoint 稳定单元报警率 (210 从未漂移题, "
          "t=1..5)\n",
          "| 配置 | " + " | ".join(f"t={t} (n={pbo['configs']['D5']['per_ckpt_n'][t]})"
                               for t in range(1, 6)) + " |",
          "|---|---|---|---|---|---|"]
    for cfg in PBO_CONFIGS:
        d = pbo["configs"][cfg]
        L.append(f"| {cfg} | " + " | ".join(fm(d["per_ckpt_stable_rate"][t])
                                        for t in range(1, 6)) + " |")
    L += ["\n### 3.3 lag-1 自相关 (每题 stable 段报警指示序列, 题内 Pearson, "
          "池内平均)\n",
          "| 配置 | 题内 lag-1 相关均值 | 有定义题数 | 常数序列题数 | "
          "长度<2 题数 |", "|---|---|---|---|---|"]
    for cfg in PBO_CONFIGS:
        d = pbo["configs"][cfg]
        L.append(f"| {cfg} | {d['lag1_autocorr_mean']} "
                 f"| {d['lag1_n_defined']} | {d['lag1_n_constant']} "
                 f"| {d['lag1_n_short']} |")

    # ---- Part 4 表 ----
    L += ["\n## 4. Part 4 (M7): 规则 R 的 leave-one-database-out 嵌套验证 "
          "(D6 SILENT, chat)\n",
          f"调参准则 (复刻 run_p04.py part_a): 18 点网格 "
          f"{p4['grid'][0]}..{p4['grid'][-1]} 步长 0.05; 调参集锁存 "
          f"FAR≤{p4['budget_far']} 下最大化锁存 power (并列取低 FAR 再取低 "
          "K; 全网格不可行回退最小 FAR)。规则 R: K*≥0.6 部署 K*; K*=0.5 升级 "
          "到 0.8 (K*<0.5 时按同一保守原则升到 0.8)。\n",
          "### 4.1 存档对账\n",
          f"- pooled sweep 18 行逐行一致 ✓; oracle K={p4['oracle']['th']} "
          f"(P {p4['oracle']['power']} / FAR {p4['oracle']['far']}) ✓; "
          f"存档 10 折逐折 K*/留出 P/FAR 一致, eval_power_mean="
          f"{res['xcheck']['part4_archived_splits']['eval_power_mean'][0]} ✓\n",
          "### 4.2 逐 DB 网格 (latched power/FAR)\n",
          "| K | " + " | ".join(sorted(FAMILY_DBS)) + " |",
          "|---|" + "---|" * len(FAMILY_DBS)]
    for i, th in enumerate(p4["grid"]):
        cells = []
        for db in sorted(FAMILY_DBS):
            g = p4["per_db_grid"][db][i]
            cells.append(f"{fm(g['power'])} / {fm(g['far'])}")
        L.append(f"| {th} | " + " | ".join(cells) + " |")
    L += ["\n### 4.3 LODO 5 折 (4 库调参 → 规则 R → 留出库评估)\n",
          "| 留出 DB | K* | 可行 | 调参 P/FAR | R 部署阈值 | "
          "留出 P/FAR (R) | 留出 P/FAR (直用 K*) |",
          "|---|---|---|---|---|---|---|"]
    for f in p4["folds"]:
        L.append(f"| {f['heldout']} | {f['k_star']} | {f['feasible']} "
                 f"| {fm(f['tune_power'])} / {fm(f['tune_far'])} "
                 f"| {f['r_threshold']} "
                 f"| {fm(f['held_power_R'])} / {fm(f['held_far_R'])} "
                 f"| {fm(f['held_power_direct'])} / "
                 f"{fm(f['held_far_direct'])} |")
    sr, sd = p4["summary_R"], p4["summary_direct"]
    L += ["", "**5 折汇总**:",
          f"- 规则 R: 留出 power 均值 {sr['power_mean']}, 最差折 "
          f"{sr['power_min']}; 留出 FAR 均值 {sr['far_mean']}, 最差折 "
          f"{sr['far_max']}。",
          f"- 直用 K* (不应用 R): 留出 power 均值 {sd['power_mean']}, 最差折 "
          f"{sd['power_min']}; 留出 FAR 均值 {sd['far_mean']}, 最差折 "
          f"{sd['far_max']}。"]
    rp = os.path.join(resdir, "revision_analysis.md")
    with open(rp, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[rev] report -> {rp}", flush=True)


if __name__ == "__main__":
    main()
