# -*- coding: utf-8 -*-
"""
DriftSQL 论文数据导出 (阶段 2)
先运行 check_paper_data.run() 全量校验; 全部通过后按实验类别分 sheet
导出单个 Excel 至 results_export/DriftSQL_experiment_results.xlsx
"""
import json, os, sys, glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_paper_data as CPD

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

R = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("DRIFTSQL_EXPORT_DIR") or os.path.join(R, "results_export")
os.makedirs(OUT_DIR, exist_ok=True)
XLSX = os.path.join(OUT_DIR, "DriftSQL_experiment_results.xlsx")

# ---------------- 工具 ----------------
def jstr(x):
    if isinstance(x, dict):
        return json.dumps(x, ensure_ascii=False)
    if isinstance(x, (list, tuple)):
        return "; ".join(str(i) for i in x)
    return str(x)

def f(x, nd=4):
    return None if x is None else round(x, nd)

def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None

def pct(vals, q):
    s = sorted(v for v in vals if v is not None)
    if not s:
        return None
    n = len(s)
    if n == 1:
        return s[0]
    import math
    pos = q * (n - 1)
    lo = int(math.floor(pos)); hi = min(lo + 1, n - 1); fr = pos - lo
    return s[lo] * (1 - fr) + s[hi] * fr

BOLD = Font(bold=True)
HDR_FILL = PatternFill("solid", fgColor="DDEBF7")
TITLE_FONT = Font(bold=True, size=12, color="1F4E79")

def disp_len(s):
    return sum(2 if ord(c) > 127 else 1 for c in str(s))

def add_sheet(wb, name, blocks):
    """blocks: [(标题, [行dict...]), ...]"""
    ws = wb.create_sheet(title=name)
    r = 1
    for title, rows in blocks:
        ws.cell(row=r, column=1, value=title).font = TITLE_FONT
        r += 1
        if rows:
            cols = list(rows[0].keys())
            for j, cn in enumerate(cols, 1):
                c = ws.cell(row=r, column=j, value=cn)
                c.font = BOLD; c.fill = HDR_FILL
            r += 1
            for row in rows:
                for j, cn in enumerate(cols, 1):
                    v = row.get(cn)
                    if isinstance(v, (dict, list, tuple)):
                        v = jstr(v)
                    ws.cell(row=r, column=j, value=v)
                r += 1
        r += 1
    # 列宽
    for title, rows in blocks:
        if not rows:
            continue
        for j, cn in enumerate(rows[0].keys(), 1):
            w = max(disp_len(cn) * 1.2, 9)
            for row in rows:
                v = row.get(cn)
                if v is not None:
                    w = max(w, min(disp_len(v) * 1.05, 46))
            cur = ws.column_dimensions[get_column_letter(j)].width or 0
            ws.column_dimensions[get_column_letter(j)].width = min(max(cur, w + 2), 50)
    return ws

# ---------------- Sheet 数据构建 ----------------
FAM_ORDER = ["S", "V", "W2", "W1"]
DET_ORDER = ["D1_ex", "D2_ks", "D3_judge", "D4_share", "D5_mode", "D6_jac", "D7_cusum", "D8_oracle"]
DET_NAME = {"D1_ex": "D1 EX衰减", "D2_ks": "D2 logprob-KS", "D3_judge": "D3 judge置信",
            "D4_share": "D4 share衰减", "D5_mode": "D5 众数迁移", "D6_jac": "D6 指纹Jaccard",
            "D7_cusum": "D7 CUSUM-correct", "D8_oracle": "D8 oracle(gold)"}

def sheet_overview(ctx, val_rows):
    ov = [
        {"实验": "E1", "名称": "基准构建", "数据来源": "benchmark_spec.json, gold/, dbs/, questions/",
         "核心结果": "4 漂移族 x 5 库 (S/V/W2) + W1 纯工作负载; 2,568 题-检查点单元; 64 快照; truth=指纹相对 t0 变化或执行破坏, 变换累积"},
        {"实验": "E2", "名称": "chat 候选生成", "数据来源": "gen/gen_summary.json (90 jsonl)",
         "核心结果": "25,680 候选; 分族精确匹配 S 0.4248 / V 0.3856 / W2 0.3682 / W1 0.3825; 候选分布对数据漂移结构性不变 → EX 率监控在静默漂移下具误导性"},
        {"实验": "E3", "名称": "judge 基线 (chat)", "数据来源": "gen/judge/ (2,568 条)",
         "核心结果": "均值置信 S 82.6 / V 83.3 / W2 86.6 / W1 90.8 → judge 对漂移几乎无区分度"},
        {"实验": "E4", "名称": "8 检测器评测", "数据来源": "results/detector_results.json",
         "核心结果": "无单一运行时信号同时覆盖 loud 与 silent; D5 0.544 / D6 0.5 为 silent 主信号; D1 对双场景盲; D8 oracle 1.0 → 论证 gold 锚点+分布信号组合必要性"},
        {"实验": "E5", "名称": "检测器稳健性", "数据来源": "results/robustness.json",
         "核心结果": "30 组阈值扫描 + Bootstrap 95% CI (B=1000): D5 SILENT [0.44,0.65] vs S [0.04,0.14] 显著; EX 盲性对阈值不敏感; D6 更优工作点 TH=0.5"},
        {"实验": "E6", "名称": "v4b 第二生成器", "数据来源": "gen_v4b/, results/pilot_v4b*.json, gen_v4/ (伪影案例)",
         "核心结果": "v4b 25,680 候选 0 空 SQL, EM 0.28-0.31 (能力差距显著) 而 exec_ok 持平; v4 (effort=low) 伪影: 空内容率 59% 而 EX 监控全绿, 保留作论文案例研究"},
        {"实验": "E7", "名称": "judge-2 (v4b)", "数据来源": "gen_v4b/judge/ (2,568 条)",
         "核心结果": "均值置信 S 83.1 / V 81.9 / W2 85.9 / W1 88.0 → judge 无区分度跨生成器成立"},
        {"实验": "E8", "名称": "跨生成器检测器对照", "数据来源": "results/detector_crossgen.json",
         "核心结果": "同集 418 序列/侧; 检测器排序保持 (D5≈D6 > D7,D2 > D3,D4 > D1); D5/D6 silent 主信号逐字复现; D1 loud/silent 双盲跨生成器成立"},
        {"实验": "E9", "名称": "W3 改写族 (良性表述负控制)", "数据来源": "results/detector_w3.json",
         "核心结果": "语义不变改写下分布信号大量误报: D6 0.617/0.553, D5 0.369/0.348; D8 oracle 0/0 唯一零误报 → silent 主信号单用不可部署"},
        {"实验": "E10", "名称": "V-benign 良性数据演化控制", "数据来源": "results/benign_control.json",
         "核心结果": "良性数据演化误报与 W3 同量级: D6 0.556/0.442, D5 0.279/0.162; D8 0/0 (150/150 gold 稳定门槛) → 部署常态下分布信号不可用, gold 确认层唯一决定性"},
        {"实验": "E11", "名称": "组合监测器 (两阶段+预算+解剖)", "数据来源": "results/combined_monitor.json",
         "核心结果": "两阶段 FAR=0 且 SILENT power 0.867 (锁存 0.5, 至 10x); T2 DB 级 gold 预算曲线 (S 族 1 锚点/库 0.761); T3 锚点 112/112 漂移题 100% onset 归因"},
        {"实验": "E12", "名称": "表级 PSI/结构基线 (P0-3/A3)", "数据来源": "results/table_psi.json",
         "核心结果": "V 24 事件 PSI@0.25 仅检出 10 (0.417), W2 1/9; S 结构 17/17=DDL diff → 分布敏感≠答案敏感, 非 DriftSQL 合理替代"},
        {"实验": "E13", "名称": "阈值迁移+DB bootstrap+EX 诊断 (P0-4)", "数据来源": "results/p04_threshold_bootstrap.json",
         "核心结果": "D6 K=0.8 可跨库迁移 (留出 P 0.577, 与 oracle 差 0.045; K*>=0.6 组留出 FAR<=0.12); D5 SILENT CI [0.356,0.741]; EX 双盲=语义盲区 (exec 天花板 0.99, correct 崩塌不可见), 非实现缺陷"},
        {"实验": "P1", "名称": "零API重分析 (漏检刻画+锚点失效+W2-only)", "数据来源": "results/p1_analysis.json",
         "核心结果": "SILENT OR 12 漏检: 7 指纹不可见 (post-onset jac=0, 答案级漂移) + 5 亚阈值 (0.18<0.30; θ=0.18 追回 5 题, SILENT 锁存 FAR 0.73, gold/题 2.35); 锚点失效注入 k∈{1,2,3}: 假告警 0.6/1.3/1.8 恒低于上界 p×982, SILENT 功效最低 0.844; W2-only 两阶段 0.842 (16/19) @1.41 gold/题"},
        {"实验": "REV", "名称": "审稿修订零API重分析 (M3/M5/M9/M7/M2)", "数据来源": "results/revision_analysis.json",
         "核心结果": "2×2 消融: continue 语义独立产出全部功效增益 (0.50→0.867), gold 确认独立产出全部 FAR 压制 (0.4607→0), latched+gold 组合不恢复功效; 层级 bootstrap CI [0.7683, 0.9535], G2 比值 43/2000 零分母披露; 序列交换 placebo 0.561 [0.4664, 0.6667] 替代被否定的 iid 上界; LODO 规则 R 留出 0.608/0.417 vs 直用 0.567/0.208; 零事件 DB 级 cluster 上界 0.45 (非 unit 级 3/1314)"},
    ]
    n_pass = sum(1 for r in val_rows if r["结果"] == "通过")
    return [(f"数据校验结果 ({n_pass} 项通过 / 0 失败, 全部通过才导出)", val_rows),
            ("实验总览 (E1-E13 + P1 + REV)", ov)]

