"""
ErgoVest engine: the ONE owner of the ESP32 serial port and the shared live state.

Adapted from the Engine class in the original app.py. The signal processing,
episode / cue / verification logic and risk calls are unchanged. Added:
  * honest motor-command results (success only when bytes were written)
  * serial race fix (a lost old port can no longer wipe a newly opened one)
  * stale / disconnected / malformed-line accounting
  * per-user monitoring sessions (history), cancel for guided tasks
  * data-epoch + sequence numbers so many browsers can follow one stream
Nothing here knows about HTTP or users beyond an integer user id.
"""
import csv, json, math, os, re, threading, time
from collections import deque

import joblib
import numpy as np

import trainer
from common import (COLS, FS, WIN, STEP, CUE_THRESHOLD, IMPROVE_MARGIN, LEVELS, parse_line, ml_features,
                    risk_score, risk_breakdown, choose_cue, compute_baseline, save_csv)
from simulator import Sim, synth

HERE = os.path.dirname(os.path.abspath(__file__))
for _d in ("data/labeled", "data/calibration", "models", "logs", "instance"):
    os.makedirs(os.path.join(HERE, _d), exist_ok=True)

RAW_POINTS = 750            # 15 s of raw rows kept for charts
RISK_POINTS = 600           # 5 min of risk history (2 per second)
STALE_SECONDS = 2.0         # no row for this long => data is stale
BAUD = 115200
LABELS = ["standing", "walking", "bend_safe", "bend_deep", "twist", "lift_safe", "lift_risky"]
LEVEL_NAMES = [n for _, _, n in LEVELS]
LIKELY_VIDS = {0x10C4, 0x1A86, 0x0403, 0x303A}      # CP210x, CH340, FTDI, Espressif
SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
P = lambda *a: os.path.join(HERE, *a)


def json_safe(o):
    """NaN/inf -> null, numpy -> python (same idea as original clean())."""
    if isinstance(o, dict): return {k: json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)): return [json_safe(v) for v in o]
    if isinstance(o, (np.floating, float)): return None if (math.isnan(o) or math.isinf(o)) else float(o)
    if isinstance(o, np.integer): return int(o)
    if isinstance(o, np.str_): return str(o)
    return o


def row_json(row):
    return [None if (v != v or v in (float("inf"), float("-inf"))) else round(float(v), 4) for v in row]


def build_demo_brain():
    """In-memory SIMULATED baseline + movement model for DEMO mode. Never saved to disk."""
    chunks = [synth("flat", 15, 101), synth("bend", 20, 102, peak=35, per=4),
              synth("twist", 20, 103), synth("bend", 15, 104, peak=40, per=5)]
    base = compute_baseline(chunks, "DEMO")
    X, y, g, k = [], [], [], 0
    spec = {"standing": ("flat", 0, 0), "walking": ("walk", 0, 0), "bend_safe": ("bend", 30, 4),
            "bend_deep": ("bend", 85, 1.8), "twist": ("twist", 0, 0)}
    for lab, (kind, pk, per) in spec.items():
        for _ in range(3):
            d = synth(kind, 40 if kind != "bend" else 36, 200 + k, peak=pk, per=per or 4,
                      twist_amp=1.8 if lab == "bend_deep" else 0)
            for s in range(0, len(d) - WIN + 1, STEP):
                X.append(ml_features(d[s:s + WIN])); y.append(lab); g.append(k)
            k += 1
    return base, trainer.fit_model(X, y, g, log=lambda *_: None, n_trees=60)


class Recorder:
    """Accumulates one user's monitoring session (what the History page shows)."""
    def __init__(self, db_id, user_id, mode, started):
        self.db_id, self.user_id, self.mode, self.started = db_id, user_id, mode, started
        self.n = 0; self.risk_sum = 0.0; self.risk_max = 0.0; self.cues = 0
        self.levels = {n: 0 for n in LEVEL_NAMES}
        self.series, self.bucket_len = [], 2.0
        self._b = None                                    # [t0, n, sum, max, flex_sum]

    def add(self, now, risk, level, flex):
        self.n += 1; self.risk_sum += risk; self.risk_max = max(self.risk_max, risk)
        self.levels[level] = self.levels.get(level, 0) + 1
        if self._b is None: self._b = [now, 0, 0.0, 0.0, 0.0]
        b = self._b; b[1] += 1; b[2] += risk; b[3] = max(b[3], risk); b[4] += flex
        if now - b[0] >= self.bucket_len:
            self.series.append([round(b[0] - self.started, 1), round(b[2] / b[1], 1), round(b[3], 1), round(b[4] / b[1], 1)])
            self._b = None
            if len(self.series) > 4000:
                self.series = self.series[::2]; self.bucket_len *= 2

    def summary(self):
        series = list(self.series)
        if self._b and self._b[1]:                         # include the not-yet-closed last interval
            b = self._b; series.append([round(b[0] - self.started, 1), round(b[2] / b[1], 1), round(b[3], 1), round(b[4] / b[1], 1)])
        return {"windows": self.n, "avg_risk": round(self.risk_sum / self.n, 1) if self.n else None,
                "max_risk": round(self.risk_max, 1) if self.n else None, "levels": self.levels,
                "cues": self.cues, "series": series}


