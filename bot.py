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
from datetime import datetime, timedelta

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
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
FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "NotoNaskhArabic-Regular.ttf")
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
    "view_payments": "💰 مشاهدة التحصيلات",
    "search_customers": "🔍 البحث عن العملاء",
    "view_reports": "📊 مشاهدة التقارير",
    "export_pdf": "📄 تصدير PDF",
    "send_messages": "📩 إرسال رسائل",
    "add_representatives": "➕ إضافة مندوبين",
    "edit_representatives": "✏️ تعديل المندوبين",
    "delete_representatives": "🗑️ حذف المندوبين",
    "manage_targets": "🎯 تحديد الأهداف",
}
DEFAULT_ON_PERMS = {"view_representatives", "view_payments", "search_customers", "view_reports", "export_pdf", "send_messages"}

# ============================================================
# DATABASE LAYER
# ============================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
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
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER,
            recipient_id INTEGER,
            recipient_type TEXT NOT NULL,
            body TEXT NOT NULL,
            sent_at TEXT DEFAULT (datetime('now'))
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


def log_message(sender_id, recipient_id, recipient_type, body):
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (sender_id, recipient_id, recipient_type, body) VALUES (?,?,?,?)",
        (sender_id, recipient_id, recipient_type, body),
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


def generate_rep_report_pdf(rep_name, rows, out_path, period_label=""):
    pdf = ArabicPDF(subtitle=f"تقرير سدادات المندوب — {rep_name}")
    if period_label:
        pdf.info_line("الفترة", period_label)
    pdf.info_line("تاريخ الاستخراج", datetime.now().strftime("%Y-%m-%d"))
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


def generate_general_report_pdf(rows, out_path, period_label=""):
    pdf = ArabicPDF(subtitle="التقرير العام للتحصيل")
    if period_label:
        pdf.info_line("الفترة", period_label)
    pdf.info_line("تاريخ الاستخراج", datetime.now().strftime("%Y-%m-%d"))
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

REP_MENU_ROWS = [["💰 التحصيل", "🔍 البحث عن عميل"], ["📊 تقرير السدادات"], ["🚪 خروج"]]

