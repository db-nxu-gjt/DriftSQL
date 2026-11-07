# -*- coding: utf-8 -*-
"""
make_paper_figures.py -- publication figures for the DriftSQL paper.

Renders 11 figures (PDF + 300-dpi PNG) into DriftSQL/paper/results/figs/
from archived experiment outputs (E4-E13). Zero API calls, fully reproducible.

Figure map (paper claims):
  fig01  E4      grouped bars: no single runtime signal covers loud + silent
  fig02  E5      forest plot: loud/silent power asymmetry, 95% bootstrap CI
  fig03  E8      paired barh: silent signals reproduce across generators
  fig04  E9/E10  heatmap: screening FAR on benign changes; gold anchor = 0
  fig05  E11-T1  Pareto: screening-latched -> two-stage (ours), FAR = 0
  fig06  E11-T1  dumbbell: two-stage power gain per family x generator
  fig07  E11-T2  anchor budget curves: DB-level power vs. # gold anchors
  fig08  E13c    dual panel: exec_ok flat while correctness collapses
  fig09  E13a    threshold transfer: K sweep + 10 held-out splits
  fig10  E12     PSI baseline: low power on answer-level drift
  fig11  E6      v4 artifact case study: per-DB empty-SQL rate
"""
import json
import os
import glob
import collections

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.lines import Line2D
from matplotlib.ticker import ScalarFormatter

BASE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(BASE, "results")
FIGS = os.environ.get("DRIFTSQL_FIGS_DIR") or os.path.join(BASE, "results", "figs")
os.makedirs(FIGS, exist_ok=True)

# ---------------- style ----------------
plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["Times New Roman", "STIXGeneral", "DejaVu Serif"],
    "mathtext.fontset": "stix",
    "font.size": 8.5,
    "axes.titlesize": 9.0,
    "axes.labelsize": 8.5,
    "xtick.labelsize": 8.0,
    "ytick.labelsize": 8.0,
    "legend.fontsize": 7.5,
    "axes.linewidth": 0.6,
    "xtick.major.width": 0.6,
    "ytick.major.width": 0.6,
    "xtick.major.size": 3.0,
    "ytick.major.size": 3.0,
    "axes.grid": True,
    "grid.alpha": 0.25,
    "grid.linewidth": 0.4,
    "axes.axisbelow": True,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.03,
    "pdf.fonttype": 42,
})

CRIM, BLUE, LBLUE = "#B2182B", "#2166AC", "#92C5DE"
GRAY, LGRAY, ORANGE, GREEN = "#8C8C8C", "#D9D9D9", "#E08214", "#1B7837"

DETS = ["D1_ex", "D2_ks", "D3_judge", "D4_share",
        "D5_mode", "D6_jac", "D7_cusum", "D8_oracle"]
SHORT = ["D1 EX", "D2 KS", "D3 Judge", "D4 Share",
         "D5 Mode", "D6 Jaccard", "D7 CUSUM", "D8 Gold"]
SHORT2 = ["D1\nEX", "D2\nKS", "D3\nJudge", "D4\nShare",
          "D5\nMode", "D6\nJacc.", "D7\nCUSUM", "D8\nGold"]


def load(name):
    with open(os.path.join(RES, name), encoding="utf-8") as f:
        return json.load(f)


E4 = load("detector_results.json")
E5 = load("robustness.json")
E8 = load("detector_crossgen.json")
E9 = load("detector_w3.json")
E10 = load("benign_control.json")
E11 = load("combined_monitor.json")
E12 = load("table_psi.json")
E13 = load("p04_threshold_bootstrap.json")


def save(fig, name):
    fig.savefig(os.path.join(FIGS, name + ".pdf"))
    fig.savefig(os.path.join(FIGS, name + ".png"), dpi=300)
    plt.close(fig)
    print("[OK] %s" % name)


def ygrid(ax):
    ax.grid(False)
    ax.grid(True, axis="y", alpha=0.25, linewidth=0.4)


def xgrid(ax):
    ax.grid(False)
    ax.grid(True, axis="x", alpha=0.25, linewidth=0.4)


def bar_vlabel(ax, bars, fmt="%.2f", dy=0.015, fs=6.2):
    for b in bars:
        h = b.get_height()
        ax.text(b.get_x() + b.get_width() / 2.0, h + dy, fmt % h,
                ha="center", va="bottom", fontsize=fs, rotation=90)


def barh_label(ax, bars, fmt="%.3f", dx=0.008, fs=6.3):
    for b in bars:
        w = b.get_width()
        ax.text(w + dx, b.get_y() + b.get_height() / 2.0, fmt % w,
                ha="left", va="center", fontsize=fs)


