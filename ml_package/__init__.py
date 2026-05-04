"""
ml_package — Phase 1 ML pipeline modules.

  mapping_lookup  : fuzzy historical match engine (Step 1)
  text_match      : BM25Plus text retrieval predictor (Step 2a)
  xgb_classifier  : XGBoost TF-IDF classifier (Step 2b)
  ensemble        : predictor combiner and QC annotator (Step 3)
  write_results   : xlsxwriter Excel output
"""