class Engine:
    def __init__(self, store=None, allow_any_port=False):
        self.lock = threading.RLock()
        self.ser_lock = threading.Lock()
        self.stop = False
        self.store = store
        self.allow_any_port = allow_any_port
        self.mode = "demo"
        self.port, self.ser = None, None
        self.sim = Sim()
        self.raw = deque(maxlen=RAW_POINTS)               # (seq, row)
        self.buf = deque(maxlen=WIN)
        self.stamps = deque(maxlen=100)
        self.seq = 0; self.n = 0; self.epoch = 0
        self.last_row_time = 0.0
        self.capture = None
        self.cancel = False
        self.task = {"name": "idle", "text": "", "remaining": 0}
        self.events = deque(maxlen=60); self.event_seq = 0
        self.report = ""
        self.diag = {"bad_lines": 0, "imu_invalid": 0, "fw_message": None}
        self.recorders = {}
        self._last_conn = None; self._last_save = time.time(); self._logwarn = False
        self.base = json.load(open(P("models", "baseline.json"))) if os.path.exists(P("models", "baseline.json")) else None
        self.model = joblib.load(P("models", "movement_model.joblib")) if os.path.exists(P("models", "movement_model.joblib")) else None
        self.demo_base = self.demo_model = None
        self.status_seq = 0
        self.reset_session(log=False)
        self.event("Started in DEMO mode (simulated data)", "info")

    # ---------- helpers ----------
    def event(self, msg, kind="info"):
        with self.lock:
            self.event_seq += 1
            self.events.appendleft({"t": time.strftime("%H:%M:%S"), "msg": msg, "kind": kind})

    def reset_session(self, log=True):
        with self.lock:
            self.risk_hist, self.flex_hist = deque(maxlen=RISK_POINTS), deque(maxlen=RISK_POINTS)
            self.in_ep, self.ep_peak, self.ep_cued, self.pending_pre = False, 0.0, False, None
            self.ep_times = deque(); self.last_cue = 0.0
            self.cues = self.improved = self.total = 0
            self.last_result, self.last_cue_info = "-", None
            self.status = {}; self.status_seq += 1
            if log: self.event("Session counters reset", "info")

    def _clear_data(self):
        with self.lock:
            self.raw.clear(); self.buf.clear(); self.stamps.clear(); self.n = 0
            self.last_row_time = 0.0; self.epoch += 1
            self.diag = {"bad_lines": 0, "imu_invalid": 0, "fw_message": None}
            self.reset_session(log=False)

    def brain(self):
        if self.mode == "demo":
            return self.demo_base, self.demo_model
        return self.base, self.model

    def start_background(self):
        threading.Thread(target=self._prepare_demo, daemon=True).start()
        threading.Thread(target=self.run, daemon=True).start()

    def _prepare_demo(self):
        try:
            self.demo_base, self.demo_model = build_demo_brain()
        except Exception as e:
            self.event(f"Could not prepare the DEMO model: {e}", "bad")

    def age(self):
        return (time.time() - self.last_row_time) if self.last_row_time else None

    def conn_state(self):
        """demo | disconnected | waiting | stale | ok"""
        if self.mode == "demo": return "demo"
        if not self.ser: return "disconnected"
        a = self.age()
        if a is None: return "waiting"
        return "stale" if a > STALE_SECONDS else "ok"

    # ---------- ports / connection ----------
    def list_ports(self):
        try:
            from serial.tools import list_ports
            out = []
            for p in list_ports.comports():
                out.append({"device": p.device, "description": p.description or "",
                            "likely": (p.vid in LIKELY_VIDS) if p.vid else False})
            return sorted(out, key=lambda d: (not d["likely"], d["device"]))
        except Exception:
            return []

    def connect(self, port):
        """Open the serial port and switch to LIVE. Only this method opens a port."""
        port = (port or "").strip()
        if not port: return False, "Choose a serial port first"
        if not self.allow_any_port and port not in [p["device"] for p in self.list_ports()]:
            return False, f"{port} is not an available serial port. Plug in the ESP32 and refresh the list."
        try:
            import serial
        except ImportError:
            return False, "pyserial is not installed (pip install -r requirements.txt)"
        with self.ser_lock:
            self._close_serial()
            self._finish_all("Connection changed")
            try:
                ser = serial.serial_for_url(port, baudrate=BAUD, timeout=1)
            except Exception as e:
                msg = f"Could not open {port}: {e}. Close Arduino Serial Monitor or any other program using it."
                self.event(msg, "bad"); return False, msg
            time.sleep(2)                                  # ESP32 resets when the port opens
            # Input is NOT flushed: the ESP32's boot messages (sensor errors) are useful diagnostics.
            self.ser, self.port, self.mode = ser, port, "live"
            self._clear_data()
        self.event(f"Opened {port} at {BAUD} baud (LIVE mode). Waiting for data...", "ok")
        return True, f"Opened {port}. Waiting for sensor data"

    def _close_serial(self):
        if self.ser:
            try: self.ser.close()
            except Exception: pass
        self.ser = None

    def disconnect(self):
        with self.ser_lock:
            had = self.port
            self._close_serial(); self.port = None
            self._finish_all("Sensor disconnected")
            with self.lock:
                self.status = {}; self.status_seq += 1
        self.event(f"Disconnected from {had}" if had else "Not connected", "info")
        return True, "Disconnected"

    def use_demo(self):
        with self.ser_lock:
            self._close_serial(); self.port = None
            self._finish_all("Mode changed")
            self.mode = "demo"; self._clear_data()
        self.event("Switched to DEMO mode (simulated worker)", "info")
        return True, "DEMO mode"

    def use_live(self):
        with self.ser_lock:
            if self.mode == "live": return True, "Already in LIVE mode"
            self._finish_all("Mode changed")
            self.mode = "live"; self._clear_data()
        self.event("Switched to LIVE mode. Connect the ESP32 to receive data.", "info")
        return True, "LIVE mode. Choose a port and connect"

    def send_cue(self, cmd):
        """Send one motor command. Returns (ok, message); ok only if bytes were written."""
        if cmd not in ("L", "R", "B", "F", "S"): return False, "Unknown motor command"
        ser = self.ser
        if self.mode != "live" or not ser:
            return False, "Not sent: DEMO mode has no motors" if self.mode == "demo" else "Not sent: no ESP32 connected"
        try:
            with self.ser_lock:
                ser.write((cmd + "\n").encode()); ser.flush()
            return True, f"Command {cmd} sent to {self.port}"
        except Exception as e:
            self.event(f"Motor command failed: {e}", "bad")
            return False, f"Not sent: {e}"

    # ---------- main loop ----------
    def run(self):
        next_t = time.time()
        while not self.stop:
            row, ser_used = None, None
            try:
                if self.mode == "demo":
                    next_t += 1 / FS
                    time.sleep(max(0, next_t - time.time()))
                    if next_t < time.time() - 1: next_t = time.time()
                    row = self.sim.next()
                elif self.ser:
                    ser_used = self.ser
                    row = self._read_serial(ser_used)
                else:
                    time.sleep(0.1)
            except Exception as e:                         # serial lost / unplugged
                with self.ser_lock:
                    if ser_used is not None and self.ser is ser_used:   # race fix: only our own port
                        self.event(f"Serial connection lost: {e}", "bad")
                        self._close_serial(); self._finish_all("Sensor disconnected")
                        with self.lock: self.status = {}; self.status_seq += 1
                time.sleep(0.2)
            if row:
                try: self.feed(row)
                except Exception as ex:                    # never let one bad window kill the reader thread
                    if time.time() - getattr(self, "_last_err", 0) > 10:
                        self._last_err = time.time(); self.event(f"Processing error (reading continues): {ex}", "bad")
            try: self._housekeeping()
            except Exception: pass

    def _read_serial(self, ser):
        line = ser.readline().decode(errors="ignore").strip()
        if not line: return None
        parts = line.split(",")
        looks_csv = len(parts) > 3 and (line[0].isdigit() or line[0] in "+-")
        if looks_csv:
            row = parse_line(line)
            if row is None:
                self.diag["bad_lines"] += 1
                if len(parts) in (16, 19):
                    self.diag["imu_invalid"] += 1
                    if self.diag["imu_invalid"] in (1, 50):
                        self.event("Rows arrive but the IMU values are invalid (MPU6050 not responding?). Check SDA/SCL wiring.", "warn")
            return row
        if not line.startswith("timestamp_ms"):          # firmware status text
            self.diag["fw_message"] = line[:120]
            self.event(f"ESP32: {line[:120]}", "warn" if "ERROR" in line.upper() else "info")
        return None

    def _housekeeping(self):
        c = self.conn_state()
        if self._last_conn != c:
            if self._last_conn in ("ok",) and c == "stale":
                self.event("Sensor data stopped arriving (stale)", "bad")
            elif self._last_conn in ("stale", "waiting") and c == "ok":
                self.event("Sensor data flowing", "ok")
            self._last_conn = c
        if time.time() - self._last_save > 30:
            self._last_save = time.time(); self._autosave()

    def feed(self, row):
        with self.lock:
            now = time.time()
            self.seq += 1
            self.raw.append((self.seq, row)); self.buf.append(row); self.n += 1
            self.stamps.append(now); self.last_row_time = now
            if self.capture is not None: self.capture.append(row)
            if len(self.buf) == WIN and self.n % STEP == 0:
                base, model = self.brain()
                if base: self.process(np.array(self.buf), base, model, now)

    # ---------- risk + closed loop (logic unchanged from original) ----------
    def process(self, w, base, model, now):
        while self.ep_times and now - self.ep_times[0] > 60: self.ep_times.popleft()
        score, level, parts, m = risk_score(w, base, len(self.ep_times))
        try: label = str(model.predict([ml_features(w)])[0]) if model else "unknown"
        except Exception: label = "unknown"
        if not self.in_ep and m["flex"] > 25:
            self.in_ep, self.ep_peak, self.ep_cued = True, score, False
            self.ep_times.append(now)
        elif self.in_ep:
            self.ep_peak = max(self.ep_peak, score)
            if not self.ep_cued and score >= CUE_THRESHOLD and now - self.last_cue > 3:
                cmd, why = choose_cue(parts, m)
                ok, _ = self.send_cue(cmd)
                self.cues += 1; self.ep_cued, self.last_cue = True, now
                for r in self.recorders.values(): r.cues += 1
                self.last_cue_info = {"cmd": cmd, "why": why, "risk": round(score), "t": time.strftime("%H:%M:%S"), "sent": ok}
                self.event(f"CUE {cmd} {'sent' if ok else '(not sent, no motors in DEMO)'}: risk {score:.0f}, main cause = {why.replace('_', ' ')}", "warn")
            if m["flex"] < 15:
                if self.pending_pre is not None:
                    ok = self.ep_peak <= self.pending_pre - IMPROVE_MARGIN
                    self.total += 1; self.improved += int(ok)
                    self.last_result = f"{self.pending_pre:.0f} -> {self.ep_peak:.0f}  ({'IMPROVED' if ok else 'not improved'})"
                    self.event("VERIFY: " + self.last_result, "ok" if ok else "bad")
                    self._log_csv("verification_log.csv", ["time", "risk_before", "risk_after", "improved"],
                                  [time.strftime("%H:%M:%S"), round(self.pending_pre, 1), round(self.ep_peak, 1), int(ok)])
                    self.pending_pre = None
                if self.ep_cued: self.pending_pre = self.ep_peak
                self.in_ep = False
        self.risk_hist.append(round(score, 1)); self.flex_hist.append(round(m["flex"], 1))
        pts = risk_breakdown(parts)
        self.status_seq += 1
        self.status = {"seq": self.status_seq, "risk": round(score, 1), "level": level, "movement": label,
                       "flex": round(m["flex"], 1),
                       "contributors": {k: round(v * 100) for k, v in parts.items()},
                       "points": {k: round(v, 1) for k, v in pts.items()},
                       "twist": round(m["twist_peak"]), "jerk": round(m["jerk"], 1), "load": round(m["load"]),
                       "asym": round(m["asym"]), "reps": len(self.ep_times), "t": now}
        for r in self.recorders.values(): r.add(now, score, level, m["flex"])
        self._log_csv("session_log.csv", ["time", "mode", "risk", "level", "movement", "flexion_deg"],
                      [time.strftime("%H:%M:%S"), self.mode, self.status["risk"], level, label, self.status["flex"]])

    def _log_csv(self, name, header, row):
        try:
            with open(P("logs", name), "a", newline="") as f:
                if f.tell() == 0: csv.writer(f).writerow(header)
                csv.writer(f).writerow(row)
            self._logwarn = False
        except OSError as e:                               # e.g. file open in Excel on Windows
            if not self._logwarn:
                self._logwarn = True; self.event(f"Cannot write logs/{name}: {e}", "warn")

    # ---------- user monitoring sessions (History) ----------
    def start_session(self, user_id):
        with self.lock:
            if user_id in self.recorders: return False, "A session is already running", self.recorders[user_id]
            if self.mode == "live" and self.conn_state() != "ok":
                return False, "No live sensor data. Connect the ESP32 first.", None
            now = time.time()
            db_id = self.store.create(user_id, self.mode, now) if self.store else 0
            rec = Recorder(db_id, user_id, self.mode, now); self.recorders[user_id] = rec
        return True, "Session started", rec

    def stop_session(self, user_id, reason="Stopped by user"):
        with self.lock:
            rec = self.recorders.pop(user_id, None)
        if not rec: return False, "No session running"
        if self.store: self.store.finish(rec.db_id, time.time(), "completed" if reason.startswith("Stopped") else "ended", rec.summary(), reason)
        return True, "Session saved"

    def _finish_all(self, reason):
        with self.lock:
            recs, self.recorders = list(self.recorders.values()), {}
        for rec in recs:
            if self.store: self.store.finish(rec.db_id, time.time(), "ended", rec.summary(), reason)

    def _autosave(self):
        with self.lock: recs = list(self.recorders.values())
        for rec in recs:
            if self.store:
                try: self.store.update(rec.db_id, time.time(), rec.summary())
                except Exception: pass

    def session_info(self, user_id):
        rec = self.recorders.get(user_id)
        if not rec: return None
        return {"id": rec.db_id, "started": rec.started, "mode": rec.mode, "windows": rec.n,
                "avg_risk": round(rec.risk_sum / rec.n, 1) if rec.n else None,
                "max_risk": round(rec.risk_max, 1) if rec.n else None, "cues": rec.cues}

    def shutdown(self):
        self.stop = True; self._finish_all("Server stopped"); self._close_serial()

    # ---------- guided tasks (calibrate / record / train) ----------
    def start_task(self, fn, *args):
        with self.lock:
            if self.task["name"] != "idle": return False, "Another task is running"
            self.task = {"name": "starting", "text": "Starting...", "remaining": 0}; self.cancel = False
        threading.Thread(target=self._wrap, args=(fn, *args), daemon=True).start()
        return True, "Started"

    def _wrap(self, fn, *args):
        try: fn(*args)
        except Exception as e: self.event(f"Task failed: {e}", "bad")
        finally: self.task = {"name": "idle", "text": "", "remaining": 0}; self.capture = None

    def cancel_task(self):
        self.cancel = True
        return True, "Cancelling..."

    def _countdown(self, name, text, secs):
        end = time.time() + secs
        while time.time() < end and not self.stop:
            if self.cancel: raise RuntimeError("Cancelled")
            self.task = {"name": name, "text": text, "remaining": round(end - time.time(), 1)}
            time.sleep(0.1)

    def _need_live_data(self):
        if self.mode != "live": raise RuntimeError("This needs LIVE mode with the vest connected")
        if self.n == 0 or self.conn_state() != "ok": raise RuntimeError("No sensor data. Connect the vest first")

    def _check_capture(self, rows, secs, what):
        if len(rows) < secs * FS * 0.8:
            raise RuntimeError(f"Sensor data was interrupted during '{what}' ({len(rows)} of ~{secs * FS} rows). Try again.")

    def task_calibrate(self, worker):
        if self.mode == "demo":                            # DEMO: instant and synthetic (as original)
            self.demo_base, self.demo_model = build_demo_brain()
            self.event("DEMO baseline rebuilt (simulated data, not saved)", "ok"); return
        self._need_live_data()
        phases = [("Stand UPRIGHT and keep still", 15), ("Bend forward and back slowly (5x)", 20),
                  ("Twist left and right slowly (5x)", 20), ("Lift a LIGHT box from the floor (3x)", 15)]
        chunks = []
        for text, sec in phases:
            self._countdown("calibrate", f"Get ready: {text}", 3)
            self.capture = []
            self._countdown("calibrate", f"GO: {text}", sec)
            rows = self.capture; self.capture = None
            self._check_capture(rows, sec, text)
            chunks.append(np.array(rows))
        base = compute_baseline(chunks, worker)
        if base["jerk_ref"] < 0.05:
            raise RuntimeError("The vest barely moved during calibration (or the IMU is not responding). Nothing was saved. Repeat it and follow each prompt.")
        with open(P("models", "baseline.json"), "w") as f: json.dump(base, f, indent=2)
        save_csv(P("data", "calibration", f"{worker}_{time.strftime('%Y%m%d-%H%M%S')}.csv"), np.vstack(chunks))
        self.base = base
        self.event(f"Calibration complete for {worker}", "ok")

    def task_record(self, label, seconds):
        self._need_live_data()
        self._countdown("record", f"Get ready to do: {label}", 3)
        self.capture = []
        self._countdown("record", f"RECORDING '{label}' - do the movement!", seconds)
        rows = self.capture; self.capture = None
        self._check_capture(rows, seconds, label)
        path = P("data", "labeled", f"{label}__{time.strftime('%Y%m%d-%H%M%S')}.csv")
        save_csv(path, np.array(rows))
        self.event(f"Saved {len(rows)} rows to data/labeled/{os.path.basename(path)}", "ok")

    def task_train(self):
        if self.mode == "demo": raise RuntimeError("Training needs LIVE mode and your recorded data. DEMO data is never used for training.")
        self.task = {"name": "train", "text": "Training model...", "remaining": 0}
        lines = []
        try:
            self.model = trainer.train(log=lines.append)
            self.event("Model trained and loaded", "ok")
        finally:
            self.report = "\n".join(lines)

    # ---------- data for the dashboard ----------
    def counts(self):
        def n(sub): return len([f for f in os.listdir(P(*sub)) if f.endswith(".csv")]) if os.path.isdir(P(*sub)) else 0
        return {"labeled_files": n(("data", "labeled")), "calibration_files": n(("data", "calibration"))}

    def model_info(self):
        base, model = self.brain()
        meta = getattr(model, "ergovest_meta", None)
        if model and not meta and os.path.exists(P("models", "movement_model_meta.json")) and self.mode == "live":
            try: meta = json.load(open(P("models", "movement_model_meta.json")))
            except Exception: meta = None
        return {"has_baseline": bool(base), "has_model": bool(model),
                "worker": (base or {}).get("worker", "-"),
                "synthetic": self.mode == "demo",
                "preparing": self.mode == "demo" and base is None,
                "meta": meta}

    def _common(self):
        age = self.age()
        rate = 0.0
        if len(self.stamps) > 5 and self.stamps[-1] > self.stamps[0]:
            rate = (len(self.stamps) - 1) / (self.stamps[-1] - self.stamps[0])
        if self.conn_state() != "ok" and self.mode == "live": rate = 0.0
        return {"mode": self.mode, "port": self.port, "connection": self.conn_state(),
                "age": None if age is None else round(age, 1), "last_row_time": self.last_row_time or None,
                "rate": round(rate, 1), "epoch": self.epoch, "seq": self.seq, "task": self.task,
                "status": self.status, "stale_after": STALE_SECONDS,
                "cues": self.cues, "improved": self.improved, "total": self.total,
                "last_result": self.last_result, "last_cue": self.last_cue_info,
                "cue_threshold": CUE_THRESHOLD, "event_seq": self.event_seq, "diag": self.diag,
                "model": self.model_info(), "server_time": time.time()}

    def snapshot(self):
        """Full state (first frame for every new browser)."""
        with self.lock:
            s = self._common()
            s.update({"type": "full", "cols": COLS, "rows": [row_json(r) for _, r in self.raw],
                      "risk_hist": list(self.risk_hist), "flex_hist": list(self.flex_hist),
                      "events": list(self.events), "report": self.report, "labels": LABELS,
                      "counts": self.counts(), "levels": LEVELS})
            return json_safe(s)

    def frame(self, cursor, ev_seq):
        """Small update: only rows newer than `cursor`."""
        with self.lock:
            s = self._common(); s["type"] = "tick"
            s["rows"] = [row_json(r) for q, r in self.raw if q > cursor]
            if ev_seq != self.event_seq: s["events"] = list(self.events)
            return json_safe(s)
