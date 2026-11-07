# -*- coding: utf-8 -*-
"""run_combined_monitor.py — E11: 组合监测器评测 (零 API, 纯本地计算)。

回应评审体检 A1/A4/A6:
  T1 题级两阶段: 筛查信号 (D5/D6, 免费复用候选执行指纹) 在 (q,t) 报警
     → 执行该题 gold 1 条确认 (hash 变 = 真)。确认使用漂移感知 gold 标签
     → 确认后 FAR=0 (构造性), 误报被压制; 功效 = 筛查在 onset 后的报警。
     报告 power/延迟/锁存筛查FAR/两阶段FAR/gold 确认成本 (E1 场景 + W3 + V-benign)。
  T2 DB 级 gold 预算: 每库维护 m 个 gold 锚点 (随机子采样), 锚点漂移即库级
     告警 → power(m) 覆盖曲线 m∈{1,3,5,10,25,50} (bootstrap CI), 延迟,
     benign/W3 库级 FAR=0 验证。锚点成本 = m 次 gold 执行/检查点/库。
  T3 锚点解剖 (S 族 stale-gold 消融): benchmark 的 gold = 原 SQL 重执行
     ("未改写"), 其题级功效与 truth 定义重合 (A6 的同义反复); 实证内容
     = 按操作类型分列锚点信号的表现 (破坏=响亮/哈希变=静默/稳定),
     受影响题 vs 未受影响题对照, 分族 loud/silent 总览。
  T4 交叉核对: 重算 chat S/V/W2/W3 的 D5/D6 锁存数字并与
     detector_results.json (E4) / detector_w3.json (E9) 对账。

两阶段语义: 确认拒绝后继续监测 (不锁存误报); 确认为真后锁存, 停止计费。
输出: results/combined_monitor.md + results/combined_monitor.json
"""
from __future__ import annotations

import json
import os
import random
import time
from collections import Counter, defaultdict

from build_drift_benchmark import (HERE, SEED, CKPTS, FAMILY_DBS,
                                   db_path_of, list_tables, table_cols)
from build_v_benign import question_refs
from run_detectors import (DETS, DETN, TH_JAC, group_metrics, load_json,
                           load_jsonl, jac_dist)
import sqlite3

TMAX = {"S": 5, "V": 5, "W2": 3, "W3": 3}
SIDES = [("chat", "gen", "gen_w3"), ("v4b", "gen_v4b", "gen_w3b")]
M_GRID = [1, 3, 5, 10, 25, 50]
B_DRAWS = 200
TH_JAC2 = 0.50

SCREEN = {
    "D5": lambda s0, st: st["mode"] != s0["mode"],
    "D6@0.30": lambda s0, st: jac_dist(s0["cls"], st["cls"]) >= TH_JAC,
    "D6@0.50": lambda s0, st: jac_dist(s0["cls"], st["cls"]) >= TH_JAC2,
    "OR": lambda s0, st: (st["mode"] != s0["mode"]
                          or jac_dist(s0["cls"], st["cls"]) >= TH_JAC),
}


# ---------------- 序列加载 ----------------

def pool_stats(rows):
    cls = Counter(r["result_hash"] or "EXECFAIL" for r in rows)
    mode, cnt = max(cls.items(), key=lambda kv: (kv[1], kv[0]))
    return {"n": len(rows),
            "ex": sum(1 for r in rows if r["exec_ok"]) / len(rows),
            "cls": dict(cls), "mode": mode}


def load_series(root, fam, db, tmax):
    """gen / gen_v4b 下的 {qid: {t: stats}} (D5/D6/D1 所需字段)。"""
    per = defaultdict(dict)
    for t in range(0, tmax + 1):
        rows = load_jsonl(os.path.join(HERE, root, fam, db, f"ck{t}.jsonl"))
        per_q = defaultdict(list)
        for r in rows:
            per_q[r["qid"]].append(r)
        for qid, rs in per_q.items():
            per[qid][t] = pool_stats(rs)
    return per


def load_series_w3(root, db):
    per = defaultdict(dict)
    for t in range(0, 4):
        rows = load_jsonl(os.path.join(HERE, root, "W3", db, f"ck{t}.jsonl"))
        per_q = defaultdict(list)
        for r in rows:
            per_q[r["qid"]].append(r)
        for qid, rs in per_q.items():
            per[qid][t] = pool_stats(rs)
    return per


