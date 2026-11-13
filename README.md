# CyberAttend v2 — Face Recognition Attendance (Admin Panel + Employee Panel)

Do bilkul alag panels:

| Panel | URL | Login |
|---|---|---|
| **Admin** | `/admin/login` | username + password |
| **Employee** | `/login` | username + password **+ face recognition** |

## Employee ka poora flow

1. Admin creates the employee account from **Admin → Employees → Create employee account**. Public self-registration is disabled.
2. **Face registration** — camera automatically 50 real samples leta hai for each employee. Har sample `dataset/<id>/` mein save hota hai
   aur saath hi **`dataset_archive/dataset.zip`** mein pack hota rehta hai.
3. Employee uploads a compulsory profile picture on the approval-pending page.
4. **"Admin's approval pending"** screen — page khud update hota hai.
5. Admin **Approvals** page par face samples dekh kar *Approve* karta hai.
6. Employee `/login` → admin-provided username/password → **face scan** → Dashboard (face match na ho to login nahi hota).
7. Dashboard par **Check In (face scan)** → working timer chalta hai.
8. Kaam khatam karke **Check Out (face scan)** → working hours save. Agar required hours poore nahi hue to
   warning aati hai aur din **"Short Hours"** record hota hai (admin ko nazar aata hai).

## Department-wise access
Employee sirf **apni department** ki information dekh sakta hai (team members, department notices, attendance).
Doosri department ka URL kholne par `403` aata hai. IT Software ka employee sirf IT Software dekhta hai; baqi sab kuch sirf admin dekhta hai.

## Admin panel
Dashboard (aaj ki attendance + working hours, absent list, department chart) · Approvals (face samples ke saath) ·
Employees (admin-created accounts, 50 demo accounts, approve / revoke / delete) · Admins · Attendance reports (date, department, employee, status filter + **Excel**) ·
Departments · Announcements (sab ko ya sirf ek department ko) · Settings (required hours, shift time, late grace, password) ·
**Dataset ZIP** download.

## Admin roles (naya)
| Role | Kya kar sakta hai |
|---|---|
| **Main admin (muzammil)** | Sab kuch. **Sirf wohi** employees ko revoke/delete kar sakta hai, aur admins add/remove kar sakta hai. |
| **Admin (permanent)** | Approvals, attendance, departments, announcements, settings. **Kisi ko remove nahi kar sakta.** |
| **Temporary admin** | Permanent admin jaisa, lekin muqarrar waqt (1 / 3 / 7 / 30 din) ke baad access **khud band** ho jata hai. |

Admins page: `/admin/admins`. Main admin ko kabhi delete nahi kiya ja sakta. Purani `attendance.db` startup par khud migrate ho jati hai
(muzammil main admin ban jata hai). Pending employee accounts ko *Reject* karna har admin kar sakta hai; approved employee ko *Revoke* ya *Delete* sirf main admin.

## Chalane ka tareeqa
```bash
python -m venv venv
venv\Scripts\activate            # Linux/Mac: source venv/bin/activate
pip install -r requirements.txt
python app.py
```
Browser: `http://127.0.0.1:5000` (employee) · `http://127.0.0.1:5000/admin/login` (admin)

Default admin: **muzammil / muzammil182921** — pehli dafa login ke baad **Settings → Change password** zaroor karein.

> **Camera:** browser camera sirf **HTTPS** ya **localhost** par chalta hai. Phone se LAN par test karna ho to
> `pip install cryptography` karke `HTTPS=1 python app.py` chalayen (self-signed certificate, browser warning accept karein).
> Railway par HTTPS pehle se hota hai.

## Settings / Environment variables
| Variable | Matlab | Default |
|---|---|---|
| `DATA_DIR` | database + dataset + trainer ka folder (Railway Volume path) | project folder |
| `SECRET_KEY` | session key (na ho to `secret.key` file auto-ban jati hai) | auto |
| `APP_TIMEZONE` | attendance ka time zone | `Asia/Karachi` |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | pehli dafa admin banane ke liye | muzammil / muzammil182921 |
| `CONFIDENCE_THRESHOLD` | face match strictness (chota = strict) | `75` |
| `STREAK_NEEDED` | login/check-in ke liye lagatar kitne frames match hon | `3` |
| `SAMPLES_NEEDED` | registration par face samples | `50` |
| `SYNC_KEY` | `sync_dataset.py` ke liye | — |
| `COOKIE_SECURE=1` | HTTPS par secure cookies | off |

**Railway:** Volume mount karein aur `DATA_DIR` us path par set karein, warna redeploy par database/dataset ud jayega.
`Procfile` gunicorn ko **1 worker** par chalata hai (zaroori hai).

## Files
```
app.py            routes (admin, employee, face verify)       database.py   SQLite + attendance/hours logic
face_utils.py     detection, training, 1:1 verification, zip  templates/     public pages, admin/, employee/
static/           css (Inter font, blue/slate theme) + js/face.js (camera)                   dataset/      face samples (dataset/<id>/n.jpg)
dataset_archive/  dataset.zip (auto)                          trainer/      trainer.yml (LBPH model)
```

