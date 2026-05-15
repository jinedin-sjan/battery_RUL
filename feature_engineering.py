"""Feature extraction from cycle_summary.csv (spec window: 1-200 cycles)."""

from __future__ import annotations

import numpy as np
import pandas as pd

WINDOWS = [(1, 20), (21, 50), (51, 100), (101, 200)]


def extract_features(
    summary: pd.DataFrame,
    metadata: pd.DataFrame,
    max_cycle: int = 200,
    cycle_offset: int = 0,
) -> pd.DataFrame:
    """
    Build per-battery features from the first `max_cycle` cycles.

    cycle_offset: physical cycles already completed before cycle_index 1
                  (0 for training on fresh cells, 500 for raw_early_200).
    """
    early = summary[summary["cycle_index"] <= max_cycle].copy()
    features: list[dict] = []

    for battery_id, group in early.groupby("battery_id"):
        group = group.sort_values("cycle_index")
        if len(group) < max_cycle * 0.5:
            continue

        cap = group["capacity_ah"]
        dcir = group["charge_dcir_mohm"]
        temp = group["avg_temperature_c"]
        rest_v = group["rest_voltage_v"]
        retention = group["capacity_retention"]

        feat: dict = {
            "battery_id": battery_id,
            "cycle_offset": cycle_offset,
            "cap_mean": cap.mean(),
            "cap_std": cap.std(),
            "retention_mean": retention.mean(),
            "retention_end": retention.iloc[-1],
            "dcir_mean": dcir.mean(),
            "dcir_max": dcir.max(),
            "dcir_end": dcir.iloc[-1],
            "temp_mean": temp.mean(),
            "rest_v_mean": rest_v.mean(),
        }

        for start, end in WINDOWS:
            win = group[(group["cycle_index"] >= start) & (group["cycle_index"] <= end)]
            if win.empty:
                continue
            feat[f"cap_win_{start}_{end}_mean"] = win["capacity_ah"].mean()
            feat[f"dcir_win_{start}_{end}_mean"] = win["charge_dcir_mohm"].mean()
            if len(win) > 1:
                feat[f"cap_slope_{start}_{end}"] = np.polyfit(
                    win["cycle_index"], win["capacity_ah"], 1
                )[0]
                feat[f"dcir_slope_{start}_{end}"] = np.polyfit(
                    win["cycle_index"], win["charge_dcir_mohm"], 1
                )[0]
                feat[f"ret_slope_{start}_{end}"] = np.polyfit(
                    win["cycle_index"], win["capacity_retention"], 1
                )[0]

        if len(group) > 1:
            feat["overall_cap_slope"] = np.polyfit(group["cycle_index"], cap, 1)[0]
            feat["overall_dcir_slope"] = np.polyfit(group["cycle_index"], dcir, 1)[0]
            feat["overall_retention_slope"] = np.polyfit(group["cycle_index"], retention, 1)[0]
            feat["cap_delta_200_1"] = cap.iloc[-1] - cap.iloc[0]
            feat["dcir_delta_200_1"] = dcir.iloc[-1] - dcir.iloc[0]

        features.append(feat)

    feat_df = pd.DataFrame(features)
    meta_cols = [
        c
        for c in metadata.columns
        if c
        not in (
            "charge_profile_json",
            "discharge_profile_json",
            "reference_physical_cycle_start",
            "reference_physical_cycle_end",
        )
    ]
    return feat_df.merge(metadata[meta_cols], on="battery_id", how="left")
