# -*- coding: utf-8 -*-
"""
بوت التحصيل وإدارة السدادات — شركة الحياة فارما
Single-file Telegram bot: config + SQLite DB layer + PDF generation + bot logic.

Requires an Arabic-capable TTF font at: fonts/NotoNaskhArabic-Regular.ttf
(Download "Noto Naskh Arabic" - Regular weight - from Google Fonts and place it there,
 or any other Arabic TTF font renamed to that filename.)
"""

import os
import re
import asyncio
import hashlib
import sqlite3
import logging
from datetime import datetime, timedelta, time as dt_time

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    PicklePersistence,
    filters,
)

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    HAVE_AR = True
except ImportError:
    HAVE_AR = False

from fpdf import FPDF

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8872405703:AAEIaRW2qsVW43TjIXoN-n1gDwYLE0MnRYM")
DB_PATH = os.environ.get("DB_PATH", "alhaya.db")
COMPANY_NAME = "شركة الحياة فارما"
BOT_TITLE = "💊 الحياة فارما – نظام التحصيل"
LOGO_PATH = os.environ.get("LOGO_PATH", "logo.png")  # optional, if provided
def _resolve_arabic_font_path():
    """يبحث عن خط عربي صالح بعدة طرق، دون الحاجة لرفعه يدوياً:
    1) ملف مرفوع يدوياً في fonts/NotoNaskhArabic-Regular.ttf (إن وُجد، له الأولوية دائماً).
    2) مسارات شائعة لحزمة الخط عند تثبيتها عبر النظام (نُثبّتها تلقائياً على السيرفر عبر nixpacks.toml).
    3) استعلام fontconfig (fc-match) عن أي خط يدعم العربية مثبّت على السيرفر.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    bundled = os.path.join(here, "fonts", "NotoNaskhArabic-Regular.ttf")
    if os.path.exists(bundled):
        return bundled

    common_paths = [
        "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoNaskhArabic-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
    ]
    for path in common_paths:
        if os.path.exists(path):
            return path

    try:
        import subprocess
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", ":lang=ar"],
            capture_output=True, text=True, timeout=5,
        )
        found = result.stdout.strip()
        if found and os.path.exists(found):
            return found
    except Exception:
        pass

    return bundled  # لم يُعثر على شيء؛ سيظهر تنبيه واضح للمستخدم لاحقاً


FONT_PATH = _resolve_arabic_font_path()
SESSION_TIMEOUT_MINUTES = 30

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("alhaya-bot")

PAYMENT_METHODS = [
    "💵 كاش",
    "🏦 مصرف التجارة والتنمية",
    "🏦 مصرف الوحدة",
    "🏦 مصرف الجمهورية",
    "🏦 المصرف الإسلامي",
    "🏦 مصرف المتوسط",
    "🏦 المصرف التجاري الوطني",
    "🏦 مصرف الصحاري",
]

MONTHS_AR = [
    "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
    "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر",
]

PERMISSIONS = {
    "view_representatives": "👥 مشاهدة المندوبين",
    "view_payments": "💰 مشاهدة الجباية",
    "search_customers": "🔍 البحث عن العملاء",
    "view_reports": "📊 مشاهدة التقارير",
    "export_pdf": "📄 تصدير PDF",
    "send_messages": "📩 إرسال رسائل",
    "add_representatives": "➕ إضافة مندوبين",
    "edit_representatives": "✏️ تعديل المندوبين",
    "delete_representatives": "🗑️ حذف المندوبين",
    "manage_targets": "🎯 تحديد الأهداف",
    "edit_payments": "✏️ تعديل/حذف السدادات",
    "collect_payments": "💰 تسجيل عمليات تحصيل",
    "receive_feedback": "📢 استلام بلاغ/فكرة تطوير",
    "view_rep_status": "📶 حالة المندوبين",
    "manage_home_target": "🏠 هدف Home Use",
    "manage_professional_target": "🩺 هدف Professional Use",
    "manage_expenses": "💵 إدارة المصاريف",
    "manage_payroll": "💼 إدارة الرواتب",
}
DEFAULT_ON_PERMS = {"view_representatives", "view_payments", "search_customers", "view_reports", "export_pdf", "send_messages"}

# ============================================================
# DATABASE LAYER
# ============================================================

def _dict_row_factory(cursor, row):
    """يحوّل كل صف مُسترجَع من قاعدة البيانات إلى dict عادي بدل sqlite3.Row،
    حتى تبقى بيانات الجلسة (context.user_data) قابلة للحفظ الدائم (pickling)
    عبر تحديثات البوت المتكررة دون فقدان تسجيل الدخول أو أي حالة مؤقتة."""
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = _dict_row_factory
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','assistant','representative')),
            active INTEGER NOT NULL DEFAULT 1,
            telegram_id INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            last_seen TEXT,
            category TEXT NOT NULL DEFAULT 'home'
        )
    """)
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_seen TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN category TEXT NOT NULL DEFAULT 'home'")
    except sqlite3.OperationalError:
        pass
    c.execute("""
        CREATE TABLE IF NOT EXISTS assistant_permissions (
            user_id INTEGER NOT NULL,
            perm_key TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, perm_key),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            amount REAL NOT NULL,
            method TEXT NOT NULL,
            payment_date TEXT NOT NULL,
            representative_id INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(representative_id) REFERENCES users(id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            representative_id INTEGER NOT NULL,
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            target_amount REAL NOT NULL,
            UNIQUE(representative_id, month, year),
            FOREIGN KEY(representative_id) REFERENCES users(id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS category_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            target_amount REAL NOT NULL,
            UNIQUE(category, month, year)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER,
            recipient_id INTEGER,
            recipient_type TEXT NOT NULL,
            body TEXT NOT NULL,
            sent_at TEXT DEFAULT (datetime('now'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS feedback_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER,
            sender_name TEXT,
            sender_role TEXT,
            body TEXT NOT NULL,
            sent_at TEXT DEFAULT (datetime('now')),
            replied INTEGER NOT NULL DEFAULT 0,
            reply_body TEXT,
            replied_by INTEGER,
            replied_by_name TEXT,
            replied_at TEXT
        )
    """)
    # ترقية آمنة لقاعدة بيانات قديمة كانت موجودة قبل إضافة أعمدة الرد (لا تؤثر إذا كانت الأعمدة موجودة أصلاً)
    for ddl in (
        "ALTER TABLE feedback_messages ADD COLUMN reply_body TEXT",
        "ALTER TABLE feedback_messages ADD COLUMN replied_by INTEGER",
        "ALTER TABLE feedback_messages ADD COLUMN replied_by_name TEXT",
        "ALTER TABLE feedback_messages ADD COLUMN replied_at TEXT",
    ):
        try:
            c.execute(ddl)
        except sqlite3.OperationalError:
            pass
    c.execute("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            occurred_at TEXT DEFAULT (datetime('now'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_activity_user_time ON activity_log(user_id, occurred_at)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount REAL NOT NULL,
            description TEXT NOT NULL,
            expense_date TEXT NOT NULL,
            attribution_type TEXT NOT NULL CHECK(attribution_type IN ('representative','assistant','department','bonus')),
            attribution_id INTEGER,
            attribution_name TEXT,
            created_by INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    # ترقية آمنة: إذا كان جدول expenses موجوداً مسبقاً بقيد CHECK قديم لا يسمح بـ 'bonus'، نعيد بناءه بأمان مع حفظ البيانات
    row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='expenses'").fetchone()
    if row and "bonus" not in row["sql"]:
        c.execute("ALTER TABLE expenses RENAME TO expenses_old_migrate")
        c.execute("""
            CREATE TABLE expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount REAL NOT NULL,
                description TEXT NOT NULL,
                expense_date TEXT NOT NULL,
                attribution_type TEXT NOT NULL CHECK(attribution_type IN ('representative','assistant','department','bonus')),
                attribution_id INTEGER,
                attribution_name TEXT,
                created_by INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        c.execute("""
            INSERT INTO expenses (id, amount, description, expense_date, attribution_type, attribution_id, attribution_name, created_by, created_at)
            SELECT id, amount, description, expense_date, attribution_type, attribution_id, attribution_name, created_by, created_at FROM expenses_old_migrate
        """)
        c.execute("DROP TABLE expenses_old_migrate")
    c.execute("""
        CREATE TABLE IF NOT EXISTS payroll_employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            emp_type TEXT NOT NULL CHECK(emp_type IN ('fixed','commission')),
            fixed_amount REAL,
            commission_rate REAL,
            linked_rep_id INTEGER,
            classification TEXT,
            retained_balance REAL NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    try:
        c.execute("ALTER TABLE payroll_employees ADD COLUMN classification TEXT")
    except sqlite3.OperationalError:
        pass
    c.execute("""
        CREATE TABLE IF NOT EXISTS payroll_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            period_month INTEGER,
            period_year INTEGER,
            collected_total REAL,
            gross_amount REAL NOT NULL,
            retained_amount REAL NOT NULL DEFAULT 0,
            paid_amount REAL NOT NULL,
            kind TEXT NOT NULL DEFAULT 'payout' CHECK(kind IN ('payout','retention_release')),
            created_by INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def hash_password(password: str, salt: str = "alhaya_pharma_salt") -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    return hash_password(password) == password_hash


def any_admin_exists() -> bool:
    conn = get_db()
    row = conn.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone()
    conn.close()
    return row is not None


def create_user(name, username, password, role):
    conn = get_db()
    try:
        c = conn.execute(
            "INSERT INTO users (name, username, password_hash, role) VALUES (?,?,?,?)",
            (name, username, hash_password(password), role),
        )
        conn.commit()
        return c.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def get_user_by_username(username):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    return row


def get_user_by_password(password):
    """يبحث عن حساب نشط برقم سري مطابق، لدعم تسجيل الدخول بالرقم السري فقط."""
    conn = get_db()
    h = hash_password(password)
    row = conn.execute("SELECT * FROM users WHERE password_hash=? AND active=1", (h,)).fetchone()
    conn.close()
    return row


def password_in_use(password, exclude_user_id=None):
    """يتحقق أن الرقم السري غير مستخدم من حساب آخر، حتى يبقى كل رقم سري فريداً للتعرف عليه عند الدخول."""
    conn = get_db()
    h = hash_password(password)
    if exclude_user_id:
        row = conn.execute("SELECT id FROM users WHERE password_hash=? AND id!=?", (h, exclude_user_id)).fetchone()
    else:
        row = conn.execute("SELECT id FROM users WHERE password_hash=?", (h,)).fetchone()
    conn.close()
    return row is not None


def get_user(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return row


def set_telegram_id(user_id, telegram_id):
    conn = get_db()
    conn.execute("UPDATE users SET telegram_id=? WHERE id=?", (telegram_id, user_id))
    conn.commit()
    conn.close()


def update_last_seen(user_id):
    conn = get_db()
    conn.execute("UPDATE users SET last_seen=datetime('now') WHERE id=?", (user_id,))
    conn.commit()
    conn.close()


def log_activity(user_id):
    conn = get_db()
    conn.execute("INSERT INTO activity_log (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()


def get_week_range(end_date=None):
    """نافذة أسبوع متدحرجة من 7 أيام تنتهي بتاريخ end_date (اليوم افتراضياً)، شاملة الطرفين."""
    end = end_date or datetime.now().date()
    start = end - timedelta(days=6)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def get_rep_week_stats(rep_id, start_date, end_date):
    conn = get_db()
    payments = conn.execute(
        "SELECT amount FROM payments WHERE representative_id=? AND payment_date BETWEEN ? AND ?",
        (rep_id, start_date, end_date),
    ).fetchall()
    activity = conn.execute(
        "SELECT DISTINCT date(occurred_at) as d FROM activity_log WHERE user_id=? AND date(occurred_at) BETWEEN ? AND ?",
        (rep_id, start_date, end_date),
    ).fetchall()
    interactions = conn.execute(
        "SELECT COUNT(*) as c FROM activity_log WHERE user_id=? AND date(occurred_at) BETWEEN ? AND ?",
        (rep_id, start_date, end_date),
    ).fetchone()
    conn.close()
    total = sum(p["amount"] for p in payments)
    active_days = len(activity)
    return {
        "total": total,
        "payment_count": len(payments),
        "interactions": interactions["c"] if interactions else 0,
        "active_days": active_days,
        "away_days": max(7 - active_days, 0),
    }


def build_weekly_report_data(category, start_date, end_date):
    reps = [u for u in list_users_by_role("representative", active_only=True) if u["category"] == category]
    stats = []
    for rep in reps:
        s = get_rep_week_stats(rep["id"], start_date, end_date)
        s["name"] = rep["name"]
        stats.append(s)
    stats.sort(key=lambda x: x["total"], reverse=True)
    overall_total = sum(s["total"] for s in stats)
    return stats, overall_total


EXPENSE_DEPARTMENTS = [
    "🏢 إيجار ومرافق",
    "📦 استيراد وشحن وجمارك",
    "🚗 نقل ومواصلات",
    "💼 رواتب وأجور",
    "📢 تسويق وإعلان",
    "🛠️ صيانة ومستلزمات مكتبية",
    "📋 مصاريف إدارية أخرى",
]


def add_expense(amount, description, expense_date, attribution_type, attribution_id, attribution_name, created_by):
    conn = get_db()
    c = conn.execute(
        "INSERT INTO expenses (amount, description, expense_date, attribution_type, attribution_id, attribution_name, created_by) "
        "VALUES (?,?,?,?,?,?,?)",
        (amount, description, expense_date, attribution_type, attribution_id, attribution_name, created_by),
    )
    conn.commit()
    eid = c.lastrowid
    conn.close()
    return eid


def get_expenses(attribution_type=None, attribution_id=None, attribution_name=None, month=None, year=None):
    conn = get_db()
    q = "SELECT * FROM expenses WHERE 1=1"
    params = []
    if attribution_type:
        q += " AND attribution_type=?"
        params.append(attribution_type)
    if attribution_id is not None:
        q += " AND attribution_id=?"
        params.append(attribution_id)
    if attribution_name is not None:
        q += " AND attribution_name=?"
        params.append(attribution_name)
    if month and year:
        q += " AND strftime('%m', expense_date)=? AND strftime('%Y', expense_date)=?"
        params += [f"{month:02d}", str(year)]
    q += " ORDER BY expense_date DESC, id DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return rows


def delete_expense(expense_id):
    conn = get_db()
    conn.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
    conn.commit()
    conn.close()


def get_total_expenses(rows):
    return sum(r["amount"] for r in rows)


RETENTION_RATE = 0.01  # 1% كنترول داخلي يُحتجز من كل عمولة صرف

CLASSIFICATION_LABELS = {
    "admin": "🗂️ إداري",
    "sales_rep": "🧑‍💼 مندوب مبيعات",
    "medical_rep": "💊 مندوب طبي",
    "collaborator": "🤝 متعاون",
}


def add_payroll_employee(name, emp_type, fixed_amount=None, commission_rate=None, linked_rep_id=None, classification=None):
    conn = get_db()
    c = conn.execute(
        "INSERT INTO payroll_employees (name, emp_type, fixed_amount, commission_rate, linked_rep_id, classification) VALUES (?,?,?,?,?,?)",
        (name, emp_type, fixed_amount, commission_rate, linked_rep_id, classification),
    )
    conn.commit()
    eid = c.lastrowid
    conn.close()
    return eid


def list_payroll_employees(active_only=True):
    conn = get_db()
    q = "SELECT * FROM payroll_employees"
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY name"
    rows = conn.execute(q).fetchall()
    conn.close()
    return rows


def get_payroll_employee(employee_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM payroll_employees WHERE id=?", (employee_id,)).fetchone()
    conn.close()
    return row


def update_payroll_employee_amount(employee_id, emp_type, value):
    conn = get_db()
    if emp_type == "fixed":
        conn.execute("UPDATE payroll_employees SET fixed_amount=? WHERE id=?", (value, employee_id))
    else:
        conn.execute("UPDATE payroll_employees SET commission_rate=? WHERE id=?", (value, employee_id))
    conn.commit()
    conn.close()


def delete_payroll_employee(employee_id):
    conn = get_db()
    conn.execute("DELETE FROM payroll_employees WHERE id=?", (employee_id,))
    conn.execute("DELETE FROM payroll_payments WHERE employee_id=?", (employee_id,))
    conn.commit()
    conn.close()


def add_to_retained_balance(employee_id, amount):
    conn = get_db()
    conn.execute("UPDATE payroll_employees SET retained_balance = retained_balance + ? WHERE id=?", (amount, employee_id))
    conn.commit()
    conn.close()


def release_retained_balance(employee_id):
    conn = get_db()
    conn.execute("UPDATE payroll_employees SET retained_balance = 0 WHERE id=?", (employee_id,))
    conn.commit()
    conn.close()


def add_payroll_payment(employee_id, period_month, period_year, collected_total, gross_amount, retained_amount, paid_amount, created_by, kind="payout"):
    conn = get_db()
    c = conn.execute(
        "INSERT INTO payroll_payments (employee_id, period_month, period_year, collected_total, gross_amount, retained_amount, paid_amount, kind, created_by) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (employee_id, period_month, period_year, collected_total, gross_amount, retained_amount, paid_amount, kind, created_by),
    )
    conn.commit()
    pid = c.lastrowid
    conn.close()
    return pid


def get_payroll_payments(employee_id=None, month=None, year=None):
    conn = get_db()
    q = """SELECT pp.*, pe.name as employee_name, pe.emp_type FROM payroll_payments pp
           JOIN payroll_employees pe ON pe.id = pp.employee_id WHERE 1=1"""
    params = []
    if employee_id:
        q += " AND pp.employee_id=?"
        params.append(employee_id)
    if month and year:
        q += " AND pp.period_month=? AND pp.period_year=?"
        params += [month, year]
    q += " ORDER BY pp.created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return rows


def list_users_by_role(role, active_only=False):
    conn = get_db()
    q = "SELECT * FROM users WHERE role=?"
    if active_only:
        q += " AND active=1"
    q += " ORDER BY name"
    rows = conn.execute(q, (role,)).fetchall()
    conn.close()
    return rows


def update_user_name(user_id, name):
    conn = get_db()
    conn.execute("UPDATE users SET name=? WHERE id=?", (name, user_id))
    conn.commit()
    conn.close()


def update_user_password(user_id, new_password):
    conn = get_db()
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(new_password), user_id))
    conn.commit()
    conn.close()


def set_user_active(user_id, active: int):
    conn = get_db()
    conn.execute("UPDATE users SET active=? WHERE id=?", (active, user_id))
    conn.commit()
    conn.close()


def delete_user(user_id):
    conn = get_db()
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.execute("DELETE FROM assistant_permissions WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def get_permissions(user_id):
    conn = get_db()
    rows = conn.execute("SELECT perm_key, enabled FROM assistant_permissions WHERE user_id=?", (user_id,)).fetchall()
    conn.close()
    perms = {k: (k in DEFAULT_ON_PERMS) for k in PERMISSIONS}
    for r in rows:
        perms[r["perm_key"]] = bool(r["enabled"])
    return perms


def set_permission(user_id, key, enabled: bool):
    conn = get_db()
    conn.execute(
        "INSERT INTO assistant_permissions (user_id, perm_key, enabled) VALUES (?,?,?) "
        "ON CONFLICT(user_id, perm_key) DO UPDATE SET enabled=excluded.enabled",
        (user_id, key, 1 if enabled else 0),
    )
    conn.commit()
    conn.close()


def ensure_default_perms(user_id):
    for key in PERMISSIONS:
        set_permission(user_id, key, key in DEFAULT_ON_PERMS)


def user_has_permission(session, key):
    if session["role"] == "admin":
        return True
    if session["role"] != "assistant":
        return False
    perms = get_permissions(session["id"])
    return perms.get(key, False)


def add_payment(customer_name, amount, method, payment_date, representative_id):
    conn = get_db()
    c = conn.execute(
        "INSERT INTO payments (customer_name, amount, method, payment_date, representative_id) VALUES (?,?,?,?,?)",
        (customer_name, amount, method, payment_date, representative_id),
    )
    conn.commit()
    pid = c.lastrowid
    conn.close()
    return pid


def get_payment(payment_id):
    conn = get_db()
    row = conn.execute(
        """SELECT p.*, u.name as rep_name FROM payments p JOIN users u ON u.id=p.representative_id
           WHERE p.id=?""",
        (payment_id,),
    ).fetchone()
    conn.close()
    return row


def update_payment_amount(payment_id, amount):
    conn = get_db()
    conn.execute("UPDATE payments SET amount=? WHERE id=?", (amount, payment_id))
    conn.commit()
    conn.close()


def update_payment_customer(payment_id, customer_name):
    conn = get_db()
    conn.execute("UPDATE payments SET customer_name=? WHERE id=?", (customer_name, payment_id))
    conn.commit()
    conn.close()


def update_payment_date(payment_id, date_str):
    conn = get_db()
    conn.execute("UPDATE payments SET payment_date=? WHERE id=?", (date_str, payment_id))
    conn.commit()
    conn.close()


def delete_payment(payment_id):
    conn = get_db()
    conn.execute("DELETE FROM payments WHERE id=?", (payment_id,))
    conn.commit()
    conn.close()


def transfer_payments(payment_ids, new_rep_id):
    if not payment_ids:
        return
    conn = get_db()
    placeholders = ",".join("?" for _ in payment_ids)
    conn.execute(f"UPDATE payments SET representative_id=? WHERE id IN ({placeholders})", (new_rep_id, *payment_ids))
    conn.commit()
    conn.close()


def get_payments_by_rep(rep_id, month=None, year=None):
    conn = get_db()
    q = "SELECT * FROM payments WHERE representative_id=?"
    params = [rep_id]
    if month and year:
        q += " AND strftime('%m', payment_date)=? AND strftime('%Y', payment_date)=?"
        params += [f"{month:02d}", str(year)]
    q += " ORDER BY payment_date DESC, id DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return rows


def get_all_payments(month=None, year=None):
    conn = get_db()
    q = """SELECT p.*, u.name as rep_name FROM payments p
           JOIN users u ON u.id = p.representative_id WHERE 1=1"""
    params = []
    if month and year:
        q += " AND strftime('%m', p.payment_date)=? AND strftime('%Y', p.payment_date)=?"
        params += [f"{month:02d}", str(year)]
    q += " ORDER BY p.payment_date DESC, p.id DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return rows


def get_payments_today():
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    rows = conn.execute(
        """SELECT p.*, u.name as rep_name FROM payments p JOIN users u ON u.id=p.representative_id
           WHERE p.payment_date=? ORDER BY p.id DESC""",
        (today,),
    ).fetchall()
    conn.close()
    return rows


def search_payments_by_customer(name, rep_id=None):
    conn = get_db()
    q = """SELECT p.*, u.name as rep_name FROM payments p JOIN users u ON u.id=p.representative_id
           WHERE p.customer_name LIKE ?"""
    params = [f"%{name}%"]
    if rep_id:
        q += " AND p.representative_id=?"
        params.append(rep_id)
    q += " ORDER BY p.payment_date DESC, p.id DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return rows


def get_total(rows):
    return sum(r["amount"] for r in rows)


def totals_by_method(month=None, year=None):
    rows = get_all_payments(month, year)
    totals = {}
    for r in rows:
        totals[r["method"]] = totals.get(r["method"], 0) + r["amount"]
    return totals


def set_target(rep_id, month, year, amount):
    conn = get_db()
    conn.execute(
        "INSERT INTO targets (representative_id, month, year, target_amount) VALUES (?,?,?,?) "
        "ON CONFLICT(representative_id, month, year) DO UPDATE SET target_amount=excluded.target_amount",
        (rep_id, month, year, amount),
    )
    conn.commit()
    conn.close()


def get_target(rep_id, month, year):
    conn = get_db()
    row = conn.execute(
        "SELECT target_amount FROM targets WHERE representative_id=? AND month=? AND year=?",
        (rep_id, month, year),
    ).fetchone()
    conn.close()
    return row["target_amount"] if row else 0


def delete_target(rep_id, month, year):
    conn = get_db()
    conn.execute("DELETE FROM targets WHERE representative_id=? AND month=? AND year=?", (rep_id, month, year))
    conn.commit()
    conn.close()


CATEGORY_LABELS = {"home": "🏠 Home Use", "professional": "🩺 Professional Use"}


def set_rep_category(rep_id, category):
    conn = get_db()
    conn.execute("UPDATE users SET category=? WHERE id=?", (category, rep_id))
    conn.commit()
    conn.close()


def set_category_target(category, month, year, amount):
    conn = get_db()
    conn.execute(
        "INSERT INTO category_targets (category, month, year, target_amount) VALUES (?,?,?,?) "
        "ON CONFLICT(category, month, year) DO UPDATE SET target_amount=excluded.target_amount",
        (category, month, year, amount),
    )
    conn.commit()
    conn.close()


def get_category_target(category, month, year):
    conn = get_db()
    row = conn.execute(
        "SELECT target_amount FROM category_targets WHERE category=? AND month=? AND year=?",
        (category, month, year),
    ).fetchone()
    conn.close()
    return row["target_amount"] if row else 0


def delete_category_target(category, month, year):
    conn = get_db()
    conn.execute("DELETE FROM category_targets WHERE category=? AND month=? AND year=?", (category, month, year))
    conn.commit()
    conn.close()


def get_category_payments(category, month=None, year=None):
    conn = get_db()
    q = """SELECT p.*, u.name as rep_name FROM payments p JOIN users u ON u.id=p.representative_id
           WHERE u.category=?"""
    params = [category]
    if month and year:
        q += " AND strftime('%m', p.payment_date)=? AND strftime('%Y', p.payment_date)=?"
        params += [f"{month:02d}", str(year)]
    q += " ORDER BY p.payment_date DESC, p.id DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return rows


def category_target_progress(category, month=None, year=None):
    if month is None or year is None:
        month, year = month_year_now()
    target = get_category_target(category, month, year)
    rows = get_category_payments(category, month, year)
    collected = get_total(rows)
    remaining = max(target - collected, 0)
    pct = (collected / target * 100) if target else 0
    return target, collected, remaining, pct, rows


def log_message(sender_id, recipient_id, recipient_type, body):
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (sender_id, recipient_id, recipient_type, body) VALUES (?,?,?,?)",
        (sender_id, recipient_id, recipient_type, body),
    )
    conn.commit()
    conn.close()


def add_feedback(sender_id, sender_name, sender_role, body):
    conn = get_db()
    c = conn.execute(
        "INSERT INTO feedback_messages (sender_id, sender_name, sender_role, body) VALUES (?,?,?,?)",
        (sender_id, sender_name, sender_role, body),
    )
    conn.commit()
    fid = c.lastrowid
    conn.close()
    return fid


def get_feedback(feedback_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM feedback_messages WHERE id=?", (feedback_id,)).fetchone()
    conn.close()
    return row


def save_feedback_reply(feedback_id, reply_body, replied_by_id, replied_by_name):
    conn = get_db()
    conn.execute(
        "UPDATE feedback_messages SET replied=1, reply_body=?, replied_by=?, replied_by_name=?, replied_at=datetime('now') WHERE id=?",
        (reply_body, replied_by_id, replied_by_name, feedback_id),
    )
    conn.commit()
    conn.close()

# ============================================================
# PDF GENERATION (Arabic RTL support)
# ============================================================

def ar(text):
    """Reshape + reorder Arabic text for correct RTL rendering in FPDF."""
    text = "" if text is None else str(text)
    if not HAVE_AR:
        return text
    try:
        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped)
    except Exception:
        return text


class ArabicPDF(FPDF):
    def __init__(self, subtitle=""):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.subtitle = subtitle
        self.font_ready = os.path.exists(FONT_PATH)
        if self.font_ready:
            self.add_font("Arabic", "", FONT_PATH)
            self.add_font("Arabic", "B", FONT_PATH)
        self.set_auto_page_break(auto=True, margin=18)
        self.add_page()

    def _font(self, style="", size=12):
        if self.font_ready:
            self.set_font("Arabic", style, size)
        else:
            self.set_font("Helvetica", style, size)

    def header(self):
        if os.path.exists(LOGO_PATH):
            try:
                # شعار الشركة أعلى اليمين
                self.image(LOGO_PATH, x=180, y=6, w=20)
            except Exception:
                pass
        self.set_fill_color(0, 90, 90)
        self.set_text_color(255, 255, 255)
        self._font("B", 16)
        self.set_xy(0, 8)
        self.cell(210, 12, ar(COMPANY_NAME), align="C", fill=True)
        self.ln(12)
        if self.subtitle:
            self.set_fill_color(0, 130, 130)
            self._font("B", 12)
            self.set_x(0)
            self.cell(210, 9, ar(self.subtitle), align="C", fill=True)
            self.ln(9)
        self.set_text_color(0, 0, 0)
        self.ln(4)

    def footer(self):
        self.set_y(-16)
        self._font("", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 6, f"Page {self.page_no()}", align="C")
        self.ln(5)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(160, 160, 160)
        self.cell(0, 5, "Powered by Anas Bu Grain", align="C")

    def rtl_cell(self, w, h, text, border=1, align="R", fill=False, style=""):
        self._font(style, 11)
        self.cell(w, h, ar(text), border=border, align=align, fill=fill)

    def section_title(self, text):
        self._font("B", 13)
        self.set_fill_color(230, 245, 245)
        self.cell(0, 10, ar(text), align="R", fill=True)
        self.ln(11)

    def info_line(self, label, value):
        self._font("B", 11)
        self.cell(0, 8, ar(f"{label}: {value}"), align="R")
        self.ln(8)


def _table(pdf: ArabicPDF, headers, rows, col_widths, zebra=True):
    total_w = sum(col_widths)
    x_start = (210 - total_w) / 2
    pdf.set_x(x_start)
    pdf.set_fill_color(0, 110, 110)
    pdf.set_text_color(255, 255, 255)
    for h, w in zip(headers, col_widths):
        pdf.rtl_cell(w, 9, h, border=1, align="C", fill=True, style="B")
    pdf.ln(9)
    pdf.set_text_color(0, 0, 0)
    fill = False
    for row in rows:
        pdf.set_x(x_start)
        if zebra and fill:
            pdf.set_fill_color(240, 248, 248)
        else:
            pdf.set_fill_color(255, 255, 255)
        for val, w in zip(row, col_widths):
            pdf.rtl_cell(w, 8, val, border=1, align="C", fill=zebra)
        pdf.ln(8)
        fill = not fill


def generate_customer_statement_pdf(customer_name, rows, out_path):
    pdf = ArabicPDF(subtitle="كشف حساب عميل")
    pdf.info_line("اسم العميل", customer_name)
    pdf.info_line("تاريخ الاستخراج", datetime.now().strftime("%Y-%m-%d"))
    pdf.ln(2)
    headers = ["المندوب", "طريقة السداد", "قيمة السداد", "التاريخ"]
    widths = [40, 55, 40, 35]
    data = [[r["rep_name"], r["method"], f"{r['amount']:,.2f}", r["payment_date"]] for r in rows]
    _table(pdf, headers, data, widths)
    total = get_total(rows)
    pdf.ln(4)
    pdf._font("B", 13)
    pdf.set_fill_color(0, 90, 90)
    pdf.set_text_color(255, 255, 255)
    pdf.set_x((210 - sum(widths)) / 2)
    pdf.cell(sum(widths), 10, ar(f"إجمالي السدادات: {total:,.2f} د.ل"), align="C", fill=True)
    pdf.output(out_path)
    return out_path


def generate_rep_report_pdf(rep_name, rows, out_path, period_label="", target_info=None):
    pdf = ArabicPDF(subtitle=f"تقرير سدادات المندوب — {rep_name}")
    if period_label:
        pdf.info_line("الفترة", period_label)
    pdf.info_line("تاريخ الاستخراج", datetime.now().strftime("%Y-%m-%d"))
    if target_info:
        target, collected, remaining, pct = target_info
        if target:
            pdf.ln(1)
            pdf.set_fill_color(230, 245, 245)
            pdf._font("B", 11)
            pdf.cell(0, 7, ar(f"الهدف الشهري: {target:,.2f} د.ل"), align="R", fill=True)
            pdf.ln(7)
            pdf.cell(0, 7, ar(f"المحصل: {collected:,.2f} د.ل"), align="R", fill=True)
            pdf.ln(7)
            pdf.cell(0, 7, ar(f"نسبة الإنجاز: {pct:.0f}%"), align="R", fill=True)
            pdf.ln(7)
            if collected >= target:
                surplus = collected - target
                pdf.cell(0, 7, ar(f"تم تجاوز الهدف بمقدار {surplus:,.2f} د.ل"), align="R", fill=True)
            else:
                pdf.cell(0, 7, ar(f"المتبقي: {remaining:,.2f} د.ل"), align="R", fill=True)
            pdf.ln(9)
    pdf.ln(2)
    headers = ["طريقة السداد", "قيمة السداد", "تاريخ السداد", "اسم العميل"]
    widths = [45, 35, 35, 55]
    data = [[r["method"], f"{r['amount']:,.2f}", r["payment_date"], r["customer_name"]] for r in rows]
    _table(pdf, headers, data, widths)
    total = get_total(rows)
    pdf.ln(4)
    pdf._font("B", 13)
    pdf.set_fill_color(0, 90, 90)
    pdf.set_text_color(255, 255, 255)
    pdf.set_x((210 - sum(widths)) / 2)
    pdf.cell(sum(widths), 10, ar(f"إجمالي السدادات: {total:,.2f} د.ل"), align="C", fill=True)
    pdf.output(out_path)
    return out_path


def generate_general_report_pdf(rows, out_path, period_label="", target_info=None):
    pdf = ArabicPDF(subtitle="التقرير العام للتحصيل")
    if period_label:
        pdf.info_line("الفترة", period_label)
    pdf.info_line("تاريخ الاستخراج", datetime.now().strftime("%Y-%m-%d"))
    if target_info:
        target, collected, remaining, pct = target_info
        if target:
            pdf.ln(1)
            pdf.set_fill_color(230, 245, 245)
            pdf._font("B", 11)
            pdf.cell(0, 7, ar(f"الهدف الشهري: {target:,.2f} د.ل"), align="R", fill=True)
            pdf.ln(7)
            pdf.cell(0, 7, ar(f"المحصل: {collected:,.2f} د.ل"), align="R", fill=True)
            pdf.ln(7)
            pdf.cell(0, 7, ar(f"نسبة الإنجاز: {pct:.0f}%"), align="R", fill=True)
            pdf.ln(7)
            if collected >= target:
                surplus = collected - target
                pdf.cell(0, 7, ar(f"تم تجاوز الهدف بمقدار {surplus:,.2f} د.ل"), align="R", fill=True)
            else:
                pdf.cell(0, 7, ar(f"المتبقي: {remaining:,.2f} د.ل"), align="R", fill=True)
            pdf.ln(9)
    pdf.ln(2)
    headers = ["طريقة السداد", "قيمة السداد", "التاريخ", "العميل", "المندوب"]
    widths = [35, 30, 30, 40, 35]
    data = [[r["method"], f"{r['amount']:,.2f}", r["payment_date"], r["customer_name"], r["rep_name"]] for r in rows]
    _table(pdf, headers, data, widths)
    total = get_total(rows)
    pdf.ln(4)
    pdf._font("B", 13)
    pdf.set_fill_color(0, 90, 90)
    pdf.set_text_color(255, 255, 255)
    pdf.set_x((210 - sum(widths)) / 2)
    pdf.cell(sum(widths), 10, ar(f"إجمالي التحصيل: {total:,.2f} د.ل"), align="C", fill=True)
    pdf.output(out_path)
    return out_path


def generate_method_report_pdf(totals: dict, out_path, period_label=""):
    pdf = ArabicPDF(subtitle="تقرير حسب طريقة السداد")
    if period_label:
        pdf.info_line("الفترة", period_label)
    pdf.info_line("تاريخ الاستخراج", datetime.now().strftime("%Y-%m-%d"))
    pdf.ln(2)
    headers = ["الإجمالي", "طريقة السداد"]
    widths = [60, 80]
    grand_total = sum(totals.values())
    data = [[f"{v:,.2f} د.ل", k] for k, v in totals.items()]
    _table(pdf, headers, data, widths)
    pdf.ln(4)
    pdf._font("B", 13)
    pdf.set_fill_color(0, 90, 90)
    pdf.set_text_color(255, 255, 255)
    pdf.set_x((210 - sum(widths)) / 2)
    pdf.cell(sum(widths), 10, ar(f"الإجمالي الكلي: {grand_total:,.2f} د.ل"), align="C", fill=True)
    pdf.output(out_path)
    return out_path


def draw_weekly_bar_chart(pdf, stats):
    if not stats:
        return
    pdf._font("B", 12)
    pdf.set_fill_color(230, 245, 245)
    pdf.cell(0, 9, ar("📊 مخطط الترتيب — من الأكثر تحصيلاً إلى الأقل"), align="C", fill=True)
    pdf.ln(11)
    max_amount = max((s["total"] for s in stats), default=0) or 1
    x_start = 20
    bar_area_w = 120
    name_x = 148
    name_w = 45
    row_h = 9
    for s in stats:
        y0 = pdf.get_y()
        if y0 > 260:  # حماية بسيطة من تجاوز الصفحة
            pdf.add_page()
            y0 = pdf.get_y()
        bar_w = (s["total"] / max_amount) * bar_area_w if max_amount else 0
        pdf.set_fill_color(0, 130, 130)
        pdf.rect(x_start, y0 + 1, max(bar_w, 0.5), row_h - 2, style="F")
        pdf._font("", 9)
        pdf.set_xy(x_start + bar_w + 2, y0)
        pdf.cell(24, row_h, f"{s['total']:,.0f}", align="L")
        pdf.set_xy(name_x, y0)
        pdf._font("B", 10)
        pdf.cell(name_w, row_h, ar(s["name"]), align="R")
        pdf.set_y(y0 + row_h)
    pdf.ln(4)


def suggestion_for_rank(index, count):
    if count <= 1:
        return "ℹ️ المندوب الوحيد في هذا التصنيف هذا الأسبوع، لا يوجد ترتيب مقارن."
    if index == 0:
        return "🏆 الأقوى أداءً هذا الأسبوع — يُقترح منحه مكافأة مالية أو إجازة 4 أيام."
    if index == count - 1:
        return "⚠️ الأضعف أداءً هذا الأسبوع — يُقترح توجيه إنذار مع خصم بسيط."
    return "💬 أداء متوسط هذا الأسبوع — يُقترح تحفيز كلامي بسيط لرفع الحماس."


def generate_weekly_report_pdf(category, start_date, end_date, stats, overall_total, out_path):
    label = CATEGORY_LABELS[category]
    pdf = ArabicPDF(subtitle=f"تقرير أسبوعي بالجباية — {label}")
    pdf.info_line("الفترة", f"من {start_date} إلى {end_date}")
    pdf.info_line("تاريخ الاستخراج", datetime.now().strftime("%Y-%m-%d"))
    pdf.ln(2)

    if not stats:
        pdf._font("", 12)
        pdf.cell(0, 10, ar("لا يوجد مندوبون في هذا التصنيف هذا الأسبوع."), align="C")
        pdf.output(out_path)
        return out_path

    headers = ["أيام الابتعاد", "عدد التفاعلات", "عدد عمليات السداد", "إجمالي التحصيل", "المندوب"]
    widths = [28, 28, 32, 40, 45]
    data = [[str(s["away_days"]), str(s["interactions"]), str(s["payment_count"]), f"{s['total']:,.2f}", s["name"]] for s in stats]
    _table(pdf, headers, data, widths)
    pdf.ln(3)
    pdf._font("B", 13)
    pdf.set_fill_color(0, 90, 90)
    pdf.set_text_color(255, 255, 255)
    pdf.set_x((210 - sum(widths)) / 2)
    pdf.cell(sum(widths), 10, ar(f"إجمالي تحصيل {label} هذا الأسبوع: {overall_total:,.2f} د.ل"), align="C", fill=True)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(14)

    draw_weekly_bar_chart(pdf, stats)

    pdf._font("B", 13)
    pdf.set_fill_color(230, 245, 245)
    pdf.cell(0, 10, ar("📝 تحليل الأداء والاقتراحات"), align="R", fill=True)
    pdf.ln(12)
    count = len(stats)
    for i, s in enumerate(stats):
        pdf._font("B", 11)
        pdf.cell(0, 7, ar(f"👤 {s['name']}"), align="R")
        pdf.ln(7)
        pdf._font("", 10)
        pdf.cell(0, 6, ar(f"إجمالي التحصيل: {s['total']:,.2f} د.ل"), align="R")
        pdf.ln(6)
        pdf.cell(0, 6, ar(f"عدد مرات تسجيل السداد: {s['payment_count']}"), align="R")
        pdf.ln(6)
        pdf.cell(0, 6, ar(f"عدد مرات التفاعل مع البوت: {s['interactions']}"), align="R")
        pdf.ln(6)
        pdf.cell(0, 6, ar(f"عدد أيام الابتعاد عن البوت (من 7): {s['away_days']}"), align="R")
        pdf.ln(6)
        pdf._font("B", 10)
        pdf.cell(0, 7, ar(suggestion_for_rank(i, count)), align="R")
        pdf.ln(10)

    pdf.output(out_path)
    return out_path


def generate_expenses_pdf(rows, out_path, period_label=""):
    pdf = ArabicPDF(subtitle="تقرير المصاريف")
    if period_label:
        pdf.info_line("الفترة/التصفية", period_label)
    pdf.info_line("تاريخ الاستخراج", datetime.now().strftime("%Y-%m-%d"))
    pdf.ln(2)
    headers = ["الفئة", "البيان", "القيمة", "التاريخ"]
    widths = [45, 60, 35, 35]
    data = [[r["attribution_name"] or "-", r["description"], f"{r['amount']:,.2f}", r["expense_date"]] for r in rows]
    _table(pdf, headers, data, widths)
    total = get_total_expenses(rows)
    pdf.ln(4)
    pdf._font("B", 13)
    pdf.set_fill_color(150, 40, 40)
    pdf.set_text_color(255, 255, 255)
    pdf.set_x((210 - sum(widths)) / 2)
    pdf.cell(sum(widths), 10, ar(f"إجمالي المصاريف: {total:,.2f} د.ل"), align="C", fill=True)
    pdf.output(out_path)
    return out_path


def generate_payroll_pdf(rows, out_path, period_label=""):
    pdf = ArabicPDF(subtitle="تقرير الرواتب")
    if period_label:
        pdf.info_line("الفترة/التصفية", period_label)
    pdf.info_line("تاريخ الاستخراج", datetime.now().strftime("%Y-%m-%d"))
    pdf.ln(2)
    headers = ["الفترة", "المحتجز", "الصافي المصروف", "النوع", "الموظف"]
    widths = [28, 28, 34, 40, 45]
    data = []
    for r in rows:
        kind_label = "صرف راتب" if r["kind"] == "payout" else "صرف رصيد محتجز"
        period = f"{r['period_month']}-{r['period_year']}" if r["period_month"] else "-"
        data.append([period, f"{r['retained_amount']:,.2f}", f"{r['paid_amount']:,.2f}", kind_label, r["employee_name"]])
    _table(pdf, headers, data, widths)
    total_paid = sum(r["paid_amount"] for r in rows)
    total_retained = sum(r["retained_amount"] for r in rows)
    pdf.ln(4)
    pdf._font("B", 12)
    pdf.set_fill_color(0, 90, 90)
    pdf.set_text_color(255, 255, 255)
    pdf.set_x((210 - sum(widths)) / 2)
    pdf.cell(sum(widths), 9, ar(f"إجمالي المصروف: {total_paid:,.2f} د.ل"), align="C", fill=True)
    pdf.ln(9)
    pdf.set_fill_color(150, 100, 0)
    pdf.set_x((210 - sum(widths)) / 2)
    pdf.cell(sum(widths), 9, ar(f"إجمالي المحتجز من هذه العمليات: {total_retained:,.2f} د.ل"), align="C", fill=True)
    pdf.output(out_path)
    return out_path


    pdf.output(out_path)
    return out_path


async def check_pdf_ready(message_target) -> bool:
    """Sends a clear warning and returns False if the Arabic font is missing,
    so PDF generation isn't attempted with a font that can't render Arabic text."""
    if not os.path.exists(FONT_PATH):
        await message_target.reply_text(
            "⚠️ تعذر إنشاء ملف PDF: لم يتم العثور على ملف الخط العربي في المستودع.\n\n"
            "الرجاء إضافة ملف الخط باسم:\n"
            "fonts/NotoNaskhArabic-Regular.ttf\n\n"
            "يمكن تحميله من Google Fonts (Noto Naskh Arabic)، ثم إعادة رفعه إلى مجلد fonts ونشر التحديث."
        )
        return False
    return True


async def safe_send_pdf(message_target, generator_func, out_path, filename, *args, **kwargs):
    """Runs a PDF generator function and sends the result, replying with a friendly
    error message instead of silently failing if anything goes wrong."""
    try:
        generator_func(*args, out_path=out_path, **kwargs)
        with open(out_path, "rb") as f:
            await message_target.reply_document(f, filename=filename)
    except Exception as e:
        logger.exception("PDF generation/send failed: %s", e)
        await message_target.reply_text(
            "❌ حدث خطأ أثناء إنشاء ملف PDF. تأكد من إضافة ملف الخط العربي في مجلد fonts، ثم أعد المحاولة."
        )
    finally:
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass

# ============================================================
# KEYBOARDS
# ============================================================

def kb(rows):
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


LOGIN_KB = kb([["🔐 دخول"]])
CANCEL_KB = kb([["❌ إلغاء الأمر"]])
SKIP_CANCEL_KB = kb([["⏭️ تخطي"], ["❌ إلغاء الأمر"]])

REP_MENU_ROWS = [["💰 التحصيل", "🔍 البحث عن عميل"], ["📊 تقرير السدادات"], ["🏦 حسابات شركة الحياة فارما"], ["📢 إبلاغ/فكرة تطوير"], ["❌ إلغاء الأمر", "🚪 خروج"]]

ADMIN_MENU_ROWS = [
    ["💰 التحصيل", "👥 المندوبين"],
    ["👨‍💼 المساعدين", "🎯 أهداف التحصيل"],
    ["💰 الجباية", "🔍 البحث عن عميل"],
    ["📊 التقارير", "📩 إرسال رسالة"],
    ["📶 حالة المندوبين"],
    ["🏠 Home Use target", "🩺 Professional Use target"],
    ["💵 مصاريف"],
    ["💼 الرواتب"],
    ["🏦 حسابات شركة الحياة فارما"],
    ["📢 إبلاغ/فكرة تطوير"],
    ["❌ إلغاء الأمر", "🚪 خروج"],
]


def main_menu_kb(session):
    role = session["role"]
    if role == "representative":
        return kb(REP_MENU_ROWS)
    if role == "admin":
        return kb(ADMIN_MENU_ROWS)
    # assistant: build dynamically from permissions
    perms = get_permissions(session["id"])
    rows = []
    r0 = []
    if perms.get("collect_payments"):
        r0.append("💰 التحصيل")
    if perms.get("view_representatives"):
        r0.append("👥 المندوبين")
    rows2 = []
    if perms.get("manage_targets"):
        rows2.append("🎯 أهداف التحصيل")
    if perms.get("view_payments"):
        rows2.append("💰 الجباية")
    rows3 = []
    if perms.get("search_customers"):
        rows3.append("🔍 البحث عن عميل")
    if perms.get("view_reports"):
        rows3.append("📊 التقارير")
    if r0:
        rows.append(r0)
    if rows2:
        rows.append(rows2)
    if rows3:
        rows.append(rows3)
    if perms.get("send_messages"):
        rows.append(["📩 إرسال رسالة"])
    if perms.get("view_rep_status"):
        rows.append(["📶 حالة المندوبين"])
    cat_row = []
    if perms.get("manage_home_target"):
        cat_row.append("🏠 Home Use target")
    if perms.get("manage_professional_target"):
        cat_row.append("🩺 Professional Use target")
    if cat_row:
        rows.append(cat_row)
    if perms.get("manage_expenses"):
        rows.append(["💵 مصاريف"])
    if perms.get("manage_payroll"):
        rows.append(["💼 الرواتب"])
    rows.append(["🏦 حسابات شركة الحياة فارما"])
    rows.append(["📢 إبلاغ/فكرة تطوير"])
    rows.append(["❌ إلغاء الأمر", "🚪 خروج"])
    return kb(rows)


def method_inline_kb():
    buttons = [[InlineKeyboardButton(m, callback_data=f"method:{i}")] for i, m in enumerate(PAYMENT_METHODS)]
    return InlineKeyboardMarkup(buttons)


def build_keypad_kb(value):
    rows = [
        [InlineKeyboardButton("1", callback_data="kp:1"), InlineKeyboardButton("2", callback_data="kp:2"), InlineKeyboardButton("3", callback_data="kp:3")],
        [InlineKeyboardButton("4", callback_data="kp:4"), InlineKeyboardButton("5", callback_data="kp:5"), InlineKeyboardButton("6", callback_data="kp:6")],
        [InlineKeyboardButton("7", callback_data="kp:7"), InlineKeyboardButton("8", callback_data="kp:8"), InlineKeyboardButton("9", callback_data="kp:9")],
        [InlineKeyboardButton(".", callback_data="kp:."), InlineKeyboardButton("0", callback_data="kp:0"), InlineKeyboardButton("⌫", callback_data="kp:back")],
        [InlineKeyboardButton("🗑️ مسح", callback_data="kp:clear"), InlineKeyboardButton("✅ تأكيد", callback_data="kp:confirm")],
    ]
    return InlineKeyboardMarkup(rows)


async def keypad_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action = query.data.split(":", 1)[1]
    cur = context.user_data.get("kp_value", "")
    target = context.user_data.get("kp_target")

    if action == "confirm":
        try:
            amount = float(cur)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await query.answer("⚠️ أدخل قيمة صحيحة أولاً", show_alert=True)
            return None
        await query.answer()
        context.user_data.pop("kp_value", None)
        context.user_data.pop("kp_target", None)
        if target == "collect":
            context.user_data["collect"]["amount"] = amount
            await query.edit_message_text(f"💰 قيمة السداد: {amount:,.2f} د.ل")
            await query.message.reply_text("اختر طريقة السداد:", reply_markup=method_inline_kb())
            return COLLECT_METHOD
        elif target == "target":
            rep_id = context.user_data.pop("target_rep_id", None)
            if rep_id:
                m, y = month_year_now()
                set_target(rep_id, m, y, amount)
                rep = get_user(rep_id)
                await query.edit_message_text(f"✅ تم حفظ الهدف الشهري لـ {rep['name']}: {amount:,.2f} د.ل")
            await send_main_menu(query, context)
            return MAIN_MENU
        elif target == "edit_payment":
            payment_id = context.user_data.pop("edit_payment_id", None)
            session = session_of(context)
            if payment_id and user_has_permission(session, "edit_payments"):
                update_payment_amount(payment_id, amount)
                await query.edit_message_text(f"✅ تم تحديث قيمة السداد إلى: {amount:,.2f} د.ل")
            await send_main_menu(query, context)
            return MAIN_MENU
        elif target and target.startswith("category:"):
            _, category, month, year = target.split(":")
            set_category_target(category, int(month), int(year), amount)
            await query.edit_message_text(f"✅ تم حفظ هدف {CATEGORY_LABELS[category]} لشهر {month}-{year}: {amount:,.2f} د.ل")
            await send_main_menu(query, context)
            return MAIN_MENU
        elif target == "expense_amount":
            context.user_data["expense"] = {"amount": amount}
            await query.edit_message_text(f"💵 قيمة المصروف: {amount:,.2f} د.ل")
            await query.message.reply_text("أدخل بيان الصرف (وصف قصير للمصروف):", reply_markup=CANCEL_KB)
            return EXPENSE_DESC
        elif target == "payroll_new_fixed":
            new_emp = context.user_data.pop("payroll_new", {})
            eid = add_payroll_employee(new_emp.get("name", ""), "fixed", fixed_amount=amount, classification=new_emp.get("classification"))
            await query.edit_message_text(f"✅ تم إضافة الموظف: {new_emp.get('name','')}\nراتب ثابت: {amount:,.2f} د.ل")
            await send_main_menu(query, context)
            return MAIN_MENU
        elif target == "payroll_new_rate":
            new_emp = context.user_data.pop("payroll_new", {})
            eid = add_payroll_employee(
                new_emp.get("name", ""), "commission",
                commission_rate=amount, linked_rep_id=new_emp.get("linked_rep_id"),
                classification=new_emp.get("classification"),
            )
            rep = get_user(new_emp.get("linked_rep_id"))
            await query.edit_message_text(
                f"✅ تم إضافة الموظف: {new_emp.get('name','')}\n"
                f"نسبة العمولة: {amount:g}%\nمرتبط بالمندوب: {rep['name'] if rep else '-'}"
            )
            await send_main_menu(query, context)
            return MAIN_MENU
        elif target and target.startswith("payroll_edit:"):
            employee_id = int(target.split(":")[1])
            emp = get_payroll_employee(employee_id)
            if emp:
                update_payroll_employee_amount(employee_id, emp["emp_type"], amount)
                label = "الراتب الثابت" if emp["emp_type"] == "fixed" else "نسبة العمولة"
                suffix = " د.ل" if emp["emp_type"] == "fixed" else "%"
                await query.edit_message_text(f"✅ تم تحديث {label} لـ {emp['name']}: {amount:g}{suffix}")
            await send_main_menu(query, context)
            return MAIN_MENU
        return None

    await query.answer()
    if action == "back":
        cur = cur[:-1]
    elif action == "clear":
        cur = ""
    elif action == ".":
        if "." not in cur:
            cur = (cur or "0") + "."
    else:
        if len(cur) < 12:
            cur += action
    context.user_data["kp_value"] = cur
    label = {"collect": "قيمة السداد", "target": "قيمة الهدف الشهري", "edit_payment": "القيمة الجديدة للسداد"}.get(target, "القيمة")
    await query.edit_message_text(
        f"أدخل {label} باستخدام لوحة الأرقام:\n\nالقيمة الحالية: {cur if cur else '0'}",
        reply_markup=build_keypad_kb(cur),
    )
    return None


def build_calendar_kb(year, month, prefix="cal"):
    import calendar as _cal
    c = _cal.Calendar(firstweekday=6)  # يبدأ الأسبوع بالأحد
    weeks = c.monthdayscalendar(year, month)
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    rows = [[
        InlineKeyboardButton("◀️", callback_data=f"{prefix}:nav:{prev_y}:{prev_m}"),
        InlineKeyboardButton(f"{MONTHS_AR[month-1]} {year}", callback_data="noop"),
        InlineKeyboardButton("▶️", callback_data=f"{prefix}:nav:{next_y}:{next_m}"),
    ]]
    day_labels = ["أحد", "اثنين", "ثلاثاء", "أربعاء", "خميس", "جمعة", "سبت"]
    rows.append([InlineKeyboardButton(d, callback_data="noop") for d in day_labels])
    for week in weeks:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="noop"))
            else:
                row.append(InlineKeyboardButton(str(day), callback_data=f"{prefix}:day:{year}:{month}:{day}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("📅 اليوم", callback_data=f"{prefix}:today")])
    return InlineKeyboardMarkup(rows)


def yesno_kb(yes_cb, no_cb, yes_label="✅ نعم", no_label="❌ إلغاء"):
    return InlineKeyboardMarkup([[InlineKeyboardButton(yes_label, callback_data=yes_cb),
                                    InlineKeyboardButton(no_label, callback_data=no_cb)]])


# ============================================================
# CONVERSATION STATES
# ============================================================

(
    ADMIN_SETUP_NAME, ADMIN_SETUP_USERNAME, ADMIN_SETUP_PASSWORD,
    LOGIN_USERNAME, LOGIN_PASSWORD,
    MAIN_MENU,
    COLLECT_CUSTOMER, COLLECT_AMOUNT, COLLECT_METHOD, COLLECT_DATE,
    SEARCH_CUSTOMER,
    ADD_REP_NAME, ADD_REP_USERNAME, ADD_REP_PASSWORD,
    REP_DETAIL,
    EDIT_REP_NAME,
    EDIT_REP_PASSWORD,
    ADD_ASSIST_NAME, ADD_ASSIST_USERNAME, ADD_ASSIST_PASSWORD,
    ASSIST_DETAIL,
    EDIT_ASSIST_PASSWORD,
    TARGET_PICK_REP, TARGET_AMOUNT,
    REPORTS_MENU, REPORT_REP_PICK,
    MSG_PICK_TARGET, MSG_BODY,
    ADMIN_SEARCH_CUSTOMER,
    MSG_CHOOSE_TYPE,
    FEEDBACK_BODY,
    FEEDBACK_REPLY_BODY,
    PAYMENT_EDIT_NAME,
    EXPENSE_DESC,
    EXPENSE_FLOW,
    PAYROLL_EMP_NAME,
    PAYROLL_EMP_AMOUNT,
    PAYROLL_EDIT_AMOUNT,
) = range(38)

CB_METHOD = "method:"

# ============================================================
# HELPERS
# ============================================================

def session_of(context):
    return context.user_data.get("session")


def touch_session(context):
    context.user_data["last_active"] = datetime.now()
    session = context.user_data.get("session")
    if session:
        update_last_seen(session["id"])
        log_activity(session["id"])


def session_expired(context):
    last = context.user_data.get("last_active")
    if not last:
        return False
    return datetime.now() - last > timedelta(minutes=SESSION_TIMEOUT_MINUTES)


async def send_main_menu(update_or_query, context, text="القائمة الرئيسية:"):
    session = session_of(context)
    target = update_or_query.message if hasattr(update_or_query, "message") and update_or_query.message else update_or_query
    await target.reply_text(text, reply_markup=main_menu_kb(session))


def month_year_now():
    now = datetime.now()
    return now.month, now.year


def format_last_seen(last_seen_str):
    """يعرض آخر وقت تفاعل فيه المستخدم مع البوت (أقرب بديل ممكن لحالة أونلاين/أوفلاين،
    لأن تيليجرام لا يمنح البوتات صلاحية معرفة حالة الاتصال الفعلية لأي مستخدم)."""
    if not last_seen_str:
        return "🛑 لم يسجّل الدخول عبر البوت بعد"
    try:
        last_seen = datetime.strptime(last_seen_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return f"🛑 {last_seen_str}"
    delta = datetime.now() - last_seen
    seconds = delta.total_seconds()
    if seconds < 300:
        return "🟢 متصل (نشاط قبل أقل من 5 دقائق)"
    if seconds < 3600:
        return f"🟠 آخر نشاط: منذ {int(seconds // 60)} دقيقة"
    if seconds < 86400:
        return f"🛑 آخر نشاط: منذ {int(seconds // 3600)} ساعة"
    return f"🛑 آخر نشاط: منذ {int(seconds // 86400)} يوم"


def current_month_target_progress(rep_id, month=None, year=None):
    if month is None or year is None:
        month, year = month_year_now()
    target = get_target(rep_id, month, year)
    collected = get_total(get_payments_by_rep(rep_id, month, year))
    remaining = max(target - collected, 0)
    pct = (collected / target * 100) if target else 0
    return target, collected, remaining, pct


def target_progress_text(target, collected, remaining, pct):
    if not target:
        return "🎯 لم يتم تحديد هدف شهري بعد لهذا المندوب."
    pct_str = f"{pct:.0f}%"
    if collected >= target:
        surplus = collected - target
        return (
            f"🎯 الهدف الشهري: {target:,.2f} د.ل\n"
            f"✅ تم تحصيل: {collected:,.2f} د.ل — نسبة الإنجاز: {pct_str}\n"
            f"🎉 تم تحقيق الهدف بالكامل (تجاوز بمقدار {surplus:,.2f} د.ل)"
        )
    return (
        f"🎯 الهدف الشهري: {target:,.2f} د.ل\n"
        f"✅ تم تحصيل: {collected:,.2f} د.ل — نسبة الإنجاز: {pct_str}\n"
        f"⏳ المتبقي للوصول للهدف: {remaining:,.2f} د.ل"
    )


# ============================================================
# ENTRY: /start
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if not any_admin_exists():
        await update.message.reply_text(
            f"{BOT_TITLE}\n\nلا يوجد حساب مدير بعد. سنقوم الآن بإنشاء حساب المدير الأول.\n\n"
            "أدخل اسم المدير:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ADMIN_SETUP_NAME

    await update.message.reply_text(
        f"{COMPANY_NAME}\n\n{BOT_TITLE}\n\nمرحباً بك، اضغط على زر الدخول لتسجيل الدخول.",
        reply_markup=LOGIN_KB,
    )
    return LOGIN_USERNAME


# ---- Admin first-time setup ----

async def admin_setup_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["setup_name"] = update.message.text.strip()
    await update.message.reply_text("أدخل اسم المستخدم (username) لحساب المدير:")
    return ADMIN_SETUP_USERNAME


async def admin_setup_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.text.strip()
    if get_user_by_username(username):
        await update.message.reply_text("⚠️ اسم المستخدم هذا مستخدم بالفعل، اختر اسماً آخر:")
        return ADMIN_SETUP_USERNAME
    context.user_data["setup_username"] = username
    await update.message.reply_text("أدخل الرقم السري لحساب المدير:")
    return ADMIN_SETUP_PASSWORD


async def admin_setup_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    name = context.user_data.pop("setup_name")
    username = context.user_data.pop("setup_username")
    uid = create_user(name, username, password, "admin")
    await update.message.reply_text(
        f"✅ تم إنشاء حساب المدير بنجاح.\n\n{COMPANY_NAME}\n\nاضغط على زر الدخول لتسجيل الدخول.",
        reply_markup=LOGIN_KB,
    )
    return LOGIN_USERNAME


# ---- Login (بالرقم السري فقط) ----

async def login_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if "دخول" in text:
        chat_id = update.effective_chat.id
        current_id = update.effective_message.message_id if update.effective_message else None
        await update.message.reply_text("🔑 الرقم السري:", reply_markup=CANCEL_KB)
        # مسح المحادثة السابقة تلقائياً في الخلفية عند الضغط على "دخول"، دون تعطيل الاستجابة
        if current_id:
            asyncio.create_task(_bulk_delete_chat_history(context.bot, chat_id, current_id - 1))
        return LOGIN_PASSWORD
    await update.message.reply_text("اضغط على زر الدخول لتسجيل الدخول.", reply_markup=LOGIN_KB)
    return LOGIN_USERNAME


async def login_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    user = get_user_by_password(password)
    if not user:
        await update.message.reply_text("❌ الرقم السري غير صحيح. حاول مرة أخرى.\n\n🔑 الرقم السري:")
        return LOGIN_PASSWORD
    set_telegram_id(user["id"], update.effective_user.id)
    session = {"id": user["id"], "name": user["name"], "username": user["username"], "role": user["role"]}
    context.user_data["session"] = session
    touch_session(context)
    role_label = {"admin": "👑 المدير", "assistant": "👨‍💼 المساعد", "representative": "👤 المندوب"}[session["role"]]
    await update.message.reply_text(
        f"✅ تم تسجيل الدخول بنجاح\nمرحباً {session['name']} ({role_label})",
        reply_markup=main_menu_kb(session),
    )
    return MAIN_MENU


async def _bulk_delete_chat_history(bot, chat_id, from_message_id, count=200):
    """يحذف رسائل المحادثة بالتوازي (غير متسلسل) في الخلفية دون تعطيل معالجة بقية الرسائل."""
    sem = asyncio.Semaphore(15)

    async def _del(mid):
        async with sem:
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                pass  # رسالة قديمة جداً (خارج حد 48 ساعة) أو محذوفة مسبقاً

    ids = range(from_message_id, max(from_message_id - count, 0), -1)
    await asyncio.gather(*[_del(mid) for mid in ids])


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    current_id = update.effective_message.message_id if update.effective_message else None
    context.user_data.clear()
    # نرسل رسالة التأكيد فوراً بدون انتظار، والحذف يتم في الخلفية بالتوازي
    await context.bot.send_message(
        chat_id,
        f"🚪 تم تسجيل الخروج.\n\n{COMPANY_NAME}\n\nاضغط على زر الدخول لتسجيل الدخول.",
        reply_markup=LOGIN_KB,
    )
    if current_id:
        asyncio.create_task(_bulk_delete_chat_history(context.bot, chat_id, current_id))
    return LOGIN_USERNAME


async def cancel_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = session_of(context)
    if not session:
        return await start(update, context)
    await update.message.reply_text("تم الإلغاء.", reply_markup=main_menu_kb(session))
    return MAIN_MENU


async def check_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Returns True and resets to login if session expired."""
    if session_of(context) and session_expired(context):
        context.user_data.clear()
        await update.message.reply_text(
            "⏳ انتهت صلاحية الجلسة لعدم النشاط. الرجاء تسجيل الدخول مرة أخرى.\n\nاضغط على زر الدخول.",
            reply_markup=LOGIN_KB,
        )
        return True
    touch_session(context)
    return False

# ============================================================
# REPRESENTATIVE: collection entry (💰 التحصيل)
# ============================================================

async def collect_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_timeout(update, context):
        return LOGIN_USERNAME
    session = session_of(context)
    context.user_data["collect"] = {}
    context.user_data.pop("collect_on_behalf_of", None)
    if session["role"] == "representative":
        await update.message.reply_text("أدخل اسم العميل:", reply_markup=CANCEL_KB)
        return COLLECT_CUSTOMER
    # المدير أو المساعد المخوّل: يختار باسمه هو أم باسم مندوب أم باسم مساعد
    buttons = [
        [InlineKeyboardButton("👤 باسمي", callback_data="collectas:self")],
        [InlineKeyboardButton("👥 باسم مندوب", callback_data="collectas:rep")],
        [InlineKeyboardButton("🧑‍💼 باسم مساعد", callback_data="collectas:assistant")],
    ]
    await update.message.reply_text("سجّل عملية التحصيل هذه:", reply_markup=InlineKeyboardMarkup(buttons))
    await update.message.reply_text("يمكنك الإلغاء في أي وقت:", reply_markup=CANCEL_KB)
    return MAIN_MENU


async def collectas_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kind = query.data.split(":")[1]
    context.user_data["collect"] = {}
    if kind == "self":
        context.user_data.pop("collect_on_behalf_of", None)
        await query.edit_message_text("👤 التحصيل باسمك.")
        await query.message.reply_text("أدخل اسم العميل:", reply_markup=CANCEL_KB)
        return COLLECT_CUSTOMER
    if kind == "assistant":
        assistants = list_users_by_role("assistant", active_only=True)
        if not assistants:
            await query.edit_message_text("لا يوجد مساعدون نشطون حالياً.")
            return MAIN_MENU
        buttons = [[InlineKeyboardButton(a["name"], callback_data=f"collectassistant:{a['id']}")] for a in assistants]
        await query.edit_message_text("اختر المساعد:", reply_markup=InlineKeyboardMarkup(buttons))
        return MAIN_MENU
    reps = list_users_by_role("representative", active_only=True)
    if not reps:
        await query.edit_message_text("لا يوجد مندوبون نشطون حالياً.")
        return MAIN_MENU
    buttons = [[InlineKeyboardButton(r["name"], callback_data=f"collectrep:{r['id']}")] for r in reps]
    await query.edit_message_text("اختر المندوب:", reply_markup=InlineKeyboardMarkup(buttons))
    return MAIN_MENU


async def collectrep_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rep_id = int(query.data.split(":")[1])
    rep = get_user(rep_id)
    context.user_data["collect"] = {}
    context.user_data["collect_on_behalf_of"] = rep_id
    await query.edit_message_text(f"👥 التحصيل باسم المندوب: {rep['name']}")
    await query.message.reply_text("أدخل اسم العميل:", reply_markup=CANCEL_KB)
    return COLLECT_CUSTOMER


async def collectassistant_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    assistant_id = int(query.data.split(":")[1])
    assistant = get_user(assistant_id)
    context.user_data["collect"] = {}
    context.user_data["collect_on_behalf_of"] = assistant_id
    await query.edit_message_text(f"🧑‍💼 التحصيل باسم المساعد: {assistant['name']}")
    await query.message.reply_text("أدخل اسم العميل:", reply_markup=CANCEL_KB)
    return COLLECT_CUSTOMER


async def collect_customer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["collect"]["customer_name"] = update.message.text.strip()
    context.user_data["kp_target"] = "collect"
    context.user_data["kp_value"] = ""
    await update.message.reply_text(
        "أدخل قيمة السداد باستخدام لوحة الأرقام:\n\nالقيمة الحالية: 0",
        reply_markup=build_keypad_kb(""),
    )
    return COLLECT_AMOUNT


async def collect_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ الرجاء إدخال رقم صحيح أكبر من صفر لقيمة السداد:")
        return COLLECT_AMOUNT
    context.user_data["collect"]["amount"] = amount
    context.user_data.pop("kp_value", None)
    context.user_data.pop("kp_target", None)
    await update.message.reply_text("اختر طريقة السداد:", reply_markup=method_inline_kb())
    return COLLECT_METHOD


async def collect_method_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":")[1])
    method = PAYMENT_METHODS[idx]
    context.user_data["collect"]["method"] = method
    await query.edit_message_text(f"طريقة السداد: {method}")
    now = datetime.now()
    await query.message.reply_text(
        "📅 اختر تاريخ السداد (أو اكتبه يدوياً بصيغة YYYY-MM-DD):",
        reply_markup=build_calendar_kb(now.year, now.month),
    )
    return COLLECT_DATE


async def calendar_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":")
    action = parts[1]
    if action == "nav":
        await query.answer()
        y, m = int(parts[2]), int(parts[3])
        await query.edit_message_reply_markup(reply_markup=build_calendar_kb(y, m))
        return COLLECT_DATE
    await query.answer()
    if action == "today":
        date_str = datetime.now().strftime("%Y-%m-%d")
    else:  # "day"
        y, m, d = int(parts[2]), int(parts[3]), int(parts[4])
        date_str = f"{y:04d}-{m:02d}-{d:02d}"
    context.user_data["collect"]["payment_date"] = date_str
    data = context.user_data["collect"]
    summary = (
        f"يرجى تأكيد بيانات السداد:\n\n"
        f"👤 العميل: {data['customer_name']}\n"
        f"💰 القيمة: {data['amount']:,.2f} د.ل\n"
        f"💳 الطريقة: {data['method']}\n"
        f"📅 التاريخ: {data['payment_date']}"
    )
    await query.edit_message_text(summary, reply_markup=yesno_kb("save_payment", "cancel_payment", "💾 حفظ السداد", "❌ إلغاء"))
    return COLLECT_DATE


