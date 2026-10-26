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
    def __init__(self, db): self.db = db
    def add_customer(self, name, phone="", email=""):
        name = name.strip()
        if not name: raise ValueError("Customer name is required.")
        return self.db.run("INSERT INTO customers(full_name,phone,email) VALUES(?,?,?)",
                           (name, phone.strip(), email.strip()))
    def update_customer(self, cid, name, phone="", email=""):
        name = name.strip()
        if not name: raise ValueError("Customer name is required.")
        self.db.run("UPDATE customers SET full_name=?,phone=?,email=? WHERE id=?",
                    (name, phone.strip(), email.strip(), cid))
    def customers(self, query=""):
        p = f"%{query.strip()}%"
        return self.db.all("""SELECT c.*,
          (SELECT plan_name FROM memberships WHERE customer_id=c.id ORDER BY id DESC LIMIT 1) plan,
          (SELECT end_date FROM memberships WHERE customer_id=c.id ORDER BY id DESC LIMIT 1) end_date
          FROM customers c WHERE full_name LIKE ? OR phone LIKE ? OR email LIKE ?
          ORDER BY full_name COLLATE NOCASE""", (p,p,p))
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
                LEFT JOIN payments p ON p.membership_id = m.id
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
        # Preserve history: customers with any linked records cannot be deleted.
        related = self.db.one("""SELECT
            (SELECT COUNT(*) FROM memberships WHERE customer_id=?) +
            (SELECT COUNT(*) FROM payments WHERE customer_id=?) +
            (SELECT COUNT(*) FROM attendance WHERE customer_id=?) AS total""", (cid, cid, cid))["total"]
        if related:
            raise ValueError("This customer has membership, payment, or attendance history. Delete linked records first to preserve the records safely.")
        self.db.run("DELETE FROM customers WHERE id=?", (cid,))

    def delete_membership(self, mid):
        payments = self.db.one("SELECT COUNT(*) n FROM payments WHERE membership_id=?", (mid,))["n"]
        if payments:
            raise ValueError("This membership has payment records. Delete its payments first before deleting the membership.")
        self.db.run("DELETE FROM memberships WHERE id=?", (mid,))

    def delete_payment(self, payment_id, authorized=False):
        if not authorized:
            raise ValueError("Administrator authentication is required to delete a payment.")
        payment = self.db.one("SELECT membership_id FROM payments WHERE id=?", (payment_id,))
        if not payment:
            raise ValueError("Payment record not found.")
        mid = payment["membership_id"]
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM payments WHERE id=?", (payment_id,))
            if mid is not None:
                membership = con.execute(
                    "SELECT plan_price FROM memberships WHERE id=?", (mid,)
                ).fetchone()
                if membership:
                    paid = con.execute(
                        "SELECT COALESCE(SUM(amount),0) total FROM payments WHERE membership_id=?",
                        (mid,)
                    ).fetchone()["total"]
                    price = float(membership["plan_price"])
                    paid = float(paid)
                    status = "Paid" if paid >= price - .005 else ("Partial" if paid > 0 else "Unpaid")
                    con.execute("UPDATE memberships SET payment_status=? WHERE id=?", (status, mid))

    def delete_attendance(self, attendance_id):
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
        paid = self.db.one("SELECT COALESCE(SUM(amount),0) total FROM payments WHERE membership_id=?", (mid,))["total"]
        return max(0.0, float(m["plan_price"])-float(paid))
    def payment(self, cid, mid, amount, pay_date, note="", method="Cash", authorized=False):
        if not authorized:
            raise ValueError("Administrator authentication is required to record a payment.")
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
            membership = con.execute(
                "SELECT plan_price FROM memberships WHERE id=? AND customer_id=?", (mid,cid)
            ).fetchone()
            if not membership: raise ValueError("Select a valid membership.")
            already_paid = con.execute(
                "SELECT COALESCE(SUM(amount),0) total FROM payments WHERE membership_id=?", (mid,)
            ).fetchone()["total"]
            balance = max(0.0, float(membership["plan_price"])-float(already_paid))
            if amount > balance + .00001: raise ValueError(f"Payment exceeds balance ₱{balance:,.2f}.")
            cursor = con.execute(
                "INSERT INTO payments(customer_id,membership_id,amount,payment_date,method,note) VALUES(?,?,?,?,?,?)",
                (cid,mid,amount,payment_day.isoformat(),method,note.strip())
            )
            payment_id = cursor.lastrowid
            con.execute("UPDATE payments SET receipt_no=? WHERE id=?",
                        (receipt_number(payment_day.isoformat(), payment_id), payment_id))
            paid = float(already_paid)+amount
            price = float(membership["plan_price"])
            status = "Paid" if paid >= price-.005 else ("Partial" if paid > 0 else "Unpaid")
            con.execute("UPDATE memberships SET payment_status=? WHERE id=?", (status,mid))
        return payment_id
    def check_in(self, cid):
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
            con.execute("INSERT INTO attendance(customer_id,check_in) VALUES(?,?)",
                        (cid, datetime.now().isoformat(timespec="seconds")))
    def check_out(self, cid):
        r = self.db.one("SELECT id FROM attendance WHERE customer_id=? AND check_out IS NULL ORDER BY check_in DESC LIMIT 1",(cid,))
        if not r: raise ValueError("This customer does not have an open check-in.")
        self.db.run("UPDATE attendance SET check_out=? WHERE id=?",
                    (datetime.now().isoformat(timespec="seconds"),r["id"]))
    def stats(self):
        today = date.today().isoformat()
        return {
          "customers": self.db.one("SELECT COUNT(*) n FROM customers")["n"],
          "active": self.db.one("SELECT COUNT(DISTINCT customer_id) n FROM memberships WHERE start_date<=? AND end_date>=?",(today,today))["n"],
          "income": self.db.one("SELECT COALESCE(SUM(amount),0) n FROM payments WHERE payment_date=?",(today,))["n"],
          "checkins": self.db.one("SELECT COUNT(*) n FROM attendance WHERE substr(check_in,1,10)=?",(today,))["n"],
          "outstanding": self.db.one("""SELECT COALESCE(SUM(MAX(m.plan_price-COALESCE(p.paid,0),0)),0) n
            FROM memberships m LEFT JOIN (SELECT membership_id,SUM(amount) paid FROM payments GROUP BY membership_id) p
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
        self.payments_unlocked = False
        self.payment_last_activity = time.monotonic()
        self.payment_failed_attempts = 0
        self.payment_locked_until = 0.0
        self.payment_lockout_seconds = 30
        self.payment_inactivity_seconds = 5 * 60
        self._creating_membership = False
        self.shell_visible = False
        page.on_keyboard_event = lambda e: self._payment_activity()

        # ---- sidebar ----
        self.nav = {}
        nav_items = []
        for label, func in [("Dashboard", self.dashboard), ("Customers", self.customer_page),
                            ("Memberships", self.membership_page), ("Payments", self.payment_page),
                            ("Attendance", self.attendance_page)]:
            item = ft.Container(
                content=ft.Text(label, color="white", size=14, text_align=ft.TextAlign.LEFT),
                width=199,
                alignment=LEFT_ALIGNMENT,
                padding=pad_symmetric(vertical=12, horizontal=16),
                border_radius=9, ink=True, bgcolor=ft.Colors.TRANSPARENT,
                on_click=lambda e, n=label, f=func: self.navigate(n, f))
            self.nav[label] = item
            nav_items.append(item)
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
        self.logout_button = Btn("Logout", on_click=lambda e: self.logout(), width=150, height=52,
                                style=ft.ButtonStyle(bgcolor=self.RED, color="white",
                                                    padding=pad_symmetric(horizontal=20, vertical=12),
                                                    shape=ft.RoundedRectangleBorder(radius=8)))
        self.header = ft.Container(
            bgcolor=self.SURFACE, padding=pad_symmetric(horizontal=26, vertical=16),
            content=ft.Row([self.title_label, ft.Row([self.clock, self.logout_button], spacing=14)],
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

    def button(self, text, cmd, width=130, color=None):
        """Create a button with extra room so labels stay on one line across Flet versions."""
        def handler(e):
            self._payment_activity()
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
            self._payment_activity()
            cmd()
        return Btn(text, on_click=handler, width=width, height=31,
                   style=ft.ButtonStyle(bgcolor=color or self.GREEN, color="white",
                                        padding=pad_symmetric(horizontal=4),
                                        shape=ft.RoundedRectangleBorder(radius=8)))

    def table(self, headers, rows, empty="No records found."):
        """Equal-width columns. Any cell that is a Flet control is shown as-is (buttons)."""
        box = ft.Column(spacing=0, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        box.controls.append(ft.Container(
            bgcolor=self.SURFACE_ALT, padding=pad_symmetric(vertical=9, horizontal=5),
            content=ft.Row([ft.Text(h, expand=1, text_align=ft.TextAlign.CENTER, size=11,
                                    weight=ft.FontWeight.BOLD, color="#FDBA74") for h in headers],
                           spacing=4)))
        for n, row in enumerate(rows):
            cells = []
            for v in row:
                if isinstance(v, ft.Control):
                    cells.append(ft.Row([v], alignment=ft.MainAxisAlignment.CENTER, expand=1))
                else:
                    cells.append(ft.Text(str(v), expand=1, text_align=ft.TextAlign.CENTER,
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
    def show_login_page(self):
        self.shell_visible = False
        self.page.controls.clear()
        setup = self.db.one("SELECT id FROM payment_admin_auth WHERE id=1") is None
        user_entry = ft.TextField(width=340, height=44, dense=True,
                                  label="Choose administrator username" if setup else "Username")
        password_entry = ft.TextField(width=340, height=44, dense=True, password=True, can_reveal_password=True,
                                      label="Create password (8+ characters)" if setup else "Password")
        confirm_entry = ft.TextField(width=340, height=44, dense=True, password=True, can_reveal_password=True,
                                     label="Confirm password") if setup else None
        error_label = ft.Text("", color=self.RED, width=340)

        def set_error(text):
            error_label.value = text; self.page.update()

        def submit(_event=None):
            username = (user_entry.value or "").strip()
            password = password_entry.value or ""
            if setup:
                if len(username) < 3:
                    return set_error("Username must contain at least 3 characters.")
                if len(password) < 8:
                    return set_error("Password must contain at least 8 characters.")
                if password != (confirm_entry.value or ""):
                    return set_error("Passwords do not match.")
                salt = secrets.token_hex(16)
                digest = self._password_digest(password, salt)
                try:
                    self.db.run("INSERT INTO payment_admin_auth(id,username,password_salt,password_hash) VALUES(1,?,?,?)", (username,salt,digest))
                except sqlite3.Error as error:
                    return set_error(f"Could not create account: {error}")
                self._show_main_system()
            else:
                row = self.db.one("SELECT username FROM payment_admin_auth WHERE id=1")
                if not row or username != row["username"] or not self._verify_admin_password(password):
                    password_entry.value = ""
                    return set_error("Incorrect username or password.")
                self._show_main_system()

        password_entry.on_submit = submit
        if confirm_entry: confirm_entry.on_submit = submit
        user_entry.on_submit = submit

        fields = [user_entry, password_entry] + ([confirm_entry] if confirm_entry else [])
        card = ft.Container(
            bgcolor=self.SURFACE, border_radius=18, width=440, padding=pad_all(26),
            content=ft.Column([
                ft.Text("🏋  KRAUSE GYM", size=25, weight=ft.FontWeight.BOLD, color=self.GREEN),
                self.T("Create administrator account" if setup else "Administrator Login", 19, True),
                ft.Text("Set up your account to protect the entire system." if setup
                        else "Sign in to access the gym management system.", color=self.MUTED, width=340),
                *fields, error_label,
                self.button("Create Account" if setup else "Login", submit, 340)],
                spacing=8, tight=True, horizontal_alignment=ft.CrossAxisAlignment.CENTER))
        # Center a content-sized login card. Keep the parent full-screen, but do not
        # let the card's internal Column stretch and create a large empty lower area.
        alignment_cls = getattr(ft, "Alignment", None)
        centered_alignment = alignment_cls(0, 0) if alignment_cls else ft.alignment.center
        self.page.add(ft.Container(
            content=card, expand=True, alignment=centered_alignment,
            padding=pad_all(20)))
        self.page.update()
        try: user_entry.focus()
        except Exception: pass

    def _show_main_system(self):
        self.page.controls.clear()
        self.page.add(self.shell)
        self.shell_visible = True
        self.navigate("Dashboard", self.dashboard)

    def logout(self):
        self.payments_unlocked = False
        self.payment_last_activity = time.monotonic()
        self.show_login_page()

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
                    self._check_payment_timeout()
            except Exception:
                pass

    def navigate(self, name, func):
        if name != "Payments" and self.payments_unlocked:
            self.payments_unlocked = False
        for n, b in self.nav.items():
            b.bgcolor = self.DARK if n == name else ft.Colors.TRANSPARENT
        func()
        self.page.update()

    @staticmethod
    def _password_digest(password, salt):
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 310000).hex()

    def _verify_admin_password(self, password):
        row = self.db.one("SELECT password_salt,password_hash FROM payment_admin_auth WHERE id=1")
        if not row:
            return False
        candidate = self._password_digest(password, row["password_salt"])
        return hmac.compare_digest(candidate, row["password_hash"])

    def _payment_activity(self, _event=None):
        if self.payments_unlocked:
            self.payment_last_activity = time.monotonic()

    def _check_payment_timeout(self):
        if (self.payments_unlocked and self.title_label.value == "Payments"
                and time.monotonic() - self.payment_last_activity >= self.payment_inactivity_seconds):
            self.payments_unlocked = False
            self.navigate("Dashboard", self.dashboard)
            self.showinfo("Payments locked", "Payments was locked after 5 minutes of inactivity.")

    def _payment_password_prompt(self, title, on_success, summary=None, confirm_label="Continue", require_username=False):
        now = time.monotonic()
        if now < self.payment_locked_until:
            remaining = max(1, int(self.payment_locked_until - now))
            self.showwarning("Please wait", f"Too many incorrect passwords. Try again in {remaining} seconds.")
            return
        existing = self.db.one("SELECT id FROM payment_admin_auth WHERE id=1")
        setup_mode = existing is None

        username = None
        if setup_mode or require_username:
            username = ft.TextField(width=450, dense=True,
                                    label="Admin username" if setup_mode else "Username")
        password = ft.TextField(width=450, dense=True, password=True, can_reveal_password=True, label="Password")
        confirm = ft.TextField(width=450, dense=True, password=True, can_reveal_password=True,
                               label="Confirm password") if setup_mode else None

        intro = ("Create the administrator account that protects Payments. Save the password somewhere safe."
                 if setup_mode else "Enter the administrator password to authorize this action.")
        body = [ft.Text(intro, width=450)]
        if username: body.append(username)
        body.append(password)
        if confirm: body.append(confirm)
        if summary:
            body.append(ft.Container(ft.Text(summary, color=self.TEXT), bgcolor=self.ACCENT_SOFT,
                                     border_radius=8, padding=10, width=450))

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Set Up Payment Admin" if setup_mode else title, weight=ft.FontWeight.BOLD),
            content=ft.Column(body, tight=True, width=470, scroll=ft.ScrollMode.AUTO, spacing=10))

        def close(): self.close_dialog(dlg)

        def submit(_e=None):
            entered = password.value or ""
            if setup_mode:
                user = (username.value or "").strip()
                if len(user) < 3:
                    return self.showerror("Invalid username", "Username must contain at least 3 characters.")
                if len(entered) < 8:
                    return self.showerror("Weak password", "Use at least 8 characters for the administrator password.")
                if entered != (confirm.value or ""):
                    return self.showerror("Password mismatch", "The passwords do not match.")
                salt = secrets.token_hex(16)
                digest = self._password_digest(entered, salt)
                try:
                    self.db.run("INSERT INTO payment_admin_auth(id,username,password_salt,password_hash) VALUES(1,?,?,?)", (user,salt,digest))
                except sqlite3.Error as error:
                    return self.showerror("Setup failed", str(error))
                self.payment_failed_attempts = 0
                close()
                def after():
                    self.payments_unlocked = True; self.payment_last_activity = time.monotonic()
                    self._render_payment_page()
                self.showinfo("Admin created", "Administrator account created. Payments is now unlocked.", after)
                return
            stored_admin = self.db.one("SELECT username FROM payment_admin_auth WHERE id=1")
            username_ok = (not require_username) or (stored_admin is not None and (username.value or "").strip() == stored_admin["username"])
            if not username_ok or not self._verify_admin_password(entered):
                self.payment_failed_attempts += 1
                if self.payment_failed_attempts >= 5:
                    self.payment_locked_until = time.monotonic() + self.payment_lockout_seconds
                    self.payment_failed_attempts = 0
                    self.showerror("Temporarily locked", "Five incorrect attempts. Try again in 30 seconds.")
                else:
                    self.showerror("Incorrect password", f"Password incorrect. Attempts remaining: {5-self.payment_failed_attempts}.")
                password.value = ""
                self.page.update()
                return
            self.payment_failed_attempts = 0
            close()
            self.payment_last_activity = time.monotonic()
            on_success()

        password.on_submit = submit
        dlg.actions = [self.button(confirm_label, submit, 190), self.button("Cancel", close, 100, self.SLATE)]
        self.open_dialog(dlg)
        try: (username or password).focus()
        except Exception: pass

    # ------------------------------------------------------------------ dashboard
    def dashboard(self):
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
        rs = self.db.all("SELECT c.full_name,a.check_in,a.check_out FROM attendance a JOIN customers c ON c.id=a.customer_id ORDER BY a.id DESC LIMIT 8")
        sec.controls.append(self.table(["Member", "Check-in", "Check-out"],
            [(r["full_name"], r["check_in"].replace("T", " "),
              r["check_out"].replace("T", " ") if r["check_out"] else "Still inside") for r in rs]))

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

        def field(label, width):
            return ft.TextField(label=label, width=width,
                                label_style=ft.TextStyle(size=13), **field_look())

        f = self.section(p, "Customer Details", "Add a customer or select a row to edit")
        self.cn = field("Full name", 190)
        self.cp = field("Phone", 145)
        self.ce = field("Email (optional)", 200)
        f.controls.append(ft.Row([
            self.cn, self.cp, self.ce,
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

    def add_customer(self):
        try:
            name, phone, email = self.cn.value or "", self.cp.value or "", self.ce.value or ""
            cid = self.manager.add_customer(name, phone, email)
            for x in (self.cn, self.cp, self.ce): x.value = ""
            self.page.update()
            self.refresh_customers()
            self.open_new_member_dialog(cid, name.strip())
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
                               "Enter the administrator password to authorize and save this payment.")
                    self._payment_password_prompt(
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
            self.manager.update_customer(self.selected_customer, self.cn.value or "", self.cp.value or "", self.ce.value or "")
            self.refresh_customers()
            self.showinfo("Updated", "Customer details updated.")
        except (ValueError, sqlite3.Error) as e:
            self.showerror("Unable to update", str(e))

    def refresh_customers(self):
        if not hasattr(self, "ctable"): return
        self.ctable.controls.clear()
        rows = self.manager.customers(self.csearch.value or "" if hasattr(self, "csearch") else "")
        data = []
        for r in rows:
            data.append([r["id"], r["full_name"], r["phone"], r["email"], r["plan"] or "—", r["end_date"] or "—",
                         self.small_button("Select", lambda rr=r: self.select_customer(rr), 65),
                         self.small_button("Delete", lambda rr=r: self.delete_customer(rr["id"], rr["full_name"]), 65,
                                           self.RED)])
        self.ctable.controls.append(self.table(
            ["ID", "Name", "Phone", "Email", "Latest Plan", "End Date", "Select", "Delete"], data,
            "No customers found."))
        self.page.update()

    def delete_customer(self, cid, name):
        def do_delete():
            try:
                self.manager.delete_customer(cid)
                if self.selected_customer == cid:
                    self.selected_customer = None
                    for field in (self.cn, self.cp, self.ce): field.value = ""
                self.refresh_customers()
                self.showinfo("Deleted", "Customer deleted successfully.")
            except (ValueError, sqlite3.Error) as e:
                self.showerror("Unable to delete customer", str(e))
        self.askyesno("Confirm customer deletion",
                      f"Delete customer {name}? Customers with membership, payment, or attendance history cannot be deleted.",
                      do_delete)

    def select_customer(self, r):
        self.selected_customer = r["id"]
        for x, v in ((self.cn, r["full_name"]), (self.cp, r["phone"]), (self.ce, r["email"])):
            x.value = v or ""
        self.page.update()

    def choices(self):
        rows = self.db.all("SELECT id,full_name,phone FROM customers ORDER BY full_name COLLATE NOCASE")
        return rows, [f"{r['id']} - {r['full_name']} ({r['phone']})" for r in rows]

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
        rows = self.db.all("""SELECT m.*,c.full_name,COALESCE((SELECT SUM(amount) FROM payments WHERE membership_id=m.id),0) paid
          FROM memberships m JOIN customers c ON c.id=m.customer_id ORDER BY m.id DESC LIMIT 250""")
        today = date.today().isoformat(); data = []
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
            data.append([x["id"], x["full_name"], x["plan_name"], x["start_date"], x["end_date"],
                         f"₱{x['plan_price']:,.2f}", x["payment_status"], f"₱{bal:,.2f}", access,
                         self.small_button("Delete", lambda m=mid: self.delete_membership(m), 62, self.RED)])
        sec.controls.append(self.table(
            ["ID", "Customer", "Plan", "Start", "End", "Price", "Payment", "Balance", "Access", "Delete"],
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

    def _unlock_payments(self):
        self.payments_unlocked = True
        self.payment_last_activity = time.monotonic()
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
        self._payment_activity()
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
          COALESCE((SELECT SUM(amount) FROM payments WHERE membership_id=m.id),0) paid
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
        try: self.pa.focus()
        except Exception: pass

    def refresh_outstanding_balances(self):
        rows = self.db.all("""SELECT m.id,c.full_name,m.plan_name,m.plan_price,m.start_date,
          COALESCE(p.paid,0) paid,MAX(m.plan_price-COALESCE(p.paid,0),0) balance
          FROM memberships m JOIN customers c ON c.id=m.customer_id
          LEFT JOIN (SELECT membership_id,SUM(amount) paid FROM payments GROUP BY membership_id) p
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
            total = self.db.one("SELECT COALESCE(SUM(amount),0) total FROM payments WHERE payment_date>=? AND payment_date<?", (start, end))["total"]
            summary.append((label, float(total)))
        method_totals = self.db.all(
            "SELECT p.method,SUM(p.amount) total FROM payments p JOIN customers c ON c.id=p.customer_id" +
            where_sql + " GROUP BY p.method", params)
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
                       f"Date: {date_text.strip()}\nMethod: {method_text}\n\nEnter the administrator password to save this payment.")
            def authorized_save():
                try:
                    payment_id = self.manager.payment(m["customer_id"], mid, amount_text, date_text, note_text,
                                                      method=method_text, authorized=True)
                    self.payment_page(); self.page.update(); self.show_receipt(payment_id)
                except (ValueError, sqlite3.Error) as error:
                    self.showerror("Unable to save payment", str(error))
            self._payment_password_prompt("Confirm Payment", authorized_save, summary, "Authenticate & Save")
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
        if customer: conditions.append("c.full_name LIKE ?"); params.append(f"%{customer}%")
        method = (self.pmethod.value or "").strip()
        if method and method != "All Methods": conditions.append("p.method=?"); params.append(method)
        where_sql = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = self.db.all("""SELECT p.id,p.customer_id,p.membership_id,p.amount,p.payment_date,p.method,p.receipt_no,p.note,
          c.full_name,m.plan_name,m.plan_price,
          COALESCE((SELECT SUM(previous.amount) FROM payments previous
            WHERE previous.membership_id=p.membership_id
              AND (previous.payment_date<p.payment_date
                   OR (previous.payment_date=p.payment_date AND previous.id<=p.id))),0) paid_at_payment
          FROM payments p JOIN customers c ON c.id=p.customer_id
          LEFT JOIN memberships m ON m.id=p.membership_id""" + where_sql + " ORDER BY p.id DESC", tuple(params))
        self.filtered_payment_rows = rows
        self.ptable.controls.clear()
        self.refresh_payment_summary(where_sql, tuple(params))
        self.payment_records_table(rows)
        self.page.update()

    def payment_records_table(self, rows):
        data = []
        for row in rows:
            balance = max(0.0, float(row["plan_price"] or 0) - float(row["paid_at_payment"]))
            payment_id = int(row["id"])
            data.append([row["id"], row["full_name"], row["plan_name"] or "—", f"₱{row['amount']:,.2f}",
                         row["payment_date"], row["method"], row["receipt_no"], f"₱{balance:,.2f}", row["note"],
                         self.small_button("View Receipt", lambda pid=payment_id: self.show_receipt(pid), 100),
                         self.small_button("Delete", lambda pid=payment_id: self.delete_payment(pid), 62, self.RED)])
        self.ptable.controls.append(self.table(
            ["ID", "Customer", "Plan", "Amount", "Date", "Method", "Receipt No", "Balance After", "Note",
             "Receipt", "Delete"], data, "No payment records found."))

    def show_receipt(self, payment_id):
        row = self.db.one("""SELECT p.id,p.receipt_no,p.payment_date,p.method,p.amount,c.full_name,m.plan_name,m.plan_price,
          COALESCE((SELECT SUM(previous.amount) FROM payments previous
            WHERE previous.membership_id=p.membership_id
              AND (previous.payment_date<p.payment_date
                   OR (previous.payment_date=p.payment_date AND previous.id<=p.id))),0) paid_at_payment
          FROM payments p JOIN customers c ON c.id=p.customer_id
          LEFT JOIN memberships m ON m.id=p.membership_id WHERE p.id=?""", (payment_id,))
        if not row: return self.showerror("Receipt unavailable", "Payment record not found.")
        balance = max(0.0, float(row["plan_price"] or 0) - float(row["paid_at_payment"]))
        details = {"Customer": row["full_name"], "Plan": row["plan_name"] or "—", "Receipt No": row["receipt_no"],
                   "Date": row["payment_date"], "Method": row["method"], "Amount Paid": f"₱{row['amount']:,.2f}",
                   "Balance After Payment": f"₱{balance:,.2f}"}

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

    def delete_payment(self, payment_id):
        row = self.db.one("SELECT p.amount,p.payment_date,p.method,p.receipt_no,c.full_name FROM payments p JOIN customers c ON c.id=p.customer_id WHERE p.id=?", (payment_id,))
        if not row:
            return self.showerror("Payment not found", "This payment record no longer exists.")
        summary = (f"Delete payment ID {payment_id}?\nMember: {row['full_name']}\nAmount: ₱{row['amount']:,.2f}\n"
                   f"Date: {row['payment_date']}\nMethod: {row['method']}\nReceipt: {row['receipt_no']}\n\n"
                   "This permanently removes the payment and recalculates the membership balance and status.")
        def do_delete():
            try:
                self.manager.delete_payment(payment_id, authorized=True)
                self.payment_page()
                self.show_payment_subpage("Payment History")
                self.showinfo("Deleted", "Payment deleted. The membership balance and status were recalculated.")
            except (ValueError, sqlite3.Error) as error:
                self.showerror("Unable to delete payment", str(error))
        def authorized_delete():
            self.askyesno("Confirm payment deletion", summary + "\n\nAre you sure you want to delete it?", do_delete)
        self._payment_password_prompt("Authorize Payment Deletion", authorized_delete, summary, "Authenticate & Continue")

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
        d = (self.afilter.value or "").strip() if hasattr(self, "afilter") else ""
        if d:
            try: date.fromisoformat(d)
            except ValueError: return self.showerror("Invalid date", "Use YYYY-MM-DD.")
            rows = self.db.all("""SELECT a.*,c.full_name FROM attendance a JOIN customers c ON c.id=a.customer_id
              WHERE substr(check_in,1,10)=? ORDER BY a.id DESC""", (d,))
        else:
            rows = self.db.all("SELECT a.*,c.full_name FROM attendance a JOIN customers c ON c.id=a.customer_id ORDER BY a.id DESC LIMIT 250")
        self.atable.controls.clear()
        data = []
        for x in rows:
            aid = int(x["id"])
            data.append([x["id"], x["full_name"], x["check_in"].replace("T", " "),
                         x["check_out"].replace("T", " ") if x["check_out"] else "Still inside",
                         self.small_button("Delete", lambda a=aid: self.delete_attendance(a), 62, self.RED)])
        self.atable.controls.append(self.table(
            ["ID", "Customer", "Check-in", "Check-out", "Delete"], data, "No attendance records found."))
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