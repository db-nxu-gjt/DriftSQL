Code and data release for the ICDE 2027 submission (EAB category)
*"DriftSQL: Benchmarking and Monitoring Silent Data Drift in Text-to-SQL
Systems"* by Jintao Gao (School of Information Engineering, Ningxia
University).

Text-to-SQL pipelines are usually validated once, at deployment time, against
a frozen database snapshot. Real databases evolve — schemas are renamed,
columns are dropped, values decay — and the answers of a Text-to-SQL system
can silently change long after its evaluation report was written. DriftSQL is
a benchmark and monitoring framework for studying this problem:

- **5 drift families** built from BIRD dev in parallel universes:
  `S` (schema/loud), `V` (value/silent), `W1` (workload shift),
  `W2` (workload/silent), `W3` (intent-preserving rewrites, benign control),
  plus a verified benign data-evolution control (`V-benign`);
- **64 drift snapshots + 25 benign snapshots**, 2,568 question-checkpoint
  units with drift-aware gold labels (re-executed per checkpoint);
- **8 runtime detectors** (pipeline-side and distribution-side signals) and
  a **two-stage monitor** — screening signals trigger a per-question gold
  anchor confirmation — evaluated on power / delay / false-alarm rate,
  gold-anchor budget curves, and a rate-matched placebo control.

Everything in the benchmark is **deterministic**: seeded stratified question
selection, seeded targeted transforms, and re-executable gold labels. All
evaluation stages are **zero-API** and re-run locally from the archived
artifacts shipped in this repository.

## Repository layout

```
├── src/                    # all pipeline code (flat; scripts import each other)
│   ├── build_drift_benchmark.py   # Stage B1: families, transforms, DB snapshots, gold
│   ├── build_v_benign.py           # Stage B2: benign data-evolution control (E10)
│   ├── build_w3_rewrites.py        # Stage B3: W3 intent-preserving rewrites (API)
│   ├── build_gold_w3.py            # Stage B4: W3 gold labels
│   ├── gen_candidates.py           # Stage C1: candidates, primary generator (API)
│   ├── gen_candidates_v4b.py       # Stage C2: candidates, second generator (API)
│   ├── gen_candidates_w3.py       # Stage C5: W3 candidates, both generators (API)
│   ├── gen_candidates_v4.py       # deprecated v4 variant (kept: artifact case study, E6)
│   ├── run_judge_baseline*.py      # Stage C3: judge confidence signals (API)
│   ├── run_detectors.py            # Stage D: 8-detector evaluation (E4)
│   ├── run_detectors_*.py          # E8 cross-generator, E10 benign, W3 control
│   ├── run_combined_monitor.py     # E11: two-stage monitor, anchor budget, anatomy
│   ├── run_table_psi.py            # E12: table-level PSI / structure baselines
│   ├── run_robustness.py           # E5/E13: bootstrap CIs, sensitivity sweeps
│   ├── run_p04.py                  # E13: threshold transfer, DB-level bootstrap
│   ├── placebo_control.py          # placebo: persistence vs. sensitivity
│   ├── verify_anchor_law.py        # coverage-law full-curve verification
│   ├── check_paper_data.py         # full-archive validation of shipped results
│   ├── export_paper_results.py     # one-file Excel export of every number
│   ├── make_paper_figures.py       # 11 publication figures from archives
│   ├── questions/  transforms/  gold/      # shipped frozen benchmark artifacts
│   ├── gen/  gen_v4b/  gen_w3/  gen_w3b/  gen_v4/   # shipped candidate archives
│   └── results/                           # shipped frozen experiment outputs
├── data/                    # you: download BIRD dev here (gitignored)
├── docs/REPRODUCING.md      # paper figure/table -> script -> output map
├── requirements.txt
└── Makefile
```

## Setup

Python 3.8+ (stdlib only for evaluation; plotting/export need three packages):

```bash
pip install -r requirements.txt
```

### Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `DRIFTSQL_BIRD_DIR` | BIRD dev directory | `data/bird/dev_20240627` |
| `DRIFTSQL_API_BASE` | LLM API base URL | `https://api.deepseek.com` |
| `GATE_LLM_API_KEY` | API key (generation stages only) | — |
| `DRIFTSQL_FIGS_DIR` | figure output directory | `src/results/figs` |
| `DRIFTSQL_EXPORT_DIR` | Excel export directory | `src/results_export` |

## Quick start: reproduce the paper from shipped archives (zero API)

The repository ships every archived artifact (candidate archives, judge
records, frozen results), so all numbers and figures regenerate locally:

