"""Smart Campus Offence, Fine & Parking Restriction System  (Flask backend)."""
import io, os, re, uuid, secrets, json, hmac, hashlib, base64, threading, urllib.request, urllib.error
from datetime import datetime, date, time, timedelta
from functools import wraps

import qrcode
from flask import (Flask, render_template, request, redirect, url_for, flash,
                   abort, send_file)
from flask_login import (LoginManager, login_user, logout_user, current_user)
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import func
from dotenv import load_dotenv

# Load .env from the app folder first, then common parent/project folders.
# This supports both layouts:
#   project\smart_campus_clean_booking\.env
#   project\.env
_APP_DIR = os.path.abspath(os.path.dirname(__file__))
_ENV_CANDIDATES = [
    os.path.join(_APP_DIR, ".env"),
    os.path.join(os.path.dirname(_APP_DIR), ".env"),
    os.path.join(os.getcwd(), ".env"),
    os.path.join(os.path.dirname(os.getcwd()), ".env"),
]
for _env_path in dict.fromkeys(_ENV_CANDIDATES):
    if os.path.isfile(_env_path):
        load_dotenv(_env_path, override=False)
        break

from models import (db, Student, Guard, Admin, Vehicle, ParkingSlot, Booking,
                    BookingVerification, Offence, Fine, Payment, Appeal,
                    Notification, FineRate, OFFENCE_TYPES)

BASE = os.path.abspath(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE, "static", "uploads")
QR_DIR = os.path.join(BASE, "static", "qrcodes")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(QR_DIR, exist_ok=True)

app = Flask(__name__)
app.config.update(
    SECRET_KEY="change-this-secret-key-for-your-project",
    SQLALCHEMY_DATABASE_URI="sqlite:///" + os.path.join(BASE, "campus.db"),
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    CAMPUS_UPI_ID=os.environ.get("CAMPUS_UPI_ID", ""),
    CAMPUS_UPI_NAME=os.environ.get("CAMPUS_UPI_NAME", "Smart Campus"),
    # --- microservices (see README) ---
    PAYMENT_SERVICE_URL=os.environ.get("PAYMENT_SERVICE_URL", "http://127.0.0.1:5001"),
    NOTIFICATION_SERVICE_URL=os.environ.get("NOTIFICATION_SERVICE_URL", "http://127.0.0.1:5002"),
    GATEWAY_KEY_ID=os.environ.get("GATEWAY_KEY_ID", "rzp_test_demo1234"),
    GATEWAY_KEY_SECRET=os.environ.get("GATEWAY_KEY_SECRET", "demo_secret_change_me"),
    SERVICE_TOKEN=os.environ.get("SERVICE_TOKEN", "internal-service-token"),
    MAIN_APP_URL=os.environ.get("MAIN_APP_URL", "http://127.0.0.1:5000"),
)
db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message_category = "warning"

ROLE_MODELS = {"student": Student, "guard": Guard, "admin": Admin}


@login_manager.user_loader
def load_user(uid):
    role, _, num = uid.partition("-")
    model = ROLE_MODELS.get(role)
    return db.session.get(model, int(num)) if model and num.isdigit() else None


# ----------------------------------------------------------------- helpers
def role_required(role):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **k):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            if current_user.role != role:
                abort(403)
            return f(*a, **k)
        return wrapper
    return deco


def home_for(role):
    return url_for(f"{role}_dashboard")


def _post_json(url, payload, headers, timeout=3):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _gateway_headers():
    token = base64.b64encode(f"{app.config['GATEWAY_KEY_ID']}:{app.config['GATEWAY_KEY_SECRET']}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _get_json(url, headers, timeout=3):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return json.loads(r.read().decode())


def notify(role, user_id, message):
    """Save the in-app notification AND forward it to the Notification microservice."""
    db.session.add(Notification(role=role, user_id=user_id, message=message))
    url, token = app.config["NOTIFICATION_SERVICE_URL"], app.config["SERVICE_TOKEN"]
    payload = {"event": "main-app.notification", "channel": "in-app", "to": f"{role}:{user_id}",
               "message": message, "source": "main-app"}

    def _send():
        try:
            _post_json(url + "/api/notify", payload, {"X-Service-Token": token})
        except Exception:
            pass          # the main app keeps working even if the microservice is down
    threading.Thread(target=_send, daemon=True).start()


def norm_vn(text):
    return re.sub(r"[^A-Za-z0-9]", "", text or "").upper()


def save_photo(file):
    """Resize + save an uploaded image as JPEG (Pillow). Returns file name or None."""
    if not file or not file.filename:
        return None
    try:
        img = Image.open(file.stream).convert("RGB")
    except Exception:
        return None
    img.thumbnail((1280, 1280))
    name = uuid.uuid4().hex + ".jpg"
    img.save(os.path.join(UPLOAD_DIR, name), "JPEG", quality=85)
    return name


def parse_window(args):
    """Read date/start/end from the URL; fall back to sensible defaults."""
    try:
        d = datetime.strptime(args.get("date", ""), "%Y-%m-%d").date()
    except ValueError:
        d = date.today()
    try:
        st = datetime.strptime(args.get("start", ""), "%H:%M").time()
    except ValueError:
        st = time(9, 0)
    try:
        et = datetime.strptime(args.get("end", ""), "%H:%M").time()
    except ValueError:
        et = time(17, 0)
    return d, st, et


def slot_status(slot, d, st, et):
    """Available / Booked / Occupied / Reserved for a date + time window."""
    if slot.is_reserved:
        return "Reserved"
    b = Booking.query.filter(
        Booking.slot_id == slot.id, Booking.date == d,
        Booking.status.in_(["Booked", "Occupied"]),
        Booking.start_time < et, Booking.end_time > st).first()
    if not b:
        return "Available"
    return "Occupied" if b.status == "Occupied" else "Booked"


def blocking_fine(student, booking_date):
    """THE SPECIAL FEATURE: an approved, unpaid fine blocks bookings on any
    day AFTER the day the fine was issued. Returns the Fine or None."""
    fines = Fine.query.filter_by(student_id=student.id, status="Pending") \
        .order_by(Fine.created_at).all()
    for f in fines:
        if booking_date > f.created_at.date():
            return f
    return None


def pending_fines(student):
    return Fine.query.filter_by(student_id=student.id, status="Pending").all()


def make_qr(booking, token):
    payload = f"SCPS|{booking.booking_code}|{token}"
    fname = f"{booking.booking_code}.png"
    qrcode.make(payload).save(os.path.join(QR_DIR, fname))
    return fname


def create_booking(student, vehicle, slot, d, st, et):
    code = "BK" + d.strftime("%y%m%d") + secrets.token_hex(2).upper()
    while Booking.query.filter_by(booking_code=code).first():
        code = "BK" + d.strftime("%y%m%d") + secrets.token_hex(2).upper()
    b = Booking(booking_code=code, student_id=student.id, vehicle_id=vehicle.id,
                slot_id=slot.id, date=d, start_time=st, end_time=et)
    db.session.add(b)
    db.session.flush()
    token = secrets.token_urlsafe(8)
    db.session.add(BookingVerification(booking_id=b.id, token=token,
                                       qr_file=make_qr(b, token)))
    return b


@app.context_processor
def inject_globals():
    unread = 0
    if current_user.is_authenticated:
        unread = Notification.query.filter_by(
            role=current_user.role, user_id=current_user.id, is_read=False).count()
    return dict(unread=unread, today=date.today())


@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", msg="You do not have access to this page."), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", msg="Page not found."), 404


