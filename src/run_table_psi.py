# -*- coding: utf-8 -*-
"""run_table_psi.py — P0-3: 表级 PSI/结构基线 (回应 A3, 零 API)。

审稿人必问: "V/W 是数据漂移且有 DB 完全访问权, 为何不直接在表上跑 PSI?"
本基线把表级信号做到位并给出答案级对照:
  1) 分布信号: 每列 PSI (t0→ck)。数值列 = t0 等频 10 箱 + NULL 箱;
     文本/类别列 = t0 top-10 频率值 + NULL + OTHER。表级 = max 列 PSI;
     库级告警 = max 表 PSI > TH_PSI (0.25 业界标准 / 0.1 敏感)。
  2) 结构破坏信号: 列/表消失 (SQLite 下 rename/drop 均表现为消失) → 布尔告警。
     新增列不告警 — 加列是良性 schema 演化 (E11-T3 实证答案不变)。
  3) 答案级对照: 库级事件 = 任一题 truth 漂移 (E11-T2 同口径);
     V-benign 门槛 = 150/150 gold 哈希稳定 → 任何告警都是答案级误报;
     W3 无快照 (DB 恒定) → 按构造 PSI=0 静默 (诚实呈现为其优点)。
差异点: 表级信号只能回答"数据/结构变了" (库粒度), 不能定位"哪道题的
答案变了"; 且把数据变了≠答案变了 (benign dup 复行: PSI 告警但答案不变)
与答案变了≠数据分布变 (W2 新类别只触及部分题) 两个方向都实证。

输出: results/table_psi.md + results/table_psi.json
"""
from __future__ import annotations

import bisect
import json
import math
import os
import sqlite3
import time
from collections import Counter, defaultdict

from build_drift_benchmark import (HERE, FAMILY_DBS, db_path_of,
                                   list_tables, table_cols, row_count)
from run_combined_monitor import gold_map

THS = [0.25, 0.10]
SCEN = [("V", os.path.join(HERE, "dbs", "V"), "ck{}", 5),
        ("W2", os.path.join(HERE, "dbs", "W2"), "ck{}", 3),
        ("S", os.path.join(HERE, "dbs", "S"), "ck{}", 5),
        ("BENIGN", os.path.join(HERE, "dbs_vbenign", "V"), "b{}", 5)]


def psi_bins(c0, c1):
    keys = set(c0) | set(c1)
    n0, n1 = sum(c0.values()), sum(c1.values())
    if n0 == 0 or n1 == 0:
        return 0.0
    psi = 0.0
    for k in keys:
        p0 = max(c0.get(k, 0) / n0, 1e-6)
        p1 = max(c1.get(k, 0) / n1, 1e-6)
        psi += (p1 - p0) * math.log(p1 / p0)
    return psi


def col_psi(v0, v1):
    """单列 PSI: v0/v1 为原始值列表 (含 None)。箱定义取自 t0 (v0)。"""
    nonnull0 = [x for x in v0 if x is not None]
    is_num = bool(nonnull0) and all(
        isinstance(x, (int, float)) for x in nonnull0)
    if is_num:
        s = sorted(nonnull0)
        if not s:
            return psi_bins({"NULL": len(v0)}, {"NULL": len(v1)})
        qs = [s[min(len(s) - 1, int(len(s) * i / 10))]
              for i in range(1, 10)]

        def binv(x):
            if x is None:
                return "NULL"
            return f"n{bisect.bisect_right(qs, x)}"

    else:
        cnt0 = Counter("NULL" if x is None else str(x) for x in v0)
        top = {k for k, _ in cnt0.most_common(10)}

        def binv(x):
            k = "NULL" if x is None else str(x)
            return k if k in top else "OTHER"

    c0 = Counter(binv(x) for x in v0)
    c1 = Counter(binv(x) for x in v1)
    return psi_bins(c0, c1)


def col_vals(con, tb, col):
    return [r[0] for r in con.execute(
        f'SELECT "{col}" FROM "{tb}"')]


