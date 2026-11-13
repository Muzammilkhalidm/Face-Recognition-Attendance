"""
database.py
SQLite layer for the Face Recognition Attendance System.

Tables
  admins          - admin accounts (password hashed). One MAIN admin (muzammil) + extra admins,
                    which can be permanent or temporary (expires_at)
  departments     - departments
  employees       - employee accounts + face-enrolment / approval status
  attendance      - one row per employee per day (check_in / check_out / worked_seconds)
  announcements   - notices (for one department, or for everyone)
  settings        - required hours, shift start, late grace
"""
import os
import sqlite3
import smsclient
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)          # Railway volume support
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "attendance.db")

DEFAULT_ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "muzammil")
DEFAULT_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "muzammil182921")

DT_FMT = "%Y-%m-%d %H:%M:%S"
D_FMT = "%Y-%m-%d"
MIN_SESSION_SECONDS = 60          # cannot check out within 1 minute of checking in

try:
    TZ = ZoneInfo(os.environ.get("APP_TIMEZONE", "Asia/Karachi"))
except Exception:                 # tzdata missing -> fall back to server time
    TZ = None


def load_secret_key():
    """Session secret. Shared with app.py; also used to encrypt the password copy for the main admin."""
    import secrets as _secrets
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(DATA_DIR, "secret.key")
    if not os.path.exists(path):
        with open(path, "w") as fh:
            fh.write(_secrets.token_hex(32))
    with open(path) as fh:
        return fh.read().strip()


def _fernet():
    try:
        import base64, hashlib
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256((load_secret_key() + ":pw-vault").encode()).digest())
    return Fernet(key)


def encrypt_secret(text):
    """Reversible (encrypted) copy so ONLY the main admin can read an employee's password."""
    f = _fernet()
    return f.encrypt(text.encode()).decode() if f and text else ""


def decrypt_secret(token):
    f = _fernet()
    if not f or not token:
        return None
    try:
        return f.decrypt(token.encode()).decode()
    except Exception:
        return None


DEFAULT_DEPARTMENTS = [
    ("IT Software", "Software development, QA and IT systems"),
    ("Human Resources", "Hiring, payroll and employee affairs"),
    ("Finance", "Accounts, budgets and audits"),
    ("Marketing", "Branding, campaigns and social media"),
    ("Sales", "Clients, leads and revenue"),
    ("Operations", "Day-to-day business operations"),
    ("Administration", "Office administration and facilities"),
    ("Production", "Manufacturing and production"),
    ("Quality Assurance", "Quality control and testing"),
    ("Research & Development", "New products and research"),
    ("Customer Support", "Helpdesk and customer care"),
    ("Logistics & Supply Chain", "Warehouse, shipping and procurement"),
]

DEFAULT_SETTINGS = {
    "required_hours": "8",
    "shift_start": "09:00",
    "late_grace_minutes": "15",
    "max_warnings": "3",           # missed check-out warnings before a day is marked Absent
    "face_threshold": "55",        # face match strictness (LOWER = stricter, 35-70)
}


def now():
    """Current local datetime (naive) in the configured timezone."""
    return datetime.now(TZ).replace(tzinfo=None) if TZ else datetime.now()


def today_str():
    return now().strftime(D_FMT)


@contextmanager
def conn_ctx():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


def _row(cur):
    r = cur.fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------- init
