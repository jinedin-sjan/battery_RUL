# Battery RUL Folder Analysis

## Overview
This folder contains a synthetic battery remaining useful life (RUL) pipeline. It generates synthetic charge/discharge cycle data, extracts predictive features from early-life cycles, trains an XGBoost regression model to predict end-of-life (EOL), and projects battery health on a tester cycle axis.

## Repository Structure

- `data/`
  - `raw/` – full synthetic dataset with 1200 cycles per battery
  - `raw_early_200/` – early-life dataset representing tester cycles 1-200
- `models/`
  - `battery_life_model.joblib` – trained XGBoost model
  - `feature_columns.joblib` – ordered feature columns used for inference
  - `model_config.joblib` – model metadata
- `reports/`
  - prediction tables and figure outputs
- `.rul_env/` – local virtual environment files (not part of the model logic)
- `feature_engineering.py` – feature extraction from cycle summaries
- `generate_synthetic_data.py` – synthetic battery life dataset generator
- `train_model.py` – training pipeline for total-life prediction
- `predict_health.py` – inference and health projection on tester axis
- `plot_cycle_curve_comparison.py` – curve visualization for measured vs predicted
- `run_pipeline.py` – end-to-end orchestration
- `requirements.txt` – Python dependencies
- `battery_life_prediction_integrated_spec.md` – integrated spec reference

## Core Workflow

1. **Data generation**
   - `generate_synthetic_data.py --mode full`
   - produces `data/raw/raw_cycle_data.csv`, `data/raw/cycle_summary.csv`, `data/raw/battery_metadata.csv`
   - can also generate early-life test input using `--mode early-from-full`

2. **Feature extraction**
   - `feature_engineering.py` computes summary statistics and slopes over windowed cycle ranges
   - used by both training and inference

3. **Model training**
   - `train_model.py` uses XGBoost regression on extracted features
   - target is `actual_eol_cycle` from `battery_metadata.csv`
   - performs GroupKFold cross-validation by `battery_id`
   - saves model and feature order for later inference

4. **Inference and projection**
   - `predict_health.py` loads the trained model and applies it to early-life tester data
   - predicts EOL and maps that to a tester cycle timeline
   - generates health/capacity checkpoints and visualization figures

5. **Visualization**
   - `plot_cycle_curve_comparison.py` compares real charge/discharge curves and a predicted trajectory
   - useful for validating predicted retention and EOL behavior

6. **End-to-end pipeline**
   - `run_pipeline.py` runs training on `data/raw` and inference on `data/raw_early_200`

## File-by-file Function Summary

### `generate_synthetic_data.py`
- `BatteryConfig`: config for each synthetic battery, including temperature, capacity, degradation rate, and actual EOL.
- `CycleState`: stores per-cycle degradation state used for summary records.
- `_temp_degradation_params()`: sets fade, resistance growth, and EOL based on ambient temperature.
- `capacity_retention()`: computes capacity retention with noise.
- `cycle_resistances()`: derives DC internal resistance and SOC-based resistances from retention.
- `simulate_charge_step()`: simulates voltage/current profile for a charge step, with CC and CC/CV modes.
- `simulate_discharge_step()`: simulates discharge voltage/current.
- `simulate_rest()`: simulates rest segment voltage and near-zero current.
- `_cycle_state_from_formulas()`: generates cycle-level degradation metrics from formulas.
- `_cycle_state_from_row()`: loads a reference row for early-life simulation from real data.
- `generate_battery_cycles()`: synthesizes raw step-level and cycle-summary data for a battery.
- `build_battery_configs()`: creates many battery configurations spanning temperature groups.
- `generate_dataset()`: creates a full synthetic dataset and writes data CSVs.
- `load_reference_early_cycles()`: extracts fixed physical cycles for early-life dataset generation.
- `generate_early_life_from_full_data()`: builds `raw_early_200` from a subset of physical cycles.