ADMIN_MENU_ROWS = [
    ["👥 المندوبين", "👨‍💼 المساعدين"],
    ["🎯 أهداف التحصيل", "💰 التحصيلات"],
    ["🔍 البحث عن عميل", "📊 التقارير"],
    ["📩 إرسال رسالة"],
    ["🚪 خروج"],
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
    r1 = []
    if perms.get("view_representatives"):
        r1.append("👥 المندوبين")
    if perms.get("view_payments") or perms.get("manage_targets"):
        pass
    rows2 = []
    if perms.get("manage_targets"):
        rows2.append("🎯 أهداف التحصيل")
    if perms.get("view_payments"):
        rows2.append("💰 التحصيلات")
    rows3 = []
    if perms.get("search_customers"):
        rows3.append("🔍 البحث عن عميل")
    if perms.get("view_reports"):
        rows3.append("📊 التقارير")
    if r1:
        rows.append(r1)
    if rows2:
        rows.append(rows2)
    if rows3:
        rows.append(rows3)
    if perms.get("send_messages"):
        rows.append(["📩 إرسال رسالة"])
    rows.append(["🚪 خروج"])
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
    label = "قيمة السداد" if target == "collect" else "قيمة الهدف الشهري"
    await query.edit_message_text(
        f"أدخل {label} باستخدام لوحة الأرقام:\n\nالقيمة الحالية: {cur if cur else '0'}",
        reply_markup=build_keypad_kb(cur),
    )
    return None


def build_calendar_kb(year, month):
    import calendar as _cal
    c = _cal.Calendar(firstweekday=6)  # يبدأ الأسبوع بالأحد
    weeks = c.monthdayscalendar(year, month)
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    rows = [[
        InlineKeyboardButton("◀️", callback_data=f"cal:nav:{prev_y}:{prev_m}"),
        InlineKeyboardButton(f"{MONTHS_AR[month-1]} {year}", callback_data="noop"),
        InlineKeyboardButton("▶️", callback_data=f"cal:nav:{next_y}:{next_m}"),
    ]]
    day_labels = ["أحد", "اثنين", "ثلاثاء", "أربعاء", "خميس", "جمعة", "سبت"]
    rows.append([InlineKeyboardButton(d, callback_data="noop") for d in day_labels])
    for week in weeks:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="noop"))
            else:
                row.append(InlineKeyboardButton(str(day), callback_data=f"cal:day:{year}:{month}:{day}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("📅 اليوم", callback_data="cal:today")])
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
) = range(30)

CB_METHOD = "method:"

# ============================================================
# HELPERS
# ============================================================

def session_of(context):
    return context.user_data.get("session")


def touch_session(context):
    context.user_data["last_active"] = datetime.now()


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


def current_month_target_progress(rep_id):
    m, y = month_year_now()
    target = get_target(rep_id, m, y)
    collected = get_total(get_payments_by_rep(rep_id, m, y))
    remaining = max(target - collected, 0)
    pct = (collected / target * 100) if target else 0
    return target, collected, remaining, pct


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


# ---- Login ----

async def login_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "🔐 دخول":
        await update.message.reply_text("اسم المستخدم:", reply_markup=CANCEL_KB)
        return LOGIN_USERNAME
    context.user_data["login_username"] = text
    await update.message.reply_text("الرقم السري:")
    return LOGIN_PASSWORD


async def login_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    username = context.user_data.get("login_username", "")
    user = get_user_by_username(username)
    if not user or not verify_password(password, user["password_hash"]):
        await update.message.reply_text("❌ اسم المستخدم أو الرقم السري غير صحيح. حاول مرة أخرى.\n\nاسم المستخدم:")
        return LOGIN_USERNAME
    if not user["active"]:
        await update.message.reply_text("⛔ هذا الحساب موقوف حالياً. تواصل مع الإدارة.", reply_markup=LOGIN_KB)
        return LOGIN_USERNAME
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
    context.user_data["collect"] = {}
    await update.message.reply_text("أدخل اسم العميل:", reply_markup=CANCEL_KB)
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
        await send_main_menu(query, context)
        return MAIN_MENU
    add_payment(data["customer_name"], data["amount"], data["method"], data["payment_date"], session["id"])
    await query.edit_message_text(
        "✅ تم تسجيل السداد بنجاح\n\n"
        f"👤 العميل: {data['customer_name']}\n"
        f"💰 القيمة: {data['amount']:,.2f} د.ل\n"
        f"💳 الطريقة: {data['method']}\n"
        f"📅 التاريخ: {data['payment_date']}\n"
        f"👤 المندوب: {session['name']}"
    )
    await notify_admins_new_payment(context, session, data)
    context.user_data.pop("collect", None)
    await query.message.reply_text("العملية التالية:", reply_markup=main_menu_kb(session))
    return MAIN_MENU


async def notify_admins_new_payment(context: ContextTypes.DEFAULT_TYPE, rep_session, data):
    """يرسل إشعاراً لجميع حسابات المدير عند تسجيل المندوب لعملية سداد جديدة."""
    text = (
        f"🔔 عملية سداد جديدة\n\n"
        f"👤 المندوب: {rep_session['name']}\n"
        f"🏪 العميل: {data['customer_name']}\n"
        f"💰 القيمة: {data['amount']:,.2f} د.ل\n"
        f"💳 الطريقة: {data['method']}\n"
        f"📅 التاريخ: {data['payment_date']}"
    )
    for admin in list_users_by_role("admin"):
        if admin["telegram_id"]:
            try:
                await context.bot.send_message(admin["telegram_id"], text)
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
    buttons = [[InlineKeyboardButton("📄 تصدير كشف حساب PDF", callback_data="export_customer_pdf")]] if can_export else None
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)
    await send_main_menu(update, context)
    return MAIN_MENU


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
    lines = [f"📊 تقرير سدادات: {session['name']}\n"]
    for r in rows[:30]:
        lines.append(f"• {r['customer_name']} | {r['amount']:,.2f} د.ل | {r['payment_date']} | {r['method']}")
    if len(rows) > 30:
        lines.append(f"... و {len(rows)-30} عملية أخرى")
    lines.append(f"\nإجمالي السدادات: {total:,.2f} د.ل")
    context.user_data["last_rep_report"] = rows
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
    if not await check_pdf_ready(query.message):
        return
    path = f"/tmp/rep_report_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"
    await safe_send_pdf(
        query.message, generate_rep_report_pdf, path,
        f"تقرير سدادات - {session['name']}.pdf", session["name"], rows,
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
        f"الحالة: {'✅ نشط' if rep['active'] else '⛔ موقوف'}\n\n"
        f"الهدف الشهري: {target:,.2f} د.ل\n"
        f"المحصل: {collected:,.2f} د.ل\n"
        f"المتبقي: {remaining:,.2f} د.ل\n"
        f"نسبة الإنجاز: {pct:.1f}%\n\n"
        f"عدد عمليات السداد (إجمالي): {count} عملية"
    )
    buttons = []
    if user_has_permission(session, "edit_representatives"):
        buttons.append([InlineKeyboardButton("✏️ تعديل الاسم", callback_data=f"rep_editname:{rep_id}")])
        buttons.append([InlineKeyboardButton("🔒 تغيير الرقم السري", callback_data=f"rep_editpass:{rep_id}")])
        buttons.append([InlineKeyboardButton(("⛔ إيقاف" if rep["active"] else "✅ تفعيل"), callback_data=f"rep_toggle:{rep_id}")])
    if user_has_permission(session, "delete_representatives"):
        buttons.append([InlineKeyboardButton("🗑️ حذف المندوب", callback_data=f"rep_delete_confirm:{rep_id}")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


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
    if rep_id:
        update_user_password(rep_id, update.message.text.strip())
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
    data = context.user_data.pop("new_rep")
    data["password"] = update.message.text.strip()
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
    buttons = [[InlineKeyboardButton(("✅ " if a["active"] else "⛔ ") + a["name"], callback_data=f"assist_view:{a['id']}")] for a in assistants]
    text = "👨‍💼 قائمة المساعدين:" if assistants else "لا يوجد مساعدون مسجلون بعد."
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)
    await update.message.reply_text("لإضافة مساعد جديد:", reply_markup=kb([["➕ إضافة مساعد"], ["🔙 رجوع للقائمة الرئيسية"]]))
    return MAIN_MENU


async def assist_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    assist_id = int(query.data.split(":")[1])
    a = get_user(assist_id)
    perms = get_permissions(assist_id)
    perm_lines = "\n".join(f"{'☑️' if v else '☐'} {PERMISSIONS[k]}" for k, v in perms.items())
    text = f"اسم المساعد: {a['name']}\nاسم المستخدم: {a['username']}\nالحالة: {'✅ نشط' if a['active'] else '⛔ موقوف'}\n\nالصلاحيات:\n{perm_lines}"
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
    if assist_id:
        update_user_password(assist_id, update.message.text.strip())
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
    data = context.user_data.pop("new_assist")
    data["password"] = update.message.text.strip()
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
    return MAIN_MENU


async def target_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


async def target_amount_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ الرجاء إدخال رقم صحيح أكبر من صفر:")
        return TARGET_AMOUNT
    rep_id = context.user_data.pop("target_rep_id", None)
    context.user_data.pop("kp_value", None)
    context.user_data.pop("kp_target", None)
    if rep_id:
        m, y = month_year_now()
        set_target(rep_id, m, y, amount)
        rep = get_user(rep_id)
        await update.message.reply_text(f"✅ تم حفظ الهدف الشهري لـ {rep['name']}: {amount:,.2f} د.ل")
    await send_main_menu(update, context)
    return MAIN_MENU


# ============================================================
# ADMIN / ASSISTANT: all payments overview (💰 التحصيلات)
# ============================================================

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
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)
    await send_main_menu(update, context)
    return MAIN_MENU


