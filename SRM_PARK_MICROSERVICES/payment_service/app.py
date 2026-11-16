"""MICROSERVICE 1 - Payment Service (port 5001)
A tiny Razorpay-style FAKE payment gateway used for the college demo.
 - Own database (payment_service.db), own process, own port.
 - Main app talks to it only through REST APIs (create order / check status).
 - Hosted checkout page with a fake UPI QR + test card form.
 - Result is signed with HMAC-SHA256 (same idea as real Razorpay).
 - On success it calls the Notification Service (service-to-service call).
NO REAL MONEY IS EVER MOVED.
"""
import base64, hashlib, hmac, io, json, os, secrets, sqlite3, threading, urllib.request
from datetime import datetime

import qrcode
from flask import Flask, jsonify, render_template, request, send_file, abort

BASE = os.path.abspath(os.path.dirname(__file__))
DB = os.path.join(BASE, "payment_service.db")

KEY_ID = os.environ.get("GATEWAY_KEY_ID", "rzp_test_demo1234")
KEY_SECRET = os.environ.get("GATEWAY_KEY_SECRET", "demo_secret_change_me")
NOTIFY_URL = os.environ.get("NOTIFICATION_SERVICE_URL", "http://127.0.0.1:5002")
SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "internal-service-token")

app = Flask(__name__, template_folder=os.path.join(BASE, "templates"))


