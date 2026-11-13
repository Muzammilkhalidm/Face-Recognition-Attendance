"""Enterprise additions for CyberAttend HRMS.
Includes admin-created accounts, demo roster generator, admin broadcasts,
employee archive/retention and complete export bundles.
"""
import os, io, csv, json, sqlite3, zipfile, tempfile, secrets, string, shutil, threading, time
from datetime import timedelta
from functools import wraps
from flask import request, redirect, url_for, flash, abort, send_file, Response, g, session, render_template
from werkzeug.security import generate_password_hash
import database as db

ARCHIVE_DB = os.path.join(db.DATA_DIR, 'employee_archive.db')
BACKUP_DIR = os.path.join(db.DATA_DIR, 'long_term_backups')
os.makedirs(BACKUP_DIR, exist_ok=True)


def _archive_conn():
    c = sqlite3.connect(ARCHIVE_DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('''CREATE TABLE IF NOT EXISTS archived_employees (
        archive_id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL,
        username TEXT NOT NULL, full_name TEXT NOT NULL, archived_at TEXT NOT NULL,
        retention_until TEXT NOT NULL, record_json TEXT NOT NULL)''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_archive_until ON archived_employees(retention_until)')
    c.commit()
    return c


def archive_employee(emp_id):
    """Snapshot employee and dependent history before the live employee is removed."""
    with db.conn_ctx() as live:
        emp = live.execute('SELECT * FROM employees WHERE id=?', (emp_id,)).fetchone()
        if not emp:
            return False
        record = {'employee': dict(emp)}
        tables = ['attendance','leave_balances','leave_requests','messages','tasks','warnings','certificates',
                  'notifications','payroll','performance_reviews','projects','expenses','employee_documents','employee_shifts','salary_profiles','wfh_requests']
        for table in tables:
            try:
                record[table] = [dict(r) for r in live.execute(f'SELECT * FROM {table} WHERE employee_id=?', (emp_id,))]
            except sqlite3.OperationalError:
                record[table] = []
        try:
            record['department'] = dict(live.execute('SELECT * FROM departments WHERE id=?',(emp['department_id'],)).fetchone() or {})
        except Exception:
            record['department'] = {}
    now = db.now()
    until = now + timedelta(days=3652)
    c = _archive_conn()
    try:
        c.execute('INSERT INTO archived_employees(employee_id,username,full_name,archived_at,retention_until,record_json) VALUES(?,?,?,?,?,?)',
                  (emp_id, emp['username'], emp['full_name'], now.strftime(db.DT_FMT), until.strftime(db.DT_FMT), json.dumps(record, default=str)))
        c.commit()
    finally: c.close()
    return True


def snapshot_live_database():
    """Daily immutable SQLite snapshots; retain ten years (3652 days)."""
    stamp = db.now().strftime('%Y%m%d_%H%M%S')
    dest = os.path.join(BACKUP_DIR, f'hrms_{stamp}.db')
    src = sqlite3.connect(db.DB_PATH, timeout=30)
    out = sqlite3.connect(dest)
    try: src.backup(out)
    finally: out.close(); src.close()
    cutoff = db.now() - timedelta(days=3652)
    for name in os.listdir(BACKUP_DIR):
        path = os.path.join(BACKUP_DIR, name)
        try:
            stamp_s = name[5:20]
            dt = __import__('datetime').datetime.strptime(stamp_s, '%Y%m%d_%H%M%S')
            if dt < cutoff: os.remove(path)
        except Exception: pass
    c = _archive_conn()
    try:
        c.execute('DELETE FROM archived_employees WHERE retention_until < ?', (db.now().strftime(db.DT_FMT),)); c.commit()
    finally: c.close()
    return dest


def _admin_guard():
    if session.get('role') != 'admin': abort(403)
    admin = db.get_admin(session.get('admin_id'))
    if not admin or db.admin_expired(admin): abort(403)
    g.admin = admin


def register(app):
    # Additive migration: message direction is explicit; old messages remain employee-originated.
    with db.conn_ctx() as c:
        cols = {r[1] for r in c.execute('PRAGMA table_info(messages)')}
        if 'sender_role' not in cols: c.execute("ALTER TABLE messages ADD COLUMN sender_role TEXT DEFAULT 'employee'")
        if 'sender_admin' not in cols: c.execute("ALTER TABLE messages ADD COLUMN sender_admin TEXT DEFAULT ''")
        if 'department_id' not in cols: c.execute('ALTER TABLE messages ADD COLUMN department_id INTEGER')
        c.execute("UPDATE messages SET sender_role='employee' WHERE sender_role IS NULL OR sender_role=''")

    @app.route('/admin/employee/create', methods=['POST'])
    @app.route('/admin/employees/create', methods=['POST'])
    def _create_employee_account():
        _admin_guard()
        name = request.form.get('full_name','').strip()[:120]
        username = request.form.get('username','').strip()[:60]
        password = request.form.get('password','')
        email = request.form.get('email','').strip()[:254]
        phone = request.form.get('phone','').strip()[:20]
        phone_digits = ''.join(ch for ch in phone if ch.isdigit())
        designation = request.form.get('designation','').strip()[:100]
        dept = request.form.get('department_id', type=int)
        if len(phone_digits) < 10 or len(phone_digits) > 15:
            flash('Enter a valid phone number (10 to 15 digits). It is needed for password recovery.','error')
        elif not name or len(username)<3 or not username.replace('_','').replace('.','').isalnum() or len(password)<8 or not db.get_department(dept or 0):
            flash('Enter employee name, valid username, password (8+ characters), and department.','error')
        else:
            # Names are intentionally NOT unique: multiple employees may share the same name.
            # Check phone numbers only against currently existing employee records.
            # Deleted/archived employees are stored separately and are deliberately ignored.
            with db.conn_ctx() as _c:
                active_phones = _c.execute("SELECT phone FROM employees WHERE COALESCE(phone,'') <> ''").fetchall()
            duplicate_phone = any(
                ''.join(ch for ch in (row['phone'] or '') if ch.isdigit()) == phone_digits
                for row in active_phones
            )
            if duplicate_phone:
                flash('This phone number is already registered to an existing employee.','error')
                return redirect(url_for('admin_employees'))
            if db.username_taken(username):
                flash('That username is already in use. Please choose a different username.','error')
                return redirect(url_for('admin_employees'))
            emp_id = db.create_employee(name,username,password,dept,email,designation,'')
            with db.conn_ctx() as _c:
                _c.execute('UPDATE employees SET phone=? WHERE id=?', (phone, emp_id))
            flash(f'Employee account created. Username: {username}. Give the password privately. Employee must upload a profile picture and enroll 50 face samples.','success')
        return redirect(url_for('admin_employees'))

    @app.route('/admin/employees/demo-50', methods=['POST'])
    def _generate_demo_employees():
        _admin_guard()
        depts = db.list_departments()
        if not depts:
            flash('Create at least one department first.','error'); return redirect(url_for('admin_employees'))
        first = ['Ayaan','Hania','Zain','Maira','Hamza','Areeba','Daniyal','Noor','Saad','Eman','Rayyan','Anaya','Huzaifa','Mishal','Arham','Laiba','Talha','Iqra','Rafay','Zoya','Abdullah','Mehak','Usman','Alina','Shayan']
        last = ['Khan','Ahmed','Malik','Sheikh','Butt','Raza','Ali','Iqbal','Shah','Akhtar']
        creds=[]; created=0
        for i in range(50):
            full = f'{first[i%len(first)]} {last[(i*7)%len(last)]}'
            username = f'demo_emp_{i+1:03d}'
            if db.username_taken(username): continue
            password = 'Demo@' + secrets.token_urlsafe(8)
            dept = depts[i % len(depts)]['id']
            db.create_employee(full,username,password,dept,f'{username}@example.test','Demo Employee','')
            creds.append((i+1,full,username,password,depts[i%len(depts)]['name']))
            created += 1
        output=io.StringIO(); w=csv.writer(output); w.writerow(['#','Employee','Username','Temporary Password','Department']); w.writerows(creds)
        data=('\ufeff'+output.getvalue()).encode('utf-8')
        response=Response(data,mimetype='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename=demo_employee_credentials.csv'})
        response.headers['X-Demo-Employees-Created']=str(created)
        return response

    @app.route('/admin/employees/full-records.zip')
    def _full_records_zip():
        _admin_guard()
        mem=io.BytesIO()
        with zipfile.ZipFile(mem,'w',zipfile.ZIP_DEFLATED) as z:
            fd,tmp=tempfile.mkstemp(suffix='.db'); os.close(fd)
            try:
                db.backup_database(tmp); z.write(tmp,'database/attendance_live.db')
            finally:
                try: os.remove(tmp)
                except OSError: pass
            z.write(fu_zip_path(),'dataset/dataset.zip') if os.path.exists(fu_zip_path()) else None
            # employee directory and all attendance rows
            with db.conn_ctx() as c:
                emps=[dict(x) for x in c.execute('SELECT e.*,d.name department_name FROM employees e LEFT JOIN departments d ON d.id=e.department_id')]
                att=[dict(x) for x in c.execute('SELECT a.*,e.full_name,e.username,d.name department_name FROM attendance a JOIN employees e ON e.id=a.employee_id LEFT JOIN departments d ON d.id=e.department_id ORDER BY a.work_date DESC')]
                leaves=[dict(x) for x in c.execute('SELECT l.*,e.full_name,e.username FROM leave_requests l JOIN employees e ON e.id=l.employee_id ORDER BY l.leave_date DESC')]
                tasks=[dict(x) for x in c.execute('SELECT t.*,e.full_name,e.username FROM tasks t JOIN employees e ON e.id=t.employee_id ORDER BY t.created_at DESC')]
            z.writestr('records/employees.json',json.dumps(emps,default=str,indent=2))
            z.writestr('records/attendance.json',json.dumps(att,default=str,indent=2))
            z.writestr('records/leave_history.json',json.dumps(leaves,default=str,indent=2))
            z.writestr('records/work_history.json',json.dumps(tasks,default=str,indent=2))
            # Full JSON snapshot of every employee-linked module (payroll, messages, warnings, projects, etc.).
            all_records = {}
            linked_tables = ['employees','attendance','leave_balances','leave_requests','messages','tasks','warnings','certificates','notifications','payroll','performance_reviews','projects','expenses','employee_documents','employee_shifts','salary_profiles','wfh_requests']
            with db.conn_ctx() as c:
                for table in linked_tables:
                    try: all_records[table] = [dict(row) for row in c.execute(f'SELECT * FROM {table}')]
                    except sqlite3.OperationalError: all_records[table] = []
            z.writestr('records/all_employee_modules.json',json.dumps(all_records,default=str,indent=2))
            if os.path.exists(ARCHIVE_DB): z.write(ARCHIVE_DB,'database/employee_archive.db')
            for root,dirs,files in os.walk(os.path.join(db.DATA_DIR,'archived_faces')):
                for f in files: z.write(os.path.join(root,f),'archived_faces/'+os.path.basename(root)+'/'+f)
            for root,dirs,files in os.walk(os.path.join(db.DATA_DIR,'profile_uploads')):
                for f in files: z.write(os.path.join(root,f),f'profile_pictures/{f}')
        mem.seek(0)
        return send_file(mem,mimetype='application/zip',as_attachment=True,download_name='CyberAttend_Full_Employee_Records.zip')

    def fu_zip_path():
        import face_utils
        return face_utils.ZIP_PATH

    @app.route('/admin/archive.db')
    def _archive_database_download():
        _admin_guard()
        if not os.path.exists(ARCHIVE_DB):
            c=_archive_conn(); c.close()
        return send_file(ARCHIVE_DB,as_attachment=True,download_name='employee_archive_10_years.db')

    @app.route('/admin/long-term-backups.zip')
    def _long_term_backups_zip():
        _admin_guard()
        mem=io.BytesIO()
        with zipfile.ZipFile(mem,'w',zipfile.ZIP_DEFLATED) as z:
            if os.path.exists(ARCHIVE_DB): z.write(ARCHIVE_DB,'employee_archive.db')
            for root,dirs,files in os.walk(os.path.join(db.DATA_DIR,'archived_faces')):
                for f in files: z.write(os.path.join(root,f),'archived_faces/'+os.path.basename(root)+'/'+f)
            for root,dirs,files in os.walk(BACKUP_DIR):
                for f in files: z.write(os.path.join(root,f),f'live_snapshots/{f}')
        mem.seek(0); return send_file(mem,mimetype='application/zip',as_attachment=True,download_name='CyberAttend_10_Year_Backups.zip')

    # Initial snapshot; daily snapshots thereafter. The archive is independent from the live DB.
    try: snapshot_live_database()
    except Exception: app.logger.exception('Initial long-term backup failed')
    def backup_worker():
        while True:
            time.sleep(86400)
            try: snapshot_live_database()
            except Exception: app.logger.exception('Scheduled HRMS backup failed')
    threading.Thread(target=backup_worker,daemon=True,name='hrms-long-term-backup').start()
