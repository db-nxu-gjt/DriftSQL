# -*- coding: utf-8 -*-
"""run_detectors.py — 8 检测器评测: 检测延迟 / 稳定期假警报 / 分族功效。

序列单元 (family, db, qid), 检查点 t=1..T 与 t0 基线状态比较。
truth(t) = gold 在 t 执行破坏 (exec_ok=False) 或 gold 指纹 != t0 指纹
(变换累积 → 漂移持续)。onset = 首个 truth 漂移的检查点 (无则为 None)。
检测器首次告警 t_a:
  命中:   t_a >= onset, 延迟 = t_a - onset (检查点数)
  假警报: 稳定检查点 (t < onset; 无 onset 则全部 t>=1) 被锁存告警覆盖
指标: 功效 (onset 题中命中比例) / 延迟 / FAR (稳定检查点覆盖率)。
分族报告: S(loud) / V(silent) / W2(silent) / SILENT(V+W2) / ALL; W1 仅 aggregate。

检测器:
  D1 EX衰减      ex_rate(t) <= ex_rate(0) - 0.30
  D2 logprob-KS   token_logprob 两样本 KS 检验 p < 0.05
  D3 judge置信    judge(t) <= judge(0) - 20  或  judge(t) < 50
  D4 share衰减    share(t) <= share(0) - 0.30   (share = 众数类占比)
  D5 众数迁移     众数 result_hash(t) != 众数(t0)  (EXECFAIL 自成一类)
  D6 指纹Jaccard  多重集类距离 1 - |交|/|并| >= 0.30
  D7 CUSUM        correct 率标准化 CUSUM (k=0.3, h=2)
  D8 oracle       gold 感知 (与 truth 等价, 上界 sanity check)

输出: results/detector_results.json + results/detector_report.md
"""
from __future__ import annotations

import json
import math
import os
import time
from collections import Counter, defaultdict

from build_drift_benchmark import CKPTS, HERE

TH_EX = 0.30
TH_SHARE = 0.30
TH_JAC = 0.30
JUDGE_DROP = 20.0
JUDGE_ABS = 50.0
KS_ALPHA = 0.05
CUSUM_K = 0.3
CUSUM_H = 2.0
DETS = ["D1_ex", "D2_ks", "D3_judge", "D4_share", "D5_mode", "D6_jac",
        "D7_cusum", "D8_oracle"]
DETN = {"D1_ex": "D1 EX衰减", "D2_ks": "D2 logprob-KS", "D3_judge": "D3 judge置信",
        "D4_share": "D4 share衰减", "D5_mode": "D5 众数迁移",
        "D6_jac": "D6 指纹Jaccard", "D7_cusum": "D7 CUSUM-correct",
        "D8_oracle": "D8 oracle(gold)"}


def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_jsonl(p):
    if not os.path.exists(p):
        return []
    return [json.loads(ln) for ln in open(p, encoding="utf-8")]


def ks_pval(x, y):
    """两样本 KS 渐近 p 值 (Stephens 近似, 无 scipy 依赖)。"""
    xs, ys = sorted(x), sorted(y)
    n, m = len(xs), len(ys)
    i = j = 0
    d = 0.0
    for v in sorted(set(xs) | set(ys)):
        while i < n and xs[i] <= v:
            i += 1
        while j < m and ys[j] <= v:
            j += 1
        d = max(d, abs(i / n - j / m))
    en = math.sqrt(n * m / (n + m))
    lam = (en + 0.12 + 0.11 / en) * d
    p = sum(2.0 * ((-1) ** (k - 1)) * math.exp(-2.0 * k * k * lam * lam)
            for k in range(1, 101))
    return max(0.0, min(1.0, p))


def jac_dist(a, b):
    """类计数多重集 Jaccard 距离 1 - |交|/|并|。"""
    keys = set(a) | set(b)
    inter = sum(min(a.get(c, 0), b.get(c, 0)) for c in keys)
    uni = sum(max(a.get(c, 0), b.get(c, 0)) for c in keys)
    return 1.0 - inter / uni if uni else 0.0


