"""Starts the main app + both microservices together.  Usage:  python run_all.py"""
import os, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
jobs = [("notification_service", "notification_service/app.py"),
        ("payment_service", "payment_service/app.py"),
        ("main_app", "app.py")]
procs = []
try:
    for name, path in jobs:
        print(f"Starting {name} ...")
        procs.append(subprocess.Popen([sys.executable, os.path.join(HERE, path)], cwd=HERE))
        time.sleep(1.2)
    print("\nMain app      : http://127.0.0.1:5000\nPayment svc   : http://127.0.0.1:5001\nNotification  : http://127.0.0.1:5002\nPress Ctrl+C to stop all.")
    for p in procs:
        p.wait()
except KeyboardInterrupt:
    pass
finally:
    for p in procs:
        p.terminate()
