"""hr_portal.py - the SEPARATE HR portal  (/hr/...)

HR staff have their own login, their own layout and their own tools, exactly like the Admin and
Employee portals. Everything is connected to the same database, so:

  employee requests leave  ->  HR reviews it  ->  admin is told and decides the salary deduction
  employee sends expense   ->  HR reviews it  ->  employee + admin are notified
  HR reviews / shifts / documents / projects  ->  appear in the employee's own HR workspace
  HR hires a candidate     ->  admin is told to create the account and set the salary

What HR can NOT do (admin-only powers are deliberately left out):
  add/remove admins, change settings, approve registrations, delete employees, reset passwords or
  face data, see audit log / backups / dataset, set salaries, set leave deductions or pay salaries.
"""
import io
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps

from flask import (Blueprint, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import database as db
import hrcore

bp = Blueprint("hr", __name__, url_prefix="/hr")

EMPLOYMENT_TYPES = ("Full-time", "Part-time", "Contract", "Intern", "Probation")
PRIORITIES = ("Low", "Medium", "High", "Urgent")
PROJECT_STATUSES = ("Planned", "In Progress", "On Hold", "Completed", "Cancelled")
DOC_TYPES = ("CNIC / ID", "Contract", "Certificate", "Offer letter", "Medical", "Other")
EXPENSE_STATUSES = ("Pending", "Approved", "Rejected")
STAGES = ("Applied", "Screening", "Interview", "Offer", "Hired", "Rejected")


# ------------------------------------------------------------------ helpers
_fails = defaultdict(list)


def _throttled(key, limit=5, window=600):
    t = time.time()
    _fails[key] = [x for x in _fails[key] if t - x < window]
    return len(_fails[key]) >= limit


def _date(v):
    v = (v or "").strip()
    try:
        datetime.strptime(v, "%Y-%m-%d")
        return v
    except ValueError:
        return ""


def _clean_id_number(doc_type, raw):
    """CNIC -> 12345-1234567-1 (13 digits). Other documents: free text, trimmed. Returns (value, error)."""
    raw = (raw or "").strip()[:40]
    if doc_type == "CNIC / ID" and raw:
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) == 13:
            return f"{digits[:5]}-{digits[5:12]}-{digits[12]}", None
        return raw, "A CNIC has 13 digits (example 35202-1234567-1). Nothing was saved - please correct it."
    return raw, None


def _label():
    return f"HR: {g.hr['full_name']}"


def _employees(c):
    return [dict(r) for r in c.execute("SELECT id, full_name, username FROM employees WHERE status='approved' ORDER BY full_name")]


def _departments(c):
    return [dict(r) for r in c.execute("SELECT id, name FROM departments ORDER BY name")]


def hr_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if session.get("role") != "hr":
            return redirect(url_for("hr.login"))
        with db.conn_ctx() as c:
            u = c.execute("SELECT * FROM hr_users WHERE id=? AND active=1", (session.get("hr_id"),)).fetchone()
        if not u:                      # disabled / removed by the main admin
            session.clear()
            flash("Your HR access is no longer active. Please contact the administrator.", "error")
            return redirect(url_for("hr.login"))
        if u["expires_at"] and u["expires_at"] <= db.now().strftime(db.DT_FMT):
            session.clear()
            flash("Your temporary HR assignment has expired.", "error")
            return redirect(url_for("hr.login"))
        g.hr = dict(u)
        return f(*a, **k)
    return wrapper


def _xlsx(sheets, filename):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    wb.remove(wb.active)
    for name, headers, rows in sheets:
        ws = wb.create_sheet(name[:31])
        ws.append(headers)
        for r in rows:
            ws.append([r.get(h, "") if isinstance(r, dict) else r[i] for i, h in enumerate(headers)])
        ws.freeze_panes = "A2"
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="183153")
            cell.alignment = Alignment(horizontal="center")
        for col in ws.columns:
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(max(len(str(c.value or "")) for c in col) + 2, 12), 40)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=filename,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.app_context_processor
def _inject():
    """Counters for the HR sidebar (live, computed on each page load)."""
    if g.get("hr") is None:
        return {}
    with db.conn_ctx() as c:
        today = db.today_str()
        soon = (db.now() + timedelta(days=30)).strftime(db.D_FMT)
        return {"hr_counts": {
            "leaves": hrcore.safe_count(c, "SELECT COUNT(*) FROM leave_requests WHERE status='pending'"),
            "expenses": hrcore.safe_count(c, "SELECT COUNT(*) FROM expenses WHERE status='Pending'"),
            "messages": hrcore.safe_count(c, "SELECT COUNT(*) FROM messages WHERE status='open' AND recipient_role IN ('HR','All (CEO, Admin & HR)')"),
            "docs": hrcore.safe_count(c, "SELECT COUNT(*) FROM employee_documents WHERE expiry_date<>'' AND expiry_date<=? AND expiry_date>=?", (soon, today)),
        }}


@bp.after_request
def _audit(resp):
    """Every successful HR change is written to the same activity log the main admin reads."""
    try:
        if request.method == "POST" and session.get("role") == "hr" and resp.status_code < 400 and request.endpoint != "hr.login":
            bits = [f"{k}={v[:40]}" for k, v in request.form.items()
                    if v and not any(w in k.lower() for w in ("pass", "csrf", "token", "secret", "current", "confirm", "new"))]
            db.log_action("HR:" + session.get("hr_name", "?"), (request.endpoint or "").replace("hr.", "").replace("_", " "),
                          ", ".join(bits)[:480], request.remote_addr or "")
    except Exception:
        pass
    return resp


