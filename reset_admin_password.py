"""Reset the MAIN CyberAttend admin password without deleting attendance data.

Run from the cyberattend folder after installing requirements:
    py reset_admin_password.py

The reset password is the documented first-login password. Change it after login.
"""
import os
import sqlite3
from pathlib import Path
from werkzeug.security import generate_password_hash

DATA_DIR = os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent))
DB_PATH = os.path.join(DATA_DIR, "attendance.db")
USERNAME = os.environ.get("ADMIN_USERNAME", "muzammil")
NEW_PASSWORD = "muzammil182921"

if not os.path.isfile(DB_PATH):
    raise SystemExit(f"Database not found: {DB_PATH}")

con = sqlite3.connect(DB_PATH)
try:
    cur = con.execute(
        "UPDATE admins SET password_hash=?, is_default_password=1 "
        "WHERE username=? AND is_main=1",
        (generate_password_hash(NEW_PASSWORD), USERNAME),
    )
    if cur.rowcount != 1:
        con.rollback()
        raise SystemExit(
            f"Expected one MAIN admin named {USERNAME!r}; found {cur.rowcount}. "
            "Check ADMIN_USERNAME and the selected database."
        )
    con.commit()
finally:
    con.close()

print("Main admin password reset successfully. Existing employee and attendance data was kept.")
print(f"Username: {USERNAME}")
print(f"Password: {NEW_PASSWORD}")
print("Log in, then immediately change this password in Admin > Settings.")
