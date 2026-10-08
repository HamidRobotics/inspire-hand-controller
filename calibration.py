"""
calibration.py — Calibration data for motion capture
=====================================================
Two layers:
  Layer A  (range)   — open / close extremes per DOF
  Layer B  (gesture) — personal gesture templates
Both saved as JSON next to this script.
"""
import json, math, pickle
import numpy as np
from pathlib import Path
from typing import Optional

RANGE_FILE   = Path(__file__).parent / "calibration_range.json"
GESTURE_FILE = Path(__file__).parent / "calibration_gestures.json"

DEFAULT_RANGE = {
    "range_min": [0.0]*6,
    "range_max": [1.0]*6,
}

# ── Range calibration ──────────────────────────────────────────────────────
def load_range() -> dict:
    try:
        if RANGE_FILE.exists():
            return json.loads(RANGE_FILE.read_text())
    except Exception:
        pass
    return dict(DEFAULT_RANGE)

def save_range(rmin: list, rmax: list):
    RANGE_FILE.write_text(json.dumps(
        {"range_min": rmin, "range_max": rmax}, indent=2))

def apply_range(raw_dofs: list, rmin: list, rmax: list) -> list:
    """Map raw DOF values through calibrated range to [0, 1000]."""
    out = []
    for i in range(6):
        rng = rmax[i] - rmin[i]
        if rng < 0.01:
            out.append(raw_dofs[i])
            continue
        mapped = (raw_dofs[i]/1000.0 - rmin[i]) / rng
        out.append(int(max(0, min(1000, mapped * 1000))))
    return out

# ── Gesture calibration ────────────────────────────────────────────────────
BUILTIN_GESTURES = {
    "✋ Open":       [1000,1000,1000,1000, 300,1000],
    "✊ Fist":       [  50,  50,  50,  50, 200, 800],
    "👆 Point":      [   0,   0,   0,1000,   0, 600],
    "✌ Victory":    [   0,   0,1000,1000,   0, 600],
    "👍 Thumbs Up": [   0,   0,   0,   0,1000,1000],
    "🤘 Rock":      [1000,   0,   0,1000,   0, 300],
    "🖖 Vulcan":    [1000,1000,1000,1000,1000,1000],
    "👌 OK":        [1000, 900, 800, 400, 630,   0],
    "🤏 Pinch":     [   0,   0,   0, 400, 700,   0],
    "🤙 Call":      [1000,   0,   0,   0,1000,1000],
    "🖐 Spread":    [1000,1000,1000,1000,1000,1000],
}

def load_gestures() -> dict:
    """Load personal gesture templates, merged over builtins."""
    try:
        if GESTURE_FILE.exists():
            personal = json.loads(GESTURE_FILE.read_text())
            merged = dict(BUILTIN_GESTURES)
            merged.update(personal)
            return merged
    except Exception:
        pass
    return dict(BUILTIN_GESTURES)

def save_gesture(name: str, dofs: list):
    try:
        existing = {}
        if GESTURE_FILE.exists():
            existing = json.loads(GESTURE_FILE.read_text())
        existing[name] = dofs
        GESTURE_FILE.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"[calib] save_gesture: {e}")

def delete_gesture(name: str):
    try:
        if GESTURE_FILE.exists():
            data = json.loads(GESTURE_FILE.read_text())
            data.pop(name, None)
            GESTURE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception:
        pass

def classify(dofs: list, templates: dict, threshold: float = 420.0) -> tuple:
    """Return (name, confidence%) of closest gesture."""
    best, best_d = "Unknown", float('inf')
    for name, tmpl in templates.items():
        d = math.sqrt(sum((a-b)**2 for a,b in zip(dofs[:5], tmpl[:5])))
        if d < best_d:
            best_d = d; best = name
    max_d = math.sqrt(5) * 1000
    conf  = max(0, int((1 - best_d/max_d)*100))
    return (best, conf) if best_d < threshold * math.sqrt(5) else ("Unknown", conf)