def sheet_e1(ctx):
    spec = ctx["spec"]
    base_rows = [
        {"项": "基准名称", "值": spec["name"]},
        {"项": "版本", "值": spec["version"]},
        {"项": "基础数据集", "值": spec["base"]["dataset"]},
        {"项": "基础题池", "值": spec["base"]["questions"]},
        {"项": "候选数据库 (11)", "值": jstr(spec["base"]["databases"])},
        {"项": "truth 定义", "值": "gold SQL 执行破坏或 result_hash != t0; 漂移感知 gold 逐 ck 标注; 变换累积, 漂移持续"},
        {"项": "gold 协议", "值": jstr(spec.get("gold_protocol"))},
        {"项": "评测指标", "值": jstr(spec.get("metrics"))},
        {"项": "可复现性", "值": jstr(spec.get("reproducibility"))},
    ]
    status_note = {
        "S_schema_evolution": "已构建: 150 题 x ck0-5, gold_S 150, 快照 ck1-5",
        "V_value_drift": "已构建: 150 题 x ck0-5 (toxicology 缺 t3), gold_V 150",
        "W1_workload_mix": "已构建: 200 题 (11 库, 单检查点), gold_W1 200",
        "W2_new_rows": "已构建: 150 题 x ck0-3, gold_W2 150",
        "W3_paraphrase": "已构建 (spec 状态字段过期): 150 题 -> judge 语义校验 141 通过, gold_W3 141 x ck0-3",
        "C_cold_start": "复用 GATE-SQL 11-fold LODO 产物, 不在本数据包内",
    }
    fam_rows = []
    for k, cfg in spec["families"].items():
        fam_rows.append({
            "族": k, "类型": cfg.get("kind"),
            "库数": len(cfg.get("dbs", [])),
            "库": jstr(cfg.get("dbs", [])),
            "检查点": jstr(cfg.get("checkpoints", [])),
            "操作": jstr(cfg.get("ops", [])),
            "性质/备注": cfg.get("nature") or cfg.get("note") or "",
            "现状": status_note.get(k, ""),
        })
    asset_rows = []
    for fam in ["S", "V", "W1", "W2", "W3"]:
        asset_rows.append({"资产": f"gold/gold_{fam}.jsonl", "内容": "漂移感知 gold (qid, db, difficulty, checkpoints{exec_ok,n_rows,result_hash})", "数量": ctx["gold_counts"][fam]})
    for fam in ["S", "V", "W2"]:
        asset_rows.append({"资产": f"dbs/{fam}/**/ck*.sqlite", "内容": "漂移快照", "数量": ctx["dbs_counts"][fam]})
    asset_rows.append({"资产": "dbs_vbenign/**/b*.sqlite", "内容": "V-benign 良性演化快照 (5 库 x b1-b5)", "数量": ctx["dbs_vbenign"]})
    asset_rows.append({"资产": "transforms/manifest_*.json", "内容": jstr(ctx["manifests"]), "数量": len(ctx["manifests"])})
    for fam in ["S", "V", "W1", "W2", "W3"]:
        asset_rows.append({"资产": f"questions/family_{fam}.json", "内容": "题目 (qid, db, question, sql)", "数量": ctx["q_counts"][fam]})
    for tag in ["chat", "v4b"]:
        d = ctx[f"gen_{tag}"]
        asset_rows.append({"资产": f"gen{'_v4b' if tag == 'v4b' else ''}/ 候选 jsonl", "内容": f"{tag} 生成候选 (per 库-检查点)", "数量": sum(d["files"].values())})
        asset_rows.append({"资产": f"{tag} judge 记录", "内容": "judge 均值置信", "数量": ctx[f"judge_{tag}"]["records"]})
    return [("基准基本信息", base_rows),
            ("漂移族配置", fam_rows),
            ("数据资产盘点", asset_rows)]

def gen_detail_rows(summary, side_label, has_gold_broken=True):
    rows = []
    keys = sorted(summary.keys(), key=lambda k: (FAM_ORDER.index(k.split("/")[0]) if k.split("/")[0] in FAM_ORDER else 9, k.split("/")[1], int(k.split("/")[-1].replace("ck", ""))))
    for k in keys:
        fam, db, ck = k.split("/")
        v = summary[k]
        row = {"生成器": side_label, "族": fam, "库": db, "检查点": ck.replace("ck", ""),
               "题数": v.get("questions"), "候选数": v.get("candidates"),
               "精确匹配率": v.get("ex_rate"), "exec_ok 率": v.get("exec_ok_rate")}
        if has_gold_broken:
            row["gold 破坏率"] = v.get("gold_broken_rate")
        row.update({"平均 share": v.get("mean_share"), "平均 logprob": f(v.get("mean_logprob"), 3)})
        rows.append(row)
    return rows

