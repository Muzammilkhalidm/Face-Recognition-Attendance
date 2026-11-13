MUZAMMILL HRMS - ALL-IN-ONE PACKAGE
===================================

This package combines the latest project version with:
- Existing face-recognition attendance and check-in/check-out
- Admin and Employee panels
- Advanced HR workspace (payroll, performance reviews, projects, expenses, shifts, documents)
- Separate HR login; Main Admin creates HR usernames/passwords
- Excel exports for employee work, attendance, and HR reports
- Database migration utility and the included SQLite database snapshot

WINDOWS / VS CODE QUICK START
-----------------------------
1. Extract this ZIP into a NEW folder.
2. Open that folder (the folder containing app.py) in VS Code.
3. Open PowerShell in that folder.
4. Create an environment (recommended):
      py -m venv .venv
      .\.venv\Scripts\Activate.ps1
5. Install dependencies:
      python -m pip install --upgrade pip
      python -m pip install -r requirements.txt
6. Back up attendance.db and secret.key before changing databases.
7. Run the database upgrade:
      python migrate_database.py
8. Start the website:
      python app.py
9. Open http://127.0.0.1:5000

LOGIN / ACCESS
--------------
Admin login: /admin/login
Employee login: /login
HR login: /hr/login

HR ACCOUNTS
-----------
Sign in as the MAIN ADMIN, open HR Login Accounts, and create an HR account.
The HR account uses its own username and password. There is no shared default HR
password. Give the credentials to the intended HR staff member securely.

IMPORTANT DATABASE NOTES
------------------------
- The included attendance.db is a project snapshot. If you already have newer
  attendance.db records on your PC, do NOT overwrite them with this snapshot.
- Copy your current attendance.db and secret.key into this project folder, then
  run migrate_database.py to add the newer tables while retaining existing data.
- Keep secret.key private and backed up. Changing it can invalidate existing
  sessions and encrypted stored values.
- The included SQLite database is suitable for local testing. For a live
  multi-user deployment, configure persistent storage (DATA_DIR) and a secure
  SECRET_KEY environment variable. Do not rely on an ephemeral hosting disk.

TESTING STATUS
--------------
Source files were syntax-compiled, Jinja templates parsed, ZIP integrity and
SQLite integrity checked during packaging. Full browser/camera/live deployment
workflow testing must still be performed on the target computer and hardware.


=====================================================================
NEW IN THIS VERSION: SALARIES + SEPARATE HR PORTAL
=====================================================================

1) ADMIN PAYS SALARIES  (Admin panel -> "Salaries & Payroll")
   - "Set employee salaries": monthly salary + allowance for every employee.
   - "Generate / refresh payroll": creates the month's payroll for everyone.
   - Bonus and "other deduction" can be typed per employee.
   - "Pay" (one employee) or "Pay everyone" (all pending). Paid rows are locked;
     "Re-open" unlocks them. "Hold" keeps one salary back.
   - Employee gets a notification when paid and sees a payslip at "My Salary".

2) LEAVE -> SALARY DEDUCTION  (admin's choice, never automatic)
   - Admin > Leave Management: when approving leave the admin can type the
     deduction in Rs (a suggestion = monthly salary / 30 x days is shown, but
     the admin may enter ANY amount, including 0).
   - Already-approved leave also has a "Save deduction" box.
   - The amount flows into that month's payroll automatically. In the salary
     page the admin can still change each leave's deduction before paying.
   - Employee sees the deduction on "My Leaves" and "My Salary".
   - If HR approved the leave, the admin gets a notification to decide the deduction.

3) SEPARATE HR PORTAL  (/hr/login - own login, own layout, own menu)
   Admin creates HR accounts: Admin > HR Login Accounts (main admin only;
   can also reset an HR password or disable the account instantly).
   HR can use:
     Dashboard (today's attendance, pending items, expiring documents)
     Employees (search + HR profile: designation, code, type, manager, joining date)
     Leave Requests (approve/reject, yearly allowance)  - cannot set deductions
     Attendance (date/employee/department filters + Excel)
     Performance reviews, Projects, Shifts, Documents (expiry alerts)
     Expense Claims (approve/reject), Payroll (VIEW ONLY)
     Recruitment (job openings + candidate pipeline; "Hired" alerts the admin)
     Employee Messages sent to HR (reply), HR Report (Excel), My Account (password)
   HR can NOT: manage admins, change settings, approve registrations, delete
   employees, reset passwords/face data, see backup/dataset/audit log, set
   salaries or deductions, or pay salaries. Every HR change is written to the
   main admin's Activity Log as "HR:<name>".

4) HOW THE PORTALS CONNECT
   Employee requests leave -> HR reviews -> admin decides deduction -> payroll -> employee payslip
   Employee expense claim  -> HR approves -> admin told to reimburse -> employee notified
   HR review / shift / project / document -> instantly visible in the employee's "My HR Workspace"
   Employee message to "HR" -> appears in the HR portal -> HR reply reaches the employee

UPGRADING
   Your existing attendance.db is NOT touched by this ZIP's code changes. On first
   start (or `python migrate_database.py`) new tables/columns are added without
   deleting any data. Back up attendance.db + secret.key first, as always.


ENTERPRISE UPGRADE (NOVEMBER 2026)
---------------------------------
- Admin creates employee accounts; public employee self-registration is disabled.
- Employee changes password from their own Profile page.
- Each employee must upload a profile picture and enroll 50 real camera face samples before approval.
- Admin can generate 50 demo employee accounts and download their credentials CSV.
- Admin can message one employee, a department, or all approved employees. Inbox conversations are separated per employee and show profile pictures.
- Improved leave management, department cards, employee messages and attendance exports.
- Attendance Excel exports include a full all-employees workbook; employees can download their own monthly attendance workbook.
- Certificates are restricted to approved employees and display decorative typed signatures for Muzammil Khalid and HR Manager.
- A separate employee_archive.db stores employee HR/attendance records before offboarding deletion. Daily live DB snapshots and employee archive records have a 10-year retention period.
- Admin ZIP export contains live database, employee archive database, face dataset archive, employee records, attendance, leave, work, and related HR records.

IMPORTANT: Do not treat demo accounts as real employees. Each demo account needs a real employee profile photo and 50 real face samples. Distribute the downloaded credentials CSV securely and delete local copies when no longer needed.
