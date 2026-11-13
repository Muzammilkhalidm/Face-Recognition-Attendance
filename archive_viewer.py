"""Admin pages for the 10-YEAR ARCHIVE of deleted employees (records + every face sample).

/admin/archive                       list, search, filters
/admin/archive/<archive_id>          full profile: all 50 face samples + every stored table
/admin/archive/face/<emp>/<n>.jpg    one archived face image
/admin/archive/<archive_id>/faces.zip   all faces of one archived employee
/admin/archive/dataset.zip           the whole deleted dataset (+ manifest.csv)
/admin/archive.csv                   archive register as CSV
Main admin only (these are biometric + personal records; only the main admin can delete employees).
"""
import os, io, csv, json, zipfile
from datetime import datetime
from flask import render_template, request, abort, send_file, Response, g, session, redirect, url_for
import database as db
import enterprise_upgrade as eu

FACES_DIR = os.path.join(db.DATA_DIR, "archived_faces")
MODULES = [("attendance", "Attendance"), ("leave_requests", "Leave requests"), ("leave_balances", "Leave balances"),
           ("tasks", "Work & tasks"), ("warnings", "Warnings"), ("messages", "Messages"), ("certificates", "Certificates"),
           ("payroll", "Payroll"), ("salary_profiles", "Salary profile"), ("performance_reviews", "Performance reviews"),
           ("projects", "Projects"), ("expenses", "Expenses"), ("employee_documents", "Documents"),
           ("employee_shifts", "Shifts"), ("wfh_requests", "WFH requests"), ("notifications", "Notifications")]


def _guard():
    eu._admin_guard()
    if not g.admin.get("is_main"):
        abort(403)


def _faces(emp_id):
    d = os.path.join(FACES_DIR, str(emp_id))
    if not os.path.isdir(d):
        return []
    names = [f for f in os.listdir(d) if f.lower().endswith((".jpg", ".jpeg", ".png"))]
    return sorted(names, key=lambda x: (len(x), x))


def _parse(s):
    try:
        return datetime.strptime(s, db.DT_FMT)
    except Exception:
        return None


def _rows():
    c = eu._archive_conn()
    try:
        raw = c.execute("SELECT * FROM archived_employees ORDER BY employee_id, archive_id").fetchall()
    finally:
        c.close()
    out, now = [], db.now()
    for r in raw:
        rec = json.loads(r["record_json"] or "{}")
        until = _parse(r["retention_until"])
        emp = rec.get("employee", {}) or {}
        out.append({
            "archive_id": r["archive_id"], "employee_id": r["employee_id"], "username": r["username"],
            "full_name": r["full_name"], "archived_at": r["archived_at"], "retention_until": r["retention_until"],
            "days_left": (until - now).days if until else None,
            "department": (rec.get("department") or {}).get("name") or "—",
            "status": emp.get("status") or "—", "designation": emp.get("designation") or "",
            "email": emp.get("email") or "", "faces": len(_faces(r["employee_id"])),
            "rec_counts": {k: len(rec.get(k) or []) for k, _ in MODULES}, "record": rec,
        })
    return out