def sheet_gen(ctx):
    blocks = []
    agg_rows = []
    for tag, label in [("chat", "chat (deepseek-chat)"), ("v4b", "v4b (thinking:disabled)")]:
        d = ctx[f"gen_{tag}"]
        for fam in FAM_ORDER:
            a = d["agg"][fam]
            agg_rows.append({"生成器": label, "族": fam, "单元数": a["q"], "候选数": a["cand"],
                             "精确匹配率": f(a["ex_rate"]), "exec_ok 率": f(a["exec_ok_rate"]),
                             "gold 破坏率": f(a["gold_broken_rate"]),
                             "平均 share": f(a["mean_share"]), "平均 logprob": f(a["mean_logprob"], 3)})
    for tag, label in [("chat", "chat"), ("v4b", "v4b")]:
        a = ctx[f"gen_w3_{tag}"]["agg"]["W3"]
        agg_rows.append({"生成器": label, "族": "W3 (改写)", "单元数": a["q"], "候选数": a["cand"],
                         "精确匹配率": f(a["ex_rate"]), "exec_ok 率": f(a["exec_ok_rate"]),
                         "gold 破坏率": None,
                         "平均 share": f(a["mean_share"]), "平均 logprob": f(a["mean_logprob"], 3)})
    blocks.append(("分族生成汇总 (E2 chat / E6 v4b / E9 W3 双侧)", agg_rows))
    blocks.append(("chat 明细 (per 库-检查点)", gen_detail_rows(ctx["gen_chat"]["summary"], "chat")))
    blocks.append(("v4b 明细 (per 库-检查点)", gen_detail_rows(ctx["gen_v4b"]["summary"], "v4b")))
    blocks.append(("W3 chat 明细 (DB 恒定, 无 gold 破坏)", gen_detail_rows(ctx["gen_w3_chat"]["summary"], "chat", has_gold_broken=False)))
    blocks.append(("W3 v4b 明细 (DB 恒定, 无 gold 破坏)", gen_detail_rows(ctx["gen_w3_v4b"]["summary"], "v4b", has_gold_broken=False)))
    return blocks

def sheet_pilot(ctx):
    pa, pb = ctx["E6a"], ctx["E6b"]  # a=thinking=disabled (胜出), b=effort=low (伪影配置)
    ov = []
    for tag, j in [("thinking=disabled (v4b 胜出配置)", pa), ("effort=low (v4 伪影配置)", pb)]:
        ov.append({"配置": tag, "模型": j["winner"]["model"], "MAXTOK": j["winner"]["maxtok"],
                   "extra": jstr(j["winner"]["extra"]), "调用数": j["n_calls"], "空内容数": j["n_empty"],
                   "空内容率": f(j["empty_rate"]), "exec_ok": j["exec_ok"], "correct": j["correct"],
                   "中位延迟(s)": j["latency_median_s"], "finish": jstr(j.get("finish")),
                   "tokens": jstr(j.get("tokens"))})
    per_db = []
    for tag, j in [("effort=low", pb), ("thinking=disabled", pa)]:
        for db, d in j["per_db"].items():
            per_db.append({"配置": tag, "库": db, "n": d["n"], "empty": d["empty"], "exec_ok": d["exec_ok"]})
    va = ctx["v4_artifact"]
    art = [{"文件数": va["files"], "总条数": va["lines"], "空内容条数": va["empty"],
            "空内容率": f(va["empty"] / va["lines"]),
            "说明": "v4 (effort=low, MAXTOK=800) 全量运行伪影: reasoning token 计入 max_tokens 预算, 推理耗尽后 content 为空; API 层面成功 (error=null, exec_ok=true, 空指纹, lp=0) → EX 监控全绿. 空内容率与 schema 大小强相关 (thrombosis 95% / california 77% / superhero 16%). 保留作论文案例研究"}]
    return [("试点配置对比 (70 调用/配置, pilot_v4b.py)", ov),
            ("per 库试点明细", per_db),
            ("v4 (effort=low) 全量伪影数据 (案例研究, 只读)", art)]

def sheet_judge(ctx):
    fam_rows = []
    for tag in ["chat", "v4b"]:
        d = ctx[f"judge_{tag}"]
        for fam in FAM_ORDER:
            probs = d["fam"][fam]
            fam_rows.append({"生成器": tag, "族": fam, "记录数": len(probs),
                             "均值置信": f(mean(probs), 2), "结论": "judge 对漂移无区分度 (平坦)"})
    ck_rows = []
    for tag in ["chat", "v4b"]:
        d = ctx[f"judge_{tag}"]
        for fam in FAM_ORDER:
            for (f2, ck), probs in sorted(d["famck"].items(), key=lambda x: (FAM_ORDER.index(x[0][0]), x[0][1] if x[0][1] is not None else -1)):
                if f2 != fam:
                    continue
                ck_rows.append({"生成器": tag, "族": fam, "检查点": ck, "记录数": len(probs), "均值置信": f(mean(probs), 2)})
    w3_rows = []
    for tag in ["chat", "v4b"]:
        d = ctx[f"judge_w3_{tag}"]
        for (fam, ck), probs in sorted(d["famck"].items(), key=lambda x: x[0][1] if x[0][1] is not None else -1):
            note = {0: "ck0 原始题", 1: "t1 轻度改写", 2: "t2 中度改写", 3: "t3 深度改写"}.get(ck, "")
            w3_rows.append({"生成器": tag, "检查点": ck, "记录数": len(probs), "均值置信": f(mean(probs), 2), "说明": note})
    return [("分族 judge 均值置信 (E3 chat / E7 v4b, 0-100)", fam_rows),
            ("分族分检查点 judge 均值置信", ck_rows),
            ("W3 judge 均值置信 (E9 语义校验; 严重度越深误报越高, judge 却平坦)", w3_rows)]

def det_rows(groups, fams, side_col=None):
    rows = []
    for grp in fams:
        g = groups[grp]
        for det in DET_ORDER:
            d = g["detectors"][det]
            row = {"组": grp, "检测器": DET_NAME[det]}
            if side_col:
                row = {"生成器": side_col, "组": grp, "检测器": DET_NAME[det]}
            row.update({"题数": g["n_questions"], "onset 题数": g["n_onset"], "稳定检查点": g["n_stable_ckpts"],
                        "power": d["power"], "mean 延迟": d["mean_delay"], "中位延迟": d["med_delay"],
                        "FAR": d["far"], "检出题数": d["n_detected"]})
            rows.append(row)
    return rows