# ------------------------------------------------------------ authentication
@app.route("/", methods=["GET", "POST"])
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(home_for(current_user.role))
    if request.method == "POST":
        role = request.form.get("role", "student")
        model = ROLE_MODELS.get(role, Student)
        user = model.query.filter_by(email=request.form.get("email", "").strip().lower()).first()
        if user and user.check_password(request.form.get("password", "")):
            login_user(user)
            return redirect(home_for(user.role))
        flash("Wrong email, password or role. Check the details and try again.", "danger")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form
        email = f.get("email", "").strip().lower()
        roll = f.get("roll_no", "").strip().upper()
        if not email.endswith("@srmist.edu.in") or email.count("@") != 1:
            flash("Use your official SRMIST email address ending with @srmist.edu.in.", "danger")
        elif len(f.get("password", "")) < 6:
            flash("Password must be at least 6 characters.", "danger")
        elif Student.query.filter((Student.email == email) | (Student.roll_no == roll)).first():
            flash("A student with this email or roll number already exists.", "danger")
        else:
            s = Student(name=f["name"].strip(), roll_no=roll, email=email, phone=f.get("phone"))
            s.set_password(f["password"])
            db.session.add(s)
            db.session.commit()
            flash("Account created. Please sign in.", "success")
            return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/logout")
def logout():
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("login"))


@app.route("/notifications")
def notifications():
    if not current_user.is_authenticated:
        return login_manager.unauthorized()
    q = Notification.query.filter_by(role=current_user.role, user_id=current_user.id)
    items = q.order_by(Notification.created_at.desc()).limit(60).all()
    q.update({"is_read": True})
    db.session.commit()
    return render_template("notifications.html", items=items)


# ================================================================== STUDENT
@app.route("/student/dashboard")
@role_required("student")
def student_dashboard():
    s = current_user
    fines = pending_fines(s)
    bookings = Booking.query.filter_by(student_id=s.id).order_by(Booking.date.desc(), Booking.start_time.desc()).limit(5).all()
    stats = dict(
        vehicles=len(s.vehicles),
        active=Booking.query.filter(Booking.student_id == s.id, Booking.status.in_(["Booked", "Occupied"]), Booking.date >= date.today()).count(),
        offences=Offence.query.filter(Offence.student_id == s.id, Offence.status == "Approved").count(),
        pending_amount=sum(f.amount for f in fines))
    restricted = bool(fines)
    return render_template("student/dashboard.html", fines=fines, bookings=bookings, stats=stats, restricted=restricted)


@app.route("/student/vehicles", methods=["GET", "POST"])
@role_required("student")
def student_vehicles():
    if request.method == "POST":
        number = norm_vn(request.form.get("number"))
        vtype = request.form.get("vtype")
        if len(number) < 6 or vtype not in ("Car", "Bike"):
            flash("Enter a valid vehicle number and choose Car or Bike.", "danger")
        elif Vehicle.query.filter_by(number=number).first():
            flash("This vehicle number is already registered.", "danger")
        else:
            db.session.add(Vehicle(student_id=current_user.id, number=number, vtype=vtype,
                                   model=request.form.get("model", "").strip()))
            db.session.commit()
            flash("Vehicle added.", "success")
        return redirect(url_for("student_vehicles"))
    return render_template("student/vehicles.html", vehicles=current_user.vehicles)


@app.route("/student/vehicles/<int:vid>/delete", methods=["POST"])
@role_required("student")
def student_vehicle_delete(vid):
    v = db.get_or_404(Vehicle, vid)
    if v.student_id != current_user.id:
        abort(403)
    if Booking.query.filter_by(vehicle_id=v.id).first() or Offence.query.filter_by(vehicle_number=v.number).first():
        flash("This vehicle has bookings or offence records, so it cannot be removed.", "warning")
    else:
        db.session.delete(v)
        db.session.commit()
        flash("Vehicle removed.", "success")
    return redirect(url_for("student_vehicles"))


