# -*- coding: utf-8 -*-
"""run_detectors_crossgen.py — 同集过滤跨生成器检测器对照 (chat vs v4b)。

子集口径: S 族中 v4b 完整时间序列题 (t=0..max(CKPTS[S]) 每个检查点恰 NCAND=10
行); W1 族中两侧均满 10 行的题。chat 侧限制到同一题集, 保证对照一致性。

评测: 复用 run_detectors.py 的 8 检测器逻辑 (阈值/eval_question/group_metrics
不变), 仅数据目录参数化。judge: chat 用 gen\\judge\\, v4b 用 gen_v4b\\judge\\。

输出: results/detector_crossgen.json + results/detector_crossgen.md
零 API 调用。
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict

from build_drift_benchmark import CKPTS, HERE
from run_detectors import (DETS, DETN, eval_question, group_metrics,
                           jac_dist, ks_pval, load_json, load_jsonl)

NCAND = 10
CHAT = ("gen", "gen/judge")
V4B = ("gen_v4b", "gen_v4b/judge")


def build_series(gendir, fam, db):
    """qid -> {t: 统计}; 与 run_detectors.build_series 相同, 目录参数化。"""
    per = defaultdict(dict)
    for t in range(0, max(CKPTS[fam]) + 1):
        rows = load_jsonl(os.path.join(HERE, gendir, fam, db, f"ck{t}.jsonl"))
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
                         for r in rs if r["correct"] is not None],
            }
    return per


def load_judge(jroot, fam, db):
    out = {}
    for t in range(0, max(CKPTS.get(fam, [0])) + 1):
        for r in load_jsonl(os.path.join(HERE, jroot, fam, db,
                                         f"ck{t}.jsonl")):
            out[(r["qid"], r["ckpt"])] = r.get("judge_prob")
    return out


def v4b_complete(fam):
    """(db, qid) 满足 v4b 全检查点恰 NCAND 行。返回 {db: set(qid)}。"""
    keep = defaultdict(set)
    for db in sorted(os.listdir(os.path.join(HERE, "gen_v4b", fam))):
        d = os.path.join(HERE, "gen_v4b", fam, db)
        if not os.path.isdir(d):
            continue
        cnt = defaultdict(dict)  # qid -> t -> n
        for t in range(0, max(CKPTS[fam]) + 1):
            for r in load_jsonl(os.path.join(d, f"ck{t}.jsonl")):
                cnt[r["qid"]][t] = cnt[r["qid"]].get(t, 0) + 1
        need = set(range(0, max(CKPTS[fam]) + 1))
        for qid, ts in cnt.items():
            if need <= set(ts) and all(ts[t] == NCAND for t in need):
                keep[db].add(qid)
    return keep


def eval_side(gendir, jroot, gold, subset):
    """单侧评测, subset={fam: {db: set(qid)}}; 返回带 fam 的 eval 列表。"""
    evals = []
    for fam, dbs in sorted(subset.items()):
        for db, qids in sorted(dbs.items()):
            jmap = load_judge(jroot, fam, db)
            per = build_series(gendir, fam, db)
            for qid in sorted(qids):
                if qid not in per or qid not in gold.get(fam, {}):
                    continue
                e = eval_question(qid, db, per[qid], jmap, gold[fam][qid])
                if e:
                    e["db"] = db
                    e["fam"] = fam
                    evals.append(e)
    return evals


def w1_agg(gendir, jroot, qids):
    """W1 aggregate (仅 ck0): 按难度 ex/share/lp/judge, 限制题集。"""
    qmap = {q["qid"]: q for q in load_json(
        os.path.join(HERE, "questions", "family_W1.json"))}
    jp = {}
    for db in sorted(os.listdir(os.path.join(HERE, jroot, "W1"))) \
            if os.path.isdir(os.path.join(HERE, jroot, "W1")) else []:
        for r in load_jsonl(os.path.join(HERE, jroot, "W1", db, "ck0.jsonl")):
            jp[r["qid"]] = r.get("judge_prob")
    by = defaultdict(lambda: {"n": 0, "ex": 0.0, "share": 0.0,
                              "lp": 0.0, "j": []})
    for db in sorted(os.listdir(os.path.join(HERE, gendir, "W1"))):
        rows = load_jsonl(os.path.join(HERE, gendir, "W1", db, "ck0.jsonl"))
        per_q = defaultdict(list)
        for r in rows:
            if r["qid"] in qids:
                per_q[r["qid"]].append(r)
        for qid, rs in per_q.items():
            if len(rs) != NCAND:
                continue
            diff = qmap.get(qid, {}).get("difficulty", "?")
            cls = Counter(r["result_hash"] or "EXECFAIL" for r in rs)
            b = by[diff]
            b["n"] += 1
            b["ex"] += sum(1 for r in rs if r["exec_ok"]) / len(rs)
            b["share"] += max(cls.values()) / len(rs)
            lps = [r["token_logprob"] for r in rs
                   if r["token_logprob"] is not None]
            b["lp"] += sum(lps) / len(lps) if lps else 0.0
            if jp.get(qid) is not None:
                b["j"].append(jp[qid])
    out = []
    for diff, b in sorted(by.items()):
        out.append({"difficulty": diff, "n_questions": b["n"],
                    "ex_rate": round(b["ex"] / b["n"], 3),
                    "mean_share": round(b["share"] / b["n"], 3),
                    "mean_logprob": round(b["lp"] / b["n"], 2),
                    "mean_judge": round(sum(b["j"]) / len(b["j"]), 1)
                                  if b["j"] else None})
    return out


def w1_common_qids():
    """W1: 两侧均满 10 行的 qid 集合。"""
    def full_qids(gendir):
        keep = set()
        d = os.path.join(HERE, gendir, "W1")
        for db in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            per_q = defaultdict(int)
            for r in load_jsonl(os.path.join(d, db, "ck0.jsonl")):
                per_q[r["qid"]] += 1
            keep |= {q for q, n in per_q.items() if n == NCAND}
        return keep
    return full_qids(CHAT[0]) & full_qids(V4B[0])


def main():
    t0 = time.time()
    gold = {}
    for fam in ("S", "V", "W2"):
        for r in load_jsonl(os.path.join(HERE, "gold", f"gold_{fam}.jsonl")):
            gold.setdefault(fam, {})[r["qid"]] = r

    subset = {fam: v4b_complete(fam) for fam in ("S", "V", "W2")}
    for fam in ("S", "V", "W2"):
        n_q = sum(len(s) for s in subset[fam].values())
        print(f"[xgen] {fam} subset: {n_q} questions across "
              f"{len(subset[fam])} dbs", flush=True)
        for db, s in sorted(subset[fam].items()):
            print(f"  {fam}/{db}: {len(s)}", flush=True)

    chat_evals = eval_side(CHAT[0], CHAT[1], gold, subset)
    v4b_evals = eval_side(V4B[0], V4B[1], gold, subset)

    def groups_of(evals):
        def sel(fam):
            return [e for e in evals if e["fam"] == fam]
        return {"S": group_metrics(sel("S")),
                "V": group_metrics(sel("V")),
                "W2": group_metrics(sel("W2")),
                "SILENT": group_metrics(sel("V") + sel("W2")),
                "ALL": group_metrics(evals)}

    chat_g, v4b_g = groups_of(chat_evals), groups_of(v4b_evals)
    print(f"[xgen] chat evals={len(chat_evals)} v4b evals={len(v4b_evals)}",
          flush=True)

    w1_q = w1_common_qids()
    print(f"[xgen] W1 common full units: {len(w1_q)}", flush=True)
    w1_chat = w1_agg(CHAT[0], CHAT[1], w1_q)
    w1_v4b = w1_agg(V4B[0], V4B[1], w1_q)

    res = {"subset": {fam: {db: sorted(s) for db, s in d.items()}
                      for fam, d in subset.items()},
           "subset_n": {fam: sum(len(s) for s in d.values())
                        for fam, d in subset.items()},
           "W1_n": len(w1_q),
           "groups": {"chat": chat_g, "v4b": v4b_g},
           "W1": {"chat": w1_chat, "v4b": w1_v4b},
           "params_note": "阈值与 run_detectors.py 完全一致"}
    resdir = os.path.join(HERE, "results")
    os.makedirs(resdir, exist_ok=True)
    with open(os.path.join(resdir, "detector_crossgen.json"), "w",
              encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)

    # ---- 报告 ----
    L = ["# 跨生成器检测器对照 (chat vs v4b, 同集过滤)\n",
         f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')} "
         "(run_detectors_crossgen.py)\n",
         "## 子集口径",
         "- 每族取 v4b 完整时间序列题 (全检查点恰 10 候选): " +
         ", ".join(f"**{fam} {sum(len(s) for s in d.values())}**"
                   for fam, d in subset.items()),
         f"- W1: 两侧均满 10 候选的题 **{len(w1_q)}** 道",
         "- chat 侧限制到同一题集; v4b = deepseek-v4-pro (thinking=disabled, "
         "MAXTOK=2000); 阈值同 run_detectors.py。\n"]

    def fam_table(gname, gm):
        cols = [("S", "S loud"), ("V", "V silent"), ("W2", "W2 silent"),
                ("SILENT", "SILENT(V+W2)")]
        rows = [f"## {gname}: 分族主表 (功效 / 中位延迟 / FAR)\n",
                "| 检测器 | " + " | ".join(n for _, n in cols) + " |",
                "|---|" + "---|" * len(cols)]
        for d in DETS:
            cells = []
            for g, _ in cols:
                m = gm[g]["detectors"][d]
                cells.append(
                    f"{m['power'] if m['power'] is not None else '-'} / "
                    f"{m['med_delay'] if m['med_delay'] is not None else '-'} / "
                    f"{m['far'] if m['far'] is not None else '-'}")
            rows.append(f"| {DETN[d]} | " + " | ".join(cells) + " |")
        rows += ["", "| 列 | n题 | n_onset | n_stable检查点 |",
                 "|---|---|---|---|"]
        for g, n in cols:
            q = gm[g]
            rows.append(f"| {n} | {q['n_questions']} | {q['n_onset']} | "
                        f"{q['n_stable_ckpts']} |")
        rows.append("")
        return rows

    L += fam_table("chat (deepseek-chat)", chat_g)
    L += fam_table("v4b (deepseek-v4-pro, thinking=disabled)", v4b_g)
    L += ["## W1 aggregate (同题集)\n",
          "| difficulty | n | chat: EX/share/lp/judge | v4b: EX/share/lp/judge |",
          "|---|---|---|---|"]
    jc = {r["difficulty"]: r for r in w1_chat}
    jv = {r["difficulty"]: r for r in w1_v4b}
    for diff in sorted(set(jc) | set(jv)):
        def fmt(r):
            if not r:
                return "-"
            return (f"{r['ex_rate']} / {r['mean_share']} / "
                    f"{r['mean_logprob']} / "
                    f"{r['mean_judge'] if r['mean_judge'] is not None else '-'}")
        ncell = jc[diff]['n_questions'] if diff in jc else \
            jv.get(diff, {}).get('n_questions', '-')
        L.append(f"| {diff} | {ncell} "
                 f"| {fmt(jc.get(diff))} | {fmt(jv.get(diff))} |")
    L += ["", "> 单元格: 功效 / 中位延迟(检查点) / FAR。W1 单元格: EX率 / "
              "mean share / mean logprob / mean judge。", ""]
    rp = os.path.join(resdir, "detector_crossgen.md")
    with open(rp, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[xgen] report -> {rp}", flush=True)
    print(f"[xgen] done in {(time.time()-t0)/60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
