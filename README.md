# Adaptive ErgoVest: web application

Wearable posture monitoring: ESP32 vest, live risk score, haptic cues, secure login, and the same live data on your laptop and your phone.

```
ESP32 --USB serial--> Python backend (one reader) --live stream--> laptop browser
                                                              \--> phone browser (same Wi-Fi)
```

Only the Python backend talks to the ESP32. Browsers never touch the COM port.

---

## 1. Quick start (Windows + PyCharm)

**Easiest:** double-click `run_app.bat`. The first run creates a private environment and installs the packages (needs internet once), then starts the app and opens your browser.

**PyCharm:**
1. Install Python 3.10 or newer from python.org (tick *Add Python to PATH*).
2. PyCharm: *File, Open*, choose this folder.
3. *File, Settings, Project, Python Interpreter, Add Interpreter, Add Local Interpreter, Virtualenv (New)*. Press OK.
4. Open the terminal at the bottom of PyCharm and run: `pip install -r requirements.txt`
5. Right-click `app.py`, *Run 'app'*. (Parameters such as `--port COM3` go in *Run, Edit Configurations, Parameters*.)
6. Open http://localhost:8000

## 2. First run: create the administrator

The first time, the app sends you to **Set up your administrator**. Choose your own password (10+ characters, with a letter and a number). There are no default passwords anywhere. Do this on the laptop itself: the setup page refuses to work from another device so nobody on the Wi-Fi can claim the admin account first.

* **Administrator** can connect the vest, switch DEMO/LIVE, calibrate, record, train and export logs.
* **Members** (people who use *Create an account*) can watch live data, run their own sessions, see only their own history and use the motor test.
* Forgot a password? On the laptop: `python reset_password.py USERNAME` (there is no email service). To close sign-ups after your demo group has joined, set `ERGOVEST_ALLOW_REGISTRATION=false` in `.env` (copy `.env.example`).

## 3. DEMO mode (no hardware)

Start the app. It begins in DEMO mode: a simulated wearer bends, twists and lifts on a loop of about one minute, passing through LOW, MODERATE, ELEVATED and HIGH RISK. Everything is labelled **DEMO** and the data never mixes with real readings. Sessions recorded in DEMO are marked DEMO in History.

## 4. LIVE mode (real vest)

1. Flash `firmware/adaptive_ergovest_v4_haptics/adaptive_ergovest_v4_haptics.ino` (unchanged from your handoff) with the Arduino IDE.
2. **Close the Arduino Serial Monitor.** Only one program can hold the COM port.
3. Plug the ESP32 in by USB. Open the app, go to **Device**, press **Live**, pick the port (ESP32-type adapters are marked "likely ESP32"), press **Connect**. Or start with `run_app.bat --port COM3`.
4. Within a few seconds the header shows *Connected COM3* and the **Live sensors** page fills with readings.
5. **Calibrate** (Device, step 1, about 80 seconds, wear the vest). Without a calibration the app shows readings but says *Calibrate* instead of a risk score. That is the original design: the risk score is relative to your own calibrated movement.
6. Optional: **Record** each movement type several times (step 2), then **Train** (step 3).

If the data stops, the app says *No data*, dims the numbers, silences the alarm and keeps the last reading visible. Unplugging is detected within a second or two.

## 5. Open it on your phone

1. Laptop and phone on the **same Wi-Fi**.
2. In the app: **Settings, Open on your phone**. It shows the laptop's address (for example `http://192.168.1.5:8000`) and a QR code. The terminal prints it too. To find it yourself: Windows key, type `cmd`, run `ipconfig`, read **IPv4 Address** of your Wi-Fi adapter.
3. Open that address in the phone browser and sign in (register a member account on the phone, or use the admin account).
4. Both screens show the same readings. The Settings page shows how many devices are connected.

**Windows Firewall:** the first start shows a prompt. Tick **Private networks only**, not Public. If you missed it: *Windows Security, Firewall, Allow an app*, and allow Python on Private only. University or guest Wi-Fi often blocks device-to-device traffic; a phone hotspot with the laptop connected to it works well.

**Safety:** this is a development server for a trusted local network. Do not port-forward it, put it on the internet, or leave it running on a public network. Sign-in is required for all data and controls, but the connection is plain HTTP, so use it on a network you trust. Use `run_app.bat --local` to switch phone access off.

## 6. Sound alerts

* Off by default on every page load, on every device. Each device has its own switch (top bar speaker, or Settings).
* Turning it on makes **no** sound.
* It plays only while the risk category is **HIGH RISK**, and stops immediately when risk drops below HIGH, when you switch it off, when data goes stale, when the sensor disconnects or the connection to the app is lost. A single timer ensures sounds never overlap.
* Browsers only allow audio after a tap. On phones, silent mode or a locked screen can still mute it, and the page must stay open.

