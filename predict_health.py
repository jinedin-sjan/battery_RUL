"""
Predict battery health on the tester cycle axis.

Train: data/raw — cycles 1-200 features, predict total EOL (physical life).
Infer: data/raw_early_200 — performance level of physical 501-700, but treated as
       tester cycles 1-200. Predict health for tester cycles 201-1200.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import joblib
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from feature_engineering import extract_features

DROP_COLS = {
    "battery_id",
    "actual_eol_cycle",
    "chemistry",
    "nominal_capacity_ah",
    "fade_per_cycle",
    "charge_profile_json",
    "discharge_profile_json",
    "cycle_offset",
    "reference_physical_cycle_start",
    "reference_physical_cycle_end",
    "remaining_eol_cycles",
}

# Tester timeline
TESTER_OBSERVED_MAX = 200
PREDICTION_START = 201
PREDICTION_HORIZON = 1200
HEALTH_CHECKPOINTS = list(range(50, PREDICTION_HORIZON + 1, 50))
EOL_RETENTION = 0.80


def predict_tester_trajectory(
    retention_at_obs_end: float,
    predicted_eol: float,
    pred_start: int = PREDICTION_START,
    pred_end: int = PREDICTION_HORIZON,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Linear fade on tester axis: anchor retention at cycle TESTER_OBSERVED_MAX,
    apply degradation from pred_start (201) to EOL_RETENTION at predicted_eol.

    Returns (cycles, health_percent, capacity_retention).
    """
    cycles = np.arange(pred_start, pred_end + 1, dtype=float)
    eol = float(np.clip(predicted_eol, pred_start + 1, pred_end + 500))
    slope = (EOL_RETENTION - retention_at_obs_end) / (eol - pred_start)
    retention = retention_at_obs_end + slope * (cycles - pred_start)
    retention = np.clip(retention, EOL_RETENTION, 1.05)
    return cycles, retention * 100.0, retention


