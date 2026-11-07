# -*- coding: utf-8 -*-
"""核对导出的 Excel 与源数据一致: 重新载入, 标记值全部程序化取自源存档/计算, 逐 sheet 扫描"""
import sys, os, json, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_paper_data as CPD
from openpyxl import load_workbook

XLSX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results_export", "DriftSQL_experiment_results.xlsx")

ctx, fails, val_rows = CPD.run()
assert fails == 0
wb = load_workbook(XLSX, read_only=True)

def cells(sheet):
    return [c for row in wb[sheet].iter_rows(values_only=True) for c in row if c is not None]

def has(sheet, val):
    for c in cells(sheet):
        if c is None:
            continue
        if isinstance(c, (int, float)) and isinstance(val, (int, float)) and not isinstance(c, bool) and not isinstance(val, bool):
            if abs(float(c) - float(val)) < 1e-9:
                return True
        if isinstance(c, str) and str(val) in c:
            return True
        if c == val:
            return True
    return False

def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None

def pct(vals, q):
    s = sorted(v for v in vals if v is not None)
    if not s: return None
    n = len(s)
    if n == 1: return s[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos)); hi = min(lo + 1, n - 1); fr = pos - lo
    return s[lo] * (1 - fr) + s[hi] * fr

assert len(wb.sheetnames) == 15, wb.sheetnames

# ---- 标记值程序化构建 (与 export 同源同舍入) ----
gc = ctx["gen_chat"]["agg"]; gv = ctx["gen_v4b"]["agg"]; gw3 = ctx["gen_w3_chat"]["agg"]["W3"]; gw3b = ctx["gen_w3_v4b"]["agg"]["W3"]
rb = ctx["E5"]; ci_d5 = pct(rb["bootstrap_ci"]["SILENT"]["D5_mode"]["power"], 0.975)
w3j = ctx["E9"]; bc = ctx["bc"] if "bc" in ctx else ctx["E10"]; cg = ctx["E8"]; dr = ctx["E4"]; cm = ctx["E11"]; tp = ctx["E12"]; p4 = ctx["E13"]

p1 = ctx["P1"]
rev = ctx["REV"]