async def collect_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text != "⏭️ تخطي":
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", text):
            await update.message.reply_text("⚠️ صيغة التاريخ غير صحيحة. استخدم YYYY-MM-DD أو اضغط تخطي:")
            return COLLECT_DATE
        context.user_data["collect"]["payment_date"] = text
    data = context.user_data["collect"]
    summary = (
        f"يرجى تأكيد بيانات السداد:\n\n"
        f"👤 العميل: {data['customer_name']}\n"
        f"💰 القيمة: {data['amount']:,.2f} د.ل\n"
        f"💳 الطريقة: {data['method']}\n"
        f"📅 التاريخ: {data['payment_date']}"
    )
    await update.message.reply_text(summary, reply_markup=yesno_kb("save_payment", "cancel_payment", "💾 حفظ السداد", "❌ إلغاء"))
    return COLLECT_DATE


async def save_payment_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    data = context.user_data.get("collect", {})
    if query.data == "cancel_payment" or not data:
        await query.edit_message_text("تم الإلغاء.")
        context.user_data.pop("collect_on_behalf_of", None)
        await send_main_menu(query, context)
        return MAIN_MENU
    on_behalf_id = context.user_data.pop("collect_on_behalf_of", None)
    recorded_user = get_user(on_behalf_id) if on_behalf_id else session
    target_user_id = recorded_user["id"] if on_behalf_id else session["id"]
    add_payment(data["customer_name"], data["amount"], data["method"], data["payment_date"], target_user_id)
    try:
        pay_month, pay_year = map(int, data["payment_date"].split("-")[1::-1])
    except Exception:
        pay_month, pay_year = month_year_now()
    target, collected, remaining, pct = current_month_target_progress(target_user_id, pay_month, pay_year)
    name_line = f"👤 باسم: {recorded_user['name']}" if on_behalf_id else f"👤 باسم: {session['name']}"
    await query.edit_message_text(
        "✅ تم تسجيل السداد بنجاح\n\n"
        f"👤 العميل: {data['customer_name']}\n"
        f"💰 القيمة: {data['amount']:,.2f} د.ل\n"
        f"💳 الطريقة: {data['method']}\n"
        f"📅 التاريخ: {data['payment_date']}\n"
        f"{name_line}\n\n"
        f"{target_progress_text(target, collected, remaining, pct)}"
    )
    await notify_admins_new_payment(context, recorded_user, data)
    context.user_data.pop("collect", None)
    await query.message.reply_text("العملية التالية:", reply_markup=main_menu_kb(session))
    return MAIN_MENU