@app.route("/student/parking")
@role_required("student")
def student_parking():
    d, st, et = parse_window(request.args)
    vehicles = current_user.vehicles
    vid = request.args.get("vehicle_id", type=int) or (vehicles[0].id if vehicles else None)
    vehicle = next((v for v in vehicles if v.id == vid), None)
    slots = ParkingSlot.query.filter_by(is_active=True).order_by(ParkingSlot.code).all()
    statuses = {s.id: slot_status(s, d, st, et) for s in slots}
    blocked = blocking_fine(current_user, d)
    return render_template("student/parking.html", vehicles=vehicles, vehicle=vehicle, slots=slots,
                           statuses=statuses, d=d, st=st, et=et, blocked=blocked)


@app.route("/student/book", methods=["POST"])
@role_required("student")
def student_book():
    f = request.form
    try:
        d = datetime.strptime(f["date"], "%Y-%m-%d").date()
        st = datetime.strptime(f["start"], "%H:%M").time()
        et = datetime.strptime(f["end"], "%H:%M").time()
    except (KeyError, ValueError):
        flash("Please choose a valid date and time.", "danger")
        return redirect(url_for("student_parking"))
    back = redirect(url_for("student_parking", vehicle_id=f.get("vehicle_id"), date=f["date"], start=f["start"], end=f["end"]))
    vehicle = db.session.get(Vehicle, int(f.get("vehicle_id") or 0))
    slot = db.session.get(ParkingSlot, int(f.get("slot_id") or 0))
    if not vehicle or vehicle.student_id != current_user.id:
        flash("Choose one of your registered vehicles (add one first).", "danger"); return back
    if not slot or not slot.is_active:
        flash("Please click an available slot first.", "danger"); return back
    if et <= st:
        flash("End time must be after start time.", "danger"); return back
    if datetime.combine(d, et) <= datetime.now():
        flash("That time has already passed.", "danger"); return back
    fine = blocking_fine(current_user, d)
    if fine:
        flash(f"Booking blocked: you have an unpaid fine of ₹{fine.amount:.0f} ({fine.reason}). Pay it to restore booking access.", "danger")
        return redirect(url_for("student_offences"))
    if slot.vtype != vehicle.vtype or slot.is_reserved:
        flash(f"A {vehicle.vtype} cannot use slot {slot.code}.", "danger"); return back
    if slot_status(slot, d, st, et) != "Available":
        flash(f"Slot {slot.code} is already taken for that time. Pick another slot.", "danger"); return back
    clash = Booking.query.filter(Booking.vehicle_id == vehicle.id, Booking.date == d,
                                 Booking.status.in_(["Booked", "Occupied"]),
                                 Booking.start_time < et, Booking.end_time > st).first()
    if clash:
        flash("This vehicle already has a booking in that time window.", "danger"); return back
    b = create_booking(current_user, vehicle, slot, d, st, et)
    notify("student", current_user.id, f"Booking {b.booking_code} confirmed for slot {slot.code} on {d:%d %b}.")
    db.session.commit()
    flash("Parking booked! Show this QR code to the security guard.", "success")
    return redirect(url_for("student_booking_detail", bid=b.id))


@app.route("/student/bookings")
@role_required("student")
def student_bookings():
    items = Booking.query.filter_by(student_id=current_user.id).order_by(Booking.date.desc(), Booking.start_time.desc()).all()
    return render_template("student/bookings.html", items=items)


@app.route("/student/bookings/<int:bid>")
@role_required("student")
def student_booking_detail(bid):
    b = db.get_or_404(Booking, bid)
    if b.student_id != current_user.id:
        abort(403)
    return render_template("student/booking_detail.html", b=b)


@app.route("/student/bookings/<int:bid>/cancel", methods=["POST"])
@role_required("student")
def student_booking_cancel(bid):
    b = db.get_or_404(Booking, bid)
    if b.student_id != current_user.id:
        abort(403)
    if b.status == "Booked":
        b.status = "Cancelled"
        db.session.commit()
        flash("Booking cancelled. The slot is free again.", "success")
    else:
        flash("Only bookings in 'Booked' status can be cancelled.", "warning")
    return redirect(url_for("student_bookings"))


@app.route("/student/offences")
@role_required("student")
def student_offences():
    offences = Offence.query.filter_by(student_id=current_user.id).order_by(Offence.created_at.desc()).all()
    fines = Fine.query.filter_by(student_id=current_user.id).order_by(Fine.created_at.desc()).all()
    return render_template("student/offences.html", offences=offences, fines=fines,
                           pending_total=sum(f.amount for f in fines if f.status == "Pending"))


@app.route("/student/appeal/<int:oid>", methods=["GET", "POST"])
@role_required("student")
def student_appeal(oid):
    o = db.get_or_404(Offence, oid)
    if o.student_id != current_user.id or o.status != "Approved":
        abort(403)
    if Appeal.query.filter_by(offence_id=o.id, student_id=current_user.id).first():
        flash("You have already appealed this offence.", "warning")
        return redirect(url_for("student_offences"))
    if request.method == "POST":
        msg = request.form.get("message", "").strip()
        if len(msg) < 10:
            flash("Please explain your appeal in at least 10 characters.", "danger")
        else:
            db.session.add(Appeal(offence_id=o.id, student_id=current_user.id, message=msg))
            for a in Admin.query.all():
                notify("admin", a.id, f"New appeal from {current_user.name} for offence #{o.id}.")
            db.session.commit()
            flash("Appeal submitted. The admin will review it.", "success")
            return redirect(url_for("student_offences"))
    return render_template("student/appeal.html", o=o)