## Zaroori limitations (imandari se)
* Recognition **OpenCV LBPH** hai — chhoti company ke liye theek, lekin deep-learning jitni mazboot nahi. Roshni acchi rakhein.
* **Liveness detection nahi hai:** koi employee ki *photo/video* camera ke saamne rakh de to fool ho sakta hai.
  Is liye admin approval aur face + password dono rakhe gaye hain. Sakht security chahiye to ArcFace/MediaPipe + blink check add karein.
* Ek din mein **ek check-in aur ek check-out**; raat 12 baje ke baad wali shift support nahi.
* Agar koi check-out bhool jaye to wo din admin ko **"No Check-out"** dikhta hai.
# dl


## Attendance workflow additions
- Main admin can manage office locations at `/admin/locations`, set office latitude/longitude and a permitted radius, and assign an employee to one office. The browser must share location, and check-in is blocked outside that office's configured radius. Configure accurate coordinates before using attendance restrictions in production.
- Employees use **Work from Home** to submit a request; an admin can approve/reject it. Approved WFH check-in records the browser-provided coordinates (requires location permission).
- Employees can message HR, Admin or CEO from their portal; admins can review and reply under Employee Messages.
- Attendance exports include work mode and location. Use **Export Excel** for `.xlsx` or **Export CSV** for CSV. The workbook is generated from current attendance records by the application; no Excel upload is required.
- Dataset ZIP download is restricted to the main admin. Do not share the dataset because it contains biometric face samples.
- Login passwords are stored as hashes. The MAIN admin additionally sees an encrypted copy (see "Main admin full records" below).


## Employee profiles and work tracker
- Employees can update their contact email, phone, designation, profile photo and professional/social links from **Profile**.
- Approved employee profiles are visible to signed-in employees and admins through **Team Directory / People Directory**. Only approved employees are listed; passwords are never public or exported.
- Employees can submit their own work proposals at `/tasks`. These remain **Proposed** until an admin reviews them.
- Admins assign work, set target dates, approve/reject proposals and track progress at `/admin/tasks`.
- Employees can update task status (Assigned / In Progress / Completed) and leave progress notes.
- Admins can download a separate `employee_work_tracker.xlsx` report from the Work & Tasks page. It includes employee, department, task, assigned-by, due date, status and latest employee update.
- New profile columns and the task table are added through safe, additive SQLite migrations at startup. Existing employee/attendance records are preserved.
- Profile image uploads accept JPG, PNG or WEBP and are limited by the application's 4 MB request limit. Install requirements after updating so Pillow is available for image validation.


## OpenCV startup troubleshooting (Windows)
If startup reports `AttributeError: module 'cv2' has no attribute 'CascadeClassifier'`, remove conflicting OpenCV packages and reinstall the package declared for this project in the same Python environment:
```powershell
python -m pip uninstall opencv-python opencv-contrib-python opencv-python-headless opencv-contrib-python-headless -y
python -m pip install --no-cache-dir -r requirements.txt
python -c "import cv2; print(cv2.__file__); print(cv2.__version__); print(hasattr(cv2, 'CascadeClassifier'))"
```
The final line should print `True`. Also make sure the project folder does not contain a file or folder named `cv2.py` / `cv2` that shadows OpenCV.

Profile photos are stored under `DATA_DIR/profile_uploads` and served through an authenticated route, so approved employees and admins can view them while unauthenticated visitors cannot. Set `DATA_DIR` to a persistent writable volume in production.
# finaldl

# Finaldl



## Missed check-out warnings, notifications, nickname recovery, task sheet, main-admin records
- **Missed check-out rule:** if an employee is still checked in after midnight, they get a warning (1, 2, 3 ...). After the warning limit
  (Settings -> "Missed check-out warnings before Absent", default 3) the next missed check-out marks that day **Absent**.
  Checked every minute in the background. Old attendance rows from before this feature are never punished.
  The main admin can reset an employee's warnings on the employee page.
- **Notifications:** every admin announcement, newly assigned task, proposal decision, admin reply and warning opens a **popup with numbered
  items (1, 2, 3 ...)** and plays a sound. Sidebar items (Dashboard, Announcements, My Work & Tasks, Messages) show red number counters until the employee
  opens them. New items are marked "New".
- **Ringtone:** put your own file at `static/sounds/notify.mp3` (also accepted: `.ogg`, `.m4a`). If no file is there, the built-in `notify.wav` plays.
  Browsers only allow sound after the user has clicked/tapped the page once; the popup always shows, and the sound plays on the first click if it was blocked.
- **Forgot password:** sign-up asks for a **nickname**. `/forgot-password` accepts username + nickname and sets a new password
  (5 wrong tries = 15 min lock). Login still needs the face scan. Older employees can add a nickname in Profile.
- **Tasks sheet:** every task is kept in the database/Excel sheet. Employees download their own sheet (`/tasks.xlsx`) and can **Delete** finished tasks from the dashboard
  (delete only hides it there; the sheet keeps it). Admins can do the same on Work & Tasks.