async def notify_admins_new_payment(context: ContextTypes.DEFAULT_TYPE, rep_session, data):
    """يرسل إشعاراً عند تسجيل المندوب لعملية سداد جديدة لكل حسابات المدير،
    ولكل مساعد فعّل له المدير صلاحية '💰 مشاهدة الجباية'."""
    text = (
        f"🔔 عملية سداد جديدة\n\n"
        f"👤 باسم: {rep_session['name']}\n"
        f"🏪 العميل: {data['customer_name']}\n"
        f"💰 القيمة: {data['amount']:,.2f} د.ل\n"
        f"💳 الطريقة: {data['method']}\n"
        f"📅 التاريخ: {data['payment_date']}"
    )
    recipients = list(list_users_by_role("admin"))
    for assistant in list_users_by_role("assistant", active_only=True):
        perms = get_permissions(assistant["id"])
        if perms.get("view_payments"):
            recipients.append(assistant)
    for user in recipients:
        if user["telegram_id"]:
            try:
                await context.bot.send_message(user["telegram_id"], text)
            except Exception:
                pass


# ============================================================
# REPRESENTATIVE: search customer (🔍 البحث عن عميل)
# ============================================================

async def search_customer_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_timeout(update, context):
        return LOGIN_USERNAME
    session = session_of(context)
    if session["role"] != "representative" and not user_has_permission(session, "search_customers"):
        await update.message.reply_text("⛔ ليست لديك صلاحية البحث عن العملاء.")
        return MAIN_MENU
    await update.message.reply_text("أدخل اسم العميل للبحث:", reply_markup=CANCEL_KB)
    return SEARCH_CUSTOMER