# ---------------------------------------------------------------- fig01
def fig01():
    """E4: grouped bars, power + FAR, loud (S) vs silent (V+W2)."""
    S = E4["groups"]["S"]["detectors"]
    SI = E4["groups"]["SILENT"]["detectors"]
    x = np.arange(8)
    w = 0.38
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.5), constrained_layout=True)
    panels = [("power", "(a) Detection power (first alarm, latched)"),
              ("far", "(b) False-alarm rate on stable checkpoints")]
    ylims = {"power": (0, 1.22), "far": (0, 0.92)}
    for ax, (key, ttl) in zip(axes, panels):
        a = [S[d][key] for d in DETS]
        b = [SI[d][key] for d in DETS]
        r1 = ax.bar(x - w / 2, a, w, color=BLUE, label="Loud drift (S)",
                    edgecolor="white", linewidth=0.4)
        r2 = ax.bar(x + w / 2, b, w, color=ORANGE, label="Silent drift (V+W2)",
                    edgecolor="white", linewidth=0.4)
        bar_vlabel(ax, list(r1)[:7])  # D8 (equal bars) labeled once below
        bar_vlabel(ax, list(r2)[:7])
        ax.set_xticks(x)
        ax.set_xticklabels(SHORT2)
        ax.set_ylim(*ylims[key])
        ax.set_xlim(-0.55, 7.55)
        ygrid(ax)
        ax.set_title(ttl, loc="left")
    axes[0].text(7, 1.03, "1.00", ha="center", va="bottom", fontsize=6.2)
    axes[1].text(7, 0.03, "0.00", ha="center", va="bottom", fontsize=6.2)
    axes[0].annotate("distribution signals\nare silent-only",
                     xy=(4.25, 0.58), xytext=(2.9, 0.95),
                     fontsize=7, color=ORANGE, ha="center",
                     arrowprops=dict(arrowstyle="->", lw=0.7, color=ORANGE))
    axes[0].text(7, 1.155, "gold (oracle)", fontsize=6.3, color=CRIM,
                 ha="center", va="bottom")
    axes[1].annotate("noisy under loud drift",
                     xy=(4.83, 0.585), xytext=(3.1, 0.80),
                     fontsize=7, color=BLUE, ha="center",
                     arrowprops=dict(arrowstyle="->", lw=0.7, color=BLUE))
    axes[0].legend(loc="upper left", frameon=False, ncol=1)
    save(fig, "fig01_detector_power_far")


# ---------------------------------------------------------------- fig02
def fig02():
    """E5: forest plot of power with question-level bootstrap 95% CI."""
    base = E5["base"]
    bc = E5["bootstrap_ci"]

    def ci95(draws):
        v = np.array([t for t in draws if t is not None], dtype=float)
        lo, hi = np.percentile(v, [2.5, 97.5])
        return float(lo), float(hi)

    fig, ax = plt.subplots(figsize=(3.5, 2.8), constrained_layout=True)
    for i, det in enumerate(DETS):
        for fam, color, mk, dy in (("S", BLUE, "o", 0.18),
                                   ("SILENT", ORANGE, "s", -0.18)):
            pt = base[fam]["detectors"][det]["power"]
            lo, hi = ci95(bc[fam][det]["power"])
            ax.errorbar(pt, i + dy, xerr=[[pt - lo], [hi - pt]], fmt="none",
                        ecolor=color, elinewidth=1.1, capsize=2.2, capthick=0.9)
            ax.plot([pt], [i + dy], mk, color=color, ms=4.2, zorder=5)
    ax.set_yticks(range(8))
    ax.set_yticklabels(SHORT)
    ax.set_ylim(-0.55, 7.55)
    ax.set_xlim(-0.04, 1.06)
    xgrid(ax)
    ax.set_xlabel("Detection power (95% CI, question bootstrap, B=1000)")
    ax.set_title("Loud vs. silent power: CIs are disjoint for D5", loc="left")
    ax.annotate("D5 CIs are disjoint:\nsilent-only signal",
                xy=(0.47, 3.84), xytext=(0.62, 3.35), fontsize=6.5, color=CRIM,
                ha="left", va="center",
                arrowprops=dict(arrowstyle="->", lw=0.7, color=CRIM))
    handles = [Line2D([], [], marker="o", ls="none", color=BLUE, label="Loud (S)"),
               Line2D([], [], marker="s", ls="none", color=ORANGE, label="Silent (V+W2)")]
    ax.legend(handles=handles, loc="upper left", frameon=False)
    save(fig, "fig02_power_ci_forest")