# ------------------------------------------------ UPI QR payment + admin verification
@app.route("/student/pay/<int:fid>")
@role_required("student")
def student_pay(fid):
    fine = db.get_or_404(Fine, fid)
    if fine.student_id != current_user.id:
        abort(403)
    if fine.status != "Pending":
        flash("This fine is not payable.", "info")
        return redirect(url_for("student_offences"))
    existing = Payment.query.filter_by(fine_id=fine.id, student_id=current_user.id,
                                       status="Pending").order_by(Payment.created_at.desc()).first()
    upi_id = app.config.get("CAMPUS_UPI_ID", "").strip()
    qr_file = None
    upi_link = None
    if upi_id and "@" in upi_id:
        from urllib.parse import urlencode
        upi_link = "upi://pay?" + urlencode({"pa": upi_id, "pn": app.config["CAMPUS_UPI_NAME"],
                                              "am": f"{fine.amount:.2f}", "cu": "INR",
                                              "tn": f"Campus fine {fine.id}"})
        qr_file = f"fine_{fine.id}.png"
        qrcode.make(upi_link).save(os.path.join(QR_DIR, qr_file))
    return render_template("student/pay.html", fine=fine, payment=existing,
                           upi_id=upi_id, upi_link=upi_link, qr_file=qr_file)


@app.route("/student/pay/<int:fid>/process", methods=["POST"])
@role_required("student")
def student_pay_process(fid):
    fine = db.get_or_404(Fine, fid)
    if fine.student_id != current_user.id or fine.status != "Pending":
        abort(403)
    upi_id = app.config.get("CAMPUS_UPI_ID", "").strip()
    if not upi_id or "@" not in upi_id:
        flash("UPI QR is not configured yet. Ask the administrator to set CAMPUS_UPI_ID.", "danger")
        return redirect(url_for("student_pay", fid=fine.id))
    utr = re.sub(r"\s+", "", request.form.get("utr", "")).upper()
    if not re.fullmatch(r"[A-Z0-9-]{8,30}", utr):
        flash("Enter the UPI UTR / transaction reference shown in your payment app (8–30 letters or digits).", "danger")
        return redirect(url_for("student_pay", fid=fine.id))
    duplicate = Payment.query.filter_by(txn_id=utr).first()
    if duplicate:
        flash("This UTR has already been submitted. Check the payment status.", "danger")
        return redirect(url_for("student_pay", fid=fine.id))
    existing = Payment.query.filter_by(fine_id=fine.id, student_id=current_user.id,
                                       status="Pending").first()
    if existing:
        existing.txn_id = utr
        existing.created_at = datetime.now()
    else:
        existing = Payment(fine_id=fine.id, student_id=current_user.id,
                           order_id=f"FINE-{fine.id}", txn_id=utr, method="UPI QR",
                           amount=fine.amount, status="Pending")
        db.session.add(existing)
    for admin in Admin.query.all():
        notify("admin", admin.id, f"UPI payment proof submitted by {current_user.name} for fine #{fine.id}. Verify UTR {utr}.")
    db.session.commit()
    flash("Payment reference submitted. Fine remains pending until admin verifies the payment in the bank/UPI account.", "success")
    return redirect(url_for("student_offences"))


@app.route("/student/pay/<int:fid>/start", methods=["POST"])
@role_required("student")
def student_pay_start(fid):
    """Calls the PAYMENT MICROSERVICE (REST) to create an order, then sends the
    browser to that service's hosted checkout page (fake Razorpay-style QR / card)."""
    fine = db.get_or_404(Fine, fid)
    if fine.student_id != current_user.id or fine.status != "Pending":
        abort(403)
    base = app.config["PAYMENT_SERVICE_URL"]
    payload = {"amount": int(round(fine.amount * 100)), "currency": "INR",
               "receipt": f"fine_{fine.id}", "description": fine.reason or f"Campus fine #{fine.id}",
               "customer": {"name": current_user.name, "email": current_user.email},
               "callback_url": f"{app.config['MAIN_APP_URL']}/student/pay/{fine.id}/callback"}
    try:
        order = _post_json(base + "/api/orders", payload, _gateway_headers(), timeout=5)
    except Exception:
        app.logger.exception("Payment service call failed")
        flash("Payment Service (microservice 1) is not running. Start it with: python payment_service/app.py", "danger")
        return redirect(url_for("student_pay", fid=fine.id))
    # forget abandoned gateway attempts, then record the new pending payment
    for old in Payment.query.filter_by(fine_id=fine.id, student_id=current_user.id, status="Pending").all():
        if (old.method or "").startswith("Gateway"):
            db.session.delete(old)
    db.session.add(Payment(fine_id=fine.id, student_id=current_user.id, order_id=order["id"],
                           method="Gateway (pending)", amount=fine.amount, status="Pending"))
    db.session.commit()
    return redirect(base + order["checkout_path"])