def init_db():
    with conn_ctx() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_default_password INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            is_main INTEGER DEFAULT 0,          -- 1 = main admin (only one)
            expires_at TEXT,                    -- NULL = permanent, otherwise temporary admin
            created_by TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS departments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            email TEXT DEFAULT '',
            designation TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            profile_picture TEXT DEFAULT '',
            social_linkedin TEXT DEFAULT '',
            social_facebook TEXT DEFAULT '',
            social_instagram TEXT DEFAULT '',
            social_x TEXT DEFAULT '',
            social_website TEXT DEFAULT '',
            department_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'face_pending',
                -- face_pending -> pending -> approved | rejected
            samples_captured INTEGER DEFAULT 0,
            flag_note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            approved_at TEXT,
            FOREIGN KEY (department_id) REFERENCES departments(id)
        );
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            work_date TEXT NOT NULL,
            check_in TEXT NOT NULL,
            check_out TEXT,
            worked_seconds INTEGER,
            is_late INTEGER DEFAULT 0,
            UNIQUE (employee_id, work_date),
            FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            department_id INTEGER,            -- NULL = everyone
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (department_id) REFERENCES departments(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS locations (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS wfh_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL, work_date TEXT NOT NULL, reason TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, reviewed_by TEXT DEFAULT '', FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE, UNIQUE(employee_id, work_date));
        CREATE TABLE IF NOT EXISTS leave_balances (
            employee_id INTEGER PRIMARY KEY, annual_total INTEGER NOT NULL DEFAULT 30,
            annual_used REAL NOT NULL DEFAULT 0, short_used REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS leave_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL,
            leave_date TEXT NOT NULL, leave_type TEXT NOT NULL DEFAULT 'Full Day',
            units REAL NOT NULL DEFAULT 1, reason TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL, reviewed_by TEXT DEFAULT '', admin_note TEXT DEFAULT '',
            FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL, recipient_role TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, status TEXT DEFAULT 'open', reply TEXT DEFAULT '', created_at TEXT NOT NULL, replied_at TEXT, FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            assigned_by TEXT DEFAULT '',
            due_date TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Assigned',
            employee_note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            work_date TEXT NOT NULL,
            number INTEGER NOT NULL,
            reason TEXT NOT NULL,
            cleared INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS certificates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER,
            recipient_name TEXT NOT NULL,
            certificate_title TEXT NOT NULL DEFAULT 'Best Employee',
            period TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            issued_by TEXT NOT NULL DEFAULT '',
            issued_at TEXT NOT NULL,
            FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            kind TEXT NOT NULL,              -- announcement | task | message | warning
            title TEXT NOT NULL,
            body TEXT DEFAULT '',
            url TEXT DEFAULT '',
            ref_id INTEGER,
            popped INTEGER DEFAULT 0,        -- popup already shown
            is_read INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS admin_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,              -- message | leave | wfh | task | approval
            title TEXT NOT NULL,
            body TEXT DEFAULT '',
            url TEXT DEFAULT '',
            ref_id INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admin_notif_state (
            admin_id INTEGER NOT NULL,
            notif_id INTEGER NOT NULL,
            popped INTEGER DEFAULT 0,
            is_read INTEGER DEFAULT 0,
            PRIMARY KEY (admin_id, notif_id)
        );
        CREATE TABLE IF NOT EXISTS holidays (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            holiday_date TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            created_by TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT DEFAULT '',
            ip TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payroll (
            id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL, pay_month TEXT NOT NULL,
            basic REAL NOT NULL DEFAULT 0, allowance REAL NOT NULL DEFAULT 0, bonus REAL NOT NULL DEFAULT 0,
            deduction REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'Pending', paid_at TEXT DEFAULT '',
            note TEXT DEFAULT '', created_at TEXT NOT NULL,
            FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE, UNIQUE(employee_id,pay_month)
        );
        CREATE TABLE IF NOT EXISTS performance_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL, review_period TEXT NOT NULL,
            score INTEGER NOT NULL DEFAULT 3, strengths TEXT DEFAULT '', improvements TEXT DEFAULT '',
            reviewer TEXT DEFAULT '', created_at TEXT NOT NULL,
            FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT DEFAULT '',
            employee_id INTEGER NOT NULL, priority TEXT DEFAULT 'Medium', status TEXT DEFAULT 'Planned',
            start_date TEXT DEFAULT '', due_date TEXT DEFAULT '', progress INTEGER DEFAULT 0,
            created_by TEXT DEFAULT '', created_at TEXT NOT NULL,
            FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL, category TEXT NOT NULL,
            amount REAL NOT NULL, expense_date TEXT NOT NULL, description TEXT DEFAULT '', receipt_url TEXT DEFAULT '',
            status TEXT DEFAULT 'Pending', admin_note TEXT DEFAULT '', created_at TEXT NOT NULL,
            FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS employee_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL, title TEXT NOT NULL,
            document_type TEXT DEFAULT 'Other', reference_url TEXT DEFAULT '', expiry_date TEXT DEFAULT '',
            note TEXT DEFAULT '', created_at TEXT NOT NULL,
            FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS employee_shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL, shift_name TEXT NOT NULL,
            start_time TEXT NOT NULL, end_time TEXT NOT NULL, effective_from TEXT NOT NULL,
            effective_to TEXT DEFAULT '', break_minutes INTEGER DEFAULT 60, created_at TEXT NOT NULL,
            FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS hr_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            full_name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
            created_by TEXT DEFAULT '', last_login TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS salary_profiles (
            employee_id INTEGER PRIMARY KEY, base_salary REAL NOT NULL DEFAULT 0, allowance REAL NOT NULL DEFAULT 0,
            updated_by TEXT DEFAULT '', updated_at TEXT DEFAULT '',
            FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS job_openings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, department_id INTEGER,
            description TEXT DEFAULT '', vacancies INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'Open',
            created_by TEXT DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, full_name TEXT NOT NULL,
            email TEXT DEFAULT '', phone TEXT DEFAULT '', stage TEXT NOT NULL DEFAULT 'Applied',
            notes TEXT DEFAULT '', created_by TEXT DEFAULT '', created_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES job_openings(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS meeting_alarms (
            id INTEGER PRIMARY KEY AUTOINCREMENT, department_id INTEGER, title TEXT NOT NULL,
            message TEXT DEFAULT '', created_by TEXT DEFAULT '', created_at TEXT NOT NULL, stopped INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS meeting_alarm_acks (
            alarm_id INTEGER NOT NULL, employee_id INTEGER NOT NULL, acked_at TEXT NOT NULL,
            PRIMARY KEY (alarm_id, employee_id)
        );
        CREATE INDEX IF NOT EXISTS idx_notif_emp ON notifications(employee_id, is_read);
        CREATE INDEX IF NOT EXISTS idx_task_employee ON tasks(employee_id, status);
        CREATE INDEX IF NOT EXISTS idx_att_date ON attendance(work_date);
        CREATE INDEX IF NOT EXISTS idx_emp_dept ON employees(department_id);
        """)

        c.execute("INSERT OR IGNORE INTO leave_balances(employee_id) SELECT id FROM employees")
        # --- migrate databases created before roles existed
        cols = {r[1] for r in c.execute("PRAGMA table_info(admins)")}
        for name, ddl in (("is_main", "INTEGER DEFAULT 0"), ("expires_at", "TEXT"),
                          ("created_by", "TEXT DEFAULT ''")):
            if name not in cols:
                c.execute(f"ALTER TABLE admins ADD COLUMN {name} {ddl}")
        # Safe additive migrations for attendance and employee location/work mode.
        att_cols = {r[1] for r in c.execute("PRAGMA table_info(attendance)")}
        for name, ddl in (("work_mode", "TEXT DEFAULT 'Office'"), ("location_name", "TEXT DEFAULT ''"), ("latitude", "REAL"), ("longitude", "REAL")):
            if name not in att_cols: c.execute(f"ALTER TABLE attendance ADD COLUMN {name} {ddl}")
        loc_cols = {r[1] for r in c.execute("PRAGMA table_info(locations)")}
        for name, ddl in (("latitude", "REAL"), ("longitude", "REAL"), ("radius_m", "INTEGER DEFAULT 150")):
            if name not in loc_cols: c.execute(f"ALTER TABLE locations ADD COLUMN {name} {ddl}")
        emp_cols = {r[1] for r in c.execute("PRAGMA table_info(employees)")}
        for name, ddl in (("phone", "TEXT DEFAULT ''"), ("profile_picture", "TEXT DEFAULT ''"),
                          ("social_linkedin", "TEXT DEFAULT ''"), ("social_facebook", "TEXT DEFAULT ''"),
                          ("social_instagram", "TEXT DEFAULT ''"), ("social_x", "TEXT DEFAULT ''"),
                          ("social_website", "TEXT DEFAULT ''")):
            if name not in emp_cols: c.execute(f"ALTER TABLE employees ADD COLUMN {name} {ddl}")
        # Advanced HR profile fields (safe additive migration; existing records stay intact).
        for name, ddl in (("employee_code", "TEXT DEFAULT ''"),
                          ("employment_type", "TEXT DEFAULT 'Full-time'"),
                          ("work_location", "TEXT DEFAULT ''"),
                          ("manager_name", "TEXT DEFAULT ''"),
                          ("joining_date", "TEXT DEFAULT ''"),
                          ("skills", "TEXT DEFAULT ''"),
                          ("education", "TEXT DEFAULT ''"),
                          ("bio", "TEXT DEFAULT ''")):
            if name not in emp_cols:
                c.execute(f"ALTER TABLE employees ADD COLUMN {name} {ddl}")
        for name, ddl in (("nickname", "TEXT DEFAULT ''"), ("password_enc", "TEXT DEFAULT ''")):
            if name not in emp_cols: c.execute(f"ALTER TABLE employees ADD COLUMN {name} {ddl}")
        att_cols = {r[1] for r in c.execute("PRAGMA table_info(attendance)")}
        if "miss_processed" not in att_cols:
            c.execute("ALTER TABLE attendance ADD COLUMN miss_processed INTEGER DEFAULT 0")
            # old days were never subject to the warning rule -> do not punish history
            c.execute("UPDATE attendance SET miss_processed = 1")
        if "marked_absent" not in att_cols:
            c.execute("ALTER TABLE attendance ADD COLUMN marked_absent INTEGER DEFAULT 0")
        task_cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
        for name in ("emp_deleted", "admin_deleted"):
            if name not in task_cols: c.execute(f"ALTER TABLE tasks ADD COLUMN {name} INTEGER DEFAULT 0")
        # HR projects: employee progress note + dashboard delete; documents: CNIC / ID number.
        for table, name, ddl in (("projects", "employee_note", "TEXT DEFAULT ''"),
                                 ("projects", "updated_at", "TEXT DEFAULT ''"),
                                 ("projects", "emp_deleted", "INTEGER DEFAULT 0"),
                                 ("employee_documents", "id_number", "TEXT DEFAULT ''")):
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        # Salary / leave-deduction / HR portal columns (additive; existing rows stay intact).
        for table, name, ddl in (("leave_requests", "salary_deduction", "REAL DEFAULT 0"),
                                 ("leave_requests", "deduction_note", "TEXT DEFAULT ''"),
                                 ("leave_requests", "deduction_by", "TEXT DEFAULT ''"),
                                 ("payroll", "leave_deduction", "REAL DEFAULT 0"),
                                 ("payroll", "other_deduction", "REAL DEFAULT 0"),
                                 ("payroll", "paid_by", "TEXT DEFAULT ''"),
                                 ("hr_users", "last_login", "TEXT DEFAULT ''")):
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        # Old payroll rows kept everything in `deduction`; treat it as "other" so totals stay correct.
        c.execute("UPDATE payroll SET other_deduction = deduction WHERE COALESCE(leave_deduction,0)=0 AND COALESCE(other_deduction,0)=0 AND deduction>0")
        if "assigned_location" not in emp_cols: c.execute("ALTER TABLE employees ADD COLUMN assigned_location TEXT DEFAULT ''")
        for loc in ("Johar Town", "Superior Gold Campus"):
            c.execute("INSERT OR IGNORE INTO locations(name, active) VALUES (?,1)", (loc,))
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        if c.execute("SELECT COUNT(*) FROM departments").fetchone()[0] == 0:
            c.executemany("INSERT INTO departments(name, description) VALUES (?, ?)", DEFAULT_DEPARTMENTS)
        if c.execute("SELECT COUNT(*) FROM admins").fetchone()[0] == 0:
            c.execute(
                "INSERT INTO admins(username, password_hash, is_default_password, created_at, is_main) "
                "VALUES (?,?,1,?,1)",
                (DEFAULT_ADMIN_USERNAME, generate_password_hash(DEFAULT_ADMIN_PASSWORD), now().strftime(DT_FMT)),
            )
        # exactly one main admin: the configured one (muzammil). Never temporary.
        if c.execute("SELECT COUNT(*) FROM admins WHERE is_main = 1").fetchone()[0] != 1:
            c.execute("UPDATE admins SET is_main = 0")
            c.execute("UPDATE admins SET is_main = 1, expires_at = NULL WHERE lower(username) = lower(?)",
                      (DEFAULT_ADMIN_USERNAME,))
            if c.execute("SELECT COUNT(*) FROM admins WHERE is_main = 1").fetchone()[0] == 0:
                c.execute("UPDATE admins SET is_main = 1, expires_at = NULL "
                          "WHERE id = (SELECT MIN(id) FROM admins)")


# ---------------------------------------------------------------- settings
def get_settings():
    with conn_ctx() as c:
        d = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}
    out = dict(DEFAULT_SETTINGS)
    out.update(d)
    return out


def required_seconds(settings=None):
    s = settings or get_settings()
    try:
        return int(float(s["required_hours"]) * 3600)
    except ValueError:
        return 8 * 3600


def save_settings(required_hours, shift_start, late_grace_minutes, max_warnings=None, face_threshold=None):
    pairs = [("required_hours", required_hours), ("shift_start", shift_start),
             ("late_grace_minutes", late_grace_minutes)]
    if max_warnings is not None:
        pairs.append(("max_warnings", max_warnings))
    if face_threshold is not None:
        pairs.append(("face_threshold", face_threshold))
    with conn_ctx() as c:
        for k, v in pairs:
            c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (k, str(v)))


# ---------------------------------------------------------------- admins
def get_admin_by_username(username):
    with conn_ctx() as c:
        return _row(c.execute("SELECT * FROM admins WHERE username = ?", (username,)))


def get_admin(admin_id):
    with conn_ctx() as c:
        return _row(c.execute("SELECT * FROM admins WHERE id = ?", (admin_id,)))


def set_admin_password(admin_id, new_password):
    with conn_ctx() as c:
        c.execute("UPDATE admins SET password_hash = ?, is_default_password = 0 WHERE id = ?",
                  (generate_password_hash(new_password), admin_id))


def verify_password(row, password):
    return bool(row) and check_password_hash(row["password_hash"], password)


def admin_expired(row):
    """True when a temporary admin's access time is over (the main admin never expires)."""
    if not row or row.get("is_main") or not row.get("expires_at"):
        return False
    try:
        return now() >= _parse(row["expires_at"])
    except (ValueError, TypeError):
        return True            # unreadable date -> fail closed


def list_admins():
    with conn_ctx() as c:
        rows = _rows(c.execute("SELECT * FROM admins ORDER BY is_main DESC, created_at"))
    for r in rows:
        r["expired"] = admin_expired(r)
    return rows


def create_admin(username, password, expires_at=None, created_by=""):
    """Add a normal (never main) admin. expires_at = None -> permanent, else 'YYYY-MM-DD HH:MM:SS'."""
    try:
        with conn_ctx() as c:
            c.execute(
                "INSERT INTO admins(username, password_hash, is_default_password, created_at, "
                "is_main, expires_at, created_by) VALUES (?,?,1,?,0,?,?)",
                (username.strip(), generate_password_hash(password), now().strftime(DT_FMT),
                 expires_at, created_by))
        return True
    except sqlite3.IntegrityError:
        return False


def delete_admin(admin_id):
    """Removes a non-main admin. The main admin can never be deleted."""
    with conn_ctx() as c:
        cur = c.execute("DELETE FROM admins WHERE id = ? AND is_main = 0", (admin_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------- departments
def list_departments():
    with conn_ctx() as c:
        return _rows(c.execute("SELECT * FROM departments ORDER BY name"))


def get_department(dept_id):
    with conn_ctx() as c:
        return _row(c.execute("SELECT * FROM departments WHERE id = ?", (dept_id,)))


def add_department(name, description=""):
    try:
        with conn_ctx() as c:
            c.execute("INSERT INTO departments(name, description) VALUES (?, ?)",
                      (name.strip(), description.strip()))
        return True
    except sqlite3.IntegrityError:
        return False


def delete_department(dept_id):
    """Only allowed when no employee belongs to it."""
    with conn_ctx() as c:
        n = c.execute("SELECT COUNT(*) FROM employees WHERE department_id = ?", (dept_id,)).fetchone()[0]
        if n:
            return False
        c.execute("DELETE FROM departments WHERE id = ?", (dept_id,))
    return True


def departments_overview():
    """Every department with employee counts and today's presence."""
    today = today_str()
    with conn_ctx() as c:
        return _rows(c.execute("""
            SELECT d.id, d.name, d.description,
                   COUNT(DISTINCT CASE WHEN e.status = 'approved' THEN e.id END) AS approved_count,
                   COUNT(DISTINCT e.id) AS total_count,
                   COUNT(DISTINCT a.id) AS present_today
            FROM departments d
            LEFT JOIN employees e ON e.department_id = d.id
            LEFT JOIN attendance a ON a.employee_id = e.id AND a.work_date = ?
            GROUP BY d.id ORDER BY d.name
        """, (today,)))


# ---------------------------------------------------------------- employees
def username_taken(username):
    with conn_ctx() as c:
        a = c.execute("SELECT 1 FROM employees WHERE lower(username) = lower(?)", (username,)).fetchone()
        b = c.execute("SELECT 1 FROM admins WHERE lower(username) = lower(?)", (username,)).fetchone()
    return bool(a or b)


def create_employee(full_name, username, password, department_id, email="", designation="", nickname=""):
    with conn_ctx() as c:
        cur = c.execute("""
            INSERT INTO employees(full_name, username, password_hash, email, designation,
                                  department_id, status, created_at, nickname, password_enc)
            VALUES (?,?,?,?,?,?, 'face_pending', ?, ?, ?)
        """, (full_name.strip(), username.strip(), generate_password_hash(password),
              email.strip(), designation.strip(), department_id, now().strftime(DT_FMT),
              nickname.strip(), encrypt_secret(password)))
        return cur.lastrowid


_EMP_SELECT = """
    SELECT e.*, d.name AS department_name
    FROM employees e JOIN departments d ON d.id = e.department_id
"""


def get_employee(emp_id):
    with conn_ctx() as c:
        return _row(c.execute(_EMP_SELECT + " WHERE e.id = ?", (emp_id,)))


def get_employee_by_username(username):
    with conn_ctx() as c:
        return _row(c.execute(_EMP_SELECT + " WHERE lower(e.username) = lower(?)", (username,)))


def list_employees(status=None, department_id=None, search=None):
    q, p = _EMP_SELECT + " WHERE 1=1", []
    if status:
        q += " AND e.status = ?"
        p.append(status)
    if department_id:
        q += " AND e.department_id = ?"
        p.append(department_id)
    if search:
        q += " AND (e.full_name LIKE ? OR e.username LIKE ?)"
        p += [f"%{search}%", f"%{search}%"]
    q += " ORDER BY e.full_name"
    with conn_ctx() as c:
        return _rows(c.execute(q, p))


def set_status(emp_id, status, flag_note=None):
    with conn_ctx() as c:
        if status == "approved":
            c.execute("UPDATE employees SET status=?, approved_at=? WHERE id=?",
                      (status, now().strftime(DT_FMT), emp_id))
        else:
            c.execute("UPDATE employees SET status=? WHERE id=?", (status, emp_id))
        if flag_note is not None:
            c.execute("UPDATE employees SET flag_note=? WHERE id=?", (flag_note, emp_id))


def set_samples(emp_id, count):
    with conn_ctx() as c:
        c.execute("UPDATE employees SET samples_captured=? WHERE id=?", (count, emp_id))


def set_employee_password(emp_id, new_password):
    with conn_ctx() as c:
        c.execute("UPDATE employees SET password_hash=?, password_enc=? WHERE id=?",
                  (generate_password_hash(new_password), encrypt_secret(new_password), emp_id))


def store_password_copy(emp_id, password):
    """Older accounts only had a hash; after a correct login keep the encrypted copy for the main admin."""
    with conn_ctx() as c:
        c.execute("UPDATE employees SET password_enc=? WHERE id=? AND COALESCE(password_enc,'')=''",
                  (encrypt_secret(password), emp_id))


def set_nickname(emp_id, nickname):
    with conn_ctx() as c:
        c.execute("UPDATE employees SET nickname=? WHERE id=?", (nickname.strip(), emp_id))


def nickname_matches(emp, nickname):
    import secrets as _s
    stored = (emp.get("nickname") or "").strip().lower()
    given = (nickname or "").strip().lower()
    return bool(stored) and _s.compare_digest(stored.encode(), given.encode())


def delete_employee(emp_id):
    with conn_ctx() as c:
        c.execute("DELETE FROM attendance WHERE employee_id=?", (emp_id,))
        c.execute("DELETE FROM employees WHERE id=?", (emp_id,))


def department_members(dept_id):
    """Approved members of ONE department + today's attendance state (used by employee panel)."""
    today = today_str()
    with conn_ctx() as c:
        return _rows(c.execute("""
            SELECT e.id, e.full_name, e.designation, a.check_in, a.check_out
            FROM employees e
            LEFT JOIN attendance a ON a.employee_id = e.id AND a.work_date = ?
            WHERE e.department_id = ? AND e.status = 'approved'
            ORDER BY e.full_name
        """, (today, dept_id)))


# ---------------------------------------------------------------- attendance
def _parse(dt):
    return datetime.strptime(dt, DT_FMT) if dt else None


def enrich(row, req_seconds, now_dt=None):
    """Add worked time, status and status colour to an attendance row."""
    now_dt = now_dt or now()
    ci, co = _parse(row["check_in"]), _parse(row["check_out"])
    if row.get("marked_absent"):
        worked, status, cls = 0, "Absent", "bad"
    elif co:
        worked = row["worked_seconds"] if row["worked_seconds"] is not None else int((co - ci).total_seconds())
        if worked >= req_seconds:
            status, cls = "Completed", "ok"
        else:
            status, cls = "Short Hours", "warn"
    elif row["work_date"] == now_dt.strftime(D_FMT):
        worked = max(0, int((now_dt - ci).total_seconds()))
        status, cls = "Working", "live"
    else:
        worked = 0
        status, cls = "No Check-out", "bad"
    row = dict(row)
    row.update(
        worked=worked,
        required=req_seconds,
        short_by=max(0, req_seconds - worked) if co else None,
        overtime=max(0, worked - req_seconds) if co else None,
        status=status,
        status_class=cls,
        check_in_time=ci.strftime("%I:%M:%S %p"),
        check_out_time=co.strftime("%I:%M:%S %p") if co else "—",
    )
    return row


def get_today_record(emp_id):
    with conn_ctx() as c:
        return _row(c.execute("SELECT * FROM attendance WHERE employee_id=? AND work_date=?",
                              (emp_id, today_str())))


def check_in(emp_id):
    """Returns (ok, message)."""
    n = now()
    settings = get_settings()
    with conn_ctx() as c:
        if c.execute("SELECT 1 FROM attendance WHERE employee_id=? AND work_date=?",
                     (emp_id, n.strftime(D_FMT))).fetchone():
            return False, "You have already checked in today."
        h, m = [int(x) for x in settings["shift_start"].split(":")]
        grace = int(settings["late_grace_minutes"])
        late_after = n.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(minutes=grace)
        is_late = 1 if n > late_after else 0
        c.execute("INSERT INTO attendance(employee_id, work_date, check_in, is_late) VALUES (?,?,?,?)",
                  (emp_id, n.strftime(D_FMT), n.strftime(DT_FMT), is_late))
    return True, "Checked in at " + n.strftime("%I:%M %p") + (" (late)." if is_late else ".")


def check_out(emp_id, allow_early=False):
    """Returns (ok, message). Early leave only when allow_early is True."""
    n = now()
    req = required_seconds()
    with conn_ctx() as c:
        rec = _row(c.execute("SELECT * FROM attendance WHERE employee_id=? AND work_date=?",
                             (emp_id, n.strftime(D_FMT))))
        if not rec:
            return False, "You have not checked in today."
        if rec["check_out"]:
            return False, "You have already checked out today."
        worked = int((n - _parse(rec["check_in"])).total_seconds())
        if worked < MIN_SESSION_SECONDS:
            return False, "Please wait at least a minute after checking in."
        if worked < req and not allow_early:
            return False, "Required working hours are not complete yet."
        c.execute("UPDATE attendance SET check_out=?, worked_seconds=? WHERE id=?",
                  (n.strftime(DT_FMT), worked, rec["id"]))
    return True, "Checked out at " + n.strftime("%I:%M %p") + ". Worked " + fmt_hm(worked) + "."


def dt_pretty(value):
    try:
        return _parse(value).strftime("%d %b %Y, %I:%M %p")
    except Exception:
        return value or "—"


def fmt_hm(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    h, m = seconds // 3600, (seconds % 3600) // 60
    return f"{h}h {m:02d}m"


def attendance_report(date_from=None, date_to=None, department_id=None, employee_id=None, status=None):
    q = """
        SELECT a.*, e.full_name, e.username, e.department_id, d.name AS department_name
        FROM attendance a
        JOIN employees e ON e.id = a.employee_id
        JOIN departments d ON d.id = e.department_id
        WHERE 1=1
    """
    p = []
    if date_from:
        q += " AND a.work_date >= ?"; p.append(date_from)
    if date_to:
        q += " AND a.work_date <= ?"; p.append(date_to)
    if department_id:
        q += " AND e.department_id = ?"; p.append(department_id)
    if employee_id:
        q += " AND a.employee_id = ?"; p.append(employee_id)
    q += " ORDER BY a.work_date DESC, a.check_in DESC"
    with conn_ctx() as c:
        rows = _rows(c.execute(q, p))
    req = required_seconds()
    nw = now()
    rows = [enrich(r, req, nw) for r in rows]
    if status:
        rows = [r for r in rows if r["status"] == status]
    return rows


def employee_history(emp_id, month=None, limit=None):
    """month = 'YYYY-MM' or None for latest records."""
    q = "SELECT * FROM attendance WHERE employee_id = ?"
    p = [emp_id]
    if month:
        q += " AND work_date LIKE ?"; p.append(month + "-%")
    q += " ORDER BY work_date DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    with conn_ctx() as c:
        rows = _rows(c.execute(q, p))
    req = required_seconds()
    nw = now()
    return [enrich(r, req, nw) for r in rows]


def month_summary(emp_id, month=None):
    month = month or now().strftime("%Y-%m")
    rows = employee_history(emp_id, month)
    done = [r for r in rows if r["check_out"]]
    total = sum(r["worked"] for r in done)
    return {
        "month": month,
        "days_present": len(rows),
        "total_seconds": total,
        "avg_seconds": int(total / len(done)) if done else 0,
        "late_days": sum(1 for r in rows if r["is_late"]),
        "short_days": sum(1 for r in rows if r["status"] == "Short Hours"),
        "missed_checkouts": sum(1 for r in rows if r["status"] == "No Check-out"),
        "absent_marked": sum(1 for r in rows if r["status"] == "Absent"),
        "warnings": active_warning_count(emp_id),
        "max_warnings": max_warnings(),
    }


# ---------------------------------------------------------------- admin stats
def admin_stats():
    today = today_str()
    with conn_ctx() as c:
        one = lambda q, p=(): c.execute(q, p).fetchone()[0]
        approved = one("SELECT COUNT(*) FROM employees WHERE status='approved'")
        present = one("SELECT COUNT(*) FROM attendance WHERE work_date=?", (today,))
        working = one("SELECT COUNT(*) FROM attendance WHERE work_date=? AND check_out IS NULL", (today,))
        return {
            "approved": approved,
            "pending": one("SELECT COUNT(*) FROM employees WHERE status='pending'"),
            "enrolling": one("SELECT COUNT(*) FROM employees WHERE status='face_pending'"),
            "departments": one("SELECT COUNT(*) FROM departments"),
            "present": present,
            "working": working,
            "left": present - working,
            "absent": max(0, approved - present),
            "late": one("SELECT COUNT(*) FROM attendance WHERE work_date=? AND is_late=1", (today,)),
        }


def absent_today():
    today = today_str()
    if is_holiday(today):
        return []
    with conn_ctx() as c:
        return _rows(c.execute("""
            SELECT e.id, e.full_name, d.name AS department_name
            FROM employees e JOIN departments d ON d.id = e.department_id
            WHERE e.status='approved'
              AND NOT EXISTS (SELECT 1 FROM attendance a WHERE a.employee_id=e.id AND a.work_date=?)
            ORDER BY d.name, e.full_name
        """, (today,)))


# ---------------------------------------------------------------- announcements
def add_announcement(department_id, title, body):
    """Saves the notice and sends a popup/notification to every approved employee it is meant for."""
    stamp = now().strftime(DT_FMT)
    with conn_ctx() as c:
        cur = c.execute("INSERT INTO announcements(department_id, title, body, created_at) VALUES (?,?,?,?)",
                        (department_id, title.strip(), body.strip(), stamp))
        ann_id = cur.lastrowid
        if department_id:
            ids = [r[0] for r in c.execute("SELECT id FROM employees WHERE status='approved' AND department_id=?",
                                           (department_id,))]
        else:
            ids = [r[0] for r in c.execute("SELECT id FROM employees WHERE status='approved'")]
        c.executemany(
            "INSERT INTO notifications(employee_id, kind, title, body, url, ref_id, created_at) VALUES (?,?,?,?,?,?,?)",
            [(i, "announcement", title.strip(), body.strip()[:160], "/announcements", ann_id, stamp) for i in ids])
    return ann_id


def delete_announcement(ann_id):
    with conn_ctx() as c:
        c.execute("DELETE FROM announcements WHERE id=?", (ann_id,))
        c.execute("DELETE FROM notifications WHERE kind='announcement' AND ref_id=?", (ann_id,))


def list_announcements(department_id=None, everything=False, limit=20):
    """Employee panel: only 'everyone' notices + the employee's own department."""
    q = """SELECT a.*, d.name AS department_name FROM announcements a
           LEFT JOIN departments d ON d.id = a.department_id"""
    p = []
    if not everything:
        q += " WHERE a.department_id IS NULL OR a.department_id = ?"
        p.append(department_id)
    q += f" ORDER BY a.created_at DESC LIMIT {int(limit)}"
    with conn_ctx() as c:
        return _rows(c.execute(q, p))


def count_status(status):
    with conn_ctx() as c:
        return c.execute("SELECT COUNT(*) FROM employees WHERE status=?", (status,)).fetchone()[0]


# ---------------------------------------------------------------- missed check-out warnings
def max_warnings():
    try:
        return max(1, int(get_settings().get("max_warnings", 3)))
    except (TypeError, ValueError):
        return 3


def active_warning_count(emp_id):
    with conn_ctx() as c:
        return c.execute("SELECT COUNT(*) FROM warnings WHERE employee_id=? AND cleared=0", (emp_id,)).fetchone()[0]


def list_warnings(emp_id, include_cleared=True):
    q = "SELECT * FROM warnings WHERE employee_id=?"
    if not include_cleared:
        q += " AND cleared=0"
    with conn_ctx() as c:
        return _rows(c.execute(q + " ORDER BY created_at DESC", (emp_id,)))


def clear_warnings(emp_id):
    with conn_ctx() as c:
        c.execute("UPDATE warnings SET cleared=1 WHERE employee_id=?", (emp_id,))


def process_missed_checkouts():
    """
    Runs automatically (every minute + on requests). A day whose check-out is still missing after
    midnight counts as a missed check-out:
        missed #1, #2, #3 (limit is configurable)  -> a warning + popup
        any miss after the limit                    -> that day is marked ABSENT
    Every attendance row is processed exactly once (miss_processed flag).
    """
    limit = max_warnings()
    today = today_str()
    done = 0
    with conn_ctx() as c:
        rows = c.execute("""SELECT id, employee_id, work_date FROM attendance
                            WHERE check_out IS NULL AND miss_processed = 0 AND work_date < ?
                            ORDER BY work_date, id""", (today,)).fetchall()
        for r in rows:
            if not c.execute("UPDATE attendance SET miss_processed=1 WHERE id=? AND miss_processed=0",
                             (r["id"],)).rowcount:
                continue
            stamp = now().strftime(DT_FMT)
            used = c.execute("SELECT COUNT(*) FROM warnings WHERE employee_id=? AND cleared=0",
                             (r["employee_id"],)).fetchone()[0]
            nice = datetime.strptime(r["work_date"], D_FMT).strftime("%d %b %Y")
            if used < limit:
                num = used + 1
                reason = f"You did not check out on {nice} before midnight."
                c.execute("INSERT INTO warnings(employee_id, work_date, number, reason, created_at) VALUES (?,?,?,?,?)",
                          (r["employee_id"], r["work_date"], num, reason, stamp))
                tail = (" This was your LAST warning. The next missed check-out will be marked Absent."
                        if num >= limit else " Please always check out before midnight.")
                c.execute("""INSERT INTO notifications(employee_id, kind, title, body, url, ref_id, created_at)
                             VALUES (?,?,?,?,?,?,?)""",
                          (r["employee_id"], "warning", f"Warning {num} of {limit}: missed check-out",
                           reason + tail, "/dashboard", r["id"], stamp))
            else:
                c.execute("UPDATE attendance SET marked_absent=1 WHERE id=?", (r["id"],))
                c.execute("""INSERT INTO notifications(employee_id, kind, title, body, url, ref_id, created_at)
                             VALUES (?,?,?,?,?,?,?)""",
                          (r["employee_id"], "warning", f"Marked Absent for {nice}",
                           f"You already used all {limit} warnings and did not check out on {nice}, "
                           "so that day was marked Absent.", "/my-attendance", r["id"], stamp))
            done += 1
    return done


# ---------------------------------------------------------------- notifications (popup + sound + counters)
@contextmanager
def _maybe_conn(conn):
    if conn is not None:
        yield conn
    else:
        with conn_ctx() as c:
            yield c


NOTIFY_KINDS = ("announcement", "task", "message", "warning", "leave", "wfh", "salary", "certificate")
ADMIN_NOTIFY_KINDS = ("message", "leave", "wfh", "task", "approval", "security", "hr")


def notify(emp_id, kind, title, body="", url="", ref_id=None, conn=None):
    """conn: pass the caller's open connection when notifying from inside a `with conn_ctx()` block (avoids a DB lock)."""
    with _maybe_conn(conn) as c:
        c.execute("""INSERT INTO notifications(employee_id, kind, title, body, url, ref_id, created_at)
                     VALUES (?,?,?,?,?,?,?)""",
                  (emp_id, kind, title, body, url, ref_id, now().strftime(DT_FMT)))
        if smsclient.enabled() and kind in smsclient.notify_kinds():      # also send it as a real SMS
            row = c.execute("SELECT phone FROM employees WHERE id=?", (emp_id,)).fetchone()
            if row and row[0]:
                text = "CyberAttend: " + title + (" - " + body if body else "")
                smsclient.send_async(row[0], text[:300])


def unread_counts(emp_id):
    out = {k: 0 for k in NOTIFY_KINDS}
    with conn_ctx() as c:
        for r in c.execute("SELECT kind, COUNT(*) n FROM notifications WHERE employee_id=? AND is_read=0 GROUP BY kind",
                           (emp_id,)):
            out[r["kind"]] = r["n"]
    out["total"] = sum(out[k] for k in NOTIFY_KINDS)
    return out


def unread_ref_ids(emp_id, kind):
    with conn_ctx() as c:
        return {r[0] for r in c.execute(
            "SELECT ref_id FROM notifications WHERE employee_id=? AND kind=? AND is_read=0 AND ref_id IS NOT NULL",
            (emp_id, kind))}


def mark_read_kind(emp_id, kind):
    with conn_ctx() as c:
        c.execute("UPDATE notifications SET is_read=1, popped=1 WHERE employee_id=? AND kind=? AND is_read=0",
                  (emp_id, kind))


def mark_read_ids(emp_id, ids):
    ids = [int(i) for i in ids if str(i).isdigit()]
    if not ids:
        return
    with conn_ctx() as c:
        c.execute(f"UPDATE notifications SET is_read=1 WHERE employee_id=? AND id IN ({','.join('?' * len(ids))})",
                  (emp_id, *ids))


def pop_new_notifications(emp_id, limit=20):
    """Notifications whose popup has not been shown yet. Returned once, then flagged as shown."""
    with conn_ctx() as c:
        rows = _rows(c.execute("""SELECT id, kind, title, body, url, created_at FROM notifications
                                  WHERE employee_id=? AND popped=0 AND is_read=0
                                  ORDER BY id LIMIT ?""", (emp_id, limit)))
        if rows:
            c.execute(f"UPDATE notifications SET popped=1 WHERE id IN ({','.join('?' * len(rows))})",
                      [r["id"] for r in rows])
    return rows


# ---------------------------------------------------------------- ADMIN notifications (popup + sound + counters)
# One shared feed; every admin has their own read/popup state, so each admin gets their own popup and numbers.
def admin_notify(kind, title, body="", url="", ref_id=None, conn=None):
    with _maybe_conn(conn) as c:
        c.execute("""INSERT INTO admin_notifications(kind, title, body, url, ref_id, created_at)
                     VALUES (?,?,?,?,?,?)""", (kind, title, body, url, ref_id, now().strftime(DT_FMT)))


_ADMIN_FEED = """FROM admin_notifications n
    JOIN admins a ON a.id = ? AND n.created_at >= a.created_at
    LEFT JOIN admin_notif_state s ON s.notif_id = n.id AND s.admin_id = a.id"""


def admin_unread_counts(admin_id):
    out = {k: 0 for k in ADMIN_NOTIFY_KINDS}
    with conn_ctx() as c:
        for r in c.execute(f"SELECT n.kind, COUNT(*) AS n {_ADMIN_FEED} WHERE COALESCE(s.is_read,0)=0 GROUP BY n.kind",
                           (admin_id,)):
            out[r["kind"]] = r["n"]
    out["total"] = sum(out[k] for k in ADMIN_NOTIFY_KINDS)
    return out


def admin_unread_ref_ids(admin_id, kind):
    with conn_ctx() as c:
        return {r[0] for r in c.execute(
            f"SELECT n.ref_id {_ADMIN_FEED} WHERE n.kind=? AND COALESCE(s.is_read,0)=0 AND n.ref_id IS NOT NULL",
            (admin_id, kind))}


def admin_mark_read_kind(admin_id, kind):
    with conn_ctx() as c:
        ids = [r[0] for r in c.execute(f"SELECT n.id {_ADMIN_FEED} WHERE n.kind=? AND COALESCE(s.is_read,0)=0",
                                       (admin_id, kind))]
        c.executemany("""INSERT INTO admin_notif_state(admin_id, notif_id, popped, is_read) VALUES (?,?,1,1)
                         ON CONFLICT(admin_id, notif_id) DO UPDATE SET popped=1, is_read=1""",
                      [(admin_id, i) for i in ids])


def admin_mark_read_ids(admin_id, ids):
    ids = [int(i) for i in ids if str(i).isdigit()]
    with conn_ctx() as c:
        c.executemany("""INSERT INTO admin_notif_state(admin_id, notif_id, popped, is_read) VALUES (?,?,1,1)
                         ON CONFLICT(admin_id, notif_id) DO UPDATE SET popped=1, is_read=1""",
                      [(admin_id, i) for i in ids])


def admin_pop_new(admin_id, limit=20):
    """Notifications this admin has not yet been shown as a popup (returned once)."""
    with conn_ctx() as c:
        rows = _rows(c.execute(f"""SELECT n.id, n.kind, n.title, n.body, n.url, n.created_at {_ADMIN_FEED}
                                   WHERE COALESCE(s.popped,0)=0 AND COALESCE(s.is_read,0)=0
                                   ORDER BY n.id LIMIT ?""", (admin_id, limit)))
        c.executemany("""INSERT INTO admin_notif_state(admin_id, notif_id, popped, is_read) VALUES (?,?,1,0)
                         ON CONFLICT(admin_id, notif_id) DO UPDATE SET popped=1""",
                      [(admin_id, r["id"]) for r in rows])
    return rows


# ---------------------------------------------------------------- holidays
def is_holiday(date_str):
    with conn_ctx() as c:
        return c.execute("SELECT 1 FROM holidays WHERE holiday_date=?", (date_str,)).fetchone() is not None


def list_holidays(upcoming_only=False, limit=200):
    q, p = "SELECT * FROM holidays", []
    if upcoming_only:
        q += " WHERE holiday_date >= ?"; p.append(today_str())
    q += " ORDER BY holiday_date" + (" LIMIT ?" if limit else "")
    if limit: p.append(limit)
    with conn_ctx() as c:
        return _rows(c.execute(q, p))


def add_holiday(date_str, name, by=""):
    with conn_ctx() as c:
        c.execute("INSERT INTO holidays(holiday_date, name, created_by, created_at) VALUES (?,?,?,?)",
                  (date_str, name.strip()[:120], by, now().strftime(DT_FMT)))


def delete_holiday(hid):
    with conn_ctx() as c:
        c.execute("DELETE FROM holidays WHERE id=?", (hid,))


# ---------------------------------------------------------------- notification history pages
def list_notifications(emp_id, limit=200):
    with conn_ctx() as c:
        return _rows(c.execute("SELECT * FROM notifications WHERE employee_id=? ORDER BY id DESC LIMIT ?", (emp_id, limit)))


def mark_all_read(emp_id):
    with conn_ctx() as c:
        c.execute("UPDATE notifications SET is_read=1, popped=1 WHERE employee_id=? AND is_read=0", (emp_id,))


def admin_list_notifications(admin_id, limit=200):
    with conn_ctx() as c:
        return _rows(c.execute(f"""SELECT n.id, n.kind, n.title, n.body, n.url, n.created_at,
                                   COALESCE(s.is_read,0) AS is_read {_ADMIN_FEED} ORDER BY n.id DESC LIMIT ?""",
                               (admin_id, limit)))


def admin_mark_all_read(admin_id):
    with conn_ctx() as c:
        ids = [r[0] for r in c.execute(f"SELECT n.id {_ADMIN_FEED} WHERE COALESCE(s.is_read,0)=0", (admin_id,))]
        c.executemany("""INSERT INTO admin_notif_state(admin_id, notif_id, popped, is_read) VALUES (?,?,1,1)
                         ON CONFLICT(admin_id, notif_id) DO UPDATE SET popped=1, is_read=1""",
                      [(admin_id, i) for i in ids])


# ---------------------------------------------------------------- audit log (what each admin did)
def log_action(admin, action, detail="", ip=""):
    with conn_ctx() as c:
        c.execute("INSERT INTO audit_log(admin, action, detail, ip, created_at) VALUES (?,?,?,?,?)",
                  (admin, action[:80], detail[:500], ip[:64], now().strftime(DT_FMT)))


def list_audit(q="", limit=300):
    sql, p = "SELECT * FROM audit_log", []
    if q:
        sql += " WHERE admin LIKE ? OR action LIKE ? OR detail LIKE ?"
        p += [f"%{q}%"] * 3
    with conn_ctx() as c:
        return _rows(c.execute(sql + " ORDER BY id DESC LIMIT ?", (*p, limit)))


def backup_database(dest_path):
    """Consistent copy of the live SQLite database (safe while the app is running)."""
    src = sqlite3.connect(DB_PATH)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


# ---------------------------------------------------------------- tasks (soft delete: the sheet keeps everything)
def employee_tasks(emp_id, visible_only=True):
    q = "SELECT * FROM tasks WHERE employee_id=?"
    if visible_only:
        q += " AND COALESCE(emp_deleted,0)=0"
    with conn_ctx() as c:
        return _rows(c.execute(q + " ORDER BY created_at DESC", (emp_id,)))


def employee_projects(emp_id, visible_only=True):
    """Projects assigned by HR / admin. Shown beside tasks on the employee's Work & Tasks page."""
    q = "SELECT * FROM projects WHERE employee_id=?"
    if visible_only:
        q += " AND COALESCE(emp_deleted,0)=0"
    with conn_ctx() as c:
        return _rows(c.execute(q + " ORDER BY id DESC", (emp_id,)))


def get_int_setting(key, default=0):
    with conn_ctx() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    try:
        return int(r[0]) if r else default
    except (TypeError, ValueError):
        return default


def set_int_setting(key, value):
    with conn_ctx() as c:
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, str(int(value))))


def employee_delete_task(emp_id, task_id):
    """Removes a finished task from the employee's dashboard only. It stays in the sheet/records."""
    with conn_ctx() as c:
        return c.execute("""UPDATE tasks SET emp_deleted=1, updated_at=updated_at
                            WHERE id=? AND employee_id=? AND status IN ('Completed','Rejected')""",
                         (task_id, emp_id)).rowcount > 0


def admin_delete_task(task_id):
    with conn_ctx() as c:
        return c.execute("""UPDATE tasks SET admin_deleted=1
                            WHERE id=? AND status IN ('Completed','Rejected')""", (task_id,)).rowcount > 0


# ------------------------------------------------------------------ meeting alarms (admin -> department, rings on web + mobile)
ALARM_WINDOW_MIN = 15          # an alarm keeps ringing until acknowledged, stopped, or this many minutes pass


def create_alarm(dept_id, title, message, by):
    with conn_ctx() as c:
        cur = c.execute("INSERT INTO meeting_alarms(department_id,title,message,created_by,created_at) VALUES(?,?,?,?,?)",
                        (dept_id or None, title, message, by, now().strftime(DT_FMT)))
        return cur.lastrowid


def active_alarms_for(emp):
    """Alarms that should be ringing right now for this employee (their department or everyone)."""
    cutoff = (now() - timedelta(minutes=ALARM_WINDOW_MIN)).strftime(DT_FMT)
    with conn_ctx() as c:
        rows = c.execute(
            "SELECT a.id,a.title,a.message,a.created_by,a.created_at,COALESCE(d.name,'Everyone') AS department_name "
            "FROM meeting_alarms a LEFT JOIN departments d ON d.id=a.department_id "
            "WHERE a.stopped=0 AND a.created_at>=? AND (a.department_id IS NULL OR a.department_id=?) "
            "AND NOT EXISTS (SELECT 1 FROM meeting_alarm_acks k WHERE k.alarm_id=a.id AND k.employee_id=?) "
            "ORDER BY a.id DESC", (cutoff, emp["department_id"], emp["id"])).fetchall()
        return [dict(r) for r in rows]


def ack_alarm(alarm_id, emp_id):
    with conn_ctx() as c:
        c.execute("INSERT OR IGNORE INTO meeting_alarm_acks(alarm_id,employee_id,acked_at) VALUES(?,?,?)",
                  (alarm_id, emp_id, now().strftime(DT_FMT)))


def stop_alarm(alarm_id):
    with conn_ctx() as c:
        c.execute("UPDATE meeting_alarms SET stopped=1 WHERE id=?", (alarm_id,))


def recent_alarms(limit=30):
    cutoff = (now() - timedelta(minutes=ALARM_WINDOW_MIN)).strftime(DT_FMT)
    with conn_ctx() as c:
        out = []
        for r in c.execute("SELECT a.*,COALESCE(d.name,'Everyone') AS department_name FROM meeting_alarms a "
                           "LEFT JOIN departments d ON d.id=a.department_id ORDER BY a.id DESC LIMIT ?", (limit,)):
            a = dict(r)
            if a["department_id"]:
                a["targets"] = c.execute("SELECT COUNT(*) FROM employees WHERE status='approved' AND department_id=?", (a["department_id"],)).fetchone()[0]
            else:
                a["targets"] = c.execute("SELECT COUNT(*) FROM employees WHERE status='approved'").fetchone()[0]
            a["acked"] = c.execute("SELECT COUNT(*) FROM meeting_alarm_acks WHERE alarm_id=?", (a["id"],)).fetchone()[0]
            a["ringing"] = (not a["stopped"]) and a["created_at"] >= cutoff
            out.append(a)
        return out


def employees_phones_for_alarm(dept_id):
    with conn_ctx() as c:
        if dept_id:
            rows = c.execute("SELECT phone FROM employees WHERE status='approved' AND department_id=? AND phone<>''", (dept_id,))
        else:
            rows = c.execute("SELECT phone FROM employees WHERE status='approved' AND phone<>''")
        return [r[0] for r in rows]