```bash
make verify          # validate the full archive against every paper number
make evaluate        # re-run archive-only evaluations (E4/E5/E8/E13, placebo, law)
make figures         # render the 11 paper figures (PDF + 300-dpi PNG)
make export          # single Excel with every number, per experiment sheet
```

or one command:

```bash
make reproduce
```

Three further scripts read materialized database snapshots, which are not
shipped (they rebuild deterministically from the manifests — see the full
rebuild section): `run_detectors_benign.py` (E10), `run_table_psi.py`
(E12), and `run_combined_monitor.py` (E11; T1/T2 archive-only, T3 reads
the original BIRD databases). After `make benchmark` + `build_v_benign.py`
they run via `make evaluate-full`.

Individual stages:

```bash
python src/run_detectors.py            # E4  8 detectors: power/delay/FAR per family
python src/run_detectors_crossgen.py   # E8  same-question chat vs v4b
python src/run_detectors_w3.py          # W3  benign rewrite control
python src/run_robustness.py           # E5  bootstrap CI + sensitivity
python src/run_p04.py                   # E13 threshold transfer + DB bootstrap
python src/placebo_control.py          # rate-matched placebo control
python src/verify_anchor_law.py        # coverage law, m = 1..25 full curve
```

Each script prints a summary and writes JSON/Markdown reports to
`src/results/`. Because generation is seeded and evaluation is
deterministic, re-running overwrites the shipped files with identical
content — this is by design (auditability).

## Full rebuild from scratch (requires API access)

1. **Download BIRD dev** (dev_20240627) from the
   [BIRD repo](https://github.com/AlibabaResearch/DAMO-ConvAI/tree/main/bird)
   and place it at `data/bird/dev_20240627` (contains `dev.json` and
   `dev_databases/`), or point `DRIFTSQL_BIRD_DIR` at it.

2. **Build the benchmark** (deterministic, zero API):

   ```bash
   python src/build_drift_benchmark.py
   ```

   Produces `questions/`, `transforms/manifest_*.json`, materialized
   snapshots `dbs/{family}/{db}/ck{t}.sqlite` for the 5 lightweight
   databases, and re-executed drift-aware gold labels `gold/`.

3. **Generate candidates** (API, resumable — every unit with 10 stored
   rows is skipped):

   ```bash
   export GATE_LLM_API_KEY=sk-...        # or setx on Windows
   python src/gen_candidates.py          # primary generator (deepseek-chat)
   python src/gen_candidates_v4b.py      # second generator (v4, thinking disabled)
   ```

   10 candidates per (family, db, checkpoint, question), temperature 1.0,
   content logprobs recorded, executed on the drift snapshot and labeled
   against drift-aware gold.

4. **W3 rewrite family** (API + local):

   ```bash
   python src/build_w3_rewrites.py      # LLM rewrites + judge equivalence check
   python src/build_gold_w3.py          # gold = t0 result, all checkpoints
   python src/gen_candidates_w3.py chat
   python src/gen_candidates_w3.py v4b
   ```

5. **Judge signals** (API): `run_judge_baseline.py`,
   `run_judge_baseline_v4b.py`, `run_judge_w3.py`.

6. **Benign control** (zero API, needs BIRD):
   `python src/build_v_benign.py` then `python src/run_detectors_benign.py`.

7. **Re-run all evaluations**: `make evaluate && make figures`.

The total API cost is dominated by ~5,100 units x 10 candidates x 2
generators (~102k completions) plus judge calls; everything after that is
local. `gen_candidates_v4.py` and `pilot_v4b.py` document the
reasoning-token artifact case study (paper Fig. 11) and are kept for
reproducibility of that finding.

## Reproducing a specific paper figure or table

See [docs/REPRODUCING.md](docs/REPRODUCING.md) for the complete map:
paper item -> script -> archived output -> expected value.

## Design notes

- **Families are parallel universes**: each family evolves independently
  from the original BIRD databases; families never contaminate each other.
- **Only the 5 lightweight databases are materialized** (~23 MB/db); any
  other BIRD database can be deterministically rebuilt from
  `transforms/manifest_*.json` via the materialize engine.
- **Drift-aware gold**: at every checkpoint the family gold SQL is
  re-executed on the drift snapshot; the label is the strict result-set
  fingerprint (sorted canonical rows, SHA-256 + row count + exec status),
  not the stale t0 answer.
- **API keys are never written to disk** — only read from the environment.

## License

MIT — see [LICENSE](LICENSE).