# ------------------------------------------------------------------ login / logout / account
@bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("role") == "hr":
        return redirect(url_for("hr.dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        key = f"hr:{request.remote_addr}:{username.lower()}"
        if _throttled(key):
            flash("Too many failed attempts. Try again in a few minutes.", "error")
            return render_template("hr/login.html")
        with db.conn_ctx() as c:
            row = c.execute("SELECT * FROM hr_users WHERE username=? AND active=1", (username,)).fetchone()
        if not row or not check_password_hash(row["password_hash"], password):
            _fails[key].append(time.time())
            flash("Invalid HR username or password.", "error")
            return render_template("hr/login.html")
        _fails.pop(key, None)
        session.clear()
        session.permanent = True
        session["role"] = "hr"
        session["hr_id"] = row["id"]
        session["hr_name"] = row["full_name"]
        with db.conn_ctx() as c:
            c.execute("UPDATE hr_users SET last_login=? WHERE id=?", (db.now().strftime(db.DT_FMT), row["id"]))
        return redirect(url_for("hr.dashboard"))
    return render_template("hr/login.html")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("hr.login"))


@bp.route("/account", methods=["GET", "POST"])
@hr_required
def account():
    if request.method == "POST":
        cur, new, conf = request.form.get("current_password", ""), request.form.get("new_password", ""), request.form.get("confirm_password", "")
        if not check_password_hash(g.hr["password_hash"], cur):
            flash("Your current password is not correct.", "error")
        elif len(new) < 8 or new != conf:
            flash("The new password must be at least 8 characters and both entries must match.", "error")
        else:
            with db.conn_ctx() as c:
                c.execute("UPDATE hr_users SET password_hash=? WHERE id=?", (generate_password_hash(new), g.hr["id"]))
            flash("Password changed.", "success")
        return redirect(url_for("hr.account"))
    return render_template("hr/account.html", active="account")


# ------------------------------------------------------------------ dashboard
@bp.route("/")
@bp.route("/dashboard")
@hr_required
def dashboard():
    today = db.today_str()
    soon = (db.now() + timedelta(days=30)).strftime(db.D_FMT)
    with db.conn_ctx() as c:
        q = lambda sql, p=(): hrcore.safe_count(c, sql, p)
        metrics = {
            "employees": q("SELECT COUNT(*) FROM employees WHERE status='approved'"),
            "present": q("SELECT COUNT(*) FROM attendance WHERE work_date=?", (today,)),
            "late": q("SELECT COUNT(*) FROM attendance WHERE work_date=? AND is_late=1", (today,)),
            "on_leave": q("SELECT COUNT(*) FROM leave_requests WHERE status='approved' AND leave_date=?", (today,)),
            "pending_leaves": q("SELECT COUNT(*) FROM leave_requests WHERE status='pending'"),
            "pending_expenses": q("SELECT COUNT(*) FROM expenses WHERE status='Pending'"),
            "open_messages": q("SELECT COUNT(*) FROM messages WHERE status='open' AND recipient_role IN ('HR','All (CEO, Admin & HR)')"),
            "open_jobs": q("SELECT COUNT(*) FROM job_openings WHERE status='Open'"),
            "candidates": q("SELECT COUNT(*) FROM candidates WHERE stage NOT IN ('Hired','Rejected')"),
            "expiring_docs": q("SELECT COUNT(*) FROM employee_documents WHERE expiry_date<>'' AND expiry_date<=? AND expiry_date>=?", (soon, today)),
        }
        pending = [dict(r) for r in c.execute("SELECT l.id, l.leave_date, l.leave_type, l.reason, e.full_name FROM leave_requests l JOIN employees e ON e.id=l.employee_id WHERE l.status='pending' ORDER BY l.leave_date LIMIT 6")]
        upcoming = [dict(r) for r in c.execute("SELECT l.leave_date, l.leave_type, e.full_name FROM leave_requests l JOIN employees e ON e.id=l.employee_id WHERE l.status='approved' AND l.leave_date>=? ORDER BY l.leave_date LIMIT 6", (today,))]
        expiring = [dict(r) for r in c.execute("SELECT d.title, d.document_type, d.expiry_date, e.full_name FROM employee_documents d JOIN employees e ON e.id=d.employee_id WHERE d.expiry_date<>'' AND d.expiry_date<=? AND d.expiry_date>=? ORDER BY d.expiry_date LIMIT 6", (soon, today))]
        expenses = [dict(r) for r in c.execute("SELECT x.category, x.amount, x.expense_date, e.full_name FROM expenses x JOIN employees e ON e.id=x.employee_id WHERE x.status='Pending' ORDER BY x.id DESC LIMIT 5")]
    return render_template("hr/dashboard.html", active="dashboard", metrics=metrics, pending=pending, upcoming=upcoming, expiring=expiring, expenses=expenses)


# ------------------------------------------------------------------ certificates
@bp.route("/certificates", methods=["GET", "POST"])
@hr_required
def certificates():
    if request.method == "POST":
        emp_id = request.form.get("employee_id", type=int)
        title = (request.form.get("certificate_title") or "Certificate of Achievement").strip()[:120]
        period = (request.form.get("period") or "").strip()[:120]
        description = (request.form.get("description") or "").strip()[:2000]
        emp = db.get_employee(emp_id) if emp_id else None
        if not emp or emp.get("status") != "approved" or not period or not description:
            flash("Select an approved employee and complete the certificate details.", "error")
        else:
            with db.conn_ctx() as c:
                cur = c.execute("""INSERT INTO certificates(employee_id,recipient_name,certificate_title,period,description,issued_by,issued_at)
                    VALUES (?,?,?,?,?,?,?)""", (emp_id, emp.get("full_name", ""), title or "Certificate of Achievement", period, description,
                    _label(), db.now().strftime(db.DT_FMT)))
                cert_id = cur.lastrowid
            db.notify(emp_id, "certificate", "New certificate issued", title, "/my-certificates", cert_id)
            db.log_action(_label(), "certificate issued", f"employee_id={emp_id}; certificate_id={cert_id}", request.remote_addr or "")
            return redirect(url_for("hr.certificate_view", cert_id=cert_id))
    with db.conn_ctx() as c:
        rows = [dict(r) for r in c.execute("""SELECT c.*, e.full_name AS employee_name, e.profile_picture FROM certificates c
            LEFT JOIN employees e ON e.id=c.employee_id ORDER BY c.id DESC LIMIT 150""")]
    return render_template("hr/certificates.html", active="certificates", employees=db.list_employees(status="approved"), certificates=rows)


@bp.route("/certificates/<int:cert_id>")
@hr_required
def certificate_view(cert_id):
    with db.conn_ctx() as c:
        cert = c.execute("""SELECT c.*, e.full_name AS employee_name, e.profile_picture FROM certificates c
            LEFT JOIN employees e ON e.id=c.employee_id WHERE c.id=?""", (cert_id,)).fetchone()
    if not cert:
        abort(404)
    return render_template("admin/certificate_view.html", cert=dict(cert), back_url=url_for("hr.certificates"))


# ------------------------------------------------------------------ employees
@bp.route("/employees")
@hr_required
def employees():
    q = request.args.get("q", "").strip()
    dept = request.args.get("dept", type=int)
    sql = ("SELECT e.id, e.full_name, e.username, e.designation, e.employee_code, e.employment_type, e.joining_date, e.phone, e.email, d.name dept "
           "FROM employees e LEFT JOIN departments d ON d.id=e.department_id WHERE e.status='approved'")
    p = []
    if q:
        sql += " AND (e.full_name LIKE ? OR e.username LIKE ? OR e.designation LIKE ? OR e.employee_code LIKE ?)"
        p += [f"%{q}%"] * 4
    if dept:
        sql += " AND e.department_id=?"
        p.append(dept)
    with db.conn_ctx() as c:
        rows = [dict(r) for r in c.execute(sql + " ORDER BY e.full_name", p)]
        depts = _departments(c)
    return render_template("hr/employees.html", active="employees", rows=rows, depts=depts, q=q, dept=dept)


@bp.route("/employees/<int:emp_id>", methods=["GET", "POST"])
@hr_required
def employee_detail(emp_id):
    with db.conn_ctx() as c:
        emp = c.execute("SELECT e.id, e.full_name, e.username, e.email, e.phone, e.designation, e.employee_code, e.employment_type, e.work_location, "
                        "e.manager_name, e.joining_date, e.skills, e.education, e.bio, e.department_id, d.name dept "
                        "FROM employees e LEFT JOIN departments d ON d.id=e.department_id WHERE e.id=? AND e.status='approved'", (emp_id,)).fetchone()
        if not emp:
            abort(404, "Employee not found.")
        if request.method == "POST":
            etype = request.form.get("employment_type", "Full-time")
            c.execute("UPDATE employees SET designation=?, employee_code=?, employment_type=?, work_location=?, manager_name=?, joining_date=?, skills=?, education=? WHERE id=?",
                      (request.form.get("designation", "")[:80], request.form.get("employee_code", "")[:30],
                       etype if etype in EMPLOYMENT_TYPES else "Full-time", request.form.get("work_location", "")[:80],
                       request.form.get("manager_name", "")[:80], _date(request.form.get("joining_date")),
                       request.form.get("skills", "")[:300], request.form.get("education", "")[:300], emp_id))
            flash("Employee HR profile updated.", "success")
            return redirect(url_for("hr.employee_detail", emp_id=emp_id))
        month_start = db.now().strftime("%Y-%m-01")
        att = c.execute("SELECT COUNT(*) days, COALESCE(SUM(is_late),0) late, COALESCE(SUM(worked_seconds),0) secs FROM attendance WHERE employee_id=? AND work_date>=?", (emp_id, month_start)).fetchone()
        c.execute("INSERT OR IGNORE INTO leave_balances(employee_id) VALUES (?)", (emp_id,))
        data = dict(
            balance=dict(c.execute("SELECT * FROM leave_balances WHERE employee_id=?", (emp_id,)).fetchone()),
            leaves=[dict(r) for r in c.execute("SELECT * FROM leave_requests WHERE employee_id=? ORDER BY leave_date DESC LIMIT 10", (emp_id,))],
            reviews=[dict(r) for r in c.execute("SELECT * FROM performance_reviews WHERE employee_id=? ORDER BY id DESC LIMIT 5", (emp_id,))],
            projects=[dict(r) for r in c.execute("SELECT * FROM projects WHERE employee_id=? ORDER BY id DESC LIMIT 5", (emp_id,))],
            shifts=[dict(r) for r in c.execute("SELECT * FROM employee_shifts WHERE employee_id=? ORDER BY id DESC LIMIT 3", (emp_id,))],
            documents=[dict(r) for r in c.execute("SELECT * FROM employee_documents WHERE employee_id=? ORDER BY id DESC LIMIT 8", (emp_id,))],
        )
    return render_template("hr/employee_detail.html", active="employees", e=dict(emp), att=dict(att), types=EMPLOYMENT_TYPES, **data)


# ------------------------------------------------------------------ leave management
@bp.route("/leaves", methods=["GET", "POST"])
@hr_required
def leaves():
    if request.method == "POST":
        action = request.form.get("action")
        with db.conn_ctx() as c:
            if action == "review":
                ok, msg = hrcore.review_leave(c, request.form.get("request_id", type=int), request.form.get("decision"),
                                              request.form.get("note", ""), _label(), by_hr=True)
                flash(msg + (" The admin has been told to decide any salary deduction." if ok and "approved" in msg else ""), "success" if ok else "error")
            elif action == "allowance":
                eid, total = request.form.get("employee_id", type=int), request.form.get("annual_total", type=int)
                if eid and total is not None and 0 <= total <= 365:
                    c.execute("INSERT OR IGNORE INTO leave_balances(employee_id) VALUES (?)", (eid,))
                    c.execute("UPDATE leave_balances SET annual_total=? WHERE employee_id=?", (total, eid))
                    flash("Annual leave allowance updated.", "success")
                else:
                    flash("Enter a valid allowance (0-365).", "error")
        return redirect(url_for("hr.leaves", status=request.args.get("status", "")))
    status = request.args.get("status", "")
    with db.conn_ctx() as c:
        c.execute("INSERT OR IGNORE INTO leave_balances(employee_id) SELECT id FROM employees WHERE status='approved'")
        sql = "SELECT l.*, e.full_name FROM leave_requests l JOIN employees e ON e.id=l.employee_id"
        p = []
        if status in ("pending", "approved", "rejected"):
            sql += " WHERE l.status=?"
            p.append(status)
        reqs = [dict(r) for r in c.execute(sql + " ORDER BY CASE l.status WHEN 'pending' THEN 0 ELSE 1 END, l.leave_date DESC LIMIT 300", p)]
        balances = [dict(r) for r in c.execute("SELECT e.id, e.full_name, b.annual_total, b.annual_used FROM employees e JOIN leave_balances b ON b.employee_id=e.id WHERE e.status='approved' ORDER BY e.full_name")]
    return render_template("hr/leaves.html", active="leaves", reqs=reqs, balances=balances, status=status)


# ------------------------------------------------------------------ attendance
def _attendance_args():
    start = _date(request.args.get("from")) or db.now().strftime("%Y-%m-01")
    end = _date(request.args.get("to")) or db.today_str()
    return start, end, request.args.get("employee_id", type=int), request.args.get("dept", type=int)


def _attendance_data(c, start, end, emp_id, dept):
    where, p = "a.work_date BETWEEN ? AND ?", [start, end]
    if emp_id:
        where += " AND a.employee_id=?"
        p.append(emp_id)
    if dept:
        where += " AND e.department_id=?"
        p.append(dept)
    summary = [dict(r) for r in c.execute(
        f"SELECT e.id, e.full_name, d.name dept, COUNT(*) days, COALESCE(SUM(a.is_late),0) late, COALESCE(SUM(a.worked_seconds),0) secs "
        f"FROM attendance a JOIN employees e ON e.id=a.employee_id LEFT JOIN departments d ON d.id=e.department_id WHERE {where} GROUP BY e.id ORDER BY e.full_name", p)]
    rows = [dict(r) for r in c.execute(
        f"SELECT a.work_date, e.full_name, a.check_in, a.check_out, a.worked_seconds, a.is_late, a.work_mode, a.location_name "
        f"FROM attendance a JOIN employees e ON e.id=a.employee_id WHERE {where} ORDER BY a.work_date DESC, e.full_name LIMIT 600", p)]
    return summary, rows


@bp.route("/attendance")
@hr_required
def attendance():
    start, end, emp_id, dept = _attendance_args()
    with db.conn_ctx() as c:
        summary, rows = _attendance_data(c, start, end, emp_id, dept)
        emps, depts = _employees(c), _departments(c)
    return render_template("hr/attendance.html", active="attendance", summary=summary, rows=rows, emps=emps, depts=depts,
                           start=start, end=end, emp_id=emp_id, dept=dept)


@bp.route("/attendance.xlsx")
@hr_required
def attendance_xlsx():
    start, end, emp_id, dept = _attendance_args()
    with db.conn_ctx() as c:
        summary, rows = _attendance_data(c, start, end, emp_id, dept)
    for s in summary:
        s["hours"] = round(s["secs"] / 3600, 1)
    return _xlsx([("Summary", ["full_name", "dept", "days", "late", "hours"], summary),
                  ("Daily", ["work_date", "full_name", "check_in", "check_out", "worked_seconds", "is_late", "work_mode", "location_name"], rows)],
                 f"HR_Attendance_{start}_to_{end}.xlsx")


# ------------------------------------------------------------------ performance reviews
@bp.route("/performance", methods=["GET", "POST"])
@hr_required
def performance():
    if request.method == "POST":
        eid = request.form.get("employee_id", type=int)
        if eid and request.form.get("review_period", "").strip():
            with db.conn_ctx() as c:
                c.execute("INSERT INTO performance_reviews(employee_id,review_period,score,strengths,improvements,reviewer,created_at) VALUES (?,?,?,?,?,?,?)",
                          (eid, request.form.get("review_period", "")[:40], max(1, min(5, request.form.get("score", 3, type=int))),
                           request.form.get("strengths", "")[:1000], request.form.get("improvements", "")[:1000], _label(), db.now().strftime(db.DT_FMT)))
                db.notify(eid, "message", "New performance review", f"Period: {request.form.get('review_period', '')[:40]}", "/my-performance", conn=c)
            flash("Performance review saved and shared with the employee.", "success")
        else:
            flash("Choose an employee and enter the review period.", "error")
        return redirect(url_for("hr.performance"))
    with db.conn_ctx() as c:
        rows = [dict(r) for r in c.execute("SELECT r.*, e.full_name FROM performance_reviews r JOIN employees e ON e.id=r.employee_id ORDER BY r.id DESC LIMIT 200")]
        emps = _employees(c)
    return render_template("hr/performance.html", active="performance", rows=rows, emps=emps)


# ------------------------------------------------------------------ projects
@bp.route("/projects", methods=["GET", "POST"])
@hr_required
def projects():
    if request.method == "POST":
        action = request.form.get("action")
        with db.conn_ctx() as c:
            if action == "create":
                eid, title = request.form.get("employee_id", type=int), request.form.get("title", "").strip()
                if eid and title:
                    pr = request.form.get("priority", "Medium")
                    pid = c.execute("INSERT INTO projects(title,description,employee_id,priority,status,start_date,due_date,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                              (title[:120], request.form.get("description", "")[:1500], eid, pr if pr in PRIORITIES else "Medium", "Planned",
                               _date(request.form.get("start_date")), _date(request.form.get("due_date")), _label(), db.now().strftime(db.DT_FMT))).lastrowid
                    db.notify(eid, "task", f"New work from HR: {title[:60]}", request.form.get("description", "")[:140], "/tasks", -pid, conn=c)
                    flash("Project assigned.", "success")
                else:
                    flash("Choose an employee and enter a title.", "error")
            elif action == "update":
                st = request.form.get("status", "In Progress")
                c.execute("UPDATE projects SET status=?, progress=? WHERE id=?",
                          (st if st in PROJECT_STATUSES else "In Progress", max(0, min(100, request.form.get("progress", 0, type=int))), request.form.get("project_id", type=int)))
                flash("Project updated.", "success")
        return redirect(url_for("hr.projects"))
    with db.conn_ctx() as c:
        rows = [dict(r) for r in c.execute("SELECT p.*, e.full_name FROM projects p JOIN employees e ON e.id=p.employee_id ORDER BY p.id DESC LIMIT 200")]
        emps = _employees(c)
    return render_template("hr/projects.html", active="projects", rows=rows, emps=emps, priorities=PRIORITIES, statuses=PROJECT_STATUSES)


