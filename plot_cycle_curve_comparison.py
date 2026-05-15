"""
Compare charge/discharge curves at cycles 100 and 1200:
  - Trained (data/raw full-life measurements)
  - Measured (tester input: raw_early_200 for cyc 100; raw ground-truth for cyc 1200)
  - Predicted (simulated from predicted retention)

X-axis: cumulative capacity (Ah)
Y1: voltage (V)   Y2: current (A)
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from generate_synthetic_data import (
    CHARGE_PROFILE,
    DISCHARGE_PROFILE,
    simulate_charge_step,
    simulate_discharge_step,
)
from predict_health import PREDICTION_HORIZON, PREDICTION_START, predict_tester_trajectory

POINTS_CHARGE = 12
POINTS_DISCHARGE = 15


def load_cycle_from_raw(raw_path: Path, battery_id: str, cycle: int) -> pd.DataFrame:
    df = pd.read_csv(raw_path)
    cyc = df[(df["battery_id"] == battery_id) & (df["cycle_index"] == cycle)].copy()
    if cyc.empty:
        raise ValueError(f"No data for {battery_id} cycle {cycle} in {raw_path}")
    cyc["timestamp"] = pd.to_datetime(cyc["timestamp"])
    return cyc.sort_values("timestamp")


def _segment_capacity_ah(seg: pd.DataFrame) -> np.ndarray:
    """Cumulative |I|*dt (Ah) within a stage segment."""
    if seg.empty:
        return np.array([])
    ts = seg["timestamp"]
    dt_h = ts.diff().dt.total_seconds().fillna(30.0) / 3600.0
    return (seg["current_a"].abs() * dt_h).cumsum().values


def raw_to_qvi(cycle_df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Build full charge+discharge profile on cumulative capacity axis.
    Returns (capacity_ah, voltage_v, current_a).
    """
    q_all, v_all, i_all = [], [], []
    q_offset = 0.0

    for stage in ("charge", "discharge"):
        seg = cycle_df[cycle_df["stage_name"] == stage].copy()
        if seg.empty:
            continue
        q_seg = _segment_capacity_ah(seg)
        if len(q_seg) == 0:
            continue
        q_all.append(q_seg + q_offset)
        v_all.append(seg["voltage_v"].values)
        i_all.append(seg["current_a"].values)
        q_offset = q_all[-1][-1] + 0.02  # small gap between charge and discharge

    return np.concatenate(q_all), np.concatenate(v_all), np.concatenate(i_all)