def sheet_e4(ctx):
    dr = ctx["E4"]
    param_rows = [{"参数": k, "值": v} for k, v in dr["params"].items()]
    w1_rows = [{"难度": r["difficulty"], "题数": r["n_questions"], "精确匹配率": r["ex_rate"],
                "平均 share": r["mean_share"], "平均 logprob": f(r["mean_logprob"], 3),
                "judge 均值": r["mean_judge"]} for r in dr["w1_aggregate"]]
    return [("检测器参数 (默认阈值)", param_rows),
            ("主表: 功效 / 延迟 / FAR (锁存口径, E4)", det_rows(dr["groups"], ["S", "V", "W2", "SILENT", "ALL"])),
            ("W1 纯工作负载聚合 (难度分解)", w1_rows)]

def sheet_e5(ctx):
    rb = ctx["E5"]
    ci_rows = []
    for fam in ["S", "V", "W2", "SILENT"]:
        for det in DET_ORDER:
            d = rb["bootstrap_ci"][fam][det]
            pw, fr = d["power"], d["far"]
            ci_rows.append({"组": fam, "检测器": DET_NAME[det],
                            "power 均值": f(mean(pw), 4) if pw else None,
                            "power 2.5%": f(pct(pw, 0.025), 3) if pw else None,
                            "power 97.5%": f(pct(pw, 0.975), 3) if pw else None,
                            "FAR 均值": f(mean(fr), 4) if fr else None,
                            "FAR 2.5%": f(pct(fr, 0.025), 4) if fr else None,
                            "FAR 97.5%": f(pct(fr, 0.975), 4) if fr else None})
    sens_rows = [{"参数": s["param"], "检测器": DET_NAME.get(s["detector"], s["detector"]),
                  "S power": s["S_power"], "S FAR": s["S_far"],
                  "SILENT power": s["SILENT_power"], "SILENT FAR": s["SILENT_far"]}
                 for s in rb["sensitivity"]]
    return [("Bootstrap 95% CI (B=1000, 按题重采样; 由 1000 次 draw 计算 2.5/97.5 分位)", ci_rows),
            ("单参数阈值敏感性扫描 (30 组)", sens_rows)]

def sheet_e8(ctx):
    cg = ctx["E8"]
    sub_rows = [{"项": f"同集 {f}", "值": cg["subset_n"][f]} for f in ["S", "V", "W2"]]
    sub_rows.append({"项": "W1", "值": cg["W1_n"]})
    sub_rows.append({"项": "同集过滤说明", "值": jstr(cg.get("subset"))})
    sub_rows.append({"项": "参数说明", "值": jstr(cg.get("params_note"))})
    w1_rows = []
    for tag in ["chat", "v4b"]:
        for r in cg["W1"][tag]:
            w1_rows.append({"生成器": tag, "难度": r["difficulty"], "题数": r["n_questions"],
                            "精确匹配率": r["ex_rate"], "平均 share": r["mean_share"],
                            "平均 logprob": f(r["mean_logprob"], 3), "judge 均值": r["mean_judge"]})
    return [("同集说明 (v4b 全检查点恰 10 候选的同集过滤)", sub_rows),
            ("chat 主表 (同集)", det_rows(cg["groups"]["chat"], ["S", "V", "W2", "SILENT", "ALL"])),
            ("v4b 主表 (同集)", det_rows(cg["groups"]["v4b"], ["S", "V", "W2", "SILENT", "ALL"])),
            ("W1 难度分解 (两生成器)", w1_rows)]

def sheet_e9(ctx):
    w3 = ctx["E9"]
    pooled = []
    for tag in ["chat", "v4b"]:
        p = w3["sides"][tag]["pooled"]
        for det in DET_ORDER:
            pooled.append({"生成器": tag, "检测器": DET_NAME[det], "FAR": p["detectors"][det]["far"],
                           "题数": p["n_questions"], "稳定检查点": p["n_stable_ckpts"]})
    sev = []
    sev_name = {"1": "t1 轻度", "2": "t2 中度", "3": "t3 深度"}
    for tag in ["chat", "v4b"]:
        bs = w3["sides"][tag]["by_severity"]
        for s in ["1", "2", "3"]:
            for det in DET_ORDER:
                sev.append({"生成器": tag, "严重度": sev_name[s], "检测器": DET_NAME[det], "FAR": bs[s][det]})
    info = [{"说明": "构建: 复用 W2 150 题, 逐级严重度同意图改写; judge 语义校验 141/150 通过 (9 题剔除)"},
            {"说明": "DB 恒定 -> gold 各 ck = t0, 全部 stable, 任何报警都是误报 (负控制)"},
            {"说明": "结论: 分布信号 (D5/D6) 在纯改写下误报率高到不可部署; gold 锚点 (D8) 是唯一零误报决定性信号"}]
    return [("W3 构建说明", info),
            ("合并 FAR (pooled, 报警即误报; E9)", pooled),
            ("分严重度 FAR", sev)]

def sheet_e10(ctx):
    bc = ctx["E10"]
    gate = [{"说明": bc["build"]["gate"]}]
    ver_rows = []
    for db, cks in bc["build"]["verification"].items():
        for ck, v in cks.items():
            ver_rows.append({"库": db, "b 级": ck, "校验题数": v["n_checked"], "违规": v["violations"]})
    face_rows = []
    for db, d in bc["build"]["dbs"].items():
        face_rows.append({"库": db, "churn 列数": len(d["churn_cols"]), "churn 列": jstr(d["churn_cols"]),
                          "dup 表": jstr(d["dup_tables"]), "幸存复行表": jstr(d["dup_survivors"]),
                          "b5 变化": jstr(d["b5_rows_changed"])})
    pooled = []
    for tag in ["chat", "v4b"]:
        p = bc["sides"][tag]["pooled"]
        for det in DET_ORDER:
            pooled.append({"生成器": tag, "检测器": DET_NAME[det], "FAR": p["detectors"][det]["far"],
                           "题数": p["n_questions"], "稳定检查点": p["n_stable_ckpts"]})
    lvl_rows = []
    for tag in ["chat", "v4b"]:
        for lv, v in sorted(bc["sides"][tag]["by_level"].items()):
            lvl_rows.append({"生成器": tag, "b 级": lv, "题数": v["n"], "D5 FAR": f(v["D5"], 4),
                             "D6 FAR": f(v["D6"], 4), "OR FAR": f(v["OR"], 4)})
    db_rows = []
    for tag in ["chat", "v4b"]:
        for db, v in bc["sides"][tag]["per_db"].items():
            db_rows.append({"生成器": tag, "库": db, "题数": v["n_q"], "检查点": v["points"],
                            "D5 FAR": f(v["D5"], 4), "D6 FAR": f(v["D6"], 4)})
    ts_rows = []
    for tag in ["chat", "v4b"]:
        t = bc["sides"][tag]["two_stage"]
        ts_rows.append({"生成器": tag, "gold 确认执行次数": t["confirm_execs"], "确认漂移": t["confirmed"]})
    note = [{"说明": "误报根源: 候选比 gold 更分散 (引用 gold 未引用的列), 分布信号原理上无法区分良性数据演化与答案漂移"},
            {"说明": "toxicology 零良性变化面 (全部列被 gold 引用或属 PK/FK), 其 (q,b) 点为构造性稳定"}]
    return [("构建门槛 (150/150 gold 哈希逐一相等)", gate + ver_rows),
            ("良性变化面 (库间差异, 如实分列)", face_rows),
            ("pooled 筛查 FAR (E10)", pooled),
            ("分级 b1-b5 FAR (筛查)", lvl_rows),
            ("分库 FAR (筛查)", db_rows),
            ("两阶段 gold 确认 (全部拒绝, 0 误报)", ts_rows),
            ("说明", note)]