# ------------------------------------------------------------------ shifts
@bp.route("/shifts", methods=["GET", "POST"])
@hr_required
def shifts():
    if request.method == "POST":
        with db.conn_ctx() as c:
            if request.form.get("action") == "delete":
                c.execute("DELETE FROM employee_shifts WHERE id=?", (request.form.get("shift_id", type=int),))
                flash("Shift removed.", "success")
            else:
                eid = request.form.get("employee_id", type=int)
                if eid:
                    c.execute("INSERT INTO employee_shifts(employee_id,shift_name,start_time,end_time,effective_from,effective_to,break_minutes,created_at) VALUES (?,?,?,?,?,?,?,?)",
                              (eid, request.form.get("shift_name", "General")[:80], request.form.get("start_time", "09:00")[:5], request.form.get("end_time", "17:00")[:5],
                               _date(request.form.get("effective_from")) or db.today_str(), _date(request.form.get("effective_to")),
                               max(0, min(240, request.form.get("break_minutes", 60, type=int))), db.now().strftime(db.DT_FMT)))
                    db.notify(eid, "task", "Your work shift was updated", request.form.get("shift_name", "")[:60], "/dashboard", conn=c)
                    flash("Shift assigned.", "success")
        return redirect(url_for("hr.shifts"))
    with db.conn_ctx() as c:
        rows = [dict(r) for r in c.execute("SELECT s.*, e.full_name FROM employee_shifts s JOIN employees e ON e.id=s.employee_id ORDER BY s.id DESC LIMIT 200")]
        emps = _employees(c)
    return render_template("hr/shifts.html", active="shifts", rows=rows, emps=emps, today=db.today_str())