# ── AI-trained gesture recognizer ───────────────────────────────────────────
# Trained on YOUR OWN recorded samples (captured during calibration — see
# CalibrationDialog's "AI Gesture Training" section), instead of relying
# only on universal hand-tuned templates. Falls back gracefully if
# scikit-learn isn't installed or no model has been trained yet.
MODEL_FILE   = Path(__file__).parent / "gesture_model.pkl"
SAMPLES_FILE = Path(__file__).parent / "gesture_samples.json"

def load_samples() -> dict:
    """label -> list of [6-float] DOF samples captured during training."""
    try:
        if SAMPLES_FILE.exists():
            return json.loads(SAMPLES_FILE.read_text())
    except Exception as e:
        print(f"[gesture_model] load_samples: {e}")
    return {}

def add_samples(label: str, frames: list):
    """Append a batch of captured DOF frames (each a 6-element list) under `label`."""
    try:
        data = load_samples()
        data.setdefault(label, [])
        clean = [[int(round(float(v))) for v in frame] for frame in frames]
        data[label].extend(clean)
        SAMPLES_FILE.write_text(json.dumps(data))
    except Exception as e:
        print(f"[gesture_model] add_samples: {e}")

def clear_samples(label: Optional[str] = None):
    """Clear samples for one gesture, or all gestures if label is None."""
    try:
        if label is None:
            SAMPLES_FILE.write_text(json.dumps({}))
            return
        data = load_samples()
        data.pop(label, None)
        SAMPLES_FILE.write_text(json.dumps(data))
    except Exception as e:
        print(f"[gesture_model] clear_samples: {e}")

def sample_counts() -> dict:
    """label -> number of samples collected so far."""
    return {k: len(v) for k, v in load_samples().items()}

def train_model(min_per_class: int = 8, model_type: str = "knn"):
  
    try:
        if model_type == "rf":
            from sklearn.ensemble import RandomForestClassifier
        else:
            from sklearn.neighbors import KNeighborsClassifier
        from sklearn.model_selection import cross_val_score
    except ImportError:
        return False, "scikit-learn not installed — run:  pip install scikit-learn", None

    data   = load_samples()
    usable = {k: v for k, v in data.items() if len(v) >= min_per_class}
    if len(usable) < 2:
        have = ", ".join(f"{k}={len(v)}" for k, v in data.items()) or "none yet"
        return False, (f"Need at least 2 gestures with ≥{min_per_class} samples each "
                       f"to train. Currently have: {have}"), None

    X, y = [], []
    for label, frames in usable.items():
        for f in frames:
            X.append(f); y.append(label)
    X = np.array(X, dtype=float)

    min_class_n = min(len(v) for v in usable.values())
    if model_type == "rf":
        clf = RandomForestClassifier(n_estimators=80, max_depth=6, random_state=0)
    else:
        k = max(1, min(5, min_class_n - 1))
        clf = KNeighborsClassifier(n_neighbors=k, weights='distance')

    acc = None
    try:
        cv = max(2, min(3, min_class_n))
        scores = cross_val_score(clf, X, y, cv=cv)
        acc = float(scores.mean())
    except Exception:
        pass

    clf.fit(X, y)
    try:
        with open(MODEL_FILE, 'wb') as f:
            pickle.dump({'clf': clf, 'labels': sorted(usable.keys()), 'model_type': model_type}, f)
    except Exception as e:
        return False, f"Failed to save model: {e}", None

    kind = "Random Forest" if model_type == "rf" else "KNN"
    return True, f"Trained {kind} on {len(X)} samples across {len(usable)} gestures.", acc

def load_model():
    """Load the trained model, or None if missing / sklearn unavailable."""
    try:
        if MODEL_FILE.exists():
            with open(MODEL_FILE, 'rb') as f:
                return pickle.load(f)
    except Exception as e:
        print(f"[gesture_model] load_model: {e}")
    return None

def predict_with_model(model: dict, dofs: list) -> tuple:
    """Returns (label, confidence%) using the trained model's probability estimate."""
    clf = model['clf']
    X = np.array([dofs[:6]], dtype=float)
    proba = clf.predict_proba(X)[0]
    classes = clf.classes_
    best_i = int(np.argmax(proba))
    return classes[best_i], int(proba[best_i] * 100)