# ---------------------------------------------------------------- fig03
def fig03():
    """E8: paired barh, SILENT power chat vs v4b (cross-generator)."""
    chat = E8["groups"]["chat"]["SILENT"]["detectors"]
    v4b = E8["groups"]["v4b"]["SILENT"]["detectors"]
    y = np.arange(8)[::-1]
    a = [chat[d]["power"] for d in DETS]
    b = [v4b[d]["power"] for d in DETS]
    fig, ax = plt.subplots(figsize=(3.5, 2.6), constrained_layout=True)
    r1 = ax.barh(y + 0.19, a, 0.36, color=BLUE, label="deepseek-chat",
                 edgecolor="white", linewidth=0.4)
    r2 = ax.barh(y - 0.19, b, 0.36, color=ORANGE, label="v4b (weaker LLM)",
                 edgecolor="white", linewidth=0.4)
    barh_label(ax, r1)
    barh_label(ax, r2)
    ax.set_yticks(y)
    ax.set_yticklabels(SHORT)
    ax.set_xlim(0, 1.14)
    ax.set_ylim(-0.55, 7.75)
    ax.set_xlabel("Silent-drift detection power")
    xgrid(ax)
    ax.set_title("Silent signals reproduce across generators", loc="left")
    d5d = abs(chat["D5_mode"]["power"] - v4b["D5_mode"]["power"])
    ax.annotate("D5 gap = %.3f" % d5d, xy=(0.60, 3.30), xytext=(0.72, 4.5),
                fontsize=6.5, color=CRIM,
                arrowprops=dict(arrowstyle="->", lw=0.7, color=CRIM))
    ax.text(0.015, 7.52, "D1 (execution decay): blind on both generators",
            fontsize=6.5, color=GRAY, va="center")
    ax.legend(loc="upper right", frameon=False)
    save(fig, "fig03_crossgen_repro")


# ---------------------------------------------------------------- fig04
def fig04():
    """E9/E10: heatmap of screening FAR on benign changes."""
    cols = [("Paraphrase\n(chat)", E9["sides"]["chat"]["pooled"]["detectors"]),
            ("Paraphrase\n(v4b)", E9["sides"]["v4b"]["pooled"]["detectors"]),
            ("Benign data\n(chat)", E10["sides"]["chat"]["pooled"]["detectors"]),
            ("Benign data\n(v4b)", E10["sides"]["v4b"]["pooled"]["detectors"])]
    M = np.array([[cols[j][1][d]["far"] for j in range(4)] for d in DETS])
    fig, ax = plt.subplots(figsize=(3.5, 2.8), constrained_layout=True)
    cmap = LinearSegmentedColormap.from_list("wr", ["#FFFFFF", CRIM])
    im = ax.imshow(M, cmap=cmap, vmin=0, vmax=0.65, aspect="auto")
    ax.grid(False)
    for i in range(8):
        for j in range(4):
            v = M[i, j]
            ax.text(j, i, "%.2f" % v, ha="center", va="center", fontsize=6.8,
                    color="white" if v > 0.40 else "#333333")
    ax.add_patch(Rectangle((-0.5, 6.5), 4, 1, fill=False, edgecolor=GREEN,
                           lw=1.4, zorder=5))
    ax.set_xticks(range(4))
    ax.set_xticklabels([c[0] for c in cols])
    ax.set_yticks(range(8))
    ax.set_yticklabels(SHORT[:-1] + ["D8 Gold (ours)"])
    ax.set_title("Screening FAR under benign changes", loc="left")
    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.03)
    cbar.ax.tick_params(labelsize=7)
    cbar.set_label("False-alarm rate", fontsize=7.5)
    save(fig, "fig04_benign_far_heatmap")


