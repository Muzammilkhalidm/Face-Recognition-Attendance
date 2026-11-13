"""hrcore.py - logic shared by the Admin, HR and Employee portals.

Keeps salary / leave-deduction rules in ONE place so the three portals always agree.

Rules
-----
* Only the ADMIN sets salaries, decides leave deductions and pays salaries.
* HR can approve / reject leave, but HR never sets money. When HR approves a leave the admin is
  notified so the admin can decide how much (if anything) to deduct from that employee's salary.
* payroll.deduction  =  payroll.leave_deduction (sum of the admin's per-leave deductions for that month)
                      + payroll.other_deduction  (anything else the admin types in)
* Paid payroll rows are locked. The admin can re-open them.
"""
import sqlite3

import database as db

DAYS_IN_MONTH_FOR_RATE = 30          # daily rate suggestion = monthly salary / 30 (admin may change it freely)


# ------------------------------------------------------------------ helpers
def money(v):
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def clean_amount(raw, maximum=100_000_000):
    """Parse an amount typed by a user. Returns None when it is invalid or negative."""
    try:
        v = float(str(raw).replace(",", "").strip() or 0)
    except ValueError:
        return None
    if v < 0 or v > maximum or v != v:
        return None
    return round(v, 2)


def valid_month(m):
    return isinstance(m, str) and len(m) == 7 and m[4] == "-" and m[:4].isdigit() and m[5:].isdigit() and 1 <= int(m[5:]) <= 12


def current_month():
    return db.now().strftime("%Y-%m")


def net_salary(row):
    return round(money(row["basic"]) + money(row["allowance"]) + money(row["bonus"]) - money(row["deduction"]), 2)


def suggested_leave_deduction(base_salary, units):
    """A SUGGESTION only (monthly salary / 30 x leave days). The admin decides the real amount."""
    return round(money(base_salary) / DAYS_IN_MONTH_FOR_RATE * float(units or 0), 2)


# ------------------------------------------------------------------ leave review (used by admin AND hr)
def review_leave(c, request_id, decision, note, reviewer_label, *, deduction=None, by_hr=False):
    """Approve / reject a pending leave request.

    deduction: amount the ADMIN wants to cut from salary for this leave (None = leave it as it is).
    by_hr: True when HR is reviewing; the admin is then told to decide the salary deduction.
    Returns (ok, message).
    """
    r = c.execute("SELECT l.*, e.full_name FROM leave_requests l JOIN employees e ON e.id=l.employee_id WHERE l.id=?",
                  (request_id,)).fetchone()
    if not r or r["status"] != "pending" or decision not in ("approved", "rejected"):
        return False, "That leave request was already reviewed or does not exist."
    c.execute("INSERT OR IGNORE INTO leave_balances(employee_id) VALUES (?)", (r["employee_id"],))
    if decision == "approved":
        c.execute("UPDATE leave_balances SET annual_used=annual_used+? WHERE employee_id=?", (r["units"], r["employee_id"]))
    c.execute("UPDATE leave_requests SET status=?, reviewed_by=?, admin_note=? WHERE id=?",
              (decision, reviewer_label, (note or "")[:300], request_id))
    msg = f"Leave {decision}."
    if decision == "approved" and deduction is not None and not by_hr:
        ok, m = set_leave_deduction(c, request_id, deduction, reviewer_label)
        msg += " " + m if m else ""
    body = f"{r['leave_type']} on {r['leave_date']}" + (f" - {note}" if note else "")
    db.notify(r["employee_id"], "leave", f"Your leave request was {decision}", body, "/leaves", request_id, conn=c)
    if by_hr:
        db.admin_notify("hr", f"HR {decision} a leave: {r['full_name']}",
                        f"{r['leave_type']} on {r['leave_date']}. "
                        + ("Set the salary deduction (if any) in Leave Management." if decision == "approved" else ""),
                        "/admin/leaves", request_id, conn=c)
    return True, msg