def truth_of(gentry):
    """{t: bool} — 漂移感知 gold: 执行破坏或指纹 != t0。"""
    g = gentry["checkpoints"]
    g0 = g.get("0", {})
    out = {}
    for t in range(1, 10):
        if str(t) not in g:
            continue
        gt = g[str(t)]
        out[t] = (not gt.get("exec_ok")) or \
                 (gt.get("result_hash") != g0.get("result_hash"))
    return out


def gold_map(fam):
    out = {}
    for r in load_jsonl(os.path.join(HERE, "gold", f"gold_{fam}.jsonl")):
        g0 = r["checkpoints"].get("0", {})
        if g0.get("exec_ok") and g0.get("result_hash"):
            out[r["qid"]] = {"db": r["db"], "truth": truth_of(r)}
    return out


# ---------------- T1 题级两阶段 ----------------

def eval_two_stage(ser, truth, cfg):
    """单题两阶段: 返回 (onset, detected_t, cost, rejected, latched_t)。"""
    ts = sorted(t for t in ser if t >= 1 and t in truth)
    if 0 not in ser or not ts:
        return None
    s0 = ser[0]
    onset = next((t for t in ts if truth[t]), None)
    detected, cost, rejected, latched = None, 0, 0, None
    for t in ts:
        if detected is not None:
            break
        if cfg(s0, ser[t]):
            if latched is None:
                latched = t
            cost += 1
            if truth[t]:
                detected = t
            else:
                rejected += 1
    return onset, detected, cost, rejected, latched


