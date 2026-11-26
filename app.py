#!/usr/bin/env python3
"""
Adaptive ErgoVest - web application.

    python app.py                    DEMO mode (simulated data, no hardware)
    python app.py --port COM3        connect to the ESP32 immediately (LIVE)
    python app.py --local            only this computer can open it (default: LAN, so phones can)
"""
import argparse, threading, webbrowser

from werkzeug.serving import make_server

import config, netinfo
from db import DB
from engine import Engine
from webapp import create_app


def main(argv=None):
    ap = argparse.ArgumentParser(description="Adaptive ErgoVest")
    ap.add_argument("--port", help="serial port of the vest, e.g. COM3 (omit for DEMO mode)")
    ap.add_argument("--http-port", type=int, default=config.HTTP_PORT)
    ap.add_argument("--host", default=config.HOST, help="0.0.0.0 = reachable on your Wi-Fi, 127.0.0.1 = this computer only")
    ap.add_argument("--local", action="store_true", help="same as --host 127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    host = "127.0.0.1" if a.local else a.host
    config.HOST, config.HTTP_PORT = host, a.http_port

    db = DB(config.DB_PATH)
    eng = Engine(store=db, allow_any_port=config.ALLOW_ANY_PORT)
    app = create_app(eng, db)
    app.extensions["ergovest"]["serve_port"] = a.http_port
    if a.port:
        ok, msg = eng.connect(a.port); print(("  " if ok else "  ! ") + msg)
    eng.start_background()

    srv = make_server(host, a.http_port, app, threaded=True)
    print("\n  Adaptive ErgoVest is running.  (Ctrl+C to stop)")
    print(f"  This computer : http://localhost:{a.http_port}")
    if host == "0.0.0.0":
        for ip in netinfo.lan_ips(): print(f"  Phone (same Wi-Fi): http://{ip}:{a.http_port}")
        print("  Reachable on your local network only. Do not port-forward it to the internet.")
    else:
        print("  Local-only mode: phones cannot connect.")
    print(f"  Accounts: {'first visit creates the administrator' if db.count_users() == 0 else str(db.count_users()) + ' account(s) exist'}\n")
    if not a.no_browser: threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{a.http_port}")).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
    finally: eng.shutdown()


if __name__ == "__main__":
    main()
