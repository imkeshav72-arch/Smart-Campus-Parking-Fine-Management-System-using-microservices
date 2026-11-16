"""Database models (one class = one SQLite table)."""
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

OFFENCE_TYPES = ["Wrong Parking", "Double Parking", "Unauthorized Parking",
                 "Reserved Slot Misuse", "Emergency Area Parking", "Parking Overstay"]


class AuthMixin(UserMixin):
    """Shared login helpers for Student, Guard and Admin."""
    def get_id(self):                      # e.g. "student-3"
        return f"{self.role}-{self.id}"

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)


class Student(AuthMixin, db.Model):
    __tablename__ = "students"
    role = "student"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    roll_no = db.Column(db.String(30), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(20))
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)


class Guard(AuthMixin, db.Model):
    __tablename__ = "guards"
    role = "guard"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    gate = db.Column(db.String(40), default="Main Gate")
    password_hash = db.Column(db.String(255), nullable=False)


class Admin(AuthMixin, db.Model):
    __tablename__ = "admins"
    role = "admin"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)


class Vehicle(db.Model):
    __tablename__ = "vehicles"
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("students.id"), nullable=False)
    number = db.Column(db.String(20), unique=True, nullable=False)   # stored like TN21AB1234
    vtype = db.Column(db.String(10), nullable=False)                 # Car / Bike
    model = db.Column(db.String(60))
    student = db.relationship("Student", backref="vehicles")


class ParkingSlot(db.Model):
    __tablename__ = "parking_slots"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(10), unique=True, nullable=False)
    vtype = db.Column(db.String(10), nullable=False)                 # Car / Bike
    zone = db.Column(db.String(20), default="A")
    is_reserved = db.Column(db.Boolean, default=False)
    note = db.Column(db.String(80))
    is_active = db.Column(db.Boolean, default=True)                  # soft delete


class Booking(db.Model):
    __tablename__ = "bookings"
    id = db.Column(db.Integer, primary_key=True)
    booking_code = db.Column(db.String(20), unique=True, nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("students.id"), nullable=False)
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"), nullable=False)
    slot_id = db.Column(db.Integer, db.ForeignKey("parking_slots.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    status = db.Column(db.String(15), default="Booked")   # Booked/Occupied/Completed/Cancelled
    created_at = db.Column(db.DateTime, default=datetime.now)
    student = db.relationship("Student")
    vehicle = db.relationship("Vehicle")
    slot = db.relationship("ParkingSlot")
    verification = db.relationship("BookingVerification", uselist=False, backref="booking")


class BookingVerification(db.Model):
    """The QR code of a booking + proof that a guard verified it."""
    __tablename__ = "booking_verifications"
    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id"), unique=True, nullable=False)
    token = db.Column(db.String(40), nullable=False)
    qr_file = db.Column(db.String(60))
    verified = db.Column(db.Boolean, default=False)
    verified_at = db.Column(db.DateTime)
    guard_id = db.Column(db.Integer, db.ForeignKey("guards.id"))


class Offence(db.Model):
    __tablename__ = "offence_reports"
    id = db.Column(db.Integer, primary_key=True)
    offence_type = db.Column(db.String(40), nullable=False)
    vehicle_number = db.Column(db.String(20), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("students.id"))   # filled if vehicle is registered
    location = db.Column(db.String(80))
    description = db.Column(db.Text)
    photo = db.Column(db.String(80))
    guard_id = db.Column(db.Integer, db.ForeignKey("guards.id"))
    status = db.Column(db.String(15), default="Pending")   # Pending/Approved/Rejected/Overturned
    admin_note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now)
    reviewed_at = db.Column(db.DateTime)
    student = db.relationship("Student")
    guard = db.relationship("Guard")


class Fine(db.Model):
    __tablename__ = "fines"
    id = db.Column(db.Integer, primary_key=True)
    offence_id = db.Column(db.Integer, db.ForeignKey("offence_reports.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("students.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    reason = db.Column(db.String(200))
    status = db.Column(db.String(10), default="Pending")   # Pending/Paid/Waived
    created_at = db.Column(db.DateTime, default=datetime.now)
    paid_at = db.Column(db.DateTime)
    offence = db.relationship("Offence", backref=db.backref("fine", uselist=False))
    student = db.relationship("Student")


class Payment(db.Model):
    __tablename__ = "payments"
    id = db.Column(db.Integer, primary_key=True)
    fine_id = db.Column(db.Integer, db.ForeignKey("fines.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("students.id"), nullable=False)
    order_id = db.Column(db.String(40))
    txn_id = db.Column(db.String(40), unique=True)
    method = db.Column(db.String(20))
    amount = db.Column(db.Float)
    status = db.Column(db.String(10))                      # Success / Failed
    created_at = db.Column(db.DateTime, default=datetime.now)
    fine = db.relationship("Fine")
    student = db.relationship("Student")


class Appeal(db.Model):
    __tablename__ = "appeals"
    id = db.Column(db.Integer, primary_key=True)
    offence_id = db.Column(db.Integer, db.ForeignKey("offence_reports.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("students.id"), nullable=False)
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(10), default="Pending")   # Pending/Accepted/Rejected
    admin_response = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now)
    decided_at = db.Column(db.DateTime)
    offence = db.relationship("Offence", backref="appeals")
    student = db.relationship("Student")


class Notification(db.Model):
    __tablename__ = "notifications"
    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(10), nullable=False)
    user_id = db.Column(db.Integer, nullable=False)
    message = db.Column(db.String(250), nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now)


class FineRate(db.Model):
    """Admin-controlled fine amount for each offence type."""
    __tablename__ = "fine_rates"
    id = db.Column(db.Integer, primary_key=True)
    offence_type = db.Column(db.String(40), unique=True, nullable=False)
    amount = db.Column(db.Float, nullable=False)