# ---------------------------------------------------------------- fig05
def fig05():
    """E11-T1: Pareto, screening-latched vs two-stage (chat, SILENT)."""
    T = E11["t1"]["chat"]["SILENT"]
    for k in ("D5", "D6@0.30", "D6@0.50", "OR"):
        c = T[k]
        print("  fig05 %-8s latched %.3f (FAR %.3f) -> two-stage %.3f "
              "(FAR %.3f, gold/q %.2f)" % (k, c["power_screen_latched"],
              c["screen_far_latched"], c["power"], c["two_stage_far"],
              c["gold_confirm_per_q"]))
    # D6@0.30 coincides with OR on silent drift (identical latched and
    # two-stage operating points); omitted from the plot for readability.
    cfgs = [("D5", "D5", 0.235, 0.544, "left"),
            ("D6@0.50", "D6 ($\\tau$=.50)", 0.277, 0.592, "left"),
            ("OR", "D5$|$D6 (OR)", 0.543, 0.443, "center")]
    fig, ax = plt.subplots(figsize=(3.5, 2.6), constrained_layout=True)
    for k, lab, lx, ly, lha in cfgs:
        c = T[k]
        x0, y0 = c["screen_far_latched"], c["power_screen_latched"]
        x1, y1 = c["two_stage_far"], c["power"]
        ax.annotate("", xy=(x1 + 0.004, y1), xytext=(x0 - 0.006, y0),
                    arrowprops=dict(arrowstyle="->", color=GRAY, lw=0.8,
                                    alpha=0.85))
        ax.plot([x0], [y0], "o", mfc="none", mec=GRAY, ms=5, mew=0.9, zorder=4)
        ax.plot([x1], [y1], "*", color=CRIM, ms=9, zorder=6)
        ax.text(lx, ly, lab, fontsize=6.8, color=GRAY, va="center", ha=lha)
        ax.text(0.020, y1 + 0.022, "%.2f" % y1, fontsize=6.8, color=CRIM,
                va="center", zorder=7)
    ax.set_xlim(-0.03, 0.62)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("False-alarm rate (latched screening)")
    ax.set_ylabel("Detection power")
    ax.set_title("Two-stage monitoring: FAR = 0, power up", loc="left")
    ax.text(0.335, 0.05, "confirmation cost: %.2f gold execs/question"
            % T["OR"]["gold_confirm_per_q"], fontsize=6.3, color=GRAY)
    handles = [Line2D([], [], marker="o", mfc="none", mec=GRAY, ls="none",
                      label="screening alone (latched)"),
               Line2D([], [], marker="*", ls="none", color=CRIM,
                      label="two-stage (ours)")]
    ax.legend(handles=handles, loc="upper right", frameon=False)
    save(fig, "fig05_pareto_two_stage")


# ---------------------------------------------------------------- fig06
def fig06():
    """E11-T1: dumbbell, latched -> two-stage power (OR config)."""
    rows = [("S (loud), chat", E11["t1"]["chat"]["S"]["OR"]),
            ("V (value), chat", E11["t1"]["chat"]["V"]["OR"]),
            ("W2 (workload), chat", E11["t1"]["chat"]["W2"]["OR"]),
            ("Silent pooled, chat", E11["t1"]["chat"]["SILENT"]["OR"]),
            ("S (loud), v4b", E11["t1"]["v4b"]["S"]["OR"]),
            ("Silent pooled, v4b", E11["t1"]["v4b"]["SILENT"]["OR"])]
    fig, ax = plt.subplots(figsize=(3.5, 2.6), constrained_layout=True)
    for i, (lab, c) in enumerate(rows):
        y = len(rows) - 1 - i
        p0, p1 = c["power_screen_latched"], c["power"]
        ax.plot([p0, p1], [y, y], color=GRAY, lw=0.9, zorder=2)
        ax.plot([p0], [y], "o", mfc="none", mec=GRAY, ms=5, mew=0.9, zorder=4)
        ax.plot([p1], [y], "*", color=CRIM, ms=9, zorder=5)
        ax.text(p1 + 0.035, y, "$\\times$%.1f" % (p1 / p0), fontsize=6.8,
                color=CRIM, va="center")
        print("  fig06 %-20s %.3f -> %.3f  (x%.1f)" % (lab, p0, p1, p1 / p0))
    ax.set_yticks(range(len(rows))[::-1])
    ax.set_yticklabels([r[0] for r in rows])
    ax.set_xlim(0, 1.14)
    ax.set_ylim(-0.55, len(rows) - 0.45)
    xgrid(ax)
    ax.set_xlabel("Detection power (two-stage FAR = 0 on every row)")
    ax.set_title("Power gain: screening-only vs. two-stage", loc="left")
    handles = [Line2D([], [], marker="o", mfc="none", mec=GRAY, ls="none",
                      label="screening alone (latched)"),
               Line2D([], [], marker="*", ls="none", color=CRIM,
                      label="two-stage (ours)")]
    ax.legend(handles=handles, loc="center left", bbox_to_anchor=(0.02, 0.45),
              frameon=False)
    save(fig, "fig06_dumbbell_power_gain")


