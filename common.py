"""Shared helpers: serial parsing, windowing, features, risk engine.
Every other script imports from here, so the SAME code is used for training and live use."""
import csv, json, time
import numpy as np

COLS = ["timestamp_ms", "ax", "ay", "az", "gx", "gy", "gz", "humidity", "ambientTempC",
        "dsTempC", "fsr1", "fsr2", "fsr3", "flex1", "flex2", "flex3",
        "forwardTilt", "sideTilt", "totalTilt"]
FS = 50            # samples per second (matches the ESP32 firmware)
WIN = 100          # window = 2 seconds
STEP = 25          # new window every 0.5 seconds

# ---- Risk engine settings (starting points - tune them with your own data) ----
WEIGHTS = {"trunk_flexion": 0.25, "trunk_rotation": 0.15, "velocity_jerk": 0.10,
           "repetition": 0.15, "loading_proxy": 0.15, "personal_deviation": 0.15, "asymmetry": 0.05}
LEVELS = [(0, 30, "Low"), (30, 55, "Moderate"), (55, 75, "Elevated"), (75, 101, "High")]
CUE_THRESHOLD = 50   # fire a haptic cue at/above this risk score
IMPROVE_MARGIN = 10  # next movement must be at least this many points lower to count as "improved"


def parse_line(line):
    """Parse ESP32 CSV rows (new 19-column firmware or legacy 16-column rows)."""
    parts = line.strip().split(",")
    if len(parts) not in (16, 19):
        return None
    try:
        values = [float(p) for p in parts]
    except (TypeError, ValueError):
        return None

    # Older/demo rows have the original 16 fields. Pad the three new tilt
    # fields so every in-memory row has the same 19-column schema.
    if len(values) == 16:
        values.extend([float("nan")] * 3)

    # Reject rows without a usable timestamp or core IMU values.
    if not all(np.isfinite(v) for v in values[:7]):
        return None
    return values


def save_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        w.writerows(rows)


def load_csv(path):
    rows = []
    with open(path) as f:
        next(f)
        for line in f:
            r = parse_line(line)
            if r:
                rows.append(r)
    return np.array(rows, dtype=float)


def read_serial_seconds(ser, seconds):
    """Read from the ESP32 for N seconds and return an array of rows."""
    rows, t_end = [], time.time() + seconds
    while time.time() < t_end:
        r = parse_line(ser.readline().decode(errors="ignore"))
        if r is not None:
            rows.append(r)
    return np.array(rows, dtype=float)


def windows(data, win=WIN, step=STEP):
    for s in range(0, len(data) - win + 1, step):
        yield data[s:s + win]


# ---------------- ML features (no calibration needed) ----------------
FEATURE_NAMES = [f"{ax}_{st}" for ax in ["ax", "ay", "az", "gx", "gy", "gz"]
                 for st in ["mean", "std", "min", "max", "range", "rms"]] + \
                ["acc_mag_mean", "acc_mag_std", "gyro_mag_mean", "gyro_mag_max", "jerk_mean", "fsr_mean"]


def ml_features(w):
    imu = w[:, 1:7]
    f = []
    for i in range(6):
        x = imu[:, i]
        f += [x.mean(), x.std(), x.min(), x.max(), x.max() - x.min(), np.sqrt((x ** 2).mean())]
    acc = np.linalg.norm(imu[:, :3], axis=1)
    gyr = np.linalg.norm(imu[:, 3:], axis=1)
    jerk = np.abs(np.diff(imu[:, :3], axis=0)) * FS
    f += [acc.mean(), acc.std(), gyr.mean(), gyr.max(), jerk.mean(), w[:, 10:13].mean()]
    return np.array(f)


# ---------------- Physical measurements (use the calibration) ----------------
def physical_metrics(w, base):
    # Use the ESP32's calibrated forward-tilt field (column 16) when present.
    # Fall back to the original accelerometer-vs-baseline angle for legacy rows.
    up = np.array(base["up_vector"], dtype=float)
    up_norm = np.linalg.norm(up)
    if up_norm < 1e-6:
        up = np.array([0.0, 0.0, 1.0])
    else:
        up = up / up_norm

    forward_samples = w[:, 16] if w.shape[1] > 16 else np.array([])
    forward_samples = forward_samples[np.isfinite(forward_samples)]
    if len(forward_samples):
        flex = float(np.median(forward_samples[-25:]))
    else:
        g = w[-25:, 1:4].mean(axis=0)
        g_norm = np.linalg.norm(g)
        flex = float(np.degrees(np.arccos(np.clip(g @ up / max(g_norm, 1e-6), -1, 1))))

    gyro = np.degrees(w[:, 4:7] - np.array(base["gyro_bias"]))   # deg/s
    twist_rate = gyro @ up
    jerk = float(np.abs(np.diff(w[:, 1:4], axis=0)).mean() * FS)
    fsr = w[:, 10:13].mean(axis=0)
    load = float(max(0.0, fsr.mean() - base["fsr_rest"]))
    asym = float(abs(fsr[0] - fsr[1]))
    return {"flex": flex, "twist_peak": float(np.percentile(np.abs(twist_rate), 95)),
            "twist_sign": float(np.sign(twist_rate.mean())), "jerk": jerk, "load": load,
            "asym": asym, "asym_sign": float(np.sign(fsr[0] - fsr[1]))}