@app.route("/student/pay/<int:fid>/callback", methods=["POST"])
@role_required("student")
def student_pay_callback(fid):
    """The Payment microservice posts the signed result back here."""
    fine = db.get_or_404(Fine, fid)
    if fine.student_id != current_user.id:
        abort(403)
    order_id = request.form.get("order_id", "")
    payment_id = request.form.get("payment_id", "")
    signature = request.form.get("signature", "")
    if request.form.get("status") != "paid":
        flash("Payment cancelled. Your fine is still pending.", "warning")
        return redirect(url_for("student_offences"))
    if fine.status != "Pending":
        return redirect(url_for("student_offences"))
    pay = Payment.query.filter_by(order_id=order_id, fine_id=fine.id, student_id=current_user.id,
                                  status="Pending").first()
    secret = app.config["GATEWAY_KEY_SECRET"]
    expected = hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    if not pay or not payment_id or not hmac.compare_digest(expected, signature):
        flash("Payment signature check failed. Fine is still pending.", "danger")
        return redirect(url_for("student_offences"))
    # second check: ask the Payment microservice directly (server-to-server)
    try:
        o = _get_json(f"{app.config['PAYMENT_SERVICE_URL']}/api/orders/{order_id}", _gateway_headers())
    except Exception:
        flash("Could not reach Payment Service to confirm. Fine is still pending.", "danger")
        return redirect(url_for("student_offences"))
    if o.get("status") != "paid" or o.get("payment_id") != payment_id or o.get("amount") != int(round(fine.amount * 100)):
        flash("Payment could not be confirmed by the Payment Service.", "danger")
        return redirect(url_for("student_offences"))
    pay.txn_id, pay.method, pay.status = payment_id, "Gateway: " + (o.get("method") or "Demo"), "Success"
    fine.status, fine.paid_at = "Paid", datetime.now()
    notify("student", current_user.id, f"Your payment of ₹{fine.amount:.0f} for fine #{fine.id} was successful.")
    for admin in Admin.query.all():
        notify("admin", admin.id, f"Online payment received: ₹{fine.amount:.0f} for fine #{fine.id} by {current_user.name}.")
    if not pending_fines(current_user):
        notify("student", current_user.id, "All pending fines are cleared. Parking booking is available again.")
    db.session.commit()
    flash("Payment successful! Your fine is cleared.", "success")
    return redirect(url_for("student_receipt", pid=pay.id))


@app.route("/student/receipt/<int:pid>")
@role_required("student")
def student_receipt(pid):
    p = db.get_or_404(Payment, pid)
    if p.student_id != current_user.id or p.status != "Success":
        abort(403)
    return render_template("student/receipt.html", p=p)


def _font(size):
    for name in ("arial.ttf", "DejaVuSans.ttf", "Helvetica.ttc", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


@app.route("/student/receipt/<int:pid>/download")
@role_required("student")
def student_receipt_download(pid):
    """Draws the receipt as a PNG image with Pillow."""
    p = db.get_or_404(Payment, pid)
    if p.student_id != current_user.id or p.status != "Success":
        abort(403)
    img = Image.new("RGB", (800, 900), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 800, 110], fill="#14202e")
    d.text((40, 30), "Smart Campus Parking & Fines", font=_font(32), fill="white")
    d.text((40, 72), "PAYMENT RECEIPT (DEMO)", font=_font(20), fill="#f2b705")
    rows = [("Receipt No", f"RCPT-{p.id:05d}"), ("Transaction ID", p.txn_id), ("Order ID", p.order_id or "-"),
            ("Date", p.created_at.strftime("%d %b %Y, %H:%M")), ("Student", p.student.name),
            ("Roll No", p.student.roll_no), ("Fine For", p.fine.reason or "-"),
            ("Vehicle", p.fine.offence.vehicle_number), ("Method", p.method), ("Status", "PAID")]
    y = 150
    for k, v in rows:
        d.text((40, y), k, font=_font(22), fill="#6b7280")
        d.text((300, y), str(v)[:38], font=_font(22), fill="#111827")
        d.line([40, y + 38, 760, y + 38], fill="#e5e7eb")
        y += 55
    d.rectangle([40, y + 10, 760, y + 100], fill="#eef3ff")
    d.text((60, y + 38), f"Amount Paid:  Rs. {p.amount:.2f}", font=_font(34), fill="#1457c4")
    d.text((40, 820), "DEMO PAYMENT - no real money was charged.", font=_font(20), fill="#dc2626")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", as_attachment=True, download_name=f"receipt_{p.txn_id}.png")


# ==================================================================== GUARD
@app.route("/guard/dashboard")
@role_required("guard")
def guard_dashboard():
    today_b = Booking.query.filter_by(date=date.today()).order_by(Booking.start_time).all()
    parked = Booking.query.filter_by(status="Occupied").order_by(Booking.created_at.desc()).all()
    mine = Offence.query.filter_by(guard_id=current_user.id).order_by(Offence.created_at.desc()).limit(8).all()
    stats = dict(today=len(today_b), occupied=len(parked),
                 reports=Offence.query.filter_by(guard_id=current_user.id).count(),
                 pending=Offence.query.filter_by(guard_id=current_user.id, status="Pending").count())
    return render_template("guard/dashboard.html", bookings=today_b, parked=parked, mine=mine, stats=stats)


@app.route("/guard/slots")
@role_required("guard")
def guard_slots():
    args = request.args
    if "start" not in args:
        n = datetime.now()
        args = {"date": date.today().isoformat(), "start": n.strftime("%H:%M"),
                "end": min(n + timedelta(hours=1), n.replace(hour=23, minute=59)).strftime("%H:%M")}
    d, st, et = parse_window(args)
    slots = ParkingSlot.query.filter_by(is_active=True).order_by(ParkingSlot.code).all()
    statuses = {s.id: slot_status(s, d, st, et) for s in slots}
    return render_template("guard/slots.html", slots=slots, statuses=statuses, d=d, st=st, et=et)


@app.route("/guard/verify", methods=["GET", "POST"])
@role_required("guard")
def guard_verify():
    raw = (request.values.get("code") or "").strip()
    result = None
    if raw:
        parts = raw.split("|")
        code = parts[1] if len(parts) == 3 and parts[0] == "SCPS" else raw.upper()
        token = parts[2] if len(parts) == 3 else None
        b = Booking.query.filter_by(booking_code=code).first()
        if not b:
            result = dict(ok=False, msg="No booking found with this code.", b=None)
        elif token and b.verification.token != token:
            result = dict(ok=False, msg="FAKE QR: the security token does not match.", b=b)
        elif b.status == "Cancelled":
            result = dict(ok=False, msg="This booking was cancelled.", b=b)
        elif b.date != date.today():
            result = dict(ok=False, msg=f"Booking is for {b.date:%d %b %Y}, not today.", b=b)
        elif b.status == "Completed":
            result = dict(ok=False, msg="This booking is already completed.", b=b)
        else:
            result = dict(ok=True, msg="Valid booking for today.", b=b)
            notice = blocking_fine(b.student, date.today())
            result["warning"] = "Student has an unpaid fine." if notice else None
    return render_template("guard/verify.html", result=result, raw=raw)