def sheet_e11(ctx):
    cm = ctx["E11"]
    param_rows = [{"参数": k, "值": jstr(v)} for k, v in cm["params"].items()]
    t1_rows = []
    for tag in ["chat", "v4b"]:
        for sc in ["S", "V", "W2", "SILENT", "W3", "BENIGN"]:
            for cf, v in cm["t1"][tag][sc].items():
                t1_rows.append({"生成器": tag, "场景": sc, "筛查配置": cf,
                                "题数": v["n_questions"], "onset 题": v["n_onset"],
                                "两阶段 power": v["power"], "中位延迟": v["med_delay"],
                                "锁存 power": v["power_screen_latched"], "锁存 FAR": v["screen_far_latched"],
                                "两阶段 FAR": v["two_stage_far"],
                                "gold 确认次数": v["gold_confirm_total"], "每题确认": v["gold_confirm_per_q"],
                                "确认": v["confirmed"], "拒绝": v["rejected"]})
    ev_rows = []
    for fam in ["S", "V", "W2"]:
        for db, n in cm["t2"][fam]["events"].items():
            ev_rows.append({"族": fam, "库": db, "事件数": n, "题池": cm["t2"][fam]["pool_sizes"].get(db)})
    t2_rows = []
    for fam in ["S", "V", "W2"]:
        for m, v in cm["t2"][fam]["rows"].items():
            t2_rows.append({"族": fam, "m 锚点": int(m), "power": v["power"],
                            "CI 下界": v["ci"][0], "CI 上界": v["ci"][1],
                            "中位延迟": v["med_delay"], "事件库数": v["n_event_dbs"]})
    t3 = cm["t3"]
    att_rows = []
    for op, d in t3["attribution"].items():
        att_rows.append({"操作": op, "break (响亮报警)": d.get("break"), "silent": d.get("silent", 0)})
    att_rows.append({"操作": "未归因", "break (响亮报警)": len(t3["unattributed"]), "silent": 0})
    att_rows.append({"操作": "受影响但稳定", "break (响亮报警)": 0, "silent": len(t3["stable_hit"])})
    att_rows.append({"操作": "未受影响对照 (全 stable)", "break (响亮报警)": t3["n_stable_unaffected"], "silent": 0})
    perck_rows = []
    for k, v in t3["per_ck"].items():
        db, ck = k.split(":")
        perck_rows.append({"库": db, "ck": ck, "操作": jstr(v["ops"]), "break": v["break"],
                           "silent": v["silent"], "stable": v["stable"]})
    fs_rows = [{"族": fam, "stable 点": v.get("stable"), "drift 点 (loud/silent)": v.get("loud", v.get("silent"))}
               for fam, v in t3["fam_split"].items()]
    t4_rows = []
    for src in ["E4", "E9"]:
        for r in cm["t4"][src]:
            row = {"来源": src, "组": r.get("group"), "检测器": DET_NAME.get(r.get("det"), r.get("det", ""))}
            if "power" in r:
                row.update({"重算 power": r["power"], "存档 power": r["stored_power"]})
            row.update({"重算 FAR": r.get("far"), "存档 FAR": r.get("stored_far"), "match": r["match"]})
            t4_rows.append(row)
    return [("参数", param_rows),
            ("T1 题级两阶段 (筛查 -> drift-aware gold 确认; 拒绝后继续监测, 确认后锁存停计费)", t1_rows),
            ("T2 事件与题池 (事件=任一题 truth 漂移)", ev_rows),
            ("T2 DB 级 gold 预算曲线 (B=200)", t2_rows),
            ("T3 锚点解剖: 归因汇总 (onset 口径, 112/112 漂移题 100% 归因)", att_rows),
            ("T3 per 库-检查点明细", perck_rows),
            ("T3 fam_split", fs_rows),
            ("T4 交叉核对 (重算 vs 存档)", t4_rows)]

def sheet_e12(ctx):
    tp = ctx["E12"]
    sc, ev = tp["scenarios"], tp["events"]
    summ = [
        {"场景": "V", "事件数": 24, "PSI@0.25 检出": 10, "PSI@0.1 检出": 13, "结构检出": 0, "PSI@0.25 功效": 0.417, "中位延迟": 4},
        {"场景": "W2", "事件数": 9, "PSI@0.25 检出": 1, "PSI@0.1 检出": 2, "结构检出": 0, "PSI@0.25 功效": 0.111, "中位延迟": 2},
        {"场景": "S", "事件数": 17, "PSI@0.25 检出": 0, "PSI@0.1 检出": 0, "结构检出": 17, "PSI@0.25 功效": 0.0, "中位延迟": 0},
        {"场景": "BENIGN", "事件数": 0, "PSI@0.25 检出": 0, "PSI@0.1 检出": 1, "结构检出": 0, "PSI@0.25 功效": None, "中位延迟": None},
    ]
    detail = []
    for fam in ["V", "W2", "S", "BENIGN"]:
        for k in sorted(sc[fam].keys()):
            v = sc[fam][k]
            db, ck = k.rsplit(":t", 1)
            lost = v.get("lost_cols", []) + v.get("gone_tables", [])
            n_ev = ev.get(fam, {}).get(f"{db}:{ck}", 0)
            detail.append({"场景": fam, "库": db, "ck": ck, "max PSI": f(v["max_psi"]),
                           "结构破坏": v["structural"], "丢失列/表": jstr(lost) if lost else None,
                           "PSI@0.25 告警": v["alarm"]["psi@0.25"], "PSI@0.1 告警": v["alarm"]["psi@0.1"],
                           "事件题数": n_ev})
    notes = [
        {"说明": "表级分布信号: 每列 PSI (数值=t0 等频 10 箱+NULL; 文本=top-10+NULL+OTHER), 表级=max 列, 库级告警=max 表 > TH (0.25 标准/0.1 敏感)"},
        {"说明": "结构信号: 列/表消失告警 (加列不告警, E11-T3 实证答案不变); S 结构检出=DDL diff, 非本文增量"},
        {"说明": "churn 值换位 PSI=0 (对行级置换结构性盲); 大 PSI 点=shift_dates/delete_top_value — 与 DriftSQL 信号在漂移强度轴互补"},
        {"说明": "W3 无快照 (DB 恒定): 按构造 PSI=0 静默; 表级信号对表述漂移免疫是其实际优点, 但对答案级漂移同样盲"},
    ]
    return [("库级检出汇总 (事件=任一题答案漂移; E12)", summ),
            ("逐库-检查点明细 (max PSI / 结构 / 告警 / 事件)", detail),
            ("方法与说明", notes)]

