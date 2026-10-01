"""Judge calibration against human-reviewed labels.

Input CSV columns: item_id, claim_index, judge_label, human_label (supported|partial|unsupported).
Outputs Cohen's kappa, confusion matrix, human-reviewed groundedness and sample size. Without enough labels
the judge stays UNCALIBRATED; nothing here turns a judge score into a probability.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

LABELS = ("supported", "partial", "unsupported")


def cohen_kappa(pairs: list[tuple[str, str]]) -> float | None:
    n = len(pairs)
    if n == 0:
        return None
    observed = sum(1 for a, b in pairs if a == b) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(ca[label] * cb[label] for label in LABELS) / (n * n)
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


def evaluate(path: Path) -> dict[str, object]:
    rows = list(csv.DictReader(path.open()))
    pairs = [(r["judge_label"], r["human_label"]) for r in rows if r.get("human_label") in LABELS]
    matrix = {j: {h: sum(1 for a, b in pairs if a == j and b == h) for h in LABELS} for j in LABELS}
    grounded = sum(1 for _, h in pairs if h == "supported") / len(pairs) if pairs else None
    return {
        "labels_file": str(path),
        "n_labelled_claims": len(pairs),
        "cohen_kappa": cohen_kappa(pairs),
        "confusion_judge_x_human": matrix,
        "human_reviewed_groundedness": grounded,
        "note": "Calibration evidence only for this dataset/model/rubric version.",
    }


if __name__ == "__main__":
    print(json.dumps(evaluate(Path(sys.argv[1])), indent=2))