# ------------------------------------------------------------------ documents
@bp.route("/documents", methods=["GET", "POST"])
@hr_required
def documents():
    if request.method == "POST":
        with db.conn_ctx() as c:
            if request.form.get("action") == "delete":
                c.execute("DELETE FROM employee_documents WHERE id=?", (request.form.get("doc_id", type=int),))
                flash("Document record removed.", "success")
            else:
                eid, title = request.form.get("employee_id", type=int), request.form.get("title", "").strip()
                if eid and title:
                    dt = request.form.get("document_type", "Other")
                    dt = dt if dt in DOC_TYPES else "Other"
                    id_number, id_warn = _clean_id_number(dt, request.form.get("id_number"))
                    if dt == "CNIC / ID" and (id_warn or not id_number):
                        flash(id_warn or "Enter the CNIC number (13 digits).", "error")
                        return redirect(url_for("hr.documents"))
                    c.execute("INSERT INTO employee_documents(employee_id,title,document_type,id_number,reference_url,expiry_date,note,created_at) VALUES (?,?,?,?,?,?,?,?)",
                              (eid, title[:120], dt, id_number, request.form.get("reference_url", "")[:500],
                               _date(request.form.get("expiry_date")), request.form.get("note", "")[:500], db.now().strftime(db.DT_FMT)))
                    flash("Document record added.", "success")
                else:
                    flash("Choose an employee and enter a title.", "error")
        return redirect(url_for("hr.documents"))
    soon = (db.now() + timedelta(days=30)).strftime(db.D_FMT)
    with db.conn_ctx() as c:
        rows = [dict(r) for r in c.execute("SELECT d.*, e.full_name FROM employee_documents d JOIN employees e ON e.id=d.employee_id ORDER BY (d.expiry_date='' ), d.expiry_date, d.id DESC LIMIT 300")]
        emps = _employees(c)
    return render_template("hr/documents.html", active="documents", rows=rows, emps=emps, types=DOC_TYPES, today=db.today_str(), soon=soon)


