"""
Imports the "deleted employees" face dataset into the 10-YEAR ARCHIVE.

    python import_deleted_dataset.py <path-to-dataset-folder-or-zip>

* Face images  -> archived_faces/<employee_id>/<n>.jpg   (kept separate from the live dataset/,
                  so old faces are never trained into the live recogniser and ids never clash)
* Records      -> employee_archive.db  (archived_employees, retention 3652 days = 10 years)
Safe to run again: employees already archived with the same id + username are skipped.
"""
import os, sys, io, csv, json, shutil, zipfile, sqlite3
from datetime import timedelta
import database as db
import enterprise_upgrade as eu

FACES_DIR = os.path.join(db.DATA_DIR, "archived_faces")


def _load_source(src):
    if os.path.isfile(src) and src.lower().endswith(".zip"):
        import tempfile
        tmp = tempfile.mkdtemp()
        with zipfile.ZipFile(src) as z:
            z.extractall(tmp)
        for root, _d, files in os.walk(tmp):
            if "manifest.csv" in files:
                return root
        raise SystemExit("manifest.csv not found inside the zip")
    return src


def main(src):
    src = _load_source(src)
    with open(os.path.join(src, "manifest.csv"), newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    # face folders that have images but no manifest row are kept too (nothing is dropped)
    known = {int(r["employee_id"]) for r in rows}
    for name in sorted(os.listdir(src), key=lambda x: (len(x), x)):
        if name.isdigit() and os.path.isdir(os.path.join(src, name)) and int(name) not in known:
            rows.append({"employee_id": name, "full_name": f"Unnamed employee (ID {name})", "username": f"employee_{name}",
                         "department": "", "status": "no manifest record"})
    os.makedirs(FACES_DIR, exist_ok=True)
    dept_by_name = {d["name"]: d for d in db.list_departments()}
    now = db.now()
    until = now + timedelta(days=3652)
    c = eu._archive_conn()
    added = 0
    try:
        have = {(r["employee_id"], r["username"]): r["archive_id"] for r in c.execute("SELECT archive_id, employee_id, username FROM archived_employees")}
        for r in rows:
            emp_id = int(r["employee_id"]); uname = r["username"]
            dst = os.path.join(FACES_DIR, str(emp_id))
            os.makedirs(dst, exist_ok=True)
            n = 0
            sdir = os.path.join(src, str(emp_id))
            if os.path.isdir(sdir):
                for fn in sorted(os.listdir(sdir), key=lambda x: (len(x), x)):
                    if fn.lower().endswith((".jpg", ".jpeg", ".png")):
                        shutil.copy2(os.path.join(sdir, fn), os.path.join(dst, fn)); n += 1
            if (emp_id, uname) in have:      # already archived (e.g. from the live DB): just attach the faces
                aid = have[(emp_id, uname)]
                rec = json.loads(c.execute("SELECT record_json FROM archived_employees WHERE archive_id=?", (aid,)).fetchone()[0])
                rec["face_dataset"] = {"folder": f"archived_faces/{emp_id}", "samples": n, "manifest_status": r["status"]}
                rec.setdefault("employee", {})["samples_captured"] = n
                c.execute("UPDATE archived_employees SET record_json=? WHERE archive_id=?", (json.dumps(rec, default=str), aid))
                continue
            dept = dept_by_name.get(r["department"], {})
            record = {
                "employee": {"id": emp_id, "full_name": r["full_name"], "username": uname,
                             "status": r["status"], "samples_captured": n,
                             "department_id": dept.get("id"), "email": "", "designation": "", "phone": ""},
                "department": {"id": dept.get("id"), "name": r["department"], "description": dept.get("description", "")},
                "face_dataset": {"folder": f"archived_faces/{emp_id}", "samples": n, "manifest_status": r["status"]},
                "source": "Deleted employees dataset (imported)",
            }
            for t in ["attendance", "leave_balances", "leave_requests", "messages", "tasks", "warnings", "certificates",
                      "notifications", "payroll", "performance_reviews", "projects", "expenses",
                      "employee_documents", "employee_shifts", "salary_profiles", "wfh_requests"]:
                record[t] = []
            c.execute("INSERT INTO archived_employees(employee_id,username,full_name,archived_at,retention_until,record_json) VALUES(?,?,?,?,?,?)",
                      (emp_id, uname, r["full_name"], now.strftime(db.DT_FMT), until.strftime(db.DT_FMT), json.dumps(record, default=str)))
            added += 1
        c.commit()
    finally:
        c.close()
    print(f"Archived {added} employees (retention until {until:%Y-%m-%d}). Faces in {FACES_DIR}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "dataset")
