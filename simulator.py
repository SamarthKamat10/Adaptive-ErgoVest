"""
DEMO-mode signal generator for Adaptive ErgoVest.

RECONSTRUCTED MODULE. The original simulator.py was not in the handoff ZIP.
This file implements the interface that app.py relies on:

    synth(kind, seconds, seed, peak=35, per=4, twist_amp=0) -> ndarray (n, 19)
    Sim().next() -> one 19-value row (same order as common.COLS), 50 rows/s

Everything produced here is SYNTHETIC. It follows the firmware's geometry
(Y axis = vertical, Z axis = forward bending, forwardTilt = atan2(|az|,|ay|))
so the unchanged risk engine in common.py can run on it. It is only ever used
in DEMO mode and is never saved as training data.
"""
import numpy as np

from common import FS

G = 9.80665


def _row_block(theta_deg, yaw_rate, seed, walk=False, load_gain=8.0):
    """Build an (n, 19) array from a forward-angle curve (deg) and a twist rate (rad/s)."""
    rng = np.random.default_rng(seed)
    n = len(theta_deg)
    t = np.arange(n) / FS
    th = np.radians(theta_deg)
    dth = np.gradient(th) * FS                       # rad/s about the X axis
    ay = G * np.cos(th) + rng.normal(0, 0.06, n)
    az = G * np.sin(th) + rng.normal(0, 0.06, n)
    ax = 0.35 * np.sin(2 * np.pi * 0.15 * t) + 0.25 * yaw_rate + rng.normal(0, 0.06, n)
    if walk:                                         # gait bounce ~1.8 Hz
        ay = ay + 1.6 * np.sin(2 * np.pi * 1.8 * t)
        az = az + 0.9 * np.sin(2 * np.pi * 1.8 * t + 1.0)
    gx = dth + rng.normal(0, 0.02, n)
    gy = yaw_rate + rng.normal(0, 0.02, n)
    gz = rng.normal(0, 0.02, n)
    fsr_base = 300 + load_gain * np.degrees(th)
    fsr1 = fsr_base + rng.normal(0, 6, n) + 25
    fsr2 = fsr_base + rng.normal(0, 6, n)
    fsr3 = 0.7 * fsr_base + rng.normal(0, 6, n)
    flex_base = 1800 + 9.0 * np.degrees(th)
    flex1 = flex_base + rng.normal(0, 5, n)
    flex2 = flex_base * 0.98 + rng.normal(0, 5, n)
    flex3 = flex_base * 1.02 + rng.normal(0, 5, n)
    hum = 45 + 0.5 * np.sin(t / 9) + rng.normal(0, 0.1, n)
    amb = 27.0 + 0.2 * np.sin(t / 13) + rng.normal(0, 0.03, n)
    ds = 33.0 + 0.3 * np.sin(t / 17) + rng.normal(0, 0.03, n)
    fwd = np.degrees(np.arctan2(np.abs(az), np.abs(ay)))
    side = np.degrees(np.arctan2(np.abs(ax), np.sqrt(ay ** 2 + az ** 2)))
    tot = np.degrees(np.arctan2(np.sqrt(ax ** 2 + az ** 2), np.abs(ay)))
    ts = np.arange(n) * (1000.0 / FS)
    out = np.column_stack([ts, ax, ay, az, gx, gy, gz, hum, amb, ds, fsr1, fsr2, fsr3,
                           np.round(flex1), np.round(flex2), np.round(flex3), fwd, side, tot])
    out[:, 10:16] = np.clip(np.round(out[:, 10:16]), 0, 4095)
    return out


def synth(kind, seconds, seed, peak=35, per=4, twist_amp=0):
    """Synthetic recording. kind: 'flat' | 'walk' | 'bend' | 'twist'."""
    n = int(seconds * FS)
    t = np.arange(n) / FS
    rng = np.random.default_rng(seed)
    theta = 2.0 + rng.normal(0, 0.15, n)             # upright: ~2 deg
    yaw = np.zeros(n)
    walk = False
    if kind == "walk":
        theta = theta + 1.5 * np.sin(2 * np.pi * 1.8 * t)
        walk = True
    elif kind == "bend":
        theta = theta + peak * 0.5 * (1 - np.cos(2 * np.pi * t / per))
        if twist_amp:
            yaw = twist_amp * np.sin(2 * np.pi * t / (per * 0.75))
    elif kind == "twist":
        amp = twist_amp or 1.6                       # rad/s (~90 deg/s)
        yaw = amp * np.sin(2 * np.pi * t / 3.5)
    elif kind != "flat":
        raise ValueError(f"unknown synth kind: {kind}")
    return _row_block(theta, yaw, seed, walk=walk)


# Demo storyline (seconds, forward-angle target in deg, twist rate rad/s, label).
# Loops forever so a demo passes through LOW, MODERATE, ELEVATED and HIGH.
_STORY = [
    (8, 3, 0.0), (5, 22, 0.0), (6, 3, 0.0),         # upright, small bend
    (7, 50, 0.0), (5, 3, 0.0),                       # elevated bend
    (9, 75, 1.2), (4, 3, 0.0),                       # deep bend -> high
    (7, 14, 1.8), (6, 3, 0.0),                       # twisting
]


class Sim:
    """Row-by-row simulated wearer. Call next() at 50 Hz."""

    def __init__(self, seed=7):
        rng = np.random.default_rng(seed)
        theta, yaw = [], []
        cur = 3.0
        for secs, target, tw in _STORY:
            n = int(secs * FS)
            ramp = np.minimum(1.0, np.arange(n) / (FS * 1.2))
            seg = cur + (target - cur) * (0.5 - 0.5 * np.cos(np.pi * ramp))
            wob = 0.4 * np.sin(2 * np.pi * np.arange(n) / FS * 0.3)
            theta.append(seg + wob + rng.normal(0, 0.15, n))
            yy = tw * np.sin(2 * np.pi * np.arange(n) / FS / 3.0) if tw else np.zeros(n)
            yaw.append(yy)
            cur = target
        self._loop = _row_block(np.concatenate(theta), np.concatenate(yaw), seed + 1)
        self._i = 0
        self._t_ms = 0.0

    def next(self):
        row = self._loop[self._i].copy()
        self._i = (self._i + 1) % len(self._loop)
        row[0] = self._t_ms
        self._t_ms += 1000.0 / FS
        return row.tolist()
