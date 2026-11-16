"""MICROSERVICE 2 - Notification Service (port 5002)
Receives events from other services over REST and "sends" email / SMS / in-app
messages (demo: printed to the console and stored in its OWN database).
Callers: Payment Service (payment success/failed) and the main Smart Campus app.
"""
import hmac, os, sqlite3
from datetime import datetime

from flask import Flask, abort, render_template, request

BASE = os.path.abspath(os.path.dirname(__file__))
DB = os.path.join(BASE, "notification_service.db")
SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "internal-service-token")

app = Flask(__name__, template_folder=os.path.join(BASE, "templates"))


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT, channel TEXT, recipient TEXT,
            subject TEXT, message TEXT, source TEXT, status TEXT, created_at TEXT)""")


@app.get("/health")
def health():
    return {"service": "notification-service", "status": "UP", "port": 5002}


@app.post("/api/notify")
def notify():
    if not hmac.compare_digest(request.headers.get("X-Service-Token", ""), SERVICE_TOKEN):
        abort(401)
    d = request.get_json(silent=True) or {}
    if not d.get("message"):
        return {"error": "message is required"}, 400
    channel = d.get("channel", "in-app")
    if channel not in ("email", "sms", "in-app"):
        return {"error": "channel must be email, sms or in-app"}, 400
    with conn() as c:
        cur = c.execute("INSERT INTO notifications(event,channel,recipient,subject,message,source,status,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (d.get("event", "generic"), channel, d.get("to", "-"), d.get("subject", ""),
                         d["message"], d.get("source", "unknown"), "SENT",
                         datetime.now().isoformat(timespec="seconds")))
    print(f"[notification-service] {channel.upper():6} -> {d.get('to','-')}: {d['message']}")
    return {"id": cur.lastrowid, "status": "SENT"}, 201


@app.get("/api/notifications")
def list_notifications():
    if not hmac.compare_digest(request.headers.get("X-Service-Token", ""), SERVICE_TOKEN):
        abort(401)
    with conn() as c:
        rows = c.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT ?",
                         (request.args.get("limit", 15, type=int),)).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/")
def index():
    with conn() as c:
        rows = c.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 50").fetchall()
    return render_template("index.html", rows=rows)


init_db()
if __name__ == "__main__":
    app.run(port=5002, debug=False)