# ------------------------------------------------------------------ expense claims
@bp.route("/expenses", methods=["GET", "POST"])
@hr_required
def expenses():
    if request.method == "POST":
        xid, status = request.form.get("expense_id", type=int), request.form.get("status")
        if xid and status in EXPENSE_STATUSES:
            with db.conn_ctx() as c:
                x = c.execute("SELECT x.*, e.full_name FROM expenses x JOIN employees e ON e.id=x.employee_id WHERE x.id=?", (xid,)).fetchone()
                if x:
                    c.execute("UPDATE expenses SET status=?, admin_note=? WHERE id=?", (status, f"{_label()}: " + request.form.get("note", "")[:400], xid))
                    db.notify(x["employee_id"], "message", f"Expense claim {status.lower()}", f"{x['category']} - Rs {x['amount']:,.0f}", "/my-hr", xid, conn=c)
                    if status == "Approved":
                        db.admin_notify("hr", f"HR approved an expense: {x['full_name']}", f"{x['category']} - Rs {x['amount']:,.0f}. Reimburse if applicable.", "/admin/advanced-hr", xid, conn=c)
            flash(f"Expense marked {status}.", "success")
        return redirect(url_for("hr.expenses"))
    with db.conn_ctx() as c:
        rows = [dict(r) for r in c.execute("SELECT x.*, e.full_name FROM expenses x JOIN employees e ON e.id=x.employee_id ORDER BY CASE x.status WHEN 'Pending' THEN 0 ELSE 1 END, x.id DESC LIMIT 300")]
    return render_template("hr/expenses.html", active="expenses", rows=rows)


# ------------------------------------------------------------------ payroll (READ ONLY - the admin pays salaries)
@bp.route("/payroll")
@hr_required
def payroll():
    month = request.args.get("month") or hrcore.current_month()
    if not hrcore.valid_month(month):
        month = hrcore.current_month()
    with db.conn_ctx() as c:
        rows = hrcore.payroll_overview(c, month)
    return render_template("hr/payroll.html", active="payroll", month=month, rows=rows, totals=hrcore.payroll_totals(rows))


# ------------------------------------------------------------------ recruitment
@bp.route("/recruitment", methods=["GET", "POST"])
@hr_required
def recruitment():
    if request.method == "POST":
        action = request.form.get("action")
        with db.conn_ctx() as c:
            if action == "job_add":
                title = request.form.get("title", "").strip()
                if title:
                    c.execute("INSERT INTO job_openings(title,department_id,description,vacancies,status,created_by,created_at) VALUES (?,?,?,?,'Open',?,?)",
                              (title[:120], request.form.get("department_id", type=int), request.form.get("description", "")[:1500],
                               max(1, min(100, request.form.get("vacancies", 1, type=int))), _label(), db.now().strftime(db.DT_FMT)))
                    flash("Job opening created.", "success")
            elif action == "job_status":
                st = request.form.get("status")
                if st in ("Open", "Closed"):
                    c.execute("UPDATE job_openings SET status=? WHERE id=?", (st, request.form.get("job_id", type=int)))
                    flash(f"Job marked {st}.", "success")
            elif action == "cand_add":
                jid, name = request.form.get("job_id", type=int), request.form.get("full_name", "").strip()
                if jid and name:
                    c.execute("INSERT INTO candidates(job_id,full_name,email,phone,stage,notes,created_by,created_at) VALUES (?,?,?,?,'Applied',?,?,?)",
                              (jid, name[:100], request.form.get("email", "")[:120], request.form.get("phone", "")[:40], request.form.get("notes", "")[:500], _label(), db.now().strftime(db.DT_FMT)))
                    flash("Candidate added.", "success")
                else:
                    flash("Choose a job and enter the candidate's name.", "error")
            elif action == "cand_stage":
                stage, cid = request.form.get("stage"), request.form.get("candidate_id", type=int)
                if stage in STAGES and cid:
                    cand = c.execute("SELECT c.full_name, j.title FROM candidates c JOIN job_openings j ON j.id=c.job_id WHERE c.id=?", (cid,)).fetchone()
                    c.execute("UPDATE candidates SET stage=? WHERE id=?", (stage, cid))
                    if stage == "Hired" and cand:
                        db.admin_notify("hr", f"Candidate hired: {cand['full_name']}", f"For {cand['title']}. Ask them to register, then approve the account and set their salary.", "/admin/approvals", cid, conn=c)
                    flash(f"Candidate moved to {stage}.", "success")
            elif action == "cand_delete":
                c.execute("DELETE FROM candidates WHERE id=?", (request.form.get("candidate_id", type=int),))
                flash("Candidate removed.", "success")
        return redirect(url_for("hr.recruitment"))
    with db.conn_ctx() as c:
        jobs = [dict(r) for r in c.execute("SELECT j.*, d.name dept, (SELECT COUNT(*) FROM candidates WHERE job_id=j.id) total, (SELECT COUNT(*) FROM candidates WHERE job_id=j.id AND stage='Hired') hired FROM job_openings j LEFT JOIN departments d ON d.id=j.department_id ORDER BY (j.status='Open') DESC, j.id DESC")]
        cands = [dict(r) for r in c.execute("SELECT c.*, j.title job_title FROM candidates c JOIN job_openings j ON j.id=c.job_id ORDER BY c.id DESC LIMIT 300")]
        depts = _departments(c)
    return render_template("hr/recruitment.html", active="recruitment", jobs=jobs, cands=cands, depts=depts, stages=STAGES)