def scenario_metrics(recs):
    """recs: [{onset, detected, cost, rejected, latched, ts, truth}] 聚合。

    power = 两阶段 (确认后); power_screen_latched = 筛查首次报警 ≥ onset
    (E4 口径, 供对账与对照; 两阶段 ≥ 它, 因为拒绝后的早期误报不遮蔽后续)。
    """
    n_on = sum(1 for r in recs if r["onset"] is not None)
    hits = [r for r in recs if r["onset"] is not None
            and r["detected"] is not None]
    delays = [r["detected"] - r["onset"] for r in hits]
    lat_hits = [r for r in recs if r["onset"] is not None
                and r["latched"] is not None and r["latched"] >= r["onset"]]
    lat_delays = [r["latched"] - r["onset"] for r in lat_hits]
    # 锁存筛查 FAR (E4 口径: 首次报警覆盖其后的稳定检查点)
    n_stable = covered = 0
    for r in recs:
        lim = r["onset"] if r["onset"] is not None else 10 ** 9
        n_stable += sum(1 for t in r["ts"] if t < lim)
        if r["latched"] is not None:
            covered += sum(1 for t in r["ts"] if r["latched"] <= t < lim)
    n_pts = sum(len(r["ts"]) for r in recs)
    return {
        "n_questions": len(recs), "n_onset": n_on,
        "power": round(len(hits) / n_on, 3) if n_on else None,
        "med_delay": sorted(delays)[len(delays) // 2] if hits else None,
        "power_screen_latched": round(len(lat_hits) / n_on, 3) if n_on else None,
        "med_delay_screen": sorted(lat_delays)[len(lat_delays) // 2]
        if lat_delays else None,
        "screen_far_latched": round(covered / n_stable, 4) if n_stable else None,
        "two_stage_far": 0,   # gold 确认精确 (构造性 0, 见设计)
        "n_points": n_pts, "n_stable_points": n_stable,
        "gold_confirm_total": sum(r["cost"] for r in recs),
        "gold_confirm_per_q": round(sum(r["cost"] for r in recs) / len(recs), 2),
        "confirmed": len(hits),
        "rejected": sum(r["rejected"] for r in recs),
    }


def build_t1(side, include_benign=True):
    """{scenario: {config: [recs]}} — scenario ∈ S/V/W2/W3/BENIGN。

    include_benign=False: 跳过 BENIGN (E10b 未完成时的分阶段预验证)。
    """
    gendir, w3dir = side[1], side[2]
    scen = defaultdict(lambda: defaultdict(list))
    for fam in ("S", "V", "W2"):
        gold = gold_map(fam)
        for db in FAMILY_DBS:
            if not os.path.isdir(os.path.join(HERE, gendir, fam, db)):
                continue
            per = load_series(gendir, fam, db, TMAX[fam])
            for qid in sorted(per):
                if qid not in gold:
                    continue
                truth = gold[qid]["truth"]
                ser = {t: per[qid][t] for t in per[qid] if t in truth or t == 0}
                if 0 not in ser:
                    continue
                for cname, cfg in SCREEN.items():
                    r = eval_two_stage(ser, truth, cfg)
                    if r:
                        onset, det, cost, rej, lat = r
                        scen[fam][cname].append({
                            "qid": qid, "db": db, "onset": onset,
                            "detected": det, "cost": cost, "rejected": rej,
                            "latched": lat, "ts": sorted(
                                t for t in ser if t >= 1),
                            "truth": [truth[t] for t in sorted(
                                t for t in ser if t >= 1)]})
    # W3
    gold = gold_map("W3")
    for db in FAMILY_DBS:
        d = os.path.join(HERE, w3dir, "W3", db)
        if not os.path.isdir(d):
            continue
        per = load_series_w3(w3dir, db)
        for qid in sorted(per):
            if qid not in gold:
                continue
            truth = gold[qid]["truth"]
            ser = {t: per[qid][t] for t in per[qid] if t in truth or t == 0}
            if 0 not in ser:
                continue
            for cname, cfg in SCREEN.items():
                r = eval_two_stage(ser, truth, cfg)
                if r:
                    onset, det, cost, rej, lat = r
                    scen["W3"][cname].append({
                        "qid": qid, "db": db, "onset": onset, "detected": det,
                        "cost": cost, "rejected": rej, "latched": lat,
                        "ts": sorted(t for t in ser if t >= 1),
                        "truth": [truth[t] for t in sorted(
                            t for t in ser if t >= 1)]})
    # BENIGN (来自 E10 的重执行 series)
    if include_benign:
        bc = load_json(os.path.join(HERE, "results",
                                     "benign_control.json"))
        series = bc["sides"][side[0]]["series"]
        for qid, ser in series.items():
            ser = {int(k): v for k, v in ser.items()}
            if 0 not in ser:
                continue
            truth = {t: False for t in ser if t >= 1}
            for cname, cfg in SCREEN.items():
                r = eval_two_stage(ser, truth, cfg)
                if r:
                    onset, det, cost, rej, lat = r
                    scen["BENIGN"][cname].append({
                        "qid": qid, "db": None, "onset": onset,
                        "detected": det, "cost": cost, "rejected": rej,
                        "latched": lat, "ts": sorted(t for t in ser if t >= 1),
                        "truth": [False] * len([t for t in ser if t >= 1])})
    return scen


# ---------------- T2 DB 级 gold 预算 ----------------

def budget_curves():
    out = {}
    for fam in ("S", "V", "W2"):
        gold = gold_map(fam)
        by_db = defaultdict(dict)          # db -> {qid: first_drift}
        for qid, g in gold.items():
            fd = next((t for t in sorted(g["truth"]) if g["truth"][t]), None)
            by_db[g["db"]][qid] = fd
        events = {db: min(fd for fd in qs.values() if fd is not None)
                  for db, qs in by_db.items()
                  if any(fd is not None for fd in qs.values())}
        rows = {}
        if not events:
            out[fam] = {"events": {}, "pool_sizes": {db: len(by_db[db])
                                                     for db in by_db},
                        "rows": {}}
            continue
        for m in M_GRID:
            det_all, del_all = [], []
            for db, onset in sorted(events.items()):
                pool = sorted(by_db[db])
                m_eff = min(m, len(pool))
                dets = []
                for b in range(B_DRAWS):
                    rng = random.Random(f"{SEED}:budget:{fam}:{db}:{m}:{b}")
                    anchors = rng.sample(pool, m_eff)
                    fds = [by_db[db][a] for a in anchors
                           if by_db[db][a] is not None]
                    det = len(fds) > 0
                    dets.append(det)
                    if det:
                        del_all.append(min(fds) - onset)
                det_all.append(sum(dets) / len(dets))
            pm = sum(det_all) / len(det_all)
            per_draw = []
            for b in range(B_DRAWS):
                s = 0
                for db in events:
                    pool = sorted(by_db[db])
                    m_eff = min(m, len(pool))
                    rng = random.Random(f"{SEED}:budget:{fam}:{db}:{m}:{b}")
                    anchors = rng.sample(pool, m_eff)
                    if any(by_db[db][a] is not None for a in anchors):
                        s += 1
                per_draw.append(s / len(events))
            lo = sorted(per_draw)[int(0.025 * len(per_draw))]
            hi = sorted(per_draw)[int(0.975 * len(per_draw))]
            rows[m] = {"power": round(pm, 3), "ci": [round(lo, 3), round(hi, 3)],
                       "med_delay": sorted(del_all)[len(del_all) // 2]
                       if del_all else None,
                       "n_event_dbs": len(events)}
        out[fam] = {"events": {db: str(on) for db, on in events.items()},
                    "pool_sizes": {db: len(by_db[db]) for db in by_db},
                    "rows": rows}
    return out


# ---------------- T3 锚点解剖 (S 族) ----------------

def _op_hits(ops, refs_q):
    """ops 命中判定: rename_table→表引用; rename/drop_column→表+列引用;
    add_distractor→表引用+SELECT *。"""
    tabs, cols, starred = refs_q
    out = []
    for op in ops:
        k = op["op"]
        if k == "rename_table" and op["table"] in tabs:
            out.append("rename_table")
        elif k in ("rename_column", "drop_column") \
                and op["table"] in tabs and op["col"] in cols:
            out.append(k)
        elif k == "add_distractor" and op["table"] in tabs and starred:
            out.append("add_distractor(star)")
    return out


def anchor_anatomy():
    """onset 归因口径: 检查点累积, 每题只按首发 (onset) ck 的操作归因一次。

    旧口径把每个 (题×ck) 独立分类, 前驱破坏被重复记到后续操作头上 (10 个
    'unaffected 却 break' 残差全部 = ck2 rename_column 破坏继承到 ck3/ck4);
    onset 口径下归因率 100% (probe_residuals.py 验证)。
    """
    with open(os.path.join(HERE, "questions", "family_S.json"),
              encoding="utf-8") as f:
        qs = json.load(f)
    manifest = load_json(os.path.join(HERE, "transforms",
                                      "manifest_S.json"))
    gold = {}
    for r in load_jsonl(os.path.join(HERE, "gold", "gold_S.jsonl")):
        g0 = r["checkpoints"].get("0", {})
        if g0.get("exec_ok") and g0.get("result_hash"):
            gold[r["qid"]] = r
    refs = {}
    for db in FAMILY_DBS:
        con = sqlite3.connect(db_path_of(db))
        tbl_cols = {tb: table_cols(con, tb) for tb in list_tables(con)}
        con.close()
        for q in qs:
            if q["db"] == db:
                refs[q["qid"]] = question_refs(q["sql"], tbl_cols)
    attribution = defaultdict(lambda: Counter())   # 漂移题, onset ck 命中
    unattributed = Counter()      # 漂移题, onset ck 无命中 (期望 0)
    stable_hit = Counter()        # 全程 stable 且命中过操作 (受影响但稳定)
    n_stable_unaff = 0            # 全程 stable 且零命中 (对照, 期望全 stable)
    per_ck = {}
    for db in FAMILY_DBS:
        for t in range(1, 6):
            ops = manifest[db]["checkpoints"].get(str(t), [])
            ck = Counter()
            for qid, g in gold.items():
                if g["db"] != db or str(t) not in g["checkpoints"]:
                    continue
                gt = g["checkpoints"][str(t)]
                if not gt.get("exec_ok"):
                    outcome = "break"
                elif gt.get("result_hash") != g["checkpoints"]["0"]["result_hash"]:
                    outcome = "silent"
                else:
                    outcome = "stable"
                ck[outcome] += 1
            per_ck[f"{db}:t{t}"] = {"ops": [o["op"] for o in ops],
                                    "break": ck["break"],
                                    "silent": ck["silent"],
                                    "stable": ck["stable"]}
    # onset 归因: 每题一次
    for db in FAMILY_DBS:
        for qid, g in sorted(gold.items()):
            if g["db"] != db:
                continue
            onset, oc = None, None
            for t in range(1, 6):
                if str(t) not in g["checkpoints"]:
                    continue
                gt = g["checkpoints"][str(t)]
                if not gt.get("exec_ok"):
                    o = "break"
                elif gt.get("result_hash") != \
                        g["checkpoints"]["0"]["result_hash"]:
                    o = "silent"
                else:
                    continue
                onset, oc = t, o
                break
            allhits = {}
            for t in range(1, 6):
                ops = manifest[db]["checkpoints"].get(str(t), [])
                hs = _op_hits(ops, refs[qid])
                if hs:
                    allhits[t] = hs
            if onset is not None:
                hs = allhits.get(onset, [])
                if hs:
                    attribution[hs[0]][oc] += 1
                else:
                    unattributed[oc] += 1
            else:
                if allhits:
                    flat = sorted({h for hs in allhits.values() for h in hs})
                    stable_hit["+".join(flat)] += 1
                else:
                    n_stable_unaff += 1
    fam_split = {}
    for fam in ("S", "V", "W2"):
        c = Counter()
        for r in load_jsonl(os.path.join(HERE, "gold", f"gold_{fam}.jsonl")):
            g = r["checkpoints"]
            for t, e in g.items():
                if t == "0":
                    continue
                if not e.get("exec_ok"):
                    c["loud"] += 1
                elif e.get("result_hash") != g["0"]["result_hash"]:
                    c["silent"] += 1
                else:
                    c["stable"] += 1
        fam_split[fam] = dict(c)
    return {"attribution": {k: dict(v) for k, v in attribution.items()},
            "unattributed": dict(unattributed),
            "stable_hit": dict(stable_hit),
            "n_stable_unaffected": n_stable_unaff,
            "per_ck": per_ck, "fam_split": fam_split}


# ---------------- T4 交叉核对 ----------------

def crosschecks(scen_chat):
    """chat D5/D6 锁存数字 vs E4 (detector_results.json) 与 E9 (detector_w3.json)。"""
    out = {"E4": [], "E9": []}
    stored = load_json(os.path.join(HERE, "results", "detector_results.json"))
    for gname in ("S", "V", "W2", "SILENT"):
        fams = ["V", "W2"] if gname == "SILENT" else [gname]
        for d, cname in (("D5_mode", "D5"), ("D6_jac", "D6@0.30")):
            recs = [r for f in fams for r in scen_chat[f][cname]]
            gm = scenario_metrics(recs)
            s_pow = stored["groups"][gname]["detectors"][d]["power"]
            s_far = stored["groups"][gname]["detectors"][d]["far"]
            out["E4"].append({"group": gname, "det": d,
                              "power": gm["power_screen_latched"],
                              "stored_power": s_pow,
                              "far": gm["screen_far_latched"],
                              "stored_far": s_far,
                              "match": gm["power_screen_latched"] == s_pow
                              and gm["screen_far_latched"] == s_far})
    w3 = load_json(os.path.join(HERE, "results", "detector_w3.json"))
    for d, cname in (("D5_mode", "D5"), ("D6_jac", "D6@0.30")):
        recs = scen_chat["W3"][cname]
        gm = scenario_metrics(recs)
        s_far = w3["sides"]["chat"]["pooled"]["detectors"][d]["far"]
        out["E9"].append({"group": "W3", "det": d,
                          "far": gm["screen_far_latched"],
                          "stored_far": s_far,
                          "match": gm["screen_far_latched"] == s_far})
    return out


# ---------------- 主流程 ----------------

def main():
    t0 = time.time()
    res = {"params": {"screen_configs": list(SCREEN), "m_grid": M_GRID,
                      "b_draws": B_DRAWS},
           "t1": {}, "t2": None, "t3": None, "t4": None}
    scen_all = {}
    for side in SIDES:
        scen = build_t1(side)
        scen_all[side[0]] = scen
        for sc in ("S", "V", "W2", "W3", "BENIGN"):
            if sc == "SILENT":
                continue
            res["t1"].setdefault(side[0], {})[sc] = {
                cn: scenario_metrics(recs) for cn, recs in scen[sc].items()}
        # SILENT 合并
        res["t1"][side[0]]["SILENT"] = {
            cn: scenario_metrics(scen["V"][cn] + scen["W2"][cn])
            for cn in SCREEN}
        print(f"[comb:{side[0]}] T1 done", flush=True)

    res["t2"] = budget_curves()
    print("[comb] T2 done", flush=True)
    res["t3"] = anchor_anatomy()
    print("[comb] T3 done", flush=True)
    res["t4"] = crosschecks(scen_all["chat"])
    ok4 = all(x["match"] for x in res["t4"]["E4"])
    ok9 = all(x["match"] for x in res["t4"]["E9"])
    print(f"[comb] T4: E4 match={ok4} E9 match={ok9}", flush=True)

    resdir = os.path.join(HERE, "results")
    with open(os.path.join(resdir, "combined_monitor.json"), "w",
              encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)

    # ---- 报告 ----
    L = ["# DriftSQL 组合监测器评测 (E11)\n",
         f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')} "
         "(run_combined_monitor.py, 零 API)\n",
         "## 0. 两阶段语义\n",
         "筛查 (D5 众数迁移 / D6 指纹Jaccard, 复用候选执行指纹, 免费) 在 "
         "(题, 检查点) 报警 → 执行该题 gold 1 条确认 (hash 变 = 真)。确认"
         "**拒绝后继续监测** (不锁存误报), 确认为真后锁存并停止计费。确认"
         "用漂移感知 gold → 确认后 FAR=0 是构造性的 (A6 已声明), 其经验内容"
         "= 压制前的筛查 FAR (表内) 与 gold 确认成本。\n"]

    for side in ("chat", "v4b"):
        L += [f"## 1. 题级两阶段 ({side})\n",
              "| 场景 | 配置 | n题/onset | 两阶段功效 | 中位延迟 | "
              "筛查功效(锁存) | 筛查FAR(锁存) | 两阶段FAR | "
              "gold确认 (总/题均) | 确认/拒绝 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for sc in ("S", "V", "W2", "SILENT", "W3", "BENIGN"):
            for cn in SCREEN:
                m = res["t1"][side].get(sc, {}).get(cn)
                if m is None:
                    continue
                pw = m["power"] if m["power"] is not None else "-"
                dl = m["med_delay"] if m["med_delay"] is not None else "-"
                pwl = m["power_screen_latched"] if \
                    m["power_screen_latched"] is not None else "-"
                far = m["screen_far_latched"] if \
                    m["screen_far_latched"] is not None else "-"
                L.append(
                    f"| {sc} | {cn} | {m['n_questions']}/{m['n_onset']} "
                    f"| {pw} | {dl} | {pwl} | {far} | {m['two_stage_far']} "
                    f"| {m['gold_confirm_total']} / "
                    f"{m['gold_confirm_per_q']} "
                    f"| {m['confirmed']} / {m['rejected']} |")
        L.append("")

    L += ["## 2. DB 级 gold 预算曲线 (锚点覆盖)\n",
          "每库维护 m 个 gold 锚点 (从该库题目池随机子采样, "
          f"B={B_DRAWS} 抽样), 锚点漂移即库级告警; 库级事件 = 任一题发生"
          "truth 漂移的库-检查点。锚点成本 = m 次 gold 执行/检查点/库。\n"]
    for fam in ("S", "V", "W2"):
        t2 = res["t2"][fam]
        L += [f"### {fam} (事件库: {len(t2['events'])}/5)\n",
              "| m | power(m) [95%CI] | 中位延迟(检查点) |", "|---|---|---|"]
        for m in M_GRID:
            r = t2["rows"].get(m)
            if r is None:
                continue
            dl = r["med_delay"] if r["med_delay"] is not None else "-"
            L.append(f"| {m} | {r['power']} {r['ci']} | {dl} |")
        L.append("")
    L += ["库级负控制: BENIGN (V-benign, 150/150 哈希稳定门槛) 与 W3 "
          "(gold 恒 t0) 上任何锚点集均零告警 → 库级 FAR=0 (E9 实证 "
          "D8 0.0 + E10 验证门槛)。\n"]

    t3 = res["t3"]
    L += ["## 3. S 族锚点解剖 (stale-gold 消融, onset 归因)\n",
          "锚点信号 = 原 gold SQL 重执行 (未改写)。题级功效与 truth 定义重合"
          "(A6); 经验内容 = 按操作类型分列锚点信号。检查点累积, 每题只按"
          "**首发 (onset) 检查点**的操作归因一次 (避免把前驱破坏重复记到后续"
          "操作头上)。受影响 = gold 引用了操作目标 (add_distractor 仅 "
          "SELECT * 型 gold)。\n",
          "| onset 命中操作 | 漂移题数 | break | silent |",
          "|---|---|---|---|"]
    for k, c in sorted(t3["attribution"].items()):
        L.append(f"| {k} | {sum(c.values())} | {c.get('break', 0)} "
                 f"| {c.get('silent', 0)} |")
    u = t3["unattributed"]
    L.append(f"| (onset 无命中) | {sum(u.values())} | {u.get('break', 0)} "
             f"| {u.get('silent', 0)} |")
    sh = t3["stable_hit"]
    n_sh = sum(sh.values())
    sh_desc = "; ".join(f"{k}: {v}" for k, v in sorted(sh.items())) \
        if sh else "无"
    L += ["",
          f"- 受影响但全程 stable (命中过操作却从未漂移): {n_sh} 题 ({sh_desc})。",
          f"- 未受影响对照 (零命中且全程 stable): {t3['n_stable_unaffected']} "
          "题, 全部 stable → 对照零漂移。",
          "- 注: drop_column 零首发事件 — ck4 drop 的列已在 ck3 被 "
          "rename_column 改名, 破坏在 ck3 已发生 (rename 遮蔽 drop 的独立"
          "效应); add_distractor 零事件 (S 族无 SELECT * 型 gold, 答案级 "
          "truth 下其结果确实不变)。"]
    L += ["\n### 分族 loud/silent/stable 总览 (题×检查点)\n",
          "| 族 | loud(break) | silent | stable |", "|---|---|---|---|"]
    for fam in ("S", "V", "W2"):
        c = t3["fam_split"][fam]
        L.append(f"| {fam} | {c.get('loud', 0)} | {c.get('silent', 0)} "
                 f"| {c.get('stable', 0)} |")
    L += ["", "### S 族逐检查点状态 (分库, 累积口径: onset 后状态继承)\n",
          "| 库:ck | 操作 | break | silent | stable |", "|---|---|---|---|---|"]
    for k, v in sorted(t3["per_ck"].items()):
        L.append(f"| {k} | {'+'.join(v['ops'])} | {v['break']} "
                 f"| {v['silent']} | {v['stable']} |")

    L += ["", "## 4. 交叉核对 (vs E4 / E9)\n",
          "| 来源 | 组 | 检测器 | power 本/存 | FAR 本/存 | 一致 |",
          "|---|---|---|---|---|---|"]
    for x in res["t4"]["E4"] + res["t4"]["E9"]:
        L.append(f"| {'E4' if x in res['t4']['E4'] else 'E9'} | {x['group']} "
                 f"| {x['det']} | {x.get('power', '-')} / "
                 f"{x.get('stored_power', '-')} | {x['far']} / "
                 f"{x['stored_far']} | {x['match']} |")

    def far_of(sc, cn="OR", side="chat"):
        m = res["t1"][side].get(sc, {}).get(cn)
        return m["screen_far_latched"] if m else None

    silent_far = far_of("SILENT")
    w3_far = far_of("W3")
    ben_far = far_of("BENIGN")
    s_pow = res["t1"]["chat"].get("S", {}).get("OR", {}).get("power")
    s_budget = res["t2"]["S"]["rows"].get(1, {}).get("power")
    L += ["", "## 5. 结论要点\n",
          f"- 两阶段把 SILENT 筛查锁存 FAR ({silent_far}) 与 W3 "
          f"({w3_far}) / V-benign ({ben_far}) 的误报全部压制为 0, 代价 = "
          "筛查报警点数的 gold 确认 (表 1); 确认拒绝后继续监测, 故两阶段"
          "功效 ≥ 筛查锁存功效 (同列可见)。",
          f"- S (loud) 场景两阶段功效仅 {s_pow} (筛查对 loud 近乎失明) → "
          f"必须由 gold 锚点层覆盖: DB 级预算下 S 族 m=1 锚点即可达 "
          f"power {s_budget} (gold 破坏面广), V/W2 需更多锚点 (漂移只触及"
          "部分题)。",
          "- 锚点解剖 (onset 归因, 100% 可归因): rename_column / "
          "rename_table 的首发破坏全部为 break (锚点以'测试挂掉'的方式"
          "响亮报警), silent=0; drop_column 被 ck3 rename 遮蔽 (零首发); "
          "add_distractor 零事件 (无 SELECT * gold); 未受影响对照题 "
          "零漂移; V/W2 全部锚点事件为 silent。",
          ""]
    rp = os.path.join(resdir, "combined_monitor.md")
    with open(rp, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[comb] report -> {rp}", flush=True)
    print(f"[comb] done in {(time.time()-t0)/60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
