# -*- coding: utf-8 -*-
"""run_p1_analysis.py — P1 零API重分析: 锚点失效敏感性 / 漏检刻画 / W2-only 功效。

项6 锚点失效敏感性: 向 G1 两阶段确认注入 k∈{1,2,3} 个过期锚点。
  模型 (最坏情形, 对应论文 Threats 的 "stale fingerprint" 双向威胁): 触碰过期
  锚点的确认 verdict 翻转 — 良性检查点上的报警被误确认为漂移 (操作员可见假
  告警, 锁存并停止监测), 真检查点上的报警被误拒绝 (继续监测; SILENT 族 truth
  累积, 后续报警仍为真 → 全被拒绝 → 漏检)。筛查报警序列不受影响 (过期只作用
  于确认层), 故逐题报警集只算一次、按抽取的过期集合重放确认语义。
  过期锚点从全部受监测题 (SILENT 300 + W3 141 + BENIGN 150) 均匀抽取,
  R=200 次。指标: 操作员可见假告警数 vs 上界 p×良性确认数 (p=k/N, 良性确认
  数 = 基线在 truth=False 检查点上的确认总数), SILENT 两阶段功效, 两阶段 FAR
  (锁存覆盖口径, 与 E11 screen_far_latched 同分母)。
项7 漏检刻画: G1 SILENT OR 两阶段 78/90 的 12 漏检, 按族/onset/报警史/池变化
  幅度 (post-onset 最大指纹 Jaccard 距离、非众数候选占比) 刻画, 与 78 命中对
  照; 并求恢复阈值 θ = min_漏检 (post-onset 最大 jac), 重放该阈值下 SILENT
  功效/FAR/gold 成本 (若某漏检 post-onset jac 恒 0 则指纹阈值无法追回, 如实
  报告)。
项8 W2-only: 直接读 combined_monitor.json 并与重算对账。
零 API: 只读 gen/gold/benign_control 缓存。输出 results/p1_analysis.{json,md}
"""
from __future__ import annotations

import json
import os
import random
import time
from collections import defaultdict

from run_combined_monitor import (HERE, TMAX, FAMILY_DBS, TH_JAC,
                                  gold_map, jac_dist, load_json,
                                  load_series, load_series_w3,
                                  scenario_metrics)

R_DRAWS = 200
K_GRID = (1, 2, 3)


def or_alarm(s0, st, th=TH_JAC):
    return st["mode"] != s0["mode"] or jac_dist(s0["cls"], st["cls"]) >= th


# ---------------- 逐题序列 + OR 报警集 (G1 chat) ----------------

def load_chat_questions():
    """{(fam,qid): {qid,fam,db,ts,truth,alarms,s0,ser}} — SILENT(V,W2)+W3+BENIGN。

    复刻 build_t1 的加载/过滤逻辑 (gold 命中且 t0 有效且 0∈ser), 报警集 = OR
    (D5 众数迁移 ∪ D6@0.30 指纹Jaccard)。键用 (fam,qid): V/W2 的裸 qid 空间
    重叠。BENIGN 序列来自 E10 缓存。
    """
    out = {}
    for fam in ("V", "W2"):
        gold = gold_map(fam)
        for db in FAMILY_DBS:
            if not os.path.isdir(os.path.join(HERE, "gen", fam, db)):
                continue
            per = load_series("gen", fam, db, TMAX[fam])
            for qid in sorted(per):
                if qid not in gold:
                    continue
                truth = gold[qid]["truth"]
                ser = {t: per[qid][t] for t in per[qid] if t in truth or t == 0}
                if 0 not in ser:
                    continue
                ts = sorted(t for t in ser if t >= 1)
                s0 = ser[0]
                out[(fam, qid)] = {"qid": qid, "fam": fam, "db": db,
                                   "ts": ts,
                                   "truth": [truth[t] for t in ts],
                                   "alarms": [or_alarm(s0, ser[t]) for t in ts],
                                   "s0": s0, "ser": ser}
    gold = gold_map("W3")
    for db in FAMILY_DBS:
        if not os.path.isdir(os.path.join(HERE, "gen_w3", "W3", db)):
            continue
        per = load_series_w3("gen_w3", db)
        for qid in sorted(per):
            if qid not in gold:
                continue
            truth = gold[qid]["truth"]
            ser = {t: per[qid][t] for t in per[qid] if t in truth or t == 0}
            if 0 not in ser:
                continue
            ts = sorted(t for t in ser if t >= 1)
            s0 = ser[0]
            out[("W3", qid)] = {"qid": qid, "fam": "W3", "db": db,
                                "ts": ts,
                                "truth": [truth[t] for t in ts],
                                "alarms": [or_alarm(s0, ser[t]) for t in ts],
                                "s0": s0, "ser": ser}
    bc = load_json(os.path.join(HERE, "results", "benign_control.json"))
    for qid, ser in bc["sides"]["chat"]["series"].items():
        ser = {int(k): v for k, v in ser.items()}
        if 0 not in ser:
            continue
        ts = sorted(t for t in ser if t >= 1)
        s0 = ser[0]
        out[("BENIGN", qid)] = {"qid": qid, "fam": "BENIGN", "db": None,
                                "ts": ts,
                                "truth": [False] * len(ts),
                                "alarms": [or_alarm(s0, ser[t]) for t in ts],
                                "s0": s0, "ser": ser}
    return out


