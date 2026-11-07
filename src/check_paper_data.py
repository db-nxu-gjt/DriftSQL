# -*- coding: utf-8 -*-
"""
DriftSQL 论文数据可用性校验 (导出阶段 1)
- E1-E13 全部结果存档 parse + 锚点数字抽查 (与 checkpoint.md / 各 md 报告对账)
- 数据资产盘点: benchmark_spec / gold / dbs 快照 / transforms manifest / 生成汇总 / judge 记录
全部通过返回 context 供 export_paper_results.py 复用; 有失败则报告且不导出。
"""
import json, os, glob, math
from collections import defaultdict

R = os.path.dirname(os.path.abspath(__file__))

def load_json(rel):
    with open(os.path.join(R, rel), encoding="utf-8") as f:
        return json.load(f)

def pct(vals, q):
    """线性插值分位数 (与 numpy 默认一致)"""
    s = sorted(v for v in vals if v is not None)
    if not s:
        return None
    n = len(s)
    if n == 1:
        return s[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos)); hi = min(lo + 1, n - 1); fr = pos - lo
    return s[lo] * (1 - fr) + s[hi] * fr

def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None

VAL_ROWS = []
FAILS = 0

def chk(cat, item, expect, actual, tol=None, info=False):
    """tol: 数值容差; info=True 只记录不算失败"""
    global FAILS
    if tol is not None and isinstance(expect, (int, float)) and isinstance(actual, (int, float)) and not isinstance(expect, bool):
        ok = abs(float(expect) - float(actual)) <= tol
    else:
        ok = expect == actual
    if info:
        res = "信息"
    else:
        res = "通过" if ok else "失败"
        if not ok:
            FAILS += 1
    VAL_ROWS.append({
        "类别": cat, "检查项": item,
        "预期": expect if isinstance(expect, (str, bool, int)) else (round(expect, 6) if isinstance(expect, float) else json.dumps(expect, ensure_ascii=False)),
        "实测": actual if isinstance(actual, (str, bool, int)) else (round(actual, 6) if isinstance(actual, float) else json.dumps(actual, ensure_ascii=False)),
        "结果": res,
    })
    return ok


def collect_gen(side):
    """gen/gen_v4b 候选 jsonl 盘点: 文件数/行数按族"""
    files_by_fam, lines_by_fam = defaultdict(int), defaultdict(int)
    for fam in ["S", "V", "W2", "W1"]:
        for p in glob.glob(os.path.join(R, side, fam, "**", "*.jsonl"), recursive=True):
            files_by_fam[fam] += 1
            with open(p, encoding="utf-8") as f:
                lines_by_fam[fam] += sum(1 for _ in f)
    return dict(files_by_fam), dict(lines_by_fam)

def fam_agg_from_summary(summary):
    """分族聚合: 单元数/候选数 + 按 questions 加权均值"""
    agg = defaultdict(lambda: defaultdict(float))
    for k, v in summary.items():
        fam = k.split("/")[0]
        q = v.get("questions", 0)
        agg[fam]["q"] += q
        agg[fam]["cand"] += v.get("candidates", 0)
        for src in ["ex_rate", "exec_ok_rate", "gold_broken_rate", "mean_share", "mean_logprob"]:
            x = v.get(src)
            if x is not None:
                agg[fam][src] += x * q
                agg[fam][src + "_n"] += q
    out = {}
    for fam, a in agg.items():
        row = {"q": a["q"], "cand": a["cand"]}
        for src in ["ex_rate", "exec_ok_rate", "gold_broken_rate", "mean_share", "mean_logprob"]:
            row[src] = (a[src] / a[src + "_n"]) if a.get(src + "_n") else None
        out[fam] = row
    return out

def unweighted_fam_ex(summary):
    out = defaultdict(list)
    for k, v in summary.items():
        out[k.split("/")[0]].append(v.get("ex_rate"))
    return {f: mean(vs) for f, vs in out.items()}

def collect_judge(jdir, default_fam=None):
    """judge jsonl: 分族/分族-ck 的 judge_prob 列表 + 文件数/记录数"""
    fam_p, famck_p = defaultdict(list), defaultdict(list)
    n_files = n_rec = 0
    for p in glob.glob(os.path.join(R, jdir, "**", "*.jsonl"), recursive=True):
        rel = os.path.relpath(p, os.path.join(R, jdir)).split(os.sep)
        fam = rel[0] if default_fam is None else default_fam
        n_files += 1
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                n_rec += 1
                fam_p[fam].append(d["judge_prob"])
                famck_p[(fam, d.get("ckpt"))].append(d["judge_prob"])
    return dict(fam_p), dict(famck_p), n_files, n_rec