# ------------------------------------------------------------------ messages from employees to HR
@bp.route("/messages", methods=["GET", "POST"])
@hr_required
def messages():
    if request.method == "POST" and request.form.get("action") == "compose":
        subject = request.form.get("subject", "").strip()[:150]
        body = request.form.get("body", "").strip()[:4000]
        target = request.form.get("target", "")
        if not subject or not body:
            flash("Subject and message are required.", "error")
            return redirect(url_for("hr.messages"))
        with db.conn_ctx() as c:
            if target.startswith("employee:") and target.split(":", 1)[1].isdigit():
                ids = [int(target.split(":", 1)[1])]
            elif target.startswith("department:") and target.split(":", 1)[1].isdigit():
                ids = [r[0] for r in c.execute("SELECT id FROM employees WHERE department_id=? AND status='approved'", (int(target.split(":", 1)[1]),))]
            elif target == "all":
                ids = [r[0] for r in c.execute("SELECT id FROM employees WHERE status='approved'")]
            else:
                ids = []
            stamp = db.now().strftime(db.DT_FMT)
            for eid in ids:
                mid = c.execute("INSERT INTO messages(employee_id,recipient_role,subject,body,status,created_at,sender_role,sender_admin,department_id) VALUES(?,?,?,?,?,?,?,?,(SELECT department_id FROM employees WHERE id=?))",
                                (eid, "Employee", subject, body, "sent", stamp, "hr", _label(), eid)).lastrowid
                db.notify(eid, "message", "Message from HR: " + subject, body[:160], "/messages", mid, conn=c)
        flash(f"Message delivered to {len(ids)} employee(s)." if ids else "Choose who should receive the message.", "success" if ids else "error")
        return redirect(url_for("hr.messages"))
    if request.method == "POST":
        mid, reply = request.form.get("message_id", type=int), request.form.get("reply", "").strip()
        if mid and reply:
            with db.conn_ctx() as c:
                m = c.execute("SELECT employee_id, subject FROM messages WHERE id=? AND recipient_role IN ('HR','All (CEO, Admin & HR)')", (mid,)).fetchone()
                if m:
                    c.execute("UPDATE messages SET reply=?, status='replied', replied_at=? WHERE id=?", (f"[HR] {reply}"[:2000], db.now().strftime(db.DT_FMT), mid))
                    db.notify(m["employee_id"], "message", "HR replied: " + m["subject"], reply[:160], "/messages", mid, conn=c)
                    flash("Reply sent to the employee.", "success")
        return redirect(url_for("hr.messages"))
    with db.conn_ctx() as c:
        rows = [dict(r) for r in c.execute("SELECT m.*, e.full_name, e.username FROM messages m JOIN employees e ON e.id=m.employee_id WHERE m.recipient_role IN ('HR','All (CEO, Admin & HR)') ORDER BY (m.status='open') DESC, m.created_at DESC LIMIT 200")]
        sent = [dict(r) for r in c.execute("SELECT m.*, e.full_name FROM messages m JOIN employees e ON e.id=m.employee_id WHERE m.sender_role='hr' ORDER BY m.id DESC LIMIT 50")]
        emps = _employees(c)
        depts = [dict(r) for r in c.execute("SELECT id, name FROM departments ORDER BY name")]
    return render_template("hr/messages.html", active="messages", rows=rows, sent=sent, emps=emps, depts=depts)


# ------------------------------------------------------------------ Main HR: temporary HR team assignments
@bp.route("/team", methods=["GET", "POST"])
@hr_required
def team():
    if not g.hr.get("is_main"):
        abort(403, "Only Main HR can assign temporary HR access.")
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()[:100]
        username = request.form.get("username", "").strip().lower()[:40]
        password = request.form.get("password", "")
        days = request.form.get("days", "7")
        if not full_name or not re.fullmatch(r"[a-z0-9_.]{3,40}", username):
            flash("Enter a name and a username of 3–40 letters, numbers, dots or underscores.", "error")
        elif len(password) < 8:
            flash("Temporary HR password must be at least 8 characters.", "error")
        elif days not in ("1", "3", "7", "30"):
            flash("Choose a valid temporary assignment duration.", "error")
        else:
            expires = (db.now() + timedelta(days=int(days))).strftime(db.DT_FMT)
            try:
                with db.conn_ctx() as c:
                    c.execute("INSERT INTO hr_users(username,password_hash,full_name,active,created_at,created_by,is_main,expires_at) VALUES(?,?,?,?,?,?,0,?)",
                              (username, generate_password_hash(password), full_name, 1,
                               db.now().strftime(db.DT_FMT), g.hr["username"], expires))
                flash(f"Temporary HR access created for {full_name}; expires {expires}.", "success")
            except sqlite3.IntegrityError:
                flash("That HR username already exists.", "error")
        return redirect(url_for("hr.team"))
    with db.conn_ctx() as c:
        users = [dict(r) for r in c.execute("SELECT id,username,full_name,active,created_at,expires_at,is_main FROM hr_users ORDER BY is_main DESC,id DESC")]
    return render_template("hr/team.html", active="team", users=users)


@bp.route("/team/<int:user_id>/toggle", methods=["POST"])
@hr_required
def team_toggle(user_id):
    if not g.hr.get("is_main"):
        abort(403)
    with db.conn_ctx() as c:
        c.execute("UPDATE hr_users SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=? AND is_main=0", (user_id,))
    flash("Temporary HR access updated.", "success")
    return redirect(url_for("hr.team"))