### `feature_engineering.py`
- `extract_features()`: generates per-battery features from cycle summary data.
- Uses cycle windows: `1-20`, `21-50`, `51-100`, `101-200`.
- Extracts means, end values, maxima, and linear slopes for capacity, DCIR, and retention.
- Produces overall slopes and deltas for the full window.
- Joins engineered features to battery metadata for later modeling.

### `train_model.py`
- `train()`: trains the XGBoost model for `actual_eol_cycle`.
- Reads `data/raw/cycle_summary.csv` and metadata.
- Calls `extract_features(..., max_cycle=200)` to use first 200 physical cycles.
- Drops metadata columns excluded from modeling.
- Uses `GroupKFold` with 5 splits by battery.
- Saves trained model and features to `models/`.
- Exports `features_train.csv` for inspection.

### `predict_health.py`
- `predict_tester_trajectory()`: projects capacity retention linearly from observed end retention to 80% EOL.
- `build_checkpoint_tables()`: creates health/capacity checkpoints at every 50 tester cycles.
- `plot_capacity_and_health()`: builds a figure showing observed capacity, predicted health, and reference training capacity.
- `run()`: loads model and feature metadata, applies the model to `raw_early_200`, and writes report outputs.
- Handles mapping from physical lifecycle prediction to tester-cycle horizon.
- Produces predicted RUL and capacity trends for each battery.

### `plot_cycle_curve_comparison.py`
- `load_cycle_from_raw()`: reads a single cycle from raw step-level data.
- `_segment_capacity_ah()`: integrates current over time to compute cumulative Ah.
- `raw_to_qvi()`: converts stage data into capacity-voltage-current curves.
- `simulate_predicted_cycle()`: uses the same charge/discharge profile generator to build predicted curves.
- `retention_at_tester_cycle()`: returns observed retention for <=200 or interpolates predictions for later cycles.
- `plot_comparison()`: draws voltage and current curves for training, measured, and predicted traces.
- `main()`: script entrypoint for producing a comparison figure.

### `run_pipeline.py`
- Simple orchestrator that calls `train()` then `run()`.
- Default behavior: train on `data/raw`, infer on `data/raw_early_200`, write reports to `reports/`.

## Data Artefacts and Formats

- `raw_cycle_data.csv`: step-level records by `battery_id`, `cycle_index`, `stage_name`, `voltage_v`, `current_a`, `temperature_c`, resistances, and rest voltage.
- `cycle_summary.csv`: one row per battery per cycle with aggregated statistics.
- `battery_metadata.csv`: battery-level metadata and true EOL cycle.
- `reports/predictions.csv`: inference results for early-life tester data.
- `models/battery_life_model.joblib`: trained regression model object.

## Core Techniques

- Synthetic battery modeling using physics-inspired profile simulation.
- Feature engineering from cycle window summaries and linear trends.
- Group-aware cross-validation to avoid battery-leakage.
- XGBoost regression for end-of-life prediction.
- Linear mapping from observed tester retention to longer-term health trajectory.
- Visualization of voltage/current profile predictions versus observed data.

## How to Use

1. Generate data:
   - `python generate_synthetic_data.py --mode full`
   - `python generate_synthetic_data.py --mode early-from-full --full-data-dir data/raw`
2. Train model:
   - `python train_model.py --data-dir data/raw --model-dir models`
3. Run inference and reports:
   - `python run_pipeline.py`
4. Optional visualization:
   - `python plot_cycle_curve_comparison.py --battery-id BAT_001 --cycles 100 1200`

## Helpful Notes

- The pipeline treats `raw_early_200` as a synthetic tester dataset with delayed physical aging.
- The model predicts absolute physical EOL, then clamps that prediction into a tester-cycle horizon.
- `feature_engineering.py` is central: any change in windowing or slope extraction affects both training and inference.
- `predict_health.py` keeps early observed cycles intact and only extrapolates from cycle 201 onward.
- The dataset generator can produce both full-cycle and early-life versions, so the workflow supports both training and deployment-style testing.
