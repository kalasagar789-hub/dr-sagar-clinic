from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
import logging
import hmac
import base64
import csv
from io import BytesIO, StringIO
from email.message import EmailMessage
import os
import re
import json
import secrets
import time
import smtplib
from types import SimpleNamespace
from urllib.parse import quote

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from itsdangerous import BadSignature, URLSafeTimedSerializer
from dotenv import load_dotenv
try:
    from openai import OpenAI, APIError
except ImportError:  # Lets the existing non-AI clinic workflows start before dependency installation.
    OpenAI = None
    APIError = Exception
import qrcode
from sqlalchemy import func, inspect, text, or_
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv(Path(__file__).with_name(".env"))
load_dotenv(Path(__file__).with_name("ai.env"))
app = Flask(__name__)
IS_PRODUCTION = os.getenv("CLINIC_ENV", "development").strip().lower() == "production"
DEFAULT_SECRET = "change-this-clinic-secret"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{Path(__file__).parent / 'clinic.db'}")
# Heroku-style URLs are still common; SQLAlchemy expects the full dialect name.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
if IS_PRODUCTION:
    if os.getenv("SECRET_KEY", DEFAULT_SECRET) == DEFAULT_SECRET:
        raise RuntimeError("SECRET_KEY must be set to a strong private value when CLINIC_ENV=production.")
    if not DATABASE_URL.startswith(("postgresql://", "postgresql+psycopg://")):
        raise RuntimeError("Production requires a managed PostgreSQL DATABASE_URL; SQLite is not permitted.")
    if os.getenv("CLINIC_AUTO_CREATE_SCHEMA", "false").lower() == "true" or os.getenv("CLINIC_BOOTSTRAP_REFERENCE_DATA", "false").lower() == "true":
        raise RuntimeError("Automatic schema creation and demo/reference bootstrapping are not permitted in production.")
    if not PUBLIC_BASE_URL.startswith("https://"):
        raise RuntimeError("PUBLIC_BASE_URL must be the clinic's HTTPS URL in production.")

app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY", DEFAULT_SECRET),
    SQLALCHEMY_DATABASE_URI=DATABASE_URL,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION or os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    PREFERRED_URL_SCHEME="https" if IS_PRODUCTION else "http",
    # Versioned static asset URLs let browsers cache CSS/JavaScript safely.
    # This avoids re-downloading every module's styling on each phone navigation.
    SEND_FILE_MAX_AGE_DEFAULT=31536000 if IS_PRODUCTION else 3600,
)
if IS_PRODUCTION:
    # Trust the HTTPS scheme supplied by exactly one reverse proxy/load balancer.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
db, login_manager = SQLAlchemy(app), LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please sign in to continue."
AUTH_FAILURES = {}
AUTH_MAX_FAILURES = 5
AUTH_LOCK_SECONDS = 15 * 60
RESET_REQUESTS = {}
RESET_WINDOW_SECONDS = 15 * 60
RESET_MAX_REQUESTS = 3

def auth_key(identifier):
    return f"{request.remote_addr or 'unknown'}:{identifier.strip().lower()}"

def authentication_is_locked(identifier):
    record = AUTH_FAILURES.get(auth_key(identifier))
    return bool(record and record["locked_until"] > time.time())

def record_auth_failure(identifier):
    key = auth_key(identifier)
    record = AUTH_FAILURES.get(key, {"count": 0, "locked_until": 0})
    record["count"] += 1
    if record["count"] >= AUTH_MAX_FAILURES:
        record["locked_until"] = time.time() + AUTH_LOCK_SECONDS
        record["count"] = 0
    AUTH_FAILURES[key] = record

def clear_auth_failures(identifier):
    AUTH_FAILURES.pop(auth_key(identifier), None)

def reset_request_allowed(email):
    key = f"{request.remote_addr or 'unknown'}:{email.strip().lower()}"
    now = time.time()
    history = [timestamp for timestamp in RESET_REQUESTS.get(key, []) if now - timestamp < RESET_WINDOW_SECONDS]
    if len(history) >= RESET_MAX_REQUESTS:
        RESET_REQUESTS[key] = history
        return False
    history.append(now)
    RESET_REQUESTS[key] = history
    return True

def send_password_reset_otp(recipient, code):
    """Send a one-time password without logging credentials or the OTP."""
    host = os.getenv("SMTP_HOST", "").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    sender = os.getenv("MAIL_FROM", username).strip()
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError:
        raise RuntimeError("SMTP_PORT must be a number.")
    if not host or not username or not password or "CHANGE_ME" in username or "CHANGE_ME" in password:
        raise RuntimeError("Clinic email settings are incomplete. Contact the clinic administrator.")
    message = EmailMessage()
    message["Subject"] = "Dr. Sagar's Clinic password reset code"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(
        f"Your password reset OTP is: {code}\n\n"
        "It expires in 10 minutes and can be used only once. If you did not request this, contact the clinic administrator immediately.\n\n"
        "Dr. Sagar's Lifestyle Clinic"
    )
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.ehlo(); smtp.starttls(); smtp.ehlo()
        smtp.login(username, password)
        smtp.send_message(message)

def generate_consultation_ai_draft(data):
    """Create an editable clinical-documentation draft; never a medical decision."""
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    # External AI is double opt-in. The built-in clinical drafting rules remain free and local.
    enabled = (
        os.getenv("AI_CLINICAL_DRAFTS_ENABLED", "false").lower() == "true"
        and os.getenv("AI_ALLOW_BILLED_REQUESTS", "false").lower() == "true"
    )
    if not enabled or not key or "PASTE_YOUR" in key:
        return None, "AI clinical drafts are not configured."
    if OpenAI is None:
        return None, "The OpenAI Python package is not installed yet."
    payload = {field: str(data.get(field, ""))[:5000] for field in ("history", "diagnosis", "notes")}
    prompt = (
        "You are a clinical documentation drafting assistant for a clinic. Use ONLY the visit text supplied below.\n"
        "Never diagnose, prescribe medication, recommend a dose, order tests, or state that a patient has a condition. Do not invent facts.\n"
        "Produce an editable, cautious draft for the treating clinician. Return JSON only in this exact shape:\n"
        '{"summary":"brief factual draft", "advice":["general editable instruction", "general editable instruction"], "follow_up":"editable follow-up consideration"}\n'
        "Every output must be reviewed, corrected, and approved by the clinician before use.\n\n"
        "VISIT TEXT:\n"
        f"History: {payload['history']}\n"
        f"Documented diagnosis: {payload['diagnosis']}\n"
        f"Notes: {payload['notes']}"
    )
    try:
        client = OpenAI(api_key=key, timeout=25, max_retries=1)
        response = client.responses.create(model=os.getenv("OPENAI_MODEL", "gpt-5.6-luna"), reasoning={"effort": "low"}, input=prompt)
        draft = json.loads((response.output_text or "").strip())
        summary = str(draft.get("summary", "")).strip()[:2000]
        advice = [str(item).strip()[:500] for item in draft.get("advice", []) if str(item).strip()][:4]
        follow_up = str(draft.get("follow_up", "")).strip()[:500]
        if not summary or not advice or not follow_up:
            raise ValueError("Incomplete AI draft")
        return {"summary": summary, "advice": advice, "follow_up": follow_up}, None
    except (APIError, OSError, ValueError, json.JSONDecodeError) as error:
        app.logger.warning("Consultation AI draft unavailable: %s", error)
        return None, "AI draft is temporarily unavailable. You can continue documenting manually."

def csrf_token():
    """Create one session-bound CSRF token for browser form and fetch requests."""
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token

@app.context_processor
def inject_security_context():
    return {"csrf_token": csrf_token}

@app.before_request
def validate_csrf():
    """Reject cross-site state-changing requests before application handlers run."""
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    expected = session.get("_csrf_token")
    if not expected or not supplied or not hmac.compare_digest(str(expected), str(supplied)):
        abort(403)
    return None

@app.after_request
def add_security_headers(response):
    """Baseline browser protections; kept compatible with the current inline UI scripts."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'",
    )
    if IS_PRODUCTION and request.is_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if current_user.is_authenticated:
        response.headers.setdefault("Cache-Control", "no-store, max-age=0")
    # Navigation changes must become visible immediately after a clinic update.
    if request.path.endswith(("/static/sidebar-active.js", "/static/security.js", "/static/reset-theme.css", "/static/appointments-enhancements.js", "/static/appointment-booking-pro.css")):
        response.headers["Cache-Control"] = "no-cache, max-age=0"
    return response

@app.errorhandler(403)
def forbidden(error):
    return render_template("error.html", code=403, title="Access restricted", message="You do not have permission to open this area."), 403

@app.errorhandler(404)
def not_found(error):
    return render_template("error.html", code=404, title="Page not found", message="The page you requested is unavailable or has moved."), 404

@app.errorhandler(413)
def request_too_large(error):
    return render_template("error.html", code=413, title="File too large", message="Please upload a file smaller than 10 MB."), 413

@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    app.logger.exception("Unhandled application error")
    return render_template("error.html", code=500, title="Something went wrong", message="The request was not completed. Please try again or contact the clinic administrator."), 500

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True)
    phone = db.Column(db.String(20), unique=True)
    password_hash = db.Column(db.String(255))
    role = db.Column(db.String(30), nullable=False, default="patient")
    approved = db.Column(db.Boolean, default=False)
    def set_password(self, password): self.password_hash = generate_password_hash(password)
    def check_password(self, password): return bool(self.password_hash and check_password_hash(self.password_hash, password))

class Patient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True)
    mrn = db.Column(db.String(20), unique=True, nullable=False)
    dob = db.Column(db.Date); gender = db.Column(db.String(20)); blood_group = db.Column(db.String(10))
    address = db.Column(db.String(250)); emergency_contact = db.Column(db.String(100)); allergies = db.Column(db.Text)
    user = db.relationship("User", backref="patient_profile")

class Service(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    category = db.Column(db.String(30), nullable=False); fee = db.Column(db.Float, nullable=False); active = db.Column(db.Boolean, default=True)

class Appointment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False); doctor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    scheduled_at = db.Column(db.DateTime, nullable=False); mode = db.Column(db.String(20), default="In clinic"); status = db.Column(db.String(30), default="Scheduled")
    reason = db.Column(db.Text); consultation_fee = db.Column(db.Float, default=0)
    patient = db.relationship("Patient", backref="appointments"); doctor = db.relationship("User", foreign_keys=[doctor_id])

class AppointmentLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointment.id"), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    action = db.Column(db.String(60), nullable=False)
    reason = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    appointment = db.relationship("Appointment", backref="activity_log")
    actor = db.relationship("User", foreign_keys=[actor_id])

class PasswordResetOtp(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    code_hash = db.Column(db.String(255), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    used_at = db.Column(db.DateTime)
    requested_ip = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    user = db.relationship("User", backref="password_reset_otps")

class Encounter(db.Model):
    id = db.Column(db.Integer, primary_key=True); appointment_id = db.Column(db.Integer, db.ForeignKey("appointment.id"), unique=True)
    history = db.Column(db.Text); diagnosis = db.Column(db.Text); bp = db.Column(db.String(20)); pulse = db.Column(db.String(20)); temperature = db.Column(db.String(20))
    weight = db.Column(db.Float); height = db.Column(db.Float); notes = db.Column(db.Text)
    # One appointment has one clinical encounter.  Explicitly make the reverse
    # relationship scalar; without this SQLAlchemy exposes `appt.encounter` as
    # a list, which prevents the reception/doctor vitals form from saving.
    appointment = db.relationship("Appointment", backref=db.backref("encounter", uselist=False))

class LabOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True); patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False); doctor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    test_name = db.Column(db.String(150), nullable=False); status = db.Column(db.String(30), default="Ordered"); result_value = db.Column(db.String(200)); reference_range = db.Column(db.String(100)); remarks = db.Column(db.Text)
    # `doctor_id` is retained for compatibility with existing clinic.db files.  It
    # records the staff member who created legacy direct orders; clinical display
    # and reports always use the explicit source fields below.
    order_source = db.Column(db.String(30), nullable=False, default="Doctor advised")
    referring_provider_name = db.Column(db.String(120))
    ordered_at = db.Column(db.DateTime, default=datetime.utcnow); completed_at = db.Column(db.DateTime)
    patient = db.relationship("Patient", backref="lab_orders"); doctor = db.relationship("User", foreign_keys=[doctor_id])

    @property
    def is_direct_walk_in(self):
        return self.order_source == "Direct walk-in"

    @property
    def referral_label(self):
        return "Direct laboratory request / self-referred" if self.is_direct_walk_in else (self.referring_provider_name or (self.doctor.name if self.doctor else "Doctor advised"))

def prepare_lab_order_display(order):
    """Keep legacy templates truthful without changing existing foreign keys."""
    if order and order.is_direct_walk_in:
        # This is presentation-only. The audit trail still records the staff user
        # who registered the order, while the report never calls them a doctor.
        order.__dict__["doctor"] = SimpleNamespace(name=order.referral_label)
    return order

def apply_development_schema_updates():
    """Add safe, additive fields to an existing local SQLite clinic database.

    Production databases must receive these changes through a reviewed migration.
    """
    if not DATABASE_URL.startswith("sqlite"):
        return
    columns = {column["name"] for column in inspect(db.engine).get_columns("lab_order")}
    additions = {
        "order_source": "VARCHAR(30) NOT NULL DEFAULT 'Doctor advised'",
        "referring_provider_name": "VARCHAR(120)",
    }
    for name, definition in additions.items():
        if name not in columns:
            db.session.execute(text(f"ALTER TABLE lab_order ADD COLUMN {name} {definition}"))
    # Correct orders created before source tracking was introduced.
    db.session.execute(text("UPDATE lab_order SET order_source = 'Direct walk-in' WHERE remarks LIKE '%walk-in%' OR remarks LIKE '%walk in%'"))
    db.session.commit()

class LabSample(db.Model):
    id = db.Column(db.Integer, primary_key=True); order_id = db.Column(db.Integer, db.ForeignKey("lab_order.id"), unique=True, nullable=False)
    sample_id = db.Column(db.String(60), unique=True); sample_type = db.Column(db.String(60), default="Blood"); container = db.Column(db.String(60), default="Vacutainer")
    condition = db.Column(db.String(60), default="Acceptable"); collected_at = db.Column(db.DateTime); collected_by = db.Column(db.Integer, db.ForeignKey("user.id")); notes = db.Column(db.Text)
    order = db.relationship("LabOrder", backref=db.backref("sample", uselist=False)); collector = db.relationship("User", foreign_keys=[collected_by])

class LabOrderAudit(db.Model):
    id = db.Column(db.Integer, primary_key=True); order_id = db.Column(db.Integer, db.ForeignKey("lab_order.id"), nullable=False); actor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    previous_status = db.Column(db.String(60)); new_status = db.Column(db.String(60)); action = db.Column(db.String(120)); reason = db.Column(db.String(300)); created_at = db.Column(db.DateTime, default=datetime.utcnow)
    order = db.relationship("LabOrder", backref="audit_log"); actor = db.relationship("User", foreign_keys=[actor_id])

class LabTestParameter(db.Model):
    id = db.Column(db.Integer, primary_key=True); test_name = db.Column(db.String(150), nullable=False); name = db.Column(db.String(120), nullable=False); unit = db.Column(db.String(40)); reference_range = db.Column(db.String(80)); display_order = db.Column(db.Integer, default=0)

class LabParameterResult(db.Model):
    id = db.Column(db.Integer, primary_key=True); order_id = db.Column(db.Integer, db.ForeignKey("lab_order.id"), nullable=False); parameter_id = db.Column(db.Integer, db.ForeignKey("lab_test_parameter.id"), nullable=False); value = db.Column(db.String(100)); flag = db.Column(db.String(20)); updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    order = db.relationship("LabOrder", backref="parameter_results"); parameter = db.relationship("LabTestParameter")

class LabInventoryItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(140), nullable=False)
    category = db.Column(db.String(60), nullable=False, default="Consumable")
    sku = db.Column(db.String(50), unique=True)
    quantity = db.Column(db.Float, nullable=False, default=0)
    reorder_level = db.Column(db.Float, nullable=False, default=0)
    unit = db.Column(db.String(30), default="units")
    supplier = db.Column(db.String(120))
    expiry_date = db.Column(db.Date)
    location = db.Column(db.String(80))
    active = db.Column(db.Boolean, default=True)

class LabInventoryLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("lab_inventory_item.id"), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    change = db.Column(db.Float, nullable=False)
    action = db.Column(db.String(40), nullable=False)
    note = db.Column(db.String(250))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    item = db.relationship("LabInventoryItem", backref="logs"); actor = db.relationship("User")

LAB_TEST_ALIASES = {
    "rft": "Renal Function Test", "renal profile": "Renal Function Test", "kft": "Renal Function Test",
    "lft": "Liver Function Test", "liver profile": "Liver Function Test",
    "thyroid function test": "Thyroid Profile", "tft": "Thyroid Profile",
    "fbs": "Fasting Blood Sugar", "ppbs": "Post Prandial Blood Sugar", "rbs": "Random Blood Sugar",
    "lipid panel": "Lipid Profile", "serum electrolytes": "Electrolytes", "urine routine": "Urine Examination",
}

def lab_master_name(test_name):
    cleaned = " ".join((test_name or "").strip().lower().split())
    return LAB_TEST_ALIASES.get(cleaned, test_name)

def lab_parameters_for(test_name):
    master_name = lab_master_name(test_name)
    return LabTestParameter.query.filter_by(test_name=master_name).order_by(LabTestParameter.display_order).all()

class Medicine(db.Model):
    id = db.Column(db.Integer, primary_key=True); name = db.Column(db.String(150), nullable=False); strength = db.Column(db.String(60)); stock = db.Column(db.Integer, default=0); reorder_level = db.Column(db.Integer, default=10); unit_price = db.Column(db.Float, default=0)

class MedicineBatch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    medicine_id = db.Column(db.Integer, db.ForeignKey("medicine.id"), nullable=False)
    batch_number = db.Column(db.String(60), nullable=False)
    expiry_date = db.Column(db.Date)
    received_at = db.Column(db.DateTime, default=datetime.utcnow)
    quantity_received = db.Column(db.Integer, default=0)
    quantity_available = db.Column(db.Integer, default=0)
    purchase_price = db.Column(db.Float, default=0)
    mrp = db.Column(db.Float, default=0)
    gst_percent = db.Column(db.Float, default=0)
    supplier = db.Column(db.String(120))
    rack_location = db.Column(db.String(80))
    medicine = db.relationship("Medicine", backref="batches")

def consume_medicine_batches(medicine, quantity):
    """Consume earliest valid batches first while retaining legacy aggregate stock support."""
    remaining = quantity
    batches = MedicineBatch.query.filter(MedicineBatch.medicine_id == medicine.id, MedicineBatch.quantity_available > 0).order_by(MedicineBatch.expiry_date).all()
    for batch in batches:
        if batch.expiry_date and batch.expiry_date < date.today():
            continue
        used = min(remaining, batch.quantity_available)
        batch.quantity_available -= used
        remaining -= used
        if remaining <= 0:
            break

class Prescription(db.Model):
    id = db.Column(db.Integer, primary_key=True); patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False); doctor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    notes = db.Column(db.Text); created_at = db.Column(db.DateTime, default=datetime.utcnow); dispensed = db.Column(db.Boolean, default=False)
    patient = db.relationship("Patient", backref="prescriptions"); doctor = db.relationship("User", foreign_keys=[doctor_id])

class PrescriptionItem(db.Model):
    id = db.Column(db.Integer, primary_key=True); prescription_id = db.Column(db.Integer, db.ForeignKey("prescription.id"), nullable=False); medicine_id = db.Column(db.Integer, db.ForeignKey("medicine.id"), nullable=False)
    dosage = db.Column(db.String(100)); duration = db.Column(db.String(100)); quantity = db.Column(db.Integer, default=1); instructions = db.Column(db.String(250))
    prescription = db.relationship("Prescription", backref="items"); medicine = db.relationship("Medicine")

class DieticianReferral(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False)
    doctor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointment.id"))
    reason = db.Column(db.Text, nullable=False); restrictions = db.Column(db.Text); notes = db.Column(db.Text)
    urgency = db.Column(db.String(20), default="Routine"); status = db.Column(db.String(30), default="New")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    patient = db.relationship("Patient", backref="dietician_referrals"); doctor = db.relationship("User", foreign_keys=[doctor_id]); appointment = db.relationship("Appointment")

class NutritionAssessment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False)
    dietician_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    height_cm = db.Column(db.Float); weight_kg = db.Column(db.Float); target_weight_kg = db.Column(db.Float); waist_cm = db.Column(db.Float); hip_cm = db.Column(db.Float)
    diet_type = db.Column(db.String(40)); cuisine = db.Column(db.String(80)); preferences = db.Column(db.Text); dislikes = db.Column(db.Text); allergies = db.Column(db.Text)
    lifestyle = db.Column(db.Text); dietary_recall = db.Column(db.Text); calorie_target = db.Column(db.Integer); protein_target = db.Column(db.Integer)
    status = db.Column(db.String(30), default="Draft saved"); updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    patient = db.relationship("Patient", backref="nutrition_assessments"); dietician = db.relationship("User", foreign_keys=[dietician_id])

class FoodMaster(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False); category = db.Column(db.String(60)); cuisine = db.Column(db.String(60)); vegetarian = db.Column(db.Boolean, default=True)
    serving = db.Column(db.String(80)); calories = db.Column(db.Float, default=0); protein = db.Column(db.Float, default=0); carbohydrates = db.Column(db.Float, default=0); fat = db.Column(db.Float, default=0); fibre = db.Column(db.Float, default=0)
    tags = db.Column(db.String(250)); active = db.Column(db.Boolean, default=True)

class DietPlan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False); dietician_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False); referral_id = db.Column(db.Integer, db.ForeignKey("dietician_referral.id"))
    title = db.Column(db.String(150), default="Nutrition plan"); status = db.Column(db.String(30), default="Draft"); version = db.Column(db.Integer, default=1)
    calorie_target = db.Column(db.Integer); protein_target = db.Column(db.Integer); meals_json = db.Column(db.Text, default="[]"); instructions = db.Column(db.Text); signed_at = db.Column(db.DateTime); created_at = db.Column(db.DateTime, default=datetime.utcnow)
    patient = db.relationship("Patient", backref="diet_plans"); dietician = db.relationship("User", foreign_keys=[dietician_id]); referral = db.relationship("DieticianReferral")

class NutritionProgress(db.Model):
    id = db.Column(db.Integer, primary_key=True); patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False); dietician_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    weight_kg = db.Column(db.Float); adherence = db.Column(db.Integer); notes = db.Column(db.Text); created_at = db.Column(db.DateTime, default=datetime.utcnow)
    patient = db.relationship("Patient", backref="nutrition_progress"); dietician = db.relationship("User", foreign_keys=[dietician_id])

class DietPlanMacroOverride(db.Model):
    id = db.Column(db.Integer, primary_key=True); plan_id = db.Column(db.Integer, db.ForeignKey("diet_plan.id"), unique=True, nullable=False)
    carbohydrates = db.Column(db.Float); fats = db.Column(db.Float); hydration = db.Column(db.String(40)); updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    plan = db.relationship("DietPlan", backref="macro_override")

class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True); patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False); category = db.Column(db.String(40), nullable=False); description = db.Column(db.String(200)); amount = db.Column(db.Float, nullable=False); paid = db.Column(db.Boolean, default=True); created_at = db.Column(db.DateTime, default=datetime.utcnow)
    patient = db.relationship("Patient", backref="invoices")

class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    expense_date = db.Column(db.Date, nullable=False, default=date.today, index=True)
    category = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(250), nullable=False)
    vendor = db.Column(db.String(120))
    payment_mode = db.Column(db.String(40), default="Cash")
    amount = db.Column(db.Float, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    creator = db.relationship("User", foreign_keys=[created_by])

class PayrollRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    pay_period = db.Column(db.String(7), nullable=False, index=True)  # YYYY-MM
    base_salary = db.Column(db.Float, nullable=False, default=0)
    allowances = db.Column(db.Float, nullable=False, default=0)
    deductions = db.Column(db.Float, nullable=False, default=0)
    status = db.Column(db.String(30), nullable=False, default="Draft")
    paid_on = db.Column(db.Date)
    note = db.Column(db.String(250))
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    employee = db.relationship("User", foreign_keys=[employee_id])
    creator = db.relationship("User", foreign_keys=[created_by])

    @property
    def net_pay(self):
        return round(max(0, (self.base_salary or 0) + (self.allowances or 0) - (self.deductions or 0)), 2)

class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    mode = db.Column(db.String(40), nullable=False, default="Cash")
    reference = db.Column(db.String(100))
    note = db.Column(db.String(250))
    received_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    invoice = db.relationship("Invoice", backref="payments")
    receiver = db.relationship("User", foreign_keys=[received_by])

def invoice_paid_amount(invoice):
    """Keeps older paid invoices compatible while new payments use a receipt trail."""
    if invoice.payments:
        return round(sum(payment.amount for payment in invoice.payments), 2)
    return round(invoice.amount if invoice.paid else 0, 2)

def invoice_balance(invoice):
    return round(max(0, invoice.amount - invoice_paid_amount(invoice)), 2)

class PharmacySaleLine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=False)
    medicine_id = db.Column(db.Integer, db.ForeignKey("medicine.id"), nullable=False)
    batch_id = db.Column(db.Integer, db.ForeignKey("medicine_batch.id"))
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)
    discount = db.Column(db.Float, default=0)
    invoice = db.relationship("Invoice", backref="pharmacy_lines"); medicine = db.relationship("Medicine"); batch = db.relationship("MedicineBatch")

class ConsultationAudit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointment.id"), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    action = db.Column(db.String(100), nullable=False)
    details = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    appointment = db.relationship("Appointment", backref="consultation_audit")
    actor = db.relationship("User")

class PrescriptionTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    category = db.Column(db.String(80), nullable=False)
    # One medicine per line: medicine name|dose|duration|quantity|instructions
    items_spec = db.Column(db.Text, nullable=False)
    advice = db.Column(db.Text)

@login_manager.user_loader
def load_user(user_id): return db.session.get(User, int(user_id))

def roles(*allowed):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated or current_user.role not in allowed:
                abort(403)
            # A later admin suspension takes effect on the next protected request,
            # not only when the user next signs in.
            if current_user.role != "patient" and not current_user.approved:
                logout_user()
                abort(403)
            return f(*args, **kwargs)
        return wrapped
    return decorator

def patient_for_user(user): return Patient.query.filter_by(user_id=user.id).first()

def whatsapp_web_url(phone, message):
    """Build a WhatsApp Web compose URL without storing patient message content."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10:
        digits = f"91{digits}"  # Clinic is configured for Indian mobile numbers.
    if not digits:
        return None
    return f"https://wa.me/{digits}?text={quote(message)}"