def simulate_predicted_cycle(
    nominal_capacity_ah: float,
    retention: float,
    seed: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Simulate charge+discharge using same profiles as data generator."""
    rng = np.random.default_rng(seed)
    q_parts, v_parts, i_parts = [], [], []
    q_off = 0.0

    for step in CHARGE_PROFILE:
        v, i = simulate_charge_step(step, nominal_capacity_ah, retention, POINTS_CHARGE, rng)
        q = np.linspace(0, nominal_capacity_ah * retention * 0.45, len(v)) + q_off
        q_parts.append(q)
        v_parts.append(v)
        i_parts.append(i)
        q_off = q[-1] + 0.02

    for step in DISCHARGE_PROFILE:
        v, i = simulate_discharge_step(step, nominal_capacity_ah, retention, POINTS_DISCHARGE, rng)
        q = np.linspace(0, nominal_capacity_ah * retention * 0.95, len(v)) + q_off
        q_parts.append(q)
        v_parts.append(v)
        i_parts.append(i)
        q_off = q[-1]

    return np.concatenate(q_parts), np.concatenate(v_parts), np.concatenate(i_parts)


def retention_at_tester_cycle(
    battery_id: str,
    tester_cycle: int,
    early_summary: pd.DataFrame,
    predicted_eol: float,
) -> float:
    """Observed retention (<=200) or predicted trajectory (>=201)."""
    obs = early_summary[
        (early_summary["battery_id"] == battery_id)
        & (early_summary["cycle_index"] == tester_cycle)
    ]
    if not obs.empty:
        return float(obs["capacity_retention"].iloc[0])

    last = early_summary[early_summary["battery_id"] == battery_id].sort_values("cycle_index").iloc[-1]
    last_ret = float(last["capacity_retention"])
    if tester_cycle < PREDICTION_START:
        return last_ret
    _, _, pred_ret = predict_tester_trajectory(last_ret, predicted_eol)
    return float(np.interp(tester_cycle, np.arange(PREDICTION_START, PREDICTION_HORIZON + 1), pred_ret))


def plot_comparison(
    battery_id: str,
    cycles: list[int],
    train_raw: Path,
    tester_raw: Path,
    early_summary: pd.DataFrame,
    meta: pd.DataFrame,
    predictions: pd.DataFrame,
    out_path: Path,
) -> None:
    nominal = float(meta.loc[meta["battery_id"] == battery_id, "nominal_capacity_ah"].iloc[0])
    pred_eol = float(
        predictions.loc[predictions["battery_id"] == battery_id, "predicted_eol_tester"].iloc[0]
    )

    fig, axes = plt.subplots(len(cycles), 1, figsize=(13, 5 * len(cycles)), squeeze=False)
    styles = {
        "trained": {"color": "#2563eb", "ls": "-", "lw": 1.8, "label": "Trained (data/raw)"},
        "measured": {"color": "#16a34a", "ls": "-", "lw": 1.8, "label": "Measured"},
        "predicted": {"color": "#dc2626", "ls": "--", "lw": 2.0, "label": "Predicted"},
    }

    for ax1, cyc in zip(axes[:, 0], cycles):
        ax2 = ax1.twinx()
        curves: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}

        # Trained / full-life actual
        curves["trained"] = raw_to_qvi(load_cycle_from_raw(train_raw, battery_id, cyc))

        # Measured: tester file for cyc<=200, else full raw ground truth
        if cyc <= 200:
            curves["measured"] = raw_to_qvi(load_cycle_from_raw(tester_raw, battery_id, cyc))
            styles["measured"]["label"] = "Measured (tester raw_early_200)"
        else:
            curves["measured"] = curves["trained"]
            styles["measured"]["label"] = "Measured (data/raw ground truth)"

        ret = retention_at_tester_cycle(battery_id, cyc, early_summary, pred_eol)
        seed = hash((battery_id, cyc)) % (2**31)
        curves["predicted"] = simulate_predicted_cycle(nominal, ret, seed)

        for key, (q, v, i) in curves.items():
            st = styles[key]
            ax1.plot(q, v, color=st["color"], linestyle=st["ls"], linewidth=st["lw"], label=f"{st['label']} — V")
            ax2.plot(q, i, color=st["color"], linestyle=st["ls"], linewidth=st["lw"] * 0.85, alpha=0.75, label=f"{st['label']} — I")

        ax1.set_ylabel("Voltage (V)", color="#1e40af")
        ax2.set_ylabel("Current (A)", color="#b45309")
        ax1.set_xlabel("Capacity (Ah)")
        ax1.set_title(f"{battery_id} — Cycle {cyc}  (retention≈{ret*100:.1f}%)")
        ax1.grid(True, alpha=0.3)

        lines1, lab1 = ax1.get_legend_handles_labels()
        lines2, lab2 = ax2.get_legend_handles_labels()
        # Deduplicate legend entries (V only labels)
        seen, handles, labels = set(), [], []
        for h, lb in zip(lines1 + lines2, lab1 + lab2):
            base = lb.split(" — ")[0]
            if base not in seen:
                seen.add(base)
                handles.append(h)
                labels.append(base)
        ax1.legend(handles, labels, loc="upper right", fontsize=8)

    fig.suptitle(
        "Charge/Discharge Curve Comparison\n"
        f"X: Capacity (Ah)  |  Y1: Voltage (V)  |  Y2: Current (A)",
        fontsize=12,
        y=1.01,
    )
    fig.tight_layout()
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--battery-id", default="BAT_001")
    parser.add_argument("--cycles", type=int, nargs="+", default=[100, 1200])
    parser.add_argument("--train-raw", type=Path, default=Path("data/raw/raw_cycle_data.csv"))
    parser.add_argument("--tester-raw", type=Path, default=Path("data/raw_early_200/raw_cycle_data.csv"))
    parser.add_argument("--out", type=Path, default=Path("reports/figures/cycle_curve_comparison.png"))
    args = parser.parse_args()

    early_summary = pd.read_csv("data/raw_early_200/cycle_summary.csv")
    meta = pd.read_csv("data/raw_early_200/battery_metadata.csv")
    predictions = pd.read_csv("reports/predictions.csv")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    plot_comparison(
        args.battery_id,
        args.cycles,
        args.train_raw,
        args.tester_raw,
        early_summary,
        meta,
        predictions,
        args.out,
    )
    print(f"Saved -> {args.out}")


if __name__ == "__main__":
    main()