# ---------------------------------------------------------------- fig07
def fig07():
    """E11-T2: gold-anchor budget curves (DB level, B=200)."""
    ms = [1, 3, 5, 10, 25, 50]
    styles = [("S", BLUE), ("V", GREEN), ("W2", ORANGE)]
    fig, ax = plt.subplots(figsize=(3.5, 2.6), constrained_layout=True)
    for fam, color in styles:
        r = E11["t2"][fam]["rows"]
        p = [r[str(m)]["power"] for m in ms]
        lo = [r[str(m)]["ci"][0] for m in ms]
        hi = [r[str(m)]["ci"][1] for m in ms]
        ax.plot(ms, p, "-o", color=color, ms=3.6, lw=1.1,
                label={"S": "S (loud)", "V": "V (value)",
                       "W2": "W2 (workload)"}[fam])
        ax.fill_between(ms, lo, hi, color=color, alpha=0.13, lw=0)
        print("  fig07 %s: %s" % (fam, ["%.2f" % v for v in p]))
    ax.axhline(1.0, color=GRAY, ls=":", lw=0.7)
    ax.set_xscale("log")
    ax.set_xticks(ms)
    ax.get_xaxis().set_major_formatter(ScalarFormatter())
    ax.set_xlim(0.9, 60)
    ax.set_ylim(0, 1.08)
    ax.set_xlabel("Gold anchors per database (m, log scale)")
    ax.set_ylabel("DB-level power (B=200)")
    ax.set_title("Anchor budget: one anchor/DB covers most S events",
                 loc="left")
    ax.annotate("m=1: %.2f" % E11["t2"]["S"]["rows"]["1"]["power"],
                xy=(1.05, E11["t2"]["S"]["rows"]["1"]["power"]),
                xytext=(1.6, 0.42), fontsize=6.8, color=BLUE,
                arrowprops=dict(arrowstyle="->", lw=0.7, color=BLUE))
    ax.legend(loc="lower right", frameon=False)
    save(fig, "fig07_anchor_budget")


# ---------------------------------------------------------------- fig08
def _s_reference_availability():
    """Scan S-family generation rows (zero API): per (side, state, ck)
    return (reference-unavailable rate, conditional baseline-gold agreement).
    A row's `correct` is None exactly when the baseline gold cannot execute
    at that checkpoint; agreement conditional on an executable reference is
    ok / (n - none). State (drifted/stable) comes from gold_S breakage."""
    gold = {}
    with open(os.path.join(BASE, "gold", "gold_S.jsonl"), encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            cps = r.get("checkpoints", {})
            drifted = any((not cps[str(c)].get("exec_ok", True))
                           for c in cps if str(c) != "0")
            gold[str(r["qid"])] = drifted
    n_drift = sum(1 for v in gold.values() if v)
    print("  fig08: gold_S drifted=%d stable=%d" % (n_drift, len(gold) - n_drift))
    out = {}
    for side, root in (("chat", "gen"), ("v4b", "gen_v4b")):
        for state in ("drifted", "stable"):
            for ck in range(6):
                out[(side, state, ck)] = [0, 0, 0]   # n, none, ok
        fam_dir = os.path.join(BASE, root, "S")
        for db in sorted(os.listdir(fam_dir)):
            for ck in range(6):
                p = os.path.join(fam_dir, db, "ck%d.jsonl" % ck)
                if not os.path.exists(p):
                    continue
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        r = json.loads(line)
                        state = "drifted" if gold.get(str(r["qid"])) else "stable"
                        cell = out[(side, state, ck)]
                        cell[0] += 1
                        c = r.get("correct")
                        if c is None:
                            cell[1] += 1
                        elif c is True:
                            cell[2] += 1
    return out


def fig08():
    """E13c: exec_ok flat while the answer-level reference breaks;
    baseline-gold agreement conditional on an executable reference stays flat."""
    sc = E13["c_v4b_ex"]["sides"]
    avail = _s_reference_availability()
    ck = ["0", "1", "2", "3", "4", "5"]
    x = np.arange(6)
    series = [("chat", "drifted", BLUE, "-", "o", "G1 drifted"),
              ("chat", "stable", BLUE, "--", "o", "G1 stable"),
              ("v4b", "drifted", ORANGE, "-", "s", "G2 drifted"),
              ("v4b", "stable", ORANGE, "--", "s", "G2 stable")]
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.5), constrained_layout=True)
    for ax, key, ttl, ylim in (
            (axes[0], "ex_by_ck",
             "(a) Execution success rate (exec\\_ok)", (0.938, 1.004)),
            (axes[1], "cor_by_ck",
             "(b) Baseline-gold agreement (undefined counted as 0)",
             (-0.03, 0.62))):
        for side, state, color, ls, mk, lab in series:
            v = [sc[side][key][state][c] for c in ck]
            filled = (ls == "-")
            ax.plot(x, v, ls=ls, color=color,
                    marker=mk, ms=3.4, lw=1.15,
                    mfc=color if filled else "none",
                    mec=color, label=lab)
        ax.set_xticks(x)
        ax.set_xticklabels(["ck%d" % i for i in range(6)])
        ax.set_ylim(*ylim)
        ygrid(ax)
        ax.set_title(ttl, loc="left")
        ax.set_xlabel("Checkpoint")
    axes[0].text(2.5, 0.9435, "execution stays healthy-looking ($\\approx$ 0.99)",
                 fontsize=6.8, color=GRAY, ha="center", va="center")
    # reference-unavailable share of drifted units (crimson step, twin axis)
    nr = [avail[("chat", "drifted", i)][1] / avail[("chat", "drifted", i)][0]
          if avail[("chat", "drifted", i)][0] else 0 for i in range(6)]
    ax2 = axes[1].twinx()
    ax2.step(x, [100 * v for v in nr], where="mid", color=CRIM, lw=1.2,
             alpha=0.85, label="reference unavailable (drifted, right axis)")
    ax2.set_ylim(0, 105)
    ax2.set_ylabel("reference unavailable (%)", fontsize=7.5, color=CRIM)
    ax2.tick_params(axis="y", labelsize=7.0, colors=CRIM)
    ax2.grid(False)
    axes[1].annotate("gold breaks $\\rightarrow$ reference undefined\n"
                     "on 12.5%/47.3%/100% of drifted units (ck2/3/5);\n"
                     "agreement flat where the reference survives",
                     xy=(3.1, 0.30), xytext=(0.15, 0.045), fontsize=6.4,
                     color=CRIM,
                     arrowprops=dict(arrowstyle="->", lw=0.8, color=CRIM))
    hs, lbs = axes[0].get_legend_handles_labels()
    hs2, lbs2 = ax2.get_legend_handles_labels()
    fig.legend(hs + hs2, lbs + lbs2, loc="outside upper center",
               ncol=5, frameon=False, fontsize=7.0, columnspacing=1.2,
               handlelength=1.6)
    save(fig, "fig08_exec_vs_correct")