def report_verification_token(order):
    return URLSafeTimedSerializer(app.config["SECRET_KEY"]).dumps({"order_id": order.id}, salt="lab-report-verification")

def report_qr_image(order):
    path = url_for("verify_lab_report", token=report_verification_token(order))
    verification_url = f"{PUBLIC_BASE_URL}{path}" if PUBLIC_BASE_URL else url_for("verify_lab_report", token=report_verification_token(order), _external=True)
    image = qrcode.make(verification_url)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"

def staff_home(user):
    destinations = {"lab": "labs", "dietician": "dietician_workspace", "pharmacy": "pharmacy", "reception": "appointments", "doctor": "dashboard", "admin": "dashboard"}
    return destinations.get(user.role, "dashboard")

@app.route("/")
def index(): return redirect(url_for("dashboard") if current_user.is_authenticated else url_for("login"))

@app.get("/health")
def health_check():
    """Unauthenticated infrastructure health check; never exposes patient data."""
    try:
        db.session.execute(text("SELECT 1"))
    except Exception:
        db.session.rollback()
        return jsonify({"status": "unhealthy"}), 503
    return jsonify({"status": "ok", "environment": "production" if IS_PRODUCTION else "development"})

@app.get("/search")
@login_required
def global_search():
    """Shared, permission-aware search used by every clinic workspace."""
    query = (request.args.get("q") or "").strip()
    if not query:
        return render_template("search_results.html", query="", patients=[], appointments=[], lab_orders=[], prescriptions=[], invoices=[])
    if len(query) > 80:
        abort(400)
    pattern = f"%{query}%"
    if current_user.role == "patient":
        profile = patient_for_user(current_user)
        patients = [profile] if profile else []
        appointments = Appointment.query.filter_by(patient_id=profile.id).order_by(Appointment.scheduled_at.desc()).limit(10).all() if profile else []
        lab_orders = LabOrder.query.filter_by(patient_id=profile.id, status="Finalised").filter(LabOrder.test_name.ilike(pattern)).order_by(LabOrder.ordered_at.desc()).limit(10).all() if profile else []
        prescriptions = Prescription.query.filter_by(patient_id=profile.id).order_by(Prescription.created_at.desc()).limit(10).all() if profile else []
        invoices = Invoice.query.filter_by(patient_id=profile.id).order_by(Invoice.created_at.desc()).limit(10).all() if profile else []
    else:
        patients = Patient.query.join(User).filter(or_(User.name.ilike(pattern), User.phone.ilike(pattern), Patient.mrn.ilike(pattern))).order_by(User.name).limit(15).all()
        patient_ids = [item.id for item in patients]
        appointments = Appointment.query.filter(Appointment.patient_id.in_(patient_ids)).order_by(Appointment.scheduled_at.desc()).limit(10).all() if patient_ids else []
        lab_orders = LabOrder.query.filter(or_(LabOrder.patient_id.in_(patient_ids) if patient_ids else False, LabOrder.test_name.ilike(pattern))).order_by(LabOrder.ordered_at.desc()).limit(10).all()
        prescriptions = Prescription.query.filter(Prescription.patient_id.in_(patient_ids)).order_by(Prescription.created_at.desc()).limit(10).all() if patient_ids else []
        invoices = Invoice.query.filter(Invoice.patient_id.in_(patient_ids)).order_by(Invoice.created_at.desc()).limit(10).all() if patient_ids else []
    return render_template("search_results.html", query=query, patients=patients, appointments=appointments, lab_orders=lab_orders, prescriptions=prescriptions, invoices=invoices)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        identifier, password = request.form["identifier"].strip(), request.form.get("password", "")
        if authentication_is_locked(identifier):
            flash("Too many unsuccessful sign-in attempts. Please try again in 15 minutes.", "warning")
            return render_template("login.html"), 429
        user = User.query.filter((User.email == identifier) | (User.phone == identifier)).first()
        if user and user.role == "patient":
            # A fixed OTP is allowed only for the local demonstration database.
            # A real OTP/SMS provider must be configured before patient access is enabled in production.
            demo_otp = os.getenv("DEMO_PATIENT_OTP", "123456" if not IS_PRODUCTION else "")
            if demo_otp and password == demo_otp:
                session.permanent = True
                login_user(user, remember=False)
                clear_auth_failures(identifier)
                return redirect(url_for("dashboard"))
            if IS_PRODUCTION:
                flash("Patient OTP sign-in is not configured. Please contact the clinic.", "warning")
            else:
                flash("Demo OTP is 123456.", "warning")
            record_auth_failure(identifier)
        elif user and user.check_password(password):
            if not user.approved: flash("Your access is awaiting admin approval.", "warning")
            else:
                session.permanent = True
                login_user(user, remember=False)
                clear_auth_failures(identifier)
                return redirect(url_for(staff_home(user)))
        else:
            record_auth_failure(identifier)
            flash("Invalid login details.", "danger")
    return render_template("login.html")

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return render_template("forgot_password.html", email=email, email_error="Enter a valid registered clinic email address."), 400
        user = User.query.filter(func.lower(User.email) == email, User.role != "patient", User.approved.is_(True)).first()
        if not user:
            return render_template("forgot_password.html", email=email, email_error="This email does not match an approved staff account."), 404
        if not reset_request_allowed(email):
            flash("Too many reset requests. Please wait 15 minutes before trying again.", "warning")
            return redirect(url_for("forgot_password"))
        code = f"{secrets.randbelow(900000) + 100000:06d}"
        try:
            send_password_reset_otp(user.email, code)
        except (OSError, smtplib.SMTPException, RuntimeError) as error:
            app.logger.warning("Password reset email could not be delivered for staff account %s: %s", user.id, error)
            flash("The reset email could not be sent. Please contact the clinic administrator.", "danger")
            return redirect(url_for("forgot_password"))
        PasswordResetOtp.query.filter_by(user_id=user.id, used_at=None).update({PasswordResetOtp.used_at: datetime.utcnow()})
        db.session.add(PasswordResetOtp(user_id=user.id, code_hash=generate_password_hash(code), expires_at=datetime.utcnow() + timedelta(minutes=10), requested_ip=request.remote_addr))
        db.session.commit()
        session["password_reset_user_id"] = user.id
        flash("A six-digit code was sent to your registered clinic email. It expires in 10 minutes.", "success")
        return redirect(url_for("reset_password"))
    return render_template("forgot_password.html")

@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    user_id = session.get("password_reset_user_id")
    user = db.session.get(User, user_id) if user_id else None
    if not user or user.role == "patient" or not user.approved:
        flash("Start the password reset process again from your registered clinic email.", "warning")
        return redirect(url_for("forgot_password"))
    if request.method == "POST":
        code = (request.form.get("otp") or "").strip()
        password = request.form.get("password") or ""
        confirm_password = request.form.get("confirm_password") or ""
        otp = PasswordResetOtp.query.filter_by(user_id=user.id, used_at=None).order_by(PasswordResetOtp.created_at.desc()).first()
        if not otp or otp.expires_at < datetime.utcnow() or otp.attempts >= 5 or not check_password_hash(otp.code_hash, code):
            if otp and otp.used_at is None:
                otp.attempts += 1
                if otp.attempts >= 5 or otp.expires_at < datetime.utcnow(): otp.used_at = datetime.utcnow()
                db.session.commit()
            flash("The reset code is invalid, expired, or has already been used.", "danger")
            return redirect(url_for("reset_password"))
        if password != confirm_password or len(password) < 12 or not re.search(r"[A-Z]", password) or not re.search(r"[a-z]", password) or not re.search(r"\d", password) or not re.search(r"[^A-Za-z0-9]", password):
            flash("Use a matching password with at least 12 characters, uppercase, lowercase, number and symbol.", "warning")
            return redirect(url_for("reset_password"))
        user.set_password(password)
        otp.used_at = datetime.utcnow()
        db.session.commit()
        session.pop("password_reset_user_id", None)
        clear_auth_failures(user.email or "")
        flash("Password updated. Sign in with your new password.", "success")
        return redirect(url_for("login"))
    return render_template("reset_password.html", email=user.email)

@app.route("/logout")
@login_required
def logout():
    logout_user(); return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    today = date.today(); appts = Appointment.query.filter(func.date(Appointment.scheduled_at) == today).order_by(Appointment.scheduled_at).all()
    if current_user.role == "patient":
        profile = patient_for_user(current_user); appts = Appointment.query.filter_by(patient_id=profile.id).order_by(Appointment.scheduled_at.desc()).all()
        return render_template("dashboard.html", appointments=appts, patient_view=True, profile=profile)
    if current_user.role == "doctor": appts = [a for a in appts if a.doctor_id == current_user.id]
    metrics = {"patients": Patient.query.count(), "appointments": len(appts), "revenue": db.session.query(func.coalesce(func.sum(Invoice.amount), 0)).filter(func.date(Invoice.created_at) == today).scalar(), "staff": User.query.filter(User.role != "patient", User.approved.is_(True)).count(), "waiting": sum(a.status in ("Waiting", "Checked In", "Vitals Pending") for a in appts), "consulting": sum(a.status == "In Consultation" for a in appts), "completed": sum(a.status == "Consulted" for a in appts), "tele": sum(a.mode != "In clinic" for a in appts), "dietician": sum("diet" in (a.reason or "").lower() for a in appts), "pending_labs": LabOrder.query.filter(LabOrder.status != "Completed").count()}
    return render_template("dashboard.html", appointments=appts, metrics=metrics, low_stock=Medicine.query.filter(Medicine.stock <= Medicine.reorder_level).all(), pending_labs=LabOrder.query.filter(LabOrder.status != "Completed").order_by(LabOrder.ordered_at.desc()).limit(4).all(), today=today)

@app.get("/api/clinic-ai-insights")
@login_required
@roles("admin", "reception", "doctor", "lab", "pharmacy", "dietician")
def clinic_ai_insights():
    """Free local operational assistant. It exposes no patient-identifying details."""
    today = date.today()
    appointments = Appointment.query.filter(func.date(Appointment.scheduled_at) == today).all()
    if current_user.role == "doctor":
        appointments = [item for item in appointments if item.doctor_id == current_user.id]
    waiting = sum(item.status in ("Waiting", "Checked In", "Vitals Pending") for item in appointments)
    in_consultation = sum(item.status == "In Consultation" for item in appointments)
    now = datetime.now()
    delayed = sum(item.status in ("Scheduled", "Checked In", "Vitals Pending", "Waiting") and item.scheduled_at < now - timedelta(minutes=15) for item in appointments)
    no_shows = sum(item.status == "No Show" for item in appointments)
    low_stock = Medicine.query.filter(Medicine.stock <= Medicine.reorder_level).order_by(Medicine.stock.asc()).all()
    expiring_batches = MedicineBatch.query.filter(MedicineBatch.quantity_available > 0, MedicineBatch.expiry_date.isnot(None), MedicineBatch.expiry_date <= today + timedelta(days=60), MedicineBatch.expiry_date >= today).order_by(MedicineBatch.expiry_date).all()
    pending_labs = LabOrder.query.filter(LabOrder.status.in_(["Ordered", "Sample Collected", "Draft Saved", "Verification Pending"])).count()
    insights = []
    if waiting:
        insights.append({"tone": "attention", "title": f"{waiting} patient(s) waiting", "detail": "Prioritise vitals, check-in and token flow before the queue grows."})
    if delayed:
        insights.append({"tone": "attention", "title": f"{delayed} appointment(s) may be delayed", "detail": "Review the live queue and update patients or providers as appropriate."})
    if in_consultation:
        insights.append({"tone": "info", "title": f"{in_consultation} consultation(s) active", "detail": "Keep reception informed if the next scheduled slot may be delayed."})
    if pending_labs:
        insights.append({"tone": "warning", "title": f"{pending_labs} laboratory item(s) need action", "detail": "Review collection, result entry and verification queues."})
    if low_stock:
        names = ", ".join(item.name for item in low_stock[:3])
        extra = "" if len(low_stock) <= 3 else f" and {len(low_stock) - 3} more"
        insights.append({"tone": "warning", "title": f"{len(low_stock)} medicine(s) at reorder level", "detail": f"Review stock for {names}{extra}."})
    if expiring_batches:
        names = ", ".join(f"{item.medicine.name} ({item.expiry_date.strftime('%b %Y')})" for item in expiring_batches[:2])
        insights.append({"tone": "warning", "title": f"{len(expiring_batches)} medicine batch(es) expire within 60 days", "detail": f"Review FEFO dispensing and procurement for {names}."})
    if no_shows:
        insights.append({"tone": "info", "title": f"{no_shows} no-show appointment(s) today", "detail": "Use the appointment list to review follow-up and reminder actions."})
    if not insights:
        insights.append({"tone": "success", "title": "Clinic workflow looks clear", "detail": "No waiting patients, active laboratory alerts or medicine reorder alerts were found."})
    return jsonify({"ok": True, "mode": "Free local operations assistant", "disclaimer": "Workflow suggestions only. Staff remain responsible for clinical and operational decisions.", "insights": insights[:4]})

