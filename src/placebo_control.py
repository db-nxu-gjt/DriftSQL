"""Rate-matched placebo control for two-stage power (reviewer request).

Question: is DriftGuard's two-stage power just persistence + confirmation?
A random screener with the same per-unit alarm rate f as D5/D6 would, under
continue-after-reject semantics, confirm any alarm that lands at/after
onset. Its expected power is mean_q [1 - (1-f)^W_q] over onset questions,
where W_q = number of post-onset checkpoints.

We estimate f by MLE from the *first-alarm* distribution over never-drifted
(stable) questions: P(first alarm at position p) = (1-f)^(p-1) * f,
P(never) = (1-f)^L. OR screener's first alarm = min(D5, D6@0.30) first
alarms (exact, since alarm events are per-(q,t) deterministic).

Local, zero-API. Output: placebo power per config vs measured two-stage
power (from E11: D5 0.700, D6@0.30 0.867, OR 0.867).
"""
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
d = json.load(open(os.path.join(HERE, "results", "detector_results.json"),
                  encoding="utf-8"))
qs = d["questions"]


def mle_rate(stable):
    """MLE of constant per-unit alarm rate from first-alarm positions."""
    if not stable:
        return 0.0
    best_f, best_ll = 0.0, -1e18
    for i in range(1, 200):
        f = i / 200.0
        ll = 0.0
        for (L, a) in stable:  # a: 1-based position or None
            if a is None:
                ll += L * math.log(1 - f)
            else:
                ll += (a - 1) * math.log(1 - f) + math.log(f)
        if ll > best_ll:
            best_ll, best_f = ll, f
    return best_f


def placebo_power(onset_qs, f):
    if not onset_qs:
        return None
    return sum(1 - (1 - f) ** w for w in onset_qs) / len(onset_qs)


for fams, label in [(["V", "W2"], "SILENT"), (["V"], "V"), (["W2"], "W2"),
                    (["S"], "S")]:
    for det, name in [("D5_mode", "D5"), ("D6_jac", "D6@0.30"),
                      ("OR", "OR(D5|D6@0.30)")]:
        stable, onset_w = [], []
        for q in qs:
            if q["fam"] not in fams:
                continue
            ts = q["ts"]
            if det == "OR":
                a5, a6 = q["alarms"]["D5_mode"], q["alarms"]["D6_jac"]
                both = [a for a in (a5, a6) if a is not None]
                a = min(both) if both else None
            else:
                a = q["alarms"][det]
            pos = {t: i + 1 for i, t in enumerate(ts)}
            if q["onset"] is None:
                stable.append((len(ts), pos[a] if a is not None else None))
            else:
                w = sum(1 for t in ts if t >= q["onset"])
                if w:
                    onset_w.append(w)
        f = mle_rate(stable)
        pp = placebo_power(onset_w, f)
        if pp is not None:
            print(f"{label:7s} {name:15s} f_hat={f:.3f} n_stable={len(stable):3d}"
                  f" n_onset={len(onset_w):3d} placebo_two_stage_power={pp:.3f}")
