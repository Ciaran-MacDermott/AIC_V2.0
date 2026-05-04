# Example Phase 1 inputs

A tiny, end-to-end working pair of files for smoke-testing the AIC
pipeline. Drop them into the Phase 1 upload form to see lookup +
BM25 + XGBoost ensemble run on a few rows, then walk through the QC
wizard, finalise, and download the produced workbook.

## Files

| File | What's in it |
|---|---|
| `AIC_Phase1_input.xlsx` | Two sheets: `META` (attribute config — which raw columns key off which output attribute) and `FINAL` (60 rows of historical labelled product data). |
| `AIC_Phase1_flat_file.csv` | 8 new products to classify. |
| `generate.py` | Regenerates the two files from `tests/fixtures.py`. |

## What the data exercises

The synthetic data covers three brands (`ACME`, `ZETA`, `OMEGA`) and
five pack sizes (`8 OZ`, `12 OZ`, `16 OZ`, `24 OZ`, `32 OZ`) across two
attributes (`BRAND`, `PACK_SIZE`). The 8 CSV rows deliberately include
each match path:

- **Exact key match** — same description and brand text as a history
  row (lookup wins immediately).
- **Same brand, novel description** — analyst-style new copy ("premium
  acme") with a recognisable brand keyword (BM25 helps).
- **Slight key drift** — abbreviated forms ("acme" vs "acme co") and
  variant pack expressions ("twenty four oz") that need fuzzy + the
  ensemble vote to land on the right label.

## Use

1. Run the app locally (FastAPI on 8000 + Next on 3000).
2. On the Phase 1 page, pick "Individual files" and upload the xlsx +
   csv from this directory.
3. Click **Run pipeline**. You should see four stages tick through
   (lookup → BM25 → XGB → ensemble → QC ready).
4. The QC wizard surfaces two sheets — `BRAND` and `PACK_SIZE`. Edit
   any predictions you don't like (try a deliberate change to confirm
   it round-trips into the produced workbook), then **Save & Finalise**.
5. Download `File_For_Mapping_QC.xlsx` and either chain into Phase 2
   directly via the button, or download the run bundle for the audit
   trail.

## Regenerate

If you tweak `tests/fixtures.py` and want the example files updated:

```bash
python examples/generate.py
```