@app.get("/patient-portal")
@login_required
@roles("patient")
def patient_portal():
    profile = patient_for_user(current_user) or abort(404)
    appointments = Appointment.query.filter_by(patient_id=profile.id).order_by(Appointment.scheduled_at.desc()).all()
    prescriptions = Prescription.query.filter_by(patient_id=profile.id).order_by(Prescription.created_at.desc()).all()
    labs = LabOrder.query.filter_by(patient_id=profile.id, status="Finalised").order_by(LabOrder.completed_at.desc()).all()
    invoices = Invoice.query.filter_by(patient_id=profile.id).order_by(Invoice.created_at.desc()).all()
    plans = DietPlan.query.filter_by(patient_id=profile.id).order_by(DietPlan.created_at.desc()).all()
    return render_template("patient_portal.html", profile=profile, appointments=appointments, prescriptions=prescriptions, labs=labs, invoices=invoices, plans=plans, now=datetime.utcnow())

@app.route("/appointments", methods=["GET", "POST"])
@login_required
@roles("admin", "reception", "doctor")
def appointments():
    if request.method == "POST":
        booking_type = request.form.get("booking_type", "existing")
        if booking_type == "lab_only":
            patient_source = request.form.get("lab_patient_source", "new")
            if patient_source == "new":
                name = request.form.get("lab_patient_name", "").strip()
                phone = re.sub(r"\D", "", request.form.get("lab_patient_phone", ""))
                tests = [test.strip() for test in request.form.getlist("lab_test_name") if test.strip()]
                if not name or not re.fullmatch(r"\d{10}", phone) or not tests:
                    flash("Patient name, a valid 10-digit mobile number, and at least one lab test are required.", "danger")
                    return redirect(url_for("appointments") + "#new-appointment")
                existing_user = User.query.filter_by(phone=phone).first()
                if existing_user:
                    profile = patient_for_user(existing_user)
                    if not profile:
                        flash("This mobile number belongs to a staff account. Use a different mobile number.", "danger")
                        return redirect(url_for("appointments") + "#new-appointment")
                else:
                    existing_user = User(name=name, phone=phone, email=request.form.get("lab_patient_email") or None, role="patient", approved=True)
                    db.session.add(existing_user); db.session.flush()
                    dob_text = request.form.get("lab_patient_dob")
                    profile = Patient(
                        user_id=existing_user.id, mrn=f"CLN-{Patient.query.count() + 1:04d}",
                        gender=request.form.get("lab_patient_gender"),
                        dob=datetime.strptime(dob_text, "%Y-%m-%d").date() if dob_text else None,
                        blood_group=request.form.get("lab_patient_blood_group") or None,
                        address=request.form.get("lab_patient_address"),
                        emergency_contact=request.form.get("lab_patient_emergency"),
                        allergies=request.form.get("lab_patient_notes"),
                    )
                    db.session.add(profile); db.session.flush()
            else:
                try:
                    profile = db.session.get(Patient, int(request.form.get("lab_existing_patient_id", "")))
                except (TypeError, ValueError):
                    profile = None
                tests = [test.strip() for test in request.form.getlist("lab_test_name") if test.strip()]
                if not profile or not tests:
                    flash("Select a registered patient and at least one laboratory test.", "danger")
                    return redirect(url_for("appointments") + "#new-appointment")

            for test_name in tests:
                order = LabOrder(patient_id=profile.id, doctor_id=current_user.id, test_name=test_name, status="Ordered", order_source="Direct walk-in", remarks="Reception-registered direct laboratory request")
                db.session.add(order); db.session.flush()
                db.session.add(LabOrderAudit(order_id=order.id, actor_id=current_user.id, previous_status="", new_status="Ordered", action="Reception direct walk-in laboratory order created", reason="No referring doctor"))
            db.session.commit()
            flash(f"Walk-in laboratory patient registered. {len(tests)} test(s) sent to the lab queue.", "success")
            return redirect(url_for("appointments") + "#new-appointment")
        if booking_type == "new":
            name = request.form.get("new_patient_name", "").strip()
            phone = re.sub(r"\D", "", request.form.get("new_patient_phone", ""))
            if not name or len(phone) != 10:
                flash("New patient name and a valid 10-digit mobile number are required.", "danger")
                return redirect(url_for("appointments") + "#new-appointment")
            existing_user = User.query.filter_by(phone=phone).first()
            if existing_user:
                profile = patient_for_user(existing_user)
                if not profile:
                    flash("This phone is already used by a staff account. Use a different mobile number.", "danger")
                    return redirect(url_for("appointments") + "#new-appointment")
            else:
                existing_user = User(name=name, phone=phone, email=request.form.get("new_patient_email") or None, role="patient", approved=True)
                db.session.add(existing_user); db.session.flush()
                profile = Patient(user_id=existing_user.id, mrn=f"CLN-{Patient.query.count() + 1:04d}", gender=request.form.get("new_patient_gender"), dob=datetime.strptime(request.form["new_patient_dob"], "%Y-%m-%d").date() if request.form.get("new_patient_dob") else None, blood_group=request.form.get("new_patient_blood_group"), address=request.form.get("new_patient_address"), emergency_contact=request.form.get("new_patient_emergency"), allergies=request.form.get("new_patient_notes"))
                db.session.add(profile); db.session.flush()
            patient_id = profile.id
        else:
            patient_id = int(request.form["patient_id"])
        reason = request.form.get("reason")
        if booking_type == "followup": reason = f"Follow-up ({request.form.get('followup_interval', '30')} days): {reason or 'Clinical review'}"
        appt = Appointment(patient_id=patient_id, doctor_id=int(request.form["doctor_id"]), scheduled_at=datetime.strptime(request.form["scheduled_at"], "%Y-%m-%dT%H:%M"), mode=request.form["mode"], reason=reason, consultation_fee=float(request.form["fee"]))
        db.session.add(appt); db.session.flush()
        db.session.add(AppointmentLog(appointment_id=appt.id, actor_id=current_user.id, action="Appointment registered", reason=f"{appt.mode} · {appt.scheduled_at.strftime('%d %b %Y, %I:%M %p')}"))
        db.session.commit()
        flash(f"Appointment registered for {appt.patient.user.name}. Scheduled for {appt.scheduled_at.strftime('%d %b %Y, %I:%M %p')}. Check in the patient to start vitals.", "success")
        return redirect(url_for("appointments"))
    all_appointments = Appointment.query.order_by(Appointment.scheduled_at.desc()).all()
    selected_status = request.args.get("status", "All")
    visible = all_appointments if selected_status == "All" else [a for a in all_appointments if a.status == selected_status]
    today_appts = [a for a in all_appointments if a.scheduled_at.date() == date.today()]
    metrics = {"today": len(today_appts), "waiting": sum(a.status in ("Waiting", "Checked In", "Vitals Pending") for a in today_appts), "consulting": sum(a.status == "In Consultation" for a in today_appts), "completed": sum(a.status == "Consulted" for a in today_appts), "tele": sum(a.mode != "In clinic" for a in today_appts)}
    providers = User.query.filter(User.role.in_(["doctor", "dietician"]), User.approved.is_(True)).all()
    lab_services = Service.query.filter_by(category="Lab", active=True).order_by(Service.name).all()
    return render_template("appointments.html", appointments=visible, patients=Patient.query.all(), doctors=providers, consultation=Service.query.filter_by(name="Consultation").first(), lab_services=lab_services, metrics=metrics, selected_status=selected_status)

@app.get("/reception/lab-walkin")
@login_required
@roles("admin", "reception")
def reception_lab_walkin():
    return render_template(
        "reception_lab_walkin.html",
        patients=Patient.query.order_by(Patient.mrn).all(),
        lab_services=Service.query.filter_by(category="Lab", active=True).order_by(Service.name).all(),
    )

@app.post("/appointments/<int:appointment_id>/check-in")
@login_required
@roles("admin", "reception")
def check_in(appointment_id):
    appt = db.session.get(Appointment, appointment_id) or abort(404)
    if appt.status not in ("Scheduled", "Booked"):
        flash("Only scheduled appointments can be checked in.", "warning")
    else:
        appt.status = "Vitals Pending"
        db.session.add(AppointmentLog(appointment_id=appt.id, actor_id=current_user.id, action="Patient checked in", reason=f"Token {appt.id} assigned; vitals pending"))
        db.session.commit()
        flash(f"{appt.patient.user.name} checked in. Token: {appt.id}. Record vitals before sending the patient to the doctor.", "success")
    return redirect(url_for("appointments"))

@app.route("/appointments/<int:appointment_id>/vitals", methods=["GET", "POST"])
@login_required
@roles("admin", "reception")
def record_vitals(appointment_id):
    """Reception's short, dedicated clinical-intake step before consultation."""
    appt = db.session.get(Appointment, appointment_id) or abort(404)
    if appt.status in {"Cancelled", "No Show", "Consulted"}:
        flash("Vitals cannot be recorded for this appointment status.", "warning")
        return redirect(url_for("appointments"))
    note = appt.encounter or Encounter(appointment_id=appt.id)
    if request.method == "POST":
        try:
            note.bp = (request.form.get("bp") or "").strip() or None
            note.pulse = (request.form.get("pulse") or "").strip() or None
            note.temperature = (request.form.get("temperature") or "").strip() or None
            note.weight = float(request.form["weight"]) if request.form.get("weight") else None
            note.height = float(request.form["height"]) if request.form.get("height") else None
        except ValueError:
            flash("Weight and height must be valid numbers.", "danger")
            return render_template("record_vitals.html", appt=appt, encounter=note)
        appt.status = "Waiting"
        db.session.add(note)
        db.session.add(AppointmentLog(appointment_id=appt.id, actor_id=current_user.id, action="Reception vitals recorded", reason="Patient moved to doctor waiting queue"))
        db.session.commit()
        flash(f"Vitals saved for {appt.patient.user.name}. The patient is now in the doctor waiting queue.", "success")
        return redirect(url_for("appointments", status="Waiting"))
    return render_template("record_vitals.html", appt=appt, encounter=note)

@app.post("/appointments/<int:appointment_id>/attendance")
@login_required
@roles("admin", "reception", "doctor")
def update_appointment_attendance(appointment_id):
    appointment = db.session.get(Appointment, appointment_id) or abort(404)
    if current_user.role == "doctor" and appointment.doctor_id != current_user.id:
        abort(403)
    action = request.form.get("action")
    reason = (request.form.get("reason") or "").strip()
    if action not in {"cancel", "no_show"}:
        return jsonify({"ok": False, "message": "Unsupported appointment action."}), 400
    if appointment.status in {"Consulted", "Cancelled", "No Show"}:
        return jsonify({"ok": False, "message": "This appointment can no longer be changed."}), 409
    if not reason:
        return jsonify({"ok": False, "message": "A reason is required for this change."}), 400
    previous = appointment.status
    appointment.status = "Cancelled" if action == "cancel" else "No Show"
    db.session.add(AppointmentLog(appointment_id=appointment.id, actor_id=current_user.id, action="Appointment cancelled" if action == "cancel" else "Patient marked no-show", reason=reason))
    db.session.commit()
    return jsonify({"ok": True, "status": appointment.status, "previous_status": previous})

@app.post("/appointments/<int:appointment_id>/reschedule")
@login_required
@roles("admin", "reception", "doctor")
def reschedule_appointment(appointment_id):
    appt = db.session.get(Appointment, appointment_id) or abort(404)
    try:
        new_time = datetime.strptime(request.form["scheduled_at"], "%Y-%m-%dT%H:%M")
    except (KeyError, ValueError):
        return jsonify({"ok": False, "message": "Invalid appointment time."}), 400
    conflict = Appointment.query.filter(Appointment.doctor_id == appt.doctor_id, Appointment.scheduled_at == new_time, Appointment.id != appt.id, Appointment.status != "Cancelled").first()
    if conflict: return jsonify({"ok": False, "message": "That provider slot is already booked."}), 409
    appt.scheduled_at = new_time; db.session.commit()
    return jsonify({"ok": True, "id": appt.id, "scheduled_at": appt.scheduled_at.isoformat()})

@app.get("/appointments/calendar")
@login_required
@roles("admin", "reception", "doctor", "dietician")
def appointment_calendar():
    return render_template("appointment_calendar.html", appointments=Appointment.query.order_by(Appointment.scheduled_at).all())

@app.get("/appointments/token-display")
@login_required
def token_display():
    queue = Appointment.query.filter(Appointment.status.in_(["Checked In", "Waiting", "Vitals Pending", "In Consultation"])).order_by(Appointment.scheduled_at).all()
    return render_template("token_display.html", queue=queue)

@app.get("/print/token/<int:appointment_id>")
@login_required
@roles("admin", "reception", "doctor")
def appointment_token_print(appointment_id):
    appointment = db.session.get(Appointment, appointment_id) or abort(404)
    if current_user.role == "doctor" and appointment.doctor_id != current_user.id:
        abort(403)
    return render_template("print_token.html", appointment=appointment)

@app.get("/print/daily-queue")
@login_required
@roles("admin", "reception", "doctor")
def daily_queue_print():
    target = request.args.get("date")
    try:
        queue_date = datetime.strptime(target, "%Y-%m-%d").date() if target else date.today()
    except ValueError:
        queue_date = date.today()
    appointments = Appointment.query.filter(func.date(Appointment.scheduled_at) == queue_date).order_by(Appointment.scheduled_at).all()
    if current_user.role == "doctor":
        appointments = [item for item in appointments if item.doctor_id == current_user.id]
    return render_template("print_daily_queue.html", appointments=appointments, queue_date=queue_date)

@app.get("/api/appointments/live")
@login_required
def live_appointments():
    queue = Appointment.query.filter(Appointment.status.in_(["Checked In", "Waiting", "Vitals Pending", "In Consultation"])).order_by(Appointment.scheduled_at).all()
    return jsonify({"updated_at": datetime.utcnow().isoformat(), "queue": [{"id": a.id, "token": a.id, "patient": a.patient.user.name, "status": a.status, "time": a.scheduled_at.strftime("%I:%M %p")} for a in queue]})

@app.get("/patient-flow")
@login_required
@roles("admin", "reception", "doctor", "lab", "pharmacy", "dietician")
def patient_flow():
    rows = []
    for appt in Appointment.query.order_by(Appointment.scheduled_at.desc()).limit(50):
        labs = LabOrder.query.filter_by(patient_id=appt.patient_id).order_by(LabOrder.ordered_at.desc()).all()
        prescriptions = Prescription.query.filter_by(patient_id=appt.patient_id).order_by(Prescription.created_at.desc()).all()
        checked_in = appt.status not in ("Scheduled", "Cancelled")
        consulted = appt.status == "Consulted"
        lab_pending = any(l.status != "Completed" for l in labs)
        lab_done = bool(labs) and not lab_pending
        prescription = prescriptions[0] if prescriptions else None
        dispensed = bool(prescription and prescription.dispensed)
        if appt.status == "Cancelled": next_owner, next_step = "—", "Visit cancelled"
        elif not checked_in: next_owner, next_step = "Reception", "Check in and assign token"
        elif not appt.encounter: next_owner, next_step = "Reception", "Record vitals"
        elif not consulted: next_owner, next_step = "Doctor", "Start consultation"
        elif lab_pending: next_owner, next_step = "Laboratory", "Complete ordered tests"
        elif prescription and not dispensed: next_owner, next_step = "Pharmacy", "Dispense prescribed medicines"
        else: next_owner, next_step = "Doctor / Reception", "Schedule follow-up or complete visit"
        rows.append({"appointment": appt, "checked_in": checked_in, "vitals": bool(appt.encounter), "consulted": consulted, "lab_pending": lab_pending, "lab_done": lab_done, "prescription": bool(prescription), "dispensed": dispensed, "next_owner": next_owner, "next_step": next_step})
    return render_template("patient_flow.html", rows=rows)

@app.get("/patients/<int:patient_id>/journey")
@login_required
@roles("admin", "reception", "doctor", "lab", "pharmacy", "dietician")
def patient_journey(patient_id):
    patient = db.session.get(Patient, patient_id) or abort(404)
    patients = Patient.query.order_by(Patient.mrn).all()
    appointments = Appointment.query.filter_by(patient_id=patient.id).order_by(Appointment.scheduled_at.desc()).all()
    latest = appointments[0] if appointments else None
    note = latest.encounter if latest else None
    labs = LabOrder.query.filter_by(patient_id=patient.id).order_by(LabOrder.ordered_at.desc()).all()
    prescriptions = Prescription.query.filter_by(patient_id=patient.id).order_by(Prescription.created_at.desc()).all()
    invoices = Invoice.query.filter_by(patient_id=patient.id).order_by(Invoice.created_at.desc()).all()
    # The overview template always renders a medicines section.  Give a newly
    # registered patient an empty read-only prescription shape rather than
    # passing None, so the overview remains available before the first Rx.
    prescription = prescriptions[0] if prescriptions else SimpleNamespace(items=[], dispensed=False)
    return render_template("patient_journey.html", patients=patients, patient=patient, latest=latest, note=note, labs=labs, prescription=prescription, invoice=invoices[0] if invoices else None)

@app.get("/patients")
@login_required
@roles("admin", "reception", "doctor", "lab", "pharmacy", "dietician")
def patient_overview():
    selected_id = request.args.get("patient_id", type=int)
    patient = db.session.get(Patient, selected_id) if selected_id else Patient.query.order_by(Patient.mrn).first()
    if not patient:
        flash("No patients have been registered yet.", "warning")
        return redirect(url_for("appointments"))
    return redirect(url_for("patient_journey", patient_id=patient.id))

@app.get("/appointments/<int:appointment_id>/whatsapp-reminder")
@login_required
@roles("admin", "reception")
def whatsapp_reminder(appointment_id):
    appt = db.session.get(Appointment, appointment_id) or abort(404)
    phone = re.sub(r"\D", "", appt.patient.user.phone or "")
    if len(phone) != 10:
        flash("This patient does not have a valid mobile number for WhatsApp.", "warning")
        return redirect(url_for("appointments"))
    message = quote(f"CareFlow Clinic reminder: Dear {appt.patient.user.name}, your appointment with {appt.doctor.name} is scheduled for {appt.scheduled_at.strftime('%d %b, %I:%M %p')}. Please arrive 10 minutes early.")
    return redirect(f"https://wa.me/91{phone}?text={message}")

@app.get("/appointments/<int:appointment_id>/whatsapp-followup")
@login_required
@roles("admin", "reception")
def whatsapp_followup_draft(appointment_id):
    """Creates a staff-reviewed reminder draft; sending remains a deliberate WhatsApp action."""
    appt = db.session.get(Appointment, appointment_id) or abort(404)
    phone = re.sub(r"\D", "", appt.patient.user.phone or "")
    if len(phone) != 10:
        flash("This patient does not have a valid mobile number for WhatsApp.", "warning")
        return redirect(url_for("appointments"))
    message = quote(f"Dear {appt.patient.user.name}, we noticed that you could not attend your appointment at Dr. Sagar's Lifestyle Clinic. Please reply or call the clinic if you would like help booking a suitable follow-up appointment.")
    return redirect(f"https://wa.me/91{phone}?text={message}")