def _clip01(x):
    return float(min(1.0, max(0.0, x)))


def risk_score(w, base, reps_last_minute):
    m = physical_metrics(w, base)
    z = np.abs((ml_features(w) - np.array(base["feat_mean"])) / np.array(base["feat_std"]))
    parts = {
        "trunk_flexion": _clip01((m["flex"] - 10) / 50),
        "trunk_rotation": _clip01((m["twist_peak"] - 30) / 120),
        "velocity_jerk": _clip01((m["jerk"] / max(base["jerk_ref"], 1e-6) - 1) / 2),   # WEB-APP GUARD: avoid /0 on a motionless calibration
        "repetition": _clip01(reps_last_minute / 10),
        "loading_proxy": _clip01(m["load"] / base["fsr_span"]),
        "personal_deviation": _clip01(z.mean() / 3),
        "asymmetry": _clip01(m["asym"] / base["fsr_span"]),
    }
    # Make sustained forward bending the dominant ergonomic signal.
    # At neutral posture the posture term is near zero; at >=60 degrees it
    # reaches its maximum. Secondary signals can add up to 30 points, so a
    # strongly bent posture cannot score safer than an upright posture solely
    # because movement/noise/deviation terms happened to be lower.
    posture_risk = _clip01((abs(m["flex"]) - 10.0) / 50.0)
    secondary_keys = ("trunk_rotation", "velocity_jerk", "repetition",
                      "loading_proxy", "personal_deviation", "asymmetry")
    secondary_weight = sum(WEIGHTS[k] for k in secondary_keys)
    secondary_risk = sum(WEIGHTS[k] * parts[k] for k in secondary_keys) / secondary_weight
    score = 100.0 * (0.70 * posture_risk + 0.30 * secondary_risk)
    parts["trunk_flexion"] = posture_risk
    level = next(n for lo, hi, n in LEVELS if lo <= score < hi)
    return score, level, parts, m


def choose_cue(parts, m):
    """Pick which motor(s) to buzz: L, R or B (both)."""
    top = max(parts, key=lambda k: WEIGHTS[k] * parts[k])
    if top == "trunk_rotation":
        return ("R" if m["twist_sign"] > 0 else "L"), top
    if top == "asymmetry":
        return ("R" if m["asym_sign"] > 0 else "L"), top
    return "B", top


def compute_baseline(chunks, worker="W01"):
    """chunks = [stand, bend, twist, lift] arrays recorded during calibration."""
    stand = chunks[0][FS * 2:]                      # skip first 2 s
    moving = np.vstack(chunks[1:])
    feats = np.array([ml_features(w) for c in chunks for w in windows(c)])
    mean = feats.mean(axis=0)
    std = np.maximum(feats.std(axis=0), 0.01 + 0.05 * np.abs(mean))   # floor so tiny stds don't explode z-scores
    return {"worker": worker, "up_vector": stand[:, 1:4].mean(axis=0).tolist(),
            "gyro_bias": stand[:, 4:7].mean(axis=0).tolist(), "fsr_rest": float(stand[:, 10:13].mean()),
            "fsr_span": 1500.0, "jerk_ref": float(np.abs(np.diff(moving[:, 1:4], axis=0)).mean() * FS),
            "feat_mean": mean.tolist(), "feat_std": std.tolist(), "feature_names": FEATURE_NAMES}


# ---------------------------------------------------------------------------
# ADDED for the web app (does not change any calculation above).
# Explains how the final score is made up, mirroring the exact formula in
# risk_score(): 70 points from forward bending + 30 points from the other six
# factors, weighted by WEIGHTS. The points always add up to the score.
# ---------------------------------------------------------------------------
def risk_breakdown(parts):
    """Return {factor: points} that sum to the risk score for the given parts."""
    secondary_keys = ("trunk_rotation", "velocity_jerk", "repetition",
                      "loading_proxy", "personal_deviation", "asymmetry")
    secondary_weight = sum(WEIGHTS[k] for k in secondary_keys)
    pts = {"trunk_flexion": 70.0 * parts["trunk_flexion"]}
    for k in secondary_keys:
        pts[k] = 30.0 * (WEIGHTS[k] * parts[k]) / secondary_weight
    return pts
