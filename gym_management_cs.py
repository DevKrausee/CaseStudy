import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk

APP_NAME = "Krause Gym Management System"
DB_PATH = Path(__file__).resolve().parent / "gym_management.db"
PLANS = {
    "Day Pass (Walk-in)": {"price": 50.0, "days": 1},
    "Weekly": {"price": 250.0, "days": 7},
    "Monthly": {"price": 800.0, "days": 30},
    "Quarterly": {"price": 2000.0, "days": 90},
}
ctk.set_appearance_mode("light")
ctk.set_default_color_theme("green")


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
                note TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS attendance(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                check_in TEXT NOT NULL, check_out TEXT);
            CREATE INDEX IF NOT EXISTS idx_customer_name ON customers(full_name);
            CREATE INDEX IF NOT EXISTS idx_membership_dates ON memberships(customer_id,start_date,end_date);
            CREATE INDEX IF NOT EXISTS idx_payment_date ON payments(payment_date);
            CREATE INDEX IF NOT EXISTS idx_attendance_checkin ON attendance(check_in);
            """)
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
        if plan not in PLANS: raise ValueError("Choose a valid membership plan.")
        try: start = date.fromisoformat(start_text)
        except ValueError: raise ValueError("Start date must use YYYY-MM-DD.")
        # Multiple memberships are allowed, including the same plan and start date.
        end = start + timedelta(days=PLANS[plan]["days"]-1)
        return self.db.run("""INSERT INTO memberships(customer_id,plan_name,start_date,end_date,plan_price)
          VALUES(?,?,?,?,?)""", (cid,plan,start.isoformat(),end.isoformat(),PLANS[plan]["price"]))
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

    def delete_payment(self, payment_id):
        payment = self.db.one("SELECT membership_id FROM payments WHERE id=?", (payment_id,))
        if not payment:
            raise ValueError("Payment record not found.")
        mid = payment["membership_id"]
        self.db.run("DELETE FROM payments WHERE id=?", (payment_id,))
        if mid is not None:
            membership = self.db.one("SELECT plan_price FROM memberships WHERE id=?", (mid,))
            if membership:
                paid = self.db.one("SELECT COALESCE(SUM(amount),0) total FROM payments WHERE membership_id=?", (mid,))["total"]
                price = float(membership["plan_price"])
                paid = float(paid)
                status = "Paid" if paid >= price - .005 else ("Partial" if paid > 0 else "Unpaid")
                self.db.run("UPDATE memberships SET payment_status=? WHERE id=?", (status, mid))

    def delete_attendance(self, attendance_id):
        self.db.run("DELETE FROM attendance WHERE id=?", (attendance_id,))

    def active_membership(self, cid, on_date=None):
        """Only paid/partially paid memberships within their dates grant access."""
        d = on_date or date.today().isoformat()
        return self.db.one("""SELECT m.* FROM memberships m
          WHERE m.customer_id=? AND m.start_date<=? AND m.end_date>=?
          AND COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.membership_id=m.id),0)>0
          ORDER BY m.end_date DESC,m.id DESC LIMIT 1""", (cid,d,d))
    def balance(self, mid):
        m = self.db.one("SELECT plan_price FROM memberships WHERE id=?", (mid,))
        if not m: return 0.0
        paid = self.db.one("SELECT COALESCE(SUM(amount),0) total FROM payments WHERE membership_id=?", (mid,))["total"]
        return max(0.0, float(m["plan_price"])-float(paid))
    def payment(self, cid, mid, amount, pay_date, note=""):
        amount = float(amount)
        if amount <= 0: raise ValueError("Payment amount must be greater than zero.")
        m = self.db.one("SELECT plan_price FROM memberships WHERE id=? AND customer_id=?", (mid,cid))
        if not m: raise ValueError("Select a valid membership.")
        balance = self.balance(mid)
        if amount > balance + .00001: raise ValueError(f"Payment exceeds balance ₱{balance:,.2f}.")
        try: date.fromisoformat(pay_date)
        except ValueError: raise ValueError("Payment date must use YYYY-MM-DD.")
        self.db.run("INSERT INTO payments(customer_id,membership_id,amount,payment_date,note) VALUES(?,?,?,?,?)",
                    (cid,mid,amount,pay_date,note.strip()))
        paid = float(m["plan_price"])-self.balance(mid)
        status = "Paid" if paid >= float(m["plan_price"])-.005 else ("Partial" if paid > 0 else "Unpaid")
        self.db.run("UPDATE memberships SET payment_status=? WHERE id=?", (status,mid))
    def check_in(self, cid):
        # Membership dates control access; payment status never blocks an active member.
        if not self.active_membership(cid):
            pending = self.db.one("""SELECT m.id FROM memberships m
              WHERE m.customer_id=? AND m.start_date<=? AND m.end_date>=?
              AND COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.membership_id=m.id),0)<=0
              ORDER BY m.end_date DESC LIMIT 1""", (cid, date.today().isoformat(), date.today().isoformat()))
            if pending:
                raise ValueError("Membership is pending payment. Record at least a partial payment before check-in.")
            raise ValueError("No paid/partially paid membership active today. Add or renew a membership first.")
        if self.db.one("SELECT id FROM attendance WHERE customer_id=? AND check_out IS NULL", (cid,)):
            raise ValueError("This customer is already checked in.")
        self.db.run("INSERT INTO attendance(customer_id,check_in) VALUES(?,?)",
                    (cid,datetime.now().isoformat(timespec="seconds")))
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


class GymApp(ctk.CTk):
    GREEN="#2e7d32"; DARK="#1b5e20"; LIGHT="#e8f5e9"; BG="#f5f7f5"; TEXT="#1f2933"; MUTED="#64748b"
    def __init__(self):
        super().__init__()
        self.title(APP_NAME); self.geometry("1240x780"); self.minsize(1050,680); self.configure(fg_color=self.BG)
        self.db=DatabaseManager(); self.manager=GymManager(self.db); self.selected_customer=None
        self.grid_columnconfigure(1,weight=1); self.grid_rowconfigure(0,weight=1)
        self.sidebar=ctk.CTkFrame(self,width=225,corner_radius=0,fg_color=self.GREEN)
        self.sidebar.grid(row=0,column=0,sticky="nsew"); self.sidebar.grid_propagate(False)
        ctk.CTkLabel(self.sidebar,text="🏋  KRAUSE GYM",font=ctk.CTkFont(size=20,weight="bold"),text_color="white").pack(anchor="w",padx=20,pady=(28,5))
        ctk.CTkLabel(self.sidebar,text="MANAGEMENT SYSTEM",font=ctk.CTkFont(size=10,weight="bold"),text_color="#d8f3dc").pack(anchor="w",padx=22,pady=(0,28))
        self.nav={}
        for label,func in [("Dashboard",self.dashboard),("Customers",self.customer_page),("Memberships",self.membership_page),("Payments",self.payment_page),("Attendance",self.attendance_page)]:
            b=ctk.CTkButton(self.sidebar,text=label,anchor="w",height=42,corner_radius=9,fg_color="transparent",hover_color=self.DARK,text_color="white",command=lambda n=label,f=func:self.navigate(n,f))
            b.pack(fill="x",padx=13,pady=4); self.nav[label]=b
        # Footer branding/technology text deliberately removed.
        self.main=ctk.CTkFrame(self,corner_radius=0,fg_color=self.BG); self.main.grid(row=0,column=1,sticky="nsew")
        self.main.grid_columnconfigure(0,weight=1); self.main.grid_rowconfigure(1,weight=1)
        self.header=ctk.CTkFrame(self.main,fg_color="white",corner_radius=0); self.header.grid(row=0,column=0,sticky="ew"); self.header.grid_columnconfigure(0,weight=1)
        self.title_label=ctk.CTkLabel(self.header,text="Dashboard",font=ctk.CTkFont(size=25,weight="bold"),text_color=self.TEXT)
        self.title_label.grid(row=0,column=0,sticky="w",padx=26,pady=22)
        self.clock=ctk.CTkLabel(self.header,text="",text_color=self.MUTED); self.clock.grid(row=0,column=1,padx=22)
        self.content=ctk.CTkScrollableFrame(self.main,fg_color=self.BG,corner_radius=0); self.content.grid(row=1,column=0,sticky="nsew",padx=18,pady=18)
        self.tick(); self.navigate("Dashboard",self.dashboard)
    def tick(self):
        self.clock.configure(text=datetime.now().strftime("%a, %d %b %Y  •  %I:%M:%S %p")); self.after(1000,self.tick)
    def navigate(self,name,func):
        for n,b in self.nav.items(): b.configure(fg_color=self.DARK if n==name else "transparent")
        func()
    def page(self,title):
        self.title_label.configure(text=title)
        for w in self.content.winfo_children(): w.destroy()
        return self.content
    def section(self,parent,title,subtitle=""):
        f=ctk.CTkFrame(parent,fg_color="white",corner_radius=13); f.pack(fill="x",pady=(0,15))
        ctk.CTkLabel(f,text=title,font=ctk.CTkFont(size=17,weight="bold"),text_color=self.TEXT).pack(anchor="w",padx=17,pady=(15,4))
        if subtitle: ctk.CTkLabel(f,text=subtitle,text_color=self.MUTED,font=ctk.CTkFont(size=12)).pack(anchor="w",padx=17,pady=(0,10))
        return f
    def entry(self,parent,placeholder,width=180):
        return ctk.CTkEntry(parent,placeholder_text=placeholder,width=width)
    def button(self,parent,text,cmd,width=110,color=None):
        return ctk.CTkButton(parent,text=text,command=cmd,width=width,height=35,corner_radius=8,fg_color=color or self.GREEN,hover_color=self.DARK)
    def table(self,parent,headers,rows):
        box=ctk.CTkFrame(parent,fg_color="white"); box.pack(fill="x",padx=14,pady=(0,15))
        h=ctk.CTkFrame(box,fg_color="#eef4ef"); h.pack(fill="x")
        # Keep columns balanced. The previous weighted sizing gave the Customer
        # column too much space and pushed the other values far apart.
        for i,x in enumerate(headers):
            h.grid_columnconfigure(i, weight=1, uniform="table")
            ctk.CTkLabel(h,text=x,text_color=self.DARK,font=ctk.CTkFont(size=11,weight="bold"),
                         anchor="center").grid(row=0,column=i,sticky="ew",padx=5,pady=9)
        for n,row in enumerate(rows):
            line=ctk.CTkFrame(box,fg_color="white" if n%2==0 else "#fafcfb")
            line.pack(fill="x")
            for i,x in enumerate(row):
                line.grid_columnconfigure(i, weight=1, uniform="table")
                ctk.CTkLabel(line,text=str(x),text_color=self.TEXT,anchor="center",
                             justify="center",wraplength=125).grid(
                    row=0,column=i,sticky="ew",padx=5,pady=8)
        if not rows: ctk.CTkLabel(box,text="No records found.",text_color=self.MUTED).pack(pady=14)
    def dashboard(self):
        p=self.page("Dashboard"); s=self.manager.stats(); cards=ctk.CTkFrame(p,fg_color="transparent"); cards.pack(fill="x",pady=(0,15))
        data=[("Total Customers",s["customers"]),("Active Memberships",s["active"]),("Today's Income",f"₱{s['income']:,.2f}"),("Outstanding Balance",f"₱{s['outstanding']:,.2f}"),("Today's Check-ins",s["checkins"])]
        for i,(lab,val) in enumerate(data):
            cards.grid_columnconfigure(i,weight=1,uniform="c"); f=ctk.CTkFrame(cards,fg_color="white",corner_radius=12); f.grid(row=0,column=i,sticky="nsew",padx=(0 if i==0 else 7,0))
            ctk.CTkLabel(f,text=lab,text_color=self.MUTED,font=ctk.CTkFont(size=11,weight="bold")).pack(anchor="w",padx=10,pady=(14,5))
            ctk.CTkLabel(f,text=str(val),text_color=self.TEXT,font=ctk.CTkFont(size=18,weight="bold")).pack(anchor="w",padx=10)
            ctk.CTkLabel(f,text="Overview",text_color=self.MUTED,font=ctk.CTkFont(size=10)).pack(anchor="w",padx=10,pady=(5,13))
        sec=self.section(p,"Recent Attendance","Latest visits")
        rs=self.db.all("SELECT c.full_name,a.check_in,a.check_out FROM attendance a JOIN customers c ON c.id=a.customer_id ORDER BY a.id DESC LIMIT 8")
        self.table(sec,["Member","Check-in","Check-out"],[(r["full_name"],r["check_in"].replace("T"," "),r["check_out"].replace("T"," ") if r["check_out"] else "Still inside") for r in rs])
        sec=self.section(p,"Membership Plans","Current standard rates")
        row=ctk.CTkFrame(sec,fg_color="transparent"); row.pack(fill="x",padx=10,pady=(0,13))
        for i,(name,pl) in enumerate(PLANS.items()):
            row.grid_columnconfigure(i,weight=1); f=ctk.CTkFrame(row,fg_color=self.LIGHT,corner_radius=9); f.grid(row=0,column=i,sticky="ew",padx=4)
            ctk.CTkLabel(f,text=name,text_color=self.DARK,font=ctk.CTkFont(weight="bold")).pack(padx=8,pady=(10,3))
            ctk.CTkLabel(f,text=f"₱{pl['price']:,.2f}",font=ctk.CTkFont(size=18,weight="bold")).pack(padx=8)
            ctk.CTkLabel(f,text=f"{pl['days']} day(s)",text_color=self.MUTED).pack(padx=8,pady=(2,10))
    def customer_page(self):
        p=self.page("Customers"); f=self.section(p,"Customer Details","Add a customer or select a row to edit")
        r=ctk.CTkFrame(f,fg_color="transparent"); r.pack(fill="x",padx=14,pady=(0,12))
        self.cn=self.entry(r,"Full name",190); self.cn.pack(side="left",padx=4,pady=4)
        self.cp=self.entry(r,"Phone",145); self.cp.pack(side="left",padx=4,pady=4)
        self.ce=self.entry(r,"Email (optional)",200); self.ce.pack(side="left",padx=4,pady=4)
        self.button(r,"Add Customer",self.add_customer,110).pack(side="left",padx=4)
        self.button(r,"Update Selected",self.update_customer,120,"#526b5a").pack(side="left",padx=4)
        sr=ctk.CTkFrame(p,fg_color="transparent"); sr.pack(fill="x",pady=(0,10))
        self.csearch=self.entry(sr,"Search name, phone, or email",290); self.csearch.pack(side="left")
        self.button(sr,"Search",self.refresh_customers,80).pack(side="left",padx=6)
        self.button(sr,"Show All",lambda:(self.csearch.delete(0,"end"),self.refresh_customers()),80,"#64748b").pack(side="left")
        self.ctable=self.section(p,"Customer List","Select a customer to load their details"); self.refresh_customers()
    def add_customer(self):
        try:
            name, phone, email = self.cn.get(), self.cp.get(), self.ce.get()
            cid = self.manager.add_customer(name, phone, email)
            for x in (self.cn,self.cp,self.ce): x.delete(0,"end")
            self.refresh_customers()
            self.open_new_member_dialog(cid, name.strip())
        except (ValueError,sqlite3.Error) as e:
            messagebox.showerror("Unable to add customer",str(e))

    def open_new_member_dialog(self, customer_id, customer_name):
        """After adding a customer, offer a modal membership and initial-payment workflow."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Set Up Membership & Payment")
        dialog.geometry("510x525")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        dialog.configure(fg_color=self.BG)

        ctk.CTkLabel(dialog, text="Customer Added Successfully",
            font=ctk.CTkFont(size=22, weight="bold"), text_color=self.TEXT).pack(anchor="w", padx=24, pady=(24, 4))
        ctk.CTkLabel(dialog, text=f"Set up a membership and record the first payment for {customer_name}.",
            text_color=self.MUTED, wraplength=455, justify="left").pack(anchor="w", padx=24, pady=(0, 18))

        ctk.CTkLabel(dialog, text="Membership plan", text_color=self.TEXT).pack(anchor="w", padx=24, pady=(4, 4))
        plan = ctk.CTkComboBox(dialog, values=list(PLANS.keys()), width=450)
        plan.set("Monthly")
        plan.pack(padx=24, pady=(0, 12))

        ctk.CTkLabel(dialog, text="Start date (YYYY-MM-DD)", text_color=self.TEXT).pack(anchor="w", padx=24, pady=(2, 4))
        start_entry = ctk.CTkEntry(dialog, width=450)
        start_entry.insert(0, date.today().isoformat())
        start_entry.pack(padx=24, pady=(0, 12))

        price_label = ctk.CTkLabel(dialog, text="Plan price: ₱800.00", text_color=self.DARK,
                                   font=ctk.CTkFont(size=15, weight="bold"))
        price_label.pack(anchor="w", padx=24, pady=(0, 8))

        ctk.CTkLabel(dialog, text="Initial payment (₱)", text_color=self.TEXT).pack(anchor="w", padx=24, pady=(0, 4))
        amount_entry = ctk.CTkEntry(dialog, width=450, placeholder_text="Enter 0 for no payment yet")
        amount_entry.insert(0, "0")
        amount_entry.pack(padx=24, pady=(0, 8))
        balance_label = ctk.CTkLabel(dialog, text="Balance after payment: ₱800.00", text_color=self.MUTED)
        balance_label.pack(anchor="w", padx=24, pady=(0, 12))

        def update_price(_=None):
            selected = plan.get()
            amount = PLANS.get(selected, {}).get("price", 0)
            price_label.configure(text=f"Plan price: ₱{amount:,.2f}")
            try:
                entered = max(0.0, float(amount_entry.get() or 0))
            except ValueError:
                entered = 0.0
            balance_label.configure(text=f"Balance after payment: ₱{max(0, amount-entered):,.2f}")
        def update_balance(_=None):
            update_price()
        plan.configure(command=update_price)
        amount_entry.bind("<KeyRelease>", update_balance)

        def save_setup():
            try:
                start_text = start_entry.get().strip()
                selected_plan = plan.get()
                payment_amount = float(amount_entry.get().strip() or 0)
                plan_price = PLANS[selected_plan]["price"]
                if payment_amount < 0:
                    raise ValueError("Payment cannot be negative.")
                if payment_amount > plan_price:
                    raise ValueError(f"Payment cannot exceed the plan price of ₱{plan_price:,.2f}.")
                membership_id = self.manager.add_membership(customer_id, selected_plan, start_text)
                if payment_amount > 0:
                    self.manager.payment(customer_id, membership_id, payment_amount, date.today().isoformat(), "Initial payment")
                dialog.destroy()
                self.refresh_customers()
                messagebox.showinfo("Setup saved", "Customer saved. Membership access is granted only if an initial payment was recorded and the membership dates are valid.")
            except (ValueError, KeyError, sqlite3.Error) as e:
                messagebox.showerror("Unable to save setup", str(e), parent=dialog)

        ctk.CTkLabel(dialog, text="No payment = Pending Payment (no check-in access).\nPartial or full payment = eligible for access during membership dates.",
            text_color=self.MUTED, justify="left", wraplength=450).pack(anchor="w", padx=24, pady=(0, 14))
        buttons = ctk.CTkFrame(dialog, fg_color="transparent")
        buttons.pack(fill="x", padx=24, pady=(0, 20))
        self.button(buttons, "Save Membership & Payment", save_setup, 235).pack(side="left", padx=(0, 8))
        self.button(buttons, "Skip for Now", dialog.destroy, 120, "#64748b").pack(side="left")
    def update_customer(self):
        if not self.selected_customer: return messagebox.showwarning("Select customer","Select a customer first.")
        try:
            self.manager.update_customer(self.selected_customer,self.cn.get(),self.cp.get(),self.ce.get()); self.refresh_customers()
            messagebox.showinfo("Updated","Customer details updated.")
        except (ValueError,sqlite3.Error) as e: messagebox.showerror("Unable to update",str(e))
    def refresh_customers(self):
        if not hasattr(self,"ctable") or not self.ctable.winfo_exists(): return
        for w in self.ctable.winfo_children()[1:]: w.destroy()
        rows=self.manager.customers(self.csearch.get() if hasattr(self,"csearch") else "")
        box=ctk.CTkFrame(self.ctable,fg_color="white"); box.pack(fill="x",padx=12,pady=(0,14))
        heads=["ID","Name","Phone","Email","Latest Plan","End Date","Select","Delete"]
        for i,h in enumerate(heads):
            box.grid_columnconfigure(i,weight=1,uniform="customer_table")
            ctk.CTkLabel(box,text=h,text_color=self.DARK,font=ctk.CTkFont(size=11,weight="bold"),
                         anchor="center").grid(row=0,column=i,sticky="ew",padx=4,pady=8)
        for n,r in enumerate(rows,1):
            vals=[r["id"],r["full_name"],r["phone"],r["email"],r["plan"] or "—",r["end_date"] or "—"]
            for i,v in enumerate(vals):
                ctk.CTkLabel(box,text=str(v),text_color=self.TEXT,anchor="center",
                             justify="center",wraplength=110).grid(row=n,column=i,sticky="ew",padx=4,pady=7)
            ctk.CTkButton(box,text="Select",width=55,height=27,command=lambda rr=r:self.select_customer(rr)).grid(row=n,column=6,padx=3,pady=3)
            ctk.CTkButton(box,text="Delete",width=58,height=27,fg_color="#b91c1c",hover_color="#991b1b",command=lambda rr=r:self.delete_customer(rr["id"],rr["full_name"])).grid(row=n,column=7,padx=3,pady=3)
        if not rows: ctk.CTkLabel(box,text="No customers found.",text_color=self.MUTED).grid(row=1,column=0,columnspan=8,pady=12)
    def delete_customer(self, cid, name):
        if not messagebox.askyesno("Confirm customer deletion", f"Delete customer {name}? Customers with membership, payment, or attendance history cannot be deleted."):
            return
        try:
            self.manager.delete_customer(cid)
            if self.selected_customer == cid:
                self.selected_customer = None
                for field in (self.cn, self.cp, self.ce): field.delete(0, "end")
            self.refresh_customers()
            messagebox.showinfo("Deleted", "Customer deleted successfully.")
        except (ValueError, sqlite3.Error) as e:
            messagebox.showerror("Unable to delete customer", str(e))

    def select_customer(self,r):
        self.selected_customer=r["id"]
        for x,v in ((self.cn,r["full_name"]),(self.cp,r["phone"]),(self.ce,r["email"])):
            x.delete(0,"end"); x.insert(0,v or "")
    def choices(self):
        rows=self.db.all("SELECT id,full_name,phone FROM customers ORDER BY full_name COLLATE NOCASE")
        return rows,[f"{r['id']} - {r['full_name']} ({r['phone']})" for r in rows]
    def membership_page(self):
        p=self.page("Memberships"); f=self.section(p,"Create / Renew Membership","End date is calculated automatically")
        _,opts=self.choices()
        if not opts: ctk.CTkLabel(f,text="Add a customer first.",text_color=self.MUTED).pack(anchor="w",padx=16,pady=(0,14))
        else:
            r=ctk.CTkFrame(f,fg_color="transparent"); r.pack(fill="x",padx=13,pady=(0,13))
            self.mc=ctk.CTkComboBox(r,values=opts,width=260); self.mc.pack(side="left",padx=4)
            self.mp=ctk.CTkComboBox(r,values=list(PLANS),width=190); self.mp.set("Monthly"); self.mp.pack(side="left",padx=4)
            self.ms=self.entry(r,"Start YYYY-MM-DD",165); self.ms.insert(0,date.today().isoformat()); self.ms.pack(side="left",padx=4)
            self.button(r,"Create Membership",self.create_membership,145).pack(side="left",padx=4)
        sec=self.section(p,"Membership Records","A partial or full payment is required for access, and membership dates must be valid.")
        rows=self.db.all("""SELECT m.*,c.full_name,COALESCE((SELECT SUM(amount) FROM payments WHERE membership_id=m.id),0) paid
          FROM memberships m JOIN customers c ON c.id=m.customer_id ORDER BY m.id DESC LIMIT 250""")
        today=date.today().isoformat(); data=[]
        for x in rows:
            bal=max(0,x["plan_price"]-x["paid"])
            if today < x["start_date"]:
                access = "Upcoming"
            elif today > x["end_date"]:
                access = "Expired"
            elif x["paid"] <= 0:
                access = "Pending Payment"
            else:
                access = "Active"
            data.append((x["id"],x["full_name"],x["plan_name"],x["start_date"],x["end_date"],f"₱{x['plan_price']:,.2f}",x["payment_status"],f"₱{bal:,.2f}",access))
        self.membership_records_table(sec, data)
    def membership_records_table(self, parent, rows):
        box=ctk.CTkFrame(parent,fg_color="white"); box.pack(fill="x",padx=12,pady=(0,14))
        headers=["ID","Customer","Plan","Start","End","Price","Payment","Balance","Access","Delete"]
        for i,h in enumerate(headers):
            box.grid_columnconfigure(i,weight=1,uniform="membership_table")
            ctk.CTkLabel(box,text=h,text_color=self.DARK,font=ctk.CTkFont(size=10,weight="bold"),anchor="center").grid(row=0,column=i,sticky="ew",padx=3,pady=8)
        for n,row in enumerate(rows,1):
            for i,value in enumerate(row):
                box.grid_columnconfigure(i,weight=1,uniform="membership_table")
                ctk.CTkLabel(box,text=str(value),text_color=self.TEXT,anchor="center",justify="center",wraplength=95).grid(row=n,column=i,sticky="ew",padx=3,pady=7)
            mid=int(row[0])
            ctk.CTkButton(box,text="Delete",width=55,height=26,fg_color="#b91c1c",hover_color="#991b1b",command=lambda m=mid:self.delete_membership(m)).grid(row=n,column=9,padx=2,pady=3)
        if not rows: ctk.CTkLabel(box,text="No membership records found.",text_color=self.MUTED).grid(row=1,column=0,columnspan=len(headers),pady=12)
    def delete_membership(self, mid):
        if not messagebox.askyesno("Confirm membership deletion", f"Delete membership ID {mid}? Any linked payments must be deleted first. This cannot be undone."):
            return
        try:
            self.manager.delete_membership(mid)
            self.membership_page()
            messagebox.showinfo("Deleted", "Membership deleted successfully.")
        except (ValueError, sqlite3.Error) as e:
            messagebox.showerror("Unable to delete membership", str(e))
    def create_membership(self):
        # Guard against rapid double-clicks while the handler is running.
        if getattr(self, "_creating_membership", False):
            return
        self._creating_membership = True
        try:
            cid = int(self.mc.get().split(" - ", 1)[0])
            plan_name = self.mp.get()
            start_text = self.ms.get().strip()
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
            if overlaps:
                customer = self.db.one("SELECT full_name FROM customers WHERE id=?", (cid,))
                existing = "\n".join(
                    f"• {row['plan_name']} ({row['start_date']} to {row['end_date']})"
                    for row in overlaps
                )
                proceed = messagebox.askyesno(
                    "Overlapping Membership",
                    f"{customer['full_name']} already has membership dates that overlap with this plan.\n\n"
                    f"Existing membership(s):\n{existing}\n\n"
                    f"New membership: {plan_name} ({start.isoformat()} to {proposed_end.isoformat()})\n\n"
                    "Do you want to create it anyway?",
                )
                if not proceed:
                    return

            self.manager.add_membership(cid, plan_name, start.isoformat())
            self.membership_page()
            messagebox.showinfo("Created", "Membership created. Record a partial or full payment to enable access.")
        except (ValueError, sqlite3.Error) as e:
            messagebox.showerror("Unable to create membership", str(e))
        finally:
            self._creating_membership = False
    def payment_page(self):
        p=self.page("Payments"); f=self.section(p,"Record Payment","Record full or partial payments")
        mem=self.db.all("""SELECT m.id,m.customer_id,c.full_name,m.plan_name,m.plan_price,
          COALESCE((SELECT SUM(amount) FROM payments WHERE membership_id=m.id),0) paid
          FROM memberships m JOIN customers c ON c.id=m.customer_id ORDER BY m.id DESC""")
        opts=[f"{x['id']} - {x['full_name']} | {x['plan_name']} | Balance ₱{max(0,x['plan_price']-x['paid']):,.2f}" for x in mem]
        if not opts: ctk.CTkLabel(f,text="Create a membership first.",text_color=self.MUTED).pack(anchor="w",padx=16,pady=(0,14))
        else:
            r=ctk.CTkFrame(f,fg_color="transparent"); r.pack(fill="x",padx=12,pady=(0,12))
            self.pm=ctk.CTkComboBox(r,values=opts,width=385); self.pm.pack(side="left",padx=4)
            self.pa=self.entry(r,"Amount ₱",115); self.pa.pack(side="left",padx=4)
            self.pd=self.entry(r,"Date YYYY-MM-DD",145); self.pd.insert(0,date.today().isoformat()); self.pd.pack(side="left",padx=4)
            self.pn=self.entry(r,"Note optional",130); self.pn.pack(side="left",padx=4)
            self.button(r,"Save Payment",self.save_payment,110).pack(side="left",padx=4)
        sr=ctk.CTkFrame(p,fg_color="transparent"); sr.pack(fill="x",pady=(0,8))
        ctk.CTkLabel(sr,text="Filter date:",text_color=self.TEXT).pack(side="left",padx=(0,6))
        self.pfilter=self.entry(sr,"YYYY-MM-DD or blank",180); self.pfilter.pack(side="left")
        self.button(sr,"Apply Filter",self.refresh_payments,100).pack(side="left",padx=6)
        self.ptable=self.section(p,"Payment History","Newest first"); self.refresh_payments()
    def save_payment(self):
        try:
            mid=int(self.pm.get().split(" - ",1)[0]); m=self.db.one("SELECT customer_id FROM memberships WHERE id=?",(mid,))
            self.manager.payment(m["customer_id"],mid,self.pa.get(),self.pd.get(),self.pn.get()); self.payment_page()
            messagebox.showinfo("Saved","Payment recorded successfully.")
        except (ValueError,sqlite3.Error) as e: messagebox.showerror("Unable to save payment",str(e))
    def refresh_payments(self):
        if not hasattr(self,"ptable") or not self.ptable.winfo_exists(): return
        for w in self.ptable.winfo_children()[1:]: w.destroy()
        d=self.pfilter.get().strip() if hasattr(self,"pfilter") else ""
        if d:
            try: date.fromisoformat(d)
            except ValueError: return messagebox.showerror("Invalid date","Use YYYY-MM-DD.")
            rows=self.db.all("""SELECT p.*,c.full_name,m.plan_name FROM payments p JOIN customers c ON c.id=p.customer_id
              LEFT JOIN memberships m ON m.id=p.membership_id WHERE payment_date=? ORDER BY p.id DESC""",(d,))
        else: rows=self.db.all("""SELECT p.*,c.full_name,m.plan_name FROM payments p JOIN customers c ON c.id=p.customer_id
              LEFT JOIN memberships m ON m.id=p.membership_id ORDER BY p.id DESC LIMIT 250""")
        self.payment_records_table([(x["id"],x["full_name"],x["plan_name"] or "—",f"₱{x['amount']:,.2f}",x["payment_date"],x["note"]) for x in rows])
    def payment_records_table(self, rows):
        box=ctk.CTkFrame(self.ptable,fg_color="white"); box.pack(fill="x",padx=12,pady=(0,14))
        headers=["ID","Customer","Plan","Amount","Date","Note","Delete"]
        for i,h in enumerate(headers):
            box.grid_columnconfigure(i,weight=1,uniform="payment_table")
            ctk.CTkLabel(box,text=h,text_color=self.DARK,font=ctk.CTkFont(size=10,weight="bold"),anchor="center").grid(row=0,column=i,sticky="ew",padx=4,pady=8)
        for n,row in enumerate(rows,1):
            for i,value in enumerate(row):
                ctk.CTkLabel(box,text=str(value),text_color=self.TEXT,anchor="center",justify="center",wraplength=110).grid(row=n,column=i,sticky="ew",padx=4,pady=7)
            pid=int(row[0])
            ctk.CTkButton(box,text="Delete",width=58,height=27,fg_color="#b91c1c",hover_color="#991b1b",command=lambda payment_id=pid:self.delete_payment(payment_id)).grid(row=n,column=6,padx=3,pady=3)
        if not rows: ctk.CTkLabel(box,text="No payment records found.",text_color=self.MUTED).grid(row=1,column=0,columnspan=len(headers),pady=12)
    def delete_payment(self, payment_id):
        if not messagebox.askyesno("Confirm payment deletion", f"Permanently delete payment ID {payment_id}? The membership balance and payment status will be recalculated."):
            return
        try:
            self.manager.delete_payment(payment_id)
            self.payment_page()
            messagebox.showinfo("Deleted", "Payment deleted. The membership balance and status were recalculated.")
        except (ValueError, sqlite3.Error) as e:
            messagebox.showerror("Unable to delete payment", str(e))
    def attendance_page(self):
        p=self.page("Attendance"); f=self.section(p,"Member Check-in / Check-out","Only active membership dates are checked. Unpaid or partial active memberships are allowed.")
        _,opts=self.choices()
        if not opts: ctk.CTkLabel(f,text="Add a customer first.",text_color=self.MUTED).pack(anchor="w",padx=16,pady=(0,14))
        else:
            r=ctk.CTkFrame(f,fg_color="transparent"); r.pack(fill="x",padx=12,pady=(0,12))
            self.ac=ctk.CTkComboBox(r,values=opts,width=330); self.ac.pack(side="left",padx=4)
            self.button(r,"Check In",self.checkin,95).pack(side="left",padx=4)
            self.button(r,"Check Out",self.checkout,95,"#526b5a").pack(side="left",padx=4)
        sr=ctk.CTkFrame(p,fg_color="transparent"); sr.pack(fill="x",pady=(0,8))
        ctk.CTkLabel(sr,text="Filter date:",text_color=self.TEXT).pack(side="left",padx=(0,6))
        self.afilter=self.entry(sr,"YYYY-MM-DD or blank",180); self.afilter.pack(side="left")
        self.button(sr,"Apply Filter",self.refresh_attendance,100).pack(side="left",padx=6)
        self.atable=self.section(p,"Attendance History","Newest first"); self.refresh_attendance()
    def checkin(self):
        try: self.manager.check_in(int(self.ac.get().split(" - ",1)[0])); self.attendance_page(); messagebox.showinfo("Success","Member checked in.")
        except (ValueError,sqlite3.Error) as e: messagebox.showerror("Check-in failed",str(e))
    def checkout(self):
        try: self.manager.check_out(int(self.ac.get().split(" - ",1)[0])); self.attendance_page(); messagebox.showinfo("Success","Member checked out.")
        except (ValueError,sqlite3.Error) as e: messagebox.showerror("Check-out failed",str(e))
    def refresh_attendance(self):
        if not hasattr(self,"atable") or not self.atable.winfo_exists(): return
        for w in self.atable.winfo_children()[1:]: w.destroy()
        d=self.afilter.get().strip() if hasattr(self,"afilter") else ""
        if d:
            try: date.fromisoformat(d)
            except ValueError: return messagebox.showerror("Invalid date","Use YYYY-MM-DD.")
            rows=self.db.all("""SELECT a.*,c.full_name FROM attendance a JOIN customers c ON c.id=a.customer_id
              WHERE substr(check_in,1,10)=? ORDER BY a.id DESC""",(d,))
        else: rows=self.db.all("SELECT a.*,c.full_name FROM attendance a JOIN customers c ON c.id=a.customer_id ORDER BY a.id DESC LIMIT 250")
        self.attendance_records_table([(x["id"],x["full_name"],x["check_in"].replace("T"," "),x["check_out"].replace("T"," ") if x["check_out"] else "Still inside") for x in rows])
    def attendance_records_table(self, rows):
        box=ctk.CTkFrame(self.atable,fg_color="white"); box.pack(fill="x",padx=12,pady=(0,14))
        headers=["ID","Customer","Check-in","Check-out","Delete"]
        for i,h in enumerate(headers):
            box.grid_columnconfigure(i,weight=1,uniform="attendance_table")
            ctk.CTkLabel(box,text=h,text_color=self.DARK,font=ctk.CTkFont(size=10,weight="bold"),anchor="center").grid(row=0,column=i,sticky="ew",padx=4,pady=8)
        for n,row in enumerate(rows,1):
            for i,value in enumerate(row):
                ctk.CTkLabel(box,text=str(value),text_color=self.TEXT,anchor="center",justify="center",wraplength=140).grid(row=n,column=i,sticky="ew",padx=4,pady=7)
            aid=int(row[0])
            ctk.CTkButton(box,text="Delete",width=58,height=27,fg_color="#b91c1c",hover_color="#991b1b",command=lambda attendance_id=aid:self.delete_attendance(attendance_id)).grid(row=n,column=4,padx=3,pady=3)
        if not rows: ctk.CTkLabel(box,text="No attendance records found.",text_color=self.MUTED).grid(row=1,column=0,columnspan=len(headers),pady=12)
    def delete_attendance(self, attendance_id):
        if not messagebox.askyesno("Confirm attendance deletion", f"Delete attendance record ID {attendance_id}? This cannot be undone."):
            return
        try:
            self.manager.delete_attendance(attendance_id)
            self.attendance_page()
            messagebox.showinfo("Deleted", "Attendance record deleted successfully.")
        except sqlite3.Error as e:
            messagebox.showerror("Unable to delete attendance", str(e))


if __name__ == "__main__":
    GymApp().mainloop()