@app.route("/encounter/<int:appointment_id>", methods=["GET", "POST"])
@login_required
@roles("doctor", "admin")
def encounter(appointment_id):
    appt = db.session.get(Appointment, appointment_id) or abort(404); note = appt.encounter or Encounter(appointment_id=appt.id)
    if current_user.role == "doctor" and appt.doctor_id != current_user.id: abort(403)
    if request.method == "POST":
        for field in ("history", "diagnosis", "bp", "pulse", "temperature", "notes"): setattr(note, field, request.form.get(field))
        note.weight = float(request.form["weight"]) if request.form.get("weight") else None; note.height = float(request.form["height"]) if request.form.get("height") else None
        appt.status = "In Consultation" if appt.status != "Consulted" else appt.status
        db.session.add_all([note, ConsultationAudit(appointment_id=appt.id, actor_id=current_user.id, action="Clinical draft saved")]); db.session.commit(); flash("Clinical draft saved.", "success")
    queue_query = Appointment.query.filter(Appointment.status.notin_(["Cancelled", "Consulted"])).order_by(Appointment.scheduled_at)
    if current_user.role == "doctor": queue_query = queue_query.filter_by(doctor_id=current_user.id)
    template_data = [{"id": t.id, "name": t.name, "category": t.category, "items": t.items_spec, "advice": t.advice} for t in PrescriptionTemplate.query.order_by(PrescriptionTemplate.category, PrescriptionTemplate.name).all()]
    return render_template("encounter.html", appt=appt, encounter=note, medicines=Medicine.query.order_by(Medicine.name).all(), lab_services=Service.query.filter_by(category="Lab", active=True).order_by(Service.name).all(), prescription_templates=template_data, doctor_queue=queue_query.all(), labs=LabOrder.query.filter_by(patient_id=appt.patient_id).order_by(LabOrder.ordered_at.desc()).all(), prior_prescriptions=Prescription.query.filter_by(patient_id=appt.patient_id).order_by(Prescription.created_at.desc()).all())

@app.get("/api/consultation-queue")
@login_required
@roles("doctor", "admin")
def consultation_queue_data():
    queue = Appointment.query.filter(Appointment.status.notin_(["Cancelled", "Consulted"])).order_by(Appointment.scheduled_at)
    if current_user.role == "doctor": queue = queue.filter_by(doctor_id=current_user.id)
    return jsonify({"queue": [{"id": item.id, "patient": item.patient.user.name, "initial": item.patient.user.name[:1].upper(), "time": item.scheduled_at.strftime("%I:%M %p"), "status": item.status, "doctor": item.doctor.name} for item in queue.all()]})

@app.get("/api/dietician/foods")
@login_required
@roles("dietician", "admin")
def dietician_foods():
    return jsonify({"foods": [{"id": item.id, "name": item.name, "serving": item.serving, "calories": item.calories, "protein": item.protein, "vegetarian": item.vegetarian} for item in FoodMaster.query.filter_by(active=True).order_by(FoodMaster.name).all()]})

@app.post("/api/dietician/plan/<int:plan_id>/macros")
@login_required
@roles("dietician", "admin")
def update_diet_plan_macros(plan_id):
    plan = db.session.get(DietPlan, plan_id) or abort(404)
    if current_user.role == "dietician" and plan.dietician_id != current_user.id: abort(403)
    data = request.get_json(silent=True) or {}
    macro = DietPlanMacroOverride.query.filter_by(plan_id=plan.id).first() or DietPlanMacroOverride(plan_id=plan.id)
    macro.carbohydrates = float(data["carbohydrates"]) if data.get("carbohydrates") is not None else None
    macro.fats = float(data["fats"]) if data.get("fats") is not None else None
    macro.hydration = str(data.get("hydration", ""))[:40] or None
    db.session.add(macro); db.session.commit()
    return jsonify({"ok": True})

@app.post("/api/consultation-assist")
@login_required
@roles("doctor", "admin")
def consultation_assist():
    data = request.get_json(silent=True) or {}
    text = " ".join(str(data.get(key, "")) for key in ("history", "diagnosis", "notes")).lower()
    signals = []
    advice = ["Take medicines exactly as prescribed and do not share medicines.", "Seek urgent care for severe or worsening symptoms."]
    follow_up = "Review in 7–14 days, or earlier if symptoms worsen."
    template_terms = ["General Medicine"]
    if any(word in text for word in ("diabet", "glucose", "hba1c", "sugar")):
        signals.append("diabetes review"); advice = ["Monitor blood glucose as advised and carry readings to the next visit.", "Follow the clinician-approved meal plan, activity plan and hypoglycaemia precautions."]; follow_up = "Suggested follow-up: Diabetes review in 30 days."; template_terms = ["Diabetes"]
    elif any(word in text for word in ("hyperten", "blood pressure", "bp", "htn")):
        signals.append("blood-pressure review"); advice = ["Record home blood-pressure readings if available.", "Limit excess salt and take medicines consistently."]; follow_up = "Suggested follow-up: Blood-pressure review in 15 days."; template_terms = ["Hypertension"]
    elif "thyroid" in text:
        signals.append("thyroid review"); advice = ["Take thyroid medicine exactly as directed, usually on an empty stomach when prescribed.", "Bring thyroid reports to follow-up."]; follow_up = "Suggested follow-up: Thyroid review in 45 days."; template_terms = ["Thyroid"]
    elif any(word in text for word in ("weight", "obesity", "bmi")):
        signals.append("lifestyle / weight review"); advice = ["Follow the agreed diet and activity plan; record weekly weight.", "Discuss any new exercise plan if there are symptoms or comorbidities."]; follow_up = "Suggested follow-up: Lifestyle review in 7 days."; template_terms = ["Lifestyle"]
    summary = "Clinical draft: " + (data.get("diagnosis") or data.get("history") or "Review documented symptoms, examination findings and treatment response.")
    prescription_notes = [
        "Prescription counselling draft — clinician to review and edit.",
        "Explain the purpose and timing of each prescribed medicine in the patient’s preferred language.",
        "Confirm recorded allergies, current medicines, and the patient’s understanding before issuing the prescription.",
        "Advise the patient to seek clinical help for severe, new, or worsening symptoms.",
    ]
    if "diabetes review" in signals:
        prescription_notes.append("Ask the patient to bring clinician-requested glucose readings to follow-up.")
    elif "blood-pressure review" in signals:
        prescription_notes.append("If home monitoring is advised, confirm the patient knows how and when to record blood pressure readings.")
    elif "thyroid review" in signals:
        prescription_notes.append("Confirm that any thyroid-medicine instructions are documented clearly on the final prescription.")
    ai_draft, ai_message = generate_consultation_ai_draft(data)
    provider = "clinic_rules"
    if ai_draft:
        summary = ai_draft["summary"]
        advice = ai_draft["advice"]
        follow_up = ai_draft["follow_up"]
        provider = "openai"
    templates = PrescriptionTemplate.query.filter(PrescriptionTemplate.category.in_(template_terms)).order_by(PrescriptionTemplate.name).all()
    return jsonify({"summary": summary, "advice": advice, "follow_up": follow_up, "prescription_notes": prescription_notes, "signals": signals or ["general clinical review"], "templates": [{"id": item.id, "name": item.name, "category": item.category} for item in templates], "provider": provider, "notice": "AI-generated drafts require clinician review and approval." if ai_draft else ai_message})

@app.post("/api/clinical-knowledge")
@login_required
@roles("doctor", "admin")
def clinical_knowledge():
    """Free local, clinician-only reference prompts; not a diagnostic or prescribing engine."""
    data = request.get_json(silent=True) or {}
    query = " ".join(str(data.get(item, "")) for item in ("history", "diagnosis", "notes")).lower()
    library = {
        "diabetes": [
            ("Structured review prompts", "Document symptoms, available glucose data, treatment adherence, hypoglycaemia history and relevant complications as clinically appropriate."),
            ("Safety check", "Review allergies, concurrent medicines, renal status or other patient-specific factors before finalising any treatment decision."),
            ("Follow-up documentation", "Record the agreed monitoring plan, education provided and the clinician-approved review interval."),
        ],
        "hypertension": [
            ("Structured review prompts", "Confirm blood-pressure measurement context, symptoms, adherence, home readings when available and cardiovascular risk factors."),
            ("Safety check", "Review allergies, concurrent medicines and patient-specific contraindications before making treatment changes."),
            ("Follow-up documentation", "Document the monitoring plan, lifestyle discussion and clinician-approved review interval."),
        ],
        "thyroid": [
            ("Structured review prompts", "Document relevant symptoms, available thyroid results, medicine adherence and the timing of previous tests."),
            ("Safety check", "Check allergies, concurrent medicines and laboratory context before any clinical decision."),
            ("Follow-up documentation", "Record the planned test or review timeline according to the clinician’s judgement and local protocol."),
        ],
        "general": [
            ("Consultation completeness", "Confirm history, examination findings, allergies, current medicines, vital signs, assessment and follow-up are documented."),
            ("Prescription safety", "Review allergy status, duplications, interactions, indication, dose, route, frequency and duration before signing."),
            ("Patient communication", "Use plain language, document counselling and provide clear return precautions suited to the patient’s condition."),
        ],
    }
    topic = "diabetes" if any(word in query for word in ("diabet", "glucose", "hba1c", "sugar")) else "hypertension" if any(word in query for word in ("hyperten", "blood pressure", "bp", "htn")) else "thyroid" if "thyroid" in query else "general"
    return jsonify({"ok": True, "topic": topic.title(), "items": [{"title": title, "detail": detail} for title, detail in library[topic]], "disclaimer": "Local clinical reference prompts only. Use current guidelines, local protocols and professional judgement; this tool does not diagnose or prescribe."})

@app.get("/consultation")
@login_required
@roles("doctor", "admin")
def consultation_workspace():
    queue = Appointment.query.filter(Appointment.status.notin_(["Cancelled", "Consulted"])).order_by(Appointment.scheduled_at)
    if current_user.role == "doctor": queue = queue.filter_by(doctor_id=current_user.id)
    first = queue.first()
    if not first:
        return render_template("consultation_empty.html")
    return redirect(url_for("encounter", appointment_id=first.id))

@app.post("/consultation/<int:appointment_id>/status")
@login_required
@roles("doctor", "admin")
def consultation_status(appointment_id):
    appt = db.session.get(Appointment, appointment_id) or abort(404)
    if current_user.role == "doctor" and appt.doctor_id != current_user.id: abort(403)
    action = request.form.get("action")
    if action == "start": appt.status = "In Consultation"; message = "Consultation started."
    elif action == "complete": appt.status = "Consulted"; message = "Consultation marked completed."
    else: abort(400)
    db.session.add(ConsultationAudit(appointment_id=appt.id, actor_id=current_user.id, action=message)); db.session.commit(); flash(message, "success")
    return redirect(url_for("encounter", appointment_id=appt.id))

@app.post("/appointments/<int:appointment_id>/follow-up")
@login_required
@roles("doctor", "admin")
def create_follow_up(appointment_id):
    appointment = db.session.get(Appointment, appointment_id) or abort(404)
    if current_user.role == "doctor" and appointment.doctor_id != current_user.id: abort(403)
    try:
        days = min(max(int(request.form.get("days") or 30), 1), 365)
    except ValueError:
        return jsonify({"ok": False, "message": "Enter a valid follow-up interval."}), 400
    mode = request.form.get("mode") or "In clinic"
    reason = (request.form.get("reason") or "Clinical follow-up").strip()
    scheduled_at = datetime.combine(date.today() + timedelta(days=days), appointment.scheduled_at.time())
    conflict = Appointment.query.filter(Appointment.doctor_id == appointment.doctor_id, Appointment.scheduled_at == scheduled_at, Appointment.status != "Cancelled").first()
    if conflict:
        return jsonify({"ok": False, "message": "That follow-up time slot is already booked. Select a different interval or reschedule from Reception Desk."}), 409
    follow_up = Appointment(patient_id=appointment.patient_id, doctor_id=appointment.doctor_id, scheduled_at=scheduled_at, mode=mode, reason=f"Follow-up: {reason}", consultation_fee=appointment.consultation_fee, status="Scheduled")
    db.session.add(follow_up); db.session.flush()
    db.session.add(AppointmentLog(appointment_id=appointment.id, actor_id=current_user.id, action="Follow-up appointment created", reason=f"Appointment #{follow_up.id} · {days} days · {mode} · {reason}"))
    db.session.commit()
    return jsonify({"ok": True, "id": follow_up.id, "scheduled_at": follow_up.scheduled_at.strftime("%d %b %Y, %I:%M %p")})

@app.post("/lab-orders")
@login_required
@roles("doctor", "admin")
def create_lab_order():
    order = LabOrder(patient_id=int(request.form["patient_id"]), doctor_id=current_user.id, test_name=request.form["test_name"], status="Ordered", order_source="Doctor advised", referring_provider_name=current_user.name)
    db.session.add(order); db.session.flush(); db.session.add(LabOrderAudit(order_id=order.id, actor_id=current_user.id, previous_status="", new_status="Ordered", action="Doctor order received")); db.session.commit(); flash("Lab test ordered and sent to the laboratory queue.", "success"); return redirect(request.referrer or url_for("dashboard"))

@app.post("/lab-orders/bulk")
@login_required
@roles("doctor", "admin")
def create_lab_orders_bulk():
    tests = [test for test in request.form.getlist("test_name") if test]
    if not tests:
        flash("Select at least one laboratory test.", "warning")
    else:
        for test in tests:
            order = LabOrder(patient_id=int(request.form["patient_id"]), doctor_id=current_user.id, test_name=test, status="Ordered", order_source="Doctor advised", referring_provider_name=current_user.name)
            db.session.add(order); db.session.flush(); db.session.add(LabOrderAudit(order_id=order.id, actor_id=current_user.id, previous_status="", new_status="Ordered", action="Doctor order received"))
        db.session.commit(); flash(f"{len(tests)} test(s) sent to the laboratory queue.", "success")
    return redirect(request.referrer or url_for("dashboard"))

@app.route("/labs", methods=["GET", "POST"])
@login_required
@roles("lab", "admin")
def labs():
    selected_id = request.form.get("order_id", type=int) or request.args.get("order_id", type=int)
    orders = LabOrder.query.order_by(LabOrder.ordered_at.desc()).all()
    selected = db.session.get(LabOrder, selected_id) if selected_id else (orders[0] if orders else None)
    if request.method == "POST":
        action = request.form.get("action", "save_result")
        if action not in {"direct", "direct_new"} and current_user.role not in {"lab", "admin"}: abort(403)
        if action in {"direct", "direct_new"} and current_user.role not in {"lab", "admin", "reception"}: abort(403)
        if action == "direct":
            order = LabOrder(patient_id=int(request.form["patient_id"]), doctor_id=current_user.id, test_name=request.form["test_name"], status="Ordered", order_source="Direct walk-in", remarks="Direct walk-in laboratory request")
            db.session.add(order); db.session.flush(); db.session.add(LabOrderAudit(order_id=order.id, actor_id=current_user.id, previous_status="", new_status="Ordered", action="Direct laboratory order created")); db.session.commit(); flash("Direct laboratory order added to the queue.", "success"); return redirect(url_for("labs", order_id=order.id))
        if action == "direct_new":
            name = request.form.get("walkin_name", "").strip(); phone = re.sub(r"\D", "", request.form.get("walkin_phone", "")); gender = request.form.get("walkin_gender") or None
            test_name = request.form.get("test_name", "").strip()
            if not name or not re.fullmatch(r"\d{10}", phone) or not test_name:
                flash("Enter patient name, a valid 10-digit mobile number, and a laboratory test.", "warning"); return redirect(url_for("labs"))
            existing = User.query.filter_by(phone=phone).first()
            profile = patient_for_user(existing) if existing else None
            if existing and not profile:
                flash("This mobile number belongs to a staff account. Use a different patient mobile number.", "warning"); return redirect(url_for("labs"))
            if not profile:
                existing = User(name=name, phone=phone, role="patient", approved=True); db.session.add(existing); db.session.flush()
                dob_text = request.form.get("walkin_dob")
                profile = Patient(user_id=existing.id, mrn=f"LAB-{Patient.query.count() + 1:05d}", gender=gender, dob=datetime.strptime(dob_text, "%Y-%m-%d").date() if dob_text else None, blood_group=request.form.get("walkin_blood_group") or None)
                db.session.add(profile); db.session.flush()
            order = LabOrder(patient_id=profile.id, doctor_id=current_user.id, test_name=test_name, status="Ordered", order_source="Direct walk-in", remarks="Direct walk-in laboratory request")
            db.session.add(order); db.session.flush(); db.session.add(LabOrderAudit(order_id=order.id, actor_id=current_user.id, previous_status="", new_status="Ordered", action="Walk-in patient registered and laboratory order created")); db.session.commit(); flash("Walk-in patient registered and laboratory order created.", "success"); return redirect(url_for("labs", order_id=order.id))
        order = selected or abort(404); previous = order.status
        if action == "collect":
            sample = LabSample.query.filter_by(order_id=order.id).first() or LabSample(order_id=order.id)
            sample.sample_id = sample.sample_id or f"SMP-{datetime.utcnow().strftime('%Y%m%d')}-{order.id:05d}"; sample.sample_type = request.form.get("sample_type", "Blood"); sample.container = request.form.get("container", "Vacutainer"); sample.condition = request.form.get("condition", "Acceptable"); sample.collected_at = datetime.utcnow(); sample.collected_by = current_user.id; sample.notes = request.form.get("notes")
            order.status = "Sample Collected" if sample.condition == "Acceptable" else "Sample Rejected"; db.session.add(sample); message = "Sample collected and label ID generated." if sample.condition == "Acceptable" else "Sample rejected; recollection is required."
        elif action == "save_result":
            order.result_value, order.reference_range, order.remarks = request.form.get("result_value"), request.form.get("reference_range"), request.form.get("remarks")
            try:
                value = float(order.result_value); bounds = [float(item.strip()) for item in (order.reference_range or "").replace("–", "-").split("-") if item.strip()]
                if len(bounds) == 2: order.remarks = (order.remarks or "") + (" [L]" if value < bounds[0] else " [H]" if value > bounds[1] else " [Normal]")
            except (TypeError, ValueError): pass
            order.status = "Draft Saved"; message = "Result draft saved with reference-range check."
        elif action == "submit":
            if order.sample and order.sample.condition != "Acceptable": flash("Rejected samples cannot be submitted for verification.", "warning"); return redirect(url_for("labs", order_id=order.id))
            order.status = "Verification Pending"; message = "Result submitted to the laboratory verifier."
        elif action == "verify":
            configured = len(lab_parameters_for(order.test_name))
            completed = sum(bool((item.value or "").strip()) for item in order.parameter_results)
            if (configured and completed < configured) or (not configured and not (order.result_value or "").strip()):
                flash("Complete every required result parameter before verification.", "warning"); return redirect(url_for("labs", order_id=order.id))
            order.status = "Verified"; message = f"Results verified by {current_user.name}."
        elif action == "finalise":
            if order.status != "Verified": flash("A laboratory verifier must verify the results before the report is finalised.", "warning"); return redirect(url_for("labs", order_id=order.id))
            order.status, order.completed_at = "Finalised", datetime.utcnow()
            if order.is_direct_walk_in:
                db.session.add(LabOrderAudit(order_id=order.id, actor_id=current_user.id, previous_status="Verified", new_status="Finalised", action="Direct walk-in report finalised", reason="Released to the patient portal; no referring doctor"))
                message = "Direct walk-in report finalised and released to the patient portal."
            else:
                db.session.add(LabOrderAudit(order_id=order.id, actor_id=current_user.id, previous_status="Verified", new_status="Finalised", action="Report sent to ordering doctor", reason=f"Available in {order.referral_label}'s consultation workspace"))
                message = f"Report finalised and sent to {order.referral_label} for consultation review."
        db.session.add(LabOrderAudit(order_id=order.id, actor_id=current_user.id, previous_status=previous, new_status=order.status, action=action, reason=request.form.get("notes"))); db.session.commit(); flash(message, "success")
        return redirect(url_for("labs", order_id=order.id))
    metrics = {"new": sum(order.status == "Ordered" for order in orders), "sample": sum(order.status in ["Ordered", "Sample Pending"] for order in orders), "collected": sum(order.status == "Sample Collected" for order in orders), "pending": sum(order.status == "Verification Pending" for order in orders), "completed": sum(order.status == "Finalised" for order in orders)}
    # The laboratory opens around a patient, not around a single isolated test.
    # Keep all of a patient's requested tests together for the worklist and the
    # full-screen result-entry action.
    patient_worklist = []
    grouped = {}
    for order in orders:
        if order.patient_id not in grouped:
            grouped[order.patient_id] = {"patient": order.patient, "orders": []}
            patient_worklist.append(grouped[order.patient_id])
        grouped[order.patient_id]["orders"].append(order)
    patient_orders = [order for order in orders if selected and order.patient_id == selected.patient_id]
    selected = prepare_lab_order_display(selected)
    return render_template("labs.html", orders=orders, selected=selected, metrics=metrics, previous=LabOrder.query.filter(LabOrder.patient_id == selected.patient_id, LabOrder.test_name == selected.test_name, LabOrder.id != selected.id, LabOrder.status == "Finalised").order_by(LabOrder.completed_at.desc()).first() if selected else None, patient_worklist=patient_worklist[:30], patient_orders=patient_orders)

