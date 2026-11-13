# HRMS database setup and repair

The application uses SQLite. The database file is `attendance.db` in the project folder by default. On a hosted deployment, set `DATA_DIR` to a persistent disk/volume directory so the database survives restarts and redeployments.

## First run

1. Install dependencies: `python -m pip install -r requirements.txt`
2. Run: `python migrate_database.py`
3. Start: `python app.py`

`app.py` also runs the safe additive database initialization automatically at startup. The migration script makes a backup before upgrading an existing database and checks SQLite integrity plus required HR tables.

## If the database appears broken

1. Stop the Flask server with Ctrl+C.
2. Make a copy of `attendance.db` somewhere safe.
3. Run `python migrate_database.py`.
4. If it reports success, run `python app.py` again.
5. If it fails, do not delete the backup; send the complete terminal error for diagnosis.

## Important

- Do not replace an existing `attendance.db` with a blank database if it contains real employee records.
- Keep `secret.key` unchanged when retaining the existing database. Changing it can invalidate login sessions and encrypted stored secrets.
- Never share `attendance.db` or `secret.key` publicly; they contain private business and employee information.
- SQLite is suitable for a small deployment. For multiple concurrent workers or larger production use, plan a tested migration to PostgreSQL.

## Enterprise upgrade: live + archive databases
- `attendance.db` is the live operational database. Do not replace it with an older ZIP copy if you already have newer records.
- `employee_archive.db` is created automatically on startup. Before a main admin deletes/offboards an employee, the app copies that employee's profile and HR history into this separate archive database.
- `long_term_backups/` stores daily SQLite snapshots of the live database. Both employee archive records and daily snapshots are retained for 10 years (3652 days), then pruned by the daily retention task.
- Keep `DATA_DIR` on a persistent disk/volume. For production, also copy backups to secure off-site storage. The archive database is an application-level retention layer, not a substitute for a disaster-recovery backup.