def replay(q, stale=False):
    """单题两阶段重放。stale=False 复现基线; True 时确认 verdict 翻转。

    返回 {onset, detected, cost, rejected, latched, false_c, cover}:
    false_c = 良性报警被误确认次数 (操作员可见假告警); cover = 假告警锁存后
    覆盖的稳定检查点数 (FAR 分子, 与 E11 锁存口径一致)。
    """
    ts, truth, alarms = q["ts"], q["truth"], q["alarms"]
    onset = next((t for t, f in zip(ts, truth) if f), None)
    lim = onset if onset is not None else 10 ** 9
    detected, cost, rej, lat = None, 0, 0, None
    false_c, cover, stop = 0, 0, False
    for t, a, f in zip(ts, alarms, truth):
        if stop or detected is not None:
            break
        if not a:
            continue
        if lat is None:
            lat = t
        cost += 1
        if f:                      # 真检查点: 基线确认; 过期 → 误拒绝
            if stale:
                rej += 1
            else:
                detected = t
        else:                      # 良性检查点: 基线拒绝继续; 过期 → 误确认锁存
            if stale:
                false_c += 1
                cover += sum(1 for u in ts if u >= t and u < lim)
                stop = True
            else:
                rej += 1
    return {"onset": onset, "detected": detected, "cost": cost,
            "rejected": rej, "latched": lat, "false_c": false_c,
            "cover": cover}


def recs_of(Q, qids, stale_ids=frozenset()):
    return [dict(replay(Q[q], q in stale_ids), **{
        "ts": Q[q]["ts"], "truth": Q[q]["truth"]}) for q in qids]


def qids_of_fam(Q, fams):
    return [q for q in sorted(Q, key=str) if Q[q]["fam"] in fams]


# ---------------- 主流程 ----------------