async def search_customer_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = session_of(context)
    name = update.message.text.strip()
    rep_id = session["id"] if session["role"] == "representative" else None
    rows = search_payments_by_customer(name, rep_id)
    if not rows:
        await update.message.reply_text("لا توجد نتائج لهذا العميل.", reply_markup=main_menu_kb(session))
        return MAIN_MENU
    total = get_total(rows)
    lines = [f"📄 نتائج البحث عن: {name}\n"]
    for r in rows[:30]:
        rep_part = f" — {r['rep_name']}" if session["role"] != "representative" else ""
        lines.append(f"• {r['payment_date']} | {r['amount']:,.2f} د.ل | {r['method']}{rep_part}")
    lines.append(f"\nإجمالي السدادات: {total:,.2f} د.ل")
    context.user_data["last_search"] = {"name": name, "rows": rows}
    can_export = session["role"] != "assistant" or user_has_permission(session, "export_pdf")
    buttons = []
    if can_export:
        buttons.append([InlineKeyboardButton("📄 تصدير كشف حساب PDF", callback_data="export_customer_pdf")])
    if session["role"] != "representative" and user_has_permission(session, "edit_payments"):
        buttons.append([InlineKeyboardButton("🔄 نقل هذا العميل لمندوب آخر", callback_data="transfer_customer_start")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)
    await send_main_menu(update, context)
    return MAIN_MENU


async def transfer_customer_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    if session["role"] == "representative" or not user_has_permission(session, "edit_payments"):
        await query.edit_message_text("⛔ ليست لديك صلاحية نقل العملاء بين المندوبين.")
        return
    data = context.user_data.get("last_search")
    if not data:
        await query.edit_message_text("انتهت صلاحية هذه النتيجة، أعد البحث من فضلك.")
        return
    reps = list_users_by_role("representative", active_only=True)
    if not reps:
        await query.edit_message_text("لا يوجد مندوبون نشطون لنقل العميل إليهم.")
        return
    buttons = [[InlineKeyboardButton(r["name"], callback_data=f"transfer_customer_to:{r['id']}")] for r in reps]
    await query.edit_message_text(
        f"اختر المندوب الذي تريد نقل عميل «{data['name']}» إليه:\n"
        f"(سيتم نقل {len(data['rows'])} عملية سداد إلى حسابه)",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def transfer_customer_to_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    if session["role"] == "representative" or not user_has_permission(session, "edit_payments"):
        await query.edit_message_text("⛔ ليست لديك صلاحية نقل العملاء بين المندوبين.")
        return
    data = context.user_data.get("last_search")
    if not data:
        await query.edit_message_text("انتهت صلاحية هذه النتيجة، أعد البحث من فضلك.")
        return
    new_rep_id = int(query.data.split(":")[1])
    new_rep = get_user(new_rep_id)
    payment_ids = [r["id"] for r in data["rows"]]
    transfer_payments(payment_ids, new_rep_id)
    context.user_data.pop("last_search", None)
    await query.edit_message_text(
        f"✅ تم نقل {len(payment_ids)} عملية سداد للعميل «{data['name']}» إلى المندوب: {new_rep['name']}\n\n"
        f"ستظهر هذه العمليات الآن ضمن سدادات وأهداف {new_rep['name']}."
    )


async def export_customer_pdf_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("جاري إنشاء الملف...")
    data = context.user_data.get("last_search")
    if not data:
        await query.message.reply_text("انتهت صلاحية هذه النتيجة، أعد البحث من فضلك.")
        return
    if not await check_pdf_ready(query.message):
        return
    path = f"/tmp/statement_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"
    await safe_send_pdf(
        query.message, generate_customer_statement_pdf, path,
        f"كشف حساب - {data['name']}.pdf", data["name"], data["rows"],
    )


# ============================================================
# REPRESENTATIVE: own report (📊 تقرير السدادات)
# ============================================================

async def rep_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_timeout(update, context):
        return LOGIN_USERNAME
    session = session_of(context)
    rows = get_payments_by_rep(session["id"])
    if not rows:
        await update.message.reply_text("لا توجد سدادات مسجلة بعد.", reply_markup=main_menu_kb(session))
        return MAIN_MENU
    total = get_total(rows)
    target, collected, remaining, pct = current_month_target_progress(session["id"])
    lines = [f"📊 تقرير سدادات: {session['name']}\n"]
    lines.append(target_progress_text(target, collected, remaining, pct))
    lines.append("")
    for r in rows[:30]:
        lines.append(f"• {r['customer_name']} | {r['amount']:,.2f} د.ل | {r['payment_date']} | {r['method']}")
    if len(rows) > 30:
        lines.append(f"... و {len(rows)-30} عملية أخرى")
    lines.append(f"\nإجمالي السدادات (كل الفترات): {total:,.2f} د.ل")
    context.user_data["last_rep_report"] = rows
    context.user_data["last_rep_target"] = (target, collected, remaining, pct)
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📄 تصدير التقرير PDF", callback_data="export_rep_report_pdf")]]),
    )
    await send_main_menu(update, context)
    return MAIN_MENU


async def export_rep_report_pdf_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("جاري إنشاء الملف...")
    session = session_of(context)
    rows = context.user_data.get("last_rep_report")
    if not rows:
        rows = get_payments_by_rep(session["id"])
    target_info = context.user_data.get("last_rep_target") or current_month_target_progress(session["id"])
    if not await check_pdf_ready(query.message):
        return
    path = f"/tmp/rep_report_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"
    await safe_send_pdf(
        query.message, generate_rep_report_pdf, path,
        f"تقرير سدادات - {session['name']}.pdf", session["name"], rows,
        target_info=target_info,
    )

# ============================================================
# ADMIN / ASSISTANT: representatives management (👥 المندوبين)
# ============================================================

async def reps_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_timeout(update, context):
        return LOGIN_USERNAME
    session = session_of(context)
    if not user_has_permission(session, "view_representatives"):
        await update.message.reply_text("⛔ ليست لديك صلاحية لهذا القسم.")
        return MAIN_MENU
    reps = list_users_by_role("representative")
    buttons = [[InlineKeyboardButton(("✅ " if r["active"] else "⛔ ") + r["name"], callback_data=f"rep_view:{r['id']}")] for r in reps]
    text = "👥 قائمة المندوبين:" if reps else "لا يوجد مندوبون مسجلون بعد."
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)
    if user_has_permission(session, "add_representatives"):
        await update.message.reply_text("لإضافة مندوب جديد:", reply_markup=kb([["➕ إضافة مندوب"], ["🔙 رجوع للقائمة الرئيسية"]]))
    else:
        await update.message.reply_text("رجوع:", reply_markup=kb([["🔙 رجوع للقائمة الرئيسية"]]))
    return MAIN_MENU