## 7. Haptic motors

Device, *Haptic motor test*: Left (`L`), Right (`R`), Both (`B`), Fast (`F`), Stop (`S`), exactly the firmware protocol. A green message appears only when the command was written to the serial port. In DEMO or with no vest connected you get an honest "Not sent". The firmware does not acknowledge commands, so "sent" means transmitted, not "motor confirmed". Automatic cues (risk of 50 or more) are separate from the manual test.

## 8. Machine learning: what exists and what you must do

* The risk score uses your original `common.py` formulas and thresholds (LOW, MODERATE, ELEVATED, HIGH RISK at 30, 55, 75). The app also shows how many points each factor contributes; these always add up to the score.
* The movement-type classifier (standing, walking, bend, twist...) needs **your** recordings. None ship with the project, so in LIVE mode before training it shows `unknown` and the dashboard says *Not trained*.
* To get meaningful real-world results: (1) calibrate the wearer, (2) record every movement type, ideally 3 or more times each, (3) train, (4) read the cross-validated accuracy in the training report. That figure is computed on your data at training time. Nothing is invented.
* DEMO uses an in-memory simulated baseline and model. It is never saved and never used for training.

## 9. Tests

```
pip install -r requirements-dev.txt
python -m pytest tests -q
```
15 automated tests, no hardware needed: serial CSV parsing, malformed lines, risk breakdown, password hashing, login/registration/CSRF/rate-limit, protected routes, logout, per-user history separation, two simultaneous clients on one stream, missing models and training files, stale and unplug detection, motor command bytes.

The serial tests use an **emulated ESP32** on a pseudo-terminal (Linux). That checks the code path, not your physical wiring. See section 11 for what you must verify with the real vest.

## 10. Files

**Original files modified**
* `app.py`: replaced by the new web entry point (the original is kept in `original_handoff/app.py`; its Engine logic moved to `engine.py`).
* `common.py`: all original calculations kept. Added `risk_breakdown()`. Added one safety guard in `risk_score` (`max(jerk_ref, 1e-6)`) so a motionless calibration cannot divide by zero.
* `dashboard.html`: superseded by `templates/` and `static/` (original kept in `original_handoff/`).
* `adaptive_ergovest_v4_haptics.ino`: **unchanged**, moved into `firmware/adaptive_ergovest_v4_haptics/` (the Arduino IDE needs a folder with the sketch's name).

**New files**
`engine.py`, `webapp.py`, `auth.py`, `db.py`, `config.py`, `netinfo.py`, `reset_password.py`, `simulator.py` and `trainer.py` (reconstructed, see below), `templates/*`, `static/css/app.css`, `static/js/{app,charts,audio,auth,theme-init}.js`, `tests/*`, `requirements.txt`, `requirements-dev.txt`, `.env.example`, `.gitignore`, `run_app.bat`, `README.md`.

**Created automatically at runtime:** `data/labeled/`, `data/calibration/`, `models/`, `logs/`, `instance/` (database and key).

**Reconstructed modules.** `simulator.py` and `trainer.py` were missing. They were rebuilt from how `app.py` calls them (signatures, file locations, data formats). `simulator.py` follows the firmware's geometry so the unchanged risk engine runs on it. For `trainer.py` the original learning algorithm is unknown; a random forest is used because the original passed `n_trees`. If you still have your original `trainer.py`, drop it in: the interface matches.

## 11. Limitations and what YOU must test on real hardware

I could not test with a physical ESP32. Please verify:
1. The COM port appears in the list and **Connect** works; readings update at about 50 Hz.
2. All 19 values look plausible (flex and FSR respond when you bend or press; temperature and humidity are sensible). Boot messages from the firmware, for example a missing DS18B20, appear in Risk analysis, Event log.
3. Each motor test button makes the right motor vibrate.
4. A 80-second calibration completes and a risk score appears.
5. Unplugging the USB cable shows *Not connected* and silences any sound.
6. Phone access over your Wi-Fi (firewall settings vary).
7. Audio on your phone (silent mode, iOS and Android behave differently).

Known limits:
* Tilt angles are magnitudes (the firmware uses `atan2(|az|,|ay|)`), so left versus right and forward versus backward cannot be told apart. The posture figure is schematic, driven only by the tilt sensor. No camera or computer vision is used.
* Firmware sensor note: Flex 3 is on **GPIO 12**, a boot-strapping pin on the ESP32. If the board fails to boot with the sensor connected, disconnect it while plugging in power. Pins were left as specified.
* Logs: `logs/session_log.csv` grows continuously while risk is computed; delete it between demos if it gets large.
* Passwords are reset from the laptop, not by email.
* One shared vest: mode and connection are global. When an administrator changes mode or connection, running sessions end and are saved with the reason.
* The built-in server is for a small trusted group (a handful of devices), not for the internet.
