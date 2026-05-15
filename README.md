# Battery RUL Synthetic Pipeline

This project generates synthetic battery cycle data, trains an EOL prediction model, and projects battery health on a tester cycle timeline.

## Quick Start

1. Activate your Python environment.
2. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```

3. Generate full synthetic data:
   ```powershell
   python generate_synthetic_data.py --mode full --output-dir data/raw --num-batteries 30 --max-cycles 1200
   ```

4. Generate early-life tester input from full data:
   ```powershell
   python generate_synthetic_data.py --mode early-from-full --full-data-dir data/raw --output-dir data/raw_early_200 --cycle-offset 500 --max-cycles 200
   ```

5. Train the model:
   ```powershell
   python train_model.py --data-dir data/raw --model-dir models
   ```

6. Run the end-to-end pipeline (train + predict):
   ```powershell
   python run_pipeline.py
   ```

7. Generate a curve comparison figure:
   ```powershell
   python plot_cycle_curve_comparison.py --battery-id BAT_001 --cycles 100 1200
   ```

## Project Files

- `generate_synthetic_data.py` - dataset generator for full-life and early-life synthetic data.
- `feature_engineering.py` - extracts model features from cycle summary CSVs.
- `train_model.py` - trains an XGBoost model on first 200 cycles to predict actual EOL.
- `predict_health.py` - projects tester-cycle health and creates reports/figures.
- `plot_cycle_curve_comparison.py` - visualizes measured vs predicted charge/discharge curves.
- `run_pipeline.py` - orchestrates training and inference.
- `ANALYSIS.md` - detailed function and workflow analysis.

## Data Layout

- `data/raw/` - full synthetic dataset (1200 cycles per battery).
- `data/raw_early_200/` - early-life tester input dataset (200 cycles per battery).
- `models/` - trained model, feature columns, and model config.
- `reports/` - inference tables and generated figures.

## Notes

- The pipeline uses early observed cycles 1-200 as input and predicts health for cycles 201-1200.
- The model predicts physical EOL, then the inference step maps that prediction to a tester-cycle horizon.
- `ANALYSIS.md` contains deeper explanations for each module, workflow, and core techniques.
