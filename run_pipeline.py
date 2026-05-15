"""
Train on data/raw and predict from data/raw_early_200.

Tester view: input cycles 1-200, predict health for cycles 201-1200.
(Performance level of physical 501-700, relabeled as tester 1-200.)
"""

from pathlib import Path

from predict_health import run
from train_model import train


def main() -> None:
    root = Path(__file__).parent
    train(root / "data" / "raw", root / "models")
    run(
        root / "data" / "raw",
        root / "data" / "raw_early_200",
        root / "models",
        root / "reports",
        max_plot_batteries=6,
    )


if __name__ == "__main__":
    main()
