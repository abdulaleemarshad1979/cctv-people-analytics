"""Score head counts against hand counts and fit a per-camera correction (method from CCTV_MONITOR/src/counting_accuracy.py).

CSV with a header: camera,model,actual
  camera = camera id, model = the "Crowd model" number shown for that camera (raw, before correction),
  actual = the number of people you counted by hand in the same frame. Three or more rows per camera for a fit.

  python3 scripts/calibrate_counts.py counts.csv              # accuracy before / after, per camera
  python3 scripts/calibrate_counts.py counts.csv --write cameras.json   # also store count_scale / count_bias

Accuracy = 100 - mean(|model - actual| / max(actual, 1)) in percent.
"""
import csv
import json
import sys
from collections import defaultdict

import numpy as np


def accuracy(predicted, actual):
    predicted, actual = np.asarray(predicted, float), np.asarray(actual, float)
    return float(np.clip(100 - np.mean(np.abs(predicted - actual) / np.maximum(actual, 1)) * 100, 0, 100))


def fit(predicted, actual):
    """Least-squares actual ~ predicted * scale + bias; scale kept in the 0.1-10 range the app accepts."""
    if len(predicted) < 3:
        raise ValueError("need at least 3 samples")
    scale, bias = np.linalg.lstsq(np.column_stack((predicted, np.ones(len(predicted)))), np.asarray(actual, float), rcond=None)[0]
    return float(np.clip(scale, 0.1, 10)), float(np.clip(bias, -500, 500))


def main(path, write=None):
    rows = defaultdict(lambda: ([], []))
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows[r["camera"]][0].append(float(r["model"]))
            rows[r["camera"]][1].append(float(r["actual"]))
    fits = {}
    for cam, (pred, act) in sorted(rows.items()):
        line = f"{cam}: {len(pred)} samples, accuracy now {accuracy(pred, act):.1f} %"
        try:
            scale, bias = fits[cam] = fit(pred, act)
            line += f" -> {accuracy(np.asarray(pred) * scale + bias, act):.1f} % with scale {scale:.3f}, bias {bias:+.1f}"
        except ValueError as exc:
            line += f" ({exc} for a fit)"
        print(line)
    allp, alla = [v for p, _ in rows.values() for v in p], [v for _, a in rows.values() for v in a]
    print(f"all cameras: {accuracy(allp, alla):.1f} % (target 90)")
    if write and fits:
        cfg = json.load(open(write))
        for cam in cfg["cameras"]:
            if cam["id"] in fits:
                cam["count_scale"], cam["count_bias"] = (round(v, 4) for v in fits[cam["id"]])
        json.dump(cfg, open(write, "w"), indent=2)
        print(f"wrote {len(fits)} cameras to {write}; restart the app to apply")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        sys.exit(__doc__)
    main(args[0], args[args.index("--write") + 1] if "--write" in args else None)