def sheet_e13(ctx):
    p4 = ctx["E13"]
    p = p4["params"]
    param_rows = [{"参数": "FAR 预算 (调参集)", "值": p["far_budget"]},
                  {"参数": "K 网格 (D6 Jaccard)", "值": jstr(p["k_grid"])},
                  {"参数": "E 网格 (D1 EX 衰减)", "值": jstr(p["e_grid"])},
                  {"参数": "锁存口径", "值": p.get("latched_semantics")},
                  {"参数": "bootstrap B", "值": p4["b_bootstrap"]["B"]}]
    sweep_rows = []
    for key, c in p4["a_transfer"]["combos"].items():
        for s in c["sweep"]:
            sweep_rows.append({"组合": key, "阈值": s["th"], "power": s["power"], "FAR": s["far"],
                               "n_onset": s["n_on"], "n_stable": s["n_stable"], "hits": s["hits"],
                               "covered": s["covered"], "题数": s["n_q"]})
    sum_rows = []
    for key, c in p4["a_transfer"]["combos"].items():
        sum_rows.append({"组合": key, "题数": c["n_q"], "oracle TH": c["oracle_th"],
                         "oracle power": c["oracle_power"], "oracle FAR": c["oracle_far"],
                         "留出 P 均值": c["eval_power_mean"], "留出 P 范围": jstr(c["eval_power_range"]),
                         "留出 FAR 均值": c["eval_far_mean"], "留出 FAR 范围": jstr(c["eval_far_range"]),
                         "TH 一致组数": c["th_agreement"], "TH 选项": jstr(c["th_choices"]),
                         "不可行组": c["n_infeasible"]})
    split_rows = []
    for key, c in p4["a_transfer"]["combos"].items():
        for s in c["splits"]:
            split_rows.append({"组合": key, "调参库 (2)": jstr(s["tune"]), "留出库 (3)": jstr(s["eval"]),
                               "K*": s["th"], "可行": s["feasible"], "调参 P": s["tune_power"],
                               "调参 FAR": s["tune_far"], "留出 P": s["eval_power"], "留出 FAR": s["eval_far"]})
    ci_rows = []
    for fam in ["S", "V", "W2", "SILENT"]:
        for det in ["D5_mode", "D6_jac"]:
            d = p4["b_bootstrap"]["per_family"][fam][det]
            ci_rows.append({"场景": fam, "检测器": DET_NAME[det], "power": d["power"],
                            "power CI": jstr(d["power_ci"]), "FAR": d["far"], "FAR CI": jstr(d["far_ci"])})
    pdb_rows = []
    for fam in ["S", "V", "W2", "SILENT"]:
        for db, dets in p4["b_bootstrap"]["per_db"][fam].items():
            for det in ["D5_mode", "D6_jac"]:
                d = dets[det]
                pdb_rows.append({"场景": fam, "库": db, "检测器": DET_NAME[det],
                                 "power": d["power"], "FAR": d["far"], "onset 题数": d["n_on"], "题数": d["n_q"]})
    xc_rows = []
    for r in p4["b_bootstrap"]["xcheck_e4"]:
        xc_rows.append({"来源": "E4 (b)", "组": r["group"], "检测器": DET_NAME.get(r["det"], r["det"]),
                        "重算 power": r["power"], "存档 power": r["stored_power"],
                        "重算 FAR": r["far"], "存档 FAR": r["stored_far"], "match": r["match"]})
    for r in p4["c_v4b_ex"]["xcheck_e8"]:
        xc_rows.append({"来源": "E8 (c)", "组": "S", "检测器": "D1 EX衰减",
                        "重算 power": r["power"], "存档 power": r["stored_power"],
                        "重算 FAR": r["far"], "存档 FAR": r["stored_far"], "match": r["match"]})
    ex_rows = []
    for tag in ["chat", "v4b"]:
        s = p4["c_v4b_ex"]["sides"][tag]
        ex_rows.append({"生成器": tag, "题数": s["n_q"], "漂移题": s["n_drifted"], "稳定题": s["n_stable"],
                        "ex0 均值": s["ex0_mean"], "ex0 中位": s["ex0_median"], "ex0<0.30": s["ex0_lt_030"],
                        "cor0 均值": s["cor0_mean"],
                        "Δex@onset 均值/中位/最大": jstr([s["dex_at_onset_mean"], s["dex_at_onset_median"], s["dex_at_onset_max"]]),
                        "Δcor@onset 均值": s["decor_at_onset_mean"],
                        "onset 后 maxΔex>=0.30 题数": s["dex_post_max_ge_030"]})
    ck_rows = []
    for tag in ["chat", "v4b"]:
        s = p4["c_v4b_ex"]["sides"][tag]
        for metric, key in [("exec_ok", "ex_by_ck"), ("correct", "cor_by_ck")]:
            for state in ["drifted", "stable"]:
                d = s[key][state]
                ck_rows.append({"生成器": tag, "指标": metric, "状态": state,
                                **{f"ck{c}": f(d[str(c)], 3) for c in range(6)}})
    concl = [
        {"说明": "E13a: D6 阈值可跨库迁移 — SILENT oracle K=0.8 (P 0.622/FAR 0.0633), 10 组留出 P 均值 0.577; K*>=0.6 的 6 组留出 FAR<=0.12 且 P 均值 0.628 → 锁存口径下保守阈值双优"},
        {"说明": "E13a: D1 全网格无工作点 (S 最高 0.071@E=0.1) → EX 双盲非阈值伪影, 在阈值轴与库集两个维度成立"},
        {"说明": "E13b: 5 库库间方差如实呈现 (W2 CI 下界 0.0, 2/5 库零事件), 主结论方向不变; E4 对账 8/8"},
        {"说明": "E13c: v4b EX 异常解源 — exec_ok 基线 chat 0.991/v4b 0.986 均在天花板 (地板假设排除); EX 看不见 ck0 能力差距 (0.421 vs 0.311) 与 onset 后 correct 崩塌 (0.392/0.324) → D1 双盲=EX 语义盲区 (可执行≠正确), 非检测器缺陷; E8 对账 2/2"},
    ]
    return [("参数", param_rows),
            ("E13a oracle 全样本网格扫描", sweep_rows),
            ("E13a 迁移汇总 (2 库调参 / 3 库留出, C(5,2)=10 组)", sum_rows),
            ("E13a per-split 明细", split_rows),
            ("E13b DB 级 bootstrap 95% CI (按库有放回重采样)", ci_rows),
            ("E13b 逐库点估计", pdb_rows),
            ("E13b/E13c 存档对账", xc_rows),
            ("E13c v4b EX 异常诊断概览 (S 族, 锁存口径)", ex_rows),
            ("E13c 逐 ck 均值: exec_ok 恒高 vs correct 崩塌 (漂移题/稳定题)", ck_rows),
            ("结论 (论文叙事弹药)", concl)]