@app.get("/lab-orders/<int:order_id>/whatsapp-report")
@login_required
@roles("lab", "admin")
def lab_whatsapp_report(order_id):
    order = db.session.get(LabOrder, order_id) or abort(404)
    if order.status != "Finalised":
        flash("WhatsApp notifications can be sent only after the laboratory report is finalised.", "warning")
        return redirect(url_for("labs", order_id=order.id))
    message = (f"Hello {order.patient.user.name}, your {order.test_name} laboratory report from "
               "Dr. Sagar's Lifestyle Clinic is ready. Please contact the clinic or sign in to your patient portal "
               "to view the verified report. Do not delay urgent medical care.")
    link = whatsapp_web_url(order.patient.user.phone, message)
    if not link:
        flash("A valid patient mobile number is required before sending a WhatsApp notification.", "warning")
        return redirect(url_for("labs", order_id=order.id))
    db.session.add(LabOrderAudit(order_id=order.id, actor_id=current_user.id, previous_status=order.status, new_status=order.status, action="WhatsApp report-ready notification opened"))
    db.session.commit()
    return redirect(link)

@app.get("/api/lab-orders/<int:order_id>/verification-link")
@login_required
@roles("lab", "admin")
def lab_report_verification_link(order_id):
    order = db.session.get(LabOrder, order_id) or abort(404)
    if order.status != "Finalised":
        return jsonify({"ok": False, "message": "Only finalised reports can be verified."}), 409
    return jsonify({"ok": True, "url": url_for("verify_lab_report", token=report_verification_token(order), _external=True)})

@app.get("/verify/lab/<token>")
def verify_lab_report(token):
    try:
        payload = URLSafeTimedSerializer(app.config["SECRET_KEY"]).loads(token, salt="lab-report-verification", max_age=60 * 60 * 24 * 365 * 5)
        order = db.session.get(LabOrder, int(payload.get("order_id")))
    except (BadSignature, TypeError, ValueError):
        order = None
    valid = bool(order and order.status == "Finalised")
    return render_template("lab_report_verify.html", order=order if valid else None, valid=valid)

@app.route("/lab-inventory", methods=["GET", "POST"])
@login_required
@roles("lab", "admin")
def lab_inventory():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            name = request.form.get("name", "").strip()
            if not name: flash("Enter an inventory item name.", "warning"); return redirect(url_for("lab_inventory"))
            item = LabInventoryItem(name=name, category=request.form.get("category") or "Consumable", sku=request.form.get("sku") or None, quantity=float(request.form.get("quantity") or 0), reorder_level=float(request.form.get("reorder_level") or 0), unit=request.form.get("unit") or "units", supplier=request.form.get("supplier") or None, location=request.form.get("location") or None, expiry_date=datetime.strptime(request.form["expiry_date"], "%Y-%m-%d").date() if request.form.get("expiry_date") else None)
            db.session.add(item); db.session.flush(); db.session.add(LabInventoryLog(item_id=item.id, actor_id=current_user.id, change=item.quantity, action="Opening stock", note="Item created")); db.session.commit(); flash("Laboratory inventory item added.", "success")
        elif action == "adjust":
            item = db.session.get(LabInventoryItem, request.form.get("item_id", type=int)) or abort(404)
            change = float(request.form.get("change") or 0)
            if item.quantity + change < 0: flash("Stock cannot go below zero.", "warning"); return redirect(url_for("lab_inventory"))
            item.quantity += change; db.session.add(LabInventoryLog(item_id=item.id, actor_id=current_user.id, change=change, action="Stock adjustment", note=request.form.get("note") or "Manual adjustment")); db.session.commit(); flash("Laboratory stock updated.", "success")
        return redirect(url_for("lab_inventory"))
    items = LabInventoryItem.query.filter_by(active=True).order_by(LabInventoryItem.category, LabInventoryItem.name).all()
    today = date.today(); low = [item for item in items if item.quantity <= item.reorder_level]; expiry = [item for item in items if item.expiry_date and item.expiry_date <= today]
    return render_template("lab_inventory.html", items=items, low_stock=low, expired=expiry)

@app.get("/api/lab-inventory-alerts")
@login_required
@roles("lab", "admin")
def lab_inventory_alerts():
    items = LabInventoryItem.query.filter_by(active=True).all()
    alerts = [item for item in items if item.quantity <= item.reorder_level or (item.expiry_date and item.expiry_date <= date.today())]
    return jsonify({"count": len(alerts), "items": [{"name": item.name, "quantity": item.quantity, "unit": item.unit, "expired": bool(item.expiry_date and item.expiry_date <= date.today())} for item in alerts]})

@app.get("/api/lab-orders/<int:order_id>/parameters")
@login_required
@roles("lab", "admin", "doctor")
def lab_order_parameters(order_id):
    order = db.session.get(LabOrder, order_id) or abort(404)
    parameters = lab_parameters_for(order.test_name)
    existing = {item.parameter_id: item for item in order.parameter_results}
    return jsonify({"parameters": [{"id": item.id, "name": item.name, "unit": item.unit, "reference": item.reference_range, "value": existing[item.id].value if item.id in existing else "", "flag": existing[item.id].flag if item.id in existing else ""} for item in parameters]})

@app.get("/api/lab-patients")
@login_required
@roles("lab", "admin", "reception")
def lab_patients():
    tests = sorted({item.name for item in Service.query.filter_by(category="Lab", active=True).all()} | {item.test_name for item in LabTestParameter.query.all()})
    return jsonify({"patients": [{"id": item.id, "name": item.user.name, "mrn": item.mrn} for item in Patient.query.order_by(Patient.mrn).all()], "tests": tests})

@app.post("/api/lab-orders/<int:order_id>/parameters")
@login_required
@roles("lab", "admin")
def save_lab_parameters(order_id):
    order = db.session.get(LabOrder, order_id) or abort(404); data = request.get_json(silent=True) or {}
    for row in data.get("parameters", []):
        parameter = db.session.get(LabTestParameter, int(row["id"])) or abort(400)
        result = LabParameterResult.query.filter_by(order_id=order.id, parameter_id=parameter.id).first() or LabParameterResult(order_id=order.id, parameter_id=parameter.id)
        result.value = str(row.get("value", "")); result.flag = ""
        try:
            value = float(result.value); limits = [float(value.strip()) for value in parameter.reference_range.split("-")]
            result.flag = "L" if value < limits[0] else "H" if value > limits[1] else "Normal"
        except (ValueError, TypeError): pass
        db.session.add(result)
    previous = order.status; order.status = "Draft Saved"; db.session.add(LabOrderAudit(order_id=order.id, actor_id=current_user.id, previous_status=previous, new_status=order.status, action="parameter_result_save")); db.session.commit()
    return jsonify({"ok": True})

@app.get("/api/lab-patients/<int:patient_id>/result-worklist")
@login_required
@roles("lab", "admin")
def lab_patient_result_worklist(patient_id):
    """Return all unfinished tests for one patient so they can be entered together."""
    patient = db.session.get(Patient, patient_id) or abort(404)
    orders = LabOrder.query.filter(
        LabOrder.patient_id == patient.id,
        LabOrder.status.notin_(["Finalised", "Cancelled", "Sample Rejected"]),
    ).order_by(LabOrder.ordered_at, LabOrder.id).all()
    payload = []
    for order in orders:
        existing = {item.parameter_id: item for item in order.parameter_results}
        parameters = lab_parameters_for(order.test_name)
        payload.append({
            "id": order.id,
            "test_name": order.test_name,
            "status": order.status,
            "sample_ready": bool(order.sample and order.sample.condition == "Acceptable"),
            "result_value": order.result_value or "",
            "reference_range": order.reference_range or "",
            "remarks": order.remarks or "",
            "parameters": [{
                "id": parameter.id, "name": parameter.name, "unit": parameter.unit or "",
                "reference": parameter.reference_range or "Reference not configured",
                "value": existing[parameter.id].value if parameter.id in existing else "",
                "flag": existing[parameter.id].flag if parameter.id in existing else "",
            } for parameter in parameters],
        })
    return jsonify({"ok": True, "patient": {"id": patient.id, "name": patient.user.name, "mrn": patient.mrn}, "orders": payload})

@app.post("/api/lab-patients/<int:patient_id>/result-worklist")
@login_required
@roles("lab", "admin")
def save_lab_patient_result_worklist(patient_id):
    """Save or submit several tests from the laboratory result-entry popup."""
    patient = db.session.get(Patient, patient_id) or abort(404)
    data = request.get_json(silent=True) or {}
    action = data.get("action", "save")
    entries = data.get("orders", [])
    if action not in {"save", "submit"} or not isinstance(entries, list) or not entries:
        return jsonify({"ok": False, "message": "Choose at least one test result to save."}), 400
    updated = 0
    try:
        for entry in entries:
            order = db.session.get(LabOrder, int(entry.get("id")))
            if not order or order.patient_id != patient.id or order.status in {"Finalised", "Cancelled", "Sample Rejected"}:
                continue
            previous = order.status
            configured = lab_parameters_for(order.test_name)
            allowed_parameters = {parameter.id: parameter for parameter in configured}
            supplied = entry.get("parameters", []) if isinstance(entry.get("parameters", []), list) else []
            for row in supplied:
                parameter = allowed_parameters.get(int(row.get("id")))
                if not parameter:
                    continue
                result = LabParameterResult.query.filter_by(order_id=order.id, parameter_id=parameter.id).first() or LabParameterResult(order_id=order.id, parameter_id=parameter.id)
                result.value = str(row.get("value", "")).strip()
                result.flag = ""
                try:
                    bounds = [float(value.strip()) for value in parameter.reference_range.replace("–", "-").split("-")]
                    numeric = float(result.value)
                    if len(bounds) == 2:
                        result.flag = "L" if numeric < bounds[0] else "H" if numeric > bounds[1] else "Normal"
                except (ValueError, TypeError, AttributeError):
                    pass
                db.session.add(result)
            if not configured:
                order.result_value = str(entry.get("result_value", "")).strip()
                order.reference_range = str(entry.get("reference_range", "")).strip()
            order.remarks = str(entry.get("remarks", "")).strip() or None
            if action == "submit":
                if not order.sample or order.sample.condition != "Acceptable":
                    raise ValueError(f"Collect an acceptable sample for {order.test_name} before submitting.")
                completed = sum(bool((item.value or "").strip()) for item in order.parameter_results)
                if (configured and completed < len(configured)) or (not configured and not (order.result_value or "").strip()):
                    raise ValueError(f"Complete all required values for {order.test_name} before submitting.")
                order.status = "Verification Pending"
                audit_action = "Bulk result entry submitted for verification"
            else:
                order.status = "Draft Saved"
                audit_action = "Bulk result entry saved"
            db.session.add(LabOrderAudit(order_id=order.id, actor_id=current_user.id, previous_status=previous, new_status=order.status, action=audit_action, reason=order.remarks))
            updated += 1
        if not updated:
            raise ValueError("No editable laboratory tests were found for this patient.")
        db.session.commit()
    except (ValueError, TypeError) as error:
        db.session.rollback()
        return jsonify({"ok": False, "message": str(error) or "Unable to save results."}), 400
    return jsonify({"ok": True, "updated": updated, "message": f"{updated} test result(s) {'submitted for verification' if action == 'submit' else 'saved as draft'}."})

@app.post("/api/lab-parameters/<int:parameter_id>/reference-range")
@login_required
@roles("admin")
def update_lab_reference_range(parameter_id):
    parameter = db.session.get(LabTestParameter, parameter_id) or abort(404)
    data = request.get_json(silent=True) or {}
    reference_range = (data.get("reference_range") or "").strip()
    if len(reference_range) > 80:
        return jsonify({"ok": False, "message": "Reference range must be 80 characters or fewer."}), 400
    parameter.reference_range = reference_range
    db.session.commit()
    return jsonify({"ok": True, "reference_range": parameter.reference_range})

@app.post("/lab-orders/<int:order_id>/doctor-review")
@login_required
@roles("doctor", "admin")
def doctor_review_lab(order_id):
    order = db.session.get(LabOrder, order_id) or abort(404)
    if current_user.role == "doctor" and order.doctor_id != current_user.id:
        abort(403)
    if order.status != "Finalised":
        flash("Only finalised laboratory reports can be reviewed by the doctor.", "warning")
        return redirect(request.referrer or url_for("dashboard"))
    note = request.form.get("review_note", "").strip()
    previous = order.status
    db.session.add(LabOrderAudit(order_id=order.id, actor_id=current_user.id, previous_status=previous, new_status=previous, action="Doctor reviewed report", reason=note or "Reviewed in consultation"))
    db.session.commit(); flash("Laboratory report marked as reviewed in this consultation.", "success")
    return redirect(request.referrer or url_for("dashboard"))

@app.get("/api/appointments/<int:appointment_id>/lab-reports")
@login_required
@roles("doctor", "admin")
def consultation_lab_reports(appointment_id):
    appointment = db.session.get(Appointment, appointment_id) or abort(404)
    if current_user.role == "doctor" and appointment.doctor_id != current_user.id:
        abort(403)
    reports = LabOrder.query.filter_by(patient_id=appointment.patient_id).order_by(LabOrder.ordered_at.desc()).all()
    return jsonify({"reports": [{"id": item.id, "test": item.test_name, "status": item.status, "source": item.order_source, "referral": item.referral_label, "sent_to_doctor": not item.is_direct_walk_in and any(audit.action == "Report sent to ordering doctor" for audit in item.audit_log), "ordered": item.ordered_at.strftime("%d %b %Y"), "result": item.result_value or "", "parameters": [{"name": value.parameter.name, "value": value.value, "unit": value.parameter.unit, "flag": value.flag} for value in item.parameter_results]} for item in reports]})

@app.get("/api/lab-orders/<int:order_id>/completeness")
@login_required
@roles("lab", "admin")
def lab_result_completeness(order_id):
    """Free local pre-verification checklist; it never interprets the result clinically."""
    order = db.session.get(LabOrder, order_id) or abort(404)
    configured = lab_parameters_for(order.test_name)
    values = {item.parameter_id: (item.value or "").strip() for item in order.parameter_results}
    missing_values = [item.name for item in configured if not values.get(item.id)]
    missing_references = [item.name for item in configured if not (item.reference_range or "").strip()]
    checks = []
    if configured:
        checks.append({"ok": not missing_values, "label": "Required parameter values", "detail": "All configured values are entered." if not missing_values else f"Missing: {', '.join(missing_values[:4])}{' and more' if len(missing_values) > 4 else ''}."})
        checks.append({"ok": not missing_references, "label": "Reference ranges", "detail": "All configured parameters have a reference range." if not missing_references else f"Reference not configured: {', '.join(missing_references[:4])}{' and more' if len(missing_references) > 4 else ''}."})
    else:
        checks.append({"ok": bool((order.result_value or "").strip()), "label": "Result entry", "detail": "A result has been entered." if (order.result_value or "").strip() else "Enter the result before verification."})
    checks.append({"ok": bool(order.sample and order.sample.condition == "Acceptable"), "label": "Sample acceptance", "detail": "Sample accepted for processing." if order.sample and order.sample.condition == "Acceptable" else "Collect and accept a suitable sample before verification."})
    return jsonify({"ok": True, "ready": all(item["ok"] for item in checks), "checks": checks, "disclaimer": "Completeness check only. A qualified laboratory professional remains responsible for validation and verification."})

@app.get("/api/lab-orders/<int:order_id>/ai-summary")
@login_required
@roles("lab", "admin", "doctor")
def lab_ai_summary(order_id):
    order = db.session.get(LabOrder, order_id) or abort(404)
    if order.status != "Finalised":
        return jsonify({"ok": False, "message": "AI summaries are available only after a report is finalised."}), 409
    values = [item for item in order.parameter_results if (item.value or "").strip()]
    abnormal = [item for item in values if item.flag and item.flag != "Normal"]
    facts = [f"{item.parameter.name}: {item.value} {item.parameter.unit or ''}".strip() + (f" ({item.flag})" if item.flag and item.flag != "Normal" else "") for item in abnormal]
    summary = f"Finalised {order.test_name} report for {order.patient.user.name}. "
    summary += f"{len(values)} parameter(s) were reported. "
    summary += ("Values needing clinical review: " + "; ".join(facts) + "." if facts else "No flagged numeric values were identified by the configured reference ranges.")
    db.session.add(LabOrderAudit(order_id=order.id, actor_id=current_user.id, previous_status=order.status, new_status=order.status, action="AI report summary generated")); db.session.commit()
    return jsonify({"ok": True, "summary": summary, "disclaimer": "AI-generated draft — laboratory or doctor review required. This summary does not diagnose disease or modify laboratory values."})

@app.post("/prescriptions")
@login_required
@roles("doctor", "admin")
def create_prescription():
    med_ids = request.form.getlist("medicine_id")
    dosages, durations = request.form.getlist("dosage"), request.form.getlist("duration")
    quantities, instructions = request.form.getlist("quantity"), request.form.getlist("instructions")
    rx = Prescription(patient_id=int(request.form["patient_id"]), doctor_id=current_user.id, notes=request.form.get("notes")); db.session.add(rx); db.session.flush()
    template_id = request.form.get("template_id")
    if template_id:
        template = db.session.get(PrescriptionTemplate, int(template_id)) or abort(404)
        rx.notes = "\n".join(filter(None, [template.advice, request.form.get("notes")]))
        for line in template.items_spec.splitlines():
            parts = [part.strip() for part in line.split("|")]
            if len(parts) != 5: continue
            medicine = Medicine.query.filter_by(name=parts[0]).first()
            if medicine: db.session.add(PrescriptionItem(prescription_id=rx.id, medicine_id=medicine.id, dosage=parts[1], duration=parts[2], quantity=int(parts[3]), instructions=parts[4]))
    else:
        added = 0
        for i, med_id in enumerate(med_ids):
            if not med_id:
                continue
            medicine = db.session.get(Medicine, int(med_id))
            if not medicine:
                continue
            try:
                quantity = max(1, int(quantities[i] if i < len(quantities) else 1))
            except ValueError:
                quantity = 1
            db.session.add(PrescriptionItem(prescription_id=rx.id, medicine_id=medicine.id, dosage=(dosages[i] if i < len(dosages) else "As directed")[:100], duration=(durations[i] if i < len(durations) else "As directed")[:100], quantity=quantity, instructions=(instructions[i] if i < len(instructions) else "")[:250]))
            added += 1
        if not added:
            db.session.rollback(); flash("Add at least one medicine or choose a prescription template.", "warning"); return redirect(request.referrer or url_for("dashboard"))
    db.session.commit()
    appointment_id = request.form.get("appointment_id", type=int)
    flash("Prescription created and sent to the Pharmacy pending-dispensing queue.", "success")
    if appointment_id:
        return redirect(url_for("encounter", appointment_id=appointment_id, tab="prescription"))
    return redirect(url_for("prescription_print", prescription_id=rx.id))