async def rep_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    rep_id = int(query.data.split(":")[1])
    rep = get_user(rep_id)
    if not rep:
        await query.edit_message_text("هذا المندوب غير موجود.")
        return
    target, collected, remaining, pct = current_month_target_progress(rep_id)
    count = len(get_payments_by_rep(rep_id))
    text = (
        f"اسم المندوب: {rep['name']}\n"
        f"اسم المستخدم: {rep['username']}\n"
        f"الحالة: {'✅ نشط' if rep['active'] else '⛔ موقوف'}\n"
        f"التصنيف: {CATEGORY_LABELS.get(rep['category'], rep['category'])}\n"
        f"{format_last_seen(rep['last_seen'])}\n\n"
        f"{target_progress_text(target, collected, remaining, pct)}\n\n"
        f"عدد عمليات السداد (إجمالي): {count} عملية"
    )
    buttons = []
    if user_has_permission(session, "edit_representatives"):
        buttons.append([InlineKeyboardButton("✏️ تعديل الاسم", callback_data=f"rep_editname:{rep_id}")])
        buttons.append([InlineKeyboardButton("🔒 تغيير الرقم السري", callback_data=f"rep_editpass:{rep_id}")])
        buttons.append([InlineKeyboardButton(("⛔ إيقاف" if rep["active"] else "✅ تفعيل"), callback_data=f"rep_toggle:{rep_id}")])
        other_cat = "professional" if rep["category"] == "home" else "home"
        buttons.append([InlineKeyboardButton(f"🏷️ نقل إلى {CATEGORY_LABELS[other_cat]}", callback_data=f"rep_setcat:{rep_id}:{other_cat}")])
    if user_has_permission(session, "delete_representatives"):
        buttons.append([InlineKeyboardButton("🗑️ حذف المندوب", callback_data=f"rep_delete_confirm:{rep_id}")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


async def rep_setcat_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    rep_id = int(query.data.split(":")[1])
    new_cat = query.data.split(":")[2]
    set_rep_category(rep_id, new_cat)
    await query.answer(f"تم النقل إلى {CATEGORY_LABELS.get(new_cat, new_cat)}")
    query.data = f"rep_view:{rep_id}"
    await rep_view_cb(update, context)


async def rep_toggle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    rep_id = int(query.data.split(":")[1])
    rep = get_user(rep_id)
    set_user_active(rep_id, 0 if rep["active"] else 1)
    await query.answer("تم تحديث حالة المندوب")
    context.user_data["_fake_cb"] = f"rep_view:{rep_id}"
    query.data = f"rep_view:{rep_id}"
    await rep_view_cb(update, context)


async def rep_delete_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rep_id = int(query.data.split(":")[1])
    await query.edit_message_text(
        "⚠️ هل أنت متأكد من حذف هذا المندوب؟ سيتم حذف جميع سداداته المسجلة أيضاً.",
        reply_markup=yesno_kb(f"rep_delete_do:{rep_id}", "noop", "✅ نعم، حذف", "❌ إلغاء"),
    )


async def rep_delete_do_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    rep_id = int(query.data.split(":")[1])
    conn = get_db()
    conn.execute("DELETE FROM payments WHERE representative_id=?", (rep_id,))
    conn.execute("DELETE FROM targets WHERE representative_id=?", (rep_id,))
    conn.commit()
    conn.close()
    delete_user(rep_id)
    await query.answer("تم الحذف")
    await query.edit_message_text("🗑️ تم حذف المندوب بنجاح.")


async def rep_editname_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rep_id = int(query.data.split(":")[1])
    context.user_data["edit_rep_id"] = rep_id
    await query.message.reply_text("أدخل الاسم الجديد للمندوب:", reply_markup=CANCEL_KB)
    return EDIT_REP_NAME


async def edit_rep_name_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rep_id = context.user_data.pop("edit_rep_id", None)
    if rep_id:
        update_user_name(rep_id, update.message.text.strip())
        await update.message.reply_text("✅ تم تحديث اسم المندوب.")
    await send_main_menu(update, context)
    return MAIN_MENU


async def rep_editpass_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rep_id = int(query.data.split(":")[1])
    context.user_data["edit_rep_id"] = rep_id
    await query.message.reply_text("أدخل الرقم السري الجديد للمندوب:", reply_markup=CANCEL_KB)
    return EDIT_REP_PASSWORD


async def edit_rep_pass_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rep_id = context.user_data.pop("edit_rep_id", None)
    password = update.message.text.strip()
    if rep_id:
        if password_in_use(password, exclude_user_id=rep_id):
            context.user_data["edit_rep_id"] = rep_id  # نعيدها لإتاحة محاولة أخرى
            await update.message.reply_text("⚠️ هذا الرقم السري مستخدم بالفعل لحساب آخر. اختر رقماً سرياً مختلفاً:")
            return EDIT_REP_PASSWORD
        update_user_password(rep_id, password)
        await update.message.reply_text("✅ تم تحديث الرقم السري.")
    await send_main_menu(update, context)
    return MAIN_MENU


async def add_rep_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = session_of(context)
    if not user_has_permission(session, "add_representatives"):
        await update.message.reply_text("⛔ ليست لديك صلاحية إضافة مندوبين.")
        return MAIN_MENU
    await update.message.reply_text("اسم المندوب:", reply_markup=CANCEL_KB)
    return ADD_REP_NAME


async def add_rep_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_rep"] = {"name": update.message.text.strip()}
    await update.message.reply_text("اسم المستخدم (username):")
    return ADD_REP_USERNAME


async def add_rep_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.text.strip()
    if get_user_by_username(username):
        await update.message.reply_text("⚠️ اسم المستخدم مستخدم بالفعل، اختر اسماً آخر:")
        return ADD_REP_USERNAME
    context.user_data["new_rep"]["username"] = username
    await update.message.reply_text("الرقم السري:")
    return ADD_REP_PASSWORD


async def add_rep_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    if password_in_use(password):
        await update.message.reply_text("⚠️ هذا الرقم السري مستخدم بالفعل لحساب آخر، لأن الدخول يعتمد على رقم سري فريد. اختر رقماً سرياً مختلفاً:")
        return ADD_REP_PASSWORD
    data = context.user_data.pop("new_rep")
    data["password"] = password
    create_user(data["name"], data["username"], data["password"], "representative")
    await update.message.reply_text(f"✅ تم إضافة المندوب بنجاح: {data['name']}")
    await send_main_menu(update, context)
    return MAIN_MENU


# ============================================================
# ADMIN: assistants management (👨‍💼 المساعدين)
# ============================================================

async def assistants_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_timeout(update, context):
        return LOGIN_USERNAME
    session = session_of(context)
    if session["role"] != "admin":
        await update.message.reply_text("⛔ هذا القسم خاص بالمدير فقط.")
        return MAIN_MENU
    assistants = list_users_by_role("assistant")
    buttons = [[InlineKeyboardButton(f"👑 حسابي (المدير): {session['name']}", callback_data="admin_self_view")]]
    buttons += [[InlineKeyboardButton(("✅ " if a["active"] else "⛔ ") + a["name"], callback_data=f"assist_view:{a['id']}")] for a in assistants]
    text = "👨‍💼 قائمة المساعدين:" if assistants else "لا يوجد مساعدون مسجلون بعد."
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    await update.message.reply_text("لإضافة مساعد جديد:", reply_markup=kb([["➕ إضافة مساعد"], ["🔙 رجوع للقائمة الرئيسية"]]))
    return MAIN_MENU


async def admin_self_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    if session["role"] != "admin":
        await query.edit_message_text("⛔ هذا القسم خاص بالمدير فقط.")
        return
    admin = get_user(session["id"])
    text = f"👑 حسابي (المدير)\n\nالاسم: {admin['name']}\nاسم المستخدم: {admin['username']}"
    buttons = [
        [InlineKeyboardButton("✏️ تغيير الاسم", callback_data="admin_self_editname")],
        [InlineKeyboardButton("🔒 تغيير الرقم السري", callback_data="admin_self_editpass")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def admin_self_editname_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    if session["role"] != "admin":
        return
    context.user_data["edit_rep_id"] = session["id"]
    await query.message.reply_text("أدخل اسمك الجديد:", reply_markup=CANCEL_KB)
    return EDIT_REP_NAME


async def admin_self_editpass_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    if session["role"] != "admin":
        return
    context.user_data["edit_rep_id"] = session["id"]
    await query.message.reply_text("أدخل رقمك السري الجديد:", reply_markup=CANCEL_KB)
    return EDIT_REP_PASSWORD


async def assist_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    assist_id = int(query.data.split(":")[1])
    a = get_user(assist_id)
    perms = get_permissions(assist_id)
    perm_lines = "\n".join(f"{'☑️' if v else '☐'} {PERMISSIONS[k]}" for k, v in perms.items())
    text = f"اسم المساعد: {a['name']}\nاسم المستخدم: {a['username']}\nالحالة: {'✅ نشط' if a['active'] else '⛔ موقوف'}\n{format_last_seen(a['last_seen'])}\n\nالصلاحيات:\n{perm_lines}"
    buttons = [
        [InlineKeyboardButton("⚙️ تعديل الصلاحيات", callback_data=f"assist_perms:{assist_id}")],
        [InlineKeyboardButton("🔒 تغيير الرقم السري", callback_data=f"assist_editpass:{assist_id}")],
        [InlineKeyboardButton(("⛔ إيقاف" if a["active"] else "✅ تفعيل"), callback_data=f"assist_toggle:{assist_id}")],
        [InlineKeyboardButton("🗑️ حذف المساعد", callback_data=f"assist_delete_confirm:{assist_id}")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def assist_toggle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    assist_id = int(query.data.split(":")[1])
    a = get_user(assist_id)
    set_user_active(assist_id, 0 if a["active"] else 1)
    await query.answer("تم تحديث الحالة")
    query.data = f"assist_view:{assist_id}"
    await assist_view_cb(update, context)


async def assist_delete_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    assist_id = int(query.data.split(":")[1])
    await query.edit_message_text(
        "⚠️ هل أنت متأكد من حذف هذا المساعد؟",
        reply_markup=yesno_kb(f"assist_delete_do:{assist_id}", "noop", "✅ نعم، حذف", "❌ إلغاء"),
    )


async def assist_delete_do_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    assist_id = int(query.data.split(":")[1])
    delete_user(assist_id)
    await query.answer("تم الحذف")
    await query.edit_message_text("🗑️ تم حذف المساعد بنجاح.")


async def assist_editpass_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    assist_id = int(query.data.split(":")[1])
    context.user_data["edit_assist_id"] = assist_id
    await query.message.reply_text("أدخل الرقم السري الجديد للمساعد:", reply_markup=CANCEL_KB)
    return EDIT_ASSIST_PASSWORD


async def edit_assist_pass_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assist_id = context.user_data.pop("edit_assist_id", None)
    password = update.message.text.strip()
    if assist_id:
        if password_in_use(password, exclude_user_id=assist_id):
            context.user_data["edit_assist_id"] = assist_id
            await update.message.reply_text("⚠️ هذا الرقم السري مستخدم بالفعل لحساب آخر. اختر رقماً سرياً مختلفاً:")
            return EDIT_ASSIST_PASSWORD
        update_user_password(assist_id, password)
        await update.message.reply_text("✅ تم تحديث الرقم السري.")
    await send_main_menu(update, context)
    return MAIN_MENU


async def assist_perms_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    assist_id = int(query.data.split(":")[1])
    await render_perms_editor(query, assist_id)


async def render_perms_editor(query, assist_id):
    perms = get_permissions(assist_id)
    buttons = [
        [InlineKeyboardButton(f"{'☑️' if v else '☐'} {PERMISSIONS[k]}", callback_data=f"permtoggle:{assist_id}:{k}")]
        for k, v in perms.items()
    ]
    buttons.append([InlineKeyboardButton("✅ تم / حفظ", callback_data=f"assist_view:{assist_id}")])
    await query.edit_message_text("⚙️ صلاحيات المساعد (اضغط لتفعيل/تعطيل):", reply_markup=InlineKeyboardMarkup(buttons))


async def perm_toggle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, assist_id, key = query.data.split(":")
    assist_id = int(assist_id)
    perms = get_permissions(assist_id)
    set_permission(assist_id, key, not perms.get(key, False))
    await query.answer()
    await render_perms_editor(query, assist_id)


async def add_assist_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = session_of(context)
    if session["role"] != "admin":
        return MAIN_MENU
    await update.message.reply_text("اسم المساعد:", reply_markup=CANCEL_KB)
    return ADD_ASSIST_NAME


async def add_assist_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_assist"] = {"name": update.message.text.strip()}
    await update.message.reply_text("اسم المستخدم (username):")
    return ADD_ASSIST_USERNAME


async def add_assist_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.text.strip()
    if get_user_by_username(username):
        await update.message.reply_text("⚠️ اسم المستخدم مستخدم بالفعل، اختر اسماً آخر:")
        return ADD_ASSIST_USERNAME
    context.user_data["new_assist"]["username"] = username
    await update.message.reply_text("الرقم السري:")
    return ADD_ASSIST_PASSWORD


async def add_assist_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    if password_in_use(password):
        await update.message.reply_text("⚠️ هذا الرقم السري مستخدم بالفعل لحساب آخر، لأن الدخول يعتمد على رقم سري فريد. اختر رقماً سرياً مختلفاً:")
        return ADD_ASSIST_PASSWORD
    data = context.user_data.pop("new_assist")
    data["password"] = password
    uid = create_user(data["name"], data["username"], data["password"], "assistant")
    ensure_default_perms(uid)
    await update.message.reply_text(f"✅ تم إضافة المساعد: {data['name']}\n\nيمكنك الآن تحديد صلاحياته من قائمة 👨‍💼 المساعدين.")
    await send_main_menu(update, context)
    return MAIN_MENU

# ============================================================
# ADMIN / ASSISTANT: targets (🎯 أهداف التحصيل)
# ============================================================

async def targets_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_timeout(update, context):
        return LOGIN_USERNAME
    session = session_of(context)
    if not user_has_permission(session, "manage_targets"):
        await update.message.reply_text("⛔ ليست لديك صلاحية لهذا القسم.")
        return MAIN_MENU
    reps = list_users_by_role("representative", active_only=True)
    if not reps:
        await update.message.reply_text("لا يوجد مندوبون نشطون بعد.", reply_markup=main_menu_kb(session))
        return MAIN_MENU
    buttons = [[InlineKeyboardButton(r["name"], callback_data=f"target_pick:{r['id']}")] for r in reps]
    await update.message.reply_text("اختر المندوب لتحديد هدفه الشهري:", reply_markup=InlineKeyboardMarkup(buttons))
    await update.message.reply_text("يمكنك الإلغاء في أي وقت:", reply_markup=CANCEL_KB)
    return MAIN_MENU


async def target_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rep_id = int(query.data.split(":")[1])
    rep = get_user(rep_id)
    m, y = month_year_now()
    current = get_target(rep_id, m, y)
    text = f"المندوب: {rep['name']}\nالشهر الحالي: {MONTHS_AR[m-1]} {y}\nالهدف الحالي: {current:,.2f} د.ل" if current else \
           f"المندوب: {rep['name']}\nالشهر الحالي: {MONTHS_AR[m-1]} {y}\nلا يوجد هدف محدد بعد لهذا الشهر."
    buttons = [[InlineKeyboardButton("✏️ تحديد / تعديل الهدف", callback_data=f"target_set:{rep_id}")]]
    if current:
        buttons.append([InlineKeyboardButton("🗑️ حذف الهدف الحالي", callback_data=f"target_delete_confirm:{rep_id}")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def target_set_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rep_id = int(query.data.split(":")[1])
    context.user_data["target_rep_id"] = rep_id
    context.user_data["kp_target"] = "target"
    context.user_data["kp_value"] = ""
    rep = get_user(rep_id)
    m, y = month_year_now()
    await query.edit_message_text(f"المندوب: {rep['name']}\nالشهر الحالي: {MONTHS_AR[m-1]} {y}")
    await query.message.reply_text(
        "أدخل قيمة الهدف الشهري باستخدام لوحة الأرقام:\n\nالقيمة الحالية: 0",
        reply_markup=build_keypad_kb(""),
    )
    return TARGET_AMOUNT


async def target_delete_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rep_id = int(query.data.split(":")[1])
    rep = get_user(rep_id)
    await query.edit_message_text(
        f"⚠️ هل تريد حذف هدف {rep['name']} لهذا الشهر؟",
        reply_markup=yesno_kb(f"target_delete_do:{rep_id}", "noop", "✅ نعم، حذف", "❌ إلغاء"),
    )


async def target_delete_do_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("تم الحذف")
    rep_id = int(query.data.split(":")[1])
    m, y = month_year_now()
    delete_target(rep_id, m, y)
    rep = get_user(rep_id)
    await query.edit_message_text(f"🗑️ تم حذف هدف {rep['name']} لشهر {MONTHS_AR[m-1]} {y}.")


async def target_amount_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ الرجاء إدخال رقم صحيح أكبر من صفر:")
        return TARGET_AMOUNT
    target = context.user_data.pop("kp_target", None)
    context.user_data.pop("kp_value", None)
    if target == "edit_payment":
        payment_id = context.user_data.pop("edit_payment_id", None)
        session = session_of(context)
        if payment_id and user_has_permission(session, "edit_payments"):
            update_payment_amount(payment_id, amount)
            await update.message.reply_text(f"✅ تم تحديث قيمة السداد إلى: {amount:,.2f} د.ل")
        await send_main_menu(update, context)
        return MAIN_MENU
    if target and target.startswith("category:"):
        _, category, month, year = target.split(":")
        set_category_target(category, int(month), int(year), amount)
        await update.message.reply_text(f"✅ تم حفظ هدف {CATEGORY_LABELS[category]} لشهر {month}-{year}: {amount:,.2f} د.ل")
        await send_main_menu(update, context)
        return MAIN_MENU
    if target == "expense_amount":
        context.user_data["expense"] = {"amount": amount}
        await update.message.reply_text(f"💵 قيمة المصروف: {amount:,.2f} د.ل\n\nأدخل بيان الصرف (وصف قصير للمصروف):", reply_markup=CANCEL_KB)
        return EXPENSE_DESC
    if target == "payroll_new_fixed":
        new_emp = context.user_data.pop("payroll_new", {})
        add_payroll_employee(new_emp.get("name", ""), "fixed", fixed_amount=amount, classification=new_emp.get("classification"))
        await update.message.reply_text(f"✅ تم إضافة الموظف: {new_emp.get('name','')}\nراتب ثابت: {amount:,.2f} د.ل")
        await send_main_menu(update, context)
        return MAIN_MENU
    if target == "payroll_new_rate":
        new_emp = context.user_data.pop("payroll_new", {})
        add_payroll_employee(new_emp.get("name", ""), "commission", commission_rate=amount, linked_rep_id=new_emp.get("linked_rep_id"), classification=new_emp.get("classification"))
        rep = get_user(new_emp.get("linked_rep_id"))
        await update.message.reply_text(
            f"✅ تم إضافة الموظف: {new_emp.get('name','')}\nنسبة العمولة: {amount:g}%\nمرتبط بالمندوب: {rep['name'] if rep else '-'}"
        )
        await send_main_menu(update, context)
        return MAIN_MENU
    if target and target.startswith("payroll_edit:"):
        employee_id = int(target.split(":")[1])
        emp = get_payroll_employee(employee_id)
        if emp:
            update_payroll_employee_amount(employee_id, emp["emp_type"], amount)
            label = "الراتب الثابت" if emp["emp_type"] == "fixed" else "نسبة العمولة"
            suffix = " د.ل" if emp["emp_type"] == "fixed" else "%"
            await update.message.reply_text(f"✅ تم تحديث {label} لـ {emp['name']}: {amount:g}{suffix}")
        await send_main_menu(update, context)
        return MAIN_MENU
    rep_id = context.user_data.pop("target_rep_id", None)
    if rep_id:
        m, y = month_year_now()
        set_target(rep_id, m, y, amount)
        rep = get_user(rep_id)
        await update.message.reply_text(f"✅ تم حفظ الهدف الشهري لـ {rep['name']}: {amount:,.2f} د.ل")
    await send_main_menu(update, context)
    return MAIN_MENU


# ============================================================
# ADMIN / ASSISTANT: all payments overview (💰 الجباية)
# ============================================================

def category_permission_for(category):
    return "manage_home_target" if category == "home" else "manage_professional_target"


def render_category_report(category, month, year):
    target, collected, remaining, pct, rows = category_target_progress(category, month, year)
    reps = [u for u in list_users_by_role("representative") if u["category"] == category]
    rep_names = "، ".join(r["name"] for r in reps) if reps else "لا يوجد مندوبون في هذا التصنيف بعد"
    label = CATEGORY_LABELS[category]
    text = (
        f"{label}\n"
        f"الشهر: {month}-{year}\n\n"
        f"المندوبون في هذا التصنيف: {rep_names}\n\n"
        f"{target_progress_text(target, collected, remaining, pct)}"
    )
    buttons = [
        [InlineKeyboardButton("✏️ تحديد/تعديل الهدف لهذا الشهر", callback_data=f"cattarget_set:{category}:{month}:{year}")],
    ]
    if target:
        buttons.append([InlineKeyboardButton("🗑️ حذف هدف هذا الشهر", callback_data=f"cattarget_delete:{category}:{month}:{year}")])
    buttons.append([InlineKeyboardButton("📅 اختيار شهر آخر (بالأرقام)", callback_data=f"catmonth:{category}:nav:{year}")])
    return text, buttons, rows, (target, collected, remaining, pct), label


async def category_target_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, category):
    if await check_timeout(update, context):
        return LOGIN_USERNAME
    session = session_of(context)
    if session["role"] != "admin" and not user_has_permission(session, category_permission_for(category)):
        await update.message.reply_text("⛔ ليست لديك صلاحية لهذا القسم.")
        return MAIN_MENU
    month, year = month_year_now()
    text, buttons, rows, target_info, label = render_category_report(category, month, year)
    context.user_data["last_report"] = ("general", rows, f"{label} - شهر {month}-{year}", target_info)
    if user_has_permission(session, "export_pdf"):
        buttons.append([InlineKeyboardButton("📄 تصدير PDF", callback_data="export_report_pdf")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    return MAIN_MENU


async def home_use_target_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await category_target_menu(update, context, "home")


async def professional_use_target_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await category_target_menu(update, context, "professional")


async def catmonth_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    parts = query.data.split(":")  # catmonth : category : nav/pick : year [: month]
    category = parts[1]
    if session["role"] != "admin" and not user_has_permission(session, category_permission_for(category)):
        await query.edit_message_text("⛔ ليست لديك صلاحية لهذا القسم.")
        return
    action = parts[2]
    if action == "nav":
        year = int(parts[3])
        await query.edit_message_reply_markup(reply_markup=month_number_kb(f"catmonth:{category}", year))
        return
    year, month = int(parts[3]), int(parts[4])
    text, buttons, rows, target_info, label = render_category_report(category, month, year)
    context.user_data["last_report"] = ("general", rows, f"{label} - شهر {month}-{year}", target_info)
    if user_has_permission(session, "export_pdf"):
        buttons.append([InlineKeyboardButton("📄 تصدير PDF", callback_data="export_report_pdf")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def cattarget_set_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    _, category, month, year = query.data.split(":")
    if session["role"] != "admin" and not user_has_permission(session, category_permission_for(category)):
        await query.edit_message_text("⛔ ليست لديك صلاحية لهذا القسم.")
        return
    context.user_data["kp_target"] = f"category:{category}:{month}:{year}"
    context.user_data["kp_value"] = ""
    await query.edit_message_text(f"{CATEGORY_LABELS[category]}\nالشهر: {month}-{year}")
    await query.message.reply_text(
        "أدخل قيمة الهدف الشهري باستخدام لوحة الأرقام:\n\nالقيمة الحالية: 0",
        reply_markup=build_keypad_kb(""),
    )
    return TARGET_AMOUNT


async def cattarget_delete_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, category, month, year = query.data.split(":")
    await query.edit_message_text(
        f"⚠️ هل تريد حذف هدف {CATEGORY_LABELS[category]} لشهر {month}-{year}؟",
        reply_markup=yesno_kb(f"cattarget_delete_do:{category}:{month}:{year}", "noop", "✅ نعم، حذف", "❌ إلغاء"),
    )


async def cattarget_delete_do_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("تم الحذف")
    _, category, month, year = query.data.split(":")
    delete_category_target(category, int(month), int(year))
    await query.edit_message_text(f"🗑️ تم حذف هدف {CATEGORY_LABELS[category]} لشهر {month}-{year}.")


# ============================================================
# ADMIN / ASSISTANT: expenses (💵 مصاريف)
# ============================================================

def expense_attribution_name(user_row):
    return user_row["name"]


async def expenses_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_timeout(update, context):
        return LOGIN_USERNAME
    session = session_of(context)
    if session["role"] != "admin" and not user_has_permission(session, "manage_expenses"):
        await update.message.reply_text("⛔ ليست لديك صلاحية لهذا القسم.")
        return MAIN_MENU
    buttons = [
        [InlineKeyboardButton("➕ إضافة مصروف", callback_data="expense_add_start")],
        [InlineKeyboardButton("📊 تقرير المصاريف", callback_data="expense_report_menu")],
    ]
    await update.message.reply_text("💵 المصاريف:", reply_markup=InlineKeyboardMarkup(buttons))
    return MAIN_MENU


async def expense_add_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["expense"] = {}
    context.user_data["kp_target"] = "expense_amount"
    context.user_data["kp_value"] = ""
    await query.edit_message_text("➕ إضافة مصروف جديد")
    await query.message.reply_text(
        "أدخل قيمة المصروف باستخدام لوحة الأرقام:\n\nالقيمة الحالية: 0",
        reply_markup=build_keypad_kb(""),
    )
    return TARGET_AMOUNT


async def expense_desc_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("expense", {})["description"] = update.message.text.strip()
    now = datetime.now()
    await update.message.reply_text(
        "📅 اختر تاريخ المصروف (أو اكتبه يدوياً بصيغة YYYY-MM-DD):",
        reply_markup=build_calendar_kb(now.year, now.month, prefix="expdate"),
    )
    return EXPENSE_FLOW


def expense_attribution_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 خاص بمندوب", callback_data="expattr:representative")],
        [InlineKeyboardButton("👨‍💼 خاص بالإدارة/مساعد", callback_data="expattr:assistant")],
        [InlineKeyboardButton("🏢 مصروف عام للشركة", callback_data="expattr:department")],
        [InlineKeyboardButton("🎁 مكافأة", callback_data="expattr:bonus")],
    ])


async def expdate_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":")
    if parts[1] == "nav":
        await query.answer()
        y, m = int(parts[2]), int(parts[3])
        await query.edit_message_reply_markup(reply_markup=build_calendar_kb(y, m, prefix="expdate"))
        return EXPENSE_FLOW
    await query.answer()
    if parts[1] == "today":
        date_str = datetime.now().strftime("%Y-%m-%d")
    else:
        y, m, d = int(parts[2]), int(parts[3]), int(parts[4])
        date_str = f"{y:04d}-{m:02d}-{d:02d}"
    context.user_data.setdefault("expense", {})["date"] = date_str
    await query.edit_message_text(f"📅 تاريخ المصروف: {date_str}\n\nهذا المصروف يخص:", reply_markup=expense_attribution_kb())
    return EXPENSE_FLOW


async def expense_date_text_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        await update.message.reply_text("⚠️ صيغة التاريخ غير صحيحة. استخدم YYYY-MM-DD أو اختر من التقويم أعلاه:")
        return EXPENSE_FLOW
    context.user_data.setdefault("expense", {})["date"] = text
    await update.message.reply_text(f"📅 تاريخ المصروف: {text}\n\nهذا المصروف يخص:", reply_markup=expense_attribution_kb())
    return EXPENSE_FLOW


async def expattr_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    attr_type = query.data.split(":")[1]
    if attr_type == "representative":
        reps = list_users_by_role("representative", active_only=True)
        if not reps:
            await query.edit_message_text("لا يوجد مندوبون نشطون حالياً.")
            return EXPENSE_FLOW
        buttons = [[InlineKeyboardButton(r["name"], callback_data=f"expattrpick:representative:{r['id']}")] for r in reps]
        await query.edit_message_text("اختر المندوب:", reply_markup=InlineKeyboardMarkup(buttons))
    elif attr_type == "assistant":
        session = session_of(context)
        assistants = list_users_by_role("assistant", active_only=True)
        admin_row = get_user(session["id"]) if session["role"] == "admin" else None
        buttons = []
        if admin_row:
            buttons.append([InlineKeyboardButton(f"👑 {admin_row['name']} (المدير)", callback_data=f"expattrpick:assistant:{admin_row['id']}")])
        buttons += [[InlineKeyboardButton(a["name"], callback_data=f"expattrpick:assistant:{a['id']}")] for a in assistants]
        if not buttons:
            await query.edit_message_text("لا يوجد مساعدون نشطون حالياً.")
            return EXPENSE_FLOW
        await query.edit_message_text("اختر الشخص المسؤول (إدارة/مساعد):", reply_markup=InlineKeyboardMarkup(buttons))
    elif attr_type == "department":
        buttons = [[InlineKeyboardButton(d, callback_data=f"expattrpick:department:{i}")] for i, d in enumerate(EXPENSE_DEPARTMENTS)]
        await query.edit_message_text("اختر القسم:", reply_markup=InlineKeyboardMarkup(buttons))
    else:  # bonus
        buttons = [[InlineKeyboardButton(label, callback_data=f"expattrpick:bonus:{key}")] for key, label in CLASSIFICATION_LABELS.items()]
        await query.edit_message_text("🎁 هذه المكافأة لفئة:", reply_markup=InlineKeyboardMarkup(buttons))
    return EXPENSE_FLOW


async def expattrpick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, attr_type, value = query.data.split(":")
    expense = context.user_data.setdefault("expense", {})
    if attr_type == "department":
        name = EXPENSE_DEPARTMENTS[int(value)]
        expense["attribution_type"] = "department"
        expense["attribution_id"] = None
        expense["attribution_name"] = name
    elif attr_type == "bonus":
        name = CLASSIFICATION_LABELS[value]
        expense["attribution_type"] = "bonus"
        expense["attribution_id"] = None
        expense["attribution_name"] = f"🎁 مكافأة - {name}"
    else:
        user_row = get_user(int(value))
        expense["attribution_type"] = attr_type
        expense["attribution_id"] = user_row["id"]
        expense["attribution_name"] = user_row["name"]
    summary = (
        "يرجى تأكيد بيانات المصروف:\n\n"
        f"💵 القيمة: {expense['amount']:,.2f} د.ل\n"
        f"📝 البيان: {expense['description']}\n"
        f"📅 التاريخ: {expense['date']}\n"
        f"🏷️ الفئة: {expense['attribution_name']}"
    )
    await query.edit_message_text(summary, reply_markup=yesno_kb("expense_save", "expense_cancel", "💾 حفظ المصروف", "❌ إلغاء"))
    return EXPENSE_FLOW


async def expense_save_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    expense = context.user_data.pop("expense", {})
    if query.data == "expense_cancel" or not expense:
        await query.edit_message_text("تم الإلغاء.")
        await send_main_menu(query, context)
        return MAIN_MENU
    add_expense(
        expense["amount"], expense["description"], expense["date"],
        expense["attribution_type"], expense.get("attribution_id"), expense["attribution_name"],
        session["id"],
    )
    await query.edit_message_text(
        "✅ تم تسجيل المصروف بنجاح\n\n"
        f"💵 القيمة: {expense['amount']:,.2f} د.ل\n"
        f"📝 البيان: {expense['description']}\n"
        f"📅 التاريخ: {expense['date']}\n"
        f"🏷️ الفئة: {expense['attribution_name']}"
    )
    await query.message.reply_text("العملية التالية:", reply_markup=main_menu_kb(session))
    return MAIN_MENU


async def expense_report_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    buttons = [
        [InlineKeyboardButton("📋 كل المصاريف", callback_data="expreport:all")],
        [InlineKeyboardButton("👤 حسب مندوب", callback_data="expreport:representative")],
        [InlineKeyboardButton("👨‍💼 حسب مساعد/إدارة", callback_data="expreport:assistant")],
        [InlineKeyboardButton("🏢 حسب قسم", callback_data="expreport:department")],
        [InlineKeyboardButton("🎁 حسب مكافآت", callback_data="expreport:bonus")],
        [InlineKeyboardButton("📅 حسب شهر", callback_data="expreport:month")],
    ]
    await query.edit_message_text("📊 تقرير المصاريف — اختر التصفية:", reply_markup=InlineKeyboardMarkup(buttons))


def show_expense_report(rows, label):
    total = get_total_expenses(rows)
    lines = [f"📊 تقرير المصاريف — {label}\n"]
    for r in rows[:30]:
        lines.append(f"• {r['expense_date']} | {r['amount']:,.2f} د.ل | {r['description']} | {r['attribution_name'] or '-'}")
    if len(rows) > 30:
        lines.append(f"... و {len(rows)-30} عملية أخرى")
    lines.append(f"\nإجمالي المصاريف: {total:,.2f} د.ل")
    return "\n".join(lines)


async def expreport_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    kind = query.data.split(":")[1]
    if kind == "all":
        rows = get_expenses()
        text = show_expense_report(rows, "كل المصاريف")
        context.user_data["last_report"] = ("expenses", rows, "كل المصاريف", None)
        buttons = [[InlineKeyboardButton("📄 تصدير PDF", callback_data="export_report_pdf")]] if user_has_permission(session, "export_pdf") else None
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)
    elif kind == "representative":
        reps = list_users_by_role("representative")
        buttons = [[InlineKeyboardButton(r["name"], callback_data=f"expreportpick:representative:{r['id']}")] for r in reps]
        await query.edit_message_text("اختر المندوب:", reply_markup=InlineKeyboardMarkup(buttons))
    elif kind == "assistant":
        assistants = list_users_by_role("assistant")
        admin_row = get_user(session["id"]) if session["role"] == "admin" else None
        buttons = []
        if admin_row:
            buttons.append([InlineKeyboardButton(f"👑 {admin_row['name']} (المدير)", callback_data=f"expreportpick:assistant:{admin_row['id']}")])
        buttons += [[InlineKeyboardButton(a["name"], callback_data=f"expreportpick:assistant:{a['id']}")] for a in assistants]
        await query.edit_message_text("اختر الشخص:", reply_markup=InlineKeyboardMarkup(buttons))
    elif kind == "department":
        buttons = [[InlineKeyboardButton(d, callback_data=f"expreportpick:department:{i}")] for i, d in enumerate(EXPENSE_DEPARTMENTS)]
        await query.edit_message_text("اختر القسم:", reply_markup=InlineKeyboardMarkup(buttons))
    elif kind == "bonus":
        buttons = [[InlineKeyboardButton(label, callback_data=f"expreportpick:bonus:{key}")] for key, label in CLASSIFICATION_LABELS.items()]
        await query.edit_message_text("اختر الفئة:", reply_markup=InlineKeyboardMarkup(buttons))
    elif kind == "month":
        now = datetime.now()
        await query.edit_message_text("📅 اختر الشهر (بالأرقام):", reply_markup=month_number_kb("expmonth", now.year))


async def expreportpick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    _, attr_type, value = query.data.split(":")
    if attr_type == "department":
        name = EXPENSE_DEPARTMENTS[int(value)]
        rows = get_expenses(attribution_type="department", attribution_name=name)
        label = name
    elif attr_type == "bonus":
        name = f"🎁 مكافأة - {CLASSIFICATION_LABELS[value]}"
        rows = get_expenses(attribution_type="bonus", attribution_name=name)
        label = name
    else:
        user_row = get_user(int(value))
        rows = get_expenses(attribution_type=attr_type, attribution_id=user_row["id"])
        label = user_row["name"]
    text = show_expense_report(rows, label)
    context.user_data["last_report"] = ("expenses", rows, label, None)
    buttons = [[InlineKeyboardButton("📄 تصدير PDF", callback_data="export_report_pdf")]] if user_has_permission(session, "export_pdf") else None
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


async def expmonth_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    parts = query.data.split(":")
    if parts[1] == "nav":
        year = int(parts[2])
        await query.edit_message_reply_markup(reply_markup=month_number_kb("expmonth", year))
        return
    year, month = int(parts[2]), int(parts[3])
    rows = get_expenses(month=month, year=year)
    label = f"شهر {month}-{year}"
    text = show_expense_report(rows, label)
    context.user_data["last_report"] = ("expenses", rows, label, None)
    buttons = [[InlineKeyboardButton("📄 تصدير PDF", callback_data="export_report_pdf")]] if user_has_permission(session, "export_pdf") else None
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


# ============================================================
# ADMIN / ASSISTANT: payroll (💼 الرواتب)
# ============================================================

EMP_TYPE_LABELS = {"fixed": "🔒 راتب ثابت", "commission": "📊 عمولة على التحصيل"}


async def payroll_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_timeout(update, context):
        return LOGIN_USERNAME
    session = session_of(context)
    if session["role"] != "admin" and not user_has_permission(session, "manage_payroll"):
        await update.message.reply_text("⛔ ليست لديك صلاحية لهذا القسم.")
        return MAIN_MENU
    buttons = [
        [InlineKeyboardButton("➕ إضافة موظف جديد", callback_data="payroll_add_start")],
        [InlineKeyboardButton("📋 قائمة الموظفين / صرف راتب", callback_data="payroll_list")],
        [InlineKeyboardButton("📊 تقرير الرواتب", callback_data="payroll_report")],
    ]
    await update.message.reply_text("💼 الرواتب:", reply_markup=InlineKeyboardMarkup(buttons))
    return MAIN_MENU


async def payroll_add_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["payroll_new"] = {}
    await query.edit_message_text("➕ إضافة موظف جديد")
    await query.message.reply_text("أدخل اسم الموظف:", reply_markup=CANCEL_KB)
    return PAYROLL_EMP_NAME


async def payroll_emp_name_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("payroll_new", {})["name"] = update.message.text.strip()
    buttons = [[InlineKeyboardButton(label, callback_data=f"payroll_class:{key}")] for key, label in CLASSIFICATION_LABELS.items()]
    await update.message.reply_text("اختر تصنيف الموظف:", reply_markup=InlineKeyboardMarkup(buttons))
    return PAYROLL_EMP_AMOUNT


async def payroll_class_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    classification = query.data.split(":")[1]
    context.user_data.setdefault("payroll_new", {})["classification"] = classification
    buttons = [
        [InlineKeyboardButton("🔒 راتب ثابت", callback_data="payroll_type:fixed")],
        [InlineKeyboardButton("📊 عمولة على التحصيل", callback_data="payroll_type:commission")],
    ]
    await query.edit_message_text(f"التصنيف: {CLASSIFICATION_LABELS[classification]}\n\nاختر نوع الموظف:", reply_markup=InlineKeyboardMarkup(buttons))
    return PAYROLL_EMP_AMOUNT


async def payroll_type_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    emp_type = query.data.split(":")[1]
    if emp_type == "fixed":
        context.user_data["kp_target"] = "payroll_new_fixed"
        context.user_data["kp_value"] = ""
        await query.edit_message_text("🔒 راتب ثابت")
        await query.message.reply_text(
            "أدخل قيمة الراتب الشهري باستخدام لوحة الأرقام:\n\nالقيمة الحالية: 0",
            reply_markup=build_keypad_kb(""),
        )
        return TARGET_AMOUNT
    reps = list_users_by_role("representative", active_only=True)
    if not reps:
        await query.edit_message_text("لا يوجد مندوبون نشطون حالياً لربط العمولة بهم.")
        return MAIN_MENU
    buttons = [[InlineKeyboardButton(r["name"], callback_data=f"payroll_link_rep:{r['id']}")] for r in reps]
    await query.edit_message_text("📊 عمولة على التحصيل\n\nاختر المندوب المرتبط بهذه العمولة:", reply_markup=InlineKeyboardMarkup(buttons))
    return PAYROLL_EMP_AMOUNT


async def payroll_link_rep_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rep_id = int(query.data.split(":")[1])
    rep = get_user(rep_id)
    context.user_data.setdefault("payroll_new", {})["linked_rep_id"] = rep_id
    context.user_data["kp_target"] = "payroll_new_rate"
    context.user_data["kp_value"] = ""
    await query.edit_message_text(f"المندوب المرتبط: {rep['name']}")
    await query.message.reply_text(
        "أدخل نسبة العمولة % باستخدام لوحة الأرقام (مثال: أدخل 5 لتعني 5%):\n\nالقيمة الحالية: 0",
        reply_markup=build_keypad_kb(""),
    )
    return TARGET_AMOUNT


async def payroll_list_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    employees = list_payroll_employees()
    if not employees:
        await query.edit_message_text("لا يوجد موظفون مسجّلون بعد. استخدم «➕ إضافة موظف جديد».")
        return
    buttons = [
        [InlineKeyboardButton(f"{CLASSIFICATION_LABELS.get(e['classification'], '👤').split()[0]} {e['name']}", callback_data=f"payroll_view:{e['id']}")]
        for e in employees
    ]
    await query.edit_message_text("📋 قائمة الموظفين:", reply_markup=InlineKeyboardMarkup(buttons))


async def payroll_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    employee_id = int(query.data.split(":")[1])
    emp = get_payroll_employee(employee_id)
    if not emp:
        await query.edit_message_text("هذا الموظف لم يعد موجوداً.")
        return
    lines = [f"👤 {emp['name']}", f"التصنيف: {CLASSIFICATION_LABELS.get(emp['classification'], '-')}", f"النوع: {EMP_TYPE_LABELS[emp['emp_type']]}"]
    if emp["emp_type"] == "fixed":
        lines.append(f"الراتب الثابت: {emp['fixed_amount']:,.2f} د.ل")
    else:
        rep = get_user(emp["linked_rep_id"]) if emp["linked_rep_id"] else None
        lines.append(f"نسبة العمولة: {emp['commission_rate']:g}%")
        lines.append(f"المندوب المرتبط: {rep['name'] if rep else '-'}")
        lines.append(f"الرصيد المحتجز (كنترول 1%): {emp['retained_balance']:,.2f} د.ل")
    buttons = [
        [InlineKeyboardButton("💰 صرف راتب هذا الشهر", callback_data=f"payroll_pay_start:{employee_id}")],
        [InlineKeyboardButton("✏️ تعديل القيمة/النسبة", callback_data=f"payroll_editamt:{employee_id}")],
    ]
    if emp["emp_type"] == "commission" and emp["retained_balance"] > 0:
        buttons.append([InlineKeyboardButton("🏦 صرف الرصيد المحتجز", callback_data=f"payroll_release:{employee_id}")])
    buttons.append([InlineKeyboardButton("🗑️ حذف الموظف", callback_data=f"payroll_delete_confirm:{employee_id}")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def payroll_editamt_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    employee_id = int(query.data.split(":")[1])
    emp = get_payroll_employee(employee_id)
    if not emp:
        await query.edit_message_text("هذا الموظف لم يعد موجوداً.")
        return
    context.user_data["kp_target"] = f"payroll_edit:{employee_id}"
    context.user_data["kp_value"] = ""
    label = "الراتب الثابت الجديد" if emp["emp_type"] == "fixed" else "نسبة العمولة الجديدة %"
    await query.edit_message_text(f"تعديل {emp['name']}")
    await query.message.reply_text(
        f"أدخل {label} باستخدام لوحة الأرقام:\n\nالقيمة الحالية: 0",
        reply_markup=build_keypad_kb(""),
    )
    return TARGET_AMOUNT


async def payroll_pay_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    employee_id = int(query.data.split(":")[1])
    emp = get_payroll_employee(employee_id)
    if not emp:
        await query.edit_message_text("هذا الموظف لم يعد موجوداً.")
        return
    month, year = month_year_now()
    if emp["emp_type"] == "fixed":
        gross = emp["fixed_amount"] or 0
        retained = 0
        paid = gross
        text = (
            f"صرف راتب {emp['name']} — شهر {month}-{year}\n\n"
            f"الراتب الثابت: {gross:,.2f} د.ل\n"
            f"الصافي المستحق للصرف: {paid:,.2f} د.ل"
        )
    else:
        rep = get_user(emp["linked_rep_id"])
        collected = get_total(get_payments_by_rep(emp["linked_rep_id"], month, year)) if rep else 0
        gross = collected * (emp["commission_rate"] or 0) / 100
        retained = gross * RETENTION_RATE
        paid = gross - retained
        text = (
            f"صرف عمولة {emp['name']} — شهر {month}-{year}\n\n"
            f"إجمالي تحصيل {rep['name'] if rep else '-'} هذا الشهر: {collected:,.2f} د.ل\n"
            f"نسبة العمولة: {emp['commission_rate']:g}%\n"
            f"إجمالي العمولة: {gross:,.2f} د.ل\n"
            f"محتجز (كنترول 1%): {retained:,.2f} د.ل\n"
            f"الصافي المستحق للصرف: {paid:,.2f} د.ل"
        )
        context.user_data["payroll_pay_collected"] = collected
    context.user_data["payroll_pay"] = {"employee_id": employee_id, "gross": gross, "retained": retained, "paid": paid, "month": month, "year": year}
    await query.edit_message_text(text, reply_markup=yesno_kb("payroll_pay_confirm", "payroll_pay_cancel", "✅ تأكيد الصرف", "❌ إلغاء"))


async def payroll_pay_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    data = context.user_data.pop("payroll_pay", None)
    collected = context.user_data.pop("payroll_pay_collected", None)
    if query.data == "payroll_pay_cancel" or not data:
        await query.edit_message_text("تم الإلغاء.")
        return
    emp = get_payroll_employee(data["employee_id"])
    add_payroll_payment(
        data["employee_id"], data["month"], data["year"], collected,
        data["gross"], data["retained"], data["paid"], session["id"],
    )
    if data["retained"]:
        add_to_retained_balance(data["employee_id"], data["retained"])
    await query.edit_message_text(f"✅ تم صرف راتب {emp['name']} بنجاح — الصافي: {data['paid']:,.2f} د.ل")


async def payroll_release_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    employee_id = int(query.data.split(":")[1])
    emp = get_payroll_employee(employee_id)
    await query.edit_message_text(
        f"⚠️ هل تريد صرف الرصيد المحتجز لـ {emp['name']} بالكامل ({emp['retained_balance']:,.2f} د.ل)؟",
        reply_markup=yesno_kb(f"payroll_release_do:{employee_id}", "noop", "✅ نعم، اصرف", "❌ إلغاء"),
    )


async def payroll_release_do_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("تم الصرف")
    session = session_of(context)
    employee_id = int(query.data.split(":")[1])
    emp = get_payroll_employee(employee_id)
    amount = emp["retained_balance"]
    m, y = month_year_now()
    add_payroll_payment(employee_id, m, y, None, amount, 0, amount, session["id"], kind="retention_release")
    release_retained_balance(employee_id)
    await query.edit_message_text(f"🏦 تم صرف الرصيد المحتجز لـ {emp['name']}: {amount:,.2f} د.ل")


async def payroll_delete_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    employee_id = int(query.data.split(":")[1])
    emp = get_payroll_employee(employee_id)
    await query.edit_message_text(
        f"⚠️ هل تريد حذف الموظف {emp['name']}؟ سيُحذف معه كل سجل صرف رواتبه.",
        reply_markup=yesno_kb(f"payroll_delete_do:{employee_id}", "noop", "✅ نعم، حذف", "❌ إلغاء"),
    )


async def payroll_delete_do_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("تم الحذف")
    employee_id = int(query.data.split(":")[1])
    delete_payroll_employee(employee_id)
    await query.edit_message_text("🗑️ تم حذف الموظف بنجاح.")


async def payroll_report_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    rows = get_payroll_payments()
    if not rows:
        await query.edit_message_text("لا توجد سجلات صرف رواتب بعد.")
        return
    total_paid = sum(r["paid_amount"] for r in rows)
    total_retained = sum(r["retained_amount"] for r in rows)
    lines = ["📊 تقرير الرواتب\n"]
    for r in rows[:30]:
        kind_label = "صرف راتب" if r["kind"] == "payout" else "صرف رصيد محتجز"
        lines.append(f"• {r['employee_name']} | {kind_label} | {r['paid_amount']:,.2f} د.ل | {r['period_month']}-{r['period_year']}")
    lines.append(f"\nإجمالي المصروف: {total_paid:,.2f} د.ل")
    lines.append(f"إجمالي المحتجز المتراكم من العمليات: {total_retained:,.2f} د.ل")
    context.user_data["last_report"] = ("payroll", rows, "كل الموظفين", None)
    buttons = [[InlineKeyboardButton("📄 تصدير PDF", callback_data="export_report_pdf")]] if user_has_permission(session, "export_pdf") else None
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


async def company_accounts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_timeout(update, context):
        return LOGIN_USERNAME
    text = (
        "🏦 <b>حسابات شركة الحياة فارما</b>\n"
        "ℹ️ بمجرد النقر على رقم الحساب سيتم النسخ أوتوماتيك\n\n"
        "🏛️ <b>مصرف التجارة والتنمية</b> (يدعم شبكة الدفع الفوري والدولي LYpay)\n"
        "رقم الحساب المحلي (اضغط للنسخ):\n"
        "<code>0012793913001</code>\n"
        "رقم الآيبان الدولي (اضغط للنسخ):\n"
        "<code>LY33010012000012793913001</code>\n\n"
        "🏛️ <b>مصرف الوحدة</b>\n"
        "رقم الحساب (اضغط للنسخ):\n"
        "<code>601022300980014</code>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    await send_main_menu(update, context)
    return MAIN_MENU


def build_rep_status_text():
    reps = list_users_by_role("representative")
    assistants = list_users_by_role("assistant")
    lines = ["📶 حالة المندوبين والمساعدين:\n"]
    lines.append("👥 المندوبون:")
    if not reps:
        lines.append("لا يوجد مندوبون مسجّلون بعد.")
    for r in reps:
        status_tag = "" if r["active"] else " (⛔ موقوف)"
        lines.append(f"👤 {r['name']}{status_tag}\n{format_last_seen(r['last_seen'])}")
    lines.append("\n👨‍💼 المساعدون:")
    if not assistants:
        lines.append("لا يوجد مساعدون مسجّلون بعد.")
    for a in assistants:
        status_tag = "" if a["active"] else " (⛔ موقوف)"
        lines.append(f"👤 {a['name']}{status_tag}\n{format_last_seen(a['last_seen'])}")
    return "\n".join(lines)


async def rep_status_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_timeout(update, context):
        return LOGIN_USERNAME
    session = session_of(context)
    if session["role"] != "admin" and not user_has_permission(session, "view_rep_status"):
        await update.message.reply_text("⛔ ليست لديك صلاحية لهذا القسم.")
        return MAIN_MENU
    text = build_rep_status_text()
    await update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 تحديث", callback_data="rep_status_refresh")]])
    )
    return MAIN_MENU


async def rep_status_refresh_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("تم التحديث")
    session = session_of(context)
    if session["role"] != "admin" and not user_has_permission(session, "view_rep_status"):
        await query.edit_message_text("⛔ ليست لديك صلاحية لهذا القسم.")
        return
    text = build_rep_status_text()
    try:
        await query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 تحديث", callback_data="rep_status_refresh")]])
        )
    except Exception:
        pass  # لا تغيير في المحتوى منذ آخر تحديث، تجاهل خطأ "Message is not modified"


async def payments_overview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_timeout(update, context):
        return LOGIN_USERNAME
    session = session_of(context)
    if not user_has_permission(session, "view_payments"):
        await update.message.reply_text("⛔ ليست لديك صلاحية لهذا القسم.")
        return MAIN_MENU
    rows = get_all_payments()
    if not rows:
        await update.message.reply_text("لا توجد سدادات مسجلة بعد.", reply_markup=main_menu_kb(session))
        return MAIN_MENU
    total = get_total(rows)
    lines = ["💰 جميع عمليات التحصيل (آخر 30):\n"]
    for r in rows[:30]:
        lines.append(f"• {r['rep_name']} | {r['customer_name']} | {r['amount']:,.2f} د.ل | {r['payment_date']}")
    lines.append(f"\nإجمالي التحصيل: {total:,.2f} د.ل")
    context.user_data["last_all_payments"] = rows
    buttons = []
    if user_has_permission(session, "export_pdf"):
        buttons.append([InlineKeyboardButton("📄 تصدير PDF", callback_data="export_all_payments_pdf")])
    if user_has_permission(session, "edit_payments"):
        buttons.append([InlineKeyboardButton("✏️ تعديل / حذف سداد (آخر 10)", callback_data="payment_edit_list")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)
    await send_main_menu(update, context)
    return MAIN_MENU


async def payment_edit_list_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    if not user_has_permission(session, "edit_payments"):
        await query.edit_message_text("⛔ ليست لديك صلاحية تعديل السدادات.")
        return
    rows = get_all_payments()[:10]
    if not rows:
        await query.edit_message_text("لا توجد سدادات لتعديلها.")
        return
    buttons = [
        [InlineKeyboardButton(f"{r['customer_name']} | {r['amount']:,.2f} د.ل | {r['payment_date']}", callback_data=f"payment_view:{r['id']}")]
        for r in rows
    ]
    await query.edit_message_text("اختر عملية السداد:", reply_markup=InlineKeyboardMarkup(buttons))


async def payment_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    if not user_has_permission(session, "edit_payments"):
        await query.edit_message_text("⛔ ليست لديك صلاحية تعديل السدادات.")
        return
    payment_id = int(query.data.split(":")[1])
    p = get_payment(payment_id)
    if not p:
        await query.edit_message_text("هذه العملية لم تعد موجودة.")
        return
    text = (
        f"👤 العميل: {p['customer_name']}\n"
        f"👤 المندوب: {p['rep_name']}\n"
        f"💰 القيمة: {p['amount']:,.2f} د.ل\n"
        f"💳 الطريقة: {p['method']}\n"
        f"📅 التاريخ: {p['payment_date']}"
    )
    buttons = [
        [InlineKeyboardButton("✏️ تعديل اسم العميل", callback_data=f"payment_editname:{payment_id}")],
        [InlineKeyboardButton("✏️ تعديل القيمة", callback_data=f"payment_editamt:{payment_id}")],
        [InlineKeyboardButton("✏️ تعديل التاريخ", callback_data=f"payment_editdate:{payment_id}")],
        [InlineKeyboardButton("🗑️ حذف العملية", callback_data=f"payment_delete_confirm:{payment_id}")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def payment_editname_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    if not user_has_permission(session, "edit_payments"):
        await query.edit_message_text("⛔ ليست لديك صلاحية تعديل السدادات.")
        return
    payment_id = int(query.data.split(":")[1])
    context.user_data["edit_payment_name_id"] = payment_id
    await query.message.reply_text("أدخل اسم العميل الجديد:", reply_markup=CANCEL_KB)
    return PAYMENT_EDIT_NAME


async def payment_editname_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment_id = context.user_data.pop("edit_payment_name_id", None)
    if payment_id:
        update_payment_customer(payment_id, update.message.text.strip())
        await update.message.reply_text("✅ تم تحديث اسم العميل.")
    await send_main_menu(update, context)
    return MAIN_MENU


async def payment_editdate_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    if not user_has_permission(session, "edit_payments"):
        await query.edit_message_text("⛔ ليست لديك صلاحية تعديل السدادات.")
        return
    payment_id = int(query.data.split(":")[1])
    context.user_data["edit_payment_date_id"] = payment_id
    now = datetime.now()
    await query.message.reply_text(
        "📅 اختر التاريخ الجديد:",
        reply_markup=build_calendar_kb(now.year, now.month, prefix="paydate"),
    )


async def paydate_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split(":")  # paydate : nav/day : ...
    action = parts[1]
    if action == "nav":
        await query.answer()
        y, m = int(parts[2]), int(parts[3])
        await query.edit_message_reply_markup(reply_markup=build_calendar_kb(y, m, prefix="paydate"))
        return
    await query.answer()
    payment_id = context.user_data.pop("edit_payment_date_id", None)
    if action == "today":
        date_str = datetime.now().strftime("%Y-%m-%d")
    else:
        y, m, d = int(parts[2]), int(parts[3]), int(parts[4])
        date_str = f"{y:04d}-{m:02d}-{d:02d}"
    if payment_id:
        update_payment_date(payment_id, date_str)
        await query.edit_message_text(f"✅ تم تحديث تاريخ السداد إلى: {date_str}")
    else:
        await query.edit_message_text("انتهت صلاحية هذه العملية، أعد المحاولة.")


async def payment_editamt_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    if not user_has_permission(session, "edit_payments"):
        await query.edit_message_text("⛔ ليست لديك صلاحية تعديل السدادات.")
        return
    payment_id = int(query.data.split(":")[1])
    context.user_data["edit_payment_id"] = payment_id
    context.user_data["kp_target"] = "edit_payment"
    context.user_data["kp_value"] = ""
    await query.edit_message_text("أدخل القيمة الجديدة للسداد باستخدام لوحة الأرقام:\n\nالقيمة الحالية: 0")
    await query.message.reply_text("لوحة الأرقام:", reply_markup=build_keypad_kb(""))
    return TARGET_AMOUNT


async def payment_delete_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    if not user_has_permission(session, "edit_payments"):
        await query.edit_message_text("⛔ ليست لديك صلاحية تعديل السدادات.")
        return
    payment_id = int(query.data.split(":")[1])
    await query.edit_message_text(
        "⚠️ هل أنت متأكد من حذف عملية السداد هذه؟ لا يمكن التراجع بعد الحذف.",
        reply_markup=yesno_kb(f"payment_delete_do:{payment_id}", "noop", "✅ نعم، حذف", "❌ إلغاء"),
    )


async def payment_delete_do_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    session = session_of(context)
    if not user_has_permission(session, "edit_payments"):
        await query.answer("⛔ ليست لديك صلاحية.", show_alert=True)
        return
    payment_id = int(query.data.split(":")[1])
    delete_payment(payment_id)
    await query.answer("تم الحذف")
    await query.edit_message_text("🗑️ تم حذف عملية السداد بنجاح.")


async def export_all_payments_pdf_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("جاري إنشاء الملف...")
    rows = context.user_data.get("last_all_payments") or get_all_payments()
    if not await check_pdf_ready(query.message):
        return
    path = f"/tmp/all_payments_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"
    await safe_send_pdf(query.message, generate_general_report_pdf, path, "جميع الجباية.pdf", rows)


# ============================================================
# ADMIN / ASSISTANT: search customer (shared with rep flow via role check)
# ============================================================
# reuse search_customer_start / search_customer_do (rep_id=None branch covers admin/assistant)


# ============================================================
# ADMIN / ASSISTANT: reports menu (📊 التقارير)
# ============================================================

async def reports_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_timeout(update, context):
        return LOGIN_USERNAME
    session = session_of(context)
    if not user_has_permission(session, "view_reports"):
        await update.message.reply_text("⛔ ليست لديك صلاحية لهذا القسم.")
        return MAIN_MENU
    buttons = [
        [InlineKeyboardButton("📅 تقرير اليوم", callback_data="report:today")],
        [InlineKeyboardButton("📆 تقرير الشهر", callback_data="report:month")],
        [InlineKeyboardButton("👨‍💼 تقرير مندوب", callback_data="report:rep")],
        [InlineKeyboardButton("🏦 تقرير حسب طريقة السداد", callback_data="report:method")],
        [InlineKeyboardButton("📋 التقرير العام", callback_data="report:general")],
    ]
    await update.message.reply_text("📊 التقارير:", reply_markup=InlineKeyboardMarkup(buttons))
    return MAIN_MENU


def month_number_kb(prefix, year):
    """لوحة اختيار الشهر بالأرقام فقط (1-12) مع تنقل بين السنوات."""
    prev_y, next_y = year - 1, year + 1
    rows = [[
        InlineKeyboardButton("◀️", callback_data=f"{prefix}:nav:{prev_y}"),
        InlineKeyboardButton(str(year), callback_data="noop"),
        InlineKeyboardButton("▶️", callback_data=f"{prefix}:nav:{next_y}"),
    ]]
    grid = [InlineKeyboardButton(str(m), callback_data=f"{prefix}:pick:{year}:{m}") for m in range(1, 13)]
    for i in range(0, 12, 4):
        rows.append(grid[i:i + 4])
    return InlineKeyboardMarkup(rows)


async def report_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    kind = query.data.split(":")[1]

    if kind == "rep":
        reps = list_users_by_role("representative")
        buttons = [[InlineKeyboardButton(r["name"], callback_data=f"reportrep:{r['id']}")] for r in reps]
        await query.edit_message_text("اختر المندوب:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if kind == "month":
        now = datetime.now()
        await query.edit_message_text("📆 اختر الشهر (بالأرقام):", reply_markup=month_number_kb("genmonth", now.year))
        return

    if kind == "method":
        buttons = [[InlineKeyboardButton(m, callback_data=f"methodreport:{i}")] for i, m in enumerate(PAYMENT_METHODS)]
        buttons.append([InlineKeyboardButton("📋 كل الطرق (ملخص إجمالي)", callback_data="methodreport:all")])
        await query.edit_message_text("🏦 اختر طريقة السداد:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    can_export = user_has_permission(session, "export_pdf")

    if kind == "today":
        rows = get_payments_today()
        total = get_total(rows)
        text = f"📅 تقرير اليوم ({datetime.now().strftime('%Y-%m-%d')})\n\nعدد العمليات: {len(rows)}\nالإجمالي: {total:,.2f} د.ل"
        context.user_data["last_report"] = ("general", rows, "تقرير اليوم")
    elif kind == "general":
        rows = get_all_payments()
        total = get_total(rows)
        text = f"📋 التقرير العام\n\nعدد العمليات: {len(rows)}\nالإجمالي: {total:,.2f} د.ل"
        context.user_data["last_report"] = ("general", rows, "التقرير العام")
    else:
        return

    buttons = [[InlineKeyboardButton("📄 تصدير PDF", callback_data="export_report_pdf")]] if can_export else None
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


async def genmonth_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    parts = query.data.split(":")
    if parts[1] == "nav":
        year = int(parts[2])
        await query.edit_message_reply_markup(reply_markup=month_number_kb("genmonth", year))
        return
    year, month = int(parts[2]), int(parts[3])
    rows = get_all_payments(month, year)
    total = get_total(rows)
    label = f"شهر {month}-{year}"
    text = f"📆 تقرير {label}\n\nعدد العمليات: {len(rows)}\nالإجمالي: {total:,.2f} د.ل"
    context.user_data["last_report"] = ("general", rows, label)
    can_export = user_has_permission(session, "export_pdf")
    buttons = [[InlineKeyboardButton("📄 تصدير PDF", callback_data="export_report_pdf")]] if can_export else None
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


async def methodreport_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    can_export = user_has_permission(session, "export_pdf")
    choice = query.data.split(":")[1]
    if choice == "all":
        totals = totals_by_method()
        grand = sum(totals.values())
        lines = [f"{k}: {v:,.2f} د.ل" for k, v in totals.items()]
        text = "🏦 تقرير حسب طريقة السداد (ملخص كل الطرق)\n\n" + "\n".join(lines) + f"\n\nالإجمالي: {grand:,.2f} د.ل"
        context.user_data["last_report"] = ("method", totals, "")
    else:
        method_name = PAYMENT_METHODS[int(choice)]
        rows = [r for r in get_all_payments() if r["method"] == method_name]
        total = get_total(rows)
        text = f"🏦 طريقة السداد: {method_name}\n\nعدد العمليات: {len(rows)}\nالإجمالي: {total:,.2f} د.ل"
        context.user_data["last_report"] = ("general", rows, method_name)
    buttons = [[InlineKeyboardButton("📄 تصدير PDF", callback_data="export_report_pdf")]] if can_export else None
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


async def report_rep_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    rep_id = int(query.data.split(":")[1])
    rep = get_user(rep_id)
    rows = get_payments_by_rep(rep_id)
    total = get_total(rows)
    target, collected, remaining, pct = current_month_target_progress(rep_id)
    text = (
        f"👨‍💼 تقرير المندوب: {rep['name']}\n\n"
        f"عدد العمليات (كل الفترات): {len(rows)}\nإجمالي التحصيل (كل الفترات): {total:,.2f} د.ل\n\n"
        f"{target_progress_text(target, collected, remaining, pct)}"
    )
    context.user_data["last_report"] = ("rep", rows, rep["name"], (target, collected, remaining, pct))
    can_export = user_has_permission(session, "export_pdf")
    buttons = []
    if can_export:
        buttons.append([InlineKeyboardButton("📄 تصدير PDF (كل الفترات)", callback_data="export_report_pdf")])
    buttons.append([InlineKeyboardButton("📅 سدادات شهر معيّن", callback_data=f"repmonth:{rep_id}:nav:{datetime.now().year}")])
    if user_has_permission(session, "edit_payments"):
        buttons.append([InlineKeyboardButton("✏️ عرض/تعديل سداداته (آخر 15)", callback_data=f"replistedit:{rep_id}")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def replistedit_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    if not user_has_permission(session, "edit_payments"):
        await query.edit_message_text("⛔ ليست لديك صلاحية تعديل السدادات.")
        return
    rep_id = int(query.data.split(":")[1])
    rep = get_user(rep_id)
    rows = get_payments_by_rep(rep_id)[:15]
    if not rows:
        await query.edit_message_text("لا توجد سدادات لهذا المندوب.")
        return
    buttons = [
        [InlineKeyboardButton(f"{r['customer_name']} | {r['amount']:,.2f} د.ل | {r['payment_date']}", callback_data=f"payment_view:{r['id']}")]
        for r in rows
    ]
    await query.edit_message_text(f"سدادات {rep['name']} — اختر عملية للتعديل:", reply_markup=InlineKeyboardMarkup(buttons))


async def repmonth_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    parts = query.data.split(":")
    rep_id = int(parts[1])
    rep = get_user(rep_id)
    if parts[2] == "nav":
        year = int(parts[3])
        await query.edit_message_text(
            f"📅 اختر الشهر (بالأرقام) لعرض سدادات {rep['name']}:",
            reply_markup=month_number_kb(f"repmonthpick:{rep_id}", year),
        )
        return


async def repmonthpick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    parts = query.data.split(":")  # repmonthpick : rep_id : action : year [: month]
    rep_id = int(parts[1])
    action = parts[2]
    rep = get_user(rep_id)
    if action == "nav":
        year = int(parts[3])
        await query.edit_message_reply_markup(reply_markup=month_number_kb(f"repmonthpick:{rep_id}", year))
        return
    year, month = int(parts[3]), int(parts[4])
    rows = get_payments_by_rep(rep_id, month, year)
    total = get_total(rows)
    label = f"{rep['name']} - شهر {month}-{year}"
    target = get_target(rep_id, month, year)
    remaining = max(target - total, 0)
    pct = (total / target * 100) if target else 0
    text = (
        f"👨‍💼 سدادات {rep['name']} - شهر {month}-{year}\n\n"
        f"عدد العمليات: {len(rows)}\nالإجمالي: {total:,.2f} د.ل\n\n"
        f"{target_progress_text(target, total, remaining, pct)}"
    )
    context.user_data["last_report"] = ("rep", rows, label, (target, total, remaining, pct))
    can_export = user_has_permission(session, "export_pdf")
    buttons = [[InlineKeyboardButton("📄 تصدير PDF", callback_data="export_report_pdf")]] if can_export else None
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


async def export_report_pdf_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("جاري إنشاء الملف...")
    report = context.user_data.get("last_report")
    if not report:
        await query.message.reply_text("انتهت صلاحية هذا التقرير، أعد استخراجه من فضلك.")
        return
    if not await check_pdf_ready(query.message):
        return
    kind, data, label = report[0], report[1], report[2]
    target_info = report[3] if len(report) > 3 else None
    path = f"/tmp/report_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"
    if kind == "general":
        await safe_send_pdf(query.message, generate_general_report_pdf, path, f"{label or 'تقرير'}.pdf", data, period_label=label, target_info=target_info)
    elif kind == "rep":
        await safe_send_pdf(query.message, generate_rep_report_pdf, path, f"تقرير - {label}.pdf", label, data, target_info=target_info)
    elif kind == "method":
        await safe_send_pdf(query.message, generate_method_report_pdf, path, "تقرير طرق السداد.pdf", data)
    elif kind == "expenses":
        await safe_send_pdf(query.message, generate_expenses_pdf, path, f"تقرير مصاريف - {label}.pdf", data, period_label=label)
    elif kind == "payroll":
        await safe_send_pdf(query.message, generate_payroll_pdf, path, f"تقرير رواتب - {label}.pdf", data, period_label=label)

# ============================================================
# ADMIN / ASSISTANT: send message (📩 إرسال رسالة)
# ============================================================

async def msg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_timeout(update, context):
        return LOGIN_USERNAME
    session = session_of(context)
    if not user_has_permission(session, "send_messages"):
        await update.message.reply_text("⛔ ليست لديك صلاحية إرسال رسائل.")
        return MAIN_MENU
    buttons = [
        [InlineKeyboardButton("📢 إرسال لجميع المندوبين", callback_data="msgto:all")],
        [InlineKeyboardButton("👤 إرسال لمندوب محدد", callback_data="msgto:single")],
    ]
    await update.message.reply_text("📩 إرسال رسالة:", reply_markup=InlineKeyboardMarkup(buttons))
    return MAIN_MENU


CANNED_MESSAGES = {
    "weak": "⚠️ التحصيل ضعيف، نرجو منك بذل مزيد من الجهد لتحقيق الهدف الشهري.",
}


def msg_type_kb():
    buttons = [
        [InlineKeyboardButton("✍️ كتابة رسالة", callback_data="msgtype:custom")],
        [InlineKeyboardButton("⚠️ رسالة جاهزة: التحصيل ضعيف", callback_data="msgtype:weak")],
    ]
    return InlineKeyboardMarkup(buttons)


async def msg_to_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kind = query.data.split(":")[1]
    if kind == "all":
        context.user_data["msg_target"] = {"type": "all"}
        await query.edit_message_text("📢 إرسال لجميع المندوبين\n\nاختر نوع الرسالة:", reply_markup=msg_type_kb())
        return MSG_CHOOSE_TYPE
    else:
        reps = list_users_by_role("representative", active_only=True)
        buttons = [[InlineKeyboardButton(r["name"], callback_data=f"msgrep:{r['id']}")] for r in reps]
        await query.edit_message_text("اختر المندوب:", reply_markup=InlineKeyboardMarkup(buttons))
        return MSG_PICK_TARGET


async def msg_pick_rep_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rep_id = int(query.data.split(":")[1])
    rep = get_user(rep_id)
    context.user_data["msg_target"] = {"type": "single", "id": rep_id, "name": rep["name"]}
    await query.edit_message_text(f"المرسل إليه: {rep['name']}\n\nاختر نوع الرسالة:", reply_markup=msg_type_kb())
    return MSG_CHOOSE_TYPE


async def msg_type_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kind = query.data.split(":")[1]
    target = context.user_data.get("msg_target", {})
    label = "جميع المندوبين" if target.get("type") == "all" else target.get("name", "")
    if kind == "custom":
        await query.edit_message_text(f"المرسل إليه: {label}")
        await query.message.reply_text("اكتب نص الرسالة، أو أرسل صورة مع تعليق (Caption):", reply_markup=CANCEL_KB)
        return MSG_BODY
    body = CANNED_MESSAGES.get(kind)
    if not body:
        return MAIN_MENU
    context.user_data["msg_body"] = body
    await query.edit_message_text(
        f"هل تريد إرسال هذه الرسالة إلى {label}؟\n\n«{body}»",
        reply_markup=yesno_kb("msg_send_confirm", "msg_send_cancel", "✅ نعم، إرسال", "❌ إلغاء"),
    )
    return MAIN_MENU


async def msg_body_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        body = (update.message.caption or "").strip()
        context.user_data["msg_photo"] = update.message.photo[-1].file_id
    else:
        body = update.message.text.strip()
        context.user_data.pop("msg_photo", None)
    context.user_data["msg_body"] = body
    target = context.user_data.get("msg_target", {})
    label = "جميع المندوبين" if target.get("type") == "all" else target.get("name", "")
    photo_note = "📷 (مع صورة)\n" if context.user_data.get("msg_photo") else ""
    await update.message.reply_text(
        f"هل تريد إرسال هذه الرسالة إلى {label}؟\n{photo_note}\n«{body}»",
        reply_markup=yesno_kb("msg_send_confirm", "msg_send_cancel", "✅ نعم، إرسال", "❌ إلغاء"),
    )
    return MAIN_MENU


async def msg_send_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    target = context.user_data.pop("msg_target", {})
    body = context.user_data.pop("msg_body", "")
    photo = context.user_data.pop("msg_photo", None)
    text = f"📩 رسالة من الإدارة\n\n{body}" if body else "📩 رسالة من الإدارة"
    delivered, failed, no_account = [], [], []

    async def _send(telegram_id):
        if photo:
            await context.bot.send_photo(telegram_id, photo=photo, caption=text)
        else:
            await context.bot.send_message(telegram_id, text)

    if target.get("type") == "all":
        reps = list_users_by_role("representative", active_only=True)
        for r in reps:
            if not r["telegram_id"]:
                no_account.append(r["name"])
                continue
            try:
                await _send(r["telegram_id"])
                delivered.append(r["name"])
            except Exception:
                failed.append(r["name"])
        log_message(session["id"], None, "all", body)
    else:
        rep = get_user(target.get("id"))
        if rep:
            if not rep["telegram_id"]:
                no_account.append(rep["name"])
            else:
                try:
                    await _send(rep["telegram_id"])
                    delivered.append(rep["name"])
                except Exception:
                    failed.append(rep["name"])
        log_message(session["id"], target.get("id"), "single", body)

    lines = [f"✅ تم تسليم الرسالة إلى تيليجرام لـ {len(delivered)} حساب."]
    if delivered:
        lines.append("📬 وصلت إلى: " + "، ".join(delivered))
    if failed:
        lines.append("⚠️ فشل الإرسال لـ (على الأغلب أوقف المستخدم البوت): " + "، ".join(failed))
    if no_account:
        lines.append("🚫 لم يسجّلوا الدخول عبر البوت بعد فلا يوجد حساب تيليجرام مرتبط: " + "، ".join(no_account))
    lines.append(
        "\nℹ️ ملاحظة: تيليجرام لا يسمح لأي بوت بمعرفة هل قرأ المستخدم الرسالة فعلاً أم لا "
        "(لا توجد علامة \"تمت القراءة\" للبوتات) — \"تم التسليم\" هنا يعني وصلت لحساب تيليجرام الخاص به بنجاح."
    )
    await query.edit_message_text("\n".join(lines))
    await send_main_menu(query, context)


async def msg_send_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("msg_target", None)
    context.user_data.pop("msg_body", None)
    await query.edit_message_text("تم الإلغاء.")
    await send_main_menu(query, context)


# ============================================================
# ALL USERS: feedback / development idea (📢 إبلاغ/فكرة تطوير)
# ============================================================

async def feedback_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_timeout(update, context):
        return LOGIN_USERNAME
    await update.message.reply_text(
        "📢 اكتب رسالتك (بلاغ عن مشكلة أو فكرة تطوير) وستصل مباشرة إلى الإدارة:",
        reply_markup=CANCEL_KB,
    )
    return FEEDBACK_BODY


async def feedback_body_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = session_of(context)
    body = update.message.text.strip()
    fid = add_feedback(session["id"], session["name"], session["role"], body)
    role_label = {"admin": "👑 المدير", "assistant": "👨‍💼 المساعد", "representative": "👤 المندوب"}[session["role"]]
    notify_text = f"📢 بلاغ / فكرة تطوير جديدة\n\nمن: {session['name']} ({role_label})\n\n{body}"
    reply_kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ رد على هذا المستخدم", callback_data=f"feedback_reply:{fid}")]])
    recipients = [a for a in list_users_by_role("admin") if a["id"] != session["id"]]
    for assistant in list_users_by_role("assistant", active_only=True):
        if assistant["id"] == session["id"]:
            continue
        if get_permissions(assistant["id"]).get("receive_feedback"):
            recipients.append(assistant)
    for user in recipients:
        if user["telegram_id"]:
            try:
                await context.bot.send_message(user["telegram_id"], notify_text, reply_markup=reply_kb)
            except Exception:
                pass
    await update.message.reply_text("✅ تم إرسال رسالتك للإدارة، شكراً لك.")
    await send_main_menu(update, context)
    return MAIN_MENU


async def feedback_reply_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    allowed = session and (session["role"] == "admin" or user_has_permission(session, "receive_feedback"))
    if not allowed:
        await query.answer("⛔ ليست لديك صلاحية الرد على البلاغات.", show_alert=True)
        return
    fid = int(query.data.split(":")[1])
    fb = get_feedback(fid)
    if not fb:
        await query.edit_message_text("هذا البلاغ لم يعد موجوداً.")
        return
    context.user_data["feedback_reply_id"] = fid
    context.user_data["feedback_reply_to"] = fb["sender_id"]
    await query.message.reply_text(f"اكتب ردك على {fb['sender_name']}:", reply_markup=CANCEL_KB)
    return FEEDBACK_REPLY_BODY


async def feedback_reply_body_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = session_of(context)
    fid = context.user_data.pop("feedback_reply_id", None)
    recipient_id = context.user_data.pop("feedback_reply_to", None)
    body = update.message.text.strip()
    if recipient_id:
        recipient = get_user(recipient_id)
        if recipient and recipient["telegram_id"]:
            try:
                await context.bot.send_message(recipient["telegram_id"], f"📩 رد من الإدارة على بلاغك:\n\n{body}")
            except Exception:
                pass
        if fid:
            save_feedback_reply(fid, body, session["id"], session["name"])
        await update.message.reply_text("✅ تم إرسال الرد، وتم حفظ المحادثة كاملة بشكل دائم.")
    await send_main_menu(update, context)
    return MAIN_MENU


async def noop_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("تم الإلغاء.")


# ============================================================
# MAIN MENU ROUTER (text button dispatch while in MAIN_MENU state)
# ============================================================

async def main_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_timeout(update, context):
        return LOGIN_USERNAME
    session = session_of(context)
    if not session:
        return await start(update, context)
    text = update.message.text.strip()

    if text == "🔙 رجوع للقائمة الرئيسية":
        await send_main_menu(update, context)
        return MAIN_MENU
    if text == "🚪 خروج":
        return await logout(update, context)
    if text == "❌ إلغاء الأمر":
        context.user_data.pop("collect_on_behalf_of", None)
        context.user_data.pop("collect", None)
        return await cancel_to_menu(update, context)

    # representative
    if text == "💰 التحصيل" and (
        session["role"] == "representative"
        or session["role"] == "admin"
        or user_has_permission(session, "collect_payments")
    ):
        return await collect_start(update, context)
    if text == "🔍 البحث عن عميل":
        return await search_customer_start(update, context)
    if text == "📊 تقرير السدادات" and session["role"] == "representative":
        return await rep_report(update, context)

    # admin / assistant
    if text == "👥 المندوبين":
        return await reps_menu(update, context)
    if text == "➕ إضافة مندوب":
        return await add_rep_start(update, context)
    if text == "👨‍💼 المساعدين" and session["role"] == "admin":
        return await assistants_menu(update, context)
    if text == "➕ إضافة مساعد" and session["role"] == "admin":
        return await add_assist_start(update, context)
    if text == "🎯 أهداف التحصيل":
        return await targets_menu(update, context)
    if text == "💰 الجباية":
        return await payments_overview(update, context)
    if text == "📊 التقارير":
        return await reports_menu(update, context)
    if text == "📩 إرسال رسالة":
        return await msg_start(update, context)
    if text == "📢 إبلاغ/فكرة تطوير":
        return await feedback_start(update, context)
    if text == "📶 حالة المندوبين":
        return await rep_status_menu(update, context)
    if text == "🏠 Home Use target":
        return await home_use_target_menu(update, context)
    if text == "🩺 Professional Use target":
        return await professional_use_target_menu(update, context)
    if text == "🏦 حسابات شركة الحياة فارما":
        return await company_accounts_menu(update, context)
    if text == "💵 مصاريف":
        return await expenses_menu(update, context)
    if text == "💼 الرواتب":
        return await payroll_menu(update, context)

    await update.message.reply_text("الرجاء استخدام الأزرار في القائمة.", reply_markup=main_menu_kb(session))
    return MAIN_MENU


async def unknown_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()


# ============================================================
# APPLICATION SETUP
# ============================================================

async def _post_init(application: Application):
    """يسجّل أمر /start في قائمة أوامر تيليجرام (زر الشرطة /)، بحيث يبقى متاحاً دائماً
    بضغطة واحدة حتى لو ظهرت لوحة أزرار قديمة أو توقف البوت عن الاستجابة مؤقتاً."""
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "🔄 بدء / تسجيل الدخول"),
        ])
    except Exception:
        pass


async def send_weekly_reports_job(context: ContextTypes.DEFAULT_TYPE):
    """يُرسل تقريراً أسبوعياً بصيغة PDF (Home Use و Professional Use) لكل حسابات
    المدير والمساعدين، كل يوم جمعة الساعة 3 عصراً بتوقيت ليبيا."""
    start_date, end_date = get_week_range()
    recipients = list(list_users_by_role("admin")) + list(list_users_by_role("assistant", active_only=True))
    for category in ("home", "professional"):
        stats, overall_total = build_weekly_report_data(category, start_date, end_date)
        path = f"/tmp/weekly_{category}_{end_date}.pdf"
        try:
            generate_weekly_report_pdf(category, start_date, end_date, stats, overall_total, path)
        except Exception:
            logger.exception("فشل إنشاء التقرير الأسبوعي لتصنيف %s", category)
            continue
        caption = f"📆 التقرير الأسبوعي — {CATEGORY_LABELS[category]}\nمن {start_date} إلى {end_date}"
        for user in recipients:
            if user["telegram_id"]:
                try:
                    with open(path, "rb") as f:
                        await context.bot.send_document(user["telegram_id"], document=f, filename=os.path.basename(path), caption=caption)
                except Exception:
                    pass
        try:
            os.remove(path)
        except Exception:
            pass


def build_app():
    init_db()
    # حفظ حالة الجلسات (تسجيل الدخول وما إلى ذلك) على نفس القرص الدائم لقاعدة البيانات،
    # حتى لا يحتاج أي مستخدم لتسجيل الدخول من جديد أو مسح المحادثة بعد كل تحديث للبوت.
    persistence_dir = os.path.dirname(DB_PATH) or "."
    persistence_path = os.path.join(persistence_dir, "bot_persistence.pkl")
    persistence = PicklePersistence(filepath=persistence_path)
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .persistence(persistence)
        .post_init(_post_init)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ADMIN_SETUP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_setup_name)],
            ADMIN_SETUP_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_setup_username)],
            ADMIN_SETUP_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_setup_password)],
            LOGIN_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_username)],
            LOGIN_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)],

            MAIN_MENU: [
                CallbackQueryHandler(collectas_cb, pattern="^collectas:"),
                CallbackQueryHandler(collectrep_cb, pattern="^collectrep:"),
                CallbackQueryHandler(collectassistant_cb, pattern="^collectassistant:"),
                CallbackQueryHandler(collect_method_cb, pattern=f"^{CB_METHOD}"),
                CallbackQueryHandler(export_customer_pdf_cb, pattern="^export_customer_pdf$"),
                CallbackQueryHandler(transfer_customer_start_cb, pattern="^transfer_customer_start$"),
                CallbackQueryHandler(transfer_customer_to_cb, pattern="^transfer_customer_to:"),
                CallbackQueryHandler(export_rep_report_pdf_cb, pattern="^export_rep_report_pdf$"),
                CallbackQueryHandler(rep_view_cb, pattern="^rep_view:"),
                CallbackQueryHandler(rep_toggle_cb, pattern="^rep_toggle:"),
                CallbackQueryHandler(rep_setcat_cb, pattern="^rep_setcat:"),
                CallbackQueryHandler(rep_delete_confirm_cb, pattern="^rep_delete_confirm:"),
                CallbackQueryHandler(rep_delete_do_cb, pattern="^rep_delete_do:"),
                CallbackQueryHandler(rep_editname_cb, pattern="^rep_editname:"),
                CallbackQueryHandler(rep_editpass_cb, pattern="^rep_editpass:"),
                CallbackQueryHandler(admin_self_view_cb, pattern="^admin_self_view$"),
                CallbackQueryHandler(admin_self_editname_cb, pattern="^admin_self_editname$"),
                CallbackQueryHandler(admin_self_editpass_cb, pattern="^admin_self_editpass$"),
                CallbackQueryHandler(assist_view_cb, pattern="^assist_view:"),
                CallbackQueryHandler(assist_toggle_cb, pattern="^assist_toggle:"),
                CallbackQueryHandler(assist_delete_confirm_cb, pattern="^assist_delete_confirm:"),
                CallbackQueryHandler(assist_delete_do_cb, pattern="^assist_delete_do:"),
                CallbackQueryHandler(assist_editpass_cb, pattern="^assist_editpass:"),
                CallbackQueryHandler(assist_perms_cb, pattern="^assist_perms:"),
                CallbackQueryHandler(perm_toggle_cb, pattern="^permtoggle:"),
                CallbackQueryHandler(target_pick_cb, pattern="^target_pick:"),
                CallbackQueryHandler(target_set_cb, pattern="^target_set:"),
                CallbackQueryHandler(target_delete_confirm_cb, pattern="^target_delete_confirm:"),
                CallbackQueryHandler(target_delete_do_cb, pattern="^target_delete_do:"),
                CallbackQueryHandler(export_all_payments_pdf_cb, pattern="^export_all_payments_pdf$"),
                CallbackQueryHandler(payment_edit_list_cb, pattern="^payment_edit_list$"),
                CallbackQueryHandler(payment_view_cb, pattern="^payment_view:"),
                CallbackQueryHandler(payment_editamt_cb, pattern="^payment_editamt:"),
                CallbackQueryHandler(payment_editname_cb, pattern="^payment_editname:"),
                CallbackQueryHandler(payment_editdate_cb, pattern="^payment_editdate:"),
                CallbackQueryHandler(paydate_cb, pattern="^paydate:"),
                CallbackQueryHandler(payment_delete_confirm_cb, pattern="^payment_delete_confirm:"),
                CallbackQueryHandler(payment_delete_do_cb, pattern="^payment_delete_do:"),
                CallbackQueryHandler(report_cb, pattern="^report:"),
                CallbackQueryHandler(genmonth_cb, pattern="^genmonth:"),
                CallbackQueryHandler(methodreport_cb, pattern="^methodreport:"),
                CallbackQueryHandler(report_rep_pick_cb, pattern="^reportrep:"),
                CallbackQueryHandler(replistedit_cb, pattern="^replistedit:"),
                CallbackQueryHandler(repmonth_cb, pattern="^repmonth:"),
                CallbackQueryHandler(repmonthpick_cb, pattern="^repmonthpick:"),
                CallbackQueryHandler(export_report_pdf_cb, pattern="^export_report_pdf$"),
                CallbackQueryHandler(msg_to_cb, pattern="^msgto:"),
                CallbackQueryHandler(msg_pick_rep_cb, pattern="^msgrep:"),
                CallbackQueryHandler(msg_type_cb, pattern="^msgtype:"),
                CallbackQueryHandler(msg_send_confirm_cb, pattern="^msg_send_confirm$"),
                CallbackQueryHandler(msg_send_cancel_cb, pattern="^msg_send_cancel$"),
                CallbackQueryHandler(feedback_reply_cb, pattern="^feedback_reply:"),
                CallbackQueryHandler(rep_status_refresh_cb, pattern="^rep_status_refresh$"),
                CallbackQueryHandler(cattarget_set_cb, pattern="^cattarget_set:"),
                CallbackQueryHandler(cattarget_delete_cb, pattern="^cattarget_delete:"),
                CallbackQueryHandler(cattarget_delete_do_cb, pattern="^cattarget_delete_do:"),
                CallbackQueryHandler(expense_add_start_cb, pattern="^expense_add_start$"),
                CallbackQueryHandler(expense_report_menu_cb, pattern="^expense_report_menu$"),
                CallbackQueryHandler(expreport_cb, pattern="^expreport:"),
                CallbackQueryHandler(expreportpick_cb, pattern="^expreportpick:"),
                CallbackQueryHandler(expmonth_cb, pattern="^expmonth:"),
                CallbackQueryHandler(payroll_add_start_cb, pattern="^payroll_add_start$"),
                CallbackQueryHandler(payroll_class_cb, pattern="^payroll_class:"),
                CallbackQueryHandler(payroll_type_cb, pattern="^payroll_type:"),
                CallbackQueryHandler(payroll_link_rep_cb, pattern="^payroll_link_rep:"),
                CallbackQueryHandler(payroll_list_cb, pattern="^payroll_list$"),
                CallbackQueryHandler(payroll_view_cb, pattern="^payroll_view:"),
                CallbackQueryHandler(payroll_editamt_cb, pattern="^payroll_editamt:"),
                CallbackQueryHandler(payroll_pay_start_cb, pattern="^payroll_pay_start:"),
                CallbackQueryHandler(payroll_pay_confirm_cb, pattern="^(payroll_pay_confirm|payroll_pay_cancel)$"),
                CallbackQueryHandler(payroll_release_cb, pattern="^payroll_release:"),
                CallbackQueryHandler(payroll_release_do_cb, pattern="^payroll_release_do:"),
                CallbackQueryHandler(payroll_delete_confirm_cb, pattern="^payroll_delete_confirm:"),
                CallbackQueryHandler(payroll_delete_do_cb, pattern="^payroll_delete_do:"),
                CallbackQueryHandler(payroll_report_cb, pattern="^payroll_report$"),
                CallbackQueryHandler(catmonth_cb, pattern="^catmonth:"),
                CallbackQueryHandler(noop_cb, pattern="^noop$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_router),
            ],

            COLLECT_CUSTOMER: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, collect_customer),
            ],
            COLLECT_AMOUNT: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                CallbackQueryHandler(keypad_cb, pattern="^kp:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, collect_amount),
            ],
            COLLECT_METHOD: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                CallbackQueryHandler(collect_method_cb, pattern=f"^{CB_METHOD}"),
            ],
            COLLECT_DATE: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                CallbackQueryHandler(save_payment_cb, pattern="^(save_payment|cancel_payment)$"),
                CallbackQueryHandler(calendar_cb, pattern="^cal:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, collect_date),
            ],

            SEARCH_CUSTOMER: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, search_customer_do),
            ],
            ADMIN_SEARCH_CUSTOMER: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, search_customer_do),
            ],

            ADD_REP_NAME: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_rep_name),
            ],
            ADD_REP_USERNAME: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_rep_username),
            ],
            ADD_REP_PASSWORD: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_rep_password),
            ],

            EDIT_REP_NAME: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_rep_name_do),
            ],
            EDIT_REP_PASSWORD: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_rep_pass_do),
            ],

            ADD_ASSIST_NAME: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_assist_name),
            ],
            ADD_ASSIST_USERNAME: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_assist_username),
            ],
            ADD_ASSIST_PASSWORD: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_assist_password),
            ],
            EDIT_ASSIST_PASSWORD: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_assist_pass_do),
            ],

            TARGET_PICK_REP: [CallbackQueryHandler(target_pick_cb, pattern="^target_pick:")],
            TARGET_AMOUNT: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                CallbackQueryHandler(keypad_cb, pattern="^kp:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, target_amount_do),
            ],

            MSG_PICK_TARGET: [CallbackQueryHandler(msg_pick_rep_cb, pattern="^msgrep:")],
            MSG_CHOOSE_TYPE: [CallbackQueryHandler(msg_type_cb, pattern="^msgtype:")],
            MSG_BODY: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.PHOTO, msg_body_do),
            ],
            FEEDBACK_BODY: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_body_do),
            ],
            FEEDBACK_REPLY_BODY: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_reply_body_do),
            ],
            PAYMENT_EDIT_NAME: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, payment_editname_do),
            ],
            EXPENSE_DESC: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, expense_desc_do),
            ],
            EXPENSE_FLOW: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                CallbackQueryHandler(expdate_cb, pattern="^expdate:"),
                CallbackQueryHandler(expattr_cb, pattern="^expattr:"),
                CallbackQueryHandler(expattrpick_cb, pattern="^expattrpick:"),
                CallbackQueryHandler(expense_save_cb, pattern="^(expense_save|expense_cancel)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, expense_date_text_do),
            ],
            PAYROLL_EMP_NAME: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, payroll_emp_name_do),
            ],
            PAYROLL_EMP_AMOUNT: [
                MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
                CallbackQueryHandler(payroll_class_cb, pattern="^payroll_class:"),
                CallbackQueryHandler(payroll_type_cb, pattern="^payroll_type:"),
                CallbackQueryHandler(payroll_link_rep_cb, pattern="^payroll_link_rep:"),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
            MessageHandler(filters.Regex("^🚪 خروج$"), logout),
            CallbackQueryHandler(feedback_reply_cb, pattern="^feedback_reply:"),
            CallbackQueryHandler(unknown_cb),
        ],
        allow_reentry=True,
        persistent=True,
        name="alhaya_conversation",
    )

    app.add_handler(conv)

    # جدولة التقرير الأسبوعي: كل يوم جمعة الساعة 3:00 عصراً بتوقيت ليبيا (طرابلس)
    try:
        from zoneinfo import ZoneInfo
        tripoli_tz = ZoneInfo("Africa/Tripoli")
        weekly_time = dt_time(hour=15, minute=0, tzinfo=tripoli_tz)
        app.job_queue.run_daily(send_weekly_reports_job, time=weekly_time, days=(4,), name="weekly_report_friday")
    except Exception:
        logger.exception("تعذّرت جدولة التقرير الأسبوعي — تأكد من توفر حزمة tzdata")

    return app


if __name__ == "__main__":
    application = build_app()
    logger.info("Alhaya Pharma collection bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
