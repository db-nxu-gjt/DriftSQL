# -*- coding: utf-8 -*-
"""run_detectors_w3.py — W3 改写族检测器评测 (chat vs v4b, 零 API)。

W3 = 同意图改写 (benign 负控制): DB 恒定, gold 各 ck = t0 → 全部 stable,
任何报警都是误报。指标:
  1) FAR_pooled: 全序列 (t1-t3) 各检测器报警率, 按侧
  2) FAR_by_severity: t1 轻 / t2 中 / t3 深 分别的报警率 (序列截断到 {0,t})
  3) judge 均值按 t (人工读数)
复用 run_detectors.eval_question/group_metrics 与 run_detectors_crossgen 的
build_series/load_judge (目录参数化), 阈值完全一致。

输出: results/detector_w3.md + results/detector_w3.json
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict

from build_drift_benchmark import CKPTS, HERE
from run_detectors import DETS, DETN, eval_question, group_metrics, load_json, load_jsonl

SIDES = [("chat", "gen_w3", "gen_w3/judge/W3"),
         ("v4b", "gen_w3b", "gen_w3b/judge/W3")]
TMAX = max(CKPTS["W2"])  # 3


def build_series_w3(gendir, db):
    per = defaultdict(dict)
    for t in range(0, TMAX + 1):
        rows = load_jsonl(os.path.join(HERE, gendir, "W3", db, f"ck{t}.jsonl"))
        if not rows:
            continue
        per_q = defaultdict(list)
        for r in rows:
            per_q[r["qid"]].append(r)
        for qid, rs in per_q.items():
            cls = Counter(r["result_hash"] or "EXECFAIL" for r in rs)
            mode, cnt = max(cls.items(), key=lambda kv: (kv[1], kv[0]))
            per[qid][t] = {
                "n": len(rs),
                "ex": sum(1 for r in rs if r["exec_ok"]) / len(rs),
                "cls": dict(cls),
                "share": cnt / len(rs),
                "mode": mode,
                "lps": [r["token_logprob"] for r in rs
                        if r["token_logprob"] is not None],
                "corr": [1 if r["correct"] is True else 0
                         for r in rs if r["correct"] is not None]}
    return per


def load_judge_w3(jroot, db):
    out = {}
    for t in range(0, TMAX + 1):
        for r in load_jsonl(os.path.join(HERE, jroot, db, f"ck{t}.jsonl")):
            out[(r["qid"], r["ckpt"])] = r.get("judge_prob")
    return out


def eval_side(gendir, jroot, gold, trunc_t=None):
    evals, jp_by_t = [], defaultdict(list)
    for db in sorted(os.listdir(os.path.join(HERE, gendir, "W3"))):
        d = os.path.join(HERE, gendir, "W3", db)
        if not os.path.isdir(d):
            continue
        jmap = load_judge_w3(jroot, db)
        per_all = build_series_w3(gendir, db)
        for qid in sorted(per_all):
            if qid not in gold or qid not in jmap and not any(
                    (qid, t) in jmap for t in range(0, TMAX + 1)):
                continue
            per = per_all[qid]
            if trunc_t is not None:
                per = {t: s for t, s in per.items() if t in (0, trunc_t)}
            if 0 not in per:
                continue
            e = eval_question(qid, db, per, jmap, gold[qid])
            if e:
                e["db"] = db
                evals.append(e)
            for t in range(1, TMAX + 1):
                jp = jmap.get((qid, t))
                if jp is not None:
                    jp_by_t[t].append(jp)
    return evals, {t: round(sum(v) / len(v), 1) for t, v in sorted(
        jp_by_t.items()) if v}


def main():
    t0 = time.time()
    gold = {}
    for r in load_jsonl(os.path.join(HERE, "gold", "gold_W3.jsonl")):
        gold[r["qid"]] = r
    print(f"[w3det] gold questions = {len(gold)}", flush=True)

    res = {"sides": {}}
    for name, gendir, jroot in SIDES:
        evals, jp = eval_side(gendir, jroot, gold)
        gm = group_metrics(evals)
        by_sev = {}
        for t in range(1, TMAX + 1):
            ev_t, _ = eval_side(gendir, jroot, gold, trunc_t=t)
            gm_t = group_metrics(ev_t)
            by_sev[t] = {d: gm_t["detectors"][d]["far"] for d in DETS}
        res["sides"][name] = {"pooled": gm, "by_severity": by_sev,
                              "judge_mean_by_t": jp,
                              "n_questions": gm["n_questions"]}
        print(f"[w3det] {name}: n={gm['n_questions']} "
              f"stable_ckpts={gm['n_stable_ckpts']} judge_by_t={jp}",
              flush=True)

    resdir = os.path.join(HERE, "results")
    os.makedirs(resdir, exist_ok=True)
    with open(os.path.join(resdir, "detector_w3.json"), "w",
              encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)

    # ---- 报告 ----
    L = ["# W3 改写族 (benign 负控制): 检测器误报评测\n",
         f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')} "
         "(run_detectors_w3.py)\n",
         "W3 = 同意图逐级改写 (t1 轻 / t2 中 / t3 深), DB 恒定, gold 同 t0 "
         "→ 全部 stable, 报警即误报 (FAR)。阈值同 run_detectors.py。\n",
         "## 合并误报率 (t1-t3 全部 stable 检查点)\n",
         "| 检测器 | chat | v4b |", "|---|---|---|"]
    for d in DETS:
        cells = []
        for name, *_ in SIDES:
            m = res["sides"][name]["pooled"]["detectors"][d]
            cells.append(f"{m['far'] if m['far'] is not None else '-'}")
        L.append(f"| {DETN[d]} | " + " | ".join(cells) + " |")
    L += ["", "## 分严重度误报率\n"]
    for name, *_ in SIDES:
        L += [f"### {name}", "",
              "| 检测器 | t1 轻 | t2 中 | t3 深 |", "|---|---|---|---|"]
        for d in DETS:
            cells = [str(res["sides"][name]["by_severity"][t].get(d, "-"))
                     for t in (1, 2, 3)]
            L.append(f"| {DETN[d]} | " + " | ".join(cells) + " |")
        L.append("")
    L += ["## judge 均值置信 (按检查点)", "",
          "| 侧 | t0 原题 | t1 轻 | t2 中 | t3 深 |", "|---|---|---|---|---|"]
    for name, *_ in SIDES:
        jp = res["sides"][name]["judge_mean_by_t"]
        L.append(f"| {name} | {jp.get(0, '-')} | {jp.get(1, '-')} | "
                 f"{jp.get(2, '-')} | {jp.get(3, '-')} |")
    L += ["", "> 解读: W3 为良性负载变化 (同意图改写)。理想监控在 W3 上应保持"
              "静默; 任何检测器的高报警率 = 对表述变化的过敏感 (部署时会误报)。"
              "D8 oracle 按构造恒 0 (gold 不变)。", ""]
    rp = os.path.join(resdir, "detector_w3.md")
    with open(rp, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[w3det] report -> {rp}", flush=True)
    print(f"[w3det] done in {(time.time()-t0)/60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