# ---------------------------------------------------------------- fig09
def fig09():
    """E13a: D6 threshold sweep + held-out transfer splits."""
    c = E13["a_transfer"]["combos"]["D6:SILENT"]
    K = [s["th"] for s in c["sweep"]]
    P = [s["power"] for s in c["sweep"]]
    F = [s["far"] for s in c["sweep"]]
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.6), constrained_layout=True)

    # (a) K sweep: power (left) + FAR (right)
    ax = axes[0]
    ax.plot(K, P, "-o", color=BLUE, ms=3.2, lw=1.1, label="Power (left)")
    ax.axvspan(0.6, 0.93, color=GREEN, alpha=0.09, lw=0)
    ax.set_xlim(0.03, 0.93)
    ax.set_ylim(0, 0.75)
    ax.set_xlabel("Threshold $K$ (Jaccard distance)")
    ax.set_ylabel("Power", color=BLUE)
    ax.tick_params(axis="y", labelcolor=BLUE)
    ax.axvline(c["oracle_th"], color=GRAY, ls=":", lw=0.8)
    ax.text(0.815, 0.70, "oracle $K$=0.8", fontsize=6.5, color=GRAY, ha="left")
    ax.text(0.76, 0.035, "safe zone ($K\\geq 0.6$)", fontsize=6.5, color=GREEN,
            ha="center")
    ax2 = ax.twinx()
    ax2.grid(False)
    ax2.plot(K, F, "-s", color=CRIM, ms=3.2, lw=1.1, label="FAR (right)")
    ax2.set_ylim(0, 0.83)
    ax2.set_ylabel("FAR", color=CRIM)
    ax2.tick_params(axis="y", labelcolor=CRIM)
    ax2.spines["right"].set_visible(True)
    handles = [Line2D([], [], color=BLUE, marker="o", ms=3, label="Power"),
               Line2D([], [], color=CRIM, marker="s", ms=3, label="FAR")]
    ax.legend(handles=handles, loc="center left", frameon=False)
    ax.set_title("(a) Threshold sweep (D6, silent drift)", loc="left")

    # (b) held-out splits
    ax = axes[1]
    ax.axvspan(0.0, 0.25, color=LGRAY, alpha=0.5, lw=0)
    ax.text(0.24, 0.368, "FAR budget", fontsize=6.3, color=GRAY, ha="right",
            va="bottom")
    for s in c["splits"]:
        sens = (s["th"] <= 0.5)
        ax.plot([s["eval_far"]], [s["eval_power"]], "o", ms=5,
                mfc=ORANGE if sens else BLUE, mec="white", mew=0.5,
                color=ORANGE if sens else BLUE)
    ax.plot([c["oracle_far"]], [c["oracle_power"]], "*", color=CRIM, ms=10,
            zorder=6)
    ax.annotate("oracle", xy=(c["oracle_far"], c["oracle_power"]),
                xytext=(0.10, 0.80), fontsize=6.8, color=CRIM,
                arrowprops=dict(arrowstyle="->", lw=0.7, color=CRIM))
    ax.text(0.235, 0.415, "splits exceeding the FAR budget\nall picked "
            "sensitive $K$=0.5 (orange)", fontsize=6.3, color=GRAY,
            ha="center", va="bottom")
    ax.set_xlim(0, 0.44)
    ax.set_ylim(0.35, 0.88)
    ax.set_xlabel("Held-out FAR")
    ax.set_ylabel("Held-out power")
    ygrid(ax)
    handles = [Line2D([], [], marker="o", ls="none", mfc=ORANGE, mec="white",
                      label="$K$*=0.5 (sensitive)"),
               Line2D([], [], marker="o", ls="none", mfc=BLUE, mec="white",
                      label="$K$*$\\geq$0.6 (conservative)"),
               Line2D([], [], marker="*", ls="none", color=CRIM,
                      label="oracle ($K$=0.8)")]
    ax.legend(handles=handles, loc="upper right", frameon=False, ncol=1)
    ax.set_title("(b) Cross-database transfer (10 splits)", loc="left")
    save(fig, "fig09_threshold_transfer")