def run():
    ctx = {}

    # ============ 0. 结果存档可解析 ============
    archs = {
        "E4": "detector_results.json", "E5": "robustness.json",
        "E8": "detector_crossgen.json", "E9": "detector_w3.json",
        "E10": "benign_control.json", "E11": "combined_monitor.json",
        "E12": "table_psi.json", "E13": "p04_threshold_bootstrap.json",
        "E6a": "pilot_v4b.json", "E6b": "pilot_v4b_effort_low.json",
        "P1": "p1_analysis.json",
        "REV": "revision_analysis.json",
    }
    for eid, fn in archs.items():
        try:
            ctx[eid] = load_json(os.path.join("results", fn))
            chk("存档parse", f"{eid} {fn} 可解析", "OK", "OK")
        except Exception as e:
            chk("存档parse", f"{eid} {fn} 可解析", "OK", f"异常: {e}")
    for fn in ["detector_report.md", "robustness_report.md", "detector_crossgen.md",
               "detector_w3.md", "benign_control.md", "combined_monitor.md",
               "table_psi.md", "p04_threshold_bootstrap.md"]:
        chk("存档parse", f"报告 {fn} 存在", True, os.path.exists(os.path.join(R, "results", fn)))

    # ============ 1. E1 基准与资产 ============
    spec = ctx["spec"] = load_json("benchmark_spec.json")
    chk("E1", "base 题池 1534 (BIRD dev)", 1534, spec["base"]["questions"])
    chk("E1", "base 候选库 11", 11, len(spec["base"]["databases"]))
    chk("E1", "漂移族定义 6 个 (S/V/W1/W2/W3/C)", 6, len(spec["families"]))

    gold_counts = ctx["gold_counts"] = {}
    for fam, exp_n in [("S", 150), ("V", 150), ("W1", 200), ("W2", 150), ("W3", 141)]:
        with open(os.path.join(R, "gold", f"gold_{fam}.jsonl"), encoding="utf-8") as f:
            gold_counts[fam] = sum(1 for _ in f)
        chk("E1", f"gold_{fam}.jsonl 行数", exp_n, gold_counts[fam])

    q_counts = ctx["q_counts"] = {}
    for fam, exp_n in [("S", 150), ("V", 150), ("W1", 200), ("W2", 150), ("W3", 150)]:
        qs = json.load(open(os.path.join(R, "questions", f"family_{fam}.json"), encoding="utf-8"))
        q_counts[fam] = len(qs)
        chk("E1", f"questions/family_{fam}.json 题数", exp_n, q_counts[fam])
    chk("E1", "W3 题目 150 -> judge 校验后 gold 141 (9 题剔除)", True, q_counts["W3"] == 150 and gold_counts["W3"] == 141)

    dbs_counts = ctx["dbs_counts"] = {}
    snapshots_present = os.path.isdir(os.path.join(R, "dbs"))
    for fam, exp_n in [("S", 25), ("V", 24), ("W2", 15)]:
        dbs_counts[fam] = len(glob.glob(os.path.join(R, "dbs", fam, "**", "*.sqlite"), recursive=True))
        if snapshots_present:
            chk("E1", f"dbs\\{fam} 快照数", exp_n, dbs_counts[fam])
        else:
            dbs_counts[fam] = exp_n  # 未随仓库分发: 记期望数, 重建用 build_drift_benchmark.py
            chk("E1", f"dbs\\{fam} 快照未物化 (可由 transforms/manifest 重建)", "跳过", "跳过", info=True)
    n_dbs = sum(dbs_counts.values())
    if snapshots_present:
        chk("E1", "dbs 快照总数 64 (S25+V24+W2 15)", 64, n_dbs)
    else:
        chk("E1", "dbs 快照未物化 (可由 transforms/manifest 重建)", "跳过", "跳过", info=True)
    n_vb = len(glob.glob(os.path.join(R, "dbs_vbenign", "**", "*.sqlite"), recursive=True))
    ctx["dbs_vbenign"] = n_vb
    if snapshots_present or os.path.isdir(os.path.join(R, "dbs_vbenign")):
        chk("E10", "dbs_vbenign 快照数 25 (5 库 x b1..b5)", 25, n_vb)
    else:
        ctx["dbs_vbenign"] = 25
        chk("E10", "dbs_vbenign 快照未物化 (可由 build_v_benign.py 重建)", "跳过", "跳过", info=True)
    mans = [os.path.basename(p) for p in glob.glob(os.path.join(R, "transforms", "manifest_*.json"))]
    ctx["manifests"] = mans
    chk("E1", "transforms manifest 5 个 (S/V/W1/W2/Vbenign)", 5, len(mans))

    # ============ 2. E2/E6 生成数据 ============
    gen_side = {}
    for side, tag, exp_ex in [("gen", "chat", {"S": 0.4248, "V": 0.3856, "W2": 0.3682, "W1": 0.3825}),
                              ("gen_v4b", "v4b", {"S": 0.2823, "V": 0.3013, "W2": 0.2830, "W1": 0.3055})]:
        summary = load_json(os.path.join(side, "gen_summary.json"))
        agg = fam_agg_from_summary(summary)
        unw = unweighted_fam_ex(summary)
        fb, lb = collect_gen(side)
        ctx[f"gen_{tag}"] = {"summary": summary, "agg": agg, "files": fb, "lines": lb, "unweighted_ex": unw}
        for fam, exp_units in [("S", 900), ("V", 868), ("W2", 600), ("W1", 200)]:
            chk("E2/E6", f"{tag} {fam} 单元数 (sum questions)", exp_units, agg[fam]["q"])
            chk("E2/E6", f"{tag} {fam} 候选数 (x10)", exp_units * 10, agg[fam]["cand"])
            chk("E2/E6", f"{tag} {fam} jsonl 文件数", {"S": 30, "V": 29, "W2": 20, "W1": 11}[fam], fb.get(fam, 0))
            chk("E2/E6", f"{tag} {fam} jsonl 行数", exp_units * 10, lb.get(fam, 0))
            w_ex, u_ex = agg[fam]["ex_rate"], unw.get(fam)
            ok = chk("E2/E6", f"{tag} {fam} 分族精确匹配率 (加权口径优先)", exp_ex[fam], round(w_ex, 4), tol=0.002)
            if not ok:
                chk("E2/E6", f"{tag} {fam} 分族精确匹配率 (非加权口径)", exp_ex[fam], round(u_ex, 4), tol=0.002)
        chk("E2/E6", f"{tag} 候选总数 25,680", 25680, sum(a["cand"] for a in agg.values()))
        gen_side[tag] = True
    # W3 生成 (chat/v4b)
    for side, tag in [("gen_w3", "chat"), ("gen_w3b", "v4b")]:
        summary = load_json(os.path.join(side, "gen_summary.json"))
        agg = fam_agg_from_summary(summary)
        lines = 0
        files = glob.glob(os.path.join(R, side, "W3", "**", "*.jsonl"), recursive=True)
        for p in files:
            with open(p, encoding="utf-8") as f:
                lines += sum(1 for _ in f)
        ctx[f"gen_w3_{tag}"] = {"summary": summary, "agg": agg, "files": len(files), "lines": lines}
        chk("E9", f"W3 {tag} jsonl 文件数 20 (5 库 x ck0-3)", 20, len(files))
        chk("E9", f"W3 {tag} 候选数 5,640 (141x4x10)", 5640, agg["W3"]["cand"])
        chk("E9", f"W3 {tag} jsonl 行数 5,640", 5640, lines)
        chk("E9", f"W3 {tag} 单元数 564", 564, agg["W3"]["q"])
    # v4 (effort=low) 伪影数据 — 案例研究
    v4_files = glob.glob(os.path.join(R, "gen_v4", "**", "*.jsonl"), recursive=True)
    v4_lines = 0; v4_empty = 0; sql_key = "sql"
    for p in v4_files:
        with open(p, encoding="utf-8") as f:
            first = True
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                if first:
                    sql_key = "sql" if "sql" in d else ("content" if "content" in d else "sql")
                    first = False
                v4_lines += 1
                if not str(d.get(sql_key) or "").strip():
                    v4_empty += 1
    ctx["v4_artifact"] = {"files": len(v4_files), "lines": v4_lines, "empty": v4_empty}
    chk("E6", "gen_v4 (伪影) 总条数 8,429", 8429, v4_lines)
    chk("E6", "gen_v4 (伪影) 空内容条数 ~4,947 (59%)", 4947, v4_empty, tol=30, info=True)

    # ============ 3. E3/E7 judge ============
    for jdir, tag, exp_mean in [("gen/judge", "chat", {"S": 82.6, "V": 83.3, "W2": 86.6, "W1": 90.8}),
                                ("gen_v4b/judge", "v4b", {"S": 83.1, "V": 81.9, "W2": 85.9, "W1": 88.0})]:
        fam_p, famck_p, nf, nr = collect_judge(jdir)
        ctx[f"judge_{tag}"] = {"fam": fam_p, "famck": famck_p, "files": nf, "records": nr}
        chk("E3/E7", f"{tag} judge 文件数 90", 90, nf)
        chk("E3/E7", f"{tag} judge 记录数 2,568", 2568, nr)
        for fam, exp_n in [("S", 900), ("V", 868), ("W2", 600), ("W1", 200)]:
            chk("E3/E7", f"{tag} {fam} judge 记录数", exp_n, len(fam_p[fam]))
            chk("E3/E7", f"{tag} {fam} judge 均值置信", exp_mean[fam], round(mean(fam_p[fam]), 1), tol=0.05)
    for jdir, tag, exp_w3 in [("gen_w3/judge", "chat", [91.1, 90.2, 91.2]),
                              ("gen_w3b/judge", "v4b", [88.6, 89.1, 87.1])]:
        fam_p, famck_p, nf, nr = collect_judge(jdir, default_fam="W3")
        ctx[f"judge_w3_{tag}"] = {"fam": fam_p, "famck": famck_p, "files": nf, "records": nr}
        chk("E9", f"W3 {tag} judge 文件数 20", 20, nf)
        chk("E9", f"W3 {tag} judge 记录数 564", 564, nr)
        for ck, exp_v in zip([1, 2, 3], exp_w3):
            chk("E9", f"W3 {tag} judge ck{ck} 均值置信", exp_v, round(mean(famck_p[("W3", ck)]), 1), tol=0.05)

    # ============ 4. E4 检测器主表锚点 ============
    dr = ctx["E4"]
    g = dr["groups"]
    chk("E4", "组键 S/V/W2/SILENT/ALL", ["ALL", "S", "SILENT", "V", "W2"], sorted(g.keys()))
    chk("E4", "题级明细 450 行 (S+V+W2)", 450, len(dr["questions"]))
    anchors_e4 = [
        ("S", "D1_ex", 0.018, 0.0097), ("S", "D5_mode", 0.08, 0.2336),
        ("S", "D6_jac", 0.089, 0.5695), ("S", "D8_oracle", 1.0, 0.0),
        ("V", "D5_mode", 0.507, 0.25), ("V", "D6_jac", 0.423, 0.5273),
        ("W2", "D5_mode", 0.684, 0.1881), ("W2", "D6_jac", 0.789, 0.5569),
        ("SILENT", "D1_ex", 0.0, 0.0087), ("SILENT", "D5_mode", 0.544, 0.2227),
        ("SILENT", "D6_jac", 0.5, 0.5404), ("SILENT", "D8_oracle", 1.0, 0.0),
    ]
    for grp, det, pw, fr in anchors_e4:
        d = g[grp]["detectors"][det]
        chk("E4", f"{grp} {det} power", pw, d["power"], tol=1e-9)
        chk("E4", f"{grp} {det} FAR", fr, d["far"], tol=1e-9)
    chk("E4", "SILENT D5 n_detected=49", 49, g["SILENT"]["detectors"]["D5_mode"]["n_detected"])
    chk("E4", "SILENT 组 n_onset=90 (300 题)", 90, g["SILENT"]["n_onset"])
    chk("E4", "W1 聚合 3 难度行", 3, len(dr["w1_aggregate"]))
    chk("E4", "参数 TH_JAC=0.30", 0.3, dr["params"]["TH_JAC"], tol=1e-9)

    # ============ 5. E5 稳健性 ============
    rb = ctx["E5"]
    chk("E5", "base 与 E4 主表一致 (S/V/W2/SILENT x 8 检测器)", True,
        all(rb["base"][fam]["detectors"] == g[fam]["detectors"] for fam in ["S", "V", "W2", "SILENT"]))
    chk("E5", "敏感性扫描 30 组", 30, len(rb["sensitivity"]))
    ci_d5 = pct(rb["bootstrap_ci"]["SILENT"]["D5_mode"]["power"], 0.025), pct(rb["bootstrap_ci"]["SILENT"]["D5_mode"]["power"], 0.975)
    chk("E5", "SILENT D5 power 95% CI [0.44, 0.65]", (0.44, 0.65), (round(ci_d5[0], 2), round(ci_d5[1], 2)), tol=0.01)
    ci_d5s = pct(rb["bootstrap_ci"]["S"]["D5_mode"]["power"], 0.025), pct(rb["bootstrap_ci"]["S"]["D5_mode"]["power"], 0.975)
    chk("E5", "S D5 power 95% CI [0.04, 0.14]", (0.04, 0.14), (round(ci_d5s[0], 2), round(ci_d5s[1], 2)), tol=0.01)
    th5 = [s for s in rb["sensitivity"] if s["param"] == "TH_JAC=0.5"]
    chk("E5", "TH_JAC=0.5 扫描存在", 1, len(th5))
    if th5:
        chk("E5", "TH_JAC=0.5: D6 SILENT power 0.6", 0.6, th5[0]["SILENT_power"], tol=0.001)
        chk("E5", "TH_JAC=0.5: D6 SILENT FAR 0.265", 0.265, th5[0]["SILENT_far"], tol=0.001)

    # ============ 6. E8 跨生成器 ============
    cg = ctx["E8"]
    chk("E8", "同集题数 S/V/W2 = 150/118/150", {"S": 150, "V": 118, "W2": 150}, cg["subset_n"])
    chk("E8", "W1 200 题", 200, cg["W1_n"])
    for side in ["chat", "v4b"]:
        gg = cg["groups"][side]
        chk("E8", f"{side} SILENT 组题数 268", 268, gg["SILENT"]["n_questions"])
        chk("E8", f"{side} S D1 power (loud 双盲)", {"chat": 0.018, "v4b": 0.0}[side], gg["S"]["detectors"]["D1_ex"]["power"], tol=1e-9)
        chk("E8", f"{side} SILENT D5 power", {"chat": 0.535, "v4b": 0.549}[side], gg["SILENT"]["detectors"]["D5_mode"]["power"], tol=1e-9)
        chk("E8", f"{side} SILENT D6 power", {"chat": 0.493, "v4b": 0.521}[side], gg["SILENT"]["detectors"]["D6_jac"]["power"], tol=1e-9)
        chk("E8", f"{side} SILENT D1 power", {"chat": 0.0, "v4b": 0.014}[side], gg["SILENT"]["detectors"]["D1_ex"]["power"], tol=1e-9)
        chk("E8", f"{side} SILENT D3 power (judge 无信息)", {"chat": 0.085, "v4b": 0.099}[side], gg["SILENT"]["detectors"]["D3_judge"]["power"], tol=1e-9)
        chk("E8", f"{side} S D8 oracle power=1.0", 1.0, gg["S"]["detectors"]["D8_oracle"]["power"], tol=1e-9)
    chk("E8", "v4b SILENT D6 FAR 0.427", 0.427, cg["groups"]["v4b"]["SILENT"]["detectors"]["D6_jac"]["far"], tol=1e-9)
    chk("E8", "v4b W2 D5 power 0.789", 0.789, cg["groups"]["v4b"]["W2"]["detectors"]["D5_mode"]["power"], tol=1e-9)
    chk("E8", "v4b W2 D6 power 0.842", 0.842, cg["groups"]["v4b"]["W2"]["detectors"]["D6_jac"]["power"], tol=1e-9)
    chk("E8", "W1 难度分解 各 3 行", (3, 3), (len(cg["W1"]["chat"]), len(cg["W1"]["v4b"])))

    # ============ 7. E9 W3 负控制 ============
    w3 = ctx["E9"]
    for side in ["chat", "v4b"]:
        p = w3["sides"][side]["pooled"]
        chk("E9", f"{side} pooled 141 题 / 423 稳定检查点", (141, 423), (p["n_questions"], p["n_stable_ckpts"]))
        for det, far in [("D1_ex", {"chat": 0.0, "v4b": 0.031}[side]),
                         ("D2_ks", {"chat": 0.3735, "v4b": 0.47}[side]),
                         ("D3_judge", {"chat": 0.1324, "v4b": 0.14}[side]),
                         ("D4_share", {"chat": 0.1277, "v4b": 0.16}[side]),
                         ("D5_mode", {"chat": 0.3688, "v4b": 0.348}[side]),
                         ("D6_jac", {"chat": 0.617, "v4b": 0.553}[side]),
                         ("D7_cusum", {"chat": 0.1844, "v4b": 0.12}[side]),
                         ("D8_oracle", 0.0)]:
            chk("E9", f"{side} {det} 合并 FAR", far, p["detectors"][det]["far"], tol=0.005)
        chk("E9", f"{side} 分严重度 3 级", 3, len(w3["sides"][side]["by_severity"]))

    # ============ 8. E10 V-benign 控制 ============
    bc = ctx["E10"]
    ver = bc["build"]["verification"]
    tot_checked = sum(c["n_checked"] for db in ver.values() for c in db.values())
    tot_viol = sum(c["violations"] for db in ver.values() for c in db.values())
    chk("E10", "构建门槛: gold 哈希校验 750 (150 题 x 5 级), 违规 0", (750, 0), (tot_checked, tot_viol))
    for side in ["chat", "v4b"]:
        p = bc["sides"][side]["pooled"]
        chk("E10", f"{side} pooled 150 题 / 718 稳定检查点", (150, 718), (p["n_questions"], p["n_stable_ckpts"]))
        for det, far in [("D1_ex", {"chat": 0.0111, "v4b": 0.003}[side]),
                         ("D5_mode", {"chat": 0.2786, "v4b": 0.162}[side]),
                         ("D6_jac", {"chat": 0.5557, "v4b": 0.442}[side]),
                         ("D8_oracle", 0.0)]:
            chk("E10", f"{side} {det} pooled FAR", far, p["detectors"][det]["far"], tol=0.005)
        chk("E10", f"{side} 两阶段 gold 确认后 confirmed=0", 0, bc["sides"][side]["two_stage"]["confirmed"])
        chk("E10", f"{side} 分级 b1..b5", 5, len(bc["sides"][side]["by_level"]))
        chk("E10", f"{side} 分库 5 库", 5, len(bc["sides"][side]["per_db"]))
    chk("E10", "良性变化面 5 库", 5, len(bc["build"]["dbs"]))

    # ============ 9. E11 组合监测器 ============
    cm = ctx["E11"]
    t1 = cm["t1"]
    all_far0 = all(t1[sd][sc][cf]["two_stage_far"] == 0 for sd in t1 for sc in t1[sd] for cf in t1[sd][sc])
    chk("E11", "T1 两阶段 FAR 全部=0 (构造性)", True, all_far0)
    or_chat = t1["chat"]["SILENT"]["OR"]
    chk("E11", "T1 chat SILENT OR power 0.867 (锁存 0.5)", 0.867, or_chat["power"], tol=1e-9)
    chk("E11", "T1 chat SILENT OR 锁存 power 0.5", 0.5, or_chat["power_screen_latched"], tol=1e-9)
    chk("E11", "T1 chat SILENT OR gold 确认 500 次 / 300 题 (1.67/题)", (500, 1.67), (or_chat["gold_confirm_total"], or_chat["gold_confirm_per_q"]), tol=0.005)
    chk("E11", "T1 chat S OR power 0.589 (锁存 0.089, 6.6x)", 0.589, t1["chat"]["S"]["OR"]["power"], tol=1e-9)
    chk("E11", "T1 chat V OR power 0.873", 0.873, t1["chat"]["V"]["OR"]["power"], tol=1e-9)
    chk("E11", "T1 chat W2 OR power 0.842", 0.842, t1["chat"]["W2"]["OR"]["power"], tol=1e-9)
    chk("E11", "T1 v4b S OR power 0.455 (锁存 0.045, 10x)", (0.455, 0.045),
        (t1["v4b"]["S"]["OR"]["power"], t1["v4b"]["S"]["OR"]["power_screen_latched"]), tol=1e-9)
    chk("E11", "T1 chat W3 OR gold 确认 236 全拒绝", (236, 0),
        (t1["chat"]["W3"]["OR"]["gold_confirm_total"], t1["chat"]["W3"]["OR"]["confirmed"]))
    chk("E11", "T1 chat BENIGN OR gold 确认 324 全拒绝", (324, 0),
        (t1["chat"]["BENIGN"]["OR"]["gold_confirm_total"], t1["chat"]["BENIGN"]["OR"]["confirmed"]))
    t2 = cm["t2"]
    chk("E11", "T2 S m=1 power 0.761 [0.4,1.0]", (0.761, 0.4, 1.0),
        (t2["S"]["rows"]["1"]["power"], t2["S"]["rows"]["1"]["ci"][0], t2["S"]["rows"]["1"]["ci"][1]), tol=1e-9)
    chk("E11", "T2 S m=10 power 1.0", 1.0, t2["S"]["rows"]["10"]["power"], tol=1e-9)
    chk("E11", "T2 V m=1 power 0.455; m=25 1.0", (0.455, 1.0),
        (t2["V"]["rows"]["1"]["power"], t2["V"]["rows"]["25"]["power"]), tol=1e-9)
    chk("E11", "T2 W2 m=1 power 0.218; m=50 1.0", (0.218, 1.0),
        (t2["W2"]["rows"]["1"]["power"], t2["W2"]["rows"]["50"]["power"]), tol=1e-9)
    t3 = cm["t3"]
    chk("E11", "T3 归因 100%: rename_table 59 + rename_column 53 break, silent 0",
        {"rename_table": {"break": 59}, "rename_column": {"break": 53}}, t3["attribution"])
    chk("E11", "T3 未归因 0 / 受影响但稳定 0 / 未受影响对照 38", (0, 38),
        (len(t3["unattributed"]), t3["n_stable_unaffected"]))
    chk("E11", "T3 per 库-检查点 25 行", 25, len(t3["per_ck"]))
    t4 = cm["t4"]
    chk("E11", "T4 E4 对账 8/8 match", (8, True), (len(t4["E4"]), all(r["match"] for r in t4["E4"])))
    chk("E11", "T4 E9(W3) 对账 2/2 match", (2, True), (len(t4["E9"]), all(r["match"] for r in t4["E9"])))

    # ============ 10. E12 表级 PSI ============
    tp = ctx["E12"]
    sc, ev = tp["scenarios"], tp["events"]
    chk("E12", "阈值 [0.25, 0.1]", [0.25, 0.1], tp["thresholds"])
    e12_exp = {"V": (24, 10, 13, 0), "W2": (9, 1, 2, 0)}
    for fam, (n_ev, d25, d10, st) in e12_exp.items():
        got = (len(ev.get(fam, {})),
               sum(1 for v in sc[fam].values() if v["alarm"]["psi@0.25"]),
               sum(1 for v in sc[fam].values() if v["alarm"]["psi@0.1"]),
               sum(1 for v in sc[fam].values() if v["structural"]))
        chk("E12", f"{fam} (事件, PSI@0.25, PSI@0.1, 结构)", (n_ev, d25, d10, st), got)
    # S: 结构信号对 17 个事件点全部检出 (17/17); 告警总数 20 含 3 个零事件 schema 变化点
    s_det = sum(1 for k in ev["S"] if sc["S"][f"{k.split(':')[0]}:t{k.split(':')[1]}"]["structural"])
    chk("E12", "S 结构对 17 事件检出 17/17", 17, s_det)
    chk("E12", "S 结构告警总数 (信息: 含 3 个零事件 schema 变化点)", None,
        sum(1 for v in sc["S"].values() if v["structural"]), info=True)
    ben = sc["BENIGN"]
    chk("E12", "BENIGN 25 库-检查点, PSI@0.25 告警 0, @0.1 告警 1, 结构 0",
        (25, 0, 1, 0),
        (len(ben),
         sum(1 for v in ben.values() if v["alarm"]["psi@0.25"]),
         sum(1 for v in ben.values() if v["alarm"]["psi@0.1"]),
         sum(1 for v in ben.values() if v["structural"])))
    chk("E12", "V student_club:t3 max PSI 10.0373", 10.0373, sc["V"]["student_club:t3"]["max_psi"], tol=1e-9)
    chk("E12", "BENIGN max PSI (唯一 @0.1 误报为 superhero 复行)", None,
        round(max(v["max_psi"] for v in ben.values()), 4), info=True)

    # ============ 11. E13 阈值迁移 + bootstrap + EX ============
    p4 = ctx["E13"]
    a = p4["a_transfer"]["combos"]
    chk("E13", "K 网格 18 档 / E 网格 9 档 / FAR 预算 0.25", (18, 9, 0.25),
        (len(p4["params"]["k_grid"]), len(p4["params"]["e_grid"]), p4["params"]["far_budget"]))
    e13_exp = {
        "D6:SILENT": dict(oracle_th=0.8, oracle_power=0.622, oracle_far=0.0633, eval_power_mean=0.577,
                          eval_far_mean=0.1638, th_agreement=4, n_infeasible=0, n_splits=10),
        "D6:S": dict(oracle_th=0.6, oracle_power=0.125, oracle_far=0.1622, eval_power_mean=0.155,
                     th_agreement=5, n_infeasible=0),
        "D1:S": dict(oracle_th=0.1, oracle_power=0.071, oracle_far=0.0502, eval_power_mean=0.065, th_agreement=9),
        "D1:SILENT": dict(oracle_th=0.15, oracle_power=0.011, oracle_far=0.024, eval_power_mean=0.005, th_agreement=1),
    }
    for key, e in e13_exp.items():
        c = a[key]
        for k, v in e.items():
            if k == "n_splits":
                chk("E13", f"{key} 10 组 split", 10, len(c["splits"]))
            else:
                chk("E13", f"{key} {k}", v, c[k], tol=1e-9)
    chk("E13", "D6:SILENT 留出 P 范围 [0.426, 0.745]", [0.426, 0.745], a["D6:SILENT"]["eval_power_range"])
    chk("E13", "D6:SILENT TH 选项 [0.5,0.6,0.8,0.85]", [0.5, 0.6, 0.8, 0.85], a["D6:SILENT"]["th_choices"])
    b = p4["b_bootstrap"]
    chk("E13", "bootstrap B=1000", 1000, b["B"])
    e13_ci = {("S", "D5_mode"): (0.08, 0.009, 0.208), ("S", "D6_jac"): (0.089, 0.046, 0.149),
              ("V", "D5_mode"): (0.507, 0.3578, 0.683), ("V", "D6_jac"): (0.423, 0.3, 0.623),
              ("W2", "D5_mode"): (0.684, 0.0, 0.889), ("W2", "D6_jac"): (0.789, 0.5, 0.875),
              ("SILENT", "D5_mode"): (0.544, 0.356, 0.741), ("SILENT", "D6_jac"): (0.5, 0.33, 0.686)}
    for (fam, det), (pw, lo, hi) in e13_ci.items():
        d = b["per_family"][fam][det]
        chk("E13", f"bootstrap {fam} {det} power CI [{lo}, {hi}]", (pw, lo, hi),
            (d["power"], d["power_ci"][0], d["power_ci"][1]), tol=1e-9)
    chk("E13", "E4 存档对账 8/8 match", (8, True), (len(b["xcheck_e4"]), all(r["match"] for r in b["xcheck_e4"])))
    c = p4["c_v4b_ex"]
    for side, e in [("chat", dict(ex0=0.991, cor0=0.421, dcor=0.392, dex_post=3)),
                    ("v4b", dict(ex0=0.986, cor0=0.311, dcor=0.324, dex_post=0))]:
        s = c["sides"][side]
        chk("E13", f"EX 诊断 {side} ex0 均值 (exec 天花板)", e["ex0"], s["ex0_mean"], tol=1e-9)
        chk("E13", f"EX 诊断 {side} ex0<0.30 题数 0", 0, s["ex0_lt_030"])
        chk("E13", f"EX 诊断 {side} cor0 均值 (能力差距)", e["cor0"], s["cor0_mean"], tol=1e-9)
        chk("E13", f"EX 诊断 {side} Δcor@onset (correct 崩塌)", e["dcor"], s["decor_at_onset_mean"], tol=1e-9)
        chk("E13", f"EX 诊断 {side} onset 后 maxΔex≥0.30 题数", e["dex_post"], s["dex_post_max_ge_030"])
    chk("E13", "E8 存档对账 2/2 match", (2, True), (len(c["xcheck_e8"]), all(r["match"] for r in c["xcheck_e8"])))

    # ============ 12. REV 审稿修订零 API 重分析 ============
    rev = ctx["REV"]

    def _rev_xc_ok(v):
        if isinstance(v, dict):
            if "match" in v:
                ok = bool(v["match"])
                if "n_match" in v and "n" in v:
                    ok = ok and v["n_match"] == v["n"]
                return ok
            if "n_match" in v and "n" in v:
                return v["n_match"] == v["n"]
            return all(_rev_xc_ok(x) for x in v.values())
        if isinstance(v, (list, tuple)):
            return all(_rev_xc_ok(x) for x in v)
        return bool(v)

    chk("REV", "零 API 声明 (只读 gen/gen_v4b/gold/results 缓存)", True, rev["params"]["zero_api"])
    for k, v in rev["xcheck"].items():
        chk("REV", f"基线对账 {k}", True, _rev_xc_ok(v))
    p1o = rev["part1_ablation_2x2"]["SILENT"]["OR"]
    chk("REV", "SILENT OR Latched/无gold power 0.5 / 操作员FAR 0.5426",
        (0.5, 0.5426), (p1o["latched_nogold"]["power"], p1o["latched_nogold"]["op_far"]))
    chk("REV", "SILENT OR Continue/无gold power 0.867 / 逐单元FAR 0.4607 / 可见报警 626",
        (0.867, 0.4607, 626), (p1o["continue_nogold"]["power"], p1o["continue_nogold"]["op_far"], p1o["continue_nogold"]["visible_total"]))
    chk("REV", "SILENT OR Latched/有gold 功效不恢复 0.5 / 确认 45 / 拒绝 165",
        (0.5, 45, 165), (p1o["latched_gold"]["power"], p1o["latched_gold"]["confirmed"], p1o["latched_gold"]["rejected"]))
    chk("REV", "SILENT OR DriftGuard power 0.867 / FAR 0 / gold 500 (1.67/题) / 确认 78 / 拒绝 422",
        (0.867, 0, 500, 1.67, 78, 422), (p1o["continue_gold"]["power"], p1o["continue_gold"]["op_far"],
                                          p1o["continue_gold"]["gold_total"], p1o["continue_gold"]["gold_per_q"],
                                          p1o["continue_gold"]["confirmed"], p1o["continue_gold"]["rejected"]))
    p2b = rev["part2_hier_bootstrap"]
    dg = p2b["driftguard_or"]
    chk("REV", "M5 DriftGuard power 0.867 CI [0.7683, 0.9535]",
        (0.867, 0.7683, 0.9535), (dg["power_point"], dg["power_ci"][0], dg["power_ci"][1]))
    chk("REV", "M5 DriftGuard gold/q 1.67 CI [1.1558, 2.142]",
        (1.67, 1.1558, 2.142), (dg["goldpq_point"], dg["goldpq_ci"][0], dg["goldpq_ci"][1]))
    cn = p2b["continue_nogold"]
    chk("REV", "M5 Continue/无gold 操作员FAR 0.4607 CI [0.285, 0.6164]",
        (0.4607, 0.285, 0.6164), (cn["op_far_point"], cn["op_far_ci"][0], cn["op_far_ci"][1]))
    ln = p2b["latched_nogold"]
    chk("REV", "M5 Latched/无gold power 0.5 CI [0.3077, 0.7188]",
        (0.5, 0.3077, 0.7188), (ln["power_point"], ln["power_ci"][0], ln["power_ci"][1]))
    r1, r2 = rev["part2_ratio"]["G1"], rev["part2_ratio"]["G2"]
    chk("REV", "M5 G1 比值 6.6x CI [3.25, 19.6667] (1 个分母=0 draw)",
        (6.6, 3.25, 19.6667, 1), (r1["rounded_power_ratio"], r1["ratio_ci"][0], r1["ratio_ci"][1], r1["n_zero_denominator_draws"]))
    chk("REV", "M5 G2 比值 10.1x CI [3.6667, 45.1] (43 个分母=0 draws, 已披露)",
        (10.1, 3.6667, 45.1, 43), (r2["rounded_power_ratio"], r2["ratio_ci"][0], r2["ratio_ci"][1], r2["n_zero_denominator_draws"]))
    ze = rev["part2_zero_event"]
    chk("REV", "M2 良性确认 982 全部真拒绝 (SILENT 422 + W3 236 + BENIGN 324)",
        (982, 422, 236, 324), (ze["benign_confirmations_g1_or"]["total"], ze["benign_confirmations_g1_or"]["SILENT"],
                                ze["benign_confirmations_g1_or"]["W3"], ze["benign_confirmations_g1_or"]["BENIGN"]))
    chk("REV", "M2 假确认 0 / DB 级 cluster 上界 0.4507 (非 unit 级)",
        (0, 0.4507), (ze["false_confirmations"], ze["db_level_p_upper_95"]))
    pc = rev["part3_placebo"]["configs"]
    chk("REV", "M9 D6@0.30 f-hat 0.265 / swap 期望功率 0.561 [0.4664, 0.6667] / iid 0.549",
        (0.265, 0.561, 0.4664, 0.6667, 0.549), (pc["D6@0.30"]["f_hat"], pc["D6@0.30"]["swap_expected_power"],
                                                pc["D6@0.30"]["swap_power_ci"][0], pc["D6@0.30"]["swap_power_ci"][1],
                                                pc["D6@0.30"]["iid_expected_power"]))
    chk("REV", "M9 D5 f-hat 0.08 / swap 0.27 > iid 0.204 (iid 上界主张被否定)",
        (0.08, 0.27, 0.204), (pc["D5"]["f_hat"], pc["D5"]["swap_expected_power"], pc["D5"]["iid_expected_power"]))
    chk("REV", "M9 OR f-hat 独立估计 0.265 (恰与 D6 相同, 非重用)",
        (0.265, 0, 1), (pc["OR"]["f_hat"], rev["part3_placebo"]["or_vs_d6_diagnostic"]["or_first_earlier_than_d6_never_q"],
                        rev["part3_placebo"]["or_vs_d6_diagnostic"]["or_extra_alarm_units_vs_d6"]))
    p4r = rev["part4_lodo"]
    chk("REV", "M7 规则 R 留出 power 均值 0.608 / 最差 0.417 / FAR 最差 0.1818",
        (0.608, 0.417, 0.1818), (p4r["summary_R"]["power_mean"], p4r["summary_R"]["power_min"], p4r["summary_R"]["far_max"]))
    chk("REV", "M7 直用 K* 留出 power 均值 0.567 / 最差 0.208 / FAR 最差 0.4739",
        (0.567, 0.208, 0.4739), (p4r["summary_direct"]["power_mean"], p4r["summary_direct"]["power_min"], p4r["summary_direct"]["far_max"]))
    thf = [f for f in p4r["folds"] if f["heldout"] == "thrombosis_prediction"][0]
    chk("REV", "M7 thrombosis 折: R 升级 K 0.8 -> 0.417/0.1609 vs 直用 K* 0.5 -> 0.208/0.4739",
        (0.8, 0.417, 0.1609, 0.5, 0.208, 0.4739), (thf["r_threshold"], thf["held_power_R"], thf["held_far_R"],
                                                  thf["k_star"], thf["held_power_direct"], thf["held_far_direct"]))
    chk("REV", "M7 oracle K=0.8 P 0.622 / FAR 0.0633 (与 E13 存档一致)",
        (0.8, 0.622, 0.0633), (p4r["oracle"]["th"], p4r["oracle"]["power"], p4r["oracle"]["far"]))

    return ctx, FAILS, VAL_ROWS


if __name__ == "__main__":
    ctx, fails, rows = run()
    n_pass = sum(1 for r in rows if r["结果"] == "通过")
    n_info = sum(1 for r in rows if r["结果"] == "信息")
    print(f"\n===== 校验完成: {n_pass} 通过 / {fails} 失败 / {n_info} 信息, 共 {len(rows)} 项 =====")
    if fails:
        print("\n--- 失败项 ---")
        for r in rows:
            if r["结果"] == "失败":
                print(f"[{r['类别']}] {r['检查项']}: 预期 {r['预期']} / 实测 {r['实测']}")
    else:
        print("全部检查通过, 数据可用于导出。")