@app.route("/guard/booking/<int:bid>/status", methods=["POST"])
@role_required("guard")
def guard_booking_status(bid):
    b = db.get_or_404(Booking, bid)
    new = request.form.get("status")
    if new == "Occupied" and b.status == "Booked":
        b.status = "Occupied"
        b.verification.verified = True
        b.verification.verified_at = datetime.now()
        b.verification.guard_id = current_user.id
        flash(f"Slot {b.slot.code} marked Occupied.", "success")
    elif new == "Completed" and b.status == "Occupied":
        b.status = "Completed"
        flash(f"Vehicle exited. Slot {b.slot.code} is free.", "success")
    else:
        flash("That status change is not allowed.", "warning")
    db.session.commit()
    return redirect(url_for("guard_verify", code=b.booking_code))


@app.route("/guard/report", methods=["GET", "POST"])
@role_required("guard")
def guard_report():
    if request.method == "POST":
        f = request.form
        vn = norm_vn(f.get("vehicle_number"))
        photo = save_photo(request.files.get("photo_camera")) or save_photo(request.files.get("photo_gallery"))
        if len(vn) < 4 or f.get("offence_type") not in OFFENCE_TYPES or not f.get("description", "").strip():
            flash("Fill in vehicle number, offence type and details.", "danger")
        elif not photo:
            flash("A valid photo is required as evidence (use camera or gallery).", "danger")
        else:
            veh = Vehicle.query.filter_by(number=vn).first()
            # Prefer the student explicitly selected by the guard; otherwise
            # automatically map the vehicle to its registered owner.
            selected_student_id = f.get("student_id", type=int)
            student = db.get_or_404(Student, selected_student_id) if selected_student_id else None
            linked_student_id = student.id if student else (veh.student_id if veh else None)
            if not linked_student_id:
                flash("Select the responsible student, or register this vehicle to a student, before submitting the report.", "danger")
            else:
                linked_student = student if student else veh.student
                o = Offence(offence_type=f["offence_type"], vehicle_number=vn, student_id=linked_student_id,
                            location=f.get("location"), description=f["description"].strip(), photo=photo,
                            guard_id=current_user.id)
                db.session.add(o)
                db.session.flush()
                for a in Admin.query.all():
                    notify("admin", a.id, f"New offence report #{o.id}: {o.offence_type} ({vn}) for {linked_student.name}.")
                db.session.commit()
                flash(f"Report submitted to Admin for approval. Student linked: {linked_student.name}.", "success")
                return redirect(url_for("guard_dashboard"))
    students = Student.query.order_by(Student.name.asc()).all()
    return render_template("guard/report.html", types=OFFENCE_TYPES, students=students)


# ==================================================================== ADMIN
def _sum(q):
    return q.scalar() or 0


@app.route("/admin/dashboard")
@role_required("admin")
def admin_dashboard():
    stats = dict(
        students=Student.query.count(), vehicles=Vehicle.query.count(), bookings=Booking.query.count(),
        offences=Offence.query.count(), fines=Fine.query.count(),
        pending_amount=_sum(db.session.query(func.sum(Fine.amount)).filter(Fine.status == "Pending")),
        collected=_sum(db.session.query(func.sum(Fine.amount)).filter(Fine.status == "Paid")),
        reviews=Offence.query.filter_by(status="Pending").count(),
        appeals=Appeal.query.filter_by(status="Pending").count(),
        restricted=db.session.query(Fine.student_id).filter(Fine.status == "Pending").distinct().count())
    by_type = dict(db.session.query(Offence.offence_type, func.count()).group_by(Offence.offence_type).all())
    by_type = {t: by_type.get(t, 0) for t in OFFENCE_TYPES}
    days = [date.today() - timedelta(days=i) for i in range(6, -1, -1)]
    per_day = [(x.strftime("%d %b"), Booking.query.filter_by(date=x).count()) for x in days]
    slot_counts = dict(db.session.query(ParkingSlot.vtype, func.count()).filter_by(is_active=True).group_by(ParkingSlot.vtype).all())
    # Fetch real bookings created by students; newest bookings appear first.
    latest_bookings = (Booking.query
                       .join(Student, Booking.student_id == Student.id)
                       .order_by(Booking.created_at.desc())
                       .limit(100).all())
    all_students = Student.query.order_by(Student.created_at.desc()).all()
    parked_now = Booking.query.filter_by(status="Occupied").order_by(Booking.created_at.desc()).all()
    recent_payments = Payment.query.order_by(Payment.created_at.desc()).limit(10).all()
    return render_template("admin/dashboard.html", stats=stats, by_type=by_type,
                           per_day=per_day, slot_counts=slot_counts,
                           latest_bookings=latest_bookings, all_students=all_students,
                           parked_now=parked_now, recent_payments=recent_payments)


@app.route("/admin/slots", methods=["GET", "POST"])
@role_required("admin")
def admin_slots():
    if request.method == "POST":
        code = request.form.get("code", "").strip().upper()
        vtype = request.form.get("vtype")
        existing = ParkingSlot.query.filter_by(code=code).first()
        if not code or vtype not in ("Car", "Bike"):
            flash("Enter a slot code and choose Car or Bike.", "danger")
        elif existing and existing.is_active:
            flash("A slot with this code already exists.", "danger")
        else:
            if existing:                                   # revive a soft-deleted slot
                existing.is_active, existing.vtype = True, vtype
                s = existing
            else:
                s = ParkingSlot(code=code, vtype=vtype)
                db.session.add(s)
            s.zone = request.form.get("zone", "A").strip() or "A"
            s.is_reserved = bool(request.form.get("is_reserved"))
            s.note = request.form.get("note", "").strip()
            db.session.commit()
            flash(f"Slot {code} added.", "success")
        return redirect(url_for("admin_slots"))
    slots = ParkingSlot.query.filter_by(is_active=True).order_by(ParkingSlot.vtype, ParkingSlot.code).all()
    return render_template("admin/slots.html", slots=slots)


