# Reproducing the Paper

Every number and figure in the TKDE submission regenerates from this
repository without any API access. This document maps each paper item to
its script, archived output, and expected values.

**Notation.** Families: `S` schema/loud, `V` value/silent, `W1` workload
shift, `W2` workload/silent, `W3` intent-preserving rewrites (benign
control), `V-benign` benign data-evolution control. Checkpoints per
family: S/V 5, W2 3, W1 4, W3 3.

## Global verification

```bash
python src/check_paper_data.py
```

Re-parses every archived result in `src/results/`, re-computes anchor
numbers (headline statistics quoted in the paper), and cross-checks the
benchmark assets (spec, gold, snapshots, manifests, generation summaries,
judge records). Exits non-zero on any mismatch. Run this first on any
clone.

## Tables

| Paper item | Content | Script | Archived output |
|---|---|---|---|
| Table I (`tab:taxonomy`) | Family taxonomy: loud/silent x schema/data/intent | — (design constants) | `src/benchmark_spec.json` |
| Table II (`tab:bench`) | Composition: 64 drift + 25 benign snapshots, 2,568 units; 868 = 900 − 32 (toxicology has no date column, ck3 skipped); cross-generator common set 118 = 150 − 32 | `build_drift_benchmark.py` | `benchmark_spec.json`, `questions/`, `transforms/` |
| Table III (`tab:signals`) | 8 detectors: D1 EX drop (E=0.30), D2 logprob KS (α=0.05), D3 judge (drop>20 or abs<50), D4 top-share (0.30), D5 mode fingerprint, D6 multiset Jaccard (K=0.30), D7 CUSUM (0.3, 2.0), D8 gold oracle | `run_detectors.py` (thresholds are module constants) | — |
| Main results (`tab:main`) | Power/delay/FAR per family per detector | `run_detectors.py` | `results/detector_results.json`, `detector_report.md` |
| Two-stage monitor (`tab:twostage`) | Screening → gold-anchor confirmation; power 0.700 (D5 rate) / 0.867 (D6∪OR); latched FAR; confirmation cost | `run_combined_monitor.py` (T1) | `results/combined_monitor.json/.md` |
| PSI vs. answer-level (`tab:psi`) | Table-level PSI low power on answer drift (V 0.417 / W2 0.111); structure signals = DDL diff | `run_table_psi.py` | `results/table_psi.json/.md` |

## Figures

| Fig. | Paper item | Claim | Script | Input |
|---|---|---|---|---|
| fig01 | `fig:power` | no single runtime signal covers loud + silent | `make_paper_figures.py` | `detector_results.json` (E4, `run_detectors.py`) |
| fig02 | `fig:ci` | loud/silent power asymmetry, 95% bootstrap CI | `make_paper_figures.py` | `robustness.json` (E5, `run_robustness.py`) |
| fig03 | `fig:crossgen` | silent signals reproduce across generators | `make_paper_figures.py` | `detector_crossgen.json` (E8, `run_detectors_crossgen.py`) |
| fig04 | `fig:benign` | screening FAR on benign evolution; gold anchor = 0 | `make_paper_figures.py` | `benign_control.json` (E10, `run_detectors_benign.py`) + `detector_w3.json` (W3) |
| fig05 | `fig:pareto` | screening-latched → two-stage, FAR = 0 | `make_paper_figures.py` | `combined_monitor.json` (E11-T1) |
| fig06 | `fig:dumbbell` | two-stage power gain per family x generator | `make_paper_figures.py` | `combined_monitor.json` (E11-T1) |
| fig07 | `fig:budget` | DB-level power vs. number of gold anchors | `make_paper_figures.py` | `combined_monitor.json` (E11-T2) |
| fig08 | `fig:exec` | exec_ok flat (0.982–0.985) while correctness collapses | `make_paper_figures.py` | `robustness.json` (E13c) |
| fig09 | `fig:transfer` | threshold transfer: K sweep + 10 held-out splits (oracle K=0.8, holdout loss 0.045) | `make_paper_figures.py` | `p04_threshold_bootstrap.json` (E13a) |
| fig10 | `fig:psi` | PSI baseline: low power on answer-level drift | `make_paper_figures.py` | `table_psi.json` (E12) |
| fig11 | `fig:artifact` | v4 artifact case study: per-DB empty-SQL rate | `make_paper_figures.py` | `gen_v4/` (E6; see `pilot_v4b.json`) |

```bash
python src/make_paper_figures.py     # writes results/figs/fig01..fig11 (.pdf + .png)
```

## Headline numbers and where they come from

| Paper claim | Value | Reproduce with |
|---|---|---|
| Loud/silent asymmetry | no pipeline signal detects silent drift; distribution signals do | `run_detectors.py` → `detector_report.md` |
| Two-stage power | 0.700 (D5 rate) / 0.867 (D6@0.30 ∪ OR) | `run_combined_monitor.py` → `combined_monitor.md` |
| Placebo upper bounds | 0.204 (f̂=0.080) / 0.549 (f̂=0.265); sensitivity share ≥ 0.318; latched 0.500 ≤ 0.549 | `placebo_control.py` (stdout) |
| Coverage law, full curve | m ∈ {1,3,5,10,25}; pred vs. meas max deviation 0.026 (e.g. S@1 0.762/0.761) | `verify_anchor_law.py` (stdout) + `combined_monitor.json` |
| Three-family false-alarm control | 1,314 control units (W3 564 + benign 750), zero alarms; 95% upper bound 3/1314 ≈ 0.23% | `run_detectors_w3.py` + `run_detectors_benign.py` |
| Cross-generator common set | 118 = 150 − 32 questions | `run_detectors_crossgen.py` |
| exec_ok is drift-blind | exec rate 0.982–0.985 flat; correct collapses to 0.392/0.324 | `run_p04.py` → `p04_threshold_bootstrap.md` |
| Threshold transfer | oracle K=0.8; holdout loss 0.045; conservative K ≥ 0.6, holdout FAR ≤ 0.12 | `run_p04.py` |

## Experiment IDs (internal naming)

E1 benchmark build · E2 candidate generation (primary) · E4 8-detector
evaluation · E5 bootstrap CI · E6 v4 artifact (reasoning-token budget
exhaustion → empty SQL; fixed by `thinking: disabled` in v4b) · E8
cross-generator · E9/E10 benign controls · E11 combined monitor (T1
two-stage / T2 anchor budget / T3 anchor anatomy) · E12 PSI baselines ·
E13 robustness (a threshold transfer / b DB-level bootstrap / c EX
diagnosis). The Excel export (`make export`) carries one sheet per
experiment with every quoted number.
