"""
Movement-classifier training for Adaptive ErgoVest.

RECONSTRUCTED MODULE. The original trainer.py was not in the handoff ZIP, so this
file is built from how app.py uses it:

    trainer.fit_model(X, y, g, log=callable, n_trees=int) -> fitted model with .predict()
    trainer.train(log=callable) -> fitted model, also saved to models/movement_model.joblib

What is known from the original code:  features come from common.ml_features(),
windows from common.windows(), recordings are data/labeled/<label>__<time>.csv,
labels are the strings in app.LABELS, and the model is loaded with joblib.
What is NOT known: the original learning algorithm and its settings. A random
forest is used here because fit_model takes `n_trees`. Any accuracy shown comes
from cross-validation run on YOUR recordings at training time (never hard-coded).
A model trained by your original trainer.py still loads, as long as it is a
scikit-learn estimator trained on common.ml_features() in FEATURE_NAMES order.
"""
import glob, json, os, time

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import GroupKFold

from common import FEATURE_NAMES, WIN, STEP, load_csv, ml_features, windows

HERE = os.path.dirname(os.path.abspath(__file__))
LABELED_DIR = os.path.join(HERE, "data", "labeled")
MODEL_PATH = os.path.join(HERE, "models", "movement_model.joblib")
META_PATH = os.path.join(HERE, "models", "movement_model_meta.json")


def fit_model(X, y, g, log=print, n_trees=200):
    """Fit a random forest. `g` = recording id of each window (windows from the same
    recording overlap, so cross-validation must never split a recording)."""
    X, y, g = np.asarray(X, float), np.asarray(y), np.asarray(g)
    classes = sorted(set(y.tolist()))
    log(f"Training on {len(X)} windows from {len(set(g.tolist()))} recordings, {len(classes)} movement types: {', '.join(classes)}")

    def make():
        return RandomForestClassifier(n_estimators=n_trees, class_weight="balanced",
                                      random_state=42, n_jobs=1)

    cv_acc, cv_note = None, "not evaluated"
    n_groups = len(set(g.tolist()))
    if n_groups >= 3 and len(classes) >= 2:
        try:
            folds = min(5, n_groups)
            scores = []
            for tr, te in GroupKFold(n_splits=folds).split(X, y, g):
                if len(set(y[tr].tolist())) < 2:
                    continue
                scores.append(float((make().fit(X[tr], y[tr]).predict(X[te]) == y[te]).mean()))
            if scores:
                cv_acc, cv_note = float(np.mean(scores)), f"{len(scores)}-fold cross-validation, recordings kept separate"
        except Exception as e:                       # never let evaluation break training
            cv_note = f"evaluation failed: {e}"
    if cv_acc is None:
        log(f"Accuracy: not evaluated ({cv_note}). Record at least 3 files for cross-validation.")
    else:
        log(f"Cross-validated accuracy on your recordings: {cv_acc * 100:.1f}% ({cv_note}).")
        log("This only reflects how well it separates YOUR recorded movements; it is not a clinical measure.")
    model = make().fit(X, y)
    model.ergovest_meta = {"classes": classes, "windows": int(len(X)), "recordings": int(n_groups),
                           "cv_accuracy": cv_acc, "cv_note": cv_note, "feature_names": FEATURE_NAMES,
                           "trained_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    return model


def load_labeled(log=print):
    files = sorted(glob.glob(os.path.join(LABELED_DIR, "*.csv")))
    X, y, g = [], [], []
    for k, path in enumerate(files):
        label = os.path.basename(path).split("__")[0]
        data = load_csv(path)
        n = 0
        for w in windows(data, WIN, STEP):
            X.append(ml_features(w)); y.append(label); g.append(k); n += 1
        log(f"  {os.path.basename(path)}: {len(data)} rows -> {n} windows ({label})")
    return X, y, g, files


def train(log=print):
    """Train from recordings in data/labeled/ and save the model. Raises a readable
    error when there is not enough real data."""
    X, y, g, files = load_labeled(log)
    if not files:
        raise RuntimeError("No recordings found in data/labeled/. Record each movement type in LIVE mode first.")
    if len(set(y)) < 2:
        raise RuntimeError(f"Only one movement type recorded ({y[0] if y else '-'}). Record at least two different types.")
    model = fit_model(X, y, g, log=log)
    os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
    joblib.dump(model, MODEL_PATH)
    with open(META_PATH, "w") as f:
        json.dump(model.ergovest_meta, f, indent=2)
    log(f"Saved model to {os.path.relpath(MODEL_PATH, HERE)}")
    return model