def build_series(fam, db):
    """qid -> {t: 统计}; t0 的类计数留给 D6/Jaccard 与基线。"""
    per = defaultdict(dict)
    for t in range(0, max(CKPTS[fam]) + 1):
        rows = load_jsonl(os.path.join(HERE, "gen", fam, db, f"ck{t}.jsonl"))
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


def load_judge(fam, db):
    """(qid, t) -> judge_prob。"""
    out = {}
    for t in range(0, max(CKPTS.get(fam, [0])) + 1):
        for r in load_jsonl(os.path.join(HERE, "gen", "judge", fam, db,
                                         f"ck{t}.jsonl")):
            out[(r["qid"], r["ckpt"])] = r.get("judge_prob")
    return out


def eval_question(qid, db, ser, jmap, gentry):
    """单题时间序列评测, 返回 {onset, alarms, ts} 或 None (不可评)。"""
    g = gentry["checkpoints"]
    g0 = g.get("0")
    if not g0 or not g0.get("exec_ok") or not g0.get("result_hash"):
        return None
    if 0 not in ser:
        return None
    ts = sorted(t for t in ser if str(t) in g and t >= 1)
    if not ts:
        return None
    truth = {}
    for t in ts:
        gt = g[str(t)]
        truth[t] = (not gt.get("exec_ok")) or \
                   (gt.get("result_hash") != g0["result_hash"])
    onset = next((t for t in ts if truth[t]), None)
    s0 = ser[0]
    alarms = {}

    # D1 EX 衰减
    alarms["D1_ex"] = next((t for t in ts
                            if s0["ex"] - ser[t]["ex"] >= TH_EX), None)
    # D2 logprob KS
    a = None
    for t in ts:
        if len(s0["lps"]) >= 3 and len(ser[t]["lps"]) >= 3 and \
                ks_pval(s0["lps"], ser[t]["lps"]) < KS_ALPHA:
            a = t
            break
    alarms["D2_ks"] = a
    # D3 judge 置信
    a = None
    j0 = jmap.get((qid, 0))
    if j0 is not None:
        for t in ts:
            jt = jmap.get((qid, t))
            if jt is not None and (j0 - jt >= JUDGE_DROP or jt < JUDGE_ABS):
                a = t
                break
    alarms["D3_judge"] = a
    # D4 share 衰减
    alarms["D4_share"] = next((t for t in ts
                               if s0["share"] - ser[t]["share"] >= TH_SHARE),
                              None)
    # D5 众数迁移
    alarms["D5_mode"] = next((t for t in ts
                              if ser[t]["mode"] != s0["mode"]), None)
    # D6 指纹 Jaccard 距离
    alarms["D6_jac"] = next((t for t in ts
                             if jac_dist(s0["cls"], ser[t]["cls"]) >= TH_JAC),
                            None)
    # D7 correct 率 CUSUM (标准化 z 增量, k=0.3, h=2)
    a = None
    mu0 = sum(s0["corr"]) / len(s0["corr"]) if s0["corr"] else None
    if mu0 is not None and mu0 > 0.0:
        S = 0.0
        for t in ts:
            c = ser[t]["corr"]
            if not c:
                continue
            r_t = sum(c) / len(c)
            if mu0 < 1.0:
                sd = math.sqrt(mu0 * (1.0 - mu0) / len(c))
                z = (mu0 - r_t) / sd
            else:
                z = 10.0 if r_t < 1.0 else 0.0
            S = max(0.0, S + z - CUSUM_K)
            if S >= CUSUM_H:
                a = t
                break
    alarms["D7_cusum"] = a
    # D8 oracle: 与 truth 等价
    alarms["D8_oracle"] = onset
    return {"qid": qid, "db": db, "onset": onset, "ts": ts,
            "truth": [truth[t] for t in ts], "alarms": alarms}