# ------------------------------------------------------------------ database
def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS orders(
            id TEXT PRIMARY KEY, amount INTEGER, currency TEXT, receipt TEXT,
            description TEXT, customer_name TEXT, customer_email TEXT, callback_url TEXT,
            status TEXT DEFAULT 'created', payment_id TEXT, method TEXT, card_last4 TEXT,
            signature TEXT, created_at TEXT, paid_at TEXT)""")


def order_row(order_id):
    with conn() as c:
        return c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()


def sign(order_id, payment_id):
    return hmac.new(KEY_SECRET.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


def api_auth():
    """Basic auth with key_id:key_secret, exactly like the real Razorpay API."""
    h = request.headers.get("Authorization", "")
    if h.startswith("Basic "):
        try:
            k, _, s = base64.b64decode(h[6:]).decode().partition(":")
            if hmac.compare_digest(k, KEY_ID) and hmac.compare_digest(s, KEY_SECRET):
                return True
        except Exception:
            pass
    abort(401)


def call_notification_service(payload):
    """Fire-and-forget call to MICROSERVICE 2. Payment never fails if it is down."""
    def _send():
        try:
            req = urllib.request.Request(NOTIFY_URL + "/api/notify", data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "X-Service-Token": SERVICE_TOKEN}, method="POST")
            urllib.request.urlopen(req, timeout=3).read()
        except Exception as e:
            print("[payment-service] notification service unreachable:", e)
    threading.Thread(target=_send, daemon=True).start()


def public(o, with_signature=False):
    d = {"id": o["id"], "amount": o["amount"], "currency": o["currency"], "receipt": o["receipt"],
         "status": o["status"], "payment_id": o["payment_id"], "method": o["method"],
         "created_at": o["created_at"], "paid_at": o["paid_at"]}
    if with_signature and o["status"] == "paid":
        d["signature"] = o["signature"]
    return d


def mark_paid(o, method, card_last4=None):
    payment_id = "pay_" + secrets.token_hex(7)
    sig = sign(o["id"], payment_id)
    with conn() as c:
        c.execute("UPDATE orders SET status='paid', payment_id=?, method=?, card_last4=?, signature=?, paid_at=? "
                  "WHERE id=? AND status='created'",
                  (payment_id, method, card_last4, sig, datetime.now().isoformat(timespec="seconds"), o["id"]))
    call_notification_service({
        "event": "payment.success", "channel": "email", "to": o["customer_email"] or "student@srmist.edu.in",
        "subject": f"Payment received - {o['receipt']}",
        "message": f"Hi {o['customer_name']}, we received Rs. {o['amount']/100:.2f} for {o['description']}. "
                   f"Payment ID: {payment_id} ({method}).", "source": "payment-service"})
    return order_row(o["id"])


# --------------------------------------------------------------- REST API
@app.get("/health")
def health():
    return {"service": "payment-service", "status": "UP", "port": 5001}


@app.post("/api/orders")
def create_order():
    api_auth()
    d = request.get_json(silent=True) or {}
    try:
        amount = int(d["amount"])                       # in paise, like Razorpay
        assert amount >= 100
    except Exception:
        return {"error": "amount (in paise, minimum 100) is required"}, 400
    oid = "order_" + secrets.token_hex(7)
    cust = d.get("customer", {})
    with conn() as c:
        c.execute("INSERT INTO orders(id,amount,currency,receipt,description,customer_name,customer_email,"
                  "callback_url,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  (oid, amount, d.get("currency", "INR"), d.get("receipt", ""), d.get("description", "Payment"),
                   cust.get("name", ""), cust.get("email", ""), d.get("callback_url", ""),
                   datetime.now().isoformat(timespec="seconds")))
    return {"id": oid, "amount": amount, "currency": "INR", "status": "created",
            "checkout_path": f"/checkout/{oid}"}, 201


@app.get("/api/orders/<oid>")
def get_order(oid):
    api_auth()
    o = order_row(oid)
    if not o:
        return {"error": "order not found"}, 404
    return public(o)


@app.get("/api/orders")
def list_orders():
    api_auth()
    with conn() as c:
        rows = c.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?",
                         (request.args.get("limit", 10, type=int),)).fetchall()
    return {"items": [public(r) for r in rows]}


# ------------------------------------------------- hosted checkout (browser)
@app.get("/checkout/<oid>")
def checkout(oid):
    o = order_row(oid)
    if not o:
        abort(404)
    return render_template("checkout.html", o=o, key_id=KEY_ID)


@app.get("/qr/<oid>.png")
def qr_png(oid):
    """The FAKE QR. It points to the 'mobile pay' page of this service, so a phone on
    the same Wi-Fi can scan it and approve the payment, just like a UPI app would."""
    if not order_row(oid):
        abort(404)
    url = f"{request.host_url}mobile-pay/{oid}"
    buf = io.BytesIO()
    qrcode.make(url, box_size=8, border=2).save(buf, "PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=0)


@app.get("/mobile-pay/<oid>")
def mobile_pay(oid):
    o = order_row(oid)
    if not o:
        abort(404)
    return render_template("mobile_pay.html", o=o)


@app.post("/checkout/<oid>/pay")
def do_pay(oid):
    """Called by the checkout page (UPI-QR simulate button / card form / phone page)."""
    o = order_row(oid)
    if not o:
        abort(404)
    if o["status"] == "paid":
        return public(o, True)
    d = request.get_json(silent=True) or request.form
    method = d.get("method", "upi_qr")
    last4 = None
    if method == "card":
        num = "".join(ch for ch in d.get("card_number", "") if ch.isdigit())
        if len(num) != 16:
            return {"error": "Enter a 16-digit test card number."}, 400
        if num == "4000000000000002":
            call_notification_service({"event": "payment.failed", "channel": "sms",
                                       "to": o["customer_email"] or "student",
                                       "subject": "Payment failed", "source": "payment-service",
                                       "message": f"Your card payment for {o['receipt']} was declined."})
            return {"error": "Card declined (test card 4000 0000 0000 0002)."}, 402
        last4 = num[-4:]
        label = "Card (demo)"
    else:
        label = "UPI QR (demo)"
    o = mark_paid(o, label, last4)
    return public(o, True)


@app.get("/checkout/<oid>/status")
def status(oid):
    """Polled by the desktop checkout page while waiting for the QR to be 'scanned'."""
    o = order_row(oid)
    if not o:
        abort(404)
    return public(o, True)


@app.get("/")
def index():
    with conn() as c:
        rows = c.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 20").fetchall()
    return render_template("index.html", rows=rows)


init_db()
if __name__ == "__main__":
    app.run(port=5001, debug=False)
