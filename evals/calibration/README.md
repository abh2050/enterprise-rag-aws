# Judge calibration

Status: **UNCALIBRATED.** No human-reviewed labels exist yet.

The judge returns categorical outcomes (`pass`/`revise`/`abstain`) and per-claim support labels. These are **not**
probabilities of correctness and must never be shown as such.

To calibrate:
1. Run `make eval` (or `make eval-live` for real models). Each report writes `judge_review_template.csv` with
   question, claims and cited passages (synthetic data only, or reviewed private data handled under policy).
2. Reviewers fill `human_label` (`supported` | `partial` | `unsupported`) per claim and `human_outcome`
   (`pass` | `revise` | `abstain`). Save the file as `labels/<dataset>-<date>.csv`.
3. Run `uv run python -m erp_evals.calibration labels/<file>.csv`. It reports Cohen's kappa, a confusion matrix,
   and the human-reviewed groundedness rate, along with sample size and dataset id.
4. Only after agreement is acceptable (target set by the data owner, e.g. κ ≥ 0.6 on ≥ 200 claims) may
   `calibrated=true` be recorded for that judge model + rubric version.