def set_leave_deduction(c, request_id, amount, admin_label, note=""):
    """ADMIN ONLY. Sets how much salary is cut for one approved leave, then refreshes that month's payroll."""
    r = c.execute("SELECT * FROM leave_requests WHERE id=?", (request_id,)).fetchone()
    if not r or r["status"] != "approved":
        return False, "Salary deduction can only be set on approved leave."
    amt = clean_amount(amount)
    if amt is None:
        return False, "Enter a valid deduction amount (0 or more)."
    c.execute("UPDATE leave_requests SET salary_deduction=?, deduction_by=?, deduction_note=? WHERE id=?",
              (amt, admin_label, (note or "")[:200], request_id))
    month = r["leave_date"][:7]
    p = c.execute("SELECT status FROM payroll WHERE employee_id=? AND pay_month=?", (r["employee_id"], month)).fetchone()
    if p and p["status"] == "Paid":
        return True, f"Saved, but the {month} salary is already paid so this deduction will NOT change it (re-open the payroll row if needed)."
    sync_payroll_row(c, r["employee_id"], month)
    if amt > 0:
        db.notify(r["employee_id"], "salary", "Salary deduction for leave",
                  f"Rs {amt:,.0f} will be deducted from your {month} salary for leave on {r['leave_date']}.",
                  "/my-salary", request_id, conn=c)
    return True, f"Deduction of Rs {amt:,.0f} saved."


# ------------------------------------------------------------------ payroll
def leave_deduction_total(c, emp_id, month):
    row = c.execute("SELECT COALESCE(SUM(salary_deduction),0) FROM leave_requests "
                    "WHERE employee_id=? AND status='approved' AND substr(leave_date,1,7)=?", (emp_id, month)).fetchone()
    return money(row[0])


def sync_payroll_row(c, emp_id, month):
    """Recalculate leave/total deduction of a NOT-yet-paid payroll row from the leave records."""
    p = c.execute("SELECT * FROM payroll WHERE employee_id=? AND pay_month=?", (emp_id, month)).fetchone()
    if not p or p["status"] == "Paid":
        return
    leave_d = leave_deduction_total(c, emp_id, month)
    other = money(p["other_deduction"])
    c.execute("UPDATE payroll SET leave_deduction=?, deduction=? WHERE id=?", (leave_d, round(leave_d + other, 2), p["id"]))


def generate_payroll(c, month, admin_label):
    """Create (or refresh) the month's payroll for EVERY approved employee that has a salary set.
    Paid rows are never touched. Returns (created, refreshed, skipped_without_salary)."""
    created = refreshed = skipped = 0
    emps = c.execute("SELECT e.id, COALESCE(s.base_salary,0) base, COALESCE(s.allowance,0) allow "
                     "FROM employees e LEFT JOIN salary_profiles s ON s.employee_id=e.id WHERE e.status='approved'").fetchall()
    for e in emps:
        if e["base"] <= 0:
            skipped += 1
            continue
        leave_d = leave_deduction_total(c, e["id"], month)
        p = c.execute("SELECT * FROM payroll WHERE employee_id=? AND pay_month=?", (e["id"], month)).fetchone()
        if not p:
            c.execute("INSERT INTO payroll(employee_id,pay_month,basic,allowance,bonus,deduction,leave_deduction,other_deduction,"
                      "status,note,created_at) VALUES (?,?,?,?,0,?,?,0,'Pending','',?)",
                      (e["id"], month, e["base"], e["allow"], leave_d, leave_d, db.now().strftime(db.DT_FMT)))
            created += 1
        elif p["status"] != "Paid":
            other = money(p["other_deduction"])
            c.execute("UPDATE payroll SET basic=?, allowance=?, leave_deduction=?, deduction=? WHERE id=?",
                      (e["base"], e["allow"], leave_d, round(leave_d + other, 2), p["id"]))
            refreshed += 1
    return created, refreshed, skipped