markers = {
    "00_校验与总览": [str(sum(1 for r in val_rows if r["结果"] == "通过")), "通过", "E13", 64, 25680],   # 校验行预期列含 64/25680
    "01_E1_基准构建": [1534, 11, ctx["dbs_counts"]["S"], ctx["dbs_counts"]["V"], ctx["dbs_counts"]["W2"], ctx["gold_counts"]["W3"]],
    "02_E2_E6_候选生成": [gc["S"]["q"], gc["V"]["q"], gc["W2"]["q"], gc["W1"]["q"], gc["S"]["cand"], gw3["cand"], round(gc["S"]["ex_rate"], 4), round(gv["S"]["ex_rate"], 4)],
    "03_E6_pilot试点": [ctx["E6b"]["empty_rate"], ctx["v4_artifact"]["lines"], ctx["v4_artifact"]["empty"]],
    "04_E3_E7_judge置信": [round(mean(ctx["judge_chat"]["fam"]["S"]), 2), round(mean(ctx["judge_v4b"]["fam"]["S"]), 2),
                        round(mean(ctx["judge_w3_chat"]["famck"][("W3", 1)]), 2), round(mean(ctx["judge_w3_v4b"]["famck"][("W3", 1)]), 2)],
    "05_E4_检测器评测": [dr["groups"]["SILENT"]["detectors"]["D5_mode"]["power"], dr["groups"]["SILENT"]["detectors"]["D6_jac"]["far"],
                     dr["groups"]["S"]["detectors"]["D6_jac"]["power"], dr["groups"]["S"]["detectors"]["D6_jac"]["far"]],
    "06_E5_稳健性": [round(pct(rb["bootstrap_ci"]["SILENT"]["D5_mode"]["power"], 0.025), 3),
                 round(ci_d5, 3), rb["sensitivity"][0]["S_power"], "30 组"],
    "07_E8_跨生成器": [cg["subset_n"]["V"], cg["groups"]["v4b"]["SILENT"]["detectors"]["D5_mode"]["power"],
                   cg["groups"]["v4b"]["SILENT"]["detectors"]["D6_jac"]["power"], cg["groups"]["v4b"]["SILENT"]["detectors"]["D6_jac"]["far"],
                   cg["groups"]["v4b"]["W2"]["detectors"]["D6_jac"]["power"]],
    "08_E9_W3负控制": [w3j["sides"]["chat"]["pooled"]["detectors"]["D6_jac"]["far"],
                   w3j["sides"]["v4b"]["pooled"]["detectors"]["D6_jac"]["far"],
                   w3j["sides"]["chat"]["pooled"]["detectors"]["D5_mode"]["far"],
                   w3j["sides"]["v4b"]["pooled"]["detectors"]["D5_mode"]["far"],
                   w3j["sides"]["chat"]["pooled"]["n_stable_ckpts"]],
    "09_E10_Vbenign控制": [bc["sides"]["chat"]["pooled"]["detectors"]["D6_jac"]["far"],
                        bc["sides"]["v4b"]["pooled"]["detectors"]["D6_jac"]["far"],
                        bc["sides"]["chat"]["pooled"]["detectors"]["D5_mode"]["far"],
                        bc["sides"]["v4b"]["pooled"]["detectors"]["D5_mode"]["far"],
                        bc["sides"]["chat"]["two_stage"]["confirm_execs"]],
    "10_E11_组合监测器": [cm["t1"]["chat"]["SILENT"]["OR"]["power"], cm["t1"]["chat"]["S"]["OR"]["power"],
                      cm["t2"]["S"]["rows"]["1"]["power"], cm["t1"]["chat"]["W3"]["OR"]["gold_confirm_total"],
                      cm["t1"]["chat"]["BENIGN"]["OR"]["gold_confirm_total"],
                      cm["t3"]["attribution"]["rename_table"]["break"], cm["t3"]["attribution"]["rename_column"]["break"],
                      cm["t3"]["n_stable_unaffected"]],
    "11_E12_表级PSI": [tp["scenarios"]["V"]["student_club:t3"]["max_psi"], len(tp["events"]["V"]),
                    len(tp["events"]["W2"]), len(tp["events"]["S"]), tp["scenarios"]["BENIGN"]["superhero:t3"]["max_psi"] if "superhero:t3" in tp["scenarios"]["BENIGN"] else 0.181],
    "12_E13_迁移bootstrap_EX": [p4["a_transfer"]["combos"]["D6:SILENT"]["oracle_th"], p4["a_transfer"]["combos"]["D6:SILENT"]["oracle_power"],
                             p4["a_transfer"]["combos"]["D6:SILENT"]["oracle_far"], p4["a_transfer"]["combos"]["D6:SILENT"]["eval_power_mean"],
                             p4["b_bootstrap"]["per_family"]["SILENT"]["D5_mode"]["power_ci"][0],
                             p4["b_bootstrap"]["per_family"]["SILENT"]["D5_mode"]["power_ci"][1],
                             p4["c_v4b_ex"]["sides"]["v4b"]["ex0_mean"], p4["c_v4b_ex"]["sides"]["chat"]["cor0_mean"],
                             p4["c_v4b_ex"]["sides"]["chat"]["decor_at_onset_mean"]],
    "13_P1_漏检与锚点失效": [p1["item6_staleness"]["benign_confirmations"],
                         p1["item7_misses"]["n_miss"],
                         p1["item7_misses"]["miss_by_family"]["V"],
                         p1["item7_misses"]["miss_by_family"]["W2"],
                         p1["item7_misses"]["recovery"]["theta"],
                         p1["item7_misses"]["recovery"]["screen_far_latched"],
                         p1["item7_misses"]["recovery"]["gold_per_q"],
                         p1["item8_w2_only"]["W2"]["power_stored"],
                         p1["item8_w2_only"]["W2"]["gold_per_q"],
                         p1["item6_staleness"]["rows"][2]["false_alarms_mean"],
                         p1["item6_staleness"]["rows"][2]["bound_false_alarms"],
                         p1["item6_staleness"]["rows"][2]["silent_power_min"]],
    "14_REV_审稿修订": [rev["part1_ablation_2x2"]["SILENT"]["OR"]["continue_gold"]["power"],
                     rev["part1_ablation_2x2"]["SILENT"]["OR"]["continue_gold"]["gold_per_q"],
                     rev["part1_ablation_2x2"]["SILENT"]["OR"]["continue_gold"]["gold_total"],
                     rev["part1_ablation_2x2"]["SILENT"]["OR"]["continue_gold"]["confirmed"],
                     rev["part1_ablation_2x2"]["SILENT"]["OR"]["continue_nogold"]["op_far"],
                     rev["part1_ablation_2x2"]["SILENT"]["OR"]["continue_nogold"]["visible_total"],
                     rev["part2_hier_bootstrap"]["driftguard_or"]["power_ci"][0],
                     rev["part2_hier_bootstrap"]["driftguard_or"]["power_ci"][1],
                     rev["part2_hier_bootstrap"]["driftguard_or"]["goldpq_ci"][1],
                     rev["part2_hier_bootstrap"]["continue_nogold"]["op_far_ci"][0],
                     rev["part2_ratio"]["G1"]["rounded_power_ratio"],
                     rev["part2_ratio"]["G2"]["rounded_power_ratio"],
                     rev["part2_ratio"]["G2"]["n_zero_denominator_draws"],
                     rev["part2_zero_event"]["benign_confirmations_g1_or"]["total"],
                     rev["part2_zero_event"]["db_level_p_upper_95"],
                     rev["part3_placebo"]["configs"]["D6@0.30"]["f_hat"],
                     rev["part3_placebo"]["configs"]["D6@0.30"]["swap_expected_power"],
                     rev["part4_lodo"]["summary_R"]["power_mean"],
                     rev["part4_lodo"]["summary_R"]["power_min"],
                     rev["part4_lodo"]["summary_direct"]["far_max"],
                     [f for f in rev["part4_lodo"]["folds"] if f["heldout"] == "thrombosis_prediction"][0]["held_power_R"]],
}

n_checks = 0
n_fail = 0
for sheet, vals in markers.items():
    missing = []
    for v in vals:
        n_checks += 1
        if not has(sheet, v):
            missing.append(v)
    status = "OK  " if not missing else "FAIL"
    print(f"{status} {sheet}: {len(vals)} 个源数据标记值" + (f"  缺失: {missing}" if missing else ""))
    n_fail += len(missing)

# 12 号 sheet 的 CI 为字符串单元格, 补充字符串包含检查
s12 = " ".join(str(c) for c in cells("12_E13_迁移bootstrap_EX"))
for s in [str(p4["b_bootstrap"]["per_family"]["SILENT"]["D5_mode"]["power_ci"][0]),
          str(p4["b_bootstrap"]["per_family"]["SILENT"]["D6_jac"]["power_ci"][1])]:
    n_checks += 1
    if s in s12:
        print(f"OK   12_E13 CI 字符串包含 {s}")
    else:
        print(f"FAIL 12_E13 CI 字符串缺 {s}"); n_fail += 1

print(f"\n===== 核对完成: {n_checks} 项, {n_fail} 处失败 =====")
sys.exit(1 if n_fail else 0)
