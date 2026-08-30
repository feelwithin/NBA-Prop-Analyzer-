"""
Generates a reliability (calibration) chart from backtest.py's output —
predicted probability buckets on the x-axis, actual hit rate on the y-axis,
against the perfect-calibration diagonal. Run scripts/backtest.py first
(it writes data/output/backtest_results.csv), then:

    python3 scripts/make_calibration_chart.py

Writes assets/calibration_chart.png.
"""
import csv
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
RESULTS_CSV = ROOT / "data" / "output" / "backtest_results.csv"
OUT_PATH = ROOT / "assets" / "calibration_chart.png"


def load_results():
    rows = []
    with open(RESULTS_CSV, newline="") as f:
        for r in csv.DictReader(f):
            rows.append({"predicted_prob": float(r["predicted_prob"]), "hit": int(r["hit"])})
    return rows


def bucket(rows):
    buckets = defaultdict(list)
    for r in rows:
        b = min(int(r["predicted_prob"] * 10), 9)
        buckets[b].append(r)
    xs, ys, ns = [], [], []
    for b in sorted(buckets):
        items = buckets[b]
        avg_pred = sum(i["predicted_prob"] for i in items) / len(items)
        actual = sum(i["hit"] for i in items) / len(items)
        xs.append(avg_pred)
        ys.append(actual)
        ns.append(len(items))
    return xs, ys, ns


def main():
    if not RESULTS_CSV.exists():
        raise SystemExit(f"{RESULTS_CSV} not found — run scripts/backtest.py first.")

    rows = load_results()
    xs, ys, ns = bucket(rows)
    n_total = len(rows)
    brier = sum((r["predicted_prob"] - r["hit"]) ** 2 for r in rows) / n_total
    baseline_brier = sum((0.5 - r["hit"]) ** 2 for r in rows) / n_total

    fig, ax = plt.subplots(figsize=(6.5, 6), dpi=150)
    ax.plot([0, 1], [0, 1], linestyle="--", color="#8A97A8", linewidth=1.5, label="Perfect calibration")

    sizes = [max(30, min(400, n / max(ns) * 400)) for n in ns]
    ax.scatter(xs, ys, s=sizes, color="#1D6FFF", zorder=3, label="Model (bucketed)")
    for x, y, n in zip(xs, ys, ns):
        ax.annotate(f"n={n}", (x, y), textcoords="offset points", xytext=(6, 6), fontsize=8, color="#4B5A6E")

    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("Predicted probability (bucket average)")
    ax.set_ylabel("Actual hit rate")
    ax.set_title(f"Calibration — Brier {brier:.4f} vs. baseline {baseline_brier:.4f}\n"
                 f"({n_total:,} backtested picks)")
    ax.legend(loc="upper left", frameon=False)
    ax.grid(alpha=0.25)
    fig.tight_layout()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PATH)
    print(f"Wrote {OUT_PATH} (Brier {brier:.4f}, n={n_total})")


if __name__ == "__main__":
    main()