def main():
    t0 = time.time()
    stored = load_json(os.path.join(HERE, "results", "combined_monitor.json"))
    Q = load_chat_questions()
    sq = qids_of_fam(Q, ("V", "W2"))
    wq = qids_of_fam(Q, ("W3", "BENIGN"))
    print(f"[p1] questions: SILENT={len(sq)} W3+BENIGN={len(wq)} "
          f"total={len(Q)}", flush=True)

    # ---- 基线重放 + 与存档对账 ----
    xchk = {}
    base_sil = recs_of(Q, sq)
    gm_sil = scenario_metrics(base_sil)
    so = stored["t1"]["chat"]["SILENT"]["OR"]
    xchk["SILENT"] = {
        "n_questions": [gm_sil["n_questions"], so["n_questions"]],
        "n_onset": [gm_sil["n_onset"], so["n_onset"]],
        "power": [gm_sil["power"], so["power"]],
        "screen_far_latched": [gm_sil["screen_far_latched"],
                               so["screen_far_latched"]],
        "confirmed": [gm_sil["confirmed"], so["confirmed"]],
        "rejected": [gm_sil["rejected"], so["rejected"]],
        "gold_confirm_total": [gm_sil["gold_confirm_total"],
                               so["gold_confirm_total"]],
        "n_stable_points": [gm_sil["n_stable_points"],
                            so["n_stable_points"]],
    }
    base_w3b = recs_of(Q, wq)
    for fam in ("W3", "BENIGN"):
        ids = qids_of_fam(Q, (fam,))
        gm = scenario_metrics(recs_of(Q, ids))
        sof = stored["t1"]["chat"][fam]["OR"]
        xchk[fam] = {
            "n_questions": [gm["n_questions"], sof["n_questions"]],
            "power": [gm["power"], sof["power"]],
            "screen_far_latched": [gm["screen_far_latched"],
                                   sof["screen_far_latched"]],
            "rejected": [gm["rejected"], sof["rejected"]],
            "n_stable_points": [gm["n_stable_points"],
                                sof["n_stable_points"]],
        }
    ok = all(a == b for d in xchk.values() for a, b in d.values())
    print(f"[p1] baseline replay vs stored: match={ok}", flush=True)
    if not ok:
        for sc, d in xchk.items():
            for kk, (a, b) in d.items():
                if a != b:
                    print(f"  MISMATCH {sc}.{kk}: replay={a} stored={b}")
        raise SystemExit("baseline replay mismatch — abort (不写任何论文数字)")

    base_m_sil = gm_sil
    benign_conf = base_m_sil["rejected"] + sum(r["rejected"]
                                               for r in base_w3b)
    print(f"[p1] benign confirmations (baseline) = {benign_conf}", flush=True)

    # ---- 项8: 分族两阶段功效 (OR, 存档 vs 重算) ----
    item8 = {}
    for fam in ("V", "W2"):
        ids = qids_of_fam(Q, (fam,))
        gm = scenario_metrics(recs_of(Q, ids))
        sof = stored["t1"]["chat"][fam]["OR"]
        item8[fam] = {"power_replay": gm["power"],
                      "power_stored": sof["power"],
                      "confirmed": gm["confirmed"],
                      "n_onset": gm["n_onset"],
                      "gold_per_q": gm["gold_confirm_per_q"]}
    item8["SILENT"] = {"power": base_m_sil["power"],
                       "confirmed": base_m_sil["confirmed"],
                       "n_onset": base_m_sil["n_onset"],
                       "gold_per_q": base_m_sil["gold_confirm_per_q"]}

    # ---- 项7: 漏检刻画 ----
    miss_q, hit_q = [], []
    for qid in sq:
        r = replay(Q[qid])
        if r["onset"] is None:
            continue
        (miss_q if r["detected"] is None else hit_q).append(qid)
    assert len(miss_q) + len(hit_q) == base_m_sil["n_onset"], "onset 集不一致"
    profs = {"miss": [], "hit": []}
    for tag, ids in (("miss", miss_q), ("hit", hit_q)):
        for qid in ids:
            q = Q[qid]
            onset = next(t for t, f in zip(q["ts"], q["truth"]) if f)
            post = [t for t in q["ts"] if t >= onset]
            pre_alarms = sum(1 for t, a in zip(q["ts"], q["alarms"])
                             if a and t < onset)
            jacs = [jac_dist(q["s0"]["cls"], q["ser"][t]["cls"])
                    for t in post]
            chgs = [1 - q["ser"][t]["cls"].get(q["s0"]["mode"], 0)
                    / q["ser"][t]["n"] for t in post]
            profs[tag].append({
                "qid": str(q["qid"]), "fam": q["fam"], "db": q["db"],
                "onset": onset,
                "pre_onset_alarms": pre_alarms,
                "max_post_jac": round(max(jacs), 4) if jacs else None,
                "max_post_nonmode_frac": round(max(chgs), 4) if chgs else None,
                "n_cand_t0": q["s0"]["n"]})

    def med(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2] if xs else None

    fam_c = defaultdict(int)
    for p in profs["miss"]:
        fam_c[p["fam"]] += 1
    maxj_miss = [p["max_post_jac"] for p in profs["miss"]]
    maxj_hit = [p["max_post_jac"] for p in profs["hit"]]
    recovery = None
    recovable_j = [j for j in maxj_miss if j and j > 0]
    if recovable_j:
        th_exact = min(recovable_j)
        th_grid = max(int(th_exact * 100) / 100.0, 0.01)

        def th_q(key):
            q = Q[key]
            al = [or_alarm(q["s0"], q["ser"][t], th_grid) for t in q["ts"]]
            return dict(replay(dict(q, alarms=al)),
                        **{"ts": q["ts"], "truth": q["truth"]})

        sil_th = [th_q(k) for k in sq]
        w3b_th = [th_q(k) for k in wq]
        gm_s = scenario_metrics(sil_th)
        gm_w = scenario_metrics(w3b_th)
        det_now = {k for k, r in zip(sq, sil_th)
                   if r["onset"] is not None and r["detected"] is not None}
        recovery = {"theta": th_grid, "theta_exact": th_exact,
                    "power": gm_s["power"],
                    "detected": gm_s["confirmed"],
                    "n_miss_recovered": sum(1 for k in miss_q
                                            if k in det_now),
                    "n_miss_total": len(miss_q),
                    "screen_far_latched": gm_s["screen_far_latched"],
                    "gold_per_q": gm_s["gold_confirm_per_q"],
                    "w3b_far": gm_w["screen_far_latched"]}
    item7 = {"n_miss": len(profs["miss"]), "miss_by_family": dict(fam_c),
             "miss_profiles": profs["miss"],
             "hit_median_max_post_jac": med(maxj_hit),
             "hit_max_max_post_jac": max(maxj_hit) if maxj_hit else None,
             "miss_median_max_post_jac": med(maxj_miss),
             "miss_max_max_post_jac": max(maxj_miss) if maxj_miss else None,
             "miss_with_pre_onset_alarms": sum(
                 1 for p in profs["miss"] if p["pre_onset_alarms"] > 0),
             "recovery": recovery}

    # ---- 项6: 锚点失效注入 ----
    pool = sorted(Q, key=str)
    n_pool = len(pool)
    n_onset = base_m_sil["n_onset"]
    n_stable_sil = base_m_sil["n_stable_points"]
    n_stable_w3b = sum(1 for r in base_w3b
                       for t, f in zip(r["ts"], r["truth"]) if not f)
    staleness = {"n_pool": n_pool, "benign_confirmations": benign_conf,
                 "r_draws": R_DRAWS, "k_grid": list(K_GRID), "rows": []}
    for k in K_GRID:
        p = k / n_pool
        bound = p * benign_conf
        fas, pows, far_s, far_w = [], [], [], []
        for d in range(R_DRAWS):
            rng = random.Random(f"p1:stale:{k}:{d}")
            stale = set(rng.sample(pool, k))
            n_fa = det_sil = cov_s = cov_w = 0
            for qid in pool:
                q = Q[qid]
                r = replay(q, qid in stale)
                n_fa += r["false_c"]
                if q["fam"] in ("V", "W2"):
                    cov_s += r["cover"]
                    if r["onset"] is not None and r["detected"] is not None:
                        det_sil += 1
                else:
                    cov_w += r["cover"]
            fas.append(n_fa)
            pows.append(det_sil / n_onset)
            far_s.append(cov_s / n_stable_sil)
            far_w.append(cov_w / n_stable_w3b)
        staleness["rows"].append({
            "k": k, "p": round(p, 5), "bound_false_alarms": round(bound, 2),
            "false_alarms_mean": round(sum(fas) / len(fas), 3),
            "false_alarms_max": max(fas),
            "false_alarms_dist": {str(v): fas.count(v) for v in set(fas)},
            "silent_power_mean": round(sum(pows) / len(pows), 4),
            "silent_power_min": min(pows),
            "silent_power_baseline": base_m_sil["power"],
            "far_silent_mean": round(sum(far_s) / len(far_s), 4),
            "far_silent_max": max(far_s),
            "far_w3b_mean": round(sum(far_w) / len(far_w), 4),
            "far_w3b_max": max(far_w)})
        print(f"[p1] stale k={k}: FA mean="
              f"{staleness['rows'][-1]['false_alarms_mean']} "
              f"bound={bound:.2f} "
              f"power={staleness['rows'][-1]['silent_power_mean']}",
              flush=True)

    out = {"params": {"r_draws": R_DRAWS, "or_threshold": TH_JAC,
                      "model": "stale confirmation verdict flip (worst case)"},
           "crosscheck": xchk, "item8_w2_only": item8,
           "item7_misses": item7, "item6_staleness": staleness}
    resdir = os.path.join(HERE, "results")
    with open(os.path.join(resdir, "p1_analysis.json"), "w",
              encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    # ---- MD 报告 ----
    L = ["# P1 零API重分析 (锚点失效敏感性 / 漏检刻画 / W2-only)\n",
         f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')} "
         "(run_p1_analysis.py, 零 API)\n",
         "## 0. 基线对账 (重放 vs combined_monitor.json, OR 配置)\n",
         "| 组 | 字段: 重放/存档 | 一致 |", "|---|---|---|"]
    for sc, d in xchk.items():
        for kk, (a, b) in d.items():
            L.append(f"| {sc} | {kk}: {a} / {b} | {a == b} |")
    L += ["", f"良性确认数 (基线 truth=False 检查点上的确认, 上界分母) = "
          f"{benign_conf} (SILENT {base_m_sil['rejected']} + "
          f"W3+BENIGN {sum(r['rejected'] for r in base_w3b)})\n"]

    L += ["## 1. 项8: 分族两阶段功效 (OR, G1)\n",
          "| 族 | 功效 (重放/存档) | 确认/onset | gold/题 |", "|---|---|---|---|"]
    for fam in ("V", "W2"):
        r = item8[fam]
        L.append(f"| {fam} | {r['power_replay']} / {r['power_stored']} "
                 f"| {r['confirmed']}/{r['n_onset']} | {r['gold_per_q']} |")
    r = item8["SILENT"]
    L.append(f"| SILENT | {r['power']} | {r['confirmed']}/{r['n_onset']} "
             f"| {r['gold_per_q']} |")

    L += ["", "## 2. 项7: SILENT OR 两阶段漏检刻画 (12/90)\n",
          f"漏检分族: {dict(fam_c)}; 有 onset 前报警史 (筛查见过该题): "
          f"{item7['miss_with_pre_onset_alarms']}/12\n",
          "post-onset 池变化幅度 (指纹 Jaccard 距离):",
          f"- 命中 78 题: max-jac 中位 {item7['hit_median_max_post_jac']}, "
          f"最大 {item7['hit_max_max_post_jac']}",
          f"- 漏检 12 题: max-jac 中位 {item7['miss_median_max_post_jac']}, "
          f"最大 {item7['miss_max_max_post_jac']}\n",
          "逐题明细 (漏检):", "| qid | 族 | db | onset | onset前报警 | "
          "max post-jac | max 非众数占比 | t0 候选数 |",
          "|---|---|---|---|---|---|---|---|"]
    for p in profs["miss"]:
        L.append(f"| {p['qid']} | {p['fam']} | {p['db']} | {p['onset']} "
                 f"| {p['pre_onset_alarms']} | {p['max_post_jac']} "
                 f"| {p['max_post_nonmode_frac']} | {p['n_cand_t0']} |")
    if recovery:
        L += ["", f"恢复实验: θ = {recovery['theta']} (可恢复漏检的最小 "
              f"post-onset max-jac, 精确 θ* = {recovery['theta_exact']}) "
              f"→ 追回 {recovery['n_miss_recovered']}/"
              f"{recovery['n_miss_total']} 漏检 (jac=0 的漏检原理上不可追回), "
              f"功效 {recovery['power']} (确认 {recovery['detected']}/90), "
              f"代价: SILENT 锁存筛查 FAR {recovery['screen_far_latched']}, "
              f"W3+BENIGN FAR {recovery['w3b_far']}, gold/题 "
              f"{recovery['gold_per_q']}。"]
    else:
        L += ["", "存在 post-onset jac 恒 0 的漏检 (指纹分布完全不变), "
              "D6 阈值无法追回 — 见 JSON 明细。"]

    L += ["", "## 3. 项6: 锚点失效敏感性 (k 个过期锚点, R=200)\n",
          "模型: 触碰过期锚点的确认 verdict 翻转 (良性报警→误确认假告警锁存; "
          f"真报警→误拒绝)。过期锚点从 {n_pool} 个受监测题均匀抽取。\n",
          "| k | p | 假告警均值 | 假告警最大 | 上界 p×良性确认 | "
          "SILENT 功效 (均值/最低/基线) | FAR SILENT | FAR W3+B |",
          "|---|---|---|---|---|---|---|---|"]
    for r in staleness["rows"]:
        L.append(f"| {r['k']} | {r['p']} | {r['false_alarms_mean']} "
                 f"| {r['false_alarms_max']} | {r['bound_false_alarms']} "
                 f"| {r['silent_power_mean']} / {r['silent_power_min']} / "
                 f"{r['silent_power_baseline']} | {r['far_silent_mean']} "
                 f"| {r['far_w3b_mean']} |")
    L += ["", "假告警分布 (k→ 计数): "
          + "; ".join(f"k={r['k']}: {r['false_alarms_dist']}"
                      for r in staleness["rows"])]

    rp = os.path.join(resdir, "p1_analysis.md")
    with open(rp, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[p1] report -> {rp}", flush=True)
    print(f"[p1] done in {time.time()-t0:.1f} s", flush=True)


if __name__ == "__main__":
    main()