@app.route("/admin/slots/<int:sid>/edit", methods=["GET", "POST"])
@role_required("admin")
def admin_slot_edit(sid):
    s = db.get_or_404(ParkingSlot, sid)
    if request.method == "POST":
        code = request.form.get("code", "").strip().upper()
        dup = ParkingSlot.query.filter(ParkingSlot.code == code, ParkingSlot.id != s.id).first()
        if not code or request.form.get("vtype") not in ("Car", "Bike") or dup:
            flash("Invalid or duplicate slot code.", "danger")
        else:
            s.code, s.vtype = code, request.form["vtype"]
            s.zone = request.form.get("zone", "A").strip() or "A"
            s.is_reserved = bool(request.form.get("is_reserved"))
            s.note = request.form.get("note", "").strip()
            db.session.commit()
            flash("Slot updated.", "success")
            return redirect(url_for("admin_slots"))
    return render_template("admin/slot_edit.html", s=s)


@app.route("/admin/slots/<int:sid>/delete", methods=["POST"])
@role_required("admin")
def admin_slot_delete(sid):
    s = db.get_or_404(ParkingSlot, sid)
    if Booking.query.filter(Booking.slot_id == s.id, Booking.status.in_(["Booked", "Occupied"]), Booking.date >= date.today()).first():
        flash("This slot has upcoming bookings. Cancel them first.", "warning")
    elif Booking.query.filter_by(slot_id=s.id).first():
        s.is_active = False                                 # keep history, hide slot
        db.session.commit()
        flash(f"Slot {s.code} removed (booking history kept).", "success")
    else:
        db.session.delete(s)
        db.session.commit()
        flash(f"Slot {s.code} deleted.", "success")
    return redirect(url_for("admin_slots"))


@app.route("/admin/rates", methods=["GET", "POST"])
@role_required("admin")
def admin_rates():
    if request.method == "POST":
        for t in OFFENCE_TYPES:
            try:
                amt = float(request.form.get(t, ""))
                if amt >= 0:
                    FineRate.query.filter_by(offence_type=t).update({"amount": amt})
            except ValueError:
                pass
        db.session.commit()
        flash("Fine amounts saved.", "success")
        return redirect(url_for("admin_rates"))
    rates = {r.offence_type: r.amount for r in FineRate.query.all()}
    return render_template("admin/rates.html", types=OFFENCE_TYPES, rates=rates)


@app.route("/admin/offences")
@role_required("admin")
def admin_offences():
    status = request.args.get("status", "")
    q = Offence.query
    if status:
        q = q.filter_by(status=status)
    return render_template("admin/offences.html", items=q.order_by(Offence.created_at.desc()).all(), status=status)


@app.route("/admin/offences/<int:oid>", methods=["GET", "POST"])
@role_required("admin")
def admin_offence_review(oid):
    o = db.get_or_404(Offence, oid)
    rate = FineRate.query.filter_by(offence_type=o.offence_type).first()
    if request.method == "POST":
        if o.status != "Pending":
            flash("This report was already reviewed.", "warning")
            return redirect(url_for("admin_offences"))
        action = request.form.get("action")
        o.admin_note = request.form.get("note", "").strip()
        o.reviewed_at = datetime.now()
        if action == "approve":
            if not o.student_id:
                flash("Cannot approve: link this report to a student first.", "danger")
                return redirect(url_for("admin_offence_review", oid=o.id))
            o.status = "Approved"
            try:
                amt = float(request.form.get("amount") or rate.amount)
                if amt <= 0:
                    raise ValueError()
            except (ValueError, TypeError):
                amt = rate.amount
            db.session.add(Fine(offence_id=o.id, student_id=o.student_id, amount=amt,
                                reason=f"{o.offence_type} ({o.vehicle_number})"))
            notify("student", o.student_id, f"Fine approved: {o.offence_type}. ₹{amt:.0f} added to My Fines. Pay online to restore booking access.")
            flash(f"Approved. ₹{amt:.0f} fine added to the student's My Fines.", "success")
        elif action == "reject":
            o.status = "Rejected"
            flash("Offence rejected.", "info")
        else:
            abort(400)
        if o.guard_id:
            notify("guard", o.guard_id, f"Your report #{o.id} was {o.status.lower()} by admin.")
        db.session.commit()
        return redirect(url_for("admin_offences"))
    return render_template("admin/offence_review.html", o=o, rate=rate)


@app.route("/admin/appeals", methods=["GET", "POST"])
@role_required("admin")
def admin_appeals():
    if request.method == "POST":
        a = db.get_or_404(Appeal, request.form.get("appeal_id", type=int))
        decision = request.form.get("decision")
        if a.status == "Pending" and decision in ("accept", "reject"):
            a.status = "Accepted" if decision == "accept" else "Rejected"
            a.admin_response = request.form.get("response", "").strip()
            a.decided_at = datetime.now()
            if decision == "accept":
                a.offence.status = "Overturned"
                if a.offence.fine and a.offence.fine.status == "Pending":
                    a.offence.fine.status = "Waived"
            notify("student", a.student_id, f"Your appeal for offence #{a.offence_id} was {a.status.lower()}." +
                   (" The fine is waived and booking access restored." if decision == "accept" else ""))
            db.session.commit()
            flash(f"Appeal {a.status.lower()}.", "success")
        return redirect(url_for("admin_appeals"))
    items = Appeal.query.order_by(Appeal.status.desc(), Appeal.created_at.desc()).all()
    return render_template("admin/appeals.html", items=items)


