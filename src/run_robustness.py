# -*- coding: utf-8 -*-
"""run_robustness.py — 检测器基线稳健性: bootstrap 95% CI + 超参敏感性。

全部基于已存信号 (gen / gold / judge) 本地重算, 零 API 成本。
  A. Bootstrap CI: 按题有放回重采样 B=1000 (百分位法), 功效/FAR 的 95% CI
  B. 阈值敏感性: 扫描各检测器阈值, 分 S(loud) 与 SILENT(V+W2) 报告功效/FAR
     (单参数扫描, 其余参数固定为默认值)
输出: results/robustness_report.md + results/robustness.json
"""
from __future__ import annotations

import json
import os
import random

import run_detectors as RD
from run_detectors import DETS, DETN, HERE

B_BOOT = 1000
SEED = 20260928


def pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[k]


def main():
    gold = {}
    for fam in ("S", "V", "W2", "W1"):
        for r in RD.load_jsonl(os.path.join(HERE, "gold", f"gold_{fam}.jsonl")):
            gold.setdefault(fam, {})[r["qid"]] = r

    # 缓存每题输入, 供阈值扫描重复评测
    inputs = []
    for fam in ("S", "V", "W2"):
        gendir = os.path.join(HERE, "gen", fam)
        for db in sorted(os.listdir(gendir)):
            jmap = RD.load_judge(fam, db)
            per = RD.build_series(fam, db)
            for qid in sorted(per):
                if qid not in gold[fam]:
                    continue
                inputs.append((fam, qid, db, per[qid], jmap))

    def eval_all(params=None):
        old = {k: getattr(RD, k) for k in
               ("TH_EX", "TH_SHARE", "TH_JAC", "JUDGE_DROP", "JUDGE_ABS",
                "KS_ALPHA", "CUSUM_K", "CUSUM_H")}
        if params:
            for k, v in params.items():
                setattr(RD, k, v)
        try:
            out = []
            for fam, qid, db, ser, jmap in inputs:
                e = RD.eval_question(qid, db, ser, jmap, gold[fam][qid])
                if e:
                    e["fam"] = fam
                    out.append(e)
        finally:
            for k, v in old.items():
                setattr(RD, k, v)
        return out

    base = eval_all()
    print(f"[rob] base question-series = {len(base)}", flush=True)

    def sel(fam):
        return [e for e in base if e["fam"] == fam]

    groups = {"S": sel("S"), "V": sel("V"), "W2": sel("W2"),
              "SILENT": sel("V") + sel("W2"), "ALL": base}
    base_metrics = {g: RD.group_metrics(ev) for g, ev in groups.items()}

    # ---- A. bootstrap CI ----
    rng = random.Random(SEED)
    ci = {g: {d: {"power": [], "far": []} for d in DETS} for g in groups}
    for gname, pool in groups.items():
        for _ in range(B_BOOT):
            sample = [pool[rng.randrange(len(pool))] for _ in pool]
            m = RD.group_metrics(sample)
            for d in DETS:
                md = m["detectors"][d]
                if md["power"] is not None:
                    ci[gname][d]["power"].append(md["power"])
                if md["far"] is not None:
                    ci[gname][d]["far"].append(md["far"])
    print("[rob] bootstrap done", flush=True)

    # ---- B. 阈值敏感性 (单参数扫描) ----
    sweeps = []
    for v in (0.1, 0.2, 0.3, 0.4, 0.5):
        sweeps.append((f"TH_EX={v}", {"TH_EX": v}, "D1_ex"))
    for v in (0.1, 0.2, 0.3, 0.4, 0.5):
        sweeps.append((f"TH_SHARE={v}", {"TH_SHARE": v}, "D4_share"))
    for v in (0.1, 0.2, 0.3, 0.4, 0.5):
        sweeps.append((f"TH_JAC={v}", {"TH_JAC": v}, "D6_jac"))
    for v in (10, 15, 20, 25, 30):
        sweeps.append((f"JUDGE_DROP={v}", {"JUDGE_DROP": v}, "D3_judge"))
    for v in (40, 50, 60):
        sweeps.append((f"JUDGE_ABS={v}", {"JUDGE_ABS": v}, "D3_judge"))
    for v in (0.01, 0.05, 0.10):
        sweeps.append((f"KS_ALPHA={v}", {"KS_ALPHA": v}, "D2_ks"))
    for k, h in ((0.25, 1.5), (0.3, 2.0), (0.5, 3.0), (0.5, 5.0)):
        sweeps.append((f"CUSUM k={k} h={h}",
                       {"CUSUM_K": k, "CUSUM_H": h}, "D7_cusum"))
    sens = []
    for label, params, det in sweeps:
        ev = eval_all(params)
        ms = RD.group_metrics([e for e in ev if e["fam"] == "S"])
        msi = RD.group_metrics([e for e in ev if e["fam"] in ("V", "W2")])
        row = {"param": label, "detector": det,
               "S_power": ms["detectors"][det]["power"],
               "S_far": ms["detectors"][det]["far"],
               "SILENT_power": msi["detectors"][det]["power"],
               "SILENT_far": msi["detectors"][det]["far"]}
        sens.append(row)
        print(f"[rob] {label:<18} S {row['S_power']}/{row['S_far']} | "
              f"SILENT {row['SILENT_power']}/{row['SILENT_far']}", flush=True)

    # ---- 输出 ----
    resdir = os.path.join(HERE, "results")
    os.makedirs(resdir, exist_ok=True)
    with open(os.path.join(resdir, "robustness.json"), "w",
              encoding="utf-8") as f:
        json.dump({"base": base_metrics, "bootstrap_ci": ci,
                   "sensitivity": sens}, f, ensure_ascii=False, indent=1)

    L = ["# DriftSQL 检测器稳健性报告\n",
         f"生成时间: {__import__('time').strftime('%Y-%m-%d %H:%M:%S')} "
         "(run_robustness.py, 全部本地重算零 API 成本)\n",
         f"## A. Bootstrap 95% CI (B={B_BOOT}, 按题有放回重采样, 百分位法)\n",
         "单元格: 功效 [lo,hi] / FAR [lo,hi], 括号外为点估计。\n",
         "| 检测器 | S loud | V silent | W2 silent | SILENT | ALL |",
         "|---|" + "---|" * 5]
    for d in DETS:
        cells = []
        for g in ("S", "V", "W2", "SILENT", "ALL"):
            b = base_metrics[g]["detectors"][d]
            cd = ci[g][d]
            plo, phi = pct(cd["power"], 0.025), pct(cd["power"], 0.975)
            flo, fhi = pct(cd["far"], 0.025), pct(cd["far"], 0.975)
            cells.append(f"{b['power']} [{plo},{phi}] / "
                         f"{b['far']} [{flo},{fhi}]")
        L.append(f"| {DETN[d]} | " + " | ".join(cells) + " |")
    L += ["", "## B. 阈值敏感性 (单参数扫描, 其余默认; "
              "单元格: S 功效/FAR | SILENT 功效/FAR)\n",
          "| 参数 | 检测器 | S loud | SILENT(V+W2) |", "|---|---|---|---|"]
    for r in sens:
        L.append(f"| {r['param']} | {DETN[r['detector']]} | "
                 f"{r['S_power']} / {r['S_far']} | "
                 f"{r['SILENT_power']} / {r['SILENT_far']} |")
    L += ["", "> 说明: 单参数扫描不探索参数间交互; D5 众数迁移与 D8 oracle "
              "无自由阈值。bootstrap 重采样单位为题 (同一题的全部检查点"
              "同时进/出样本)。\n"]
    p = os.path.join(resdir, "robustness_report.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[rob] report -> {p}", flush=True)


if __name__ == "__main__":
    main()