# ---------------------------------------------------------------- fig10
def fig10():
    """E12: PSI/structural baseline vs. answer-level truth."""
    scen = E12["scenarios"]
    fams = ["V", "W2", "S"]

    def stats(fam):
        pts = [(k, v) for k, v in scen[fam].items() if v["event_q"] >= 1]
        n = len(pts)
        d25 = sum(1 for _, v in pts if v["alarm"]["psi@0.25"])
        d10 = sum(1 for _, v in pts if v["alarm"]["psi@0.1"])
        st = sum(1 for _, v in pts if v["structural"])
        return n, d25, d10, st

    counts = {f: stats(f) for f in fams}
    print("  fig10 events/psi25/psi10/struct:", counts)
    ben = scen["BENIGN"]
    b25 = sum(1 for v in ben.values() if v["alarm"]["psi@0.25"])
    b10 = sum(1 for v in ben.values() if v["alarm"]["psi@0.1"])
    print("  fig10 BENIGN alarms @0.25: %d/%d, @0.1: %d/%d"
          % (b25, len(ben), b10, len(ben)))

    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.6), constrained_layout=True)

    # (a) detection counts
    ax = axes[0]
    x = np.arange(3)
    offs = {"Events": (-0.30, LGRAY), "PSI@0.25": (-0.10, BLUE),
            "PSI@0.10": (0.10, LBLUE), "Structural": (0.30, GREEN)}
    for lab, (dx, color) in offs.items():
        idx = {"Events": 0, "PSI@0.25": 1, "PSI@0.10": 2, "Structural": 3}[lab]
        vals = [counts[f][idx] for f in fams]
        bars = ax.bar(x + dx, vals, 0.19, color=color, label=lab,
                      edgecolor="white", linewidth=0.3)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v + 0.45, str(v),
                    ha="center", va="bottom", fontsize=6.3)
    ax.set_xticks(x)
    ax.set_xticklabels(["V (value)", "W2 (workload)", "S (loud)"])
    ax.set_ylim(0, 27)
    ygrid(ax)
    ax.set_ylabel("Library-checkpoints with answer drift")
    ax.set_title("(a) Column-PSI detection vs. events", loc="left")
    ax.legend(loc="upper right", frameon=False, ncol=2, columnspacing=0.9,
              handlelength=1.2)

    # (b) max PSI vs. event question count (log-log)
    ax = axes[1]
    for fam, color, mk in (("V", BLUE, "o"), ("W2", ORANGE, "^")):
        pts = [(v["event_q"], v["max_psi"], v["alarm"]["psi@0.25"])
               for v in scen[fam].values() if v["event_q"] >= 1]
        hit = [(a, b) for a, b, h in pts if h]
        miss = [(a, b) for a, b, h in pts if not h]
        if hit:
            ax.plot([a for a, _ in hit], [b for _, b in hit], mk, ms=4.5,
                    mfc=color, mec="white", mew=0.4, color=color,
                    label="%s: alarm@0.25" % fam)
        if miss:
            ax.plot([a for a, _ in miss], [b for _, b in miss], mk, ms=4.5,
                    mfc="none", mec=color, mew=0.8, color=color, ls="none",
                    label="%s: missed" % fam)
    ax.axhline(0.25, color=CRIM, lw=0.7)
    ax.axhline(0.10, color=CRIM, lw=0.7, ls=":")
    ax.text(27, 0.30, "PSI 0.25", fontsize=6, color=CRIM, ha="right")
    ax.text(27, 0.121, "PSI 0.10", fontsize=6, color=CRIM, ha="right")
    ax.annotate("answer drift with\nnegligible PSI",
                xy=(9, 0.0002), xytext=(1.6, 0.0006), fontsize=6.5,
                color=GRAY, ha="center",
                arrowprops=dict(arrowstyle="->", lw=0.7, color=GRAY))
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(0.85, 32)
    ax.set_ylim(1.3e-4, 30)
    ax.set_xticks([1, 2, 5, 10, 20])
    ax.get_xaxis().set_major_formatter(ScalarFormatter())
    ax.set_yticks([1e-4, 1e-3, 1e-2, 1e-1, 1, 10])
    ax.get_yaxis().set_major_formatter(ScalarFormatter())
    ax.set_xlabel("Questions with drifted answers")
    ax.set_ylabel("Max column PSI")
    ax.set_title("(b) PSI vs. answer-drift magnitude", loc="left")
    ax.legend(loc="upper left", frameon=False, fontsize=6.3, ncol=2,
              columnspacing=0.8, handlelength=1.0)
    save(fig, "fig10_psi_baseline")