@app.route("/pharmacy", methods=["GET", "POST"])
@login_required
@roles("pharmacy", "admin")
def pharmacy():
    if request.method == "POST":
        action = request.form.get("action", "dispense")
        if action == "stock":
            medicine = db.session.get(Medicine, int(request.form["medicine_id"]))
            quantity = int(request.form["quantity"])
            if quantity <= 0: flash("Stock quantity must be greater than zero.", "warning")
            else:
                expiry = datetime.strptime(request.form["expiry_date"], "%Y-%m-%d").date() if request.form.get("expiry_date") else None
                batch_number = request.form.get("batch_number", "").strip()
                mrp = float(request.form.get("mrp") or medicine.unit_price)
                medicine.stock += quantity
                if mrp > 0: medicine.unit_price = mrp
                if batch_number:
                    db.session.add(MedicineBatch(medicine_id=medicine.id, batch_number=batch_number, expiry_date=expiry, quantity_received=quantity, quantity_available=quantity, purchase_price=float(request.form.get("purchase_price") or 0), mrp=mrp, gst_percent=float(request.form.get("gst_percent") or 0), supplier=request.form.get("supplier") or None, rack_location=request.form.get("rack_location") or None))
                db.session.commit(); flash(f"Added {quantity} units of {medicine.name} to stock.", "success")
            return redirect(url_for("pharmacy", tab="stock"))
        if action == "new_medicine":
            name = request.form.get("name", "").strip(); strength = request.form.get("strength", "").strip()
            if not name: flash("Medicine name is required.", "warning")
            elif Medicine.query.filter_by(name=name, strength=strength).first(): flash("This medicine and strength already exist.", "warning")
            else:
                db.session.add(Medicine(name=name, strength=strength, stock=0, reorder_level=int(request.form.get("reorder_level") or 10), unit_price=float(request.form.get("unit_price") or 0))); db.session.commit(); flash("Medicine added to the pharmacy master. Add its first batch below.", "success")
            return redirect(url_for("pharmacy", tab="stock"))
        if action == "pos":
            medicine_ids = request.form.getlist("medicine_id"); quantities = request.form.getlist("quantity")
            requested = {}
            for medicine_id, quantity in zip(medicine_ids, quantities):
                if not medicine_id: continue
                requested[int(medicine_id)] = requested.get(int(medicine_id), 0) + int(quantity or 0)
            if not requested: flash("Add at least one medicine to the bill.", "warning"); return redirect(url_for("pharmacy", tab="pos"))
            medicines_for_sale = []
            for medicine_id, quantity in requested.items():
                medicine = db.session.get(Medicine, medicine_id)
                if not medicine or quantity <= 0 or quantity > medicine.stock:
                    flash("One or more medicine quantities are unavailable.", "danger"); return redirect(url_for("pharmacy", tab="pos"))
                medicines_for_sale.append((medicine, quantity))
            payment_mode = request.form.get("payment_mode", "Cash")
            discount = max(0, float(request.form.get("discount") or 0))
            subtotal = sum(medicine.unit_price * quantity for medicine, quantity in medicines_for_sale); total = max(0, subtotal - discount)
            description = "POS medicines: " + "; ".join(f"{medicine.name} x {quantity}" for medicine, quantity in medicines_for_sale) + f" · Payment: {payment_mode} · Discount: ₹{discount:.2f}"
            invoice = Invoice(patient_id=int(request.form["patient_id"]), category="Pharmacy POS", description=description, amount=total)
            db.session.add(invoice); db.session.flush()
            for index, (medicine, quantity) in enumerate(medicines_for_sale):
                batch = MedicineBatch.query.filter(MedicineBatch.medicine_id == medicine.id, MedicineBatch.quantity_available >= 0).order_by(MedicineBatch.expiry_date).first()
                medicine.stock -= quantity; consume_medicine_batches(medicine, quantity)
                line_discount = discount if index == 0 else 0
                db.session.add(PharmacySaleLine(invoice_id=invoice.id, medicine_id=medicine.id, batch_id=batch.id if batch else None, quantity=quantity, unit_price=medicine.unit_price, discount=line_discount))
            prescription_id = request.form.get("prescription_id", type=int)
            if prescription_id:
                billed_rx = db.session.get(Prescription, prescription_id)
                if not billed_rx or billed_rx.dispensed or billed_rx.patient_id != invoice.patient_id:
                    db.session.rollback()
                    flash("The linked prescription is invalid or was already dispensed.", "danger")
                    return redirect(url_for("pharmacy"))
                prescribed = {}
                for item in billed_rx.items:
                    prescribed[item.medicine_id] = prescribed.get(item.medicine_id, 0) + item.quantity
                if requested != prescribed:
                    db.session.rollback()
                    flash("Prescription medicines or quantities changed. Reopen the prescription before billing.", "danger")
                    return redirect(url_for("pharmacy", tab="pos", rx=billed_rx.id))
                billed_rx.dispensed = True
            db.session.commit()
            return redirect(url_for("invoice_print", invoice_id=invoice.id))
        rx = db.session.get(Prescription, int(request.form["prescription_id"]))
        if rx.dispensed: flash("This prescription was already dispensed.", "warning")
        elif any(item.medicine.stock < item.quantity for item in rx.items): flash("Insufficient stock for one or more items.", "danger")
        else:
            return redirect(url_for("pharmacy", tab="pos", rx=rx.id))
    medicines = Medicine.query.order_by(Medicine.name).all(); batches = MedicineBatch.query.order_by(MedicineBatch.expiry_date, MedicineBatch.received_at.desc()).all(); today = date.today()
    low_stock = [medicine for medicine in medicines if medicine.stock <= medicine.reorder_level]; expiring_batches = [batch for batch in batches if batch.expiry_date and batch.expiry_date <= today]
    pending_prescriptions = Prescription.query.filter_by(dispensed=False).order_by(Prescription.created_at.desc()).all()
    selected_rx_id = request.args.get("rx", type=int)
    selected_rx = next((rx for rx in pending_prescriptions if rx.id == selected_rx_id), None) or (pending_prescriptions[0] if pending_prescriptions else None)
    pos_rx = db.session.get(Prescription, selected_rx_id) if request.args.get("tab") == "pos" and selected_rx_id else None
    if pos_rx and pos_rx.dispensed:
        pos_rx = None
    pos_rx_payload = ({
        "id": pos_rx.id,
        "patient_id": pos_rx.patient_id,
        "patient": pos_rx.patient.user.name,
        "items": [{"medicine_id": item.medicine_id, "quantity": item.quantity} for item in pos_rx.items],
    } if pos_rx else None)
    pos_prescriptions_payload = {}
    for rx in pending_prescriptions:
        if str(rx.patient_id) in pos_prescriptions_payload:
            continue
        pos_prescriptions_payload[str(rx.patient_id)] = {
            "id": rx.id, "patient_id": rx.patient_id, "patient": rx.patient.user.name,
            "doctor": rx.doctor.name, "created": rx.created_at.strftime("%d %b %Y, %I:%M %p"),
            "notes": rx.notes or "", "items": [{
                "medicine_id": item.medicine_id, "name": item.medicine.name,
                "strength": item.medicine.strength or "", "quantity": item.quantity,
                "dosage": item.dosage or "As directed", "duration": item.duration or "As directed",
                "instructions": item.instructions or "", "stock": item.medicine.stock,
                "unit_price": item.medicine.unit_price,
            } for item in rx.items],
        }
    pharmacy_invoices = Invoice.query.filter(Invoice.category.in_(["Pharmacy", "Pharmacy POS"])).order_by(Invoice.created_at.desc()).limit(8).all()
    today_invoices = [invoice for invoice in pharmacy_invoices if invoice.created_at.date() == today]
    today_sales = round(sum(invoice.amount for invoice in today_invoices), 2)
    stock_units = sum(max(0, medicine.stock or 0) for medicine in medicines)
    return render_template("pharmacy.html", medicines=medicines, batches=batches, low_stock=low_stock,
        expiring_batches=expiring_batches, today=today, prescriptions=pending_prescriptions,
        selected_rx=selected_rx, pharmacy_invoices=pharmacy_invoices, today_invoices=today_invoices,
        today_sales=today_sales, stock_units=stock_units, pos_rx_payload=pos_rx_payload,
        pos_prescriptions_payload=pos_prescriptions_payload,
        patients=Patient.query.order_by(Patient.mrn).all(),
        active_tab=request.args.get("tab", "dispense"))

@app.route("/billing/collections", methods=["GET", "POST"])
@login_required
@roles("admin", "pharmacy", "reception")
def billing_collections():
    if request.method == "POST":
        invoice = db.session.get(Invoice, request.form.get("invoice_id", type=int)) or abort(404)
        try:
            amount = round(float(request.form.get("amount") or 0), 2)
        except ValueError:
            amount = 0
        balance = invoice_balance(invoice)
        if amount <= 0 or amount > balance:
            flash(f"Enter a collection amount between ₹0.01 and ₹{balance:.2f}.", "warning")
            return redirect(url_for("billing_collections"))
        payment = Payment(
            invoice_id=invoice.id, amount=amount, mode=request.form.get("mode") or "Cash",
            reference=(request.form.get("reference") or "").strip() or None,
            note=(request.form.get("note") or "").strip() or None, received_by=current_user.id,
        )
        db.session.add(payment)
        invoice.paid = amount >= balance
        db.session.commit()
        flash(f"Payment of ₹{amount:.2f} recorded. Remaining balance: ₹{invoice_balance(invoice):.2f}.", "success")
        return redirect(url_for("billing_collections"))
    invoices = Invoice.query.order_by(Invoice.created_at.desc()).limit(100).all()
    rows = [{"invoice": invoice, "paid": invoice_paid_amount(invoice), "balance": invoice_balance(invoice)} for invoice in invoices]
    return render_template("billing_collections.html", rows=rows, total_due=round(sum(row["balance"] for row in rows), 2), collected_today=round(sum(payment.amount for payment in Payment.query.filter(func.date(Payment.created_at) == date.today()).all()), 2))

@app.get("/api/pharmacy/medicine/<int:medicine_id>/billing-details")
@login_required
@roles("pharmacy", "admin")
def pharmacy_billing_details(medicine_id):
    medicine = db.session.get(Medicine, medicine_id) or abort(404)
    batch = MedicineBatch.query.filter(MedicineBatch.medicine_id == medicine.id, MedicineBatch.quantity_available > 0).order_by(MedicineBatch.expiry_date).first()
    return jsonify({"name": medicine.name, "strength": medicine.strength or "", "available": medicine.stock, "price": medicine.unit_price, "batch": batch.batch_number if batch else "Not recorded", "expiry": batch.expiry_date.strftime("%b %Y") if batch and batch.expiry_date else "Not recorded", "gst": batch.gst_percent if batch else 0, "mrp": batch.mrp if batch and batch.mrp else medicine.unit_price})

@app.route("/dietician/workspace", methods=["GET", "POST"])
@login_required
@roles("dietician", "admin")
def dietician_workspace():
    patients = Patient.query.order_by(Patient.mrn).all()
    referrals = DieticianReferral.query.filter(DieticianReferral.status.notin_(["Completed", "Closed"])).order_by(DieticianReferral.created_at.desc()).all()
    doctor_diet_appointments = Appointment.query.filter(Appointment.reason.ilike("%diet%"), Appointment.doctor.has(User.role == "doctor")).order_by(Appointment.scheduled_at).all()
    direct_queue_query = Appointment.query.filter(Appointment.doctor.has(User.role == "dietician"))
    if current_user.role == "dietician":
        direct_queue_query = direct_queue_query.filter_by(doctor_id=current_user.id)
    direct_queue = direct_queue_query.order_by(Appointment.scheduled_at).all()
    queue_patient_ids = [item.patient_id for item in referrals] + [item.patient_id for item in doctor_diet_appointments] + [item.patient_id for item in direct_queue]
    selected_id = request.form.get("patient_id", type=int) or request.args.get("patient_id", type=int) or (queue_patient_ids[0] if queue_patient_ids else (patients[0].id if patients else None))
    patient = db.session.get(Patient, selected_id) if selected_id else None
    assessment = NutritionAssessment.query.filter_by(patient_id=selected_id).order_by(NutritionAssessment.updated_at.desc()).first() if patient else None
    if request.method == "POST" and patient:
        action = request.form.get("action")
        if action == "save_assessment":
            assessment = assessment or NutritionAssessment(patient_id=patient.id, dietician_id=current_user.id)
            for field in ("diet_type", "cuisine", "preferences", "dislikes", "allergies", "lifestyle", "dietary_recall"):
                if field in request.form: setattr(assessment, field, request.form.get(field))
            for field in ("height_cm", "weight_kg", "target_weight_kg", "waist_cm", "hip_cm"):
                if field in request.form:
                    value = request.form.get(field); setattr(assessment, field, float(value) if value else None)
            if "calorie_target" in request.form: assessment.calorie_target = request.form.get("calorie_target", type=int)
            if "protein_target" in request.form: assessment.protein_target = request.form.get("protein_target", type=int)
            assessment.status = "Draft saved"
            db.session.add(assessment); db.session.commit(); flash("Nutrition assessment saved.", "success")
        elif action == "generate_plan":
            assessment = assessment or NutritionAssessment(patient_id=patient.id, dietician_id=current_user.id)
            if not assessment.calorie_target or not assessment.protein_target:
                flash("Save calorie and protein targets before generating a draft.", "warning"); return redirect(url_for("dietician_workspace", patient_id=patient.id))
            foods = FoodMaster.query.filter_by(active=True).all()
            vegetarian = (assessment.diet_type or "").lower() in ("vegetarian", "vegan", "jain")
            foods = [food for food in foods if not vegetarian or food.vegetarian] or foods
            excluded = (" ".join(filter(None, [patient.allergies, assessment.allergies, assessment.dislikes]))).lower()
            foods = [food for food in foods if food.name.lower() not in excluded] or foods
            slots = [("Early morning", "06:30"), ("Breakfast", "08:30"), ("Mid-morning", "11:00"), ("Lunch", "13:30"), ("Evening snack", "17:00"), ("Dinner", "20:00")]
            duration = min(max(request.form.get("plan_duration", type=int) or 1, 1), 30)
            meals = [{"day": day, "meal": meal, "time": time, "food_id": foods[(index + day - 1) % len(foods)].id, "food": foods[(index + day - 1) % len(foods)].name, "serving": foods[(index + day - 1) % len(foods)].serving, "calories": foods[(index + day - 1) % len(foods)].calories, "protein": foods[(index + day - 1) % len(foods)].protein} for day in range(1, duration + 1) for index, (meal, time) in enumerate(slots)]
            template_name = request.form.get("plan_template") or f"{assessment.diet_type or 'Personalised'} nutrition"
            db.session.add(DietPlan(patient_id=patient.id, dietician_id=current_user.id, title=f"{template_name} · {duration}-day draft", status="Draft generated", calorie_target=assessment.calorie_target, protein_target=assessment.protein_target, meals_json=json.dumps(meals), instructions=f"{duration}-day AI-assisted draft using approved food-master items only. Dietician review and signing required before sharing.")); db.session.commit(); flash("Draft created from approved food records. Review each meal before signing.", "success")
        elif action == "create_manual_plan":
            assessment = assessment or NutritionAssessment(patient_id=patient.id, dietician_id=current_user.id)
            duration = min(max(request.form.get("plan_duration", type=int) or 1, 1), 30)
            slots = [("Early morning", "06:30"), ("Breakfast", "08:30"), ("Mid-morning", "11:00"), ("Lunch", "13:30"), ("Evening snack", "17:00"), ("Dinner", "20:00")]
            food_ids = request.form.getlist("manual_food_id")
            selected_foods = [db.session.get(FoodMaster, int(food_id)) for food_id in food_ids if food_id]
            selected_foods = [food for food in selected_foods if food and food.active]
            if not selected_foods:
                flash("Choose at least one approved food to create a manual plan.", "warning"); return redirect(url_for("dietician_workspace", patient_id=patient.id))
            meals = [{"day": day, "meal": meal, "time": time, "food_id": selected_foods[index % len(selected_foods)].id, "food": selected_foods[index % len(selected_foods)].name, "serving": selected_foods[index % len(selected_foods)].serving, "calories": selected_foods[index % len(selected_foods)].calories, "protein": selected_foods[index % len(selected_foods)].protein} for day in range(1, duration + 1) for index, (meal, time) in enumerate(slots)]
            db.session.add(DietPlan(patient_id=patient.id, dietician_id=current_user.id, title=f"Manual nutrition plan · {duration}-day draft", status="Draft", calorie_target=assessment.calorie_target, protein_target=assessment.protein_target, meals_json=json.dumps(meals), instructions="Manual dietician draft. Review every meal and patient restriction before signing.")); db.session.commit(); flash("Manual diet-plan draft created.", "success")
        elif action == "sign_plan":
            plan = db.session.get(DietPlan, request.form.get("plan_id", type=int))
            if plan and plan.patient_id == patient.id and plan.status != "Signed": plan.status, plan.signed_at = "Signed", datetime.utcnow(); db.session.commit(); flash("Diet plan signed.", "success")
        elif action == "add_progress":
            db.session.add(NutritionProgress(patient_id=patient.id, dietician_id=current_user.id, weight_kg=request.form.get("progress_weight", type=float), adherence=request.form.get("adherence", type=int), notes=request.form.get("progress_notes"))); db.session.commit(); flash("Progress updated.", "success")
        target_tab = "plan" if action in ("generate_plan", "create_manual_plan", "sign_plan") else "progress" if action == "add_progress" else "assessment"
        return redirect(url_for("dietician_workspace", patient_id=patient.id, tab=target_tab))
    queue = doctor_diet_appointments + direct_queue
    latest_visit = Encounter.query.join(Appointment).filter(Appointment.patient_id == selected_id).order_by(Appointment.scheduled_at.desc()).first() if patient else None
    labs = LabOrder.query.filter_by(patient_id=selected_id).order_by(LabOrder.ordered_at.desc()).limit(5).all() if patient else []
    prescription = Prescription.query.filter_by(patient_id=selected_id).order_by(Prescription.created_at.desc()).first() if patient else None
    plans = DietPlan.query.filter_by(patient_id=selected_id).order_by(DietPlan.created_at.desc()).all() if patient else []
    active_plan = plans[0] if plans else None; meals = json.loads(active_plan.meals_json or "[]") if active_plan else []
    macro_override = DietPlanMacroOverride.query.filter_by(plan_id=active_plan.id).first() if active_plan else None
    progress = NutritionProgress.query.filter_by(patient_id=selected_id).order_by(NutritionProgress.created_at.desc()).limit(6).all() if patient else []
    stats = {"today": sum(item.scheduled_at.date() == date.today() for item in queue), "waiting": sum(item.status in ("Checked In", "Waiting") for item in queue), "plans": DietPlan.query.filter(DietPlan.status != "Signed").count(), "followups": len(progress), "referred": len(referrals) + len(doctor_diet_appointments), "direct": len(direct_queue)}
    return render_template("dietician_workspace.html", patients=patients, patient=patient, assessment=assessment, queue=queue, referrals=referrals, doctor_diet_appointments=doctor_diet_appointments, direct_queue=direct_queue, latest_visit=latest_visit, labs=labs, prescription=prescription, plans=plans, active_plan=active_plan, macro_override=macro_override, meals=meals, progress=progress, stats=stats, foods=FoodMaster.query.filter_by(active=True).order_by(FoodMaster.name).all())