async def export_all_payments_pdf_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("جاري إنشاء الملف...")
    rows = context.user_data.get("last_all_payments") or get_all_payments()
    if not await check_pdf_ready(query.message):
        return
    path = f"/tmp/all_payments_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"
    await safe_send_pdf(query.message, generate_general_report_pdf, path, "جميع التحصيلات.pdf", rows)


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

    can_export = user_has_permission(session, "export_pdf")

    if kind == "today":
        rows = get_payments_today()
        total = get_total(rows)
        text = f"📅 تقرير اليوم ({datetime.now().strftime('%Y-%m-%d')})\n\nعدد العمليات: {len(rows)}\nالإجمالي: {total:,.2f} د.ل"
        context.user_data["last_report"] = ("general", rows, "تقرير اليوم")
    elif kind == "month":
        m, y = month_year_now()
        rows = get_all_payments(m, y)
        total = get_total(rows)
        text = f"📆 تقرير الشهر ({MONTHS_AR[m-1]} {y})\n\nعدد العمليات: {len(rows)}\nالإجمالي: {total:,.2f} د.ل"
        context.user_data["last_report"] = ("general", rows, f"شهر {MONTHS_AR[m-1]} {y}")
    elif kind == "method":
        totals = totals_by_method()
        grand = sum(totals.values())
        lines = [f"{k}: {v:,.2f} د.ل" for k, v in totals.items()]
        text = "🏦 تقرير حسب طريقة السداد\n\n" + "\n".join(lines) + f"\n\nالإجمالي: {grand:,.2f} د.ل"
        context.user_data["last_report"] = ("method", totals, "")
    elif kind == "general":
        rows = get_all_payments()
        total = get_total(rows)
        text = f"📋 التقرير العام\n\nعدد العمليات: {len(rows)}\nالإجمالي: {total:,.2f} د.ل"
        context.user_data["last_report"] = ("general", rows, "التقرير العام")
    else:
        return

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
        f"عدد العمليات: {len(rows)}\nإجمالي التحصيل: {total:,.2f} د.ل\n\n"
        f"الهدف الشهري: {target:,.2f} د.ل\nالمحصل هذا الشهر: {collected:,.2f} د.ل\n"
        f"المتبقي: {remaining:,.2f} د.ل\nنسبة الإنجاز: {pct:.1f}%"
    )
    context.user_data["last_report"] = ("rep", rows, rep["name"])
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
    kind, data, label = report
    path = f"/tmp/report_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"
    if kind == "general":
        await safe_send_pdf(query.message, generate_general_report_pdf, path, f"{label or 'تقرير'}.pdf", data, period_label=label)
    elif kind == "rep":
        await safe_send_pdf(query.message, generate_rep_report_pdf, path, f"تقرير - {label}.pdf", label, data)
    elif kind == "method":
        await safe_send_pdf(query.message, generate_method_report_pdf, path, "تقرير طرق السداد.pdf", data)

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
        await query.message.reply_text("اكتب نص الرسالة:", reply_markup=CANCEL_KB)
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
    body = update.message.text.strip()
    context.user_data["msg_body"] = body
    target = context.user_data.get("msg_target", {})
    label = "جميع المندوبين" if target.get("type") == "all" else target.get("name", "")
    await update.message.reply_text(
        f"هل تريد إرسال هذه الرسالة إلى {label}؟\n\n«{body}»",
        reply_markup=yesno_kb("msg_send_confirm", "msg_send_cancel", "✅ نعم، إرسال", "❌ إلغاء"),
    )
    return MAIN_MENU