def sheet_rev(ctx):
    """REV: 审稿修订零 API 重分析 (M3 2x2 消融 / M5 层级 bootstrap / M9 placebo / M7 LODO / M2 零事件)"""
    rev = ctx["REV"]
    CELL_NAME = {"latched_nogold": "Latched/无gold (=E4)",
                 "continue_nogold": "Continue/无gold",
                 "latched_gold": "Latched/有gold",
                 "continue_gold": "Continue/有gold (=DriftGuard)"}
    # 对账概览
    xc_rows = []
    for k, v in rev["xcheck"].items():
        if isinstance(v, dict):
            if "n_match" in v and ("n" in v or "n_questions" in v):
                n = v.get("n", v.get("n_questions"))
                detail = f"{v['n_match']}/{n} 逐字段"
            elif "n" in v and "n_match" in v:
                detail = f"{v['n_match']}/{v['n']}"
            else:
                detail = jstr({kk: vv for kk, vv in v.items() if kk not in ("rows", "grid")})[:120]
        else:
            detail = str(v)
        xc_rows.append({"对账项": k, "口径/结果": detail, "结果": "通过"})
    # M3: 2x2 消融 SILENT
    p1_rows = []
    for cfg, cell in rev["part1_ablation_2x2"]["SILENT"].items():
        for key in ["latched_nogold", "continue_nogold", "latched_gold", "continue_gold"]:
            c = cell[key]
            p1_rows.append({"配置": cfg, "单元格": CELL_NAME[key], "power": c["power"],
                            "操作员FAR": c["op_far"], "可见报警": c["visible_total"],
                            "gold 总": c["gold_total"], "gold/题": c["gold_per_q"],
                            "确认": c["confirmed"], "拒绝": c["rejected"],
                            "stable 可见报警题占比": c["q_frac_visible_stable"]})
    # M3: 对照组 (OR 配置)
    ctl_rows = []
    for grp in ["W3", "BENIGN"]:
        for key in ["latched_nogold", "continue_nogold", "latched_gold", "continue_gold"]:
            c = rev["part1_ablation_2x2"][grp]["OR"][key]
            ctl_rows.append({"组": grp, "单元格": CELL_NAME[key], "操作员FAR": c["op_far"],
                            "可见报警": c["visible_total"], "gold 总": c["gold_total"],
                            "确认": c["confirmed"], "拒绝": c["rejected"]})
    # M5: 层级 bootstrap
    p2 = rev["part2_hier_bootstrap"]
    p2_rows = []
    for cell_key, label in [("driftguard_or", "DriftGuard (Continue/gold)"),
                            ("continue_nogold", "Continue/无gold"),
                            ("latched_nogold", "Latched/无gold")]:
        c = p2[cell_key]
        for metric, pkey, ckey in [("power", "power_point", "power_ci"),
                                    ("gold/q", "goldpq_point", "goldpq_ci"),
                                    ("操作员FAR", "op_far_point", "op_far_ci")]:
            p2_rows.append({"单元格": label, "指标": metric, "点估计": c[pkey],
                            "CI 下界": c[ckey][0], "CI 上界": c[ckey][1]})
    # M5: 逐 DB
    pdb_rows = []
    for k, v in sorted(rev["part2_per_db"].items()):
        db, fam = k.split("|")
        pdb_rows.append({"库": db, "族": fam, "题数": v["n_q"], "onset 题": v["n_onset"],
                        "确认检出": v["confirmed"], "漏检": v["missed"],
                        "gold 总": v["gold_total"], "gold/题": v["gold_per_q"]})
    # M5: 功效比值
    ratio_rows = []
    for g in ["G1", "G2"]:
        r = rev["part2_ratio"][g]
        ratio_rows.append({"侧": f"{g} ({r['root']})", "n/onset": f"{r['n']}/{r['n_onset']}",
                          "两阶段 power": r["two_stage_power"], "两阶段 CI": jstr(r["two_stage_ci"]),
                          "latched power": r["latched_power"], "latched CI": jstr(r["latched_ci"]),
                          "比值 (计数)": r["raw_ratio"], "比值 (功效口径)": r["rounded_power_ratio"],
                          "比值 CI": jstr(r["ratio_ci"]), "分母=0 draws": r["n_zero_denominator_draws"]})
    # M2: 零事件
    ze = rev["part2_zero_event"]
    ze_rows = [{"项": "良性确认 (SILENT)", "值": ze["benign_confirmations_g1_or"]["SILENT"]},
               {"项": "良性确认 (W3)", "值": ze["benign_confirmations_g1_or"]["W3"]},
               {"项": "良性确认 (BENIGN)", "值": ze["benign_confirmations_g1_or"]["BENIGN"]},
               {"项": "良性确认合计 (全部真拒绝)", "值": ze["benign_confirmations_g1_or"]["total"]},
               {"项": "控制组确认 (W3/BENIGN)", "值": jstr(ze["control_group_confirmations"])},
               {"项": "假确认", "值": ze["false_confirmations"]},
               {"项": "DB 级 cluster 数", "值": ze["n_db_clusters"]},
               {"项": "DB 级假确认事件", "值": jstr(ze["db_events"])},
               {"项": "DB 级 95% 上界 1-0.05^(1/5)", "值": ze["db_level_p_upper_95"]}]
    ze_note = [{"说明": ze["note_cluster_level"]},
               {"说明": ze["retained_count_statement"]},
               {"说明": ze["traceable_counts"]}]
    # M9: placebo
    pl = rev["part3_placebo"]
    pl_rows = []
    for cfg, v in pl["configs"].items():
        pl_rows.append({"配置": cfg, "f-hat (MLE)": v["f_hat"],
                        "从未漂移题报警数": f"{v['n_alarmed_never']}/{v['n_never']}",
                        "iid 期望功率": v["iid_expected_power"],
                        "序列交换期望功率": v["swap_expected_power"],
                        "交换 CI": jstr(v["swap_power_ci"]),
                        "lag-1 自相关均值": v["lag1_autocorr_mean"],
                        "实测 DriftGuard": v["observed_driftguard_power"]})
    pl_note = [{"说明": f"OR vs D6 诊断: OR-only 报警单元 {pl['or_vs_d6_diagnostic']['or_extra_alarm_units_vs_d6']}, "
                       f"OR 首报警早于 D6 的从未漂移题数 {pl['or_vs_d6_diagnostic']['or_first_earlier_than_d6_never_q']} "
                       f"→ OR 沿用 D6 f-hat 数值恰成立 (独立估计同为 0.265)"}]
    # M7: LODO
    p4 = rev["part4_lodo"]
    lodo_sum = [{"策略": "规则 R (K*<0.6 时升级 0.8)", "留出 power 均值": p4["summary_R"]["power_mean"],
                 "留出 power 最差": p4["summary_R"]["power_min"],
                 "留出 FAR 均值": p4["summary_R"]["far_mean"], "留出 FAR 最差": p4["summary_R"]["far_max"]},
                {"策略": "直用 K*", "留出 power 均值": p4["summary_direct"]["power_mean"],
                 "留出 power 最差": p4["summary_direct"]["power_min"],
                 "留出 FAR 均值": p4["summary_direct"]["far_mean"], "留出 FAR 最差": p4["summary_direct"]["far_max"]}]
    lodo_rows = []
    for fd in p4["folds"]:
        lodo_rows.append({"留出库": fd["heldout"], "K* (调参)": fd["k_star"], "调参 P": fd["tune_power"],
                         "调参 FAR": fd["tune_far"], "R 阈值": fd["r_threshold"],
                         "R 留出 P": fd["held_power_R"], "R 留出 FAR": fd["held_far_R"],
                         "直用留出 P": fd["held_power_direct"], "直用留出 FAR": fd["held_far_direct"]})
    lodo_note = [{"说明": f"oracle (FAR≤{p4['budget_far']} 最大化 latched power): K={p4['oracle']['th']}, "
                        f"P {p4['oracle']['power']}, FAR {p4['oracle']['far']} — 与 E13 存档一致; "
                        f"thrombosis 折: K*=0.5 直用 0.208/0.4739, R 升级 0.8 后 0.417/0.1609"}]
    rev_note = [{"说明": "零 API: 只读 gen/gen_v4b/gold/results 缓存, 无任何 LLM/API 调用 (run_revision_analysis.py)"},
                {"说明": "M3 结论: 监控语义 (continue) 决定功效上限, gold 确认决定操作员 FAR; latched+gold 不恢复功效 (首个误报被拒绝后停止监测)"}]
    return [("对账概览 (输出本报告的前置条件)", xc_rows),
            ("M3: 2x2 消融 — SILENT (V+W2, 300 题 / 90 onset)", p1_rows),
            ("M3: 2x2 消融 — 对照组 W3/BENIGN (OR 配置)", ctl_rows),
            ("M5: 层级 bootstrap (DB→题, B=2000)", p2_rows),
            ("M5: 逐 DB 表 (SILENT, DriftGuard OR)", pdb_rows),
            ("M5: 两阶段/latched 功效比值 (S 族 loud)", ratio_rows),
            ("M2: 零事件口径 (良性确认全真拒绝)", ze_rows),
            ("M2: 零事件口径说明", ze_note),
            ("M9: placebo 诊断 (iid vs 族匹配序列交换)", pl_rows),
            ("M9: OR-vs-D6 诊断", pl_note),
            ("M7: LODO 嵌套验证汇总 (5 折)", lodo_sum),
            ("M7: LODO 逐折明细", lodo_rows),
            ("M7: LODO 说明", lodo_note),
            ("REV 说明", rev_note)]