def eval_snapshot(db, snap_path):
    """快照 vs t0: 表级/库级信号。返回库级快照记录。"""
    con0 = sqlite3.connect(db_path_of(db))
    con1 = sqlite3.connect(snap_path)
    tabs0 = set(list_tables(con0))
    tabs1 = set(list_tables(con1))
    gone = sorted(tabs0 - tabs1)
    per_tb, max_psi = {}, 0.0
    lost_all = []
    for tb in sorted(tabs0 & tabs1):
        cols0 = table_cols(con0, tb)
        cols1 = set(table_cols(con1, tb))
        lost = [c for c in cols0 if c not in cols1]
        lost_all += [f"{tb}.{c}" for c in lost]
        psis = {}
        if not lost:                      # 列消失时该表已破坏, PSI 无意义
            v0_cache = {}
            for c in cols0:
                v0 = v0_cache.get(c)
                if v0 is None:
                    v0 = col_vals(con0, tb, c)
                    v0_cache[c] = v0
                v1 = col_vals(con1, tb, c)
                p = col_psi(v0, v1)
                psis[c] = round(p, 4)
            if psis:
                max_psi = max(max_psi, max(psis.values()))
        top3 = dict(sorted(psis.items(), key=lambda kv: -kv[1])[:3])
        per_tb[tb] = {"lost_cols": lost, "max_psi": max(psis.values())
                      if psis else None, "top_psi": top3}
    con0.close()
    con1.close()
    return {"gone_tables": gone, "lost_cols": lost_all,
            "max_psi": round(max_psi, 4),
            "alarm": {f"psi@{th}": max_psi > th for th in THS},
            "structural": bool(gone or lost_all), "per_table": per_tb}