async def msg_send_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = session_of(context)
    target = context.user_data.pop("msg_target", {})
    body = context.user_data.pop("msg_body", "")
    text = f"📩 رسالة من الإدارة\n\n{body}"
    sent = 0
    if target.get("type") == "all":
        reps = list_users_by_role("representative", active_only=True)
        for r in reps:
            if r["telegram_id"]:
                try:
                    await context.bot.send_message(r["telegram_id"], text)
                    sent += 1
                except Exception:
                    pass
        log_message(session["id"], None, "all", body)
    else:
        rep = get_user(target.get("id"))
        if rep and rep["telegram_id"]:
            try:
                await context.bot.send_message(rep["telegram_id"], text)
                sent += 1
            except Exception:
                pass
        log_message(session["id"], target.get("id"), "single", body)
    await query.edit_message_text(f"✅ تم إرسال الرسالة ({sent} مستلم).")
    await send_main_menu(query, context)


async def msg_send_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("msg_target", None)
    context.user_data.pop("msg_body", None)
    await query.edit_message_text("تم الإلغاء.")
    await send_main_menu(query, context)


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

    # representative
    if text == "💰 التحصيل" and session["role"] == "representative":
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
    if text == "💰 التحصيلات":
        return await payments_overview(update, context)
    if text == "📊 التقارير":
        return await reports_menu(update, context)
    if text == "📩 إرسال رسالة":
        return await msg_start(update, context)

    await update.message.reply_text("الرجاء استخدام الأزرار في القائمة.", reply_markup=main_menu_kb(session))
    return MAIN_MENU