@app.route("/admin/payments", methods=["GET", "POST"])
@role_required("admin")
def admin_payments():
    if request.method == "POST":
        payment = db.get_or_404(Payment, request.form.get("payment_id", type=int))
        action = request.form.get("action")
        if payment.status != "Pending":
            flash("This payment has already been reviewed.", "warning")
        elif action == "approve" and (payment.method or "").startswith("Gateway"):
            flash("Gateway payments are verified automatically by the Payment microservice. Do not approve them manually.", "warning")
        elif action == "approve":
            # Admin must compare the submitted UTR against the actual bank/UPI statement first.
            payment.status = "Success"
            payment.fine.status = "Paid"
            payment.fine.paid_at = datetime.now()
            notify("student", payment.student_id,
                   f"Your UPI payment of ₹{payment.amount:.0f} was verified. Fine #{payment.fine_id} is paid.")
            remaining = Fine.query.filter_by(student_id=payment.student_id, status="Pending").count()
            if remaining == 0:
                notify("student", payment.student_id, "All pending fines are cleared. Future parking bookings are available again.")
            flash("Payment verified and fine marked as paid.", "success")
        elif action == "reject":
            payment.status = "Failed"
            notify("student", payment.student_id,
                   f"Payment reference for fine #{payment.fine_id} could not be verified. Please contact the admin or submit a valid payment.")
            flash("Payment reference rejected. The fine remains pending.", "warning")
        else:
            abort(400)
        db.session.commit()
        return redirect(url_for("admin_payments"))
    items = Payment.query.order_by(Payment.created_at.desc()).all()
    total = sum(p.amount for p in items if p.status == "Success")
    return render_template("admin/payments.html", items=items, total=total)


@app.route("/admin/services")
@role_required("admin")
def admin_services():
    """Shows the microservice architecture + live health of both services."""
    pay_url, note_url = app.config["PAYMENT_SERVICE_URL"], app.config["NOTIFICATION_SERVICE_URL"]
    services = []
    for name, url, port in (("Payment Service", pay_url, 5001), ("Notification Service", note_url, 5002)):
        try:
            h = _get_json(url + "/health", {}, timeout=2)
            services.append(dict(name=name, url=url, port=port, up=h.get("status") == "UP"))
        except Exception:
            services.append(dict(name=name, url=url, port=port, up=False))
    logs, orders = [], []
    if services[1]["up"]:
        try:
            logs = _get_json(note_url + "/api/notifications?limit=12", {"X-Service-Token": app.config["SERVICE_TOKEN"]})["items"]
        except Exception:
            pass
    if services[0]["up"]:
        try:
            orders = _get_json(pay_url + "/api/orders?limit=8", _gateway_headers())["items"]
        except Exception:
            pass
    return render_template("admin/services.html", services=services, logs=logs, orders=orders,
                           pay_url=pay_url, note_url=note_url)


@app.route("/admin/restricted")
@role_required("admin")
def admin_restricted():
    rows = []
    for sid, in db.session.query(Fine.student_id).filter(Fine.status == "Pending").distinct():
        s = db.session.get(Student, sid)
        fs = pending_fines(s)
        appeal = Appeal.query.filter(Appeal.student_id == sid, Appeal.status == "Pending").first()
        rows.append(dict(s=s, fines=fs, total=sum(f.amount for f in fs), since=min(f.created_at for f in fs), appeal=appeal))
    return render_template("admin/restricted.html", rows=rows)


# ================================================================ SAMPLE DATA
def seed():
    """Create only system accounts and parking setup. No fake students,
    sample bookings, offences, fines, or payments are inserted."""
    if not Admin.query.first():
        a = Admin(name="Campus Admin", email="admin@campus.edu")
        a.set_password("admin123")
        db.session.add(a)
    if not Guard.query.first():
        g = Guard(name="Campus Security", email="guard@campus.edu", gate="Main Gate")
        g.set_password("guard123")
        db.session.add(g)

    # Named campus parking areas. Add missing slots on both fresh and existing
    # databases; old demo C01..C08/B01..B08 slots are hidden but history is kept.
    legacy_codes = [f"{prefix}{i:02d}" for prefix in ("C", "B") for i in range(1, 9)]
    for legacy in ParkingSlot.query.filter(ParkingSlot.code.in_(legacy_codes)).all():
        legacy.is_active = False
    zone_specs = [
        ("BEL", "BEL", 10, False, "BEL Block student car parking"),
        ("JAV", "JAVA", 8, False, "Java Block student car parking"),
        ("FAC", "FACULTY", 10, True, "Faculty reserved parking"),
        ("VIP", "VIP", 5, True, "VIP reserved parking"),
    ]
    for prefix, zone, count, reserved, note in zone_specs:
        for i in range(1, count + 1):
            code = f"{prefix}-C{i:02d}"
            slot = ParkingSlot.query.filter_by(code=code).first()
            if not slot:
                slot = ParkingSlot(code=code, vtype="Car", zone=zone,
                                   is_reserved=reserved, note=note, is_active=True)
                db.session.add(slot)
            else:
                slot.vtype, slot.zone, slot.is_reserved = "Car", zone, reserved
                slot.note, slot.is_active = note, True
    if FineRate.query.count() == 0:
        for offence_type, amount in zip(OFFENCE_TYPES, [200, 300, 500, 400, 1000, 150]):
            db.session.add(FineRate(offence_type=offence_type, amount=amount))
    db.session.commit()


with app.app_context():
    db.create_all()
    seed()

if __name__ == "__main__":
    app.run(debug=True)
