import inspect
import re
import sqlite3
import hashlib
import hmac
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
 
import flet as ft
 
APP_NAME = "Krause Gym Management System"
DB_PATH = Path(__file__).resolve().parent / "gym_management.db"
PLANS = {
    "Day Pass (Walk-in)": {"price": 50.0, "days": 1},
    "Weekly": {"price": 250.0, "days": 7},
    "Monthly": {"price": 800.0, "days": 30},
    "Quarterly": {"price": 2000.0, "days": 90},
}
PAYMENT_METHODS = ["Cash", "GCash", "Maya", "Bank Transfer"]
EXTENSIONS = ["None", "Jr.", "Sr.", "II", "III", "IV", "V"]
IDLE_LOGOUT_SECONDS = 10 * 60  # signed out automatically after this much inactivity
 
# Shared sizes for the Payments page so every tab, filter and card lines up.
TAB_W, TAB_H = 210, 44
FIELD_W, FIELD_H = 190, 48
CARD_W = 200
 
# Small compatibility shims so the file runs on current Flet releases.
Btn = getattr(ft, "Button", None) or ft.ElevatedButton
Opt = getattr(ft, "DropdownOption", None) or ft.dropdown.Option
 
 
def _pad(name):
    """Padding helpers that work on both old (ft.padding.*) and new (ft.Padding.*) Flet."""
    cls = getattr(ft, "Padding", None)
    if cls is not None and hasattr(cls, name):
        return getattr(cls, name)
    return getattr(ft.padding, name)
 
 
pad_all = _pad("all")
pad_only = _pad("only")
pad_symmetric = _pad("symmetric")
 
 
def field_look(**overrides):
    """One look for every text field and dropdown in the app."""
    look = dict(
        height=FIELD_H, dense=False, text_size=13,
        content_padding=pad_symmetric(horizontal=12, vertical=12),
        border=ft.OutlineInputBorder(border_radius=8, side=ft.BorderSide(1, "#71717A")),
    )
    look.update(overrides)
    return look
 
 
def _left_alignment():
    """Return a left-center alignment supported by old and new Flet APIs."""
    alignment_cls = getattr(ft, "Alignment", None)
    if alignment_cls is not None:
        try:
            return alignment_cls(-1, 0)
        except Exception:
            pass
    alignment_module = getattr(ft, "alignment", None)
    if alignment_module is not None and hasattr(alignment_module, "center_left"):
        return alignment_module.center_left
    return None
 
 
LEFT_ALIGNMENT = _left_alignment()
 
 
def summarize_payment_methods(totals):
    """Return compact payment summary cards while retaining method-level records."""
    grouped = [
        ("Filtered Cash", float(totals.get("Cash", 0.0))),
        ("E-Wallet", sum(float(totals.get(method, 0.0))
                        for method in ("GCash", "Maya", "Bank Transfer"))),
    ]
    other_methods = [name for name in totals
                    if name not in ("Cash", "GCash", "Maya", "Bank Transfer")]
    grouped.extend((f"Filtered {method}", float(totals.get(method, 0.0)))
                for method in other_methods)
    return grouped
 
 
def receipt_number(payment_date, payment_id):
    day = date.fromisoformat(str(payment_date)[:10]).strftime("%Y%m%d")
    return f"KGM-{day}-{int(payment_id):04d}"
 
 
def _middle_initial(middle):
    middle = (middle or "").strip()
    return f"{middle[0].upper()}." if middle else ""
 
 
def build_full_name(first, middle="", last="", ext=""):
    """'Juan D. Cruz Jr.' - stored in customers.full_name and used on receipts and records."""
    name = " ".join(p for p in (first, _middle_initial(middle), last) if p)
    return f"{name} {ext}" if ext and ext != "None" else name
 
 
def list_name(first, middle="", last="", ext=""):
    """'Cruz, Juan D. Jr.' - used for the sorted customer list and customer pickers.
    Records saved before names were split have no last name, so they show as typed."""
    first, last = (first or "").strip(), (last or "").strip()
    if not last:
        return first
    rest = " ".join(p for p in (first, _middle_initial(middle), ext if ext and ext != "None" else "") if p)
    return f"{last}, {rest}"
 
 