async def unknown_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()


# ============================================================
# APPLICATION SETUP
# ============================================================

def build_app():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ADMIN_SETUP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_setup_name)],
            ADMIN_SETUP_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_setup_username)],
            ADMIN_SETUP_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_setup_password)],
            LOGIN_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_username)],
            LOGIN_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)],

            MAIN_MENU: [
                CallbackQueryHandler(collect_method_cb, pattern=f"^{CB_METHOD}"),
                CallbackQueryHandler(save_payment_cb, pattern="^(save_payment|cancel_payment)$"),
                CallbackQueryHandler(export_customer_pdf_cb, pattern="^export_customer_pdf$"),
                CallbackQueryHandler(export_rep_report_pdf_cb, pattern="^export_rep_report_pdf$"),
                CallbackQueryHandler(rep_view_cb, pattern="^rep_view:"),
                CallbackQueryHandler(rep_toggle_cb, pattern="^rep_toggle:"),
                CallbackQueryHandler(rep_delete_confirm_cb, pattern="^rep_delete_confirm:"),
                CallbackQueryHandler(rep_delete_do_cb, pattern="^rep_delete_do:"),
                CallbackQueryHandler(rep_editname_cb, pattern="^rep_editname:"),
                CallbackQueryHandler(rep_editpass_cb, pattern="^rep_editpass:"),
                CallbackQueryHandler(assist_view_cb, pattern="^assist_view:"),
                CallbackQueryHandler(assist_toggle_cb, pattern="^assist_toggle:"),
                CallbackQueryHandler(assist_delete_confirm_cb, pattern="^assist_delete_confirm:"),
                CallbackQueryHandler(assist_delete_do_cb, pattern="^assist_delete_do:"),
                CallbackQueryHandler(assist_editpass_cb, pattern="^assist_editpass:"),
                CallbackQueryHandler(assist_perms_cb, pattern="^assist_perms:"),
                CallbackQueryHandler(perm_toggle_cb, pattern="^permtoggle:"),
                CallbackQueryHandler(target_pick_cb, pattern="^target_pick:"),
                CallbackQueryHandler(export_all_payments_pdf_cb, pattern="^export_all_payments_pdf$"),
                CallbackQueryHandler(report_cb, pattern="^report:"),
                CallbackQueryHandler(report_rep_pick_cb, pattern="^reportrep:"),
                CallbackQueryHandler(export_report_pdf_cb, pattern="^export_report_pdf$"),
                CallbackQueryHandler(msg_to_cb, pattern="^msgto:"),
                CallbackQueryHandler(msg_pick_rep_cb, pattern="^msgrep:"),
                CallbackQueryHandler(msg_type_cb, pattern="^msgtype:"),
                CallbackQueryHandler(msg_send_confirm_cb, pattern="^msg_send_confirm$"),
                CallbackQueryHandler(msg_send_cancel_cb, pattern="^msg_send_cancel$"),
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
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_body_do),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^❌ إلغاء الأمر$"), cancel_to_menu),
            MessageHandler(filters.Regex("^🚪 خروج$"), logout),
            CallbackQueryHandler(unknown_cb),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)
    return app


if __name__ == "__main__":
    application = build_app()
    logger.info("Alhaya Pharma collection bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