def build_checkpoint_tables(
    battery_id: str,
    observed: pd.DataFrame,
    predicted_eol: float,
    nominal_capacity_ah: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Health & capacity at every 50 tester cycles; 1-200 observed, 201+ predicted."""
    obs = observed.sort_values("cycle_index")
    obs_end = int(obs["cycle_index"].max())
    last_row = obs.iloc[-1]
    last_ret = float(last_row["capacity_retention"])
    last_cap = float(last_row["capacity_ah"])

    pred_cycles, pred_health, pred_ret = predict_tester_trajectory(last_ret, predicted_eol)
    health_map = dict(zip(pred_cycles.astype(int), pred_health))
    ret_map = dict(zip(pred_cycles.astype(int), pred_ret))
    cap_map = {c: float(nominal_capacity_ah * r) for c, r in ret_map.items()}

    rows: list[dict] = []
    for c in HEALTH_CHECKPOINTS:
        if c <= obs_end:
            row = obs[obs["cycle_index"] == c]
            if row.empty:
                continue
            health = float(row["capacity_retention"].iloc[0]) * 100.0
            capacity = float(row["capacity_ah"].iloc[0])
            source = "observed"
        elif c in health_map:
            health = float(health_map[c])
            capacity = cap_map[c]
            source = "predicted"
        elif c > PREDICTION_HORIZON:
            continue
        else:
            health = float(np.interp(c, pred_cycles, pred_health))
            retention = float(np.interp(c, pred_cycles, pred_ret))
            capacity = nominal_capacity_ah * retention
            source = "predicted"

        rows.append(
            {
                "battery_id": battery_id,
                "tester_cycle": c,
                "health_percent": round(health, 2),
                "capacity_ah": round(capacity, 4),
                "source": source,
                "predicted_eol_tester": round(predicted_eol, 1),
            }
        )

    checkpoint = pd.DataFrame(rows)
    health_df = checkpoint[
        ["battery_id", "tester_cycle", "health_percent", "source", "predicted_eol_tester"]
    ]
    capacity_df = checkpoint[
        ["battery_id", "tester_cycle", "capacity_ah", "source", "predicted_eol_tester"]
    ]
    return health_df, capacity_df


def plot_capacity_and_health(
    battery_id: str,
    train_summary: pd.DataFrame,
    tester_summary: pd.DataFrame,
    predicted_eol: float,
    health_df: pd.DataFrame,
    out_path: Path,
) -> None:
    """Tester cycles 1-200 observed; 201-1200 predicted health; training capacity as reference."""
    train = train_summary[train_summary["battery_id"] == battery_id].sort_values("cycle_index")
    tester = tester_summary[tester_summary["battery_id"] == battery_id].sort_values("cycle_index")

    last_ret = float(tester["capacity_retention"].iloc[-1])
    pred_cycles, pred_health, _ = predict_tester_trajectory(last_ret, predicted_eol)

    fig, ax1 = plt.subplots(figsize=(12, 6))

    # Reference: full training lifecycle (physical 1-1200, faint)
    ax1.plot(
        train["cycle_index"],
        train["capacity_ah"],
        color="#94a3b8",
        linewidth=1.0,
        alpha=0.45,
        label="Training capacity (reference, physical 1-1200)",
    )
    # Tester observed input
    ax1.plot(
        tester["cycle_index"],
        tester["capacity_ah"],
        color="#2563eb",
        linewidth=1.8,
        label=f"Tester observed capacity (cyc 1-{TESTER_OBSERVED_MAX})",
    )
    ax1.set_xlabel("Tester cycle")
    ax1.set_ylabel("Capacity (Ah)", color="#2563eb")
    ax1.tick_params(axis="y", labelcolor="#2563eb")
    ax1.set_xlim(0, PREDICTION_HORIZON + 20)
    ax1.grid(True, alpha=0.3)

    ax2 = ax1.twinx()
    ax2.plot(
        tester["cycle_index"],
        tester["capacity_retention"] * 100.0,
        color="#16a34a",
        linewidth=1.5,
        label=f"Tester observed health (cyc 1-{TESTER_OBSERVED_MAX})",
    )
    ax2.plot(
        pred_cycles,
        pred_health,
        color="#dc2626",
        linewidth=2.0,
        linestyle="--",
        label=f"Predicted health (cyc {PREDICTION_START}-{PREDICTION_HORIZON})",
    )
    ax2.axvline(TESTER_OBSERVED_MAX, color="#9333ea", linestyle=":", alpha=0.8, label="Input end (cyc 200)")
    ax2.axvline(PREDICTION_START, color="#f59e0b", linestyle=":", alpha=0.8, label=f"Prediction start (cyc {PREDICTION_START})")
    ax2.axhline(EOL_RETENTION * 100, color="#6b7280", linestyle="--", alpha=0.5, label="EOL 80%")
    ax2.set_ylabel("Battery health (%)", color="#dc2626")
    ax2.tick_params(axis="y", labelcolor="#dc2626")
    ax2.set_ylim(70, 102)

    for _, row in health_df.iterrows():
        c = int(row["tester_cycle"])
        h = row["health_percent"]
        color = "#16a34a" if row["source"] == "observed" else "#dc2626"
        ax2.scatter(c, h, color=color, s=40, zorder=5)
        if c >= PREDICTION_START or c % 100 == 0:
            ax2.annotate(
                f"{h:.1f}%",
                (c, h),
                textcoords="offset points",
                xytext=(0, 8),
                ha="center",
                fontsize=7,
                color=color,
            )

    temp = tester["avg_temperature_c"].iloc[0] if len(tester) else np.nan
    ax1.set_title(
        f"{battery_id}  |  T≈{temp:.0f}°C  |  "
        f"Input: tester cyc 1-200  |  Pred: cyc {PREDICTION_START}-{PREDICTION_HORIZON}  |  "
        f"Pred. EOL={predicted_eol:.0f}"
    )
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax2.legend(lines1 + lines2, labels1 + labels2, loc="upper right", fontsize=7)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def run(
    train_data_dir: Path,
    infer_data_dir: Path,
    model_dir: Path,
    report_dir: Path,
    max_plot_batteries: int = 6,
) -> None:
    model = joblib.load(model_dir / "battery_life_model.joblib")
    feature_cols = joblib.load(model_dir / "feature_columns.joblib")

    train_summary = pd.read_csv(train_data_dir / "cycle_summary.csv")
    tester_summary = pd.read_csv(infer_data_dir / "cycle_summary.csv")
    tester_meta = pd.read_csv(infer_data_dir / "battery_metadata.csv")

    # Tester treats raw_early_200 as cycles 1-200 (cycle_offset=0 for features)
    physical_offset = int(tester_meta.get("cycle_offset", pd.Series([500])).iloc[0])

    features = extract_features(
        tester_summary, tester_meta, max_cycle=TESTER_OBSERVED_MAX, cycle_offset=0
    )
    X = features.drop(columns=[c for c in DROP_COLS if c in features.columns])
    X = X.reindex(columns=feature_cols, fill_value=0)

    raw_eol = model.predict(X)
    # Map physical-life prediction to tester horizon (cap at 1200)
    features["predicted_eol_physical"] = raw_eol
    features["predicted_eol_tester"] = np.clip(raw_eol, PREDICTION_START + 1, PREDICTION_HORIZON)
    features["predicted_rul_from_200"] = features["predicted_eol_tester"] - TESTER_OBSERVED_MAX
    features["predicted_rul_from_201"] = features["predicted_eol_tester"] - PREDICTION_START
    features["physical_performance_cycle_start"] = physical_offset + 1
    features["physical_performance_cycle_end"] = physical_offset + TESTER_OBSERVED_MAX

    report_dir.mkdir(parents=True, exist_ok=True)
    fig_dir = report_dir / "figures"
    fig_dir.mkdir(exist_ok=True)

    health_parts = []
    capacity_parts = []
    battery_ids = features["battery_id"].tolist()
    nominal_map = features.set_index("battery_id")["nominal_capacity_ah"].to_dict()

    for i, bid in enumerate(battery_ids):
        pred_eol = float(features.loc[features["battery_id"] == bid, "predicted_eol_tester"].iloc[0])
        obs = tester_summary[tester_summary["battery_id"] == bid]
        hdf, cdf = build_checkpoint_tables(
            bid, obs, pred_eol, float(nominal_map[bid])
        )
        health_parts.append(hdf)
        capacity_parts.append(cdf)
        if i < max_plot_batteries:
            plot_capacity_and_health(
                bid,
                train_summary,
                tester_summary,
                pred_eol,
                hdf,
                fig_dir / f"{bid}_capacity_health.png",
            )

    health_all = pd.concat(health_parts, ignore_index=True)
    capacity_all = pd.concat(capacity_parts, ignore_index=True)
    health_all.to_csv(report_dir / "health_every_50_cycles.csv", index=False)
    capacity_all.to_csv(report_dir / "capacity_every_50_cycles.csv", index=False)
    features.to_csv(report_dir / "predictions.csv", index=False)

    pred_only = health_all[health_all["source"] == "predicted"]
    pivot = pred_only.groupby("tester_cycle")["health_percent"].agg(["mean", "std"])
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.fill_between(
        pivot.index,
        pivot["mean"] - pivot["std"],
        pivot["mean"] + pivot["std"],
        alpha=0.2,
        color="#dc2626",
    )
    ax.plot(pivot.index, pivot["mean"], "o-", color="#dc2626", label="Mean predicted health")
    ax.axhline(80, color="gray", linestyle="--", label="EOL 80%")
    ax.axvline(TESTER_OBSERVED_MAX, color="#9333ea", linestyle=":", label="Input end (cyc 200)")
    ax.axvline(PREDICTION_START, color="#f59e0b", linestyle=":", label=f"Prediction start (cyc {PREDICTION_START})")
    ax.set_xlabel("Tester cycle")
    ax.set_ylabel("Health (%)")
    ax.set_title(f"Fleet average predicted health (cycles {PREDICTION_START}-{PREDICTION_HORIZON})")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(fig_dir / "fleet_health_summary.png", dpi=120)
    plt.close(fig)

    print("Tester inference axis:")
    print(f"  Input cycles     : 1-{TESTER_OBSERVED_MAX} (performance ~ physical {physical_offset+1}-{physical_offset+TESTER_OBSERVED_MAX})")
    print(f"  Prediction range : {PREDICTION_START}-{PREDICTION_HORIZON}")
    print(f"Predictions -> {report_dir / 'predictions.csv'}")
    print(f"Health table   -> {report_dir / 'health_every_50_cycles.csv'}")
    print(f"Capacity table -> {report_dir / 'capacity_every_50_cycles.csv'}")
    print(f"Figures -> {fig_dir}")
    print("\nSample predictions:")
    cols = [
        "battery_id",
        "temperature_c",
        "predicted_eol_tester",
        "predicted_rul_from_201",
        "predicted_eol_physical",
        "actual_eol_cycle",
    ]
    print(features[cols].head(8).to_string(index=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train-data", type=Path, default=Path("data/raw"))
    parser.add_argument("--infer-data", type=Path, default=Path("data/raw_early_200"))
    parser.add_argument("--model-dir", type=Path, default=Path("models"))
    parser.add_argument("--report-dir", type=Path, default=Path("reports"))
    parser.add_argument("--max-plots", type=int, default=6)
    args = parser.parse_args()
    run(args.train_data, args.infer_data, args.model_dir, args.report_dir, args.max_plots)


if __name__ == "__main__":
    main()