- **Main admin full records** (`/admin/records`, + `full_employee_records.xlsx`): username, **password**, nickname, work, warnings, attendance, messages.
  Only the main admin can open it. Passwords are kept as an **encrypted copy** (needs `cryptography`, key derived from `SECRET_KEY` / `secret.key` - keep it safe and do not change it).
  Employees who already existed get their copy saved at their next login or password change.

## Face recognition rules (stricter) + extra features
- **Only enrolled faces match.** At every start the model is re-synced with the `dataset/` folder. Anybody whose samples are not in `dataset/` is never recognised (state `not_enrolled`). If `dataset/` is empty, the old `trainer.yml` is cleared automatically.
- **Strictness** is now 55 by default (was 75, too loose) and can be changed by any admin in **Settings -> Face match strictness** (lower = stricter).
- Repeated wrong-face attempts on an account send a **Security alert** popup to all admins.
- Main admin can **Re-register face** and **Reset password** for an employee (employee page).
- **Notifications** pages (history + mark all read) for employees and admins. **Holidays** (admin adds, employees are notified, nobody is listed absent on a holiday).
- **Activity Log** (main admin): who did what. **Database Backup** (main admin): downloads a copy of the SQLite database - keep it private.


## Advanced HR profile upgrade (v3)
- Employee profiles now support employee code, employment type, work location, reporting manager, joining date, education, skills and a professional bio.
- The employee directory search also matches employee code and skills.
- Admin employee details show the expanded HR profile.
- Admins can export the complete employee directory to CSV from **Admin → Employees**.
- These are safe additive database migrations; existing employees and attendance history are preserved.


## Separate HR portal and access rules

- HR sign-in: `/hr/login` (short entry: `/hr`).
- Main HR account is created automatically: username `muzi`; initial password `muzammil182921`. Change it after first login.
- HR has a separate dashboard and session; employee accounts cannot open HR pages.
- The old employee `/my-hr` URL redirects to the employee dashboard instead of returning 404. Employees can view only their own salary at `/my-salary`.
- Admin-only HR management, HR account control and salary setup remain in the Admin panel.


## Enterprise HRMS upgrade (November 2026)
- Employee self-registration is disabled. Admin provisions each employee username and initial password. Employees can change their own password from Profile.
- Employee profile picture is compulsory before admin approval. Login after approval requires password plus face verification. Each employee enrolls **50 real camera-captured face samples**; sample images are not fabricated for demo accounts.
- Admin → Employees includes a form to create an account and a button to generate 50 demo employee accounts. The button downloads a CSV containing temporary usernames/passwords; store it securely and distribute credentials privately.
- Admin messages support one employee, one department, or all approved employees. The inbox is grouped into separate employee conversations with profile photos.
- Attendance supports individual Excel exports and a full all-employees attendance workbook. Full Employee ZIP contains the live database, face dataset archive, employee/profile records, attendance, leave, work, and other HR module data.
- A separate `employee_archive.db` stores a full employee record snapshot before an employee is deleted/offboarded. Daily live-database snapshots and archived employee records are retained for 10 years (3652 days), then automatically pruned. Keep `DATA_DIR` on persistent storage and include it in your own off-site backup plan.
- Face biometric samples are removed from the active recognition dataset when an employee is deleted; the archived employee record keeps HR/attendance history, not biometric templates.
- Certificates can only be issued to approved employees. Printed certificates show a decorative typed “Muzammil Khalid” signature and a generic “HR Manager” signatory label.

### New admin downloads
- `/admin/employees/full-records.zip` — full employee records bundle.
- `/admin/archive.db` — 10-year employee archive database.
- `/admin/long-term-backups.zip` — archive DB plus daily live DB snapshots.
- `/admin/attendance/all.xlsx` — all employees' complete attendance history.
- `/my-attendance.xlsx` — logged-in employee's own attendance for the selected month.

### Important first run
Install `requirements.txt`, back up your current `attendance.db` and `secret.key`, then run `python app.py`. The new database columns and the archive database are created automatically. Browser/camera and full workflow testing must be completed on the machine where the project is deployed.


## Local run with the bundled SMSGate service

See `../README_START_HERE.txt` and `../START_HERE_WINDOWS.bat`. CyberAttend runs on port 5000; SMSGate runs on port 8080. The SMSGate root URL redirects to `/admin`. Actual SMS delivery requires an online Android SIM gateway; queueing alone cannot send an SMS. Configure `SMSGATE_URL`, `SMSGATE_SID`, and `SMSGATE_TOKEN` in the CyberAttend process environment.

### If your existing admin password still does not match
If you are keeping your own existing `attendance.db` rather than using the database included in this ZIP,
open PowerShell in the `cyberattend` folder and run `py reset_admin_password.py`. This resets only the
main admin password to `muzammil182921` and keeps employee and attendance records. Change the password
immediately after logging in.
#   F a c e - R e c o g n i t i o n - A t t e n d a n c e  
 