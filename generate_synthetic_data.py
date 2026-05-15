"""
Generate synthetic battery charge/discharge cycle data per integrated spec (Section 16.5).

Outputs:
  data/raw/raw_cycle_data.csv   - step-level records (charge / discharge / rest)
  data/raw/cycle_summary.csv    - one row per battery per cycle (ML-friendly)
  data/raw/battery_metadata.csv - battery-level metadata and ground-truth EOL
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# Spec charge profile (4-step) and discharge profile (2-step)
CHARGE_PROFILE = [
    {"step_no": 1, "mode": "CC", "rate_c": 2.0, "until_voltage_v": 4.35},
    {"step_no": 2, "mode": "CC", "rate_c": 1.6, "until_voltage_v": 4.35},
    {"step_no": 3, "mode": "CC_CV", "rate_c": 1.3, "cv_voltage_v": 4.45, "until_current_c": 1.0},
    {"step_no": 4, "mode": "CC_CV", "rate_c": 1.0, "cv_voltage_v": 4.55, "until_current_c": 0.1},
]
DISCHARGE_PROFILE = [
    {"step_no": 1, "mode": "CC", "rate_c": 1.0, "until_voltage_v": 3.3},
    {"step_no": 2, "mode": "CC", "rate_c": 0.5, "until_voltage_v": 3.0},
]

RAW_COLUMNS = [
    "battery_id",
    "cycle_index",
    "timestamp",
    "stage_name",
    "voltage_v",
    "current_a",
    "temperature_c",
    "rest_voltage_v",
    "charge_dcir_mohm",
    "discharge_dcir_mohm",
    "soc30_resistance_mohm",
    "soc50_resistance_mohm",
    "soc70_resistance_mohm",
]


@dataclass
class BatteryConfig:
    battery_id: str
    temperature_c: float
    nominal_capacity_ah: float
    fade_per_cycle: float
    dcir_growth_per_cycle: float
    rest_voltage_drift: float
    actual_eol_cycle: int
    chemistry: str = "NMC"
    seed: int = 0
    cycle_offset: int = 0


@dataclass
class CycleState:
    """Per-cycle degradation state (from formulas or reference CSV)."""

    cycle_index: int
    capacity_ah: float
    capacity_retention: float
    rest_voltage_v: float
    charge_dcir_mohm: float
    discharge_dcir_mohm: float
    soc30_resistance_mohm: float
    soc50_resistance_mohm: float
    soc70_resistance_mohm: float


def _temp_degradation_params(temp_c: float, rng: np.random.Generator) -> tuple[float, float, int]:
    """Return (fade_per_cycle, dcir_growth, typical_eol) by ambient temperature."""
    if temp_c >= 40:
        fade = rng.normal(1.65e-4, 2.0e-5)
        dcir = rng.normal(4.5e-4, 5.0e-5)
        eol = int(rng.normal(850, 80))
    elif temp_c >= 20:
        fade = rng.normal(1.05e-4, 1.2e-5)
        dcir = rng.normal(2.8e-4, 3.0e-5)
        eol = int(rng.normal(1350, 120))
    else:
        fade = rng.normal(8.5e-5, 1.0e-5)
        dcir = rng.normal(2.2e-4, 2.5e-5)
        eol = int(rng.normal(1650, 150))
    return max(fade, 5e-5), max(dcir, 1e-4), max(eol, 600)


def capacity_retention(cycle: int, fade_per_cycle: float, rng: np.random.Generator) -> float:
    base = 1.0 - fade_per_cycle * cycle
    noise = rng.normal(0.0, 0.0015)
    return float(np.clip(base + noise, 0.72, 1.02))


def cycle_resistances(
    cycle: int,
    retention: float,
    cfg: BatteryConfig,
    rng: np.random.Generator,
) -> dict[str, float]:
    base_dcir = 12.0 + cfg.dcir_growth_per_cycle * cycle * 1000
    fade_factor = 1.0 + (1.0 - retention) * 2.5
    charge_dcir = base_dcir * fade_factor + rng.normal(0, 0.15)
    discharge_dcir = charge_dcir * rng.uniform(1.05, 1.12)
    return {
        "charge_dcir_mohm": float(max(charge_dcir, 8.0)),
        "discharge_dcir_mohm": float(max(discharge_dcir, 9.0)),
        "soc30_resistance_mohm": float(max(charge_dcir * 1.08, 8.5)),
        "soc50_resistance_mohm": float(max(charge_dcir * 1.00, 8.0)),
        "soc70_resistance_mohm": float(max(charge_dcir * 0.95, 7.5)),
    }


def simulate_charge_step(
    step: dict,
    capacity_ah: float,
    retention: float,
    n_points: int,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray]:
    """Return voltage and current arrays for one charge step."""
    eff_cap = capacity_ah * retention
    if step["mode"] == "CC":
        i = step["rate_c"] * eff_cap
        v_start = 3.5 + (step["step_no"] - 1) * 0.15
        v_end = step["until_voltage_v"]
        voltage = np.linspace(v_start, v_end, n_points)
        current = np.full(n_points, i) + rng.normal(0, 0.02, n_points)
    else:
        v_cv = step["cv_voltage_v"]
        i_start = step["rate_c"] * eff_cap
        i_end = step["until_current_c"] * eff_cap
        voltage = np.full(n_points, v_cv) + rng.normal(0, 0.005, n_points)
        current = np.linspace(i_start, i_end, n_points) + rng.normal(0, 0.02, n_points)
    return voltage, np.clip(current, 0.05, None)


def simulate_discharge_step(
    step: dict,
    capacity_ah: float,
    retention: float,
    n_points: int,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray]:
    eff_cap = capacity_ah * retention
    i = -step["rate_c"] * eff_cap
    v_start = 3.8 - (step["step_no"] - 1) * 0.25
    v_end = step["until_voltage_v"]
    voltage = np.linspace(v_start, v_end, n_points)
    current = np.full(n_points, i) + rng.normal(0, 0.02, n_points)
    return voltage, current


def simulate_rest(
    rest_voltage: float,
    n_points: int,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray]:
    voltage = np.full(n_points, rest_voltage) + rng.normal(0, 0.003, n_points)
    current = rng.normal(0, 0.002, n_points)
    return voltage, current


def _cycle_state_from_formulas(cfg: BatteryConfig, cycle: int, rng: np.random.Generator) -> CycleState:
    physical = cycle + cfg.cycle_offset
    retention = capacity_retention(physical, cfg.fade_per_cycle, rng)
    resist = cycle_resistances(physical, retention, cfg, rng)
    rest_v = 4.18 - cfg.rest_voltage_drift * physical + rng.normal(0, 0.004)
    return CycleState(
        cycle_index=cycle,
        capacity_ah=cfg.nominal_capacity_ah * retention,
        capacity_retention=retention,
        rest_voltage_v=rest_v,
        **resist,
    )


def _cycle_state_from_row(row: pd.Series) -> CycleState:
    return CycleState(
        cycle_index=int(row["cycle_index"]),
        capacity_ah=float(row["capacity_ah"]),
        capacity_retention=float(row["capacity_retention"]),
        rest_voltage_v=float(row["rest_voltage_v"]),
        charge_dcir_mohm=float(row["charge_dcir_mohm"]),
        discharge_dcir_mohm=float(row["discharge_dcir_mohm"]),
        soc30_resistance_mohm=float(row["soc30_resistance_mohm"]),
        soc50_resistance_mohm=float(row["soc50_resistance_mohm"]),
        soc70_resistance_mohm=float(row["soc70_resistance_mohm"]),
    )


def generate_battery_cycles(
    cfg: BatteryConfig,
    max_cycles: int,
    points_per_charge_step: int = 12,
    points_per_discharge_step: int = 15,
    points_rest: int = 8,
    reference_cycles: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(cfg.seed)
    raw_rows: list[dict] = []
    summary_rows: list[dict] = []

    ref_by_cycle: dict[int, pd.Series] | None = None
    if reference_cycles is not None:
        ref = reference_cycles.sort_values("cycle_index")
        ref_by_cycle = {int(row["cycle_index"]): row for _, row in ref.iterrows()}

    t0 = datetime(2024, 6, 1, 8, 0, 0)
    elapsed = timedelta(0)

    for cycle in range(1, max_cycles + 1):
        if ref_by_cycle is not None:
            if cycle not in ref_by_cycle:
                raise ValueError(f"Missing reference row for cycle {cycle} ({cfg.battery_id})")
            state = _cycle_state_from_row(ref_by_cycle[cycle])
        else:
            state = _cycle_state_from_formulas(cfg, cycle, rng)

        retention = state.capacity_retention
        cap_ah = state.capacity_ah
        rest_v = state.rest_voltage_v
        resist = {
            "charge_dcir_mohm": state.charge_dcir_mohm,
            "discharge_dcir_mohm": state.discharge_dcir_mohm,
            "soc30_resistance_mohm": state.soc30_resistance_mohm,
            "soc50_resistance_mohm": state.soc50_resistance_mohm,
            "soc70_resistance_mohm": state.soc70_resistance_mohm,
        }

        stage_voltages: list[float] = []
        stage_currents: list[float] = []
        stage_temps: list[float] = []

        # --- Charge (4 steps) ---
        for step in CHARGE_PROFILE:
            v, i = simulate_charge_step(step, cfg.nominal_capacity_ah, retention, points_per_charge_step, rng)
            op_temp = cfg.temperature_c + 2.5 + 0.3 * step["step_no"] + rng.normal(0, 0.4, len(v))
            for j in range(len(v)):
                elapsed += timedelta(seconds=int(rng.integers(25, 45)))
                raw_rows.append(
                    {
                        "battery_id": cfg.battery_id,
                        "cycle_index": cycle,
                        "timestamp": (t0 + elapsed).isoformat(),
                        "stage_name": "charge",
                        "voltage_v": round(float(v[j]), 4),
                        "current_a": round(float(i[j]), 4),
                        "temperature_c": round(float(op_temp[j]), 2),
                        "rest_voltage_v": round(rest_v, 4),
                        **{k: round(vv, 3) for k, vv in resist.items()},
                    }
                )
                stage_voltages.append(v[j])
                stage_currents.append(i[j])
                stage_temps.append(op_temp[j])

        # --- Discharge (2 steps) ---
        for step in DISCHARGE_PROFILE:
            v, i = simulate_discharge_step(step, cfg.nominal_capacity_ah, retention, points_per_discharge_step, rng)
            op_temp = cfg.temperature_c + 3.0 + rng.normal(0, 0.5, len(v))
            for j in range(len(v)):
                elapsed += timedelta(seconds=int(rng.integers(30, 55)))
                raw_rows.append(
                    {
                        "battery_id": cfg.battery_id,
                        "cycle_index": cycle,
                        "timestamp": (t0 + elapsed).isoformat(),
                        "stage_name": "discharge",
                        "voltage_v": round(float(v[j]), 4),
                        "current_a": round(float(i[j]), 4),
                        "temperature_c": round(float(op_temp[j]), 2),
                        "rest_voltage_v": round(rest_v, 4),
                        **{k: round(vv, 3) for k, vv in resist.items()},
                    }
                )
                stage_voltages.append(v[j])
                stage_currents.append(i[j])
                stage_temps.append(op_temp[j])

        # --- Rest ---
        v, i = simulate_rest(rest_v, points_rest, rng)
        rest_temp = cfg.temperature_c + 0.5 + rng.normal(0, 0.2, len(v))
        for j in range(len(v)):
            elapsed += timedelta(minutes=int(rng.integers(8, 15)))
            raw_rows.append(
                {
                    "battery_id": cfg.battery_id,
                    "cycle_index": cycle,
                    "timestamp": (t0 + elapsed).isoformat(),
                    "stage_name": "rest",
                    "voltage_v": round(float(v[j]), 4),
                    "current_a": round(float(i[j]), 4),
                    "temperature_c": round(float(rest_temp[j]), 2),
                    "rest_voltage_v": round(float(v[j]), 4),
                    **{k: round(vv, 3) for k, vv in resist.items()},
                }
            )
            stage_voltages.append(v[j])
            stage_currents.append(abs(i[j]))
            stage_temps.append(rest_temp[j])

        summary_rows.append(
            {
                "battery_id": cfg.battery_id,
                "cycle_index": cycle,
                "capacity_ah": round(cap_ah, 4),
                "capacity_retention": round(retention, 5),
                "avg_voltage_v": round(float(np.mean(stage_voltages)), 4),
                "avg_current_a": round(float(np.mean(np.abs(stage_currents))), 4),
                "avg_temperature_c": round(float(np.mean(stage_temps)), 2),
                "rest_voltage_v": round(rest_v, 4),
                **resist,
            }
        )

    return pd.DataFrame(raw_rows, columns=RAW_COLUMNS), pd.DataFrame(summary_rows)


def build_battery_configs(
    num_batteries: int,
    temperatures: list[float],
    seed: int,
) -> list[BatteryConfig]:
    rng = np.random.default_rng(seed)
    configs: list[BatteryConfig] = []
    per_temp = max(1, num_batteries // len(temperatures))

    idx = 0
    for temp in temperatures:
        for _ in range(per_temp):
            if idx >= num_batteries:
                break
            fade, dcir_g, eol = _temp_degradation_params(temp, rng)
            configs.append(
                BatteryConfig(
                    battery_id=f"BAT_{idx + 1:03d}",
                    temperature_c=temp,
                    nominal_capacity_ah=round(float(rng.normal(3.2, 0.05)), 3),
                    fade_per_cycle=fade,
                    dcir_growth_per_cycle=dcir_g,
                    rest_voltage_drift=float(rng.uniform(2.5e-5, 4.5e-5)),
                    actual_eol_cycle=eol,
                    chemistry=rng.choice(["NMC", "NMC", "LCO"]),
                    seed=seed + idx,
                )
            )
            idx += 1

    while idx < num_batteries:
        temp = float(rng.choice(temperatures))
        fade, dcir_g, eol = _temp_degradation_params(temp, rng)
        configs.append(
            BatteryConfig(
                battery_id=f"BAT_{idx + 1:03d}",
                temperature_c=temp,
                nominal_capacity_ah=round(float(rng.normal(3.2, 0.05)), 3),
                fade_per_cycle=fade,
                dcir_growth_per_cycle=dcir_g,
                rest_voltage_drift=float(rng.uniform(2.5e-5, 4.5e-5)),
                actual_eol_cycle=eol,
                chemistry=rng.choice(["NMC", "NMC", "LCO"]),
                seed=seed + idx,
            )
        )
        idx += 1

    return configs


def generate_dataset(
    output_dir: Path,
    num_batteries: int = 30,
    max_cycles: int = 1200,
    seed: int = 42,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    temperatures = [15.0, 23.0, 45.0]
    configs = build_battery_configs(num_batteries, temperatures, seed)

    raw_parts: list[pd.DataFrame] = []
    summary_parts: list[pd.DataFrame] = []
    meta_rows: list[dict] = []

    for cfg in configs:
        raw_df, summary_df = generate_battery_cycles(cfg, max_cycles)
        raw_parts.append(raw_df)
        summary_parts.append(summary_df)
        meta_rows.append(
            {
                "battery_id": cfg.battery_id,
                "chemistry": cfg.chemistry,
                "temperature_c": cfg.temperature_c,
                "nominal_capacity_ah": cfg.nominal_capacity_ah,
                "actual_eol_cycle": cfg.actual_eol_cycle,
                "fade_per_cycle": cfg.fade_per_cycle,
                "charge_profile_json": json.dumps(CHARGE_PROFILE),
                "discharge_profile_json": json.dumps(DISCHARGE_PROFILE),
            }
        )

    raw_all = pd.concat(raw_parts, ignore_index=True)
    summary_all = pd.concat(summary_parts, ignore_index=True)
    meta_df = pd.DataFrame(meta_rows)

    raw_path = output_dir / "raw_cycle_data.csv"
    summary_path = output_dir / "cycle_summary.csv"
    meta_path = output_dir / "battery_metadata.csv"

    raw_all.to_csv(raw_path, index=False)
    summary_all.to_csv(summary_path, index=False)
    meta_df.to_csv(meta_path, index=False)

    print(f"Generated {num_batteries} batteries x {max_cycles} cycles")
    print(f"  raw_cycle_data.csv    : {len(raw_all):,} rows -> {raw_path}")
    print(f"  cycle_summary.csv     : {len(summary_all):,} rows -> {summary_path}")
    print(f"  battery_metadata.csv  : {len(meta_df)} rows -> {meta_path}")
    print(f"Temperature distribution:\n{meta_df['temperature_c'].value_counts().sort_index()}")


def load_reference_early_cycles(
    summary_path: Path,
    cycle_offset: int,
    num_cycles: int,
) -> dict[str, pd.DataFrame]:
    """
    Extract physical cycles (offset+1 .. offset+num_cycles) and reindex to 1..num_cycles.
    Example: offset=500, num_cycles=200 -> uses original cycles 501-700 as new cycles 1-200.
    """
    summary = pd.read_csv(summary_path)
    start = cycle_offset + 1
    end = cycle_offset + num_cycles
    window = summary[(summary["cycle_index"] >= start) & (summary["cycle_index"] <= end)].copy()
    if window.empty:
        raise FileNotFoundError(f"No cycles {start}-{end} in {summary_path}")

    refs: dict[str, pd.DataFrame] = {}
    for battery_id, group in window.groupby("battery_id"):
        g = group.sort_values("cycle_index").copy()
        g["cycle_index"] = np.arange(1, len(g) + 1)
        g["reference_physical_cycle"] = np.arange(start, start + len(g))
        refs[battery_id] = g

    expected = summary["battery_id"].nunique()
    if len(refs) != expected:
        missing = set(summary["battery_id"].unique()) - set(refs.keys())
        raise ValueError(f"Reference window incomplete for batteries: {sorted(missing)[:5]}...")

    return refs


def generate_early_life_from_full_data(
    full_data_dir: Path,
    output_dir: Path,
    cycle_offset: int = 500,
    num_cycles: int = 200,
) -> None:
    """
    ML input set: 200 cycles where cycle 1 equals post-500-cycle degradation from full dataset.
    """
    summary_path = full_data_dir / "cycle_summary.csv"
    meta_path = full_data_dir / "battery_metadata.csv"
    if not summary_path.exists():
        raise FileNotFoundError(f"Run full 1200-cycle generation first: {summary_path}")

    refs = load_reference_early_cycles(summary_path, cycle_offset, num_cycles)
    meta_full = pd.read_csv(meta_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_parts: list[pd.DataFrame] = []
    summary_parts: list[pd.DataFrame] = []
    meta_rows: list[dict] = []

    for _, row in meta_full.iterrows():
        bid = row["battery_id"]
        ref_df = refs[bid]
        cfg = BatteryConfig(
            battery_id=bid,
            temperature_c=float(row["temperature_c"]),
            nominal_capacity_ah=float(row["nominal_capacity_ah"]),
            fade_per_cycle=float(row["fade_per_cycle"]),
            dcir_growth_per_cycle=0.0,
            rest_voltage_drift=0.0,
            actual_eol_cycle=int(row["actual_eol_cycle"]),
            chemistry=str(row["chemistry"]),
            seed=0,
            cycle_offset=cycle_offset,
        )
        raw_df, summary_df = generate_battery_cycles(
            cfg, num_cycles, reference_cycles=ref_df
        )
        raw_parts.append(raw_df)
        summary_parts.append(summary_df)
        meta_rows.append(
            {
                "battery_id": bid,
                "chemistry": row["chemistry"],
                "temperature_c": row["temperature_c"],
                "nominal_capacity_ah": row["nominal_capacity_ah"],
                "actual_eol_cycle": int(row["actual_eol_cycle"]),
                "fade_per_cycle": row["fade_per_cycle"],
                "cycle_offset": cycle_offset,
                "reference_physical_cycle_start": cycle_offset + 1,
                "reference_physical_cycle_end": cycle_offset + num_cycles,
                "remaining_eol_cycles": max(int(row["actual_eol_cycle"]) - cycle_offset, 0),
                "charge_profile_json": row.get("charge_profile_json", json.dumps(CHARGE_PROFILE)),
                "discharge_profile_json": row.get(
                    "discharge_profile_json", json.dumps(DISCHARGE_PROFILE)
                ),
            }
        )

    raw_all = pd.concat(raw_parts, ignore_index=True)
    summary_all = pd.concat(summary_parts, ignore_index=True)
    meta_df = pd.DataFrame(meta_rows)

    raw_all.to_csv(output_dir / "raw_cycle_data.csv", index=False)
    summary_all.to_csv(output_dir / "cycle_summary.csv", index=False)
    meta_df.to_csv(output_dir / "battery_metadata.csv", index=False)

    print(f"Early-life ML set: {len(meta_df)} batteries x {num_cycles} cycles")
    print(f"  Physical reference: cycles {cycle_offset + 1}-{cycle_offset + num_cycles}")
    print(f"  raw_cycle_data.csv   : {len(raw_all):,} rows -> {output_dir / 'raw_cycle_data.csv'}")
    print(f"  cycle_summary.csv    : {len(summary_all):,} rows")
    print(f"  battery_metadata.csv : {len(meta_df)} rows")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic battery cycle CSV data")
    parser.add_argument("--output-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--num-batteries", type=int, default=30)
    parser.add_argument("--max-cycles", type=int, default=1200)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--mode",
        choices=["full", "early-from-full"],
        default="full",
        help="full: new 1200-cycle set; early-from-full: 200 cycles anchored at cycle 500+",
    )
    parser.add_argument(
        "--full-data-dir",
        type=Path,
        default=Path("data/raw"),
        help="Source directory for early-from-full mode",
    )
    parser.add_argument(
        "--cycle-offset",
        type=int,
        default=500,
        help="In early-from-full: physical cycle before window (500 -> use cycles 501-700)",
    )
    args = parser.parse_args()

    if args.mode == "early-from-full":
        generate_early_life_from_full_data(
            full_data_dir=args.full_data_dir,
            output_dir=args.output_dir,
            cycle_offset=args.cycle_offset,
            num_cycles=args.max_cycles,
        )
    else:
        generate_dataset(
            output_dir=args.output_dir,
            num_batteries=args.num_batteries,
            max_cycles=args.max_cycles,
            seed=args.seed,
        )


if __name__ == "__main__":
    main()