# ---------------------------------------------------------------- fig11
def fig11():
    """E6: v4 (effort=low) empty-SQL rate per database (case study)."""
    tot = collections.Counter()
    emp = collections.Counter()
    root = os.path.join(BASE, "gen_v4")
    for p in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        rel = os.path.relpath(p, root)
        db = rel.split(os.sep)[-2]
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                sql = rec.get("sql", rec.get("content"))
                tot[db] += 1
                if sql is None or str(sql).strip() == "":
                    emp[db] += 1
    core = ["superhero", "student_club", "toxicology",
            "thrombosis_prediction", "california_schools"]
    rates = sorted(((db, 100.0 * emp[db] / tot[db], tot[db]) for db in core),
                   key=lambda t: t[1], reverse=True)
    overall = 100.0 * sum(emp.values()) / sum(tot.values())
    print("  fig11 per-db rates:", [(d, "%.1f%%" % r, "n=%d" % n)
                                    for d, r, n in rates],
          "overall %.1f%%" % overall)

    fig, ax = plt.subplots(figsize=(3.5, 2.4), constrained_layout=True)
    y = np.arange(len(rates))[::-1]
    bars = ax.barh(y, [r for _, r, _ in rates], 0.62, color=BLUE,
                   edgecolor="white", linewidth=0.4)
    for b, (_, r, n) in zip(bars, rates):
        ax.text(r + 1.2, b.get_y() + b.get_height() / 2, "%.1f%%  (n=%s)"
                % (r, format(n, ",")), va="center", fontsize=6.6)
    ax.set_yticks(y)
    ax.set_yticklabels([d.replace("_", " ") for d, _, _ in rates])
    ax.axvline(overall, color=CRIM, ls="--", lw=1.0)
    ax.text(overall + 1.2, len(rates) - 0.52, "overall %.1f%%" % overall,
            fontsize=6.8, color=CRIM, va="center")
    ax.set_xlim(0, 118)
    ax.set_ylim(-0.55, len(rates) - 0.1)
    xgrid(ax)
    ax.set_xlabel("Empty-SQL rate (%)")
    ax.set_title("v4 (effort=low): reasoning starves the token budget",
                 loc="left")
    save(fig, "fig11_v4_artifact")


# ---------------------------------------------------------------- main
def main():
    print("matplotlib %s / numpy %s" % (matplotlib.__version__, np.__version__))
    print("output dir: %s" % FIGS)
    for f in (fig01, fig02, fig03, fig04, fig05, fig06, fig07,
              fig08, fig09, fig10, fig11):
        print(f.__name__)
        f()
    print("ALL 11 FIGURES DONE")


if __name__ == "__main__":
    main()