def register(app):
    @app.context_processor
    def _archive_ctx():
        if g.get("admin") and g.admin.get("is_main"):
            try:
                c = eu._archive_conn()
                n = c.execute("SELECT COUNT(*) FROM archived_employees").fetchone()[0]
                c.close()
                # Badge shows only what is NEW since the main admin last opened the archive page.
                import database as _db
                seen = _db.get_int_setting("archive_seen", 0)
                if seen > n:          # archive was purged -> reset
                    seen = n
                return {"archive_count": max(0, n - seen)}
            except Exception:
                return {"archive_count": 0}
        return {}

    @app.route("/admin/archive")
    def admin_archive():
        _guard()
        rows = _rows()
        q = (request.args.get("q") or "").strip().lower()
        dept = request.args.get("department") or ""
        status = request.args.get("status") or ""
        depts = sorted({r["department"] for r in rows})
        statuses = sorted({r["status"] for r in rows})
        if q:
            rows = [r for r in rows if q in f'{r["full_name"]} {r["username"]} {r["employee_id"]} {r["department"]} {r["email"]}'.lower()]
        if dept:
            rows = [r for r in rows if r["department"] == dept]
        if status:
            rows = [r for r in rows if r["status"] == status]
        allrows = _rows()
        try:
            import database as _db
            _db.set_int_setting("archive_seen", len(allrows))      # opened -> badge disappears
        except Exception:
            pass
        stats = {"people": len(allrows), "faces": sum(r["faces"] for r in allrows),
                 "full": sum(1 for r in allrows if r["faces"] >= 50),
                 "partial": sum(1 for r in allrows if 0 < r["faces"] < 50),
                 "none": sum(1 for r in allrows if r["faces"] == 0)}
        return render_template("admin/archive.html", active="archive", rows=rows, stats=stats, depts=depts,
                               statuses=statuses, f_q=q, f_dept=dept, f_status=status)

    @app.route("/admin/archive/<int:archive_id>")
    def admin_archive_detail(archive_id):
        _guard()
        row = next((r for r in _rows() if r["archive_id"] == archive_id), None)
        if not row:
            abort(404)
        mods = [(label, row["record"].get(key) or []) for key, label in MODULES]
        return render_template("admin/archive_detail.html", active="archive", r=row,
                               faces=_faces(row["employee_id"]), modules=mods,
                               emp=row["record"].get("employee", {}) or {})

    @app.route("/admin/archive/face/<int:emp_id>/<path:name>")
    def admin_archive_face(emp_id, name):
        _guard()
        if "/" in name or "\\" in name or ".." in name:
            abort(404)
        p = os.path.join(FACES_DIR, str(emp_id), name)
        if not os.path.isfile(p):
            abort(404)
        return send_file(p, max_age=3600)

    @app.route("/admin/archive/<int:archive_id>/faces.zip")
    def admin_archive_faces_zip(archive_id):
        _guard()
        row = next((r for r in _rows() if r["archive_id"] == archive_id), None)
        if not row:
            abort(404)
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
            for f in _faces(row["employee_id"]):
                z.write(os.path.join(FACES_DIR, str(row["employee_id"]), f), f"{row['employee_id']}/{f}")
            z.writestr("record.json", json.dumps(row["record"], indent=2, default=str))
        mem.seek(0)
        return send_file(mem, mimetype="application/zip", as_attachment=True,
                         download_name=f"archived_{row['employee_id']}_{row['username']}.zip")

    @app.route("/admin/archive/dataset.zip")
    def admin_archive_dataset_zip():
        _guard()
        rows = _rows()
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
            if os.path.isdir(FACES_DIR):
                for emp in sorted(os.listdir(FACES_DIR)):
                    d = os.path.join(FACES_DIR, emp)
                    if os.path.isdir(d):
                        for f in os.listdir(d):
                            z.write(os.path.join(d, f), f"{emp}/{f}")
            buf = io.StringIO(); w = csv.writer(buf)
            w.writerow(["employee_id", "full_name", "username", "department", "status", "samples", "archived_at", "retention_until"])
            for r in rows:
                w.writerow([r["employee_id"], r["full_name"], r["username"], r["department"], r["status"], r["faces"], r["archived_at"], r["retention_until"]])
            z.writestr("manifest.csv", buf.getvalue())
        mem.seek(0)
        return send_file(mem, mimetype="application/zip", as_attachment=True, download_name="deleted_employees_dataset_10_years.zip")

    @app.route("/admin/archive.csv")
    def admin_archive_csv():
        _guard()
        buf = io.StringIO(); w = csv.writer(buf)
        w.writerow(["Archive ID", "Employee ID", "Name", "Username", "Department", "Status", "Face samples", "Archived at", "Keep until", "Days left"])
        for r in _rows():
            w.writerow([r["archive_id"], r["employee_id"], r["full_name"], r["username"], r["department"], r["status"],
                        r["faces"], r["archived_at"], r["retention_until"], r["days_left"]])
        return Response("\ufeff" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=archived_employees.csv"})
