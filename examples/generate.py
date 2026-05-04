"""
Generate the example AIC Phase 1 input pair.

Produces:
  examples/AIC_Phase1_input.xlsx     META + FINAL sheets
  examples/AIC_Phase1_flat_file.csv  new products to classify

The shape mirrors what the real ml_package expects in production —
META defines which raw columns key off which output attribute, FINAL
is historical labelled data, the CSV is the new-product flat file.

Run from the repo root:
    python examples/generate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Reuse the test fixture builders so the example data stays in lock-step
# with what the integration suite exercises.  If those builders change,
# the examples regenerate to match.
from tests.fixtures import build_flat_file_df, build_history_df, build_meta_df

import pandas as pd

OUT_DIR = REPO / "examples"
XLSX = OUT_DIR / "AIC_Phase1_input.xlsx"
CSV  = OUT_DIR / "AIC_Phase1_flat_file.csv"


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)

    with pd.ExcelWriter(XLSX, engine="xlsxwriter") as writer:
        build_meta_df().to_excel(writer, sheet_name="META", index=False)
        build_history_df().to_excel(writer, sheet_name="FINAL", index=False)

    build_flat_file_df().to_csv(CSV, index=False)

    print(f"wrote {XLSX.relative_to(REPO)}  ({XLSX.stat().st_size:,} bytes)")
    print(f"wrote {CSV.relative_to(REPO)}  ({CSV.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