@app.route("/dietician", methods=["GET", "POST"])
@login_required
@roles("dietician", "admin")
def dietician():
    bmi = plan = None
    if request.method == "POST":
        weight, height = float(request.form["weight"]), float(request.form["height"]) / 100
        bmi = round(weight / (height * height), 1)
        goal = request.form.get("goal", "maintenance")
        calorie_note = "a balanced maintenance plan" if goal == "maintenance" else f"a {goal} focused calorie-controlled plan"
        plan = f"AI-style starter plan for {calorie_note}: breakfast—protein plus whole grains; lunch—half plate vegetables, lean protein and complex carbohydrates; snack—fruit or nuts; dinner—vegetables and protein. Adjust for allergies, culture, medical history and doctor advice."
    return render_template("dietician.html", bmi=bmi, plan=plan, patients=Patient.query.all())

@app.route("/admin", methods=["GET", "POST"])
@login_required
@roles("admin")
def admin():
    if request.method == "POST":
        if request.form["action"] == "approve": db.session.get(User, int(request.form["user_id"])).approved = True
        else: db.session.get(Service, int(request.form["service_id"])).fee = float(request.form["fee"])
        db.session.commit(); flash("Admin setting updated.", "success")
    daily = db.session.query(Invoice.category, func.sum(Invoice.amount)).filter(func.date(Invoice.created_at) == date.today()).group_by(Invoice.category).all()
    days = [date.today().fromordinal(date.today().toordinal() - offset) for offset in range(6, -1, -1)]
    series = []
    for day in days:
        total = db.session.query(func.coalesce(func.sum(Invoice.amount), 0)).filter(func.date(Invoice.created_at) == day).scalar()
        series.append({"label": day.strftime("%a"), "amount": float(total or 0)})
    total_today = sum(float(total or 0) for _, total in daily); weekly_total = sum(item["amount"] for item in series)
    admin_metrics = {"today": total_today, "week": weekly_total, "invoices": Invoice.query.filter(func.date(Invoice.created_at) == date.today()).count(), "patients": Patient.query.count(), "staff": User.query.filter(User.role != "patient", User.approved.is_(True)).count(), "low_medicines": Medicine.query.filter(Medicine.stock <= Medicine.reorder_level).count(), "low_lab": LabInventoryItem.query.filter(LabInventoryItem.quantity <= LabInventoryItem.reorder_level, LabInventoryItem.active.is_(True)).count()}
    return render_template("admin.html", pending=User.query.filter_by(approved=False).all(), services=Service.query.order_by(Service.category, Service.name).all(), daily=daily, series=series, admin_metrics=admin_metrics, users=User.query.filter(User.role != "patient").order_by(User.role, User.name).all())

def finance_date_range(start_text, end_text):
    """Return a validated inclusive date range for the administration reports."""
    today = date.today()
    try:
        start = datetime.strptime(start_text, "%Y-%m-%d").date() if start_text else today.replace(day=1)
        end = datetime.strptime(end_text, "%Y-%m-%d").date() if end_text else today
    except ValueError:
        raise ValueError("Use valid report dates.")
    if end < start:
        raise ValueError("End date must be on or after the start date.")
    if (end - start).days > 366:
        raise ValueError("Select a report period of up to 366 days.")
    return start, end

@app.route("/admin/finance", methods=["GET", "POST"])
@login_required
@roles("admin")
def admin_finance():
    today = date.today()
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "expense":
                expense_date = datetime.strptime(request.form.get("expense_date", ""), "%Y-%m-%d").date()
                category = (request.form.get("category") or "").strip()
                description = (request.form.get("description") or "").strip()
                amount = float(request.form.get("amount") or 0)
                if not category or not description or amount <= 0:
                    raise ValueError("Enter an expense category, description and amount greater than zero.")
                db.session.add(Expense(expense_date=expense_date, category=category[:80], description=description[:250], vendor=(request.form.get("vendor") or "").strip()[:120] or None, payment_mode=(request.form.get("payment_mode") or "Cash")[:40], amount=amount, created_by=current_user.id))
                message = "Expense recorded."
            elif action == "payroll":
                employee = db.session.get(User, int(request.form.get("employee_id") or 0))
                pay_period = (request.form.get("pay_period") or "").strip()
                if not employee or employee.role == "patient" or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", pay_period):
                    raise ValueError("Select a staff member and a valid payroll month.")
                base = float(request.form.get("base_salary") or 0)
                allowances = float(request.form.get("allowances") or 0)
                deductions = float(request.form.get("deductions") or 0)
                if min(base, allowances, deductions) < 0 or base + allowances - deductions < 0:
                    raise ValueError("Payroll amounts must be valid and net pay cannot be negative.")
                record = PayrollRecord.query.filter_by(employee_id=employee.id, pay_period=pay_period).first()
                if not record:
                    record = PayrollRecord(employee_id=employee.id, pay_period=pay_period, created_by=current_user.id)
                    db.session.add(record)
                record.base_salary, record.allowances, record.deductions = base, allowances, deductions
                record.status = request.form.get("status") if request.form.get("status") in {"Draft", "Approved", "Paid"} else "Draft"
                paid_on_text = request.form.get("paid_on")
                record.paid_on = datetime.strptime(paid_on_text, "%Y-%m-%d").date() if paid_on_text and record.status == "Paid" else None
                record.note = (request.form.get("note") or "").strip()[:250] or None
                message = "Payroll record saved."
            else:
                abort(400)
            db.session.commit(); flash(message, "success")
        except (ValueError, TypeError):
            db.session.rollback(); flash("Please check the values entered and try again.", "danger")
        return redirect(url_for("admin_finance", start=request.args.get("start", ""), end=request.args.get("end", "")))
    try:
        start, end = finance_date_range(request.args.get("start", ""), request.args.get("end", ""))
    except ValueError as error:
        flash(str(error), "warning"); start, end = today.replace(day=1), today
    revenue_rows = db.session.query(Invoice.category, func.coalesce(func.sum(Invoice.amount), 0)).filter(Invoice.paid.is_(True), func.date(Invoice.created_at) >= start, func.date(Invoice.created_at) <= end).group_by(Invoice.category).order_by(func.sum(Invoice.amount).desc()).all()
    expenses = Expense.query.filter(Expense.expense_date >= start, Expense.expense_date <= end).order_by(Expense.expense_date.desc(), Expense.id.desc()).all()
    payroll = PayrollRecord.query.filter(PayrollRecord.pay_period >= start.strftime("%Y-%m"), PayrollRecord.pay_period <= end.strftime("%Y-%m")).order_by(PayrollRecord.pay_period.desc(), PayrollRecord.id.desc()).all()
    revenue_total = round(sum(float(value or 0) for _, value in revenue_rows), 2)
    expense_total = round(sum(item.amount for item in expenses), 2)
    payroll_total = round(sum(item.net_pay for item in payroll if item.status == "Paid"), 2)
    metrics = {"revenue": revenue_total, "expenses": expense_total, "payroll": payroll_total, "net": round(revenue_total - expense_total - payroll_total, 2)}
    staff = User.query.filter(User.role != "patient", User.approved.is_(True)).order_by(User.name).all()
    return render_template("admin_finance.html", start=start, end=end, revenue_rows=revenue_rows, expenses=expenses, payroll=payroll, metrics=metrics, staff=staff, today=today)