def save_payroll_row(c, payroll_id, *, bonus, other_deduction, note, leave_amounts, admin_label):
    """Admin edit of one unpaid payroll row. leave_amounts = {leave_request_id: amount}."""
    p = c.execute("SELECT * FROM payroll WHERE id=?", (payroll_id,)).fetchone()
    if not p:
        return False, "Payroll row not found."
    if p["status"] == "Paid":
        return False, "This salary is already paid. Re-open it first to change it."
    bonus, other = clean_amount(bonus), clean_amount(other_deduction)
    if bonus is None or other is None:
        return False, "Bonus and deduction must be valid amounts (0 or more)."
    for lid, amt in (leave_amounts or {}).items():
        lr = c.execute("SELECT employee_id, leave_date, status FROM leave_requests WHERE id=?", (lid,)).fetchone()
        if not lr or lr["employee_id"] != p["employee_id"] or lr["leave_date"][:7] != p["pay_month"] or lr["status"] != "approved":
            continue
        a = clean_amount(amt)
        if a is None:
            return False, "A leave deduction amount is invalid."
        c.execute("UPDATE leave_requests SET salary_deduction=?, deduction_by=? WHERE id=?", (a, admin_label, lid))
    leave_d = leave_deduction_total(c, p["employee_id"], p["pay_month"])
    c.execute("UPDATE payroll SET bonus=?, other_deduction=?, leave_deduction=?, deduction=?, note=? WHERE id=?",
              (bonus, other, leave_d, round(leave_d + other, 2), (note or "")[:300], payroll_id))
    return True, "Salary row saved."


def pay_payroll_row(c, payroll_id, admin_label):
    p = c.execute("SELECT p.*, e.full_name FROM payroll p JOIN employees e ON e.id=p.employee_id WHERE p.id=?", (payroll_id,)).fetchone()
    if not p or p["status"] == "Paid":
        return False
    net = net_salary(p)
    c.execute("UPDATE payroll SET status='Paid', paid_at=?, paid_by=? WHERE id=?",
              (db.now().strftime(db.DT_FMT), admin_label, payroll_id))
    body = f"Net Rs {net:,.0f}"
    if money(p["deduction"]) > 0:
        body += f" (after deduction of Rs {money(p['deduction']):,.0f})"
    db.notify(p["employee_id"], "salary", f"Salary paid for {p['pay_month']}", body, "/my-salary", payroll_id, conn=c)
    return True


def pay_all(c, month, admin_label):
    ids = [r[0] for r in c.execute("SELECT id FROM payroll WHERE pay_month=? AND status='Pending'", (month,))]
    return sum(1 for i in ids if pay_payroll_row(c, i, admin_label))


def reopen_payroll_row(c, payroll_id):
    c.execute("UPDATE payroll SET status='Pending', paid_at='', paid_by='' WHERE id=? AND status='Paid'", (payroll_id,))


def month_leaves(c, emp_id, month):
    return [dict(r) for r in c.execute(
        "SELECT id, leave_date, leave_type, units, reason, salary_deduction FROM leave_requests "
        "WHERE employee_id=? AND status='approved' AND substr(leave_date,1,7)=? ORDER BY leave_date", (emp_id, month))]


def payroll_overview(c, month):
    """Rows for the admin salary page (and the read-only HR payroll view)."""
    out = []
    rows = c.execute("SELECT p.*, e.full_name, e.username, e.designation FROM payroll p JOIN employees e ON e.id=p.employee_id "
                     "WHERE p.pay_month=? ORDER BY e.full_name", (month,)).fetchall()
    for r in rows:
        d = dict(r)
        d["net"] = net_salary(r)
        d["leaves"] = month_leaves(c, r["employee_id"], month)
        d["leave_days"] = sum(l["units"] for l in d["leaves"])
        d["suggested"] = suggested_leave_deduction(r["basic"], d["leave_days"])
        out.append(d)
    return out


def payroll_totals(rows):
    return {
        "count": len(rows),
        "gross": round(sum(money(r["basic"]) + money(r["allowance"]) + money(r["bonus"]) for r in rows), 2),
        "deduction": round(sum(money(r["deduction"]) for r in rows), 2),
        "net": round(sum(r["net"] for r in rows), 2),
        "paid": sum(1 for r in rows if r["status"] == "Paid"),
        "pending": sum(1 for r in rows if r["status"] != "Paid"),
    }


def safe_count(c, sql, params=()):
    try:
        return c.execute(sql, params).fetchone()[0]
    except sqlite3.OperationalError:
        return 0