# ------------------------------------------------------------------ one-click HR report
def _hr_workbook_sheets(c, emp_id=None):
    """Everything HR needs about employees, one sheet per topic. emp_id=None -> all employees.
    Never contains passwords or face data."""
    def rows(sql, params=()):
        try:
            return [dict(r) for r in c.execute(sql, params)]
        except sqlite3.OperationalError:
            return []

    ef = " AND e.id=%d" % int(emp_id) if emp_id else ""          # e = employees alias in every query below
    hhmm = "printf('%02d:%02d', COALESCE(a.worked_seconds,0)/3600, (COALESCE(a.worked_seconds,0)%3600)/60)"
    cnic = ("(SELECT d.id_number FROM employee_documents d WHERE d.employee_id=e.id AND d.document_type='CNIC / ID' "
            "AND d.id_number<>'' ORDER BY d.id DESC LIMIT 1)")
    return [
        ("Employees", ["id", "employee_code", "full_name", "username", "cnic", "email", "phone", "designation", "department", "employment_type", "work_location", "assigned_location", "manager_name", "joining_date", "skills", "education", "status"],
         rows(f"SELECT e.id,e.employee_code,e.full_name,e.username,{cnic} cnic,e.email,e.phone,e.designation,d.name department,e.employment_type,e.work_location,e.assigned_location,e.manager_name,e.joining_date,e.skills,e.education,e.status FROM employees e LEFT JOIN departments d ON d.id=e.department_id WHERE e.status='approved'{ef} ORDER BY e.full_name")),
        ("Attendance Summary", ["employee_code", "full_name", "department", "days_present", "late_days", "absent_days", "total_hours", "first_day", "last_day"],
         rows(f"""SELECT e.employee_code,e.full_name,d.name department,
                  COUNT(a.id) - COALESCE(SUM(a.marked_absent),0) days_present,
                  COALESCE(SUM(a.is_late),0) late_days, COALESCE(SUM(a.marked_absent),0) absent_days,
                  ROUND(COALESCE(SUM(a.worked_seconds),0)/3600.0,1) total_hours,
                  MIN(a.work_date) first_day, MAX(a.work_date) last_day
                  FROM employees e LEFT JOIN departments d ON d.id=e.department_id LEFT JOIN attendance a ON a.employee_id=e.id
                  WHERE e.status='approved'{ef} GROUP BY e.id ORDER BY e.full_name""")),
        ("Attendance (daily)", ["work_date", "employee_code", "full_name", "department", "check_in", "check_out", "hours_worked", "late", "absent", "work_mode", "location_name"],
         rows(f"""SELECT a.work_date,e.employee_code,e.full_name,d.name department,a.check_in,a.check_out,{hhmm} hours_worked,
                  CASE WHEN a.is_late=1 THEN 'Late' ELSE 'On time' END late,
                  CASE WHEN COALESCE(a.marked_absent,0)=1 THEN 'Absent' ELSE '' END absent,
                  a.work_mode,a.location_name
                  FROM attendance a JOIN employees e ON e.id=a.employee_id LEFT JOIN departments d ON d.id=e.department_id
                  WHERE 1=1{ef} ORDER BY a.work_date DESC,e.full_name""")),
        ("Warnings", ["full_name", "work_date", "number", "reason", "cleared", "created_at"],
         rows(f"SELECT e.full_name,w.work_date,w.number,w.reason,CASE WHEN w.cleared=1 THEN 'Cleared' ELSE 'Active' END cleared,w.created_at FROM warnings w JOIN employees e ON e.id=w.employee_id WHERE 1=1{ef} ORDER BY w.id DESC")),
        ("Leave Requests", ["employee", "leave_date", "leave_type", "units", "status", "reason", "salary_deduction"],
         rows(f"SELECT e.full_name employee,l.leave_date,l.leave_type,l.units,l.status,l.reason,l.salary_deduction FROM leave_requests l JOIN employees e ON e.id=l.employee_id WHERE 1=1{ef} ORDER BY l.leave_date DESC")),
        ("WFH Requests", ["employee", "work_date", "reason", "status", "created_at", "reviewed_by"],
         rows(f"SELECT e.full_name employee,w.work_date,w.reason,w.status,w.created_at,w.reviewed_by FROM wfh_requests w JOIN employees e ON e.id=w.employee_id WHERE 1=1{ef} ORDER BY w.work_date DESC")),
        ("Work & Tasks", ["employee", "title", "details", "assigned_by", "due_date", "status", "employee_update", "created_at"],
         rows(f"SELECT e.full_name employee,t.title,t.description details,t.assigned_by,t.due_date,t.status,t.employee_note employee_update,t.created_at FROM tasks t JOIN employees e ON e.id=t.employee_id WHERE 1=1{ef} ORDER BY t.id DESC")),
        ("Projects", ["title", "employee", "priority", "status", "progress", "start_date", "due_date", "description", "employee_note", "created_by"],
         rows(f"SELECT p.title,e.full_name employee,p.priority,p.status,p.progress,p.start_date,p.due_date,p.description,p.employee_note,p.created_by FROM projects p JOIN employees e ON e.id=p.employee_id WHERE 1=1{ef} ORDER BY p.id DESC")),
        ("Messages", ["employee", "direction", "to_or_from", "subject", "message", "reply", "created_at"],
         rows(f"""SELECT e.full_name employee,
                  CASE WHEN m.sender_role IN ('admin','hr') THEN 'Received from management' ELSE 'Sent by employee' END direction,
                  CASE WHEN m.sender_role IN ('admin','hr') THEN m.sender_admin ELSE m.recipient_role END to_or_from,
                  m.subject,m.body message,m.reply,m.created_at
                  FROM messages m JOIN employees e ON e.id=m.employee_id WHERE 1=1{ef} ORDER BY m.id DESC""")),
        ("Payroll", ["employee", "pay_month", "basic", "allowance", "bonus", "deduction", "status", "paid_at", "note"],
         rows(f"SELECT e.full_name employee,p.pay_month,p.basic,p.allowance,p.bonus,p.deduction,p.status,p.paid_at,p.note FROM payroll p JOIN employees e ON e.id=p.employee_id WHERE 1=1{ef} ORDER BY p.pay_month DESC,e.full_name")),
        ("Performance", ["employee", "review_period", "score", "strengths", "improvements", "reviewer", "created_at"],
         rows(f"SELECT e.full_name employee,r.review_period,r.score,r.strengths,r.improvements,r.reviewer,r.created_at FROM performance_reviews r JOIN employees e ON e.id=r.employee_id WHERE 1=1{ef} ORDER BY r.id DESC")),
        ("Shifts", ["employee", "shift_name", "start_time", "end_time", "effective_from", "effective_to", "break_minutes"],
         rows(f"SELECT e.full_name employee,s.shift_name,s.start_time,s.end_time,s.effective_from,s.effective_to,s.break_minutes FROM employee_shifts s JOIN employees e ON e.id=s.employee_id WHERE 1=1{ef} ORDER BY s.id DESC")),
        ("Documents", ["employee", "title", "document_type", "id_number", "reference_url", "expiry_date", "note"],
         rows(f"SELECT e.full_name employee,d.title,d.document_type,d.id_number,d.reference_url,d.expiry_date,d.note FROM employee_documents d JOIN employees e ON e.id=d.employee_id WHERE 1=1{ef} ORDER BY d.id DESC")),
        ("Expenses", ["employee", "category", "amount", "expense_date", "description", "status", "admin_note"],
         rows(f"SELECT e.full_name employee,x.category,x.amount,x.expense_date,x.description,x.status,x.admin_note FROM expenses x JOIN employees e ON e.id=x.employee_id WHERE 1=1{ef} ORDER BY x.id DESC")),
    ] + ([] if emp_id else [
        ("Job Openings", ["title", "department", "description", "vacancies", "status", "created_by", "created_at"],
         rows("SELECT j.title,d.name department,j.description,j.vacancies,j.status,j.created_by,j.created_at FROM job_openings j LEFT JOIN departments d ON d.id=j.department_id ORDER BY j.id DESC")),
        ("Candidates", ["job_title", "full_name", "email", "phone", "stage", "notes", "created_at"],
         rows("SELECT j.title job_title,c.full_name,c.email,c.phone,c.stage,c.notes,c.created_at FROM candidates c JOIN job_openings j ON j.id=c.job_id ORDER BY c.id DESC")),
    ])


