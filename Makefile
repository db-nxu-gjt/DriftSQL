PY ?= python
SRC = src

.PHONY: verify evaluate evaluate-full figures export reproduce benchmark clean

# --- zero-API reproduction from shipped archives ---
# `evaluate` needs only the repo (archives); `evaluate-full` additionally
# needs the BIRD dev set in data/ (DRIFTSQL_BIRD_DIR) and materialized
# snapshots (make benchmark + build_v_benign.py).

verify:            ## validate every archived number used in the paper
	$(PY) $(SRC)/check_paper_data.py

evaluate:          ## archive-only evaluations (E4/E5/E8/E13, placebo, coverage law)
	$(PY) $(SRC)/run_detectors.py
	$(PY) $(SRC)/run_detectors_crossgen.py
	$(PY) $(SRC)/run_detectors_w3.py
	$(PY) $(SRC)/run_robustness.py
	$(PY) $(SRC)/run_p04.py
	$(PY) $(SRC)/placebo_control.py
	$(PY) $(SRC)/verify_anchor_law.py

evaluate-full: benchmark evaluate   ## + evaluations that read DB snapshots
	$(PY) $(SRC)/build_v_benign.py
	$(PY) $(SRC)/run_detectors_benign.py
	$(PY) $(SRC)/run_table_psi.py
	$(PY) $(SRC)/run_combined_monitor.py

figures:           ## render the 11 paper figures (PDF + PNG)
	$(PY) $(SRC)/make_paper_figures.py

export: verify     ## export every paper number to one Excel, then re-verify it
	$(PY) $(SRC)/export_paper_results.py
	$(PY) $(SRC)/verify_excel.py

reproduce: evaluate figures export

# --- full rebuild (requires BIRD dev in data/ and API key) ---

benchmark:         ## rebuild families/transforms/snapshots/gold from BIRD dev
	$(PY) $(SRC)/build_drift_benchmark.py

clean:
	rm -rf $(SRC)/results/figs $(SRC)/results_export