def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 310000).hex()
 
 
class DatabaseManager:
    def __init__(self, db_path=DB_PATH):
        self.db_path = str(db_path)
        with self.connect() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS customers(
                id INTEGER PRIMARY KEY AUTOINCREMENT, full_name TEXT NOT NULL,
                phone TEXT DEFAULT '', email TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS memberships(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                plan_name TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
                plan_price REAL NOT NULL CHECK(plan_price >= 0),
                payment_status TEXT NOT NULL DEFAULT 'Unpaid'
                CHECK(payment_status IN ('Paid','Partial','Unpaid')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS payments(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                membership_id INTEGER REFERENCES memberships(id) ON DELETE SET NULL,
                amount REAL NOT NULL CHECK(amount > 0), payment_date TEXT NOT NULL,
                method TEXT NOT NULL DEFAULT 'Cash', receipt_no TEXT, note TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS payment_admin_auth(
                id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL UNIQUE,
                password_salt TEXT NOT NULL, password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('owner','staff')),
                password_salt TEXT NOT NULL, password_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attendance(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                check_in TEXT NOT NULL, check_out TEXT);
            CREATE INDEX IF NOT EXISTS idx_customer_name ON customers(full_name);
            CREATE INDEX IF NOT EXISTS idx_membership_dates ON memberships(customer_id,start_date,end_date);
            CREATE INDEX IF NOT EXISTS idx_payment_date ON payments(payment_date);
            CREATE INDEX IF NOT EXISTS idx_attendance_checkin ON attendance(check_in);
            """)
            payment_columns = {row["name"] for row in con.execute("PRAGMA table_info(payments)")}
            if "method" not in payment_columns:
                con.execute("ALTER TABLE payments ADD COLUMN method TEXT NOT NULL DEFAULT 'Cash'")
            if "receipt_no" not in payment_columns:
                con.execute("ALTER TABLE payments ADD COLUMN receipt_no TEXT")
            attendance_columns = {row["name"] for row in con.execute("PRAGMA table_info(attendance)")}
            if "auto_closed" not in attendance_columns:
                con.execute("ALTER TABLE attendance ADD COLUMN auto_closed INTEGER NOT NULL DEFAULT 0")
            customer_columns = {row["name"] for row in con.execute("PRAGMA table_info(customers)")}
            for column in ("first_name", "middle_name", "last_name", "extension"):
                if column not in customer_columns:
                    con.execute(f"ALTER TABLE customers ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            # Existing customers only have one name: keep it intact in first_name until it is edited.
            con.execute("UPDATE customers SET first_name=full_name WHERE first_name='' AND last_name=''")
            for column, ddl in (("recorded_by", "INTEGER"), ("voided", "INTEGER NOT NULL DEFAULT 0"),
                                ("void_reason", "TEXT NOT NULL DEFAULT ''"), ("voided_by", "INTEGER"),
                                ("voided_at", "TEXT")):
                if column not in payment_columns:
                    con.execute(f"ALTER TABLE payments ADD COLUMN {column} {ddl}")
            if "recorded_by" not in attendance_columns:
                con.execute("ALTER TABLE attendance ADD COLUMN recorded_by INTEGER")
            # The old single shared admin login becomes the owner account (same username and password).
            if con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"] == 0:
                old_admin = con.execute(
                    "SELECT username,password_salt,password_hash FROM payment_admin_auth WHERE id=1").fetchone()
                if old_admin:
                    con.execute("INSERT INTO users(username,display_name,role,password_salt,password_hash) VALUES(?,?,?,?,?)",
                                (old_admin["username"], old_admin["username"], "owner",
                                 old_admin["password_salt"], old_admin["password_hash"]))
            old_payments = con.execute(
                "SELECT id,payment_date FROM payments WHERE receipt_no IS NULL OR receipt_no=''"
            ).fetchall()
            for payment in old_payments:
                con.execute("UPDATE payments SET receipt_no=? WHERE id=?",
                            (receipt_number(payment["payment_date"], payment["id"]), payment["id"]))
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_receipt_no ON payments(receipt_no)")
    def connect(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con
    def all(self, sql, params=()):
        with self.connect() as con: return con.execute(sql, params).fetchall()
    def one(self, sql, params=()):
        with self.connect() as con: return con.execute(sql, params).fetchone()
    def run(self, sql, params=()):
        with self.connect() as con: return con.execute(sql, params).lastrowid
 
 
class GymManager:
    def __init__(self, db):
        self.db = db
        self.current_user = None  # set at sign-in: {"id", "username", "display_name", "role"}
 
    # ---- accounts ---------------------------------------------------------
    def _require_user(self):
        if not self.current_user: raise ValueError("Sign in first.")
        return self.current_user["id"]
    def _require_owner(self):
        if not self.current_user or self.current_user["role"] != "owner":
            raise ValueError("Only the owner can do this.")
    @staticmethod
    def _user_dict(row):
        return {"id": row["id"], "username": row["username"],
                "display_name": row["display_name"], "role": row["role"]}
    @staticmethod
    def _check_password(password):
        if len(password or "") < 8: raise ValueError("Password must contain at least 8 characters.")
    @staticmethod
    def _validate_account(display_name, username, password):
        display_name = " ".join((display_name or "").split())
        username = (username or "").strip()
        if len(display_name) < 2: raise ValueError("Enter the person's name (at least 2 characters).")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
            raise ValueError("Username must be 3-32 characters: letters, numbers, dot, dash or underscore.")
        GymManager._check_password(password)
        return display_name, username
    def _insert_user(self, display_name, username, role, password):
        salt = secrets.token_hex(16)
        try:
            return self.db.run(
                "INSERT INTO users(username,display_name,role,password_salt,password_hash) VALUES(?,?,?,?,?)",
                (username, display_name, role, salt, hash_password(password, salt)))
        except sqlite3.IntegrityError:
            raise ValueError("That username is already taken.")
    def _set_password(self, uid, password):
        salt = secrets.token_hex(16)
        self.db.run("UPDATE users SET password_salt=?,password_hash=? WHERE id=?",
                    (salt, hash_password(password, salt), uid))
    def has_users(self):
        return self.db.one("SELECT COUNT(*) n FROM users")["n"] > 0
    def create_owner(self, display_name, username, password):
        if self.has_users(): raise ValueError("An account already exists. Sign in instead.")
        display_name, username = self._validate_account(display_name, username, password)
        uid = self._insert_user(display_name, username, "owner", password)
        return self._user_dict(self.db.one("SELECT * FROM users WHERE id=?", (uid,)))
    def create_staff(self, display_name, username, password):
        self._require_owner()
        display_name, username = self._validate_account(display_name, username, password)
        return self._insert_user(display_name, username, "staff", password)
    def authenticate(self, username, password):
        """Return the user dict for a correct username + password on an active account, else None."""
        row = self.db.one("SELECT * FROM users WHERE username=?", ((username or "").strip(),))
        digest = hash_password(password or "", row["password_salt"] if row else "00" * 16)
        if row and row["active"] and hmac.compare_digest(digest, row["password_hash"]):
            return self._user_dict(row)
        return None
    def verify_password(self, uid, password):
        row = self.db.one("SELECT password_salt,password_hash FROM users WHERE id=?", (uid,))
        return bool(row) and hmac.compare_digest(hash_password(password or "", row["password_salt"]), row["password_hash"])
    def users_list(self):
        return self.db.all("""SELECT id,display_name,username,role,active FROM users
            ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END, display_name COLLATE NOCASE""")
    def set_user_active(self, uid, active):
        self._require_owner()
        row = self.db.one("SELECT role FROM users WHERE id=?", (uid,))
        if not row: raise ValueError("Account not found.")
        if row["role"] == "owner": raise ValueError("The owner account can't be disabled.")
        self.db.run("UPDATE users SET active=? WHERE id=?", (1 if active else 0, uid))
    def reset_password(self, uid, new_password):
        self._require_owner()
        self._check_password(new_password)
        if not self.db.one("SELECT 1 FROM users WHERE id=?", (uid,)): raise ValueError("Account not found.")
        self._set_password(uid, new_password)
    def has_recovery_code(self):
        return self.db.one("SELECT 1 FROM app_settings WHERE key='recovery_hash'") is not None
    def new_recovery_code(self):
        """Create (and store only the hash of) a new owner recovery code. The caller shows it once."""
        code = "-".join(secrets.token_hex(2).upper() for _ in range(4))
        salt = secrets.token_hex(16)
        with self.db.connect() as con:
            con.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('recovery_salt',?)", (salt,))
            con.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('recovery_hash',?)",
                        (hash_password(code.replace("-", ""), salt),))
        return code
    def recover_owner_password(self, code, new_password):
        """Reset the owner's password with the recovery code. Returns a NEW recovery code."""
        self._check_password(new_password)
        salt = self.db.one("SELECT value FROM app_settings WHERE key='recovery_salt'")
        stored = self.db.one("SELECT value FROM app_settings WHERE key='recovery_hash'")
        if not (salt and stored): raise ValueError("No recovery code has been set up for this system.")
        entered = "".join(ch for ch in (code or "").upper() if ch.isalnum())
        if not hmac.compare_digest(hash_password(entered, salt["value"]), stored["value"]):
            raise ValueError("Incorrect recovery code.")
        owner = self.db.one("SELECT id FROM users WHERE role='owner' ORDER BY id LIMIT 1")
        if not owner: raise ValueError("No owner account found.")
        self._set_password(owner["id"], new_password)
        return self.new_recovery_code()
 
    # ---- customers / memberships / payments -------------------------------
    @staticmethod
    def _clean_name(first, middle, last, ext):
        """First and last name are required; middle name and extension are optional."""
        first, middle, last = (" ".join((v or "").split()) for v in (first, middle, last))
        ext = (ext or "").strip()
        if ext == "None": ext = ""
        if not first: raise ValueError("First name is required.")
        if not last: raise ValueError("Last name is required.")
        if ext and ext not in EXTENSIONS: raise ValueError("Choose a valid name extension.")
        return first, middle, last, ext
    def add_customer(self, first, middle="", last="", ext="", phone="", email=""):
        first, middle, last, ext = self._clean_name(first, middle, last, ext)
        return self.db.run(
            "INSERT INTO customers(full_name,first_name,middle_name,last_name,extension,phone,email) VALUES(?,?,?,?,?,?,?)",
            (build_full_name(first, middle, last, ext), first, middle, last, ext, phone.strip(), email.strip()))
    def update_customer(self, cid, first, middle="", last="", ext="", phone="", email=""):
        first, middle, last, ext = self._clean_name(first, middle, last, ext)
        self.db.run(
            "UPDATE customers SET full_name=?,first_name=?,middle_name=?,last_name=?,extension=?,phone=?,email=? WHERE id=?",
            (build_full_name(first, middle, last, ext), first, middle, last, ext, phone.strip(), email.strip(), cid))
    def customers(self, query=""):
        p = f"%{query.strip()}%"
        return self.db.all("""SELECT c.*,
          (SELECT plan_name FROM memberships WHERE customer_id=c.id ORDER BY id DESC LIMIT 1) plan,
          (SELECT end_date FROM memberships WHERE customer_id=c.id ORDER BY id DESC LIMIT 1) end_date
          FROM customers c
          WHERE full_name LIKE ? OR first_name LIKE ? OR middle_name LIKE ? OR last_name LIKE ?
             OR phone LIKE ? OR email LIKE ?
          ORDER BY COALESCE(NULLIF(last_name,''),first_name) COLLATE NOCASE, first_name COLLATE NOCASE""",
          (p,p,p,p,p,p))
    def add_membership(self, cid, plan, start_text):
        if plan not in PLANS:
            raise ValueError("Choose a valid membership plan.")
        try:
            start = date.fromisoformat(start_text)
        except ValueError:
            raise ValueError("Start date must use YYYY-MM-DD.")
 
        # Check outstanding balances and create the membership in one write
        # transaction so two rapid requests cannot bypass the renewal rule.
        end = start + timedelta(days=PLANS[plan]["days"] - 1)
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            outstanding = con.execute("""SELECT m.id, m.plan_name,
                    MAX(m.plan_price - COALESCE(SUM(p.amount), 0), 0) AS balance
                FROM memberships m
                LEFT JOIN payments p ON p.membership_id = m.id AND p.voided = 0
                WHERE m.customer_id = ?
                GROUP BY m.id
                HAVING balance > 0.005
                ORDER BY m.id
                LIMIT 1""", (cid,)).fetchone()
            if outstanding:
                raise ValueError(
                    f"Renewal blocked: {outstanding['plan_name']} membership "
                    f"(ID {outstanding['id']}) still has an outstanding balance "
                    f"of ₱{outstanding['balance']:,.2f}. Settle the balance before "
                    "creating another membership."
                )
            cursor = con.execute("""INSERT INTO memberships
                (customer_id,plan_name,start_date,end_date,plan_price)
                VALUES(?,?,?,?,?)""",
                (cid, plan, start.isoformat(), end.isoformat(), PLANS[plan]["price"]))
            return cursor.lastrowid
    def delete_customer(self, cid):
        self._require_owner()
        # Preserve history: customers with any linked records cannot be deleted.
        related = self.db.one("""SELECT
            (SELECT COUNT(*) FROM memberships WHERE customer_id=?) +
            (SELECT COUNT(*) FROM payments WHERE customer_id=?) +
            (SELECT COUNT(*) FROM attendance WHERE customer_id=?) AS total""", (cid, cid, cid))["total"]
        if related:
            raise ValueError("This customer has membership, payment, or attendance history, so the record is kept.")
        self.db.run("DELETE FROM customers WHERE id=?", (cid,))
 
    def delete_membership(self, mid):
        self._require_owner()
        payments = self.db.one("SELECT COUNT(*) n FROM payments WHERE membership_id=? AND voided=0", (mid,))["n"]
        if payments:
            raise ValueError("This membership has payments. Void its payments first before deleting the membership.")
        self.db.run("DELETE FROM memberships WHERE id=?", (mid,))
 
    def _refresh_membership_status(self, con, mid):
        if mid is None: return
        membership = con.execute("SELECT plan_price FROM memberships WHERE id=?", (mid,)).fetchone()
        if not membership: return
        paid = float(con.execute("SELECT COALESCE(SUM(amount),0) total FROM payments WHERE membership_id=? AND voided=0",
                                 (mid,)).fetchone()["total"])
        price = float(membership["plan_price"])
        status = "Paid" if paid >= price - .005 else ("Partial" if paid > 0 else "Unpaid")
        con.execute("UPDATE memberships SET payment_status=? WHERE id=?", (status, mid))
 
    def void_payment(self, payment_id, reason, authorized=False):
        """Payments are never deleted: voiding keeps the record, marks it VOID with a reason and who did it."""
        self._require_owner()
        if not authorized:
            raise ValueError("Password confirmation is required to void a payment.")
        reason = " ".join((reason or "").split())
        if len(reason) < 3:
            raise ValueError("A reason (at least 3 characters) is required to void a payment.")
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            payment = con.execute("SELECT membership_id,voided FROM payments WHERE id=?", (payment_id,)).fetchone()
            if not payment: raise ValueError("Payment record not found.")
            if payment["voided"]: raise ValueError("This payment is already voided.")
            con.execute("UPDATE payments SET voided=1,void_reason=?,voided_by=?,voided_at=? WHERE id=?",
                        (reason, self.current_user["id"], datetime.now().isoformat(timespec="seconds"), payment_id))
            self._refresh_membership_status(con, payment["membership_id"])
 
    def delete_attendance(self, attendance_id):
        self._require_owner()
        self.db.run("DELETE FROM attendance WHERE id=?", (attendance_id,))
 
    def active_membership(self, cid, on_date=None):
        """Access is determined by membership dates, not payment status."""
        d = on_date or date.today().isoformat()
        return self.db.one("""SELECT m.* FROM memberships m
          WHERE m.customer_id=? AND m.start_date<=? AND m.end_date>=?
          ORDER BY m.end_date DESC,m.id DESC LIMIT 1""", (cid, d, d))
    def balance(self, mid):
        m = self.db.one("SELECT plan_price FROM memberships WHERE id=?", (mid,))
        if not m: return 0.0
        paid = self.db.one("SELECT COALESCE(SUM(amount),0) total FROM payments WHERE membership_id=? AND voided=0", (mid,))["total"]
        return max(0.0, float(m["plan_price"])-float(paid))
    def payment(self, cid, mid, amount, pay_date, note="", method="Cash", authorized=False):
        if not authorized:
            raise ValueError("Password confirmation is required to record a payment.")
        recorded_by = self._require_user()
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            raise ValueError("Enter a valid amount.")
        if amount <= 0: raise ValueError("Payment amount must be greater than zero.")
        if method not in PAYMENT_METHODS:
            raise ValueError("Choose a valid payment method.")
        try: payment_day = date.fromisoformat(pay_date)
        except ValueError: raise ValueError("Payment date must use YYYY-MM-DD.")
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")  # balance check + insert as one step, so a double-click can't overpay
            membership = con.execute(
                "SELECT plan_price FROM memberships WHERE id=? AND customer_id=?", (mid,cid)
            ).fetchone()
            if not membership: raise ValueError("Select a valid membership.")
            already_paid = con.execute(
                "SELECT COALESCE(SUM(amount),0) total FROM payments WHERE membership_id=? AND voided=0", (mid,)
            ).fetchone()["total"]
            balance = max(0.0, float(membership["plan_price"])-float(already_paid))
            if amount > balance + .00001: raise ValueError(f"Payment exceeds balance ₱{balance:,.2f}.")
            cursor = con.execute(
                "INSERT INTO payments(customer_id,membership_id,amount,payment_date,method,note,recorded_by) VALUES(?,?,?,?,?,?,?)",
                (cid,mid,amount,payment_day.isoformat(),method,note.strip(),recorded_by)
            )
            payment_id = cursor.lastrowid
            con.execute("UPDATE payments SET receipt_no=? WHERE id=?",
                        (receipt_number(payment_day.isoformat(), payment_id), payment_id))
            paid = float(already_paid)+amount
            price = float(membership["plan_price"])
            status = "Paid" if paid >= price-.005 else ("Partial" if paid > 0 else "Unpaid")
            con.execute("UPDATE memberships SET payment_status=? WHERE id=?", (status,mid))
        return payment_id
    def close_stale_checkins(self):
        """Close check-ins left open from a previous day at 23:59:59 of that day, flagged as auto-closed."""
        self.db.run("""UPDATE attendance SET check_out=substr(check_in,1,10)||'T23:59:59', auto_closed=1
                       WHERE check_out IS NULL AND substr(check_in,1,10)<?""", (date.today().isoformat(),))
    def check_in(self, cid):
        recorded_by = self._require_user()
        self.close_stale_checkins()
        # Check dates (not payment status) and insert atomically to prevent
        # duplicate open check-ins from rapid repeated clicks.
        today = date.today().isoformat()
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            active = con.execute("""SELECT id FROM memberships
                WHERE customer_id=? AND start_date<=? AND end_date>=?
                ORDER BY end_date DESC,id DESC LIMIT 1""", (cid, today, today)).fetchone()
            if not active:
                raise ValueError("No active membership for today. Add or renew a membership first.")
            if con.execute(
                "SELECT id FROM attendance WHERE customer_id=? AND check_out IS NULL LIMIT 1", (cid,)
            ).fetchone():
                raise ValueError("This customer is already checked in.")
            con.execute("INSERT INTO attendance(customer_id,check_in,recorded_by) VALUES(?,?,?)",
                        (cid, datetime.now().isoformat(timespec="seconds"), recorded_by))
    def check_out(self, cid):
        self.close_stale_checkins()
        r = self.db.one("SELECT id FROM attendance WHERE customer_id=? AND check_out IS NULL ORDER BY check_in DESC LIMIT 1",(cid,))
        if not r: raise ValueError("This customer does not have an open check-in.")
        self.db.run("UPDATE attendance SET check_out=? WHERE id=?",
                    (datetime.now().isoformat(timespec="seconds"),r["id"]))
    def stats(self):
        today = date.today().isoformat()
        return {
          "customers": self.db.one("SELECT COUNT(*) n FROM customers")["n"],
          "active": self.db.one("SELECT COUNT(DISTINCT customer_id) n FROM memberships WHERE start_date<=? AND end_date>=?",(today,today))["n"],
          "income": self.db.one("SELECT COALESCE(SUM(amount),0) n FROM payments WHERE payment_date=? AND voided=0",(today,))["n"],
          "checkins": self.db.one("SELECT COUNT(*) n FROM attendance WHERE substr(check_in,1,10)=?",(today,))["n"],
          "outstanding": self.db.one("""SELECT COALESCE(SUM(MAX(m.plan_price-COALESCE(p.paid,0),0)),0) n
            FROM memberships m LEFT JOIN (SELECT membership_id,SUM(amount) paid FROM payments WHERE voided=0 GROUP BY membership_id) p
            ON p.membership_id=m.id""")["n"]}
 
 
# ---------------------------------------------------------------------------
# Flet UI
# ---------------------------------------------------------------------------
class GymApp:
    GREEN="#F97316"; DARK="#EA580C"; LIGHT="#27272A"; BG="#111113"; TEXT="#F4F4F5"; MUTED="#A1A1AA"
    RED="#EF4444"; RED_DARK="#B91C1C"; SLATE="#52525B"; MOSS="#C2410C"
    SURFACE="#202124"; SURFACE_ALT="#27272A"; BORDER="#3F3F46"; ACCENT_SOFT="#431407"
 
    def __init__(self, page: ft.Page):
        self.page = page
        page.title = APP_NAME
        try:
            page.window.width = 1240; page.window.height = 780
            page.window.min_width = 1050; page.window.min_height = 680
        except Exception:
            pass
        page.padding = 0
        page.spacing = 0
        page.bgcolor = self.BG
        page.theme_mode = ft.ThemeMode.DARK
        page.theme = ft.Theme(color_scheme_seed=self.GREEN)
 
        self.db = DatabaseManager(); self.manager = GymManager(self.db); self.selected_customer = None
        self.current_user = None
        self.last_activity = time.monotonic()
        self.pw_failed_attempts = 0
        self.pw_locked_until = 0.0
        self.pw_lockout_seconds = 30
        self._creating_membership = False
        self.shell_visible = False
        page.on_keyboard_event = lambda e: self._touch()
 
        # ---- sidebar ----
        self.nav = {}
        nav_items = []
        for label, func in [("Dashboard", self.dashboard), ("Customers", self.customer_page),
                            ("Memberships", self.membership_page), ("Payments", self.payment_page),
                            ("Attendance", self.attendance_page), ("Accounts", self.accounts_page)]:
            item = ft.Container(
                content=ft.Text(label, color="white", size=14, text_align=ft.TextAlign.LEFT),
                width=199,
                alignment=LEFT_ALIGNMENT,
                padding=pad_symmetric(vertical=12, horizontal=16),
                border_radius=9, ink=True, bgcolor=ft.Colors.TRANSPARENT,
                on_click=lambda e, n=label, f=func: self.navigate(n, f))
            self.nav[label] = item
            nav_items.append(item)
        self.nav["Accounts"].visible = False  # shown only while the owner is signed in
        self.sidebar = ft.Container(
            width=225, bgcolor="#18181B", padding=pad_symmetric(horizontal=13),
            content=ft.Column([
                ft.Container(height=24),
                ft.Container(ft.Text("🏋  KRAUSE GYM", size=20, weight=ft.FontWeight.BOLD, color="white"),
                             padding=pad_only(left=7)),
                ft.Container(ft.Text("MANAGEMENT SYSTEM", size=10, weight=ft.FontWeight.BOLD, color="#FDBA74"),
                             padding=pad_only(left=9, bottom=22)),
                *nav_items], spacing=4))
 
        # ---- header + content ----
        self.title_label = ft.Text("Dashboard", size=25, weight=ft.FontWeight.BOLD, color=self.TEXT)
        self.clock = ft.Text("", color=self.MUTED)
        self.user_label = ft.Text("", size=12, color=self.MUTED)
        self.logout_button = Btn("Logout", on_click=lambda e: self.logout(), width=150, height=52,
                                style=ft.ButtonStyle(bgcolor=self.RED, color="white",
                                                    padding=pad_symmetric(horizontal=20, vertical=12),
                                                    shape=ft.RoundedRectangleBorder(radius=8)))
        self.header = ft.Container(
            bgcolor=self.SURFACE, padding=pad_symmetric(horizontal=26, vertical=16),
            content=ft.Row([ft.Column([self.title_label, self.user_label], spacing=0),
                            ft.Row([self.clock, self.logout_button], spacing=14)],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN))
        self.content = ft.Column(scroll=ft.ScrollMode.AUTO, expand=True, spacing=15,
                                 horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.main = ft.Column([self.header, ft.Container(self.content, padding=18, expand=True)],
                              expand=True, spacing=0, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.shell = ft.Row([self.sidebar, self.main], expand=True, spacing=0,
                            vertical_alignment=ft.CrossAxisAlignment.STRETCH)
 
        threading.Thread(target=self._background_loop, daemon=True).start()
        self.show_login_page()
 
    # ------------------------------------------------------------------ helpers
    def T(self, value, size=13, bold=False, color=None, **kw):
        return ft.Text(str(value), size=size, weight=ft.FontWeight.BOLD if bold else None,
                       color=color or self.TEXT, **kw)
 
    def open_dialog(self, dlg):
        if hasattr(self.page, "show_dialog"): self.page.show_dialog(dlg)
        else: self.page.open(dlg)
 
    def close_dialog(self, dlg):
        if hasattr(self.page, "pop_dialog"): self.page.pop_dialog()
        else: self.page.close(dlg)
 
    def _message(self, title, message, on_ok=None):
        def ok(e):
            self.close_dialog(dlg)
            if on_ok: on_ok()
        dlg = ft.AlertDialog(
            modal=True, title=ft.Text(title, weight=ft.FontWeight.BOLD),
            content=ft.Container(ft.Text(str(message)), width=420),
            actions=[self.button("OK", lambda: ok(None), 90)])
        self.open_dialog(dlg)
 
    def showinfo(self, title, message, on_ok=None): self._message(title, message, on_ok)
    def showwarning(self, title, message, on_ok=None): self._message(title, message, on_ok)
    def showerror(self, title, message, on_ok=None): self._message(title, message, on_ok)
 
    def askyesno(self, title, message, on_yes, on_no=None):
        def yes():
            self.close_dialog(dlg); on_yes()
        def no():
            self.close_dialog(dlg)
            if on_no: on_no()
        dlg = ft.AlertDialog(
            modal=True, title=ft.Text(title, weight=ft.FontWeight.BOLD),
            content=ft.Container(ft.Text(str(message)), width=440),
            actions=[self.button("Yes", yes, 90), self.button("No", no, 90, self.SLATE)])
        self.open_dialog(dlg)
 
    def new_page(self, title):
        self.title_label.value = title
        self.content.controls.clear()
        return self.content
 
    def section(self, parent, title, subtitle=""):
        body = ft.Column(spacing=6, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        head = [self.T(title, 17, True)]
        if subtitle: head.append(self.T(subtitle, 12, color=self.MUTED))
        card = ft.Container(
            bgcolor=self.SURFACE, border=ft.Border.all(1, self.BORDER), border_radius=13, padding=pad_all(17),
            content=ft.Column(head + [body], spacing=4, horizontal_alignment=ft.CrossAxisAlignment.STRETCH))
        parent.controls.append(card)
        return body
 
    def entry(self, placeholder, width=180, value=None, **kw):
        # Let callers override the default height without passing height twice
        # to Flet's TextField constructor.
        height = kw.pop("height", 44)
        return ft.TextField(label=placeholder, width=width, height=height, dense=True, value=value,
                            text_size=13, **kw)
 
    def dropdown(self, options, value=None, width=200):
        return ft.Dropdown(options=[Opt(o) for o in options], value=value, width=width,
                           dense=True, text_size=13)
 
    @staticmethod
    def checkout_text(r):
        if not r["check_out"]:
            return "Still inside"
        return r["check_out"].replace("T", " ") + (" (auto)" if r["auto_closed"] else "")
 
    def button(self, text, cmd, width=130, color=None):
        """Create a button with extra room so labels stay on one line across Flet versions."""
        def handler(e):
            self._touch()
            cmd()
        # Keep text on one line and give the button generous padding/height. The explicit
        # Text content prevents newer Flet versions from wrapping long button labels.
        label = ft.Text(text, no_wrap=True, text_align=ft.TextAlign.CENTER, size=13)
        return Btn(label, on_click=handler, width=max(width, len(text) * 10 + 38), height=48,
                   style=ft.ButtonStyle(bgcolor=color or self.GREEN, color="white",
                                        padding=pad_symmetric(horizontal=16, vertical=10),
                                        shape=ft.RoundedRectangleBorder(radius=8)))
 
    def small_button(self, text, cmd, width=70, color=None):
        def handler(e):
            self._touch()
            cmd()
        return Btn(text, on_click=handler, width=width, height=31,
                   style=ft.ButtonStyle(bgcolor=color or self.GREEN, color="white",
                                        padding=pad_symmetric(horizontal=4),
                                        shape=ft.RoundedRectangleBorder(radius=8)))
 
    def table(self, headers, rows, empty="No records found.", weights=None):
        """Columns share the width equally unless `weights` (one whole number per column) is given.
        Any cell that is a Flet control is shown as-is (buttons, styled text)."""
        weights = weights or [1] * len(headers)
        box = ft.Column(spacing=0, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        box.controls.append(ft.Container(
            bgcolor=self.SURFACE_ALT, padding=pad_symmetric(vertical=9, horizontal=5),
            content=ft.Row([ft.Text(h, expand=w, text_align=ft.TextAlign.CENTER, size=11,
                                    weight=ft.FontWeight.BOLD, color="#FDBA74")
                            for h, w in zip(headers, weights)], spacing=4)))
        for n, row in enumerate(rows):
            cells = []
            for v, w in zip(row, weights):
                if isinstance(v, ft.Control):
                    cells.append(ft.Row([v], alignment=ft.MainAxisAlignment.CENTER, expand=w))
                else:
                    cells.append(ft.Text(str(v), expand=w, text_align=ft.TextAlign.CENTER,
                                         color=self.TEXT, size=12))
            box.controls.append(ft.Container(
                bgcolor=self.SURFACE if n % 2 == 0 else "#252529",
                padding=pad_symmetric(vertical=6, horizontal=5),
                content=ft.Row(cells, spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER)))
        if not rows:
            box.controls.append(ft.Container(
                padding=14, content=ft.Row([self.T(empty, color=self.MUTED)],
                                           alignment=ft.MainAxisAlignment.CENTER)))
        return box
 
    # ------------------------------------------------------------------ login / session
    def _focus(self, control):
        """Give a control keyboard focus (focus() is async on current Flet, plain on older versions)."""
        try:
            if inspect.iscoroutinefunction(control.focus):
                self.page.run_task(control.focus)
            else:
                control.focus()
        except Exception:
            pass
 
    @property
    def is_owner(self):
        return bool(self.current_user and self.current_user["role"] == "owner")
 
    def _touch(self, _event=None):
        self.last_activity = time.monotonic()
 
    def _centered_card(self, *controls):
        card = ft.Container(
            bgcolor=self.SURFACE, border_radius=18, width=440, padding=pad_all(26),
            content=ft.Column(
                [ft.Text("🏋  KRAUSE GYM", size=25, weight=ft.FontWeight.BOLD, color=self.GREEN), *controls],
                spacing=8, tight=True, horizontal_alignment=ft.CrossAxisAlignment.CENTER))
        # Center a content-sized card without letting its Column stretch to the full window height.
        alignment_cls = getattr(ft, "Alignment", None)
        centered_alignment = alignment_cls(0, 0) if alignment_cls else ft.alignment.center
        self.page.controls.clear()
        self.page.add(ft.Container(content=card, expand=True, alignment=centered_alignment,
                                   padding=pad_all(20)))
        self.page.update()
 
    def _login_field(self, label, password=False):
        return ft.TextField(label=label, width=340, label_style=ft.TextStyle(size=13),
                            password=password, can_reveal_password=password, **field_look())
 
    def show_login_page(self, notice=""):
        self.shell_visible = False
        self.current_user = None
        self.manager.current_user = None
        setup = not self.manager.has_users()   # first run: create the owner account
        name_entry = self._login_field("Your name") if setup else None
        user_entry = self._login_field("Choose a username" if setup else "Username")
        password_entry = self._login_field("Create password (8+ characters)" if setup else "Password", password=True)
        confirm_entry = self._login_field("Confirm password", password=True) if setup else None
        info_label = ft.Text(notice, color=self.MUTED, width=340)
        error_label = ft.Text("", color=self.RED, width=340)
 
        def set_error(text):
            error_label.value = text; self.page.update()
 
        def submit(_event=None):
            now = time.monotonic()
            if not setup and now < self.pw_locked_until:
                return set_error(f"Too many attempts. Try again in {max(1, int(self.pw_locked_until - now))} seconds.")
            try:
                if setup:
                    if (password_entry.value or "") != (confirm_entry.value or ""):
                        return set_error("Passwords do not match.")
                    user = self.manager.create_owner(name_entry.value, user_entry.value, password_entry.value or "")
                else:
                    user = self.manager.authenticate(user_entry.value or "", password_entry.value or "")
                    if not user:
                        self.pw_failed_attempts += 1
                        password_entry.value = ""
                        if self.pw_failed_attempts >= 5:
                            self.pw_locked_until = time.monotonic() + self.pw_lockout_seconds
                            self.pw_failed_attempts = 0
                            return set_error(f"Too many incorrect attempts. Try again in {self.pw_lockout_seconds} seconds.")
                        return set_error("Incorrect username or password, or the account is disabled.")
            except (ValueError, sqlite3.Error) as error:
                return set_error(str(error))
            self.pw_failed_attempts = 0
            self._sign_in(user)
 
        fields = [e for e in (name_entry, user_entry, password_entry, confirm_entry) if e]
        for entry in fields:
            entry.on_submit = submit
        controls = [
            self.T("Create the owner account" if setup else "Sign In", 19, True),
            ft.Text("The owner adds the staff accounts and is the only one who can delete records or void payments."
                    if setup else "Sign in with your own username and password.", color=self.MUTED, width=340),
            info_label, *fields, error_label,
            self.button("Create Owner Account" if setup else "Sign In", submit, 340)]
        if not setup:
            controls.append(ft.TextButton("Forgot owner password?", on_click=lambda e: self.show_recovery_page()))
        self._centered_card(*controls)
        self._focus(name_entry or user_entry)
 
    def show_recovery_page(self):
        code_entry = self._login_field("Recovery code")
        new_entry = self._login_field("New owner password (8+ characters)", password=True)
        confirm_entry = self._login_field("Confirm new password", password=True)
        error_label = ft.Text("", color=self.RED, width=340)
 
        def set_error(text):
            error_label.value = text; self.page.update()
 
        def submit(_event=None):
            now = time.monotonic()
            if now < self.pw_locked_until:
                return set_error(f"Too many attempts. Try again in {max(1, int(self.pw_locked_until - now))} seconds.")
            if (new_entry.value or "") != (confirm_entry.value or ""):
                return set_error("Passwords do not match.")
            try:
                new_code = self.manager.recover_owner_password(code_entry.value or "", new_entry.value or "")
            except (ValueError, sqlite3.Error) as error:
                if str(error) == "Incorrect recovery code.":
                    self.pw_failed_attempts += 1
                    if self.pw_failed_attempts >= 5:
                        self.pw_locked_until = time.monotonic() + self.pw_lockout_seconds
                        self.pw_failed_attempts = 0
                return set_error(str(error))
            self.pw_failed_attempts = 0
            self._show_recovery_code(
                new_code, "Password reset - save your new recovery code",
                on_ok=lambda: self.show_login_page("Owner password reset. Sign in with your new password."))
 
        for entry in (code_entry, new_entry, confirm_entry):
            entry.on_submit = submit
        self._centered_card(
            self.T("Reset owner password", 19, True),
            ft.Text("Enter the recovery code you saved when the owner account was set up.",
                    color=self.MUTED, width=340),
            code_entry, new_entry, confirm_entry, error_label,
            self.button("Reset Password", submit, 340),
            ft.TextButton("Back to sign in", on_click=lambda e: self.show_login_page()))
        self._focus(code_entry)
 
    def _show_recovery_code(self, code, title, on_ok=None):
        def ok():
            self.close_dialog(dlg)
            if on_ok: on_ok()
        dlg = ft.AlertDialog(
            modal=True, title=ft.Text(title, weight=ft.FontWeight.BOLD),
            content=ft.Column([
                ft.Text("Write this code down and keep it somewhere safe, away from this computer. "
                        "If the owner forgets the password, this code is the only way to reset it. "
                        "It is shown only once.", width=420),
                ft.Container(ft.Row([ft.Text(code, size=24, weight=ft.FontWeight.BOLD, color=self.GREEN,
                                              selectable=True)], alignment=ft.MainAxisAlignment.CENTER),
                             bgcolor=self.ACCENT_SOFT, border_radius=8, padding=14, width=420)],
                tight=True, width=440, spacing=12),
            actions=[self.button("I saved it", ok, 140)])
        self.open_dialog(dlg)
 
    def _sign_in(self, user):
        self.current_user = user
        self.manager.current_user = user
        self.user_label.value = f"Signed in as {user['display_name']} ({user['role'].title()})"
        self.nav["Accounts"].visible = user["role"] == "owner"
        self._touch()
        self._show_main_system()
        if user["role"] == "owner" and not self.manager.has_recovery_code():
            self._show_recovery_code(self.manager.new_recovery_code(), "Save your recovery code")
 
    def _show_main_system(self):
        self.page.controls.clear()
        self.page.add(self.shell)
        self.shell_visible = True
        self.navigate("Dashboard", self.dashboard)
 
    def logout(self, notice=""):
        for _ in range(5):  # close any dialog still open so it doesn't sit on top of the sign-in screen
            try:
                if not self.page.pop_dialog(): break
            except Exception:
                break
        self.show_login_page(notice)
 
    def _background_loop(self):
        ticks = 0
        while True:
            time.sleep(1)
            ticks += 1
            try:
                if self.shell_visible:
                    self.clock.value = datetime.now().strftime("%a, %d %b %Y  •  %I:%M:%S %p")
                    self.clock.update()
                if ticks % 15 == 0:
                    self._check_idle_logout()
            except Exception:
                pass
 
    def _check_idle_logout(self):
        if self.shell_visible and time.monotonic() - self.last_activity >= IDLE_LOGOUT_SECONDS:
            self.logout(f"You were signed out after {IDLE_LOGOUT_SECONDS // 60} minutes of inactivity.")
 
    def navigate(self, name, func):
        if name == "Accounts" and not self.is_owner:
            return
        self._touch()
        for n, b in self.nav.items():
            b.bgcolor = self.DARK if n == name else ft.Colors.TRANSPARENT
        func()
        self.page.update()
 
    def _confirm_with_password(self, title, on_success, summary=None, confirm_label="Continue"):
        """Ask the signed-in person to re-enter THEIR OWN password before a sensitive action."""
        if not self.current_user:
            return self.showwarning("Not signed in", "Sign in first.")
        now = time.monotonic()
        if now < self.pw_locked_until:
            remaining = max(1, int(self.pw_locked_until - now))
            self.showwarning("Please wait", f"Too many incorrect passwords. Try again in {remaining} seconds.")
            return
        password = ft.TextField(label="Your password", width=450, label_style=ft.TextStyle(size=13),
                                password=True, can_reveal_password=True, **field_look())
        body = [ft.Text("Enter your password to authorize this action.", width=450), password]
        if summary:
            body.append(ft.Container(ft.Text(summary, color=self.TEXT), bgcolor=self.ACCENT_SOFT,
                                     border_radius=8, padding=10, width=450))
        dlg = ft.AlertDialog(
            modal=True, title=ft.Text(title, weight=ft.FontWeight.BOLD),
            content=ft.Column(body, tight=True, width=470, scroll=ft.ScrollMode.AUTO, spacing=10))
 
        def close(): self.close_dialog(dlg)
 
        def submit(_e=None):
            if not self.manager.verify_password(self.current_user["id"], password.value or ""):
                self.pw_failed_attempts += 1
                if self.pw_failed_attempts >= 5:
                    self.pw_locked_until = time.monotonic() + self.pw_lockout_seconds
                    self.pw_failed_attempts = 0
                    self.showerror("Temporarily locked", f"Five incorrect attempts. Try again in {self.pw_lockout_seconds} seconds.")
                else:
                    self.showerror("Incorrect password", f"Password incorrect. Attempts remaining: {5-self.pw_failed_attempts}.")
                password.value = ""
                self.page.update()
                return
            self.pw_failed_attempts = 0
            close()
            self._touch()
            on_success()
 
        password.on_submit = submit
        dlg.actions = [self.button(confirm_label, submit, 190), self.button("Cancel", close, 100, self.SLATE)]
        self.open_dialog(dlg)
        self._focus(password)
 
    # ------------------------------------------------------------------ dashboard
    def dashboard(self):
        self.manager.close_stale_checkins()
        p = self.new_page("Dashboard"); s = self.manager.stats()
        data = [("Total Customers", s["customers"]), ("Active Memberships", s["active"]),
                ("Today's Income", f"₱{s['income']:,.2f}"), ("Outstanding Balance", f"₱{s['outstanding']:,.2f}"),
                ("Today's Check-ins", s["checkins"])]
        cards = ft.Row(spacing=7)
        for lab, val in data:
            cards.controls.append(ft.Container(
                expand=1, bgcolor=self.SURFACE, border=ft.Border.all(1, self.BORDER), border_radius=12, padding=pad_all(10),
                content=ft.Column([self.T(lab, 11, True, self.MUTED), self.T(val, 18, True),
                                   self.T("Overview", 10, color=self.MUTED)], spacing=5)))
        p.controls.append(cards)
 
        sec = self.section(p, "Recent Attendance", "Latest visits")
        rs = self.db.all("SELECT c.full_name,a.check_in,a.check_out,a.auto_closed FROM attendance a JOIN customers c ON c.id=a.customer_id ORDER BY a.id DESC LIMIT 8")
        sec.controls.append(self.table(["Member", "Check-in", "Check-out"],
            [(r["full_name"], r["check_in"].replace("T", " "), self.checkout_text(r)) for r in rs]))
 
        sec = self.section(p, "Membership Plans", "Current standard rates")
        row = ft.Row(spacing=8)
        for name, pl in PLANS.items():
            row.controls.append(ft.Container(
                expand=1, bgcolor=self.LIGHT, border_radius=9, padding=pad_symmetric(vertical=10, horizontal=8),
                content=ft.Column([self.T(name, 14, True, self.DARK), self.T(f"₱{pl['price']:,.2f}", 18, True),
                                   self.T(f"{pl['days']} day(s)", color=self.MUTED)],
                                  spacing=3, horizontal_alignment=ft.CrossAxisAlignment.CENTER)))
        sec.controls.append(row)
        self.page.update()
 
    # ------------------------------------------------------------------ customers
    def customer_page(self):
        p = self.new_page("Customers")
        self.selected_customer = None  # fields are rebuilt blank, so drop any old selection
 
        def field(label, width):
            return ft.TextField(label=label, width=width,
                                label_style=ft.TextStyle(size=13), **field_look())
 
        f = self.section(p, "Customer Details", "Add a customer or select a row to edit")
        self.cfn = field("First name", 190)
        self.cmn = field("Middle name (optional)", 190)
        self.cln = field("Last name", 190)
        self.cext = ft.Dropdown(options=[Opt(o) for o in EXTENSIONS], value="None", width=130,
                                label="Extension", label_style=ft.TextStyle(size=13), **field_look())
        self.cp = field("Phone", 160)
        self.ce = field("Email (optional)", 220)
        f.controls.append(ft.Row([self.cfn, self.cmn, self.cln, self.cext],
                                 wrap=True, spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER))
        f.controls.append(ft.Row([
            self.cp, self.ce,
            self.button("Add Customer", self.add_customer, 150),
            self.button("Update Selected", self.update_customer, 190, self.MOSS)],
            wrap=True, spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER))
        self.csearch = field("Search name, phone, or email", 320)
        p.controls.append(ft.Row([
            self.csearch, self.button("Search", self.refresh_customers, 125),
            self.button("Show All", lambda: (setattr(self.csearch, "value", ""), self.refresh_customers()), 140, self.SLATE)],
            spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER))
        self.ctable = self.section(p, "Customer List", "Select a customer to load their details")
        self.refresh_customers()
 
    def clear_customer_form(self):
        for x in (self.cfn, self.cmn, self.cln, self.cp, self.ce): x.value = ""
        self.cext.value = "None"
 
    def add_customer(self):
        try:
            cid = self.manager.add_customer(
                self.cfn.value or "", self.cmn.value or "", self.cln.value or "", self.cext.value or "",
                self.cp.value or "", self.ce.value or "")
            full_name = self.db.one("SELECT full_name FROM customers WHERE id=?", (cid,))["full_name"]
            self.selected_customer = None  # the form is cleared, so nothing is selected anymore
            self.clear_customer_form()
            self.page.update()
            self.refresh_customers()
            self.open_new_member_dialog(cid, full_name)
        except (ValueError, sqlite3.Error) as e:
            self.showerror("Unable to add customer", str(e))
 
    def open_new_member_dialog(self, customer_id, customer_name):
        """After adding a customer, offer a modal membership and initial-payment workflow."""
        plan = self.dropdown(list(PLANS.keys()), "Monthly", 450)
        start_entry = self.entry("Start date (YYYY-MM-DD)", 450, date.today().isoformat())
        price_label = self.T("Plan price: ₱800.00", 15, True, self.DARK)
        amount_entry = self.entry("Initial payment (₱)", 450, "0", hint_text="Enter 0 for no payment yet")
        method_menu = self.dropdown(PAYMENT_METHODS, "Cash", 450)
        balance_label = self.T("Balance after payment: ₱800.00", color=self.MUTED)
 
        def update_price(_=None):
            amount = PLANS.get(plan.value, {}).get("price", 0)
            price_label.value = f"Plan price: ₱{amount:,.2f}"
            try:
                entered = max(0.0, float(amount_entry.value or 0))
            except ValueError:
                entered = 0.0
            balance_label.value = f"Balance after payment: ₱{max(0, amount-entered):,.2f}"
            self.page.update()
        plan.on_change = update_price
        plan.on_select = update_price   # newer Flet versions
        amount_entry.on_change = update_price
 
        def close(): self.close_dialog(dialog)
 
        def save_setup_authorized(start_text, selected_plan, payment_amount, payment_method):
            try:
                membership_id = self.manager.add_membership(customer_id, selected_plan, start_text)
                payment_id = None
                if payment_amount > 0:
                    payment_id = self.manager.payment(
                        customer_id, membership_id, payment_amount, date.today().isoformat(),
                        "Initial payment", method=payment_method, authorized=True)
                close()
                self.refresh_customers()
                if payment_id is not None:
                    self.show_receipt(payment_id)
                else:
                    self.showinfo("Setup saved", "Customer and membership saved. No initial payment was recorded.")
            except (ValueError, KeyError, sqlite3.Error) as e:
                self.showerror("Unable to save setup", str(e))
 
        def save_setup():
            try:
                start_text = (start_entry.value or "").strip()
                selected_plan = plan.value
                try:
                    payment_amount = float((amount_entry.value or "").strip() or 0)
                except ValueError:
                    raise ValueError("Enter a valid amount.")
                plan_price = PLANS[selected_plan]["price"]
                if payment_amount < 0:
                    raise ValueError("Payment cannot be negative.")
                if payment_amount > plan_price:
                    raise ValueError(f"Payment cannot exceed the plan price of ₱{plan_price:,.2f}.")
                payment_method = method_menu.value
                if payment_amount > 0:
                    summary = (f"Initial payment for new membership\nAmount: ₱{payment_amount:,.2f}\n"
                               f"Plan: {selected_plan}\nMethod: {payment_method}\n\n"
                               "Enter your password to authorize and save this payment.")
                    self._confirm_with_password(
                        "Confirm Initial Payment",
                        lambda: save_setup_authorized(start_text, selected_plan, payment_amount, payment_method),
                        summary, "Authenticate & Save")
                else:
                    save_setup_authorized(start_text, selected_plan, payment_amount, payment_method)
            except (ValueError, KeyError, sqlite3.Error) as e:
                self.showerror("Unable to save setup", str(e))
 
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Customer Added Successfully", weight=ft.FontWeight.BOLD),
            content=ft.Column([
                ft.Text(f"Set up a membership and record the first payment for {customer_name}.",
                        color=self.MUTED, width=450),
                self.T("Membership plan"), plan, start_entry, price_label,
                amount_entry, self.T("Payment method"), method_menu, balance_label,
                ft.Text("Membership dates determine check-in access, regardless of payment status. "
                        "Outstanding balances must be settled before another membership can be created.",
                        color=self.MUTED, width=450)],
                tight=True, width=470, scroll=ft.ScrollMode.AUTO, spacing=10),
            actions=[self.button("Save Membership & Payment", save_setup, 235),
                     self.button("Skip for Now", close, 120, self.SLATE)])
        self.open_dialog(dialog)
 
    def update_customer(self):
        if not self.selected_customer:
            return self.showwarning("Select customer", "Select a customer first.")
        try:
            self.manager.update_customer(
                self.selected_customer, self.cfn.value or "", self.cmn.value or "", self.cln.value or "",
                self.cext.value or "", self.cp.value or "", self.ce.value or "")
            self.refresh_customers()
            self.showinfo("Updated", "Customer details updated.")
        except (ValueError, sqlite3.Error) as e:
            self.showerror("Unable to update", str(e))
 
    def refresh_customers(self):
        if not hasattr(self, "ctable"): return
        self.ctable.controls.clear()
        rows = self.manager.customers(self.csearch.value or "" if hasattr(self, "csearch") else "")
        owner = self.is_owner
        data = []
        for r in rows:
            row = [r["id"], list_name(r["first_name"], r["middle_name"], r["last_name"], r["extension"]),
                   r["phone"], r["email"], r["plan"] or "—", r["end_date"] or "—",
                   self.small_button("Select", lambda rr=r: self.select_customer(rr), 65)]
            if owner:  # only the owner can delete customers
                row.append(self.small_button("Delete", lambda rr=r: self.delete_customer(rr["id"], rr["full_name"]),
                                             65, self.RED))
            data.append(row)
        self.ctable.controls.append(self.table(
            ["ID", "Name", "Phone", "Email", "Latest Plan", "End Date", "Select"] + (["Delete"] if owner else []),
            data, "No customers found."))
        self.page.update()
 
    def delete_customer(self, cid, name):
        def do_delete():
            try:
                self.manager.delete_customer(cid)
                if self.selected_customer == cid:
                    self.selected_customer = None
                    self.clear_customer_form()
                self.refresh_customers()
                self.showinfo("Deleted", "Customer deleted successfully.")
            except (ValueError, sqlite3.Error) as e:
                self.showerror("Unable to delete customer", str(e))
        self.askyesno("Confirm customer deletion",
                      f"Delete customer {name}? Customers with membership, payment, or attendance history cannot be deleted.",
                      do_delete)
 
    def select_customer(self, r):
        self.selected_customer = r["id"]
        self.cfn.value = r["first_name"] or ""
        self.cmn.value = r["middle_name"] or ""
        self.cln.value = r["last_name"] or ""
        self.cext.value = r["extension"] or "None"
        self.cp.value = r["phone"] or ""
        self.ce.value = r["email"] or ""
        self.page.update()
 
    def choices(self):
        rows = self.db.all("""SELECT id,first_name,middle_name,last_name,extension,phone FROM customers
          ORDER BY COALESCE(NULLIF(last_name,''),first_name) COLLATE NOCASE, first_name COLLATE NOCASE""")
        return rows, [f"{r['id']} - {list_name(r['first_name'], r['middle_name'], r['last_name'], r['extension'])} ({r['phone']})"
                      for r in rows]
 
    # ------------------------------------------------------------------ memberships
    def membership_page(self):
        p = self.new_page("Memberships")
        f = self.section(p, "Create / Renew Membership", "End date is calculated automatically")
        _, opts = self.choices()
        if not opts:
            f.controls.append(self.T("Add a customer first.", color=self.MUTED))
        else:
            # Same shared look as the Payment History filters: non-dense, 48px, one border.
            # (entry() is dense=True, which is why the date field drew shorter than the dropdowns.)
            look = dict(
                height=FIELD_H, dense=False, text_size=13,
                content_padding=pad_symmetric(horizontal=12, vertical=12),
                border=ft.OutlineInputBorder(border_radius=8, side=ft.BorderSide(1, "#71717A")),
            )
            self.mc = ft.Dropdown(options=[Opt(o) for o in opts], value=opts[0], width=300, **look)
            self.mp = ft.Dropdown(options=[Opt(o) for o in PLANS], value="Monthly", width=200, **look)
            self.ms = ft.TextField(label="Start YYYY-MM-DD", value=date.today().isoformat(), width=170,
                                   label_style=ft.TextStyle(size=13), **look)
            f.controls.append(ft.Row([self.mc, self.mp, self.ms,
                                      self.button("Create Membership", self.create_membership, 160)],
                                     wrap=True, spacing=8,
                                     vertical_alignment=ft.CrossAxisAlignment.CENTER))
        sec = self.section(p, "Membership Records",
                           "Access depends on valid membership dates. Outstanding balances must be settled before another membership can be created.")
        rows = self.db.all("""SELECT m.*,c.full_name,COALESCE((SELECT SUM(amount) FROM payments WHERE membership_id=m.id AND voided=0),0) paid
          FROM memberships m JOIN customers c ON c.id=m.customer_id ORDER BY m.id DESC LIMIT 250""")
        today = date.today().isoformat(); data = []
        owner = self.is_owner
        for x in rows:
            bal = max(0, x["plan_price"] - x["paid"])
            if today < x["start_date"]:
                access = "Upcoming"
            elif today > x["end_date"]:
                access = "Expired"
            else:
                # Active dates grant access even when the balance is unpaid.
                access = "Active"
            mid = int(x["id"])
            row = [x["id"], x["full_name"], x["plan_name"], x["start_date"], x["end_date"],
                   f"₱{x['plan_price']:,.2f}", x["payment_status"], f"₱{bal:,.2f}", access]
            if owner:  # only the owner can delete memberships
                row.append(self.small_button("Delete", lambda m=mid: self.delete_membership(m), 62, self.RED))
            data.append(row)
        sec.controls.append(self.table(
            ["ID", "Customer", "Plan", "Start", "End", "Price", "Payment", "Balance", "Access"]
            + (["Delete"] if owner else []),
            data, "No membership records found."))
        self.page.update()
 
    def delete_membership(self, mid):
        def do_delete():
            try:
                self.manager.delete_membership(mid)
                self.membership_page()
                self.showinfo("Deleted", "Membership deleted successfully.")
            except (ValueError, sqlite3.Error) as e:
                self.showerror("Unable to delete membership", str(e))
        self.askyesno("Confirm membership deletion",
                      f"Delete membership ID {mid}? Any linked payments must be deleted first. This cannot be undone.",
                      do_delete)
 
    def create_membership(self):
        # Guard against rapid double-clicks while the handler is running.
        if self._creating_membership:
            return
        self._creating_membership = True
        waiting_for_dialog = False
        try:
            cid = int(self.mc.value.split(" - ", 1)[0])
            plan_name = self.mp.value
            start_text = (self.ms.value or "").strip()
            if plan_name not in PLANS:
                raise ValueError("Choose a valid membership plan.")
            try:
                start = date.fromisoformat(start_text)
            except ValueError:
                raise ValueError("Start date must use YYYY-MM-DD.")
            proposed_end = start + timedelta(days=PLANS[plan_name]["days"] - 1)
 
            # Warn about date overlap, but let the user continue if intentional.
            overlaps = self.db.all("""SELECT plan_name, start_date, end_date
                FROM memberships
                WHERE customer_id=? AND start_date<=? AND end_date>=?
                ORDER BY start_date""", (cid, proposed_end.isoformat(), start.isoformat()))
 
            def do_create():
                try:
                    self.manager.add_membership(cid, plan_name, start.isoformat())
                    self.membership_page()
                    self.showinfo("Created", "Membership created successfully.")
                except (ValueError, sqlite3.Error) as e:
                    self.showerror("Unable to create membership", str(e))
                finally:
                    self._creating_membership = False
 
            if overlaps:
                customer = self.db.one("SELECT full_name FROM customers WHERE id=?", (cid,))
                existing = "\n".join(
                    f"• {row['plan_name']} ({row['start_date']} to {row['end_date']})" for row in overlaps)
                waiting_for_dialog = True
                self.askyesno(
                    "Overlapping Membership",
                    f"{customer['full_name']} already has membership dates that overlap with this plan.\n\n"
                    f"Existing membership(s):\n{existing}\n\n"
                    f"New membership: {plan_name} ({start.isoformat()} to {proposed_end.isoformat()})\n\n"
                    "Do you want to create it anyway?",
                    do_create,
                    lambda: setattr(self, "_creating_membership", False))
                return
            waiting_for_dialog = True
            do_create()
        except (ValueError, sqlite3.Error, AttributeError) as e:
            self.showerror("Unable to create membership", str(e))
        finally:
            if not waiting_for_dialog:
                self._creating_membership = False
 
    # ------------------------------------------------------------------ payments
    def payment_page(self):
        # Payments is accessible to any user already signed in to the main app.
        # Password verification happens only when authorizing a financial action.
        self._render_payment_page()
 
    def _tab_style(self, selected):
        """One style for every payment tab: same text color and padding, only the background changes."""
        return ft.ButtonStyle(
            bgcolor=self.GREEN if selected else self.SURFACE_ALT,
            color="white",
            padding=pad_symmetric(horizontal=16, vertical=10),
            shape=ft.RoundedRectangleBorder(radius=8))
 
    def _tab_button(self, label, selected):
        """Fixed-size tab so all three payment sub-pages are identical in width and height."""
        text = ft.Text(label, no_wrap=True, size=13, weight=ft.FontWeight.BOLD,
                       text_align=ft.TextAlign.CENTER)
        return Btn(text, width=TAB_W, height=TAB_H, style=self._tab_style(selected),
                   on_click=lambda e, name=label: self.show_payment_subpage(name))
 
    def _render_payment_page(self):
        p = self.new_page("Payments")
        self.title_label.value = "Payments"
        self.payment_sub_buttons = {}
        nav = ft.Row(spacing=8)
        for label in ("Record Payment", "Outstanding Balance", "Payment History"):
            button = self._tab_button(label, label == "Record Payment")
            nav.controls.append(button)
            self.payment_sub_buttons[label] = button
        self.payment_panel = ft.Column(spacing=15, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        p.controls.append(nav)
        p.controls.append(self.payment_panel)
        self.show_payment_subpage("Record Payment")
 
    def show_payment_subpage(self, name):
        self._touch()
        self.payment_panel.controls.clear()
        for label, button in self.payment_sub_buttons.items():
            button.style = self._tab_style(label == name)
        if name == "Record Payment":
            self.build_record_payment_page()
        elif name == "Outstanding Balance":
            self.build_outstanding_balance_page()
        else:
            self.build_payment_history_page()
        self.page.update()
 
    def build_record_payment_page(self):
        p = self.payment_panel
        f = self.section(p, "Record Payment", "Record full or partial payments")
        mem = self.db.all("""SELECT m.id,m.customer_id,c.full_name,m.plan_name,m.plan_price,
          COALESCE((SELECT SUM(amount) FROM payments WHERE membership_id=m.id AND voided=0),0) paid
          FROM memberships m JOIN customers c ON c.id=m.customer_id ORDER BY m.id DESC""")
        opts = [f"{x['id']} - {x['full_name']} | {x['plan_name']} | Balance ₱{max(0,x['plan_price']-x['paid']):,.2f}"
                for x in mem]
        self.payment_options = {int(row["id"]): option for row, option in zip(mem, opts)}
 
        def field(label, width, value=None):
            return ft.TextField(label=label, width=width, value=value,
                                label_style=ft.TextStyle(size=13), **field_look())
 
        if not opts:
            f.controls.append(self.T("Create a membership first.", color=self.MUTED))
        else:
            self.pm = ft.Dropdown(options=[Opt(o) for o in opts], value=opts[0], width=480,
                                  **field_look())
            self.pmeth = ft.Dropdown(options=[Opt(o) for o in PAYMENT_METHODS], value="Cash", width=160,
                                     **field_look())
            f.controls.append(ft.Row([self.pm, self.pmeth,
                                      self.button("Pay Full Balance", self.fill_full_balance, 150, self.MOSS)],
                                     wrap=True, spacing=8,
                                     vertical_alignment=ft.CrossAxisAlignment.CENTER))
            self.pa = field("Amount ₱", 145)
            self.pd = field("Date YYYY-MM-DD", 160, date.today().isoformat())
            self.pn = field("Note optional", 220)
            f.controls.append(ft.Row([self.pa, self.pd, self.pn,
                                      self.button("Save Payment", self.save_payment, 125)],
                                     wrap=True, spacing=8,
                                     vertical_alignment=ft.CrossAxisAlignment.CENTER))
 
        info = self.section(p, "Payment Tips", "Choose the correct membership before recording a payment.")
        info.controls.append(ft.Text(
            "You can record a partial payment or use Pay Full Balance to fill the remaining amount. "
            "A receipt is generated after a successful payment.", color=self.MUTED, size=13))
 
    def build_outstanding_balance_page(self):
        self.outstanding_section = self.section(
            self.payment_panel, "Outstanding Balances", "Oldest membership start date first")
        self.refresh_outstanding_balances()
 
    def build_payment_history_page(self):
        p = self.payment_panel
        # Cards wrap onto a new line instead of being squeezed or clipped.
        self.payment_summary = ft.Row(spacing=8, wrap=True)
        p.controls.append(self.payment_summary)
 
        # One shared look for all four filter inputs.
        # Why the old version mismatched: entry() sets dense=True, and dense inputs skip
        # Flutter's 48px minimum height. height=48 then only grew the invisible box while
        # the drawn outline stayed ~36px. The dropdown was not dense, so it drew at 48.
        look = dict(
            width=FIELD_W, height=FIELD_H, dense=False, text_size=13,
            content_padding=pad_symmetric(horizontal=12, vertical=12),
            border=ft.OutlineInputBorder(border_radius=8, side=ft.BorderSide(1, "#71717A")),
        )
        # Labels default to 16px, which looked bigger than the dropdown's 13px text.
        label_style = ft.TextStyle(size=13)
 
        self.pfrom = ft.TextField(label="From date", hint_text="YYYY-MM-DD", label_style=label_style, **look)
        self.pto = ft.TextField(label="To date", hint_text="YYYY-MM-DD", label_style=label_style, **look)
        self.pcustomer = ft.TextField(label="Customer name", label_style=label_style, **look)
        self.pmethod = ft.Dropdown(
            options=[Opt(o) for o in (["All Methods"] + PAYMENT_METHODS)],
            value="All Methods", **look)
 
        apply_button = self.button("Apply Filters", self.refresh_payments, FIELD_W)
        show_all_button = self.button("Show All", self.clear_payment_filters, FIELD_W, self.SLATE)
        for control in (apply_button, show_all_button):
            control.width = FIELD_W
            control.height = FIELD_H
 
        p.controls.append(ft.Row(
            [self.pfrom, self.pto, self.pcustomer, self.pmethod,
             apply_button, show_all_button],
            spacing=8, wrap=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER))
        self.ptable = self.section(p, "Payment History", "Newest first")
        self.refresh_payments()
 
    def fill_full_balance(self):
        try:
            membership_id = int(self.pm.value.split(" - ", 1)[0])
            balance = self.manager.balance(membership_id)
        except (AttributeError, ValueError):
            return self.showwarning("Select membership", "Select a membership first.")
        self.pa.value = f"{balance:.2f}"
        self.page.update()
 
    def clear_payment_filters(self):
        for field in (self.pfrom, self.pto, self.pcustomer): field.value = ""
        self.pmethod.value = "All Methods"
        self.refresh_payments()
 
    def select_outstanding_membership(self, membership_id):
        # The Record Payment widgets are rebuilt when the subpage is shown.
        self.show_payment_subpage("Record Payment")
        option = self.payment_options.get(membership_id)
        if not option:
            self.showwarning("Membership unavailable",
                             "This membership could not be found. Refresh the Outstanding Balance page and try again.")
            return
        self.pm.value = option
        self.page.update()
        self._focus(self.pa)
 
    def refresh_outstanding_balances(self):
        rows = self.db.all("""SELECT m.id,c.full_name,m.plan_name,m.plan_price,m.start_date,
          COALESCE(p.paid,0) paid,MAX(m.plan_price-COALESCE(p.paid,0),0) balance
          FROM memberships m JOIN customers c ON c.id=m.customer_id
          LEFT JOIN (SELECT membership_id,SUM(amount) paid FROM payments WHERE voided=0 GROUP BY membership_id) p
          ON p.membership_id=m.id WHERE m.plan_price-COALESCE(p.paid,0)>0
          ORDER BY m.start_date,m.id""")
        data = [[row["full_name"], row["plan_name"], f"₱{row['plan_price']:,.2f}", f"₱{row['paid']:,.2f}",
                 f"₱{row['balance']:,.2f}", row["start_date"],
                 self.small_button("Pay", lambda mid=int(row["id"]): self.select_outstanding_membership(mid), 60)]
                for row in rows]
        self.outstanding_section.controls.append(self.table(
            ["Customer", "Plan", "Price", "Paid", "Balance", "Start Date", "Action"], data,
            "No outstanding balances."))
 
    def refresh_payment_summary(self, where_sql, params):
        self.payment_summary.controls.clear()
        today = date.today(); week_start = today - timedelta(days=today.weekday()); week_end = week_start + timedelta(days=7)
        month_start = today.replace(day=1)
        month_end = date(today.year+1, 1, 1) if today.month == 12 else date(today.year, today.month+1, 1)
        periods = [("Income Today", today.isoformat(), (today+timedelta(days=1)).isoformat()),
                   ("Income This Week", week_start.isoformat(), week_end.isoformat()),
                   ("Income This Month", month_start.isoformat(), month_end.isoformat())]
        summary = []
        for label, start, end in periods:
            total = self.db.one("SELECT COALESCE(SUM(amount),0) total FROM payments WHERE payment_date>=? AND payment_date<? AND voided=0", (start, end))["total"]
            summary.append((label, float(total)))
        # Voided payments stay in the history but never count toward totals.
        summary_where = (where_sql + " AND " if where_sql else " WHERE ") + "p.voided=0"
        method_totals = self.db.all(
            "SELECT p.method,SUM(p.amount) total FROM payments p JOIN customers c ON c.id=p.customer_id" +
            summary_where + " GROUP BY p.method", params)
        totals = {row["method"]: float(row["total"]) for row in method_totals}
        # GCash, Maya, and Bank Transfer remain separate methods in records and
        # filters, but their totals share one compact E-Wallet card.
        summary.extend(summarize_payment_methods(totals))
        for label, value in summary:
            self.payment_summary.controls.append(ft.Container(
                width=CARD_W, bgcolor=self.SURFACE, border_radius=8, padding=pad_all(9),
                content=ft.Column([self.T(label, 10, True, self.MUTED), self.T(f"₱{value:,.2f}", 15, True)],
                                  spacing=4)))
 
    def save_payment(self):
        try:
            mid = int(self.pm.value.split(" - ", 1)[0])
            m = self.db.one("SELECT customer_id,plan_name FROM memberships WHERE id=?", (mid,))
            if not m: raise ValueError("Select a valid membership.")
            amount = float(self.pa.value)
            if amount <= 0: raise ValueError("Payment amount must be greater than zero.")
            date.fromisoformat((self.pd.value or "").strip())
            amount_text, date_text, note_text, method_text = self.pa.value, self.pd.value, self.pn.value or "", self.pmeth.value
            summary = (f"Member: {self.db.one('SELECT full_name FROM customers WHERE id=?',(m['customer_id'],))['full_name']}\n"
                       f"Membership: {m['plan_name']} (ID {mid})\nAmount: ₱{amount:,.2f}\n"
                       f"Date: {date_text.strip()}\nMethod: {method_text}\n\nEnter your password to save this payment.")
            def authorized_save():
                try:
                    payment_id = self.manager.payment(m["customer_id"], mid, amount_text, date_text, note_text,
                                                      method=method_text, authorized=True)
                    self.payment_page(); self.page.update(); self.show_receipt(payment_id)
                except (ValueError, sqlite3.Error) as error:
                    self.showerror("Unable to save payment", str(error))
            self._confirm_with_password("Confirm Payment", authorized_save, summary, "Authenticate & Save")
        except (ValueError, sqlite3.Error, AttributeError, TypeError) as e:
            self.showerror("Unable to save payment", str(e))
 
    def refresh_payments(self):
        if not hasattr(self, "ptable"): return
        from_text = (self.pfrom.value or "").strip(); to_text = (self.pto.value or "").strip()
        for value in (from_text, to_text):
            if value:
                try: date.fromisoformat(value)
                except ValueError: return self.showerror("Invalid date", "Use YYYY-MM-DD.")
        if from_text and to_text and from_text > to_text:
            return self.showerror("Invalid date range", "From date must be on or before To date.")
        conditions = []; params = []
        if from_text: conditions.append("p.payment_date>=?"); params.append(from_text)
        if to_text: conditions.append("p.payment_date<=?"); params.append(to_text)
        customer = (self.pcustomer.value or "").strip()
        if customer:
            conditions.append("(c.full_name LIKE ? OR (c.first_name || ' ' || c.last_name) LIKE ?)")
            params.extend([f"%{customer}%"] * 2)
        method = (self.pmethod.value or "").strip()
        if method and method != "All Methods": conditions.append("p.method=?"); params.append(method)
        where_sql = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = self.db.all("""SELECT p.id,p.customer_id,p.membership_id,p.amount,p.payment_date,p.method,p.receipt_no,p.note,
          p.voided,p.void_reason,u.display_name staff_name,
          c.full_name,m.plan_name,m.plan_price,
          COALESCE((SELECT SUM(previous.amount) FROM payments previous
            WHERE previous.membership_id=p.membership_id AND previous.voided=0
              AND (previous.payment_date<p.payment_date
                   OR (previous.payment_date=p.payment_date AND previous.id<=p.id))),0) paid_at_payment
          FROM payments p JOIN customers c ON c.id=p.customer_id
          LEFT JOIN memberships m ON m.id=p.membership_id
          LEFT JOIN users u ON u.id=p.recorded_by""" + where_sql + " ORDER BY p.id DESC", tuple(params))
        self.filtered_payment_rows = rows
        self.ptable.controls.clear()
        self.refresh_payment_summary(where_sql, tuple(params))
        self.payment_records_table(rows)
        self.page.update()
 
    def payment_records_table(self, rows):
        owner = self.is_owner
        data = []
        for row in rows:
            payment_id = int(row["id"])
            voided = bool(row["voided"])
            balance = max(0.0, float(row["plan_price"] or 0) - float(row["paid_at_payment"]))
            amount = f"₱{row['amount']:,.2f}"
            cells = [
                row["id"], row["full_name"], row["plan_name"] or "—",
                # Voided payments stay visible: struck-through amount, red VOID note, no balance.
                ft.Text(amount, size=12, color=self.MUTED, text_align=ft.TextAlign.CENTER,
                        style=ft.TextStyle(decoration=ft.TextDecoration.LINE_THROUGH)) if voided else amount,
                row["payment_date"], row["method"], row["receipt_no"],
                "—" if voided else f"₱{balance:,.2f}",
                row["staff_name"] or "—",
                ft.Text(f"VOID: {row['void_reason']}", size=12, color=self.RED,
                        text_align=ft.TextAlign.CENTER) if voided else row["note"],
                self.small_button("View Receipt", lambda pid=payment_id: self.show_receipt(pid), 100)]
            if owner:  # only the owner can void payments
                cells.append(ft.Text("Voided", size=12, color=self.MUTED) if voided else
                             self.small_button("Void", lambda pid=payment_id: self.void_payment(pid), 62, self.RED))
            data.append(cells)
        self.ptable.controls.append(self.table(
            ["ID", "Customer", "Plan", "Amount", "Date", "Method", "Receipt No", "Balance After", "Recorded By",
             "Note", "Receipt"] + (["Void"] if owner else []), data, "No payment records found.",
            weights=[1, 3, 3, 3, 3, 2, 4, 3, 3, 3, 4] + ([3] if owner else [])))
 
    def show_receipt(self, payment_id):
        row = self.db.one("""SELECT p.id,p.receipt_no,p.payment_date,p.method,p.amount,c.full_name,m.plan_name,m.plan_price,
          p.voided,p.void_reason,u.display_name staff_name,
          COALESCE((SELECT SUM(previous.amount) FROM payments previous
            WHERE previous.membership_id=p.membership_id AND previous.voided=0
              AND (previous.payment_date<p.payment_date
                   OR (previous.payment_date=p.payment_date AND previous.id<=p.id))),0) paid_at_payment
          FROM payments p JOIN customers c ON c.id=p.customer_id
          LEFT JOIN memberships m ON m.id=p.membership_id
          LEFT JOIN users u ON u.id=p.recorded_by WHERE p.id=?""", (payment_id,))
        if not row: return self.showerror("Receipt unavailable", "Payment record not found.")
        balance = max(0.0, float(row["plan_price"] or 0) - float(row["paid_at_payment"]))
        details = {"Customer": row["full_name"], "Plan": row["plan_name"] or "—", "Receipt No": row["receipt_no"],
                   "Date": row["payment_date"], "Method": row["method"], "Amount Paid": f"₱{row['amount']:,.2f}",
                   "Balance After Payment": f"₱{balance:,.2f}", "Recorded By": row["staff_name"] or "—"}
        if row["voided"]:
            details["Status"] = "VOIDED"
            details["Void Reason"] = row["void_reason"]
 
        def save_receipt():
            # Native save dialog (tkinter is used only for the file chooser).
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk(); root.withdraw()
                try: root.attributes("-topmost", True)
                except Exception: pass
                path = filedialog.asksaveasfilename(
                    parent=root, defaultextension=".txt", filetypes=[("Text files", "*.txt")],
                    initialfile=f"{row['receipt_no']}.txt")
                root.destroy()
            except Exception as error:
                return self.showerror("Unable to save receipt", str(error))
            if not path: return
            try:
                with open(path, "w", encoding="utf-8") as receipt_file:
                    receipt_file.write("Krause Gym Management System\nPayment Receipt\n\n")
                    receipt_file.writelines(f"{label}: {value}\n" for label, value in details.items())
                self.showinfo("Receipt saved", "Receipt saved successfully.")
            except OSError as error:
                self.showerror("Unable to save receipt", str(error))
 
        lines = [ft.Row([self.T(label, color=self.MUTED), self.T(value, bold=True)],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN) for label, value in details.items()]
        dlg = ft.AlertDialog(
            modal=True, title=ft.Text("Payment Receipt", weight=ft.FontWeight.BOLD),
            content=ft.Container(ft.Column(lines, spacing=10, tight=True), width=400,
                                 bgcolor=self.SURFACE, border_radius=8, padding=14),
            actions=[self.button("Save as .txt", save_receipt, 145),
                     self.button("Close", lambda: self.close_dialog(dlg), 100, self.SLATE)])
        self.open_dialog(dlg)
 
    def void_payment(self, payment_id):
        row = self.db.one("SELECT p.amount,p.payment_date,p.method,p.receipt_no,p.voided,c.full_name FROM payments p JOIN customers c ON c.id=p.customer_id WHERE p.id=?", (payment_id,))
        if not row:
            return self.showerror("Payment not found", "This payment record no longer exists.")
        if row["voided"]:
            return self.showwarning("Already voided", "This payment has already been voided.")
        summary = (f"Void payment ID {payment_id}?\nMember: {row['full_name']}\nAmount: ₱{row['amount']:,.2f}\n"
                   f"Date: {row['payment_date']}\nMethod: {row['method']}\nReceipt: {row['receipt_no']}\n\n"
                   "The payment is NOT deleted: it stays in the history marked VOID with your name and the reason. "
                   "The membership balance and status are recalculated.")
        reason = ft.TextField(label="Reason for voiding (required)", width=450,
                              label_style=ft.TextStyle(size=13), **field_look())
 
        def go():
            text = " ".join((reason.value or "").split())
            if len(text) < 3:
                return self.showerror("Reason required", "Enter a short reason (at least 3 characters).")
            self.close_dialog(dlg)
            def do_void():
                try:
                    self.manager.void_payment(payment_id, text, authorized=True)
                    self.payment_page()
                    self.show_payment_subpage("Payment History")
                    self.showinfo("Payment voided",
                                  "The payment was voided. The membership balance and status were recalculated.")
                except (ValueError, sqlite3.Error) as error:
                    self.showerror("Unable to void payment", str(error))
            self._confirm_with_password("Authorize Void", do_void, summary, "Authenticate & Void")
 
        dlg = ft.AlertDialog(
            modal=True, title=ft.Text("Void Payment", weight=ft.FontWeight.BOLD),
            content=ft.Column([ft.Container(ft.Text(summary, color=self.TEXT), bgcolor=self.ACCENT_SOFT,
                                            border_radius=8, padding=10, width=450), reason],
                              tight=True, width=470, scroll=ft.ScrollMode.AUTO, spacing=10),
            actions=[self.button("Continue", go, 130),
                     self.button("Cancel", lambda: self.close_dialog(dlg), 100, self.SLATE)])
        self.open_dialog(dlg)
        self._focus(reason)
 
    # ------------------------------------------------------------------ accounts (owner only)
    def accounts_page(self):
        p = self.new_page("Accounts")
        if not self.is_owner:
            self.section(p, "Accounts", "Only the owner can manage accounts.")
            return
 
        def field(label, width, password=False):
            return ft.TextField(label=label, width=width, label_style=ft.TextStyle(size=13),
                                password=password, can_reveal_password=password, **field_look())
 
        f = self.section(p, "Add Staff Account",
                         "Staff can check members in, add customers, create memberships and record payments. "
                         "Only the owner can delete records, void payments and manage accounts.")
        self.sn = field("Staff name", 180)
        self.su = field("Username", 150)
        self.sp = field("Password (8+ characters)", 200, True)
        self.sc = field("Confirm password", 180, True)
        f.controls.append(ft.Row([self.sn, self.su, self.sp, self.sc, self.button("Add Staff", self.add_staff, 130)],
                                 wrap=True, spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER))
        self.accounts_table = self.section(p, "Accounts", "Disabled accounts cannot sign in but keep their history.")
        self.refresh_accounts()
 
    def add_staff(self):
        if (self.sp.value or "") != (self.sc.value or ""):
            return self.showerror("Passwords do not match", "Type the same password in both password fields.")
        try:
            self.manager.create_staff(self.sn.value or "", self.su.value or "", self.sp.value or "")
        except (ValueError, sqlite3.Error) as e:
            return self.showerror("Unable to add staff", str(e))
        for x in (self.sn, self.su, self.sp, self.sc): x.value = ""
        self.refresh_accounts()
        self.showinfo("Staff added", "The account is ready. Give the person their username and password.")
 
    def refresh_accounts(self):
        self.accounts_table.controls.clear()
        data = []
        for u in self.manager.users_list():
            is_active = bool(u["active"])
            data.append([
                u["display_name"], u["username"], u["role"].title(), "Active" if is_active else "Disabled",
                self.small_button("Reset", lambda uid=u["id"], n=u["display_name"]: self.reset_password(uid, n), 70),
                ft.Text("—", color=self.MUTED) if u["role"] == "owner" else
                self.small_button("Disable" if is_active else "Enable",
                                  lambda uid=u["id"], n=u["display_name"], a=is_active: self.toggle_account(uid, n, a),
                                  75, self.RED if is_active else None)])
        self.accounts_table.controls.append(self.table(
            ["Name", "Username", "Role", "Status", "Password", "Access"], data, "No accounts."))
        self.page.update()
 
    def reset_password(self, uid, name):
        def ask_new_password():
            new_pw = ft.TextField(label="New password (8+ characters)", width=420, label_style=ft.TextStyle(size=13),
                                  password=True, can_reveal_password=True, **field_look())
            confirm = ft.TextField(label="Confirm new password", width=420, label_style=ft.TextStyle(size=13),
                                   password=True, can_reveal_password=True, **field_look())
            def save():
                if (new_pw.value or "") != (confirm.value or ""):
                    return self.showerror("Passwords do not match", "Type the same password in both fields.")
                try:
                    self.manager.reset_password(uid, new_pw.value or "")
                except (ValueError, sqlite3.Error) as e:
                    return self.showerror("Unable to reset password", str(e))
                self.close_dialog(dlg)
                self.showinfo("Password changed", f"{name}'s password was changed.")
            dlg = ft.AlertDialog(
                modal=True, title=ft.Text(f"New password for {name}", weight=ft.FontWeight.BOLD),
                content=ft.Column([new_pw, confirm], tight=True, width=440, spacing=10),
                actions=[self.button("Save Password", save, 150),
                         self.button("Cancel", lambda: self.close_dialog(dlg), 100, self.SLATE)])
            self.open_dialog(dlg)
            self._focus(new_pw)
        self._confirm_with_password("Authorize Password Reset", ask_new_password,
                                    f"Change the password for {name}.", "Authenticate & Continue")
 
    def toggle_account(self, uid, name, is_active):
        verb = "Disable" if is_active else "Enable"
        def do():
            try:
                self.manager.set_user_active(uid, not is_active)
            except (ValueError, sqlite3.Error) as e:
                return self.showerror(f"Unable to {verb.lower()} account", str(e))
            self.refresh_accounts()
        self.askyesno(f"{verb} account",
                      f"{verb} the account for {name}?" + (" They will no longer be able to sign in." if is_active else ""),
                      do)
 
    # ------------------------------------------------------------------ attendance
    def attendance_page(self):
        p = self.new_page("Attendance")
        f = self.section(p, "Member Check-in / Check-out",
                         "Only active membership dates are checked. Unpaid or partial active memberships are allowed.")
        _, opts = self.choices()
        if not opts:
            f.controls.append(self.T("Add a customer first.", color=self.MUTED))
        else:
            self.ac = ft.Dropdown(options=[Opt(o) for o in opts], value=opts[0], width=360,
                                  **field_look())
            f.controls.append(ft.Row([self.ac, self.button("Check In", self.checkin, 95),
                                      self.button("Check Out", self.checkout, 100, self.MOSS)],
                                     wrap=True, spacing=8,
                                     vertical_alignment=ft.CrossAxisAlignment.CENTER))
        self.afilter = ft.TextField(label="YYYY-MM-DD or blank", width=190,
                                    label_style=ft.TextStyle(size=13), **field_look())
        p.controls.append(ft.Row([self.T("Filter date:"), self.afilter,
                                  self.button("Apply Filter", self.refresh_attendance, 105)],
                                 spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER))
        self.atable = self.section(p, "Attendance History", "Newest first")
        self.refresh_attendance()
 
    def checkin(self):
        try:
            self.manager.check_in(int(self.ac.value.split(" - ", 1)[0]))
            self.attendance_page(); self.page.update()
            self.showinfo("Success", "Member checked in.")
        except (ValueError, sqlite3.Error, AttributeError) as e:
            self.showerror("Check-in failed", str(e))
 
    def checkout(self):
        try:
            self.manager.check_out(int(self.ac.value.split(" - ", 1)[0]))
            self.attendance_page(); self.page.update()
            self.showinfo("Success", "Member checked out.")
        except (ValueError, sqlite3.Error, AttributeError) as e:
            self.showerror("Check-out failed", str(e))
 
    def refresh_attendance(self):
        if not hasattr(self, "atable"): return
        self.manager.close_stale_checkins()
        d = (self.afilter.value or "").strip() if hasattr(self, "afilter") else ""
        base = """SELECT a.*,c.full_name,u.display_name staff_name FROM attendance a
              JOIN customers c ON c.id=a.customer_id LEFT JOIN users u ON u.id=a.recorded_by"""
        if d:
            try: date.fromisoformat(d)
            except ValueError: return self.showerror("Invalid date", "Use YYYY-MM-DD.")
            rows = self.db.all(base + " WHERE substr(a.check_in,1,10)=? ORDER BY a.id DESC", (d,))
        else:
            rows = self.db.all(base + " ORDER BY a.id DESC LIMIT 250")
        self.atable.controls.clear()
        owner = self.is_owner
        data = []
        for x in rows:
            aid = int(x["id"])
            row = [x["id"], x["full_name"], x["check_in"].replace("T", " "), self.checkout_text(x),
                x["staff_name"] or "—"]
            if owner:  # only the owner can delete attendance records
                row.append(self.small_button("Delete", lambda a=aid: self.delete_attendance(a), 62, self.RED))
            data.append(row)
        self.atable.controls.append(self.table(
            ["ID", "Customer", "Check-in", "Check-out", "Checked In By"] + (["Delete"] if owner else []),
            data, "No attendance records found."))
        self.page.update()
 
    def delete_attendance(self, attendance_id):
        def do_delete():
            try:
                self.manager.delete_attendance(attendance_id)
                self.attendance_page(); self.page.update()
                self.showinfo("Deleted", "Attendance record deleted successfully.")
            except sqlite3.Error as e:
                self.showerror("Unable to delete attendance", str(e))
        self.askyesno("Confirm attendance deletion",
                      f"Delete attendance record ID {attendance_id}? This cannot be undone.", do_delete)
 
 
def main(page: ft.Page):
    GymApp(page)
 
 
if __name__ == "__main__":
    if hasattr(ft, "run"):
        ft.run(main)
    else:
        ft.app(target=main)
 
