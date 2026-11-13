"""Safely upgrade the HRMS SQLite database without deleting existing records.

Run from the project folder:
    python migrate_database.py
"""
from pathlib import Path
import sqlite3
import shutil
import sys

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "attendance.db"
BACKUP_DIR = BASE_DIR / "database_backups"

if not DB_FILE.exists():
    print("attendance.db was not found. A new database will be created.")
else:
    BACKUP_DIR.mkdir(exist_ok=True)
    backup = BACKUP_DIR / "attendance_before_upgrade.db"
    if not backup.exists():
        shutil.copy2(DB_FILE, backup)
        print(f"Backup created: {backup.name}")
    else:
        # Keep the first backup safe and create a timestamped backup for this run.
        from datetime import datetime
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = BACKUP_DIR / f"attendance_before_upgrade_{stamp}.db"
        shutil.copy2(DB_FILE, backup)
        print(f"Backup created: {backup.name}")

try:
    import database as db
    db.init_db()
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {result}")
        required = {
            "admins", "employees", "attendance", "leave_requests", "payroll",
            "performance_reviews", "projects", "expenses", "employee_documents",
            "employee_shifts", "notifications", "admin_notifications", "audit_log"
        }
        existing = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(required - existing)
        if missing:
            raise RuntimeError("Missing database tables: " + ", ".join(missing))
        counts = {}
        for table in ("admins", "employees", "attendance", "payroll", "projects", "expenses"):
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print("Database upgrade completed successfully.")
    print("Integrity check: OK")
    for table, count in counts.items():
        print(f"{table}: {count} records")
except Exception as exc:
    print(f"Database upgrade failed: {exc}", file=sys.stderr)
    print("Your original database backup has been kept. Do not delete it.", file=sys.stderr)
    sys.exit(1)
