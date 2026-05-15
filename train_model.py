"""Train total-life predictor on data/raw (first 200 cycles)."""

from __future__ import annotations

import argparse
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import GroupKFold

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


def train(data_dir: Path, model_dir: Path) -> None:
    summary = pd.read_csv(data_dir / "cycle_summary.csv")
    meta = pd.read_csv(data_dir / "battery_metadata.csv")

    features = extract_features(summary, meta, max_cycle=200, cycle_offset=0)
    features.to_csv(model_dir.parent / "features_train.csv", index=False)

    X = features.drop(columns=[c for c in DROP_COLS if c in features.columns])
    y = features["actual_eol_cycle"]
    groups = features["battery_id"]

    maes, rmses, r2s = [], [], []
    gkf = GroupKFold(n_splits=5)
    print("5-Fold Group CV (battery-level)...")
    for tr, va in gkf.split(X, y, groups=groups):
        model = xgb.XGBRegressor(
            n_estimators=400,
            learning_rate=0.05,
            max_depth=5,
            subsample=0.85,
            colsample_bytree=0.85,
            random_state=42,
        )
        model.fit(X.iloc[tr], y.iloc[tr])
        pred = model.predict(X.iloc[va])
        maes.append(mean_absolute_error(y.iloc[va], pred))
        rmses.append(np.sqrt(mean_squared_error(y.iloc[va], pred)))
        r2s.append(r2_score(y.iloc[va], pred))

    print(f"  CV MAE  : {np.mean(maes):.1f} cycles")
    print(f"  CV RMSE : {np.mean(rmses):.1f} cycles")
    print(f"  CV R2   : {np.mean(r2s):.4f}")

    final = xgb.XGBRegressor(
        n_estimators=400,
        learning_rate=0.05,
        max_depth=5,
        subsample=0.85,
        colsample_bytree=0.85,
        random_state=42,
    )
    final.fit(X, y)

    model_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(final, model_dir / "battery_life_model.joblib")
    joblib.dump(X.columns.tolist(), model_dir / "feature_columns.joblib")
    joblib.dump({"max_cycle": 200, "target": "actual_eol_cycle"}, model_dir / "model_config.joblib")

    features["predicted_eol_train"] = final.predict(X)
    features.to_csv(model_dir / "train_predictions.csv", index=False)
    print(f"Model saved -> {model_dir}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--model-dir", type=Path, default=Path("models"))
    args = parser.parse_args()
    train(args.data_dir, args.model_dir)


if __name__ == "__main__":
    main()