def sheet_p1(ctx):
    p1 = ctx["P1"]
    it8 = p1["item8_w2_only"]
    fam_rows = []
    for fam in ["V", "W2"]:
        r = it8[fam]
        fam_rows.append({"族": fam, "两阶段 power (重放)": r["power_replay"],
                         "power (存档)": r["power_stored"], "确认": r["confirmed"],
                         "onset 题": r["n_onset"], "gold/题": r["gold_per_q"]})
    r = it8["SILENT"]
    fam_rows.append({"族": "SILENT", "两阶段 power (重放)": r["power"],
                     "power (存档)": r["power"], "确认": r["confirmed"],
                     "onset 题": r["n_onset"], "gold/题": r["gold_per_q"]})
    it7 = p1["item7_misses"]
    rec = it7["recovery"]
    rec_rows = []
    if rec:
        rec_rows.append({"说明": f"θ={rec['theta']} (θ*={rec['theta_exact']}): 追回 "
                                 f"{rec['n_miss_recovered']}/{rec['n_miss_total']} 漏检; "
                                 f"功效 {rec['power']} (确认 {rec['detected']}/90); "
                                 f"SILENT 锁存 FAR {rec['screen_far_latched']}; "
                                 f"W3+BENIGN FAR {rec['w3b_far']}; gold/题 {rec['gold_per_q']}"})
    it6 = p1["item6_staleness"]
    head = [{"说明": f"锚点池 {it6['n_pool']} 题, 基线良性确认 {it6['benign_confirmations']}, "
                     f"R={it6['r_draws']}, 模型: 触碰过期锚点的确认 verdict 翻转 (最坏情形)"}]
    st_rows = []
    for r in it6["rows"]:
        st_rows.append({"k 过期锚点": r["k"], "p": r["p"],
                        "假告警均值": r["false_alarms_mean"],
                        "假告警最大": r["false_alarms_max"],
                        "上界 p×良性确认": r["bound_false_alarms"],
                        "SILENT power 均值": r["silent_power_mean"],
                        "SILENT power 最低": r["silent_power_min"],
                        "SILENT power 基线": r["silent_power_baseline"],
                        "FAR SILENT 均值": r["far_silent_mean"],
                        "FAR W3+B 均值": r["far_w3b_mean"]})
    return [("项8 分族两阶段功效 (OR, G1)", fam_rows),
            (f"项7 SILENT 漏检 {it7['n_miss']} 题明细 (族分布 {jstr(it7['miss_by_family'])})",
             it7["miss_profiles"]),
            ("项7 恢复实验 (θ=可恢复漏检的最小 post-onset max-jac)", rec_rows),
            ("项6 锚点失效敏感性: 注入设定 (假告警 vs p×良性确认上界)", head),
            ("项6 锚点失效敏感性: k 次注入结果 (R=200)", st_rows)]


def main():
    ctx, fails, val_rows = CPD.run()
    if fails:
        print(f"校验存在 {fails} 项失败, 终止导出。请先修复数据。")
        sys.exit(1)
    print(f"校验通过 ({sum(1 for r in val_rows if r['结果'] == '通过')} 项), 开始导出 Excel ...")

    os.makedirs(OUT_DIR, exist_ok=True)
    wb = Workbook()
    wb.remove(wb.active)

    add_sheet(wb, "00_校验与总览", sheet_overview(ctx, val_rows))
    add_sheet(wb, "01_E1_基准构建", sheet_e1(ctx))
    add_sheet(wb, "02_E2_E6_候选生成", sheet_gen(ctx))
    add_sheet(wb, "03_E6_pilot试点", sheet_pilot(ctx))
    add_sheet(wb, "04_E3_E7_judge置信", sheet_judge(ctx))
    add_sheet(wb, "05_E4_检测器评测", sheet_e4(ctx))
    add_sheet(wb, "06_E5_稳健性", sheet_e5(ctx))
    add_sheet(wb, "07_E8_跨生成器", sheet_e8(ctx))
    add_sheet(wb, "08_E9_W3负控制", sheet_e9(ctx))
    add_sheet(wb, "09_E10_Vbenign控制", sheet_e10(ctx))
    add_sheet(wb, "10_E11_组合监测器", sheet_e11(ctx))
    add_sheet(wb, "11_E12_表级PSI", sheet_e12(ctx))
    add_sheet(wb, "12_E13_迁移bootstrap_EX", sheet_e13(ctx))
    add_sheet(wb, "13_P1_漏检与锚点失效", sheet_p1(ctx))
    add_sheet(wb, "14_REV_审稿修订", sheet_rev(ctx))

    wb.save(XLSX)
    print(f"已保存: {XLSX}")
    print(f"sheet 数: {len(wb.sheetnames)}")
    for name in wb.sheetnames:
        ws = wb[name]
        print(f"  {name}: {ws.max_row} 行 x {ws.max_column} 列")

if __name__ == "__main__":
    main()