def group_metrics(evals):
    n_on = sum(1 for e in evals if e["onset"] is not None)
    n_stable = 0
    for e in evals:
        lim = e["onset"] if e["onset"] is not None else 10 ** 9
        n_stable += sum(1 for t in e["ts"] if t < lim)
    dets = {}
    for d in DETS:
        delays = [e["alarms"][d] - e["onset"] for e in evals
                  if e["onset"] is not None and e["alarms"][d] is not None
                  and e["alarms"][d] >= e["onset"]]
        fa_ck = 0
        for e in evals:
            a = e["alarms"][d]
            if a is None:
                continue
            lim = e["onset"] if e["onset"] is not None else 10 ** 9
            fa_ck += sum(1 for t in e["ts"] if a <= t < lim)
        dets[d] = {
            "power": round(len(delays) / n_on, 3) if n_on else None,
            "mean_delay": round(sum(delays) / len(delays), 2)
                          if delays else None,
            "med_delay": sorted(delays)[len(delays) // 2]
                         if delays else None,
            "far": round(fa_ck / n_stable, 4) if n_stable else None,
            "n_detected": len(delays),
        }
    return {"n_questions": len(evals), "n_onset": n_on,
            "n_stable_ckpts": n_stable, "detectors": dets}


def w1_aggregate():
    """W1 纯工作负载族仅 t0 候选: 按难度的 aggregate 描述。"""
    qmap = {q["qid"]: q for q in load_json(
        os.path.join(HERE, "questions", "family_W1.json"))}
    jp = {}
    for r in load_jsonl(os.path.join(HERE, "gen", "judge", "W1", "agg",
                                     "ck0.jsonl")):
        jp[r["qid"]] = r.get("judge_prob")
    for db in sorted(os.listdir(os.path.join(HERE, "gen", "W1"))):
        for r in load_jsonl(os.path.join(HERE, "gen", "judge", "W1", db,
                                         "ck0.jsonl")):
            jp[r["qid"]] = r.get("judge_prob")
    by = defaultdict(lambda: {"n": 0, "ex": 0.0, "share": 0.0,
                              "lp": 0.0, "j": []})
    for db in sorted(os.listdir(os.path.join(HERE, "gen", "W1"))):
        rows = load_jsonl(os.path.join(HERE, "gen", "W1", db, "ck0.jsonl"))
        per_q = defaultdict(list)
        for r in rows:
            per_q[r["qid"]].append(r)
        for qid, rs in per_q.items():
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


def write_report(groups, w1, resdir):
    L = []
    L.append("# DriftSQL 检测器基线报告\n")
    L.append(f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')} "
             "(run_detectors.py)\n")
    L.append("## 评测设置\n")
    L.append("- 序列单元 (family, db, qid); 检查点 t=1..T 与 t0 基线比较。")
    L.append("- truth = 漂移感知 gold: 指纹相对 t0 变化或执行破坏; 变换累积, "
             "漂移持续。")
    L.append("- 功效 = onset 题中被命中比例; 延迟 = 首次告警 − onset "
             "(检查点数, 越小越好); FAR = 锁存告警覆盖的稳定检查点比例。")
    L.append(f"- 阈值: EX/share/Jaccard={TH_EX}, judge 降{JUDGE_DROP:.0f}或"
             f"绝对<{JUDGE_ABS:.0f}, KS alpha={KS_ALPHA}, "
             f"CUSUM k={CUSUM_K} h={CUSUM_H}。\n")
    L.append("## 主表: 8 检测器 × 分族 (功效 / 中位延迟 / FAR)\n")
    cols = [("S", "S loud"), ("V", "V silent"), ("W2", "W2 silent"),
            ("SILENT", "SILENT(V+W2)"), ("ALL", "ALL")]
    L.append("| 检测器 | " + " | ".join(n for _, n in cols) + " |")
    L.append("|---|" + "---|" * len(cols))
    for d in DETS:
        cells = []
        for g, _ in cols:
            m = groups[g]["detectors"][d]
            cells.append(f"{m['power'] if m['power'] is not None else '-'} / "
                         f"{m['med_delay'] if m['med_delay'] is not None else '-'} / "
                         f"{m['far'] if m['far'] is not None else '-'}")
        L.append(f"| {DETN[d]} | " + " | ".join(cells) + " |")
    L.append("")
    L.append("| 列 | n题 | n_onset | n_stable检查点 |")
    L.append("|---|---|---|---|")
    for g, n in cols:
        gm = groups[g]
        L.append(f"| {n} | {gm['n_questions']} | {gm['n_onset']} | "
                 f"{gm['n_stable_ckpts']} |")
    L.append("")
    L.append("> 单元格: 功效 / 中位延迟(检查点) / FAR。D8 oracle 与 truth "
             "等价, 延迟恒 0、FAR 恒 0, 作为上界 sanity check。\n")
    L.append("## W1 (纯工作负载族, 仅 t0 aggregate)\n")
    L.append("| difficulty | n题 | EX率 | mean share | mean logprob | "
             "mean judge |")
    L.append("|---|---|---|---|---|---|")
    for r in w1:
        L.append(f"| {r['difficulty']} | {r['n_questions']} | "
                 f"{r['ex_rate']} | {r['mean_share']} | "
                 f"{r['mean_logprob']} | "
                 f"{r['mean_judge'] if r['mean_judge'] is not None else '-'} |")
    L.append("")
    p = os.path.join(resdir, "detector_report.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return p


def main():
    gold = {}
    for fam in ("S", "V", "W2", "W1"):
        for r in load_jsonl(os.path.join(HERE, "gold", f"gold_{fam}.jsonl")):
            gold.setdefault(fam, {})[r["qid"]] = r

    evals = []
    for fam in ("S", "V", "W2"):
        gendir = os.path.join(HERE, "gen", fam)
        for db in sorted(os.listdir(gendir)):
            if not os.path.isdir(gendir):
                continue
            jmap = load_judge(fam, db)
            per = build_series(fam, db)
            for qid in sorted(per):
                if qid not in gold[fam]:
                    continue
                e = eval_question(qid, db, per[qid], jmap, gold[fam][qid])
                if e:
                    e["fam"] = fam
                    evals.append(e)
    print(f"[det] evaluated question-series = {len(evals)}", flush=True)

    def sel(fam):
        return [e for e in evals if e["fam"] == fam]

    groups = {"S": group_metrics(sel("S")),
              "V": group_metrics(sel("V")),
              "W2": group_metrics(sel("W2")),
              "SILENT": group_metrics(sel("V") + sel("W2")),
              "ALL": group_metrics(evals)}
    for g, gm in groups.items():
        print(f"[det] {g}: n={gm['n_questions']} onset={gm['n_onset']} "
              f"stable={gm['n_stable_ckpts']}", flush=True)
        for d in DETS:
            m = gm["detectors"][d]
            print(f"  {d:<10} power={m['power']} med_delay={m['med_delay']} "
                  f"far={m['far']}", flush=True)

    w1 = w1_aggregate()
    resdir = os.path.join(HERE, "results")
    os.makedirs(resdir, exist_ok=True)
    with open(os.path.join(resdir, "detector_results.json"), "w",
              encoding="utf-8") as f:
        json.dump({"params": {"TH_EX": TH_EX, "TH_SHARE": TH_SHARE,
                              "TH_JAC": TH_JAC, "JUDGE_DROP": JUDGE_DROP,
                              "JUDGE_ABS": JUDGE_ABS, "KS_ALPHA": KS_ALPHA,
                              "CUSUM_K": CUSUM_K, "CUSUM_H": CUSUM_H},
                   "groups": groups,
                   "w1_aggregate": w1,
                   "questions": evals}, f, ensure_ascii=False, indent=1)
    rp = write_report(groups, w1, resdir)
    print(f"[det] report -> {rp}", flush=True)


if __name__ == "__main__":
    main()
