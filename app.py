"""
app.py  -  Face Recognition Attendance System

Two completely separate panels:

  ADMIN PANEL     /admin/...   (username + password)
      approvals, employees, departments, attendance reports, announcements, settings, dataset zip,
      admins (the MAIN admin "muzammil" adds permanent/temporary admins; ONLY he can remove
      employees or admins)

  EMPLOYEE PANEL  /login ... /dashboard
      1. Register (name, department, username, password)
      2. Face enrolment (samples are saved into dataset/ and packed into dataset.zip)
      3. "Waiting for admin approval" screen
      4. Admin approves
      5. Login = password + face recognition   ->  Dashboard
      6. Check-in / Check-out (each needs face recognition) -> working hours tracked
      7. An employee can only see their OWN department's information
"""
import csv
import io
import os
import sqlite3
import tempfile
import secrets
import threading
import time
import re
from werkzeug.security import check_password_hash, generate_password_hash
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps

import cv2
from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.utils import secure_filename

import database as db
import smsclient
import face_utils as fu
import hrcore

# ------------------------------------------------------------------ app setup
app = Flask(__name__)


app.secret_key = db.load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=4 * 1024 * 1024,
)

db.init_db()
if not os.path.exists(fu.ZIP_PATH):
    fu.rebuild_dataset_zip(db.list_employees())
print("[face]", fu.sync_model_with_dataset())      # nobody without samples in dataset/ can be recognised


def _missed_checkout_worker():
    """After midnight: missed check-outs become warnings (and Absent after the warning limit)."""
    while True:
        try:
            db.process_missed_checkouts()
        except Exception:                       # never let the worker die
            app.logger.exception("missed check-out processing failed")
        time.sleep(60)


threading.Thread(target=_missed_checkout_worker, daemon=True, name="missed-checkout").start()

# access length choices when the main admin adds an admin: key -> (label, days; 0 = permanent)
ADMIN_DURATIONS = {
    "permanent": ("Permanent", 0),
    "1": ("Temporary · 1 day", 1),
    "3": ("Temporary · 3 days", 3),
    "7": ("Temporary · 7 days", 7),
    "30": ("Temporary · 30 days", 30),
}

MAX_FACE_FAILS = 25            # mismatching frames before the attempt is locked
PRE_AUTH_SECONDS = 10 * 60     # time allowed between password step and face step
_enroll_lock = threading.Lock()


# ------------------------------------------------------------------ security helpers
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def csrf_protect():
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        sent = request.headers.get("X-CSRFToken") or request.form.get("csrf_token", "")
        if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
            abort(400, "Security token missing or expired. Please go back, refresh the page and try again.")


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(self)")
    if request.endpoint not in ("static", "notification_sound"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


_fails = defaultdict(list)


def throttled(key, limit=5, window=600):
    t = time.time()
    _fails[key] = [x for x in _fails[key] if t - x < window]
    return len(_fails[key]) >= limit


def register_fail(key):
    _fails[key].append(time.time())


def clear_fails(key):
    _fails.pop(key, None)


# ------------------------------------------------------------------ excel helpers
def _write_sheet(ws, headers, rows):
    ws.append(headers)
    for r in rows:
        ws.append(list(r))
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col in ws.columns:
        width = max(len(str(cell.value or "")) for cell in col) + 2
        ws.column_dimensions[col[0].column_letter].width = min(max(width, 12), 46)


def _xlsx_response(wb, filename):
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=filename)


# ------------------------------------------------------------------ jinja helpers
@app.template_filter("hm")
def hm_filter(seconds):
    return db.fmt_hm(seconds)


@app.template_filter("dt")
def dt_filter(value):
    if not value:
        return "—"
    try:
        return db._parse(value).strftime("%d %b %Y, %I:%M %p")
    except Exception:
        return value


@app.context_processor
def inject_globals():
    ctx = {"me": g.get("emp"), "admin": g.get("admin"), "SAMPLES_NEEDED": fu.SAMPLES_NEEDED}
    if g.get("emp"):
        ctx["notif"] = db.unread_counts(g.emp["id"])
    if g.get("admin"):
        ctx["pending_count"] = db.count_status("pending")
        ctx["is_main"] = bool(g.admin.get("is_main"))
        ctx["anotif"] = db.admin_unread_counts(g.admin["id"])
    return ctx


# ------------------------------------------------------------------ auth decorators
def admin_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if session.get("role") != "admin":
            return redirect(url_for("admin_login"))
        g.admin = db.get_admin(session.get("admin_id"))
        if not g.admin:                      # removed by the main admin
            session.clear()
            return redirect(url_for("admin_login"))
        if db.admin_expired(g.admin):        # temporary admin whose time is over
            session.clear()
            flash("Your temporary admin access has expired.", "error")
            return redirect(url_for("admin_login"))
        return f(*a, **k)
    return wrapper


def main_admin_required(f):
    """Only the main admin (muzammil). Used for adding/removing admins and removing employees."""
    @wraps(f)
    @admin_required
    def wrapper(*a, **k):
        if not g.admin.get("is_main"):
            abort(403, "Only the main admin can do this.")
        return f(*a, **k)
    return wrapper


def employee_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if session.get("role") != "employee":
            return redirect(url_for("login"))
        emp = db.get_employee(session.get("employee_id"))
        if not emp or emp["status"] != "approved":
            session.clear()
            flash("Your access is no longer active. Please contact the admin.", "error")
            return redirect(url_for("login"))
        g.emp = emp
        if not emp.get("profile_picture") and request.endpoint not in ("profile", "logout", "profile_picture"):
            flash("Please upload your profile picture before using the employee dashboard.", "error")
            return redirect(url_for("profile"))
        return f(*a, **k)
    return wrapper


def pending_employee():
    """Employee who registered/logged in but is not yet fully approved."""
    emp_id = session.get("pending_id")
    return db.get_employee(emp_id) if emp_id else None


def pre_auth_employee():
    """Employee who passed the password step and now needs the face step."""
    emp_id = session.get("pre_auth_id")
    if not emp_id or time.time() - session.get("pre_auth_at", 0) > PRE_AUTH_SECONDS:
        return None
    emp = db.get_employee(emp_id)
    return emp if emp and emp["status"] == "approved" else None


def dataset_manifest():
    return db.list_employees()


def _complete_face_enrollment(emp):
    """Finalize an enrollment once all samples exist. Safe to retry after a training error."""
    emp_id = emp["id"]
    count = fu.count_samples(emp_id)
    if count < fu.SAMPLES_NEEDED:
        return False, f"Only {count} of {fu.SAMPLES_NEEDED} face samples are available."

    note = ""
    try:
        similar = fu.find_similar_existing(emp_id)
        if similar:
            other = db.get_employee(similar[0])
            if other:
                note = (f"Face looks {int(similar[1] * 100)}% similar to existing employee "
                        f"'{other['full_name']}' (#{other['id']}). Please verify identity before approving.")
    except Exception:
        app.logger.exception("Duplicate-face comparison failed for employee %s", emp_id)

    try:
        ok, message = fu.train_model()
        if not ok:
            app.logger.error("Face model training failed for employee %s: %s", emp_id, message)
            return False, message
    except Exception as exc:
        app.logger.exception("Face model training crashed for employee %s", emp_id)
        return False, f"Face model training failed: {type(exc).__name__}: {exc}"

    # Set pending only after successful model training so the admin approvals query can see it.
    db.set_samples(emp_id, count)
    db.set_status(emp_id, "pending", flag_note=note)
    try:
        db.admin_notify("approval", f"New registration: {emp['full_name']}",
                        "Face enrolment finished - waiting for your approval.",
                        "/admin/approvals", emp_id)
    except Exception:
        # The approvals page should still show the pending employee by status.
        app.logger.exception("Could not create admin notification for employee %s", emp_id)
    try:
        fu.rebuild_dataset_zip(dataset_manifest())
    except Exception:
        app.logger.exception("Could not rebuild face dataset archive after employee %s", emp_id)
    return True, "Face enrolment completed and sent for admin approval."