@bp.route("/export-all.xlsx")
@hr_required
def export_all_xlsx():
    """All employees: profile (with CNIC), attendance, warnings, leave, work, projects, messages ... one workbook."""
    with db.conn_ctx() as c:
        sheets = _hr_workbook_sheets(c)
    return _xlsx(sheets, "CyberAttend_HR_Complete_Export.xlsx")


@bp.route("/employees/<int:emp_id>/export.xlsx")
@hr_required
def employee_export_xlsx(emp_id):
    """One employee: the same sheets filtered to that person."""
    with db.conn_ctx() as c:
        who = c.execute("SELECT username FROM employees WHERE id=?", (emp_id,)).fetchone()
        if not who:
            abort(404)
        sheets = _hr_workbook_sheets(c, emp_id)
    return _xlsx(sheets, f"Employee_{who['username']}_full_record.xlsx")


@bp.route("/report.xlsx")
@hr_required
def report_xlsx():
    month_start = db.now().strftime("%Y-%m-01")
    with db.conn_ctx() as c:
        def rows(sql, p=()):
            try:
                return [dict(r) for r in c.execute(sql, p)]
            except sqlite3.OperationalError:
                return []
        sheets = [
            ("Employees", ["employee_code", "full_name", "username", "designation", "employment_type", "work_location", "manager_name", "joining_date", "email", "phone"],
             rows("SELECT employee_code,full_name,username,designation,employment_type,work_location,manager_name,joining_date,email,phone FROM employees WHERE status='approved' ORDER BY full_name")),
            ("Leaves", ["full_name", "leave_date", "leave_type", "units", "status", "reason", "salary_deduction"],
             rows("SELECT e.full_name,l.leave_date,l.leave_type,l.units,l.status,l.reason,l.salary_deduction FROM leave_requests l JOIN employees e ON e.id=l.employee_id ORDER BY l.leave_date DESC")),
            ("Attendance (this month)", ["full_name", "days", "late", "hours"],
             rows("SELECT e.full_name, COUNT(*) days, COALESCE(SUM(a.is_late),0) late, ROUND(COALESCE(SUM(a.worked_seconds),0)/3600.0,1) hours FROM attendance a JOIN employees e ON e.id=a.employee_id WHERE a.work_date>=? GROUP BY e.id ORDER BY e.full_name", (month_start,))),
            ("Performance", ["full_name", "review_period", "score", "strengths", "improvements", "reviewer"],
             rows("SELECT e.full_name,r.review_period,r.score,r.strengths,r.improvements,r.reviewer FROM performance_reviews r JOIN employees e ON e.id=r.employee_id ORDER BY r.id DESC")),
            ("Recruitment", ["job_title", "full_name", "email", "phone", "stage"],
             rows("SELECT j.title job_title,c.full_name,c.email,c.phone,c.stage FROM candidates c JOIN job_openings j ON j.id=c.job_id ORDER BY c.id DESC")),
        ]
    return _xlsx(sheets, "HR_Report.xlsx")


def register(app):
    # Keep the HR workspace independent from the Admin and Employee portals.
    # Migrate older databases safely before creating the built-in Main HR account.
    with db.conn_ctx() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(hr_users)")}
        new_main_flag = "is_main" not in cols
        if new_main_flag:
            c.execute("ALTER TABLE hr_users ADD COLUMN is_main INTEGER NOT NULL DEFAULT 0")
        if "expires_at" not in cols:
            c.execute("ALTER TABLE hr_users ADD COLUMN expires_at TEXT DEFAULT ''")
        main = c.execute("SELECT id FROM hr_users WHERE username=?", ("muzi",)).fetchone()
        if not main:
            c.execute("INSERT INTO hr_users(username,password_hash,full_name,active,created_at,created_by,is_main,expires_at) VALUES(?,?,?,?,?,?,1,'')",
                      ("muzi", generate_password_hash("muzammil182921"), "Main HR", 1,
                       db.now().strftime(db.DT_FMT), "system"))
        else:
            if new_main_flag:
                c.execute("UPDATE hr_users SET is_main=1, full_name='Main HR', password_hash=? WHERE username='muzi'",
                          (generate_password_hash("muzammil182921"),))
            else:
                c.execute("UPDATE hr_users SET is_main=1, full_name='Main HR' WHERE username='muzi'")
    app.register_blueprint(bp)
    app.add_url_rule("/hrlogin", endpoint="hr_login_alias", view_func=login, methods=["GET", "POST"])