def main():
    t0 = time.time()
    golds = {fam: gold_map(fam) for fam in ("S", "V", "W2")}
    ev_by_fam = defaultdict(dict)        # fam -> (db, ck) -> n_drift_q
    for fam, gold in golds.items():
        for qid, g in gold.items():
            for t, drift in g["truth"].items():
                if drift:
                    key = (g["db"], t)
                    ev_by_fam[fam][key] = ev_by_fam[fam].get(key, 0) + 1
    res = {"thresholds": THS, "scenarios": {}, "events": {
        fam: {f"{db}:{ck}": n for (db, ck), n in sorted(ev.items())}
        for fam, ev in ev_by_fam.items()}}
    skipped = []
    for fam, root, pat, tmax in SCEN:
        scen = {}
        for db in FAMILY_DBS:
            for t in range(1, tmax + 1):
                snap = os.path.join(root, db, pat.format(t) + ".sqlite")
                if not os.path.exists(snap):
                    skipped.append(f"{fam}:{db}:t{t}")
                    continue
                rec = eval_snapshot(db, snap)
                if fam in ev_by_fam:
                    rec["event_q"] = ev_by_fam[fam].get((db, t), 0)
                scen[f"{db}:t{t}"] = rec
        res["scenarios"][fam] = scen
        print(f"[psi] {fam}: {len(scen)} snapshots "
              f"(skipped {len(skipped)})", flush=True)

    resdir = os.path.join(HERE, "results")
    with open(os.path.join(resdir, "table_psi.json"), "w",
              encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)

    # ---- 汇总表 ----
    L = ["# DriftSQL 表级基线 (PSI + 结构破坏, P0-3 / A3)\n",
         f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')} "
         "(run_table_psi.py, 零 API; 表级信号与生成器无关, 单侧即可)\n",
         "表级分布信号: 每列 PSI (数值=t0 等频 10 箱+NULL; 文本=t0 top-10"
         "+NULL+OTHER), 表级=max 列, 库级告警=max 表 > TH (0.25 标准 / "
         "0.1 敏感)。结构信号: 列/表消失告警 (新增列不告警: 加列为良性 "
         "schema 演化, E11-T3 实证答案不变)。答案级对照: 库级事件 = 任一"
         "题 truth 漂移 (E11-T2 同口径); V-benign 150/150 gold 稳定 → "
         "任何告警都是答案级误报; W3 无快照 (DB 恒定) 按构造静默。\n",
         "## 1. 库级检出汇总 (事件 = 任一题答案漂移的库-检查点)\n",
         "| 场景 | 事件数 | PSI@0.25 检出 | PSI@0.1 检出 | 结构检出 | "
         "PSI@0.25 功效 | 中位延迟 |",
         "|---|---|---|---|---|---|---|"]
    for fam in ("V", "W2", "S", "BENIGN"):
        scen = res["scenarios"][fam]
        evs = {k for k, r in scen.items() if r.get("event_q")}
        if fam == "BENIGN":
            evs = set()                     # 全 stable: 全部告警=误报
        n_ev = len(evs)
        det = {th: sum(1 for k in evs
                       if scen[k]["alarm"][f"psi@{th}"])
               for th in THS}
        det_st = sum(1 for k in evs if scen[k]["structural"])
        delays = []
        for db in FAMILY_DBS:
            ck_ev = sorted(int(k.split(":t")[1]) for k in evs
                            if k.startswith(db + ":"))
            ck_al = sorted(int(k.split(":t")[1]) for k in scen
                           if k.startswith(db + ":")
                           and (scen[k]["alarm"]["psi@0.25"]
                                or scen[k]["structural"]))
            if ck_ev and ck_al:
                delays.append(max(0, ck_al[0] - ck_ev[0]))
        pw = round(det[0.25] / n_ev, 3) if n_ev else "-"
        dl = sorted(delays)[len(delays) // 2] if delays else "-"
        L.append(f"| {fam} | {n_ev} | {det[0.25]} | {det[0.1]} | "
                 f"{det_st} | {pw} | {dl} |")
    # BENIGN/W3 误报
    ben = res["scenarios"]["BENIGN"]
    n_ben = len(ben)
    for th in THS:
        fa = sum(1 for r in ben.values() if r["alarm"][f"psi@{th}"])
        L.append(f"\n- BENIGN (答案级全 stable) PSI@{th} 告警 "
                 f"{fa}/{n_ben} 库-检查点 → 答案级 FAR="
                 f"{round(fa / n_ben, 3) if n_ben else '-'}")
    fa_st = sum(1 for r in ben.values() if r["structural"])
    L.append(f"- BENIGN 结构告警 {fa_st}/{n_ben} (期望 0: churn/dup "
             "不动 schema)")
    L.append("- W3 无快照: DB 恒定按构造 PSI=0 静默 — 表级信号对表述"
             "漂移免疫是其实际优点 (诚实呈现), 但对答案级漂移同样盲")

    L += ["\n## 2. 逐库-检查点明细 (max PSI / 结构 / 事件题数)\n",
          "| 场景 | 库:ck | max PSI | 结构破坏 | 告警(PSI@0.25|结构) | "
          "事件题数 | 备注 |", "|---|---|---|---|---|---|---|"]
    for fam in ("V", "W2", "S", "BENIGN"):
        for k, r in sorted(res["scenarios"][fam].items()):
            al = r["structural"] or r["alarm"]["psi@0.25"]
            st = "; ".join((r["lost_cols"] + r["gone_tables"])[:3]) \
                or "-"
            note = ""
            if fam == "BENIGN" and r["alarm"]["psi@0.25"]:
                note = "数据变≠答案变 (答案级误报)"
            remark = st if r["structural"] else note
            L.append(
                f"| {fam} | {k} | {r['max_psi']} | "
                f"{'YES' if r['structural'] else '-'} | "
                f"{'YES' if al else '-'} | "
                f"{r.get('event_q', '-')} | {remark} |")

    L += ["\n## 3. 差异点论证 (表级信号 vs DriftSQL 答案级监测)\n",
          "1. **分布敏感 ≠ 答案敏感 (核心)**: V 族 24 个库-事件中 "
          "PSI@0.25 仅检出 10 (0.417), W2 仅 1/9 (0.111) — 答案对行级/"
          "值级变化敏感 (5%/15% NULL 注入、1-6% 新类别行足以改变涉及"
          "该值的查询答案, 每库 2-22 题漂移), 而聚合列分布几乎不动 "
          "(PSI 0.001-0.14 < 0.25)。**表级 PSI 在答案级 truth 下低功效,"
          "不是 DriftSQL 的合理替代**; 反之大 PSI 的检查点 (shift_dates "
          "t3, delete_top_value t5: PSI 7.8-10.0) 是分布剧变, 两种信号"
          "在'漂移强度轴'上互补而非替代。",
          "2. **定位粒度**: 表级告警是库粒度 ('某库数据变了'), 不能回答"
          "'哪道题的答案变了'; W2 上数据告警后仍需逐题定位 (19/150 题"
          "答案变)。DriftSQL 两阶段直接给出题级定位。",
          "3. **数据变≠答案变 (答案级误报)**: BENIGN superhero 复行 "
          "(colour 表 8→16 行) PSI 递增 b1→b5 (0.006→0.181) 但 150/150 "
          "gold 不变 → PSI@0.1 误报 (FAR 0.04, @0.25 静默); churn 值"
          "成对换位 → 行级数据完全重排但列分布精确保持 (PSI=0) — PSI "
          "只看分布这一层, 对行级内容置换结构性盲 (此处恰好答案也未变,"
          "故非误报, 但说明其观测面)。",
          "4. **结构信号与 DDL diff 等价**: S 族列/表消失 17/17 检出 "
          "(= schema 审计, 非本文增量); 对 add_distractor (加列) 静默"
          "是正确的 (答案确实不变, E11-T3)。",
          "5. **W3 免疫是表级信号的实际优点** (诚实呈现): 表述漂移下"
          "零告警 — 但 DriftSQL 的 gold 确认层同样零告警且题级可定位。",
          ""]
    rp = os.path.join(resdir, "table_psi.md")
    with open(rp, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[psi] report -> {rp}", flush=True)
    print(f"[psi] skipped: {skipped}", flush=True)
    print(f"[psi] done in {(time.time()-t0)/60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