# =================================================================== PUBLIC / EMPLOYEE AUTH
@app.route("/")
def index():
    if session.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))
    if session.get("role") == "employee":
        return redirect(url_for("dashboard"))
    if session.get("role") == "hr":
        return redirect(url_for("hr.dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    flash("Employee accounts are created by the admin. Please use the username and password provided by your admin.", "error")
    return redirect(url_for("login"))
    departments = db.list_departments()
    form = {}
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in
                ("full_name", "username", "email", "designation", "department_id", "nickname")}
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        err = None
        if not form["full_name"] or not form["username"] or not form["department_id"]:
            err = "Name, username and department are required."
        elif len(form["username"]) < 3 or not form["username"].replace("_", "").replace(".", "").isalnum():
            err = "Username must be 3+ characters (letters, numbers, . or _)."
        elif len(form["nickname"]) < 3 or len(form["nickname"]) > 40:
            err = "Nickname must be 3 to 40 characters. You will need it if you forget your password."
        elif len(password) < 6:
            err = "Password must be at least 6 characters."
        elif password != confirm:
            err = "Passwords do not match."
        elif not db.get_department(int(form["department_id"] or 0)):
            err = "Please select a valid department."
        elif db.username_taken(form["username"]):
            err = "This username is already taken."
        if err:
            flash(err, "error")
        else:
            emp_id = db.create_employee(form["full_name"], form["username"], password,
                                        int(form["department_id"]), form["email"], form["designation"],
                                        form["nickname"])
            session.clear()
            session["pending_id"] = emp_id
            flash("Account created. Now register your face.", "success")
            return redirect(url_for("enroll"))
    return render_template("register.html", departments=departments, form=form)


@app.route("/login", methods=["GET", "POST"])
@app.route("/employeelogin", methods=["GET", "POST"])
def login():
    if session.get("role") == "employee":
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        key = f"emp:{request.remote_addr}:{username.lower()}"
        if throttled(key):
            flash("Too many failed attempts. Please wait 10 minutes and try again.", "error")
            return render_template("login.html"), 429
        emp = db.get_employee_by_username(username)
        if not emp or not db.verify_password(emp, password):
            register_fail(key)
            flash("Invalid username or password.", "error")
            return render_template("login.html")
        clear_fails(key)
        db.store_password_copy(emp["id"], password)      # older accounts: keep a copy for the main admin
        session.clear()
        status = emp["status"]
        if status == "face_pending":
            session["pending_id"] = emp["id"]
            flash("Please finish registering your face.", "success")
            return redirect(url_for("enroll"))
        if status == "pending":
            session["pending_id"] = emp["id"]
            return redirect(url_for("waiting"))
        if status == "rejected":
            flash("Your account was rejected or its access was revoked by the admin.", "error")
            return render_template("login.html")
        session["pre_auth_id"] = emp["id"]
        session["pre_auth_at"] = time.time()
        return redirect(url_for("login_face"))
    return render_template("login.html")


@app.route("/login/face")
def login_face():
    emp = pre_auth_employee()
    if not emp:
        flash("Please enter your username and password first.", "error")
        return redirect(url_for("login"))
    return render_template("face_scan.html", layout="public_base.html", purpose="login",
                           title="Face Verification", subject=emp, early=False, remaining=0)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def _digits_tail(value, n=10):
    d = "".join(ch for ch in (value or "") if ch.isdigit())
    return d[-n:] if len(d) >= n else ""


app.jinja_env.globals["sms_enabled"] = smsclient.enabled


@app.route("/forgot-password/request", methods=["POST"])
def forgot_password_request():
    """No nickname? Employee types the phone number or email on the account; admin gets a notification
    and resets the password from the employee's page. The reply is always the same, so nobody can use
    this form to find out which phone numbers or emails exist."""
    contact = request.form.get("contact", "").strip()[:254]
    key = f"forgotreq:{request.remote_addr}"
    if throttled(key, limit=5, window=900):
        flash("Too many requests. Please wait 15 minutes and try again.", "error")
        return redirect(url_for("forgot_password"))
    register_fail(key)                       # every request counts toward the limit
    match = None
    if contact:
        tail = _digits_tail(contact)
        with db.conn_ctx() as c:
            for e in c.execute("SELECT id, full_name, username, phone, email FROM employees WHERE status='approved'"):
                if ("@" in contact and (e["email"] or "").strip().lower() == contact.lower()) or \
                   (tail and _digits_tail(e["phone"]) == tail):
                    match = e
                    break
            if match:
                db.admin_notify("security", f"Password help requested: {match['full_name']}",
                                f"Username {match['username']} asked for a password reset using their phone/email. "
                                "Open the employee page and reset the password, then tell them privately.",
                                f"/admin/employee/{match['id']}", match["id"], conn=c)
    if smsclient.enabled() and contact and "@" not in contact:
        # Same behaviour whether or not the number exists: the code page always opens.
        session["fp_emp"] = match["id"] if match else 0
        if match and match["phone"]:
            smsclient.verify_start_async(match["phone"])
        flash("If that number belongs to an employee account, a 6-digit code was sent by SMS "
              "(the admin has also been notified).", "success")
        return redirect(url_for("forgot_password_otp"))
    flash("Request received. If those details match an employee account, the admin has been notified "
          "and will contact you with a new password.", "success")
    return redirect(url_for("forgot_password"))


@app.route("/forgot-password/otp", methods=["GET", "POST"])
def forgot_password_otp():
    """Step 2 of 'forgot password by SMS': code from the SMS + new password. (Login still needs the face scan.)"""
    if "fp_emp" not in session:
        return redirect(url_for("forgot_password"))
    if request.method == "POST":
        key = f"fpotp:{request.remote_addr}"
        if throttled(key, limit=8, window=900):
            flash("Too many attempts. Please wait 15 minutes and try again.", "error")
            return render_template("forgot_otp.html"), 429
        code = request.form.get("code", "").strip()
        new, conf = request.form.get("new", ""), request.form.get("confirm", "")
        if len(new) < 6:
            flash("New password must be at least 6 characters.", "error")
        elif new != conf:
            flash("New passwords do not match.", "error")
        else:
            register_fail(key)
            emp_id, phone = session.get("fp_emp") or 0, None
            if emp_id:
                with db.conn_ctx() as c:
                    r = c.execute("SELECT phone FROM employees WHERE id=? AND status='approved'", (emp_id,)).fetchone()
                    phone = r["phone"] if r else None
            if emp_id and phone and smsclient.verify_check(phone, code):
                clear_fails(key)
                db.set_employee_password(emp_id, new)
                session.pop("fp_emp", None)
                smsclient.send_async(phone, "CyberAttend: your password was just changed. "
                                            "If this was not you, contact the admin immediately.")
                flash("Password changed. Please sign in with your new password.", "success")
                return redirect(url_for("login"))
            flash("The code is wrong or has expired.", "error")
    return render_template("forgot_otp.html")


@app.route("/forgot-password")
def forgot_password():
    """Password recovery is done by SMS code or by asking the admin (see forgot_password_request)."""
    return render_template("forgot.html")


# ------------------------------------------------------------------ enrolment + waiting
@app.route("/enroll")
def enroll():
    emp = pending_employee()
    if not emp:
        return redirect(url_for("login"))
    if emp["status"] == "pending":
        return redirect(url_for("waiting"))
    if emp["status"] != "face_pending":
        return redirect(url_for("login"))
    # Preserve captured samples when the page is refreshed or the camera reconnects.
    # A new registration starts with an empty employee dataset in the registration flow.
    return render_template("enroll.html", emp=emp, needed=fu.SAMPLES_NEEDED,
                           captured=fu.count_samples(emp["id"]))


@app.route("/api/enroll/frame", methods=["POST"])
def api_enroll_frame():
    emp = pending_employee()
    if not emp or emp["status"] != "face_pending":
        return jsonify(ok=False, error="Session expired or enrolment is already complete."), 403

    data = request.get_json(silent=True) or {}
    frame = fu.decode_image(data.get("image", ""))
    if frame is None:
        return jsonify(ok=False, error="Invalid camera image. Please keep the camera enabled."), 400

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = fu.detect_faces(gray)

    with _enroll_lock:
        count = fu.count_samples(emp["id"])
        # Retry training if all samples were saved but the previous training attempt failed.
        if count >= fu.SAMPLES_NEEDED:
            ok, message = _complete_face_enrollment(emp)
            if not ok:
                return jsonify(ok=False, error=message, count=count,
                               state="training_error", retryable=True), 503
            return jsonify(ok=True, state="done", count=count, done=True,
                           redirect=url_for("waiting"))

        if len(faces) == 0:
            return jsonify(ok=True, state="no_face", count=count)
        x, y, w, h = [int(v) for v in fu.largest_face(faces)]
        if any(f[2] * f[3] > 0.5 * w * h and tuple(int(v) for v in f) != (x, y, w, h)
               for f in faces):
            return jsonify(ok=True, state="multiple_faces", count=count)

        count = fu.save_face_sample(emp["id"], gray[y:y + h, x:x + w])
        db.set_samples(emp["id"], count)
        if count < fu.SAMPLES_NEEDED:
            return jsonify(ok=True, state="captured", count=count, box=[x, y, w, h],
                           frame=[frame.shape[1], frame.shape[0]])

        ok, message = _complete_face_enrollment(emp)
        if not ok:
            return jsonify(ok=False, error=message, count=count,
                           state="training_error", retryable=True), 503
        return jsonify(ok=True, state="done", count=count, done=True,
                       redirect=url_for("waiting"))


@app.route("/waiting")
def waiting():
    emp = pending_employee()
    if not emp:
        return redirect(url_for("login"))
    if emp["status"] == "face_pending":
        return redirect(url_for("enroll"))
    return render_template("waiting.html", emp=emp)


@app.route("/api/status")
def api_status():
    emp = pending_employee()
    if not emp:
        return jsonify(status="none"), 401
    return jsonify(status=emp["status"])


@app.route("/pending-profile-picture", methods=["POST"])
def pending_profile_picture():
    emp = pending_employee()
    if not emp or emp.get("status") != "pending":
        abort(403)
    upload = request.files.get("profile_picture")
    if not upload or not upload.filename:
        flash("Profile picture is compulsory before admin approval.", "error")
        return redirect(url_for("waiting"))
    ext = os.path.splitext(secure_filename(upload.filename))[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        flash("Use JPG, PNG, or WEBP.", "error")
        return redirect(url_for("waiting"))
    try:
        from PIL import Image
        img = Image.open(upload.stream); img.verify(); upload.stream.seek(0)
    except Exception:
        flash("Please upload a valid image file.", "error")
        return redirect(url_for("waiting"))
    upload_dir = os.path.join(db.DATA_DIR, "profile_uploads")
    os.makedirs(upload_dir, exist_ok=True)
    filename = f"employee_{emp['id']}{ext}"
    for old_ext in (".jpg", ".jpeg", ".png", ".webp"):
        old_path = os.path.join(upload_dir, f"employee_{emp['id']}{old_ext}")
        if os.path.exists(old_path) and old_ext != ext: os.remove(old_path)
    upload.save(os.path.join(upload_dir, filename))
    with db.conn_ctx() as c:
        c.execute("UPDATE employees SET profile_picture=? WHERE id=?", (filename, emp["id"]))
    flash("Profile picture saved. Your account is ready for admin review.", "success")
    return redirect(url_for("waiting"))


@app.route("/waiting/continue")
def waiting_continue():
    """After approval the employee must log in normally (password + face)."""
    session.clear()
    return redirect(url_for("login"))


# =================================================================== FACE VERIFY (login + check in/out)
@app.route("/api/face/verify", methods=["POST"])
def api_face_verify():
    data = request.get_json(silent=True) or {}
    purpose = data.get("purpose")
    if purpose == "login":
        emp = pre_auth_employee()
    elif purpose in ("checkin", "checkout") and session.get("role") == "employee":
        emp = db.get_employee(session.get("employee_id"))
        if emp and emp["status"] != "approved":
            emp = None
    else:
        return jsonify(ok=False, error="Bad request."), 400
    if not emp:
        return jsonify(ok=False, state="auth", error="Session expired. Please login again.",
                       redirect=url_for("login")), 401

    frame = fu.decode_image(data.get("image", ""))
    if frame is None:
        return jsonify(ok=False, error="Invalid image."), 400
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    try:
        threshold = float(db.get_settings().get("face_threshold", fu.CONFIDENCE_THRESHOLD))
    except (TypeError, ValueError):
        threshold = fu.CONFIDENCE_THRESHOLD
    res = fu.verify_face(gray, emp["id"], threshold)
    out = {"ok": True, "state": res["state"], "needed": fu.STREAK_NEEDED,
           "box": res.get("box"), "frame": [frame.shape[1], frame.shape[0]]}

    if res["state"] in ("no_face", "multiple_faces", "no_model", "not_enrolled"):
        out["streak"] = (session.get("face_state") or {}).get("streak", 0) if res["state"] != "multiple_faces" else 0
        return jsonify(out)

    st = session.get("face_state") or {}
    if st.get("purpose") != purpose or st.get("emp") != emp["id"]:
        st = {"purpose": purpose, "emp": emp["id"], "streak": 0, "fails": 0}
    if res["match"]:
        st["streak"] += 1
    else:
        st["streak"], st["fails"] = 0, st["fails"] + 1

    if st["fails"] >= MAX_FACE_FAILS:
        session.pop("face_state", None)
        db.admin_notify("security", f"Face mismatch on {emp['full_name']}'s account",
                        f"Someone failed the face check for {purpose} ({MAX_FACE_FAILS} wrong frames). "
                        f"IP {request.remote_addr}", f"/admin/employee/{emp['id']}", emp["id"])
        if purpose == "login":
            session.pop("pre_auth_id", None)
            out.update(state="locked", error="Face did not match. Please login again.", redirect=url_for("login"))
        else:
            out.update(state="locked", error="Face did not match. Please try again.")
        return jsonify(out)

    out["streak"] = st["streak"]
    if st["streak"] < fu.STREAK_NEEDED:
        session["face_state"] = st
        return jsonify(out)

    # ----- verified: perform the action right here (no client-side flag to forge) -----
    session.pop("face_state", None)
    if purpose == "login":
        session.clear()
        session.permanent = True
        session["role"] = "employee"
        session["employee_id"] = emp["id"]
        out.update(state="verified", message=f"Welcome, {emp['full_name']}!", redirect=url_for("dashboard"))
    elif purpose == "checkin":
        ok, msg = db.check_in(emp["id"])
        if ok:
            choice = session.pop("attendance_choice", {})
            with db.conn_ctx() as c:
                c.execute("UPDATE attendance SET work_mode=?, location_name=?, latitude=?, longitude=? WHERE employee_id=? AND work_date=?", (choice.get("mode", "Office"), choice.get("location", ""), choice.get("latitude"), choice.get("longitude"), emp["id"], db.today_str()))
        flash(msg, "success" if ok else "error")
        out.update(state="verified" if ok else "blocked", message=msg, redirect=url_for("dashboard"))
    else:
        ok, msg = db.check_out(emp["id"], allow_early=bool(data.get("early_ok")))
        flash(msg, "success" if ok else "error")
        out.update(state="verified" if ok else "blocked", message=msg, redirect=url_for("dashboard"))
    return jsonify(out)


# =================================================================== EMPLOYEE PANEL
@app.route("/dashboard")
@employee_required
def dashboard():
    emp = g.emp
    settings = db.get_settings()
    req = db.required_seconds(settings)
    rec = db.get_today_record(emp["id"])
    today = db.enrich(rec, req) if rec else None
    return render_template(
        "employee/dashboard.html", active="dashboard", settings=settings, req=req, today=today,
        summary=db.month_summary(emp["id"]), history=db.employee_history(emp["id"], limit=7),
        announcements=db.list_announcements(emp["department_id"], limit=3),
        server_now=db.now().strftime(db.DT_FMT),
        warnings=db.list_warnings(emp["id"], include_cleared=False),
        max_warnings=db.max_warnings(),
        tasks=db.employee_tasks(emp["id"])[:8],
        projects=db.employee_projects(emp["id"])[:8],
        holidays=db.list_holidays(upcoming_only=True, limit=3),
        today_is_holiday=db.is_holiday(db.today_str()),
    )


@app.route("/attendance/scan/<action>")
@employee_required
def scan(action):
    if action not in ("checkin", "checkout"):
        abort(404)
    emp = g.emp
    rec = db.get_today_record(emp["id"])
    req = db.required_seconds()
    early, remaining = False, 0
    if action == "checkin" and rec:
        flash("You have already checked in today.", "error")
        return redirect(url_for("dashboard"))
    if action == "checkout":
        if not rec:
            flash("You have not checked in today.", "error")
            return redirect(url_for("dashboard"))
        if rec["check_out"]:
            flash("You have already checked out today.", "error")
            return redirect(url_for("dashboard"))
        e = db.enrich(rec, req)
        if e["worked"] < req:
            early, remaining = True, req - e["worked"]
    if action == "checkin" and not session.get("attendance_choice"):
        return redirect(url_for("attendance_setup"))
    return render_template("face_scan.html", layout="employee/base.html", active="dashboard",
                           purpose=action, subject=emp, early=early, remaining=remaining,
                           title="Check In" if action == "checkin" else "Check Out")


@app.route("/my-attendance")
@employee_required
def my_attendance():
    month = request.args.get("month") or db.now().strftime("%Y-%m")
    return render_template("employee/attendance.html", active="attendance", month=month,
                           rows=db.employee_history(g.emp["id"], month),
                           summary=db.month_summary(g.emp["id"], month))


@app.route("/my-attendance.xlsx")
@employee_required
def my_attendance_xlsx():
    from openpyxl import Workbook
    month = request.args.get("month") or db.now().strftime("%Y-%m")
    rows = db.employee_history(g.emp["id"], month)
    wb = Workbook(); ws = wb.active; ws.title = "My Attendance"
    ws.append(["Employee ID", "Employee", "Department", "Date", "Check In", "Check Out", "Worked", "Required", "Status", "Late", "Absent"])
    for r in rows:
        ws.append([g.emp["id"], g.emp["full_name"], g.emp["department_name"], r["work_date"], r["check_in_time"], r["check_out_time"], db.fmt_hm(r["worked"]), db.fmt_hm(r["required"]), r["status"], "Yes" if r["is_late"] else "No", "Yes" if r.get("marked_absent") else "No"])
    ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions
    buf=io.BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(buf,mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",as_attachment=True,download_name=f"attendance_{g.emp['username']}_{month}.xlsx")


@app.route("/department")
@employee_required
def my_department():
    return redirect(url_for("department", dept_id=g.emp["department_id"]))


@app.route("/department/<int:dept_id>")
@employee_required
def department(dept_id):
    if dept_id != g.emp["department_id"]:
        abort(403)                       # employees only ever see THEIR department
    return render_template("employee/department.html", active="department",
                           dept=db.get_department(dept_id), members=db.department_members(dept_id),
                           announcements=db.list_announcements(dept_id, limit=20))


@app.route("/profile", methods=["GET", "POST"])
@employee_required
def profile():
    if request.method == "POST":
        action = request.form.get("action", "profile")
        if action == "password":
            cur, new, conf = (request.form.get(k, "") for k in ("current", "new", "confirm"))
            if not db.verify_password(g.emp, cur):
                flash("Current password is wrong.", "error")
            elif len(new) < 8:
                flash("Use at least 8 characters for your new password.", "error")
            elif new != conf:
                flash("New passwords do not match.", "error")
            else:
                db.set_employee_password(g.emp["id"], new)
                flash("Password updated.", "success")
            return redirect(url_for("profile"))
        fields = {
            "email": request.form.get("email", "").strip()[:254],
            "phone": request.form.get("phone", "").strip()[:40],
            "designation": request.form.get("designation", "").strip()[:100],
            "social_linkedin": request.form.get("social_linkedin", "").strip()[:500],
            "social_facebook": request.form.get("social_facebook", "").strip()[:500],
            "social_instagram": request.form.get("social_instagram", "").strip()[:500],
            "social_x": request.form.get("social_x", "").strip()[:500],
            "social_website": request.form.get("social_website", "").strip()[:500],
            "employee_code": request.form.get("employee_code", "").strip()[:30],
            "employment_type": request.form.get("employment_type", "Full-time").strip()[:40],
            "work_location": request.form.get("work_location", "").strip()[:100],
            "manager_name": request.form.get("manager_name", "").strip()[:100],
            "joining_date": request.form.get("joining_date", "").strip()[:10],
            "skills": request.form.get("skills", "").strip()[:1000],
            "education": request.form.get("education", "").strip()[:300],
            "bio": request.form.get("bio", "").strip()[:1000],
        }
        # Only accept ordinary web links for social fields.
        for key in ("social_linkedin", "social_facebook", "social_instagram", "social_x", "social_website"):
            val = fields[key]
            if val and not re.match(r"^https?://", val, re.I):
                fields[key] = ""
        upload = request.files.get("profile_picture")
        if not g.emp.get("profile_picture") and (not upload or not upload.filename):
            flash("Profile picture is compulsory. Upload a picture to activate your employee profile.", "error")
            return redirect(url_for("profile"))
        if upload and upload.filename:
            ext = os.path.splitext(secure_filename(upload.filename))[1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp"):
                flash("Profile picture must be JPG, PNG, or WEBP.", "error")
                return redirect(url_for("profile"))
            try:
                from PIL import Image
                img = Image.open(upload.stream)
                img.verify()
                upload.stream.seek(0)
            except Exception:
                flash("That image could not be verified. Please upload a valid picture.", "error")
                return redirect(url_for("profile"))
            upload_dir = os.path.join(db.DATA_DIR, "profile_uploads")
            os.makedirs(upload_dir, exist_ok=True)
            filename = f"employee_{g.emp['id']}{ext}"
            # Remove an old image with a different extension.
            for old_ext in (".jpg", ".jpeg", ".png", ".webp"):
                old_path = os.path.join(upload_dir, f"employee_{g.emp['id']}{old_ext}")
                if os.path.exists(old_path) and old_ext != ext:
                    os.remove(old_path)
            upload.save(os.path.join(upload_dir, filename))
            fields["profile_picture"] = filename
        with db.conn_ctx() as c:
            sets = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE employees SET {sets} WHERE id=?", (*fields.values(), g.emp["id"]))
        flash("Your profile has been updated.", "success")
        return redirect(url_for("profile"))
    return render_template("employee/profile.html", active="profile",
                           dept=db.get_department(g.emp["department_id"]))


@app.route("/profile-picture/<int:emp_id>")
def profile_picture(emp_id):
    role = session.get("role")
    if role == "employee":
        viewer = db.get_employee(session.get("employee_id"))
        if not viewer or viewer.get("status") != "approved":
            abort(403)
    elif role == "admin":
        viewer = db.get_admin(session.get("admin_id"))
        if not viewer or db.admin_expired(viewer):
            abort(403)
    elif role == "hr":
        with db.conn_ctx() as c:
            viewer = c.execute("SELECT id FROM hr_users WHERE id=? AND active=1", (session.get("hr_id"),)).fetchone()
        if not viewer:
            abort(403)
    else:
        abort(403)
    person = db.get_employee(emp_id)
    if not person or person.get("status") != "approved" or not person.get("profile_picture"):
        abort(404)
    filename = person["profile_picture"]
    if os.path.basename(filename) != filename or not filename.startswith(f"employee_{emp_id}."):
        abort(404)
    path = os.path.join(db.DATA_DIR, "profile_uploads", filename)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype={"jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png","webp":"image/webp"}.get(filename.rsplit(".",1)[-1].lower(),"application/octet-stream"))


@app.route("/my-certificates")
@employee_required
def employee_certificates():
    with db.conn_ctx() as c:
        certificates = [dict(r) for r in c.execute(
            "SELECT * FROM certificates WHERE employee_id=? ORDER BY id DESC", (g.emp["id"],))]
    return render_template("employee/certificates.html", active="certificates", certificates=certificates)


@app.route("/my-certificates/<int:cert_id>")
@employee_required
def employee_certificate_view(cert_id):
    with db.conn_ctx() as c:
        cert = c.execute("""SELECT c.*, e.full_name AS employee_name, e.profile_picture
            FROM certificates c LEFT JOIN employees e ON e.id=c.employee_id
            WHERE c.id=? AND c.employee_id=?""", (cert_id, g.emp["id"])).fetchone()
    if not cert:
        abort(404)
    return render_template("admin/certificate_view.html", cert=dict(cert), back_url=url_for("employee_certificates"))


@app.route("/directory")
@employee_required
def employee_directory():
    q = request.args.get("q", "").strip().lower()
    people = [e for e in db.list_employees(status="approved")
              if not q or q in (e.get("full_name") or "").lower()
              or q in (e.get("department_name") or "").lower()
              or q in (e.get("designation") or "").lower()
              or q in (e.get("skills") or "").lower()
              or q in (e.get("employee_code") or "").lower()]
    return render_template("employee/directory.html", active="directory", people=people, q=q)


@app.route("/directory/<int:emp_id>")
@employee_required
def employee_public_profile(emp_id):
    person = db.get_employee(emp_id)
    if not person or person.get("status") != "approved":
        abort(404)
    return render_template("employee/public_profile.html", active="directory", person=person)


@app.route("/tasks", methods=["GET", "POST"])
@employee_required
def employee_tasks():
    if request.method == "POST":
        title = request.form.get("title", "").strip()[:180]
        description = request.form.get("description", "").strip()[:4000]
        if not title:
            flash("Please enter a task or work proposal.", "error")
        else:
            stamp = db.now().strftime(db.DT_FMT)
            with db.conn_ctx() as c:
                c.execute("""INSERT INTO tasks(employee_id,title,description,assigned_by,due_date,status,created_at,updated_at)
                    VALUES (?,?,?,'Employee proposal',?,'Proposed',?,?)""",
                    (g.emp["id"], title, description, request.form.get("due_date", "")[:10], stamp, stamp))
            db.admin_notify("task", f"Work proposal from {g.emp['full_name']}", title, "/admin/tasks")
            flash("Your work proposal was sent to admin for review.", "success")
        return redirect(url_for("employee_tasks"))
    new_ids = db.unread_ref_ids(g.emp["id"], "task")      # highlighted as "New" on this visit
    db.mark_read_kind(g.emp["id"], "task")
    return render_template("employee/tasks.html", active="tasks", tasks=db.employee_tasks(g.emp["id"]),
                           projects=db.employee_projects(g.emp["id"]), new_ids=new_ids, today=db.today_str())


PROJECT_STATUSES_EMP = ("Planned", "In Progress", "Blocked", "Completed")


@app.route("/projects/<int:project_id>/update", methods=["POST"])
@employee_required
def employee_project_update(project_id):
    """Work assigned by HR (or admin) as a project: the employee reports status, progress and a note."""
    status = request.form.get("status", "")
    if status not in PROJECT_STATUSES_EMP:
        abort(400)
    progress = max(0, min(100, request.form.get("progress", 0, type=int) or 0))
    if status == "Completed":
        progress = 100
    note = request.form.get("employee_note", "").strip()[:2000]
    with db.conn_ctx() as c:
        cur = c.execute("UPDATE projects SET status=?, progress=?, employee_note=?, updated_at=? WHERE id=? AND employee_id=?",
                        (status, progress, note, db.now().strftime(db.DT_FMT), project_id, g.emp["id"]))
        if not cur.rowcount:
            flash("That project could not be updated.", "error")
        else:
            row = c.execute("SELECT title FROM projects WHERE id=?", (project_id,)).fetchone()
            db.admin_notify("task", f"{g.emp['full_name']} updated project: {status} ({progress}%)",
                            (row["title"] if row else "")[:140], "/admin/tasks", conn=c)
            flash("Project updated. HR and admin can see your progress.", "success")
    return redirect(url_for("employee_tasks"))


@app.route("/projects/<int:project_id>/delete", methods=["POST"])
@employee_required
def employee_project_delete(project_id):
    with db.conn_ctx() as c:
        cur = c.execute("UPDATE projects SET emp_deleted=1 WHERE id=? AND employee_id=? AND status='Completed'",
                        (project_id, g.emp["id"]))
    flash("Project removed from your list. It stays saved in your work sheet." if cur.rowcount
          else "Only completed projects can be removed.", "success" if cur.rowcount else "error")
    return redirect(url_for("employee_tasks"))


@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
@employee_required
def employee_task_delete(task_id):
    if db.employee_delete_task(g.emp["id"], task_id):
        flash("Task removed from your dashboard. It stays saved in your work sheet.", "success")
    else:
        flash("Only completed or rejected tasks can be deleted.", "error")
    return redirect(request.referrer or url_for("employee_tasks"))


@app.route("/tasks.xlsx")
@employee_required
def employee_tasks_xlsx():
    """The employee's own work sheet: every task, including the ones deleted from the dashboard."""
    from openpyxl import Workbook
    rows = db.employee_tasks(g.emp["id"], visible_only=False)
    wb = Workbook(); ws = wb.active; ws.title = "My Work"
    _write_sheet(ws, ["Task ID", "Task / Work", "Details", "Assigned By", "Due Date", "Status",
                      "My Update", "Created At", "Last Updated", "Shown on dashboard"],
                 [[t["id"], t["title"], t["description"], t["assigned_by"], t["due_date"], t["status"],
                   t["employee_note"], t["created_at"], t["updated_at"],
                   "No" if t.get("emp_deleted") else "Yes"] for t in rows])
    ws2 = wb.create_sheet("My Projects")
    _write_sheet(ws2, ["Project", "Details", "Assigned By", "Priority", "Status", "Progress %", "Start", "Deadline", "My Update"],
                 [[p["title"], p["description"], p["created_by"], p["priority"], p["status"], p["progress"],
                   p["start_date"], p["due_date"], p.get("employee_note", "")]
                  for p in db.employee_projects(g.emp["id"], visible_only=False)])
    return _xlsx_response(wb, f"my_work_{g.emp['username']}.xlsx")


@app.route("/announcements")
@employee_required
def employee_announcements():
    new_ids = db.unread_ref_ids(g.emp["id"], "announcement")
    items = db.list_announcements(g.emp["department_id"], limit=100)
    db.mark_read_kind(g.emp["id"], "announcement")
    return render_template("employee/announcements.html", active="announcements", items=items, new_ids=new_ids)


# ---- notification popup + sound polling
@app.route("/api/notifications/poll")
def api_notifications_poll():
    if session.get("role") != "employee":
        return jsonify(ok=False), 401
    emp = db.get_employee(session.get("employee_id"))
    if not emp or emp["status"] != "approved":
        return jsonify(ok=False), 401
    return jsonify(ok=True, counts=db.unread_counts(emp["id"]), popups=db.pop_new_notifications(emp["id"]))


@app.route("/api/notifications/read", methods=["POST"])
def api_notifications_read():
    if session.get("role") != "employee":
        return jsonify(ok=False), 401
    ids = (request.get_json(silent=True) or {}).get("ids", [])
    db.mark_read_ids(session["employee_id"], ids if isinstance(ids, list) else [])
    return jsonify(ok=True, counts=db.unread_counts(session["employee_id"]))


@app.route("/tasks/<int:task_id>/update", methods=["POST"])
@employee_required
def employee_task_update(task_id):
    status = request.form.get("status", "")
    note = request.form.get("employee_note", "").strip()[:2000]
    if status not in ("Assigned", "In Progress", "Completed"):
        abort(400)
    with db.conn_ctx() as c:
        cur = c.execute("UPDATE tasks SET status=?, employee_note=?, updated_at=? WHERE id=? AND employee_id=? AND status IN ('Assigned','In Progress')",
                        (status, note, db.now().strftime(db.DT_FMT), task_id, g.emp["id"]))
        if not cur.rowcount:
            flash("That task cannot be updated.", "error")
        else:
            db.admin_notify("task", f"{g.emp['full_name']} updated a task: {status}", note[:140], "/admin/tasks", task_id)
            flash("Task updated.", "success")
    return redirect(url_for("employee_tasks"))


@app.route("/admin/directory")
@admin_required
def admin_directory():
    q = request.args.get("q", "").strip().lower()
    people = [e for e in db.list_employees(status="approved")
              if not q or q in (e.get("full_name") or "").lower()
              or q in (e.get("department_name") or "").lower()
              or q in (e.get("designation") or "").lower()
              or q in (e.get("skills") or "").lower()
              or q in (e.get("employee_code") or "").lower()]
    return render_template("admin/directory.html", active="directory", people=people, q=q)


@app.route("/admin/tasks", methods=["GET", "POST"])
@admin_required
def admin_tasks():
    if request.method == "POST":
        action = request.form.get("action", "assign")
        stamp = db.now().strftime(db.DT_FMT)
        if action == "decision":
            task_id = request.form.get("task_id", type=int)
            decision = request.form.get("decision", "")
            if decision not in ("Assigned", "Rejected"):
                abort(400)
            with db.conn_ctx() as c:
                row = c.execute("SELECT employee_id, title FROM tasks WHERE id=? AND status='Proposed'",
                                (task_id,)).fetchone()
                c.execute("UPDATE tasks SET status=?, assigned_by=?, updated_at=? WHERE id=? AND status='Proposed'",
                          (decision, g.admin["username"], stamp, task_id))
            if row:
                db.notify(row["employee_id"], "task",
                          "Your work proposal was " + ("approved" if decision == "Assigned" else "rejected"),
                          row["title"], "/tasks", task_id)
            flash("Employee proposal reviewed.", "success")
        else:
            emp_id = request.form.get("employee_id", type=int)
            title = request.form.get("title", "").strip()[:180]
            description = request.form.get("description", "").strip()[:4000]
            due = request.form.get("due_date", "")[:10]
            if not db.get_employee(emp_id) or not title:
                flash("Choose an employee and enter a task title.", "error")
            else:
                with db.conn_ctx() as c:
                    cur = c.execute("""INSERT INTO tasks(employee_id,title,description,assigned_by,due_date,status,created_at,updated_at)
                        VALUES (?,?,?,?,?,'Assigned',?,?)""",
                        (emp_id, title, description, g.admin["username"], due, stamp, stamp))
                    task_id = cur.lastrowid
                db.notify(emp_id, "task", "New task from admin", title, "/tasks", task_id)
                flash("Task assigned to employee.", "success")
        return redirect(url_for("admin_tasks"))
    with db.conn_ctx() as c:
        tasks = [dict(r) for r in c.execute("""SELECT t.*, e.full_name, e.email, d.name AS department_name
            FROM tasks t JOIN employees e ON e.id=t.employee_id
            LEFT JOIN departments d ON d.id=e.department_id WHERE COALESCE(t.admin_deleted,0)=0 ORDER BY
            CASE t.status WHEN 'Proposed' THEN 0 WHEN 'In Progress' THEN 1 WHEN 'Assigned' THEN 2 ELSE 3 END,
            t.created_at DESC""")]
    employees = db.list_employees(status="approved")
    with db.conn_ctx() as c:
        projects = [dict(r) for r in c.execute("""SELECT p.*, e.full_name FROM projects p JOIN employees e ON e.id=p.employee_id
                                                  ORDER BY (p.status='Completed'), p.id DESC LIMIT 200""")]
    db.admin_mark_read_kind(g.admin["id"], "task")
    return render_template("admin/tasks.html", active="tasks", tasks=tasks, employees=employees, projects=projects)


@app.route("/admin/tasks/<int:task_id>/delete", methods=["POST"])
@admin_required
def admin_task_delete(task_id):
    if db.admin_delete_task(task_id):
        flash("Task removed from the dashboard. It stays saved in the work sheet.", "success")
    else:
        flash("Only completed or rejected tasks can be deleted.", "error")
    return redirect(url_for("admin_tasks"))


@app.route("/admin/tasks.xlsx")
@admin_required
def admin_tasks_xlsx():
    """Work sheet. The main admin also gets the tasks that were deleted from dashboards."""
    from openpyxl import Workbook
    where = "" if g.admin.get("is_main") else "WHERE COALESCE(t.admin_deleted,0)=0"
    with db.conn_ctx() as c:
        rows = c.execute(f"""SELECT t.id,e.id AS employee_id,e.full_name,d.name AS department_name,
            t.title,t.description,t.assigned_by,t.due_date,t.status,t.employee_note,t.created_at,t.updated_at,
            t.emp_deleted,t.admin_deleted
            FROM tasks t JOIN employees e ON e.id=t.employee_id LEFT JOIN departments d ON d.id=e.department_id
            {where} ORDER BY e.full_name,t.created_at DESC""").fetchall()
    headers = ["Task ID", "Employee ID", "Employee Name", "Department", "Task / Work", "Description",
               "Assigned By", "Due Date", "Status", "Employee Update", "Created At", "Last Updated"]
    data = [list(r)[:12] for r in rows]
    if g.admin.get("is_main"):
        headers += ["Deleted by employee", "Deleted by admin"]
        data = [list(r)[:12] + ["Yes" if r["emp_deleted"] else "No", "Yes" if r["admin_deleted"] else "No"] for r in rows]
    wb = Workbook(); ws = wb.active; ws.title = "Work Tracker"
    _write_sheet(ws, headers, data)
    return _xlsx_response(wb, "employee_work_tracker.xlsx")


# =================================================================== ADMIN PANEL
@app.route("/admin/login", methods=["GET", "POST"])
@app.route("/adminlogin", methods=["GET", "POST"])
def admin_login():
    if session.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        key = f"adm:{request.remote_addr}:{username.lower()}"
        if throttled(key):
            flash("Too many failed attempts. Please wait 10 minutes.", "error")
            return render_template("admin/login.html"), 429
        row = db.get_admin_by_username(username)
        if not db.verify_password(row, request.form.get("password", "")):
            register_fail(key)
            flash("Invalid username or password.", "error")
            return render_template("admin/login.html")
        if db.admin_expired(row):
            flash("This temporary admin account has expired. Please contact the main admin.", "error")
            return render_template("admin/login.html")
        clear_fails(key)
        session.clear()
        session.permanent = True
        session["role"] = "admin"
        session["admin_id"] = row["id"]
        return redirect(url_for("admin_dashboard"))
    return render_template("admin/login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/leaves", methods=["GET", "POST"])
@employee_required
def employee_leaves():
    with db.conn_ctx() as c:
        c.execute("INSERT OR IGNORE INTO leave_balances(employee_id) VALUES (?)",(g.emp["id"],))
        if request.method=="POST":
            d=request.form.get("leave_date","").strip(); typ=request.form.get("leave_type","Full Day")
            if d:
                c.execute("INSERT INTO leave_requests(employee_id,leave_date,leave_type,units,reason,created_at) VALUES (?,?,?,?,?,?)",(g.emp["id"],d,typ,0.5 if typ=="Short Leave" else 1,request.form.get("reason",""),db.now().strftime(db.DT_FMT)))
                db.admin_notify("leave", f"Leave request from {g.emp['full_name']}", f"{typ} on {d}" + (f" - {request.form.get('reason','')[:120]}" if request.form.get("reason") else ""), "/admin/leaves", conn=c)
                flash("Leave request sent to admin.","success"); return redirect(url_for("employee_leaves"))
            flash("Choose a leave date.","error")
        balance=dict(c.execute("SELECT * FROM leave_balances WHERE employee_id=?",(g.emp["id"],)).fetchone())
        rows=[dict(r) for r in c.execute("SELECT * FROM leave_requests WHERE employee_id=? ORDER BY leave_date DESC",(g.emp["id"],))]
    db.mark_read_kind(g.emp["id"],"leave")
    return render_template("employee/leaves.html",active="leaves",balance=balance,rows=rows)

@app.route("/admin/leaves", methods=["GET","POST"])
@admin_required
def admin_leaves():
    with db.conn_ctx() as c:
        if request.method=="POST":
            action=request.form.get("action"); eid=int(request.form.get("employee_id",0) or 0)
            rid=int(request.form.get("request_id",0) or 0)
            c.execute("INSERT OR IGNORE INTO leave_balances(employee_id) SELECT id FROM employees WHERE id=?",(eid,))
            if action=="balance":
                c.execute("UPDATE leave_balances SET annual_total=?,annual_used=? WHERE employee_id=?",(max(0,int(request.form.get("annual_total",30) or 30)),max(0,float(request.form.get("annual_used",0) or 0)),eid))
                flash("Leave balance updated.","success")
            elif action=="grant":
                c.execute("UPDATE leave_balances SET annual_used=MAX(0,annual_used-?) WHERE employee_id=?",(max(0,float(request.form.get("units",0) or 0)),eid))
                flash("Leave granted back.","success")
            elif action=="review":
                ded=request.form.get("salary_deduction","").strip()
                ok,msg=hrcore.review_leave(c,rid,request.form.get("decision"),request.form.get("admin_note",""),g.admin["username"],deduction=(ded if ded!="" else None))
                flash(msg,"success" if ok else "error")
            elif action=="deduction":
                ok,msg=hrcore.set_leave_deduction(c,rid,request.form.get("salary_deduction"),g.admin["username"],request.form.get("deduction_note",""))
                flash(msg,"success" if ok else "error")
            return redirect(url_for("admin_leaves"))
        c.execute("INSERT OR IGNORE INTO leave_balances(employee_id) SELECT id FROM employees")
        employees=[dict(r) for r in c.execute("SELECT e.id,e.full_name,e.username,COALESCE(b.annual_total,30) annual_total,COALESCE(b.annual_used,0) annual_used FROM employees e LEFT JOIN leave_balances b ON b.employee_id=e.id ORDER BY e.full_name")]
        requests=[dict(r) for r in c.execute("SELECT lr.*,e.full_name,e.username,COALESCE(s.base_salary,0) base_salary FROM leave_requests lr JOIN employees e ON e.id=lr.employee_id LEFT JOIN salary_profiles s ON s.employee_id=lr.employee_id ORDER BY CASE lr.status WHEN 'pending' THEN 0 ELSE 1 END,lr.leave_date DESC")]
        for r in requests: r["suggested"]=hrcore.suggested_leave_deduction(r["base_salary"],r["units"])
    db.admin_mark_read_kind(g.admin["id"],"leave")
    db.admin_mark_read_kind(g.admin["id"],"hr")
    return render_template("admin/leaves.html",active="leaves",employees=employees,requests=requests)

@app.route("/admin")
@admin_required
def admin_dashboard():
    today_rows = db.attendance_report(date_from=db.today_str(), date_to=db.today_str())
    return render_template(
        "admin/dashboard.html", active="dashboard", stats=db.admin_stats(),
        pending=db.list_employees(status="pending"), today_rows=today_rows,
        absent=db.absent_today(), depts=db.departments_overview(),
        settings=db.get_settings(), req=db.required_seconds())


@app.route("/admin/approvals")
@admin_required
def admin_approvals():
    db.admin_mark_read_kind(g.admin["id"], "approval")
    return render_template("admin/approvals.html", active="approvals",
                           pending=db.list_employees(status="pending"),
                           enrolling=db.list_employees(status="face_pending"))


def _admin_employee_action(emp_id, status, message):
    emp = db.get_employee(emp_id)
    if not emp:
        abort(404)
    if status == "rejected" and emp["status"] == "approved" and not g.admin.get("is_main"):
        flash("Only the main admin can revoke an employee's access.", "error")
    elif status == "approved" and emp["status"] not in ("pending", "rejected"):
        flash("This employee has not finished face registration yet.", "error")
    else:
        if status == "approved" and not emp.get("profile_picture"):
            flash("Employee must upload a profile picture before approval.", "error")
            return redirect(url_for("admin_employee_detail", emp_id=emp_id))
        if status == "approved" and int(emp.get("samples_captured") or 0) < fu.SAMPLES_NEEDED:
            flash(f"Employee must complete all {fu.SAMPLES_NEEDED} real face samples before approval.", "error")
            return redirect(url_for("admin_employee_detail", emp_id=emp_id))
        db.set_status(emp_id, status)
        flash(message.format(name=emp["full_name"]), "success")
    return redirect(request.referrer or url_for("admin_employees"))


@app.route("/admin/employee/<int:emp_id>/approve", methods=["POST"])
@admin_required
def admin_approve(emp_id):
    return _admin_employee_action(emp_id, "approved", "{name} approved. They can now login with face recognition.")


@app.route("/admin/employee/<int:emp_id>/reject", methods=["POST"])
@admin_required
def admin_reject(emp_id):
    return _admin_employee_action(emp_id, "rejected", "{name} rejected / access revoked.")


@app.route("/admin/employee/<int:emp_id>/delete", methods=["POST"])
@main_admin_required
def admin_delete_employee(emp_id):
    emp = db.get_employee(emp_id)
    if not emp:
        abort(404)
    from enterprise_upgrade import archive_employee
    archive_employee(emp_id)
    db.delete_employee(emp_id)
    fu.delete_employee_data(emp_id, dataset_manifest())
    flash(f"{emp['full_name']} and their face data were deleted.", "success")
    return redirect(url_for("admin_employees"))


@app.route("/admin/employees")
@admin_required
def admin_employees():
    status = request.args.get("status") or None
    dept = request.args.get("department_id", type=int)
    q = request.args.get("q", "").strip() or None
    return render_template("admin/employees.html", active="employees",
                           employees=db.list_employees(status, dept, q),
                           departments=db.list_departments(), f_status=status, f_dept=dept, f_q=q or "")


@app.route("/admin/employees.csv")
@admin_required
def admin_employees_csv():
    rows = db.list_employees()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Employee ID", "Employee Code", "Name", "Username", "Department", "Designation", "Employment Type", "Work Location", "Manager", "Joining Date", "Email", "Phone", "Skills", "Education", "Status"])
    for e in rows:
        writer.writerow([e.get("id"), e.get("employee_code"), e.get("full_name"), e.get("username"), e.get("department_name"), e.get("designation"), e.get("employment_type"), e.get("work_location"), e.get("manager_name"), e.get("joining_date"), e.get("email"), e.get("phone"), e.get("skills"), e.get("education"), e.get("status")])
    return Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=employee_directory.csv"})


@app.route("/admin/employee/<int:emp_id>")
@admin_required
def admin_employee_detail(emp_id):
    emp = db.get_employee(emp_id)
    if not emp:
        abort(404)
    month = request.args.get("month") or db.now().strftime("%Y-%m")
    n = fu.count_samples(emp_id)
    picks = sorted({i for i in (1, n // 5, 2 * n // 5, 3 * n // 5, 4 * n // 5, n) if i >= 1}) if n else []
    private = _private_record(emp) if g.admin.get("is_main") else None
    return render_template("admin/employee_detail.html", active="employees", emp=emp, month=month,
                           rows=db.employee_history(emp_id, month), summary=db.month_summary(emp_id, month),
                           sample_ids=picks, sample_total=n, private=private)


def _private_record(emp):
    """Main-admin-only private records. Passwords are never displayed; only reset is safe."""
    with db.conn_ctx() as c:
        tasks = [dict(r) for r in c.execute("SELECT * FROM tasks WHERE employee_id=? ORDER BY created_at DESC", (emp["id"],))]
        msgs = [dict(r) for r in c.execute("SELECT * FROM messages WHERE employee_id=? ORDER BY created_at DESC", (emp["id"],))]
        leaves = [dict(r) for r in c.execute("SELECT * FROM leave_requests WHERE employee_id=? ORDER BY leave_date DESC", (emp["id"],))]
    return {"nickname": emp.get("nickname") or "", "warnings": db.list_warnings(emp["id"]),
            "active_warnings": db.active_warning_count(emp["id"]), "max_warnings": db.max_warnings(),
            "tasks": tasks, "messages": msgs, "leaves": leaves}


@app.route("/admin/employee/<int:emp_id>/warnings/clear", methods=["POST"])
@main_admin_required
def admin_clear_warnings(emp_id):
    if not db.get_employee(emp_id):
        abort(404)
    db.clear_warnings(emp_id)
    flash("Missed check-out warnings were reset.", "success")
    return redirect(url_for("admin_employee_detail", emp_id=emp_id))


def _password_text(emp):
    """(password, note) for the main-admin records page. Passwords are stored only as an encrypted copy."""
    token = emp.get("password_enc") or ""
    if not token:
        return None, "Not saved yet (appears after the employee's next login or password change)"
    plain = db.decrypt_secret(token)
    if plain is None:
        return None, "Cannot be decrypted (secret key changed or 'cryptography' is not installed)"
    return plain, ""


# ---- admin notification popup + sound polling (same engine as the employee side)
@app.route("/admin/api/notifications/poll")
def admin_api_notifications_poll():
    if session.get("role") != "admin":
        return jsonify(ok=False), 401
    adm = db.get_admin(session.get("admin_id"))
    if not adm or db.admin_expired(adm):
        return jsonify(ok=False), 401
    return jsonify(ok=True, counts=db.admin_unread_counts(adm["id"]), popups=db.admin_pop_new(adm["id"]))


@app.route("/admin/api/notifications/read", methods=["POST"])
def admin_api_notifications_read():
    if session.get("role") != "admin":
        return jsonify(ok=False), 401
    ids = (request.get_json(silent=True) or {}).get("ids", [])
    db.admin_mark_read_ids(session["admin_id"], ids if isinstance(ids, list) else [])
    return jsonify(ok=True, counts=db.admin_unread_counts(session["admin_id"]))


@app.route("/admin/records")
@main_admin_required
def admin_records():
    """MAIN ADMIN ONLY: every employee with their private data (password, nickname, work, warnings)."""
    rows = []
    with db.conn_ctx() as c:
        task_n = {r[0]: r[1] for r in c.execute("SELECT employee_id, COUNT(*) FROM tasks GROUP BY employee_id")}
        absent_n = {r[0]: r[1] for r in c.execute("SELECT employee_id, COUNT(*) FROM attendance WHERE marked_absent=1 GROUP BY employee_id")}
    for e in db.list_employees():
        pw, note = _password_text(e)
        rows.append({**e, "password": pw, "password_note": note, "tasks": task_n.get(e["id"], 0),
                     "absent_days": absent_n.get(e["id"], 0), "warnings": db.active_warning_count(e["id"])})
    return render_template("admin/records.html", active="records", rows=rows, max_warnings=db.max_warnings())


@app.route("/admin/records.xlsx")
@main_admin_required
def admin_records_xlsx():
    """MAIN ADMIN ONLY: complete workbook - employees (with password + nickname), attendance, work, warnings, messages."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active; ws.title = "Employees"
    emp_rows = []
    for e in db.list_employees():
        pw, note = _password_text(e)
        emp_rows.append([e["id"], e["full_name"], e["username"], pw if pw is not None else note, e.get("nickname") or "",
                         e["department_name"], e["designation"], e["email"], e["phone"], e["status"],
                         e["created_at"], e["approved_at"] or "", db.active_warning_count(e["id"])])
    _write_sheet(ws, ["ID", "Name", "Username", "Password", "Nickname", "Department", "Designation", "Email",
                      "Phone", "Status", "Registered", "Approved", "Active warnings"], emp_rows)
    with db.conn_ctx() as c:
        att = c.execute("""SELECT a.employee_id, e.full_name, a.work_date, a.check_in, a.check_out, a.worked_seconds,
                           a.is_late, a.marked_absent, a.work_mode, a.location_name FROM attendance a
                           JOIN employees e ON e.id=a.employee_id ORDER BY a.work_date DESC""").fetchall()
        tasks = c.execute("""SELECT t.id, e.full_name, t.title, t.description, t.assigned_by, t.due_date, t.status,
                             t.employee_note, t.created_at, t.updated_at, t.emp_deleted, t.admin_deleted FROM tasks t
                             JOIN employees e ON e.id=t.employee_id ORDER BY e.full_name, t.created_at DESC""").fetchall()
        warns = c.execute("""SELECT e.full_name, w.number, w.work_date, w.reason, w.created_at, w.cleared FROM warnings w
                             JOIN employees e ON e.id=w.employee_id ORDER BY w.created_at DESC""").fetchall()
        msgs = c.execute("""SELECT e.full_name, m.recipient_role, m.subject, m.body, m.reply, m.created_at FROM messages m
                            JOIN employees e ON e.id=m.employee_id ORDER BY m.created_at DESC""").fetchall()
    _write_sheet(wb.create_sheet("Attendance"),
                 ["Employee ID", "Name", "Date", "Check in", "Check out", "Worked", "Late", "Marked absent", "Mode", "Location"],
                 [[r[0], r[1], r[2], r[3], r[4] or "", db.fmt_hm(r[5]) if r[5] is not None else "",
                   "Yes" if r[6] else "No", "Yes" if r[7] else "No", r[8], r[9]] for r in att])
    _write_sheet(wb.create_sheet("Work"),
                 ["Task ID", "Employee", "Task", "Details", "Assigned by", "Due", "Status", "Employee update",
                  "Created", "Updated", "Deleted by employee", "Deleted by admin"],
                 [[*list(r)[:10], "Yes" if r[10] else "No", "Yes" if r[11] else "No"] for r in tasks])
    _write_sheet(wb.create_sheet("Warnings"), ["Employee", "Warning no.", "Missed date", "Reason", "Created", "Reset"],
                 [[r[0], r[1], r[2], r[3], r[4], "Yes" if r[5] else "No"] for r in warns])
    _write_sheet(wb.create_sheet("Messages"), ["Employee", "To", "Subject", "Message", "Reply", "Sent"], [list(r) for r in msgs])
    return _xlsx_response(wb, "full_employee_records.xlsx")


@app.route("/admin/employee/<int:emp_id>/sample/<int:n>.jpg")
@admin_required
def admin_sample(emp_id, n):
    p = fu.sample_path(emp_id, n)
    if not p:
        abort(404)
    return send_file(p, mimetype="image/jpeg")


@app.route("/admin/admins")
@admin_required
def admin_admins():
    return render_template("admin/admins.html", active="admins", admins=db.list_admins(),
                           durations=ADMIN_DURATIONS)


@app.route("/admin/admins/add", methods=["POST"])
@main_admin_required
def admin_add_admin():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    duration = request.form.get("duration", "permanent")
    err = None
    if len(username) < 3 or not username.replace("_", "").replace(".", "").isalnum():
        err = "Username must be 3+ characters (letters, numbers, . or _)."
    elif len(password) < 8:
        err = "Admin password must be at least 8 characters."
    elif password != confirm:
        err = "Passwords do not match."
    elif duration not in ADMIN_DURATIONS:
        err = "Please choose a valid access duration."
    elif db.username_taken(username):
        err = "This username is already taken."
    if err:
        flash(err, "error")
        return redirect(url_for("admin_admins"))
    days = ADMIN_DURATIONS[duration][1]
    expires = (db.now() + timedelta(days=days)).strftime(db.DT_FMT) if days else None
    if db.create_admin(username, password, expires, created_by=g.admin["username"]):
        flash(f"Admin '{username}' added" + (f" (temporary, until {db.dt_pretty(expires)})." if expires
                                             else " (permanent)."), "success")
    else:
        flash("This username is already taken.", "error")
    return redirect(url_for("admin_admins"))


@app.route("/admin/admins/<int:admin_id>/delete", methods=["POST"])
@main_admin_required
def admin_delete_admin(admin_id):
    target = db.get_admin(admin_id)
    if not target:
        abort(404)
    if target["is_main"]:
        flash("The main admin cannot be removed.", "error")
    elif db.delete_admin(admin_id):
        flash(f"Admin '{target['username']}' was removed.", "success")
    return redirect(url_for("admin_admins"))


@app.route("/admin/departments", methods=["GET", "POST"])
@admin_required
def admin_departments():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Department name is required.", "error")
        elif db.add_department(name, request.form.get("description", "")):
            flash("Department added.", "success")
        else:
            flash("A department with this name already exists.", "error")
        return redirect(url_for("admin_departments"))
    return render_template("admin/departments.html", active="departments", depts=db.departments_overview())


@app.route("/admin/departments/<int:dept_id>/delete", methods=["POST"])
@admin_required
def admin_delete_department(dept_id):
    if db.delete_department(dept_id):
        flash("Department deleted.", "success")
    else:
        flash("Cannot delete a department that still has employees.", "error")
    return redirect(url_for("admin_departments"))


def _report_args():
    today = db.today_str()
    return dict(
        date_from=request.args.get("from") or today,
        date_to=request.args.get("to") or today,
        department_id=request.args.get("department_id", type=int),
        employee_id=request.args.get("employee_id", type=int),
        status=request.args.get("status") or None,
    )


@app.route("/admin/attendance")
@admin_required
def admin_attendance():
    args = _report_args()
    rows = db.attendance_report(**args)
    done = [r for r in rows if r["check_out"]]
    return render_template("admin/attendance.html", active="attendance", rows=rows, a=args,
                           departments=db.list_departments(),
                           employees=db.list_employees(status="approved"),
                           total_worked=sum(r["worked"] for r in done), req=db.required_seconds(),
                           qs=request.query_string.decode())


@app.route("/admin/attendance.csv")
@admin_required
def admin_attendance_csv():
    rows = db.attendance_report(**_report_args())
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Employee", "Username", "Department", "Check In", "Check Out",
                "Worked (hh:mm)", "Required (hh:mm)", "Status", "Late"])
    for r in rows:
        w.writerow([r["work_date"], r["full_name"], r["username"], r["department_name"],
                    r["check_in_time"], r["check_out_time"], db.fmt_hm(r["worked"]).replace("h ", ":").replace("m", ""),
                    db.fmt_hm(r["required"]).replace("h ", ":").replace("m", ""), r["status"],
                    "Yes" if r["is_late"] else "No"])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=attendance_report.csv"})


@app.route("/admin/announcements", methods=["GET", "POST"])
@admin_required
def admin_announcements():
    if request.method == "POST":
        title, body = request.form.get("title", "").strip(), request.form.get("body", "").strip()
        dept = request.form.get("department_id", type=int)
        if not title or not body:
            flash("Title and message are required.", "error")
        else:
            db.add_announcement(dept, title, body)
            flash("Announcement posted.", "success")
        return redirect(url_for("admin_announcements"))
    return render_template("admin/announcements.html", active="announcements",
                           items=db.list_announcements(everything=True, limit=100),
                           departments=db.list_departments())


@app.route("/admin/announcements/<int:ann_id>/delete", methods=["POST"])
@admin_required
def admin_delete_announcement(ann_id):
    db.delete_announcement(ann_id)
    flash("Announcement deleted.", "success")
    return redirect(url_for("admin_announcements"))


@app.route("/admin/certificates", methods=["GET", "POST"])
@admin_required
def admin_certificates():
    """All admins can create and review editable employee departure/achievement certificates."""
    if request.method == "POST":
        emp_id = request.form.get("employee_id", type=int)
        emp = db.get_employee(emp_id) if emp_id else None
        recipient = ""
        title = request.form.get("certificate_title", "Best Employee").strip()[:120] or "Best Employee"
        period = request.form.get("period", "").strip()[:120]
        description = request.form.get("description", "").strip()[:2000]
        if emp and emp.get("status") == "approved":
            recipient = emp.get("full_name", "")
        if not emp or emp.get("status") != "approved" or not period or not description:
            flash("Recipient name, time period and description are required.", "error")
        else:
            with db.conn_ctx() as c:
                cur = c.execute("""INSERT INTO certificates(employee_id,recipient_name,certificate_title,period,description,issued_by,issued_at)
                    VALUES (?,?,?,?,?,?,?)""", (emp_id, recipient, title, period, description,
                    g.admin.get("username", "Admin"), db.now().strftime(db.DT_FMT)))
                cert_id = cur.lastrowid
            db.notify(emp_id, "certificate", "New certificate issued", title, "/my-certificates", cert_id)
            return redirect(url_for("admin_certificate_view", cert_id=cert_id))
    with db.conn_ctx() as c:
        certificates = [dict(r) for r in c.execute("""SELECT c.*, e.full_name AS employee_name, e.profile_picture FROM certificates c
            LEFT JOIN employees e ON e.id=c.employee_id ORDER BY c.id DESC LIMIT 100""")]
    return render_template("admin/certificates.html", active="certificates", employees=db.list_employees(status="approved"), certificates=certificates)


@app.route("/admin/certificates/<int:cert_id>")
@admin_required
def admin_certificate_view(cert_id):
    with db.conn_ctx() as c:
        cert = c.execute("""SELECT c.*, e.full_name AS employee_name, e.profile_picture FROM certificates c
            LEFT JOIN employees e ON e.id=c.employee_id WHERE c.id=?""", (cert_id,)).fetchone()
    if not cert:
        abort(404)
    return render_template("admin/certificate_view.html", cert=dict(cert), back_url=url_for("admin_certificates"))


@app.route("/admin/certificates/<int:cert_id>/delete", methods=["POST"])
@admin_required
def admin_certificate_delete(cert_id):
    with db.conn_ctx() as c:
        c.execute("DELETE FROM certificates WHERE id=?", (cert_id,))
    flash("Certificate deleted.", "success")
    return redirect(url_for("admin_certificates"))


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        if request.form.get("section") == "password":
            cur, new, conf = (request.form.get(k, "") for k in ("current", "new", "confirm"))
            if not db.verify_password(g.admin, cur):
                flash("Current password is wrong.", "error")
            elif len(new) < 8:
                flash("New admin password must be at least 8 characters.", "error")
            elif new != conf:
                flash("New passwords do not match.", "error")
            else:
                db.set_admin_password(g.admin["id"], new)
                flash("Admin password updated.", "success")
        else:
            try:
                hours = float(request.form.get("required_hours", ""))
                hh, mm = request.form.get("shift_start", "09:00").split(":")
                int(hh), int(mm)
                grace = int(request.form.get("late_grace_minutes", "15"))
                warn_limit = int(request.form.get("max_warnings", "3"))
                face_thr = float(request.form.get("face_threshold", "55"))
                if not (0.5 <= hours <= 16) or grace < 0 or not (1 <= warn_limit <= 10) or not (30 <= face_thr <= 75):
                    raise ValueError
                db.save_settings(hours, f"{int(hh):02d}:{int(mm):02d}", grace, warn_limit, face_thr)
                flash("Working-hours settings saved.", "success")
            except ValueError:
                flash("Please enter valid values (hours between 0.5 and 16).", "error")
        return redirect(url_for("admin_settings"))
    return render_template("admin/settings.html", active="settings", s=db.get_settings())


@app.route("/admin/dataset.zip")
@main_admin_required
def admin_dataset_zip():
    fu.rebuild_dataset_zip(dataset_manifest())
    return send_file(fu.ZIP_PATH, mimetype="application/zip", as_attachment=True, download_name="dataset.zip")


@app.route("/api/dataset_zip")
def dataset_zip_sync():
    """Used by sync_dataset.py:  /api/dataset_zip?key=<SYNC_KEY env var>"""
    sync_key = os.environ.get("SYNC_KEY", "")
    provided = request.args.get("key", "")
    if not ((session.get("role") == "admin" and db.get_admin(session.get("admin_id")) and db.get_admin(session.get("admin_id")).get("is_main")) or (sync_key and secrets.compare_digest(provided, sync_key))):
        return jsonify(error="Unauthorized"), 401
    fu.rebuild_dataset_zip(dataset_manifest())
    return send_file(fu.ZIP_PATH, mimetype="application/zip", as_attachment=True, download_name="dataset.zip")



# --------------------------- locations, WFH approval, Excel, messaging
@app.route("/attendance/setup", methods=["GET", "POST"])
@employee_required
def attendance_setup():
    with db.conn_ctx() as c:
        approved = c.execute("SELECT 1 FROM wfh_requests WHERE employee_id=? AND work_date=? AND status='approved'", (g.emp['id'], db.today_str())).fetchone()
    if request.method == 'POST':
        mode = request.form.get('work_mode', 'Office')
        try:
            lat, lon = float(request.form.get('latitude')), float(request.form.get('longitude'))
            if not (-90 <= lat <= 90 and -180 <= lon <= 180): raise ValueError
        except (TypeError, ValueError):
            flash('Please allow location access to record your check-in location.', 'error')
            return redirect(url_for('attendance_setup'))
        location = 'Work From Home' if mode == 'Home' else ('Office / On-site' if mode == 'Office' else 'Other location')
        session['attendance_choice'] = {'mode': mode, 'location': location, 'latitude': lat, 'longitude': lon}
        return redirect(url_for('scan', action='checkin'))
    return render_template('employee/attendance_setup.html', active='dashboard', locations=[], approved=bool(approved), assigned='')

@app.route('/wfh-request', methods=['GET','POST'])
@employee_required
def wfh_request():
    if request.method == 'POST':
        reason=request.form.get('reason','').strip()
        if not reason:
            flash('Please provide a reason for your work-from-home request.', 'error')
            return redirect(url_for('wfh_request'))
        with db.conn_ctx() as c:
            c.execute("INSERT INTO wfh_requests(employee_id,work_date,reason,status,created_at) VALUES (?,?,?,'pending',?) ON CONFLICT(employee_id,work_date) DO UPDATE SET reason=excluded.reason,status='pending',created_at=excluded.created_at", (g.emp['id'],db.today_str(),reason,db.now().strftime(db.DT_FMT)))
        db.admin_notify('wfh', f"Work-from-home request from {g.emp['full_name']}", reason[:160], '/admin/locations')
        flash('WFH request sent to admin for approval.', 'success'); return redirect(url_for('wfh_request'))
    with db.conn_ctx() as c:
        rows=[dict(x) for x in c.execute('SELECT * FROM wfh_requests WHERE employee_id=? ORDER BY work_date DESC LIMIT 30',(g.emp['id'],))]
    db.mark_read_kind(g.emp['id'],'wfh')
    return render_template('employee/wfh_request.html',active='wfh',rows=rows)

@app.route('/admin/locations', methods=['GET','POST'])
@admin_required
def admin_locations():
    if request.method == 'POST':
        action = request.form.get('form_action', '').strip()
        # Only the main admin may configure office locations or employee assignments.
        if action in ('add_location', 'update_coordinates', 'assign_employee') and not g.admin.get('is_main'):
            abort(403, 'Only the main admin can change office locations.')
        with db.conn_ctx() as c:
            if action == 'add_location':
                name = request.form.get('name', '').strip()
                try:
                    lat = float(request.form.get('latitude', ''))
                    lon = float(request.form.get('longitude', ''))
                    radius = int(request.form.get('radius_m', 150))
                    if not name or not (-90 <= lat <= 90) or not (-180 <= lon <= 180) or not (25 <= radius <= 2000):
                        raise ValueError
                except (TypeError, ValueError):
                    flash('Enter a location name, valid latitude/longitude, and a radius from 25 to 2000 meters.', 'error')
                    return redirect(url_for('admin_locations'))
                try:
                    c.execute('INSERT INTO locations(name,active,latitude,longitude,radius_m) VALUES (?,1,?,?,?)', (name, lat, lon, radius))
                    flash('Office location added and geofence configured.', 'success')
                except Exception:
                    flash('That location already exists or could not be added.', 'error')
            elif action == 'update_coordinates':
                loc_id = request.form.get('location_id', type=int)
                try:
                    lat = float(request.form.get('latitude', ''))
                    lon = float(request.form.get('longitude', ''))
                    radius = int(request.form.get('radius_m', 150))
                    if not loc_id or not (-90 <= lat <= 90) or not (-180 <= lon <= 180) or not (25 <= radius <= 2000):
                        raise ValueError
                except (TypeError, ValueError):
                    flash('Choose an office and enter valid coordinates and a radius from 25 to 2000 meters.', 'error')
                    return redirect(url_for('admin_locations'))
                cur = c.execute('UPDATE locations SET latitude=?,longitude=?,radius_m=? WHERE id=?', (lat, lon, radius, loc_id))
                flash('Office coordinates saved successfully.' if cur.rowcount else 'Office location was not found.', 'success' if cur.rowcount else 'error')
            elif action == 'assign_employee':
                emp_id = request.form.get('employee_id', type=int)
                assigned = request.form.get('assigned_location', '').strip()
                emp = c.execute("SELECT id FROM employees WHERE id=? AND status='approved'", (emp_id,)).fetchone() if emp_id else None
                active_location = c.execute('SELECT name FROM locations WHERE name=? AND active=1', (assigned,)).fetchone() if assigned else None
                if not emp:
                    flash('Please select a valid approved employee.', 'error')
                elif assigned and not active_location:
                    flash('Please assign an active office location.', 'error')
                else:
                    c.execute('UPDATE employees SET assigned_location=? WHERE id=?', (assigned, emp_id))
                    flash('Employee office assignment saved.', 'success')
            else:
                flash('Unknown form action. Please refresh the page and try again.', 'error')
        return redirect(url_for('admin_locations'))
    with db.conn_ctx() as c:
        locations = [dict(x) for x in c.execute('SELECT * FROM locations ORDER BY name')]
        employees = [dict(x) for x in c.execute("SELECT id,full_name,assigned_location FROM employees WHERE status='approved' ORDER BY full_name")]
        requests = [dict(x) for x in c.execute("SELECT w.*,e.full_name,e.username FROM wfh_requests w JOIN employees e ON e.id=w.employee_id WHERE w.status='pending' ORDER BY w.created_at")]
    db.admin_mark_read_kind(g.admin['id'],'wfh')
    return render_template('admin/locations.html', active='locations', locations=locations, employees=employees, requests=requests)

@app.route('/admin/location/<int:loc_id>/toggle',methods=['POST'])
@main_admin_required
def admin_location_toggle(loc_id):
    with db.conn_ctx() as c: c.execute('UPDATE locations SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?',(loc_id,))
    return redirect(url_for('admin_locations'))

@app.route('/admin/wfh/<int:req_id>/<decision>',methods=['POST'])
@admin_required
def admin_wfh_decide(req_id,decision):
    if decision not in ('approve','reject'): abort(404)
    with db.conn_ctx() as c:
        w=c.execute("SELECT employee_id,work_date FROM wfh_requests WHERE id=? AND status='pending'",(req_id,)).fetchone()
        c.execute('UPDATE wfh_requests SET status=?,reviewed_by=? WHERE id=? AND status=\'pending\'',('approved' if decision=='approve' else 'rejected',g.admin['username'],req_id))
    if w: db.notify(w['employee_id'],'wfh','Your work-from-home request was '+('approved' if decision=='approve' else 'rejected'),f"For {w['work_date']}",'/wfh-request',req_id)
    flash('WFH request updated.','success'); return redirect(url_for('admin_locations'))

@app.route('/admin/attendance/all.xlsx')
@main_admin_required
def admin_all_attendance_xlsx():
    from openpyxl import Workbook
    with db.conn_ctx() as c:
        rows=[dict(r) for r in c.execute("SELECT a.*,e.full_name,e.username,e.email,e.phone,e.designation,d.name department_name FROM attendance a JOIN employees e ON e.id=a.employee_id LEFT JOIN departments d ON d.id=e.department_id ORDER BY e.full_name,a.work_date DESC")]
    wb=Workbook(); ws=wb.active; ws.title="All Attendance"
    ws.append(["Employee ID","Employee","Username","Email","Phone","Designation","Department","Date","Check In","Check Out","Worked Seconds","Late","Absent","Work Mode","Location","Latitude","Longitude"])
    for r in rows:
        ws.append([r.get("employee_id"),r.get("full_name"),r.get("username"),r.get("email"),r.get("phone"),r.get("designation"),r.get("department_name"),r.get("work_date"),r.get("check_in"),r.get("check_out"),r.get("worked_seconds"),"Yes" if r.get("is_late") else "No","Yes" if r.get("marked_absent") else "No",r.get("work_mode"),r.get("location_name"),r.get("latitude"),r.get("longitude")])
    ws.freeze_panes="A2"; ws.auto_filter.ref=ws.dimensions
    for col in ws.columns: ws.column_dimensions[col[0].column_letter].width=min(max(max(len(str(cell.value or "")) for cell in col)+2,12),32)
    buf=io.BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(buf,mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",as_attachment=True,download_name="all_employees_attendance_history.xlsx")


@app.route('/admin/attendance.xlsx')
@admin_required
def admin_attendance_xlsx():
    try: from openpyxl import Workbook
    except ImportError: abort(500,'Excel export dependency missing. Install requirements.txt.')
    rows=db.attendance_report(**_report_args()); buf=io.BytesIO(); wb=Workbook(); ws=wb.active; ws.title='Attendance'
    ws.append(['Employee ID','Name','Department','Date','Check-in time','Check-out time','Work mode','Location','Latitude','Longitude','Worked','Status'])
    with db.conn_ctx() as c:
        for r in rows:
            extra=c.execute('SELECT work_mode,location_name,latitude,longitude FROM attendance WHERE employee_id=? AND work_date=?',(r['employee_id'],r['work_date'])).fetchone()
            ws.append([r['employee_id'],r['full_name'],r['department_name'],r['work_date'],r['check_in_time'],r['check_out_time'],extra['work_mode'] if extra else 'Office',extra['location_name'] if extra else '',extra['latitude'] if extra else '',extra['longitude'] if extra else '',db.fmt_hm(r['worked']),r['status']])
    ws.freeze_panes='A2'; ws.auto_filter.ref=ws.dimensions
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width=min(max(max(len(str(cell.value or '')) for cell in col)+2,12),32)
    wb.save(buf); buf.seek(0)
    return send_file(buf,mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',as_attachment=True,download_name='attendance_report.xlsx')

@app.route('/messages',methods=['GET','POST'])
@employee_required
def employee_messages():
    if request.method=='POST':
        subject=request.form.get('subject','').strip(); body=request.form.get('body','').strip(); role=request.form.get('recipient_role','Admin')
        if role not in ('HR','Admin','CEO','All (CEO, Admin & HR)'): role='Admin'
        if subject and body:
            with db.conn_ctx() as c:
                mid=c.execute("INSERT INTO messages(employee_id,recipient_role,subject,body,created_at,sender_role) VALUES (?,?,?,?,?,'employee')",(g.emp['id'],role,subject,body,db.now().strftime(db.DT_FMT))).lastrowid
            db.admin_notify('message', f"New message from {g.emp['full_name']}", f"{subject}: {body[:140]}", '/admin/messages', mid)
            flash('Message sent.','success')
        else: flash('Subject and message are required.','error')
        return redirect(url_for('employee_messages'))
    with db.conn_ctx() as c:
        rows=[dict(x) for x in c.execute("SELECT m.*,e.full_name AS sender_name,e.profile_picture FROM messages m JOIN employees e ON e.id=m.employee_id WHERE m.employee_id=? ORDER BY m.created_at DESC",(g.emp['id'],))]
    new_ids=db.unread_ref_ids(g.emp['id'],'message'); db.mark_read_kind(g.emp['id'],'message')
    return render_template('employee/messages.html',active='messages',rows=rows,new_ids=new_ids)

@app.route('/admin/messages',methods=['GET','POST'])
@admin_required
def admin_messages():
    if request.method=='POST':
        action=request.form.get('action','reply')
        with db.conn_ctx() as c:
            if action=='compose':
                subject=request.form.get('subject','').strip()[:150]; body=request.form.get('body','').strip()[:4000]; target=request.form.get('target','')
                if not subject or not body: flash('Subject and message are required.','error')
                else:
                    if target.startswith('employee:'):
                        ids=[int(target.split(':',1)[1])]
                    elif target.startswith('department:'):
                        ids=[r[0] for r in c.execute("SELECT id FROM employees WHERE department_id=? AND status='approved'",(int(target.split(':',1)[1]),))]
                    elif target=='all': ids=[r[0] for r in c.execute("SELECT id FROM employees WHERE status='approved'")]
                    else: ids=[]
                    for eid in ids:
                        _mid=c.execute("INSERT INTO messages(employee_id,recipient_role,subject,body,status,created_at,sender_role,sender_admin,department_id) VALUES(?,?,?,?,?,?,?,?,(SELECT department_id FROM employees WHERE id=?))",(eid,'Employee',subject,body,'sent',db.now().strftime(db.DT_FMT),'admin',g.admin['username'],eid)).lastrowid
                        db.notify(eid,'message','Message from Admin: '+subject,body[:160],'/messages',_mid,conn=c)
                    flash(f'Message delivered to {len(ids)} employee(s).','success' if ids else 'error')
            else:
                mid=request.form.get('message_id',type=int); reply=request.form.get('reply','').strip()
                m=c.execute('SELECT employee_id,subject FROM messages WHERE id=?',(mid,)).fetchone() if mid else None
                if m and reply:
                    c.execute("UPDATE messages SET reply=?,status='replied',replied_at=? WHERE id=?",(reply,db.now().strftime(db.DT_FMT),mid))
                    db.notify(m['employee_id'],'message','Admin replied: '+m['subject'],reply[:160],'/messages',mid,conn=c)
                    flash('Reply sent.','success')
        return redirect(url_for('admin_messages'))
    with db.conn_ctx() as c:
        rows=[dict(x) for x in c.execute("SELECT m.*,e.full_name,e.username,e.profile_picture,d.name AS department_name FROM messages m JOIN employees e ON e.id=m.employee_id LEFT JOIN departments d ON d.id=e.department_id ORDER BY m.created_at DESC")]
    new_ids=db.admin_unread_ref_ids(g.admin['id'],'message'); db.admin_mark_read_kind(g.admin['id'],'message')
    return render_template('admin/messages.html',active='messages',rows=rows,new_ids=new_ids,departments=db.list_departments(),employees=db.list_employees(status='approved'))



# =================================================================== EXTRA FEATURES
# ---- employee: notification history + holidays
@app.route("/notifications", methods=["GET", "POST"])
@employee_required
def employee_notifications():
    if request.method == "POST":
        db.mark_all_read(g.emp["id"])
        flash("All notifications marked as read.", "success")
        return redirect(url_for("employee_notifications"))
    return render_template("employee/notifications.html", active="notifications",
                           items=db.list_notifications(g.emp["id"]))


@app.route("/holidays")
@employee_required
def employee_holidays():
    return render_template("employee/holidays.html", active="holidays", items=db.list_holidays(),
                           today=db.today_str())


# ---- admin: notification history, holidays, audit log, backup, re-enrol face, password reset
@app.route("/admin/notifications", methods=["GET", "POST"])
@admin_required
def admin_notifications():
    if request.method == "POST":
        db.admin_mark_all_read(g.admin["id"])
        flash("All notifications marked as read.", "success")
        return redirect(url_for("admin_notifications"))
    return render_template("admin/notifications.html", active="notifications",
                           items=db.admin_list_notifications(g.admin["id"]))


@app.route("/admin/holidays", methods=["GET", "POST"])
@admin_required
def admin_holidays():
    if request.method == "POST":
        date_s = request.form.get("holiday_date", "").strip()
        name = request.form.get("name", "").strip()
        try:
            datetime.strptime(date_s, "%Y-%m-%d")
            if not name:
                raise ValueError
            db.add_holiday(date_s, name, g.admin["username"])
            db.add_announcement(None, f"Holiday: {name}", f"Office is closed on {date_s} ({name}).")
            flash("Holiday added and all employees were notified.", "success")
        except sqlite3.IntegrityError:
            flash("A holiday already exists on that date.", "error")
        except ValueError:
            flash("Enter a valid date and a holiday name.", "error")
        return redirect(url_for("admin_holidays"))
    return render_template("admin/holidays.html", active="holidays", items=db.list_holidays(), today=db.today_str())


@app.route("/admin/holidays/<int:hid>/delete", methods=["POST"])
@admin_required
def admin_holiday_delete(hid):
    db.delete_holiday(hid)
    flash("Holiday removed.", "success")
    return redirect(url_for("admin_holidays"))


@app.route("/admin/audit")
@main_admin_required
def admin_audit():
    q = request.args.get("q", "").strip()
    return render_template("admin/audit.html", active="audit", items=db.list_audit(q), q=q)


@app.route("/admin/backup.db")
@main_admin_required
def admin_backup():
    """Main admin: download a consistent copy of the whole database (keep it private)."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db.backup_database(tmp)
        with open(tmp, "rb") as fh:
            data = fh.read()
    finally:
        try: os.remove(tmp)
        except OSError: pass
    stamp = db.now().strftime("%Y%m%d_%H%M")
    return Response(data, mimetype="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename=cyberattend_backup_{stamp}.db"})


@app.route("/admin/employee/<int:emp_id>/reenroll", methods=["POST"])
@main_admin_required
def admin_reenroll(emp_id):
    """Wipe an employee's face samples; at the next login they register their face again."""
    emp = db.get_employee(emp_id)
    if not emp:
        abort(404)
    fu.reset_samples(emp_id)
    db.set_samples(emp_id, 0)
    db.set_status(emp_id, "face_pending")
    fu.train_model()
    fu.rebuild_dataset_zip(dataset_manifest())
    flash(f"{emp['full_name']}'s face data was removed. They must register their face again at next login.", "success")
    return redirect(url_for("admin_employee_detail", emp_id=emp_id))


@app.route("/admin/employee/<int:emp_id>/reset-password", methods=["POST"])
@main_admin_required
def admin_reset_password(emp_id):
    emp = db.get_employee(emp_id)
    if not emp:
        abort(404)
    new = request.form.get("new_password", "")
    if len(new) < 8:
        flash("New password must be at least 8 characters.", "error")
    else:
        db.set_employee_password(emp_id, new)
        db.notify(emp_id, "message", "Your password was reset by the admin", "Please log in with the new password and change it in Profile.", "/profile")
        flash(f"Password for {emp['full_name']} was reset.", "success")
    return redirect(url_for("admin_employee_detail", emp_id=emp_id))


# ---- audit trail: every successful admin POST is recorded (passwords/tokens are never stored)
_AUDIT_SKIP = {"admin_api_notifications_read", "admin_login"}


@app.after_request
def audit_admin_actions(resp):
    try:
        if (request.method == "POST" and session.get("role") == "admin" and resp.status_code < 400
                and request.endpoint and request.endpoint not in _AUDIT_SKIP):
            adm = db.get_admin(session.get("admin_id"))
            if adm:
                bits = [f"{k}={v[:40]}" for k, v in request.form.items()
                        if v and not any(w in k.lower() for w in ("pass", "csrf", "token", "secret", "current", "confirm", "new"))]
                db.log_action(adm["username"], request.endpoint.replace("admin_", "").replace("_", " "),
                              ", ".join(bits)[:480], request.remote_addr or "")
    except Exception:
        app.logger.exception("audit log failed")
    return resp


# ------------------------------------------------------------------ installable web app (Add to home screen)
@app.route("/manifest.webmanifest")
def web_manifest():
    data = {
        "name": "CyberAttend", "short_name": "CyberAttend", "start_url": "/", "scope": "/",
        "display": "standalone", "background_color": "#0f172a", "theme_color": "#0f172a",
        "icons": [{"src": url_for("static", filename="icons/icon-192.png"), "sizes": "192x192", "type": "image/png"},
                  {"src": url_for("static", filename="icons/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
    }
    resp = jsonify(data)
    resp.headers["Content-Type"] = "application/manifest+json"
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


# ------------------------------------------------------------------ meeting alarm (admin rings every employee of a department)
@app.route("/admin/meeting-alert", methods=["GET", "POST"])
@admin_required
def admin_meeting_alert():
    if request.method == "POST":
        action = request.form.get("action", "ring")
        if action == "stop":
            db.stop_alarm(request.form.get("alarm_id", type=int) or 0)
            flash("Alarm stopped for everyone.", "success")
        else:
            dept = request.form.get("department_id", type=int) or None
            title = request.form.get("title", "").strip()[:120] or "Meeting starting now"
            msg = request.form.get("message", "").strip()[:300]
            db.create_alarm(dept, title, msg, g.admin["username"])
            if smsclient.enabled():           # bonus: reach phones whose browser is closed
                for ph in db.employees_phones_for_alarm(dept):
                    smsclient.send_async(ph, ("CyberAttend MEETING ALERT: " + title + (" - " + msg if msg else ""))[:300])
            flash("Meeting alarm is ringing now for the selected employees.", "success")
        return redirect(url_for("admin_meeting_alert"))
    return render_template("admin/meeting_alert.html", active="meeting_alert",
                           departments=db.list_departments(), alarms=db.recent_alarms(30))


@app.route("/api/alarm/poll")
def api_alarm_poll():
    if session.get("role") != "employee":
        return jsonify(ok=False), 401
    emp = db.get_employee(session.get("employee_id"))
    if not emp or emp["status"] != "approved":
        return jsonify(ok=False), 401
    resp = jsonify(ok=True, alarms=db.active_alarms_for(emp))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/alarm/ack", methods=["POST"])
def api_alarm_ack():
    if session.get("role") != "employee":
        return jsonify(ok=False), 401
    aid = (request.get_json(silent=True) or {}).get("id")
    if isinstance(aid, int):
        db.ack_alarm(aid, session["employee_id"])
    return jsonify(ok=True)


# ------------------------------------------------------------------ notification ringtone
@app.route("/notification-sound")
def notification_sound():
    """Serves static/sounds/notify.mp3 (your own ringtone) if present, otherwise the built-in notify.wav."""
    folder = os.path.join(app.static_folder, "sounds")
    for name, mime in (("notify.mp3", "audio/mpeg"), ("notify.ogg", "audio/ogg"),
                       ("notify.m4a", "audio/mp4"), ("notify.wav", "audio/wav")):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            resp = send_file(path, mimetype=mime)
            resp.headers["Cache-Control"] = "public, max-age=3600"
            return resp
    abort(404)


# ------------------------------------------------------------------ errors
@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def http_error(e):
    return render_template("error.html", code=e.code, message=getattr(e, "description", "")), e.code


# ------------------------------------------------------------------ Advanced HR suite
@app.route("/admin/advanced-hr", methods=["GET", "POST"])
@admin_required
def admin_advanced_hr():
    if request.method == "POST":
        action = request.form.get("action", "")
        emp_id = request.form.get("employee_id", type=int)
        with db.conn_ctx() as c:
            if action == "payroll" and emp_id:
                c.execute("INSERT INTO payroll(employee_id,pay_month,basic,allowance,bonus,deduction,status,note,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(employee_id,pay_month) DO UPDATE SET basic=excluded.basic,allowance=excluded.allowance,bonus=excluded.bonus,deduction=excluded.deduction,status=excluded.status,note=excluded.note", (emp_id, request.form.get("pay_month", ""), request.form.get("basic", 0, type=float), request.form.get("allowance", 0, type=float), request.form.get("bonus", 0, type=float), request.form.get("deduction", 0, type=float), request.form.get("pay_status", "Pending"), request.form.get("note", "")[:500], db.now().strftime(db.DT_FMT)))
                flash("Payroll record saved.", "success")
            elif action == "review" and emp_id:
                c.execute("INSERT INTO performance_reviews(employee_id,review_period,score,strengths,improvements,reviewer,created_at) VALUES(?,?,?,?,?,?,?)", (emp_id, request.form.get("review_period", "")[:40], max(1,min(5,request.form.get("score", 3, type=int))), request.form.get("strengths", "")[:1000], request.form.get("improvements", "")[:1000], g.admin["username"], db.now().strftime(db.DT_FMT)))
                db.notify(emp_id, "message", "New performance review", "Period: " + request.form.get("review_period", "")[:40], "/my-performance", conn=c)
                flash("Performance review added and shared with the employee.", "success")
            elif action == "project" and emp_id:
                c.execute("INSERT INTO projects(title,description,employee_id,priority,status,start_date,due_date,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (request.form.get("title", "")[:120], request.form.get("description", "")[:1500], emp_id, request.form.get("priority", "Medium"), "Planned", request.form.get("start_date", ""), request.form.get("due_date", ""), g.admin["username"], db.now().strftime(db.DT_FMT)))
                _pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
                db.notify(emp_id, "task", "New project from admin: " + request.form.get("title", "")[:60], request.form.get("description", "")[:140], "/tasks", -_pid, conn=c)
                flash("Project assigned.", "success")
            elif action == "shift" and emp_id:
                c.execute("INSERT INTO employee_shifts(employee_id,shift_name,start_time,end_time,effective_from,effective_to,break_minutes,created_at) VALUES(?,?,?,?,?,?,?,?)", (emp_id, request.form.get("shift_name", "General")[:80], request.form.get("start_time", "09:00"), request.form.get("end_time", "17:00"), request.form.get("effective_from", db.today_str()), request.form.get("effective_to", ""), request.form.get("break_minutes", 60, type=int), db.now().strftime(db.DT_FMT)))
                flash("Employee shift assigned.", "success")
            elif action == "document" and emp_id:
                c.execute("INSERT INTO employee_documents(employee_id,title,document_type,id_number,reference_url,expiry_date,note,created_at) VALUES(?,?,?,?,?,?,?,?)", (emp_id, request.form.get("doc_title", "")[:120], request.form.get("document_type", "Other"), request.form.get("id_number", "").strip()[:40], request.form.get("reference_url", "")[:500], request.form.get("expiry_date", ""), request.form.get("doc_note", "")[:500], db.now().strftime(db.DT_FMT)))
                flash("Document record added.", "success")
            elif action == "expense_decision":
                c.execute("UPDATE expenses SET status=?,admin_note=? WHERE id=?", (request.form.get("status", "Pending"), request.form.get("admin_note", "")[:500], request.form.get("expense_id", type=int)))
                flash("Expense decision saved.", "success")
        return redirect(url_for("admin_advanced_hr"))
    with db.conn_ctx() as c:
        employees = [dict(r) for r in c.execute("SELECT id,full_name,username FROM employees WHERE status='approved' ORDER BY full_name")]
        payroll = [dict(r) for r in c.execute("SELECT p.*,e.full_name FROM payroll p JOIN employees e ON e.id=p.employee_id ORDER BY p.pay_month DESC,p.id DESC LIMIT 200")]
        reviews = [dict(r) for r in c.execute("SELECT r.*,e.full_name FROM performance_reviews r JOIN employees e ON e.id=r.employee_id ORDER BY r.id DESC LIMIT 100")]
        projects = [dict(r) for r in c.execute("SELECT p.*,e.full_name FROM projects p JOIN employees e ON e.id=p.employee_id ORDER BY p.id DESC LIMIT 100")]
        expenses = [dict(r) for r in c.execute("SELECT x.*,e.full_name FROM expenses x JOIN employees e ON e.id=x.employee_id ORDER BY x.id DESC LIMIT 200")]
        shifts = [dict(r) for r in c.execute("SELECT s.*,e.full_name FROM employee_shifts s JOIN employees e ON e.id=s.employee_id ORDER BY s.id DESC LIMIT 100")]
        documents = [dict(r) for r in c.execute("SELECT d.*,e.full_name FROM employee_documents d JOIN employees e ON e.id=d.employee_id ORDER BY d.id DESC LIMIT 100")]
    return render_template("admin/advanced_hr.html", active="advanced_hr", employees=employees, payroll=payroll, reviews=reviews, projects=projects, expenses=expenses, shifts=shifts, documents=documents)

@app.route("/my-hr", methods=["GET", "POST"])
@employee_required
def employee_advanced_hr():
    # HR is a separate, private portal. Employees must not see HR staff tools.
    # Keep this legacy URL working so old bookmarks never show a 404.
    flash("The HR workspace is private. You can view your own salary under My Salary.", "info")
    return redirect(url_for("dashboard"))

# ------------------------------------------------------------------ HR role-aware dashboard
@app.route("/admin/hr-accounts", methods=["GET", "POST"])
@admin_required
def admin_hr_accounts():
    if not g.admin.get("is_main"):
        flash("Only the main admin can create or manage HR accounts.", "error")
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        username=request.form.get("username", "").strip(); password=request.form.get("password", ""); full_name=request.form.get("full_name", "").strip()
        if len(username)<3 or len(password)<8 or not full_name:
            flash("Enter a name, username (at least 3 characters) and password (at least 8 characters).", "error")
        else:
            try:
                with db.conn_ctx() as c:
                    c.execute("INSERT INTO hr_users(username,password_hash,full_name,created_at,created_by) VALUES(?,?,?,?,?)", (username,generate_password_hash(password),full_name,datetime.now().strftime("%Y-%m-%d %H:%M:%S"),g.admin["username"]))
                flash("HR account created. Share its login details securely with the HR staff member.", "success")
            except sqlite3.IntegrityError: flash("That HR username already exists.", "error")
    with db.conn_ctx() as c: users=[dict(r) for r in c.execute("SELECT id,username,full_name,active,created_at,last_login FROM hr_users ORDER BY id DESC")]
    return render_template("admin/hr_accounts.html", users=users, active="hr_accounts")

@app.route("/admin/hr-accounts/<int:user_id>/toggle", methods=["POST"])
@admin_required
def admin_hr_toggle(user_id):
    if not g.admin.get("is_main"): return redirect(url_for("admin_dashboard"))
    with db.conn_ctx() as c: c.execute("UPDATE hr_users SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (user_id,))
    flash("HR account status updated.", "success"); return redirect(url_for("admin_hr_accounts"))

@app.route("/admin/hr-accounts/<int:user_id>/reset", methods=["POST"])
@admin_required
def admin_hr_reset(user_id):
    if not g.admin.get("is_main"): return redirect(url_for("admin_dashboard"))
    pw = request.form.get("password", "")
    if len(pw) < 8:
        flash("The new HR password must be at least 8 characters.", "error")
    else:
        with db.conn_ctx() as c: c.execute("UPDATE hr_users SET password_hash=? WHERE id=?", (generate_password_hash(pw), user_id))
        flash("HR password reset. Share it with the HR staff member securely.", "success")
    return redirect(url_for("admin_hr_accounts"))

@app.route("/hr")
def hr_portal_entry():
    # Dedicated HR sign-in; employee sessions do not grant HR access.
    return redirect(url_for("hr.login"))


@app.route("/hr-dashboard")
def hr_dashboard():
    # Legacy URL: HR staff must use their independent HR portal.
    if session.get("role") == "hr":
        return redirect(url_for("hr.dashboard"))
    return redirect(url_for("hr.login"))


# ------------------------------------------------------------------ SALARIES (admin pays everyone; leave deductions are the admin's choice)
@app.route("/admin/salaries/setup", methods=["GET", "POST"])
@admin_required
def admin_salary_setup():
    if request.method == "POST":
        saved = bad = 0
        with db.conn_ctx() as c:
            for emp_id in [r[0] for r in c.execute("SELECT id FROM employees WHERE status='approved'")]:
                if f"base_{emp_id}" not in request.form: continue
                base = hrcore.clean_amount(request.form.get(f"base_{emp_id}"))
                allow = hrcore.clean_amount(request.form.get(f"allow_{emp_id}"))
                if base is None or allow is None:
                    bad += 1; continue
                c.execute("INSERT INTO salary_profiles(employee_id,base_salary,allowance,updated_by,updated_at) VALUES (?,?,?,?,?) "
                          "ON CONFLICT(employee_id) DO UPDATE SET base_salary=excluded.base_salary,allowance=excluded.allowance,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                          (emp_id, base, allow, g.admin["username"], db.now().strftime(db.DT_FMT)))
                saved += 1
        flash(f"Salary details saved for {saved} employee(s)." + (f" {bad} row(s) had invalid amounts and were skipped." if bad else ""), "error" if bad else "success")
        return redirect(url_for("admin_salary_setup"))
    with db.conn_ctx() as c:
        rows = [dict(r) for r in c.execute("SELECT e.id,e.full_name,e.username,e.designation,COALESCE(s.base_salary,0) base_salary,COALESCE(s.allowance,0) allowance,s.updated_at,s.updated_by FROM employees e LEFT JOIN salary_profiles s ON s.employee_id=e.id WHERE e.status='approved' ORDER BY e.full_name")]
    return render_template("admin/salary_setup.html", active="salaries", rows=rows)


@app.route("/admin/salaries", methods=["GET", "POST"])
@admin_required
def admin_salaries():
    month = request.values.get("month") or hrcore.current_month()
    if not hrcore.valid_month(month): month = hrcore.current_month()
    if request.method == "POST":
        action = request.form.get("action", "")
        pid = request.form.get("payroll_id", type=int)
        with db.conn_ctx() as c:
            if action == "generate":
                created, refreshed, skipped = hrcore.generate_payroll(c, month, g.admin["username"])
                flash(f"Payroll for {month}: {created} created, {refreshed} refreshed" + (f", {skipped} employee(s) skipped because no salary is set yet." if skipped else "."), "success")
            elif action in ("save", "save_pay") and pid:
                leave_amounts = {int(k[10:]): v for k, v in request.form.items() if k.startswith("leave_ded_") and k[10:].isdigit()}
                ok, msg = hrcore.save_payroll_row(c, pid, bonus=request.form.get("bonus", 0), other_deduction=request.form.get("other_deduction", 0),
                                                  note=request.form.get("note", ""), leave_amounts=leave_amounts, admin_label=g.admin["username"])
                if ok and action == "save_pay":
                    hrcore.pay_payroll_row(c, pid, g.admin["username"]); msg = "Salary saved and paid."
                flash(msg, "success" if ok else "error")
            elif action == "pay" and pid:
                flash("Salary paid." if hrcore.pay_payroll_row(c, pid, g.admin["username"]) else "Nothing to pay.", "success")
            elif action == "pay_all":
                flash(f"{hrcore.pay_all(c, month, g.admin['username'])} salaries paid for {month}.", "success")
            elif action == "reopen" and pid:
                hrcore.reopen_payroll_row(c, pid); flash("Salary re-opened for editing.", "success")
            elif action in ("hold", "unhold") and pid:
                c.execute("UPDATE payroll SET status=? WHERE id=? AND status<>'Paid'", ("On Hold" if action == "hold" else "Pending", pid))
                flash("Salary status updated.", "success")
        return redirect(url_for("admin_salaries", month=month))
    with db.conn_ctx() as c:
        rows = hrcore.payroll_overview(c, month)
        no_salary = hrcore.safe_count(c, "SELECT COUNT(*) FROM employees e LEFT JOIN salary_profiles s ON s.employee_id=e.id WHERE e.status='approved' AND COALESCE(s.base_salary,0)<=0")
    return render_template("admin/salaries.html", active="salaries", month=month, rows=rows, totals=hrcore.payroll_totals(rows), no_salary=no_salary)


@app.route("/my-salary")
@employee_required
def employee_salary():
    with db.conn_ctx() as c:
        rows = []
        for r in c.execute("SELECT * FROM payroll WHERE employee_id=? ORDER BY pay_month DESC", (g.emp["id"],)):
            d = dict(r); d["net"] = hrcore.net_salary(r); d["leaves"] = hrcore.month_leaves(c, g.emp["id"], r["pay_month"]); rows.append(d)
        prof = c.execute("SELECT base_salary, allowance FROM salary_profiles WHERE employee_id=?", (g.emp["id"],)).fetchone()
    db.mark_read_kind(g.emp["id"], "salary")
    return render_template("employee/salary.html", active="salary", rows=rows, prof=dict(prof) if prof else None)


@app.route("/my-performance")
@employee_required
def employee_performance():
    """Every employee can see the performance reviews HR/admin has given to them (read-only)."""
    with db.conn_ctx() as c:
        reviews = [dict(r) for r in c.execute(
            "SELECT id, review_period, score, strengths, improvements, reviewer, created_at "
            "FROM performance_reviews WHERE employee_id=? ORDER BY id DESC", (g.emp["id"],))]
        # opening the page marks the "New performance review" notifications as read
        c.execute("UPDATE notifications SET is_read=1 WHERE employee_id=? AND url='/my-performance'", (g.emp["id"],))
    avg = round(sum(r["score"] for r in reviews) / len(reviews), 1) if reviews else None
    return render_template("employee/performance.html", active="performance", reviews=reviews,
                           latest=reviews[0] if reviews else None, avg=avg)


# ------------------------------------------------------------------ Excel reporting for Admin and Employee workspaces
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

def _excel_workbook(sheets):
    """Build a clean, formatted multi-sheet Excel workbook in memory."""
    output = io.BytesIO()
    wb = Workbook()
    wb.remove(wb.active)
    for sheet_name, headers, rows in sheets:
        ws = wb.create_sheet(sheet_name[:31])
        ws.append(headers)
        for row in rows:
            ws.append([row.get(h, "") if hasattr(row, "get") else v for h, v in zip(headers, row)] if not isinstance(row, dict) else [row.get(h, "") for h in headers])
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="183153")
            cell.alignment = Alignment(horizontal="center")
        for col in ws.columns:
            width = min(max(max(len(str(c.value or "")) for c in col) + 2, 12), 38)
            ws.column_dimensions[get_column_letter(col[0].column)].width = width
    wb.save(output)
    output.seek(0)
    return output

def _fetch_excel_rows(c, query, params=()):
    try:
        return [dict(r) for r in c.execute(query, params).fetchall()]
    except sqlite3.OperationalError:
        return []

@app.route("/admin/hr/export.xlsx")
@admin_required
def admin_hr_excel_export():
    with db.conn_ctx() as c:
        sheets = [
            ("Employees", ["id","employee_code","full_name","username","email","phone","designation","employment_type","work_location","manager_name","joining_date","skills","education","status"], _fetch_excel_rows(c, "SELECT id,employee_code,full_name,username,email,phone,designation,employment_type,work_location,manager_name,joining_date,skills,education,status FROM employees ORDER BY full_name")),
            ("Attendance", ["employee_id","full_name","work_date","check_in","check_out","worked_seconds","is_late","work_mode","location_name"], _fetch_excel_rows(c, "SELECT a.employee_id,e.full_name,a.work_date,a.check_in,a.check_out,a.worked_seconds,a.is_late,a.work_mode,a.location_name FROM attendance a JOIN employees e ON e.id=a.employee_id ORDER BY a.work_date DESC")),
            ("Leave Requests", ["id","employee_id","full_name","leave_date","leave_type","units","reason","status","admin_note"], _fetch_excel_rows(c, "SELECT l.id,l.employee_id,e.full_name,l.leave_date,l.leave_type,l.units,l.reason,l.status,l.admin_note FROM leave_requests l JOIN employees e ON e.id=l.employee_id ORDER BY l.id DESC")),
            ("Payroll", ["id","employee_id","full_name","pay_month","basic","allowance","bonus","deduction","status","paid_at","note"], _fetch_excel_rows(c, "SELECT p.id,p.employee_id,e.full_name,p.pay_month,p.basic,p.allowance,p.bonus,p.deduction,p.status,p.paid_at,p.note FROM payroll p JOIN employees e ON e.id=p.employee_id ORDER BY p.pay_month DESC")),
            ("Projects", ["id","employee_id","full_name","title","priority","status","progress","start_date","due_date","description"], _fetch_excel_rows(c, "SELECT p.id,p.employee_id,e.full_name,p.title,p.priority,p.status,p.progress,p.start_date,p.due_date,p.description FROM projects p JOIN employees e ON e.id=p.employee_id ORDER BY p.id DESC")),
            ("Expenses", ["id","employee_id","full_name","category","amount","expense_date","description","status","admin_note"], _fetch_excel_rows(c, "SELECT x.id,x.employee_id,e.full_name,x.category,x.amount,x.expense_date,x.description,x.status,x.admin_note FROM expenses x JOIN employees e ON e.id=x.employee_id ORDER BY x.id DESC")),
            ("Performance", ["id","employee_id","full_name","review_period","score","strengths","improvements","reviewer","created_at"], _fetch_excel_rows(c, "SELECT r.id,r.employee_id,e.full_name,r.review_period,r.score,r.strengths,r.improvements,r.reviewer,r.created_at FROM performance_reviews r JOIN employees e ON e.id=r.employee_id ORDER BY r.id DESC")),
            ("Shifts", ["id","employee_id","full_name","shift_name","start_time","end_time","effective_from","effective_to","break_minutes"], _fetch_excel_rows(c, "SELECT s.id,s.employee_id,e.full_name,s.shift_name,s.start_time,s.end_time,s.effective_from,s.effective_to,s.break_minutes FROM employee_shifts s JOIN employees e ON e.id=s.employee_id ORDER BY s.id DESC")),
        ]
    return send_file(_excel_workbook(sheets), as_attachment=True, download_name="Muzammill_HRMS_Admin_Report.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.route("/my-hr/export.xlsx")
@employee_required
def employee_hr_excel_export():
    eid = g.emp["id"]
    with db.conn_ctx() as c:
        sheets = [
            ("My Profile", ["id","employee_code","full_name","username","email","phone","designation","department_id","employment_type","work_location","manager_name","joining_date","skills","education","bio"], _fetch_excel_rows(c, "SELECT id,employee_code,full_name,username,email,phone,designation,department_id,employment_type,work_location,manager_name,joining_date,skills,education,bio FROM employees WHERE id=?", (eid,))),
            ("My Attendance", ["work_date","check_in","check_out","worked_seconds","is_late","work_mode","location_name"], _fetch_excel_rows(c, "SELECT work_date,check_in,check_out,worked_seconds,is_late,work_mode,location_name FROM attendance WHERE employee_id=? ORDER BY work_date DESC", (eid,))),
            ("My Leave", ["leave_date","leave_type","units","reason","status","admin_note"], _fetch_excel_rows(c, "SELECT leave_date,leave_type,units,reason,status,admin_note FROM leave_requests WHERE employee_id=? ORDER BY id DESC", (eid,))),
            ("My Payroll", ["pay_month","basic","allowance","bonus","deduction","status","paid_at","note"], _fetch_excel_rows(c, "SELECT pay_month,basic,allowance,bonus,deduction,status,paid_at,note FROM payroll WHERE employee_id=? ORDER BY pay_month DESC", (eid,))),
            ("My Projects", ["title","description","priority","status","progress","start_date","due_date"], _fetch_excel_rows(c, "SELECT title,description,priority,status,progress,start_date,due_date FROM projects WHERE employee_id=? ORDER BY id DESC", (eid,))),
            ("My Expenses", ["category","amount","expense_date","description","status","admin_note"], _fetch_excel_rows(c, "SELECT category,amount,expense_date,description,status,admin_note FROM expenses WHERE employee_id=? ORDER BY id DESC", (eid,))),
            ("My Shifts", ["shift_name","start_time","end_time","effective_from","effective_to","break_minutes"], _fetch_excel_rows(c, "SELECT shift_name,start_time,end_time,effective_from,effective_to,break_minutes FROM employee_shifts WHERE employee_id=? ORDER BY id DESC", (eid,))),
        ]
    return send_file(_excel_workbook(sheets), as_attachment=True, download_name="My_HRMS_Report.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.route("/admin/hr/excel-template.xlsx")
@admin_required
def admin_hr_excel_template():
    sheets = [
        ("Payroll Upload", ["employee_id","pay_month_YYYY-MM","basic","allowance","bonus","deduction","status","note"], []),
        ("Employee Import Guide", ["Field","Required","Example"], [
            {"Field":"employee_id","Required":"Yes - existing employee ID","Example":"12"},
            {"Field":"pay_month_YYYY-MM","Required":"Yes","Example":"2026-10"},
            {"Field":"basic","Required":"Yes - number","Example":"50000"},
            {"Field":"allowance / bonus / deduction","Required":"Optional - number","Example":"5000"},
            {"Field":"status","Required":"Pending, Paid, or On Hold","Example":"Pending"},
        ]),
    ]
    return send_file(_excel_workbook(sheets), as_attachment=True, download_name="HRMS_Excel_Import_Template.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.route("/admin/hr/import-payroll.xlsx", methods=["POST"])
@admin_required
def admin_hr_excel_import_payroll():
    from openpyxl import load_workbook
    upload = request.files.get("file")
    if not upload or not upload.filename.lower().endswith(".xlsx"):
        flash("Choose an .xlsx payroll workbook.", "error")
        return redirect(url_for("admin_advanced_hr"))
    try:
        wb = load_workbook(upload, read_only=True, data_only=True)
        ws = wb["Payroll Upload"] if "Payroll Upload" in wb.sheetnames else wb.active
        records = list(ws.iter_rows(values_only=True))
        if not records:
            raise ValueError("The workbook is empty.")
        headers = [str(v or "").strip().lower() for v in records[0]]
        required = ["employee_id", "pay_month_yyyy-mm", "basic"]
        if not all(x in headers for x in required):
            raise ValueError("Required columns: employee_id, pay_month_YYYY-MM, basic.")
        idx = {name: headers.index(name) for name in headers}
        imported = 0
        with db.conn_ctx() as c:
            for row in records[1:]:
                if not row or all(v is None for v in row):
                    continue
                def val(key, default=None):
                    i = idx.get(key)
                    return row[i] if i is not None and i < len(row) and row[i] is not None else default
                emp_id = int(val("employee_id"))
                month = str(val("pay_month_yyyy-mm", ""))[:7]
                if not re.fullmatch(r"\d{4}-\d{2}", month):
                    raise ValueError(f"Invalid pay month for employee {emp_id}.")
                basic = float(val("basic", 0))
                allowance, bonus, deduction = (float(val(k, 0) or 0) for k in ("allowance", "bonus", "deduction"))
                status = str(val("status", "Pending"))
                if status not in ("Pending", "Paid", "On Hold") or min(basic, allowance, bonus, deduction) < 0:
                    raise ValueError(f"Invalid salary values or status for employee {emp_id}.")
                if not c.execute("SELECT 1 FROM employees WHERE id=?", (emp_id,)).fetchone():
                    raise ValueError(f"Employee ID {emp_id} does not exist.")
                c.execute("INSERT INTO payroll(employee_id,pay_month,basic,allowance,bonus,deduction,status,note,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(employee_id,pay_month) DO UPDATE SET basic=excluded.basic,allowance=excluded.allowance,bonus=excluded.bonus,deduction=excluded.deduction,status=excluded.status,note=excluded.note", (emp_id,month,basic,allowance,bonus,deduction,status,str(val("note", ""))[:500],db.now().strftime(db.DT_FMT)))
                imported += 1
        flash(f"Imported or updated {imported} payroll rows from Excel.", "success")
    except Exception as exc:
        flash(f"Excel import failed: {exc}", "error")
    return redirect(url_for("admin_advanced_hr"))


import hr_portal
hr_portal.register(app)
import enterprise_upgrade
enterprise_upgrade.register(app)
import archive_viewer
archive_viewer.register(app)


if __name__ == "__main__":
    ssl_ctx = "adhoc" if os.environ.get("HTTPS") == "1" else None
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True,
            debug=os.environ.get("DEBUG") == "1", ssl_context=ssl_ctx)