@app.get("/admin/finance/export")
@login_required
@roles("admin")
def admin_finance_export():
    try:
        start, end = finance_date_range(request.args.get("start", ""), request.args.get("end", ""))
    except ValueError:
        abort(400)
    report_type = request.args.get("type", "all")
    output = StringIO(); writer = csv.writer(output)
    writer.writerow(["Dr. Sagar's Lifestyle Clinic financial export", start.isoformat(), end.isoformat()])
    if report_type in {"all", "revenue"}:
        writer.writerow([]); writer.writerow(["REVENUE", "Date", "Invoice ID", "Service / category", "Amount", "Paid"])
        for invoice in Invoice.query.filter(func.date(Invoice.created_at) >= start, func.date(Invoice.created_at) <= end).order_by(Invoice.created_at).all():
            writer.writerow(["Revenue", invoice.created_at.strftime("%Y-%m-%d"), invoice.id, invoice.category, f"{invoice.amount:.2f}", "Yes" if invoice.paid else "No"])
    if report_type in {"all", "expenses"}:
        writer.writerow([]); writer.writerow(["EXPENSE", "Date", "Category", "Description", "Vendor", "Payment mode", "Amount"])
        for item in Expense.query.filter(Expense.expense_date >= start, Expense.expense_date <= end).order_by(Expense.expense_date).all():
            writer.writerow(["Expense", item.expense_date.isoformat(), item.category, item.description, item.vendor or "", item.payment_mode, f"{item.amount:.2f}"])
    if report_type in {"all", "payroll"}:
        writer.writerow([]); writer.writerow(["PAYROLL", "Pay period", "Employee", "Role", "Base", "Allowances", "Deductions", "Net pay", "Status", "Paid date"])
        for item in PayrollRecord.query.filter(PayrollRecord.pay_period >= start.strftime("%Y-%m"), PayrollRecord.pay_period <= end.strftime("%Y-%m")).order_by(PayrollRecord.pay_period, PayrollRecord.id).all():
            writer.writerow(["Payroll", item.pay_period, item.employee.name, item.employee.role, f"{item.base_salary:.2f}", f"{item.allowances:.2f}", f"{item.deductions:.2f}", f"{item.net_pay:.2f}", item.status, item.paid_on.isoformat() if item.paid_on else ""])
    filename = f"clinic-finance-{start.isoformat()}-to-{end.isoformat()}.csv"
    return app.response_class(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

@app.get("/api/admin/finance-insights")
@login_required
@roles("admin")
def admin_finance_insights():
    """Free local financial analysis; no financial records leave the clinic app."""
    try:
        start, end = finance_date_range(request.args.get("start", ""), request.args.get("end", ""))
    except ValueError:
        return jsonify({"ok": False, "message": "Use a valid date range."}), 400
    revenue_rows = db.session.query(Invoice.category, func.coalesce(func.sum(Invoice.amount), 0)).filter(Invoice.paid.is_(True), func.date(Invoice.created_at) >= start, func.date(Invoice.created_at) <= end).group_by(Invoice.category).order_by(func.sum(Invoice.amount).desc()).all()
    expenses = Expense.query.filter(Expense.expense_date >= start, Expense.expense_date <= end).all()
    payroll = PayrollRecord.query.filter(PayrollRecord.pay_period >= start.strftime("%Y-%m"), PayrollRecord.pay_period <= end.strftime("%Y-%m")).all()
    revenue = round(sum(float(amount or 0) for _, amount in revenue_rows), 2)
    expense_total = round(sum(item.amount for item in expenses), 2)
    payroll_total = round(sum(item.net_pay for item in payroll if item.status == "Paid"), 2)
    largest_expense = max(expenses, key=lambda item: item.amount, default=None)
    insights = []
    if revenue_rows:
        category, amount = revenue_rows[0]
        share = round((float(amount or 0) / revenue * 100), 1) if revenue else 0
        insights.append({"tone": "positive", "title": f"Top revenue source: {category}", "detail": f"₹{float(amount or 0):,.2f} ({share}% of paid revenue) in the selected period."})
    else:
        insights.append({"tone": "attention", "title": "No paid revenue recorded", "detail": "Review invoice collection and date selection before closing the period."})
    outgoings = expense_total + payroll_total
    if revenue and outgoings > revenue:
        insights.append({"tone": "attention", "title": "Outgoings exceed recorded revenue", "detail": f"Expenses and paid payroll are ₹{outgoings:,.2f}; review cash flow and outstanding collections."})
    elif revenue:
        insights.append({"tone": "positive", "title": "Operating balance is positive", "detail": f"Current recorded balance before tax is ₹{revenue - outgoings:,.2f}."})
    if largest_expense:
        insights.append({"tone": "info", "title": f"Largest expense: {largest_expense.category}", "detail": f"{largest_expense.description} · ₹{largest_expense.amount:,.2f}."})
    draft_payroll = sum(item.status != "Paid" for item in payroll)
    if draft_payroll:
        insights.append({"tone": "warning", "title": f"{draft_payroll} payroll record(s) not paid", "detail": "Review Draft or Approved records before payroll closure."})
    return jsonify({"ok": True, "mode": "Free local finance analyst", "insights": insights[:4], "disclaimer": "Operational finance summary only. Confirm accounting, tax and statutory obligations with your qualified accountant."})

@app.route("/admin/staff", methods=["GET", "POST"])
@login_required
@roles("admin")
def admin_staff():
    allowed_roles = {"doctor", "reception", "lab", "pharmacy", "dietician"}
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        phone = re.sub(r"\D", "", request.form.get("phone") or "")
        role = request.form.get("role") or ""
        password = request.form.get("password") or ""
        approved = request.form.get("approved") == "on"
        if len(name) < 2 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or role not in allowed_roles:
            flash("Enter the employee name, a valid clinic email and a permitted role.", "danger")
        elif phone and not re.fullmatch(r"\d{10}", phone):
            flash("Mobile number must contain 10 digits when provided.", "danger")
        elif len(password) < 10 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
            flash("Temporary password must be at least 10 characters and include a letter and number.", "danger")
        elif User.query.filter(or_(func.lower(User.email) == email, User.phone == phone if phone else False)).first():
            flash("That clinic email or mobile number is already registered.", "danger")
        else:
            employee = User(name=name[:100], email=email, phone=phone or None, role=role, approved=approved)
            employee.set_password(password)
            db.session.add(employee); db.session.commit()
            flash(f"{employee.name} was registered as {role.title()}. {'They can sign in now.' if approved else 'Approve their account before they can sign in.'}", "success")
            return redirect(url_for("admin_staff"))
    staff = User.query.filter(User.role != "patient").order_by(User.role, User.name).all()
    return render_template("admin_staff.html", staff=staff)

@app.post("/api/admin/staff")
@login_required
@roles("admin")
def create_staff_account_api():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    email = str(data.get("email") or "").strip().lower()
    phone = re.sub(r"\D", "", str(data.get("phone") or ""))
    role = str(data.get("role") or "")
    password = str(data.get("password") or "")
    approved = bool(data.get("approved"))
    allowed_roles = {"doctor", "reception", "lab", "pharmacy", "dietician"}
    if len(name) < 2 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or role not in allowed_roles:
        return jsonify({"ok": False, "message": "Enter the employee name, a valid clinic email and a permitted role."}), 400
    if phone and not re.fullmatch(r"\d{10}", phone):
        return jsonify({"ok": False, "message": "Mobile number must contain 10 digits when provided."}), 400
    if len(password) < 10 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return jsonify({"ok": False, "message": "Temporary password must be at least 10 characters and include a letter and number."}), 400
    if User.query.filter(or_(func.lower(User.email) == email, User.phone == phone if phone else False)).first():
        return jsonify({"ok": False, "message": "That clinic email or mobile number is already registered."}), 409
    employee = User(name=name[:100], email=email, phone=phone or None, role=role, approved=approved)
    employee.set_password(password); db.session.add(employee); db.session.commit()
    return jsonify({"ok": True, "message": f"{employee.name} was registered as {role.title()}. {'They can sign in now.' if approved else 'Approve their account before they can sign in.'}"})

@app.get("/api/admin/staff-reset-emails")
@login_required
@roles("admin")
def staff_reset_emails():
    staff = User.query.filter(User.role != "patient").order_by(User.role, User.name).all()
    return jsonify({"staff": [{"id": user.id, "name": user.name, "role": user.role, "email": user.email or "", "approved": bool(user.approved)} for user in staff]})

@app.post("/api/admin/staff/<int:user_id>/reset-email")
@login_required
@roles("admin")
def update_staff_reset_email(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if user.role == "patient": abort(400)
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify({"ok": False, "message": "Enter a valid clinic email address."}), 400
    existing = User.query.filter(User.email == email, User.id != user.id).first()
    if existing:
        return jsonify({"ok": False, "message": "That email is already assigned to another account."}), 409
    user.email = email
    db.session.commit()
    return jsonify({"ok": True, "email": email})

@app.route("/print/prescription/<int:prescription_id>")
@login_required
def prescription_print(prescription_id):
    rx = db.session.get(Prescription, prescription_id) or abort(404)
    if current_user.role == "patient" and rx.patient.user_id != current_user.id: abort(403)
    visit = Encounter.query.join(Appointment).filter(Appointment.patient_id == rx.patient_id, Appointment.doctor_id == rx.doctor_id).order_by(Appointment.scheduled_at.desc()).first()
    patient_age = None
    if rx.patient.dob:
        patient_age = date.today().year - rx.patient.dob.year - ((date.today().month, date.today().day) < (rx.patient.dob.month, rx.patient.dob.day))
    return render_template("print_prescription.html", rx=rx, visit=visit, patient_age=patient_age)

@app.route("/dietician/plan/<int:plan_id>/print")
@login_required
def diet_plan_print(plan_id):
    plan = db.session.get(DietPlan, plan_id) or abort(404)
    if current_user.role == "patient" and plan.patient.user_id != current_user.id: abort(403)
    if current_user.role not in ("admin", "dietician", "patient", "doctor"): abort(403)
    assessment = NutritionAssessment.query.filter_by(patient_id=plan.patient_id).order_by(NutritionAssessment.updated_at.desc()).first()
    meals = json.loads(plan.meals_json or "[]")
    macro_override = DietPlanMacroOverride.query.filter_by(plan_id=plan.id).first()
    meal_days = {}
    for meal in meals:
        day = meal.get("day", 1)
        meal_days.setdefault(day, {})[meal.get("meal")] = meal
    meal_rows = [{"day": day, "meals": meal_days[day]} for day in sorted(meal_days)]
    patient_age = None
    if plan.patient.dob:
        patient_age = date.today().year - plan.patient.dob.year - ((date.today().month, date.today().day) < (plan.patient.dob.month, plan.patient.dob.day))
    return render_template("print_diet_plan.html", plan=plan, assessment=assessment, macro_override=macro_override, meals=meals, meal_rows=meal_rows, patient_age=patient_age)

@app.route("/print/lab/<int:order_id>")
@login_required
def lab_print(order_id):
    order = db.session.get(LabOrder, order_id) or abort(404)
    if current_user.role == "patient":
        profile = patient_for_user(current_user)
        if not profile or profile.id != order.patient_id or order.status != "Finalised": abort(403)
    elif current_user.role == "doctor":
        if order.doctor_id != current_user.id:
            abort(403)
    elif current_user.role not in {"admin", "lab"}:
        abort(403)
    db.session.add(LabOrderAudit(order_id=order.id, actor_id=current_user.id, previous_status=order.status, new_status=order.status, action="Report viewed / printed")); db.session.commit()
    order = prepare_lab_order_display(order)
    return render_template("print_lab.html", order=order, verification_qr=report_qr_image(order) if order.status == "Finalised" else None)

@app.route("/print/invoice/<int:invoice_id>")
@login_required
@roles("pharmacy", "admin", "patient")
def invoice_print(invoice_id):
    invoice = db.session.get(Invoice, invoice_id) or abort(404)
    if current_user.role == "patient" and invoice.patient.user_id != current_user.id:
        abort(403)
    if current_user.role not in {"pharmacy", "admin", "patient"}:
        abort(403)
    bill_items = [{"name": line.medicine.name, "strength": line.medicine.strength, "quantity": line.quantity, "rate": line.unit_price, "discount": line.discount, "batch": line.batch} for line in invoice.pharmacy_lines]
    if not bill_items and invoice.category == "Pharmacy POS":
        match = re.search(r"Walk-in POS: (.*?) x (\d+)", invoice.description or "")
        if match:
            medicine = Medicine.query.filter_by(name=match.group(1)).first(); quantity = int(match.group(2))
            batch = MedicineBatch.query.filter_by(medicine_id=medicine.id).order_by(MedicineBatch.received_at.desc()).first() if medicine else None
            bill_items.append({"name": medicine.name if medicine else match.group(1), "strength": medicine.strength if medicine else "", "quantity": quantity, "rate": medicine.unit_price if medicine else invoice.amount / max(quantity, 1), "batch": batch})
    elif not bill_items and invoice.category == "Pharmacy":
        match = re.search(r"Prescription #(\d+)", invoice.description or "")
        rx = db.session.get(Prescription, int(match.group(1))) if match else None
        if rx:
            for line in rx.items:
                batch = MedicineBatch.query.filter_by(medicine_id=line.medicine_id).order_by(MedicineBatch.received_at.desc()).first()
                bill_items.append({"name": line.medicine.name, "strength": line.medicine.strength, "quantity": line.quantity, "rate": line.medicine.unit_price, "batch": batch})
    return render_template("print_invoice.html", invoice=invoice, bill_items=bill_items)

def seed():
    if IS_PRODUCTION:
        raise RuntimeError("Demo accounts must never be seeded in production.")
    if User.query.first(): return
    records = [("Clinic Admin", "admin@clinic.local", "admin", "Admin@123"), ("Dr. Meera Shah", "doctor@clinic.local", "doctor", "Doctor@123"), ("Reception Desk", "reception@clinic.local", "reception", "Reception@123"), ("Lab Technician", "lab@clinic.local", "lab", "Lab@123"), ("Pharmacy Desk", "pharmacy@clinic.local", "pharmacy", "Pharmacy@123"), ("Dietician", "diet@clinic.local", "dietician", "Diet@123")]
    users = []
    for name, email, role, password in records:
        u = User(name=name, email=email, role=role, approved=True); u.set_password(password); users.append(u)
    patient_user = User(name="Aarav Kumar", phone="9999999999", role="patient", approved=True); db.session.add_all(users + [patient_user]); db.session.flush()
    db.session.add(Patient(user_id=patient_user.id, mrn="CLN-0001", dob=date(1991, 5, 12), gender="Male", blood_group="B+", allergies="No known allergies"))
    db.session.add_all([Service(name="Consultation", category="Consultation", fee=500), Service(name="CBC", category="Lab", fee=300), Service(name="Blood Glucose", category="Lab", fee=150)])
    db.session.add_all([Medicine(name="Paracetamol", strength="500 mg", stock=150, reorder_level=30, unit_price=2), Medicine(name="Amoxicillin", strength="500 mg", stock=80, reorder_level=20, unit_price=12), Medicine(name="Pantoprazole", strength="40 mg", stock=8, reorder_level=15, unit_price=8)])
    db.session.commit()

def ensure_reference_data():
    medicines = [
        ("Glycomet GP 1/500", "Glimepiride 1 mg + Metformin SR 500 mg", 60, 15, 18),
        ("Telmikind 40", "Telmisartan 40 mg", 75, 20, 12),
        ("Amoxyclav 625", "Amoxicillin 500 mg + Clavulanic acid 125 mg", 45, 15, 22),
        ("Atorva 10", "Atorvastatin 10 mg", 90, 20, 9),
        ("Thyronorm 50", "Levothyroxine 50 mcg", 50, 15, 7),
        ("Glycomet GP 2/500", "Glimepiride 2 mg + Metformin SR 500 mg", 55, 15, 22),
        ("Janumet 50/500", "Sitagliptin 50 mg + Metformin 500 mg", 40, 12, 28),
        ("Voglibose M 0.2", "Voglibose 0.2 mg + Metformin 500 mg", 35, 10, 18),
        ("Teneligliptin M 20/500", "Teneligliptin 20 mg + Metformin 500 mg", 36, 10, 24),
        ("Telmikind H", "Telmisartan 40 mg + Hydrochlorothiazide 12.5 mg", 48, 12, 16),
        ("Telmikind AM", "Telmisartan 40 mg + Amlodipine 5 mg", 50, 12, 20),
        ("Amlodac AT", "Amlodipine 5 mg + Atenolol 50 mg", 42, 12, 15),
        ("Olmezest AM", "Olmesartan 20 mg + Amlodipine 5 mg", 38, 10, 22),
        ("Rosuvas F", "Rosuvastatin 10 mg + Fenofibrate 160 mg", 32, 10, 30),
        ("Atorva ASP", "Atorvastatin 10 mg + Aspirin 75 mg", 45, 12, 16),
        ("Ecosprin AV", "Aspirin 75 mg + Atorvastatin 10 mg", 44, 12, 14),
        ("Rabekind DSR", "Rabeprazole 20 mg + Domperidone SR 30 mg", 60, 15, 12),
        ("Pan D", "Pantoprazole 40 mg + Domperidone 30 mg", 55, 15, 12),
        ("Montair LC", "Montelukast 10 mg + Levocetirizine 5 mg", 48, 12, 14),
        ("Augmentin Duo", "Amoxicillin 400 mg + Clavulanic acid 57 mg", 30, 10, 20),
        ("Zerodol SP", "Aceclofenac 100 mg + Paracetamol 325 mg + Serratiopeptidase 15 mg", 30, 10, 12),
        ("Dolo 650", "Paracetamol 650 mg", 120, 30, 3),
        ("Neurobion Forte", "Methylcobalamin + Pyridoxine + Nicotinamide", 40, 12, 10),
        ("Calcium D3", "Calcium carbonate 500 mg + Vitamin D3 250 IU", 50, 15, 8),
        ("Metformin ER", "Metformin hydrochloride prolonged release 500 mg", 90, 20, 5),
        ("Sitagliptin", "Sitagliptin 100 mg", 40, 12, 22),
        ("Empagliflozin", "Empagliflozin 10 mg", 35, 10, 28),
        ("Amlodipine", "Amlodipine 5 mg", 75, 20, 4),
        ("Rosuvastatin", "Rosuvastatin 10 mg", 60, 15, 9),
        ("Losartan", "Losartan potassium 50 mg", 65, 18, 6),
        ("Cefixime", "Cefixime 200 mg", 45, 12, 14),
        ("Azithromycin", "Azithromycin 500 mg", 40, 12, 13),
        ("Cetirizine", "Cetirizine 10 mg", 80, 20, 3),
        ("Levocetirizine", "Levocetirizine 5 mg", 70, 20, 3),
        ("Ondansetron", "Ondansetron 4 mg", 45, 12, 5),
        ("Dicyclomine", "Dicyclomine 20 mg", 45, 12, 4),
        ("ORS", "Oral rehydration salts", 100, 25, 2),
        ("Cholecalciferol", "Vitamin D3 60,000 IU", 35, 10, 20),
        ("Methylcobalamin", "Methylcobalamin 1500 mcg", 45, 12, 11),
        ("Glipizide Metformin", "Glipizide 5 mg + Metformin 500 mg", 35, 10, 16),
        ("Dapagliflozin Metformin", "Dapagliflozin 10 mg + Metformin 1000 mg", 30, 10, 32),
        ("Linagliptin Metformin", "Linagliptin 2.5 mg + Metformin 500 mg", 30, 10, 30),
        ("Bisoprolol", "Bisoprolol 5 mg", 50, 15, 6),
        ("Metoprolol XL", "Metoprolol succinate prolonged release 25 mg", 50, 15, 6),
        ("Olmesartan", "Olmesartan medoxomil 20 mg", 45, 12, 9),
        ("Hydrochlorothiazide", "Hydrochlorothiazide 12.5 mg", 40, 12, 3),
        ("Clopidogrel", "Clopidogrel 75 mg", 60, 15, 7),
        ("Aspirin", "Aspirin 75 mg", 60, 15, 3),
        ("Ezetimibe", "Ezetimibe 10 mg", 35, 10, 11),
        ("Levofloxacin", "Levofloxacin 500 mg", 30, 10, 16),
        ("Doxycycline", "Doxycycline 100 mg", 35, 10, 8),
        ("Nimesulide Paracetamol", "Nimesulide 100 mg + Paracetamol 325 mg", 30, 10, 8),
        ("Ibuprofen Paracetamol", "Ibuprofen 400 mg + Paracetamol 325 mg", 35, 10, 7),
        ("Levocetirizine Montelukast", "Levocetirizine 5 mg + Montelukast 10 mg", 35, 10, 12),
        ("Budesonide Formoterol", "Budesonide 200 mcg + Formoterol 6 mcg", 25, 8, 190),
        ("Lactulose", "Lactulose solution 10 g / 15 mL", 25, 8, 120),
        ("Saccharomyces boulardii", "Probiotic 250 mg", 30, 10, 14),
    ]
    tests = [("HbA1c", 450), ("TSH", 300), ("Lipid Profile", 650), ("Renal Function Test", 800), ("Liver Function Test", 750), ("ESR", 150), ("CRP", 400), ("Urine Examination", 120), ("Electrolytes", 500), ("Complete Blood Count (CBC)", 300), ("Peripheral Smear", 250), ("Blood Grouping & Rh", 200), ("Fasting Blood Sugar", 100), ("Post Prandial Blood Sugar", 100), ("Random Blood Sugar", 100), ("Serum Insulin", 700), ("Free T3", 250), ("Free T4", 250), ("Thyroid Profile", 650), ("Vitamin D3", 900), ("Vitamin B12", 700), ("Serum Ferritin", 500), ("Serum Iron Studies", 650), ("Calcium", 180), ("Magnesium", 250), ("Uric Acid", 180), ("Creatinine", 150), ("Blood Urea", 150), ("Urine Culture", 650), ("Stool Examination", 150), ("Stool Occult Blood", 200), ("HIV 1 & 2", 450), ("HBsAg", 350), ("Anti-HCV", 500), ("Dengue NS1 Antigen", 700), ("Malaria Parasite", 250), ("Troponin I", 900), ("ECG", 300), ("Chest X-Ray", 500), ("Ultrasound Abdomen", 1200)]
    changed = False
    for name, strength, stock, reorder, price in medicines:
        if not Medicine.query.filter_by(name=name).first():
            db.session.add(Medicine(name=name, strength=strength, stock=stock, reorder_level=reorder, unit_price=price)); changed = True
    foods = [
        ("Idli with sambar", "South Indian", True, "2 idlis + 1 bowl sambar", 260, 10, 48, 4, 7, "diabetes-friendly,low-sodium"),
        ("Pesarattu", "Telugu", True, "2 medium pesarattu", 280, 15, 42, 6, 8, "high-protein,diabetes-friendly"),
        ("Vegetable oats upma", "South Indian", True, "1 medium bowl", 250, 8, 42, 6, 6, "high-fibre"),
        ("Brown rice dal bowl", "Indian", True, "1 cup rice + 1 bowl dal", 410, 15, 70, 7, 10, "high-fibre"),
        ("Millet vegetable bowl", "Telugu", True, "1 medium bowl", 360, 12, 60, 8, 9, "diabetes-friendly,high-fibre"),
        ("Curd vegetable salad", "Indian", True, "1 bowl", 180, 9, 18, 7, 5, "high-protein"),
        ("Fruit and nuts", "Indian", True, "1 fruit + 10 almonds", 190, 5, 24, 9, 5, "snack"),
        ("Egg vegetable omelette", "Indian", False, "2 eggs", 230, 16, 7, 15, 2, "high-protein"),
        ("Grilled chicken with vegetables", "Indian", False, "120 g chicken + vegetables", 310, 34, 15, 12, 6, "high-protein"),
        ("Fish curry with millet", "Coastal Andhra", False, "1 bowl", 390, 28, 42, 12, 5, "high-protein"),
        ("Moong dal soup", "Indian", True, "1 bowl", 180, 11, 28, 3, 7, "high-protein,low-sodium"),
        ("Buttermilk", "Indian", True, "1 glass", 70, 4, 8, 2, 0, "low-sodium"),
    ]
    for name, cuisine, vegetarian, serving, calories, protein, carbs, fat, fibre, tags in foods:
        if not FoodMaster.query.filter_by(name=name).first():
            db.session.add(FoodMaster(name=name, category="Clinic food", cuisine=cuisine, vegetarian=vegetarian, serving=serving, calories=calories, protein=protein, carbohydrates=carbs, fat=fat, fibre=fibre, tags=tags)); changed = True
    parameter_sets = {"Complete Blood Count (CBC)": [("Haemoglobin", "g/dL", "13.0 - 17.0"), ("Total WBC Count", "cells/cumm", "4000 - 11000"), ("RBC Count", "million/cumm", "4.5 - 5.5"), ("Platelet Count", "lakh/cumm", "1.5 - 4.5")], "CBC": [("Haemoglobin", "g/dL", "13.0 - 17.0"), ("Total WBC Count", "cells/cumm", "4000 - 11000"), ("RBC Count", "million/cumm", "4.5 - 5.5"), ("Platelet Count", "lakh/cumm", "1.5 - 4.5")], "Fasting Blood Sugar": [("Glucose", "mg/dL", "70 - 99")], "Post Prandial Blood Sugar": [("Glucose", "mg/dL", "70 - 140")], "Random Blood Sugar": [("Glucose", "mg/dL", "70 - 140")], "Blood Glucose": [("Glucose", "mg/dL", "70 - 140")], "HbA1c": [("HbA1c", "%", "4.0 - 5.6")], "Lipid Profile": [("Total Cholesterol", "mg/dL", "0 - 200"), ("Triglycerides", "mg/dL", "0 - 150"), ("HDL Cholesterol", "mg/dL", "40 - 100"), ("LDL Cholesterol", "mg/dL", "0 - 100")], "Renal Function Test": [("Blood Urea", "mg/dL", "15 - 40"), ("Creatinine", "mg/dL", "0.6 - 1.2"), ("Uric Acid", "mg/dL", "3.5 - 7.2")], "Liver Function Test": [("Total Bilirubin", "mg/dL", "0.3 - 1.2"), ("SGOT / AST", "U/L", "0 - 40"), ("SGPT / ALT", "U/L", "0 - 41"), ("Alkaline Phosphatase", "U/L", "44 - 147")], "Thyroid Profile": [("TSH", "uIU/mL", "0.4 - 4.0"), ("Free T3", "pg/mL", "2.0 - 4.4"), ("Free T4", "ng/dL", "0.8 - 1.8")], "Electrolytes": [("Sodium", "mEq/L", "135 - 145"), ("Potassium", "mEq/L", "3.5 - 5.0"), ("Chloride", "mEq/L", "98 - 107")], "Urine Examination": [("Protein", "", "Negative"), ("Sugar", "", "Negative"), ("Pus Cells", "/HPF", "0 - 5"), ("RBC", "/HPF", "0 - 2")], "ESR": [("ESR", "mm/hr", "0 - 20")], "CRP": [("CRP", "mg/L", "0 - 6")], "Vitamin D3": [("25-OH Vitamin D", "ng/mL", "30 - 100")], "Vitamin B12": [("Vitamin B12", "pg/mL", "200 - 900")], "Serum Ferritin": [("Ferritin", "ng/mL", "30 - 400")]}
    for test_name, parameters in parameter_sets.items():
        for index, (name, unit, ref) in enumerate(parameters):
            if not LabTestParameter.query.filter_by(test_name=test_name, name=name).first(): db.session.add(LabTestParameter(test_name=test_name, name=name, unit=unit, reference_range=ref, display_order=index)); changed = True
    for name, fee in tests:
        if not Service.query.filter_by(name=name).first():
            db.session.add(Service(name=name, category="Lab", fee=fee)); changed = True
    lab_stock = [("CBC Reagent Kit", "Reagent", "LAB-REA-001", 2, 3, "kits", "Cold storage"), ("Glucose Reagent", "Reagent", "LAB-REA-002", 8, 5, "bottles", "Cold storage"), ("EDTA Vacutainer", "Sample collection", "LAB-SAM-001", 45, 50, "tubes", "Collection room"), ("Plain Vacutainer", "Sample collection", "LAB-SAM-002", 70, 50, "tubes", "Collection room"), ("Urine Containers", "Sample collection", "LAB-SAM-003", 30, 40, "containers", "Collection room"), ("Disposable Syringes 5 mL", "Consumable", "LAB-CON-001", 90, 100, "pieces", "Store room"), ("Gloves", "PPE", "LAB-PPE-001", 6, 10, "boxes", "Store room"), ("Microscope Slides", "Consumable", "LAB-CON-002", 120, 100, "slides", "Store room")]
    for name, category, sku, quantity, reorder, unit, location in lab_stock:
        if not LabInventoryItem.query.filter_by(sku=sku).first():
            db.session.add(LabInventoryItem(name=name, category=category, sku=sku, quantity=quantity, reorder_level=reorder, unit=unit, location=location)); changed = True
    templates = [
        ("Diabetes Follow-up", "Diabetes", "Glycomet GP 1/500|1-0-1|30 days|60|After food\nAtorva 10|0-0-1|30 days|30|At bedtime", "Monitor blood glucose, follow diabetic diet and bring glucose records for review."),
        ("Hypertension Follow-up", "Hypertension", "Telmikind 40|1-0-0|30 days|30|After breakfast", "Monitor blood pressure regularly, reduce salt intake and continue daily activity."),
        ("Thyroid Follow-up", "Thyroid", "Thyronorm 50|1-0-0|30 days|30|Empty stomach", "Take on an empty stomach and keep a 30-minute gap before food."),
        ("Viral Fever Support", "General Medicine", "Paracetamol|1-0-1|3 days|6|After food", "Maintain hydration. Seek urgent care for persistent fever, breathing difficulty or worsening symptoms."),
        ("Gastritis / Acidity", "General Medicine", "Pantoprazole|1-0-0|14 days|14|Empty stomach", "Avoid late meals, spicy foods, alcohol and excess caffeine."),
        ("BP Combination Follow-up", "Hypertension", "Telmikind AM|1-0-0|30 days|30|After breakfast\nAtorva 10|0-0-1|30 days|30|At bedtime", "Monitor home blood pressure, limit salt and bring readings for review."),
        ("Diabetes Triple Therapy Review", "Diabetes", "Glycomet GP 2/500|1-0-1|30 days|60|After food\nVoglibose M 0.2|1-1-1|30 days|90|Before meals", "Monitor glucose closely and report symptoms of hypoglycaemia promptly."),
        ("Allergy / Rhinitis Support", "General Medicine", "Montair LC|0-0-1|7 days|7|At bedtime", "Avoid known triggers. Seek review for wheeze, breathing difficulty or persistent symptoms."),
        ("Weight Management Follow-up", "Lifestyle", "Thyronorm 50|1-0-0|30 days|30|Empty stomach", "Follow the diet plan, record weekly weight, walk regularly and sleep 7–8 hours."),
    ]
    for name, category, items_spec, advice in templates:
        if not PrescriptionTemplate.query.filter_by(name=name).first():
            db.session.add(PrescriptionTemplate(name=name, category=category, items_spec=items_spec, advice=advice)); changed = True
    if changed: db.session.commit()

if not IS_PRODUCTION or os.getenv("CLINIC_AUTO_CREATE_SCHEMA", "false").lower() == "true":
    with app.app_context():
        db.create_all()
        if not IS_PRODUCTION:
            apply_development_schema_updates()

if not IS_PRODUCTION or os.getenv("CLINIC_BOOTSTRAP_REFERENCE_DATA", "false").lower() == "true":
    with app.app_context():
        seed()
        ensure_reference_data()

if __name__ == "__main__":
    # Flask's development server is deliberately limited to local use.
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=not IS_PRODUCTION)
