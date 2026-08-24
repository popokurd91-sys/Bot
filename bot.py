import os
import io
import sqlite3
import logging
import requests
import qrcode
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters
)

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]

API_BASE_URL = os.environ.get("API_BASE_URL", "https://iranisystem.com/bot/api/v1/")
API_KEY = os.environ.get("API_KEY")  # Bearer token for the VPN panel API

CARD_NUMBER = os.environ.get("CARD_NUMBER", "0000-0000-0000-0000")
CARD_HOLDER = os.environ.get("CARD_HOLDER", "نام صاحب کارت")

# Renewal pricing (used when a user extends their own service from wallet balance)
PRICE_PER_DAY = int(os.environ.get("PRICE_PER_DAY", "2000"))
PRICE_PER_GIG = int(os.environ.get("PRICE_PER_GIG", "6000"))

# Referral commission (% of plan price credited to the referrer's wallet on each purchase)
COMMISSION_PERCENT = float(os.environ.get("COMMISSION_PERCENT", "10"))

DB_PATH = os.path.abspath("vpn_bot.db")

# Default plans seeded into the DB on first run (admin can add/edit/delete via /admin from then on)
DEFAULT_PLANS = [
    {"name": "۱۰ گیگ / ۳۰ روز", "gig": 10, "day": 30, "price": 60000},
    {"name": "۲۰ گیگ / ۳۰ روز", "gig": 20, "day": 30, "price": 100000},
    {"name": "۵۰ گیگ / ۳۰ روز", "gig": 50, "day": 30, "price": 200000},
    {"name": "۱۰۰ گیگ / ۳۰ روز", "gig": 100, "day": 30, "price": 350000},
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# In-memory pending-input state, e.g. {user_id: {"action": "awaiting_receipt", "plan_index": 0}}
PENDING_ACTIONS = {}

# Persistent reply-keyboard button labels
BTN_BUY = "🛍 خرید اشتراک"
BTN_SERVICES = "🔍 اشتراک‌ها"
BTN_ACCOUNT = "👤 حساب کاربری"
BTN_TOPUP = "💳 شارژ حساب"
BTN_TRIAL = "🎁 تست رایگان"
BTN_REFERRAL = "👥 شارژ رایگان"
BTN_HELP = "💬 آموزش اتصال و پشتیبانی"
BTN_ADMIN = "🛠 پنل مدیریت"


# ============================================================
# DATABASE
# ============================================================
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            plan_name TEXT,
            gig REAL,
            day INTEGER,
            price INTEGER,
            status TEXT DEFAULT 'pending',
            receipt_file_id TEXT,
            service_username TEXT,
            config_link TEXT,
            created_at TEXT,
            decided_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            service_username TEXT,
            plan_name TEXT,
            expiry_time INTEGER,
            expiry_notified INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            gig REAL NOT NULL,
            day INTEGER NOT NULL,
            price INTEGER NOT NULL,
            active INTEGER DEFAULT 1
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 0,
            referrer_id INTEGER,
            free_trial_used INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS wallet_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            reason TEXT,
            admin_id INTEGER,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS topup_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            amount INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            receipt_file_id TEXT,
            created_at TEXT,
            decided_at TEXT
        )
    """)
    conn.commit()

    # Seed default plans only if the table is empty (first run)
    c.execute("SELECT COUNT(*) as cnt FROM plans")
    if c.fetchone()["cnt"] == 0:
        for p in DEFAULT_PLANS:
            c.execute(
                "INSERT INTO plans (name, gig, day, price, active) VALUES (?, ?, ?, ?, 1)",
                (p["name"], p["gig"], p["day"], p["price"])
            )
        conn.commit()

    conn.close()


def get_active_plans():
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT * FROM plans WHERE active = 1 ORDER BY id")
    rows = c.fetchall()
    conn.close()
    return rows


def get_all_plans():
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT * FROM plans ORDER BY id")
    rows = c.fetchall()
    conn.close()
    return rows


def get_plan(plan_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT * FROM plans WHERE id = ?", (plan_id,))
    row = c.fetchone()
    conn.close()
    return row


def add_plan(name, gig, day, price):
    conn = db_connect()
    c = conn.cursor()
    c.execute(
        "INSERT INTO plans (name, gig, day, price, active) VALUES (?, ?, ?, ?, 1)",
        (name, gig, day, price)
    )
    plan_id = c.lastrowid
    conn.commit()
    conn.close()
    return plan_id


def update_plan(plan_id, name, gig, day, price):
    conn = db_connect()
    c = conn.cursor()
    c.execute(
        "UPDATE plans SET name = ?, gig = ?, day = ?, price = ? WHERE id = ?",
        (name, gig, day, price, plan_id)
    )
    conn.commit()
    conn.close()


def delete_plan(plan_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("UPDATE plans SET active = 0 WHERE id = ?", (plan_id,))
    conn.commit()
    conn.close()


def create_order(user_id, username, plan):
    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        INSERT INTO orders (user_id, username, plan_name, gig, day, price, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
    """, (user_id, username, plan["name"], plan["gig"], plan["day"], plan["price"],
          datetime.utcnow().isoformat()))
    order_id = c.lastrowid
    conn.commit()
    conn.close()
    return order_id


def set_order_receipt(order_id, file_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("UPDATE orders SET receipt_file_id = ? WHERE id = ?", (file_id, order_id))
    conn.commit()
    conn.close()


def get_order(order_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    row = c.fetchone()
    conn.close()
    return row


def update_order_status(order_id, status, service_username=None, config_link=None):
    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        UPDATE orders SET status = ?, service_username = ?, config_link = ?, decided_at = ?
        WHERE id = ?
    """, (status, service_username, config_link, datetime.utcnow().isoformat(), order_id))
    conn.commit()
    conn.close()


def add_service(user_id, service_username, plan_name, expiry_time=None):
    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        INSERT INTO services (user_id, service_username, plan_name, expiry_time, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, service_username, plan_name, expiry_time, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def update_service_expiry(service_username, expiry_time):
    conn = db_connect()
    c = conn.cursor()
    c.execute(
        "UPDATE services SET expiry_time = ?, expiry_notified = 0 WHERE service_username = ?",
        (expiry_time, service_username)
    )
    conn.commit()
    conn.close()


def get_services_expiring_soon(within_seconds):
    """Services expiring within the given window, not yet notified."""
    import time
    cutoff = int(time.time()) + within_seconds
    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        SELECT * FROM services
        WHERE expiry_time IS NOT NULL AND expiry_time <= ? AND expiry_notified = 0
    """, (cutoff,))
    rows = c.fetchall()
    conn.close()
    return rows


def mark_service_notified(service_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("UPDATE services SET expiry_notified = 1 WHERE id = ?", (service_id,))
    conn.commit()
    conn.close()


def get_user_services(user_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT * FROM services WHERE user_id = ? ORDER BY id DESC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows


# ------------------------------------------------------------
# Wallet
# ------------------------------------------------------------
def register_user(user_id, username, referrer_id=None):
    conn = db_connect()
    c = conn.cursor()
    c.execute(
        "INSERT INTO users (user_id, username, balance, referrer_id) VALUES (?, ?, 0, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET username = excluded.username",
        (user_id, username, referrer_id)
    )
    conn.commit()
    conn.close()


def get_referrer_id(user_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT referrer_id FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row["referrer_id"] if row else None


def get_referral_earnings(user_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM wallet_transactions "
        "WHERE user_id = ? AND reason LIKE 'کمیسیون%'",
        (user_id,)
    )
    total = c.fetchone()["total"]
    conn.close()
    return total


def get_referral_count(user_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as cnt FROM users WHERE referrer_id = ?", (user_id,))
    cnt = c.fetchone()["cnt"]
    conn.close()
    return cnt


def get_wallet_history(user_id, limit=10):
    conn = db_connect()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM wallet_transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    rows = c.fetchall()
    conn.close()
    return rows


def get_sales_stats():
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as cnt, COALESCE(SUM(price), 0) as total FROM orders WHERE status = 'approved'")
    approved = c.fetchone()
    c.execute("SELECT COUNT(*) as cnt FROM orders WHERE status = 'pending'")
    pending = c.fetchone()
    c.execute("SELECT COUNT(*) as cnt FROM users")
    users_count = c.fetchone()
    conn.close()
    return {
        "approved_count": approved["cnt"],
        "approved_revenue": approved["total"],
        "pending_count": pending["cnt"],
        "users_count": users_count["cnt"],
    }


# ------------------------------------------------------------
# Free trial
# ------------------------------------------------------------
def has_used_free_trial(user_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT free_trial_used FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return bool(row["free_trial_used"]) if row else False


def mark_free_trial_used(user_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute(
        "INSERT INTO users (user_id, username, balance, free_trial_used) VALUES (?, '', 0, 1) "
        "ON CONFLICT(user_id) DO UPDATE SET free_trial_used = 1",
        (user_id,)
    )
    conn.commit()
    conn.close()


# ------------------------------------------------------------
# Wallet top-up requests (card-to-card, admin-approved)
# ------------------------------------------------------------
def create_topup_request(user_id, username, amount):
    conn = db_connect()
    c = conn.cursor()
    c.execute(
        "INSERT INTO topup_requests (user_id, username, amount, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
        (user_id, username, amount, datetime.utcnow().isoformat())
    )
    req_id = c.lastrowid
    conn.commit()
    conn.close()
    return req_id


def set_topup_receipt(request_id, file_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("UPDATE topup_requests SET receipt_file_id = ? WHERE id = ?", (file_id, request_id))
    conn.commit()
    conn.close()


def get_topup_request(request_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT * FROM topup_requests WHERE id = ?", (request_id,))
    row = c.fetchone()
    conn.close()
    return row


def update_topup_status(request_id, status):
    conn = db_connect()
    c = conn.cursor()
    c.execute(
        "UPDATE topup_requests SET status = ?, decided_at = ? WHERE id = ?",
        (status, datetime.utcnow().isoformat(), request_id)
    )
    conn.commit()
    conn.close()


def get_balance(user_id):
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row["balance"] if row else 0


def adjust_balance(user_id, amount, reason=None, admin_id=None):
    """amount can be positive (credit) or negative (debit). Returns new balance."""
    conn = db_connect()
    c = conn.cursor()
    c.execute(
        "INSERT INTO users (user_id, username, balance) VALUES (?, '', 0) "
        "ON CONFLICT(user_id) DO NOTHING",
        (user_id,)
    )
    c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    c.execute(
        "INSERT INTO wallet_transactions (user_id, amount, reason, admin_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, amount, reason, admin_id, datetime.utcnow().isoformat())
    )
    conn.commit()
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    new_balance = c.fetchone()["balance"]
    conn.close()
    return new_balance


def credit_referral_commission(buyer_user_id, price):
    """If the buyer was referred by someone, credit that referrer's wallet. Returns (referrer_id, amount) or None."""
    referrer_id = get_referrer_id(buyer_user_id)
    if not referrer_id or referrer_id == buyer_user_id:
        return None
    commission = int(round(price * COMMISSION_PERCENT / 100))
    if commission <= 0:
        return None
    adjust_balance(referrer_id, commission, reason=f"کمیسیون خرید زیرمجموعه (ID: {buyer_user_id})")
    return referrer_id, commission


# ============================================================
# VPN PANEL API CLIENT
# ============================================================
def api_request(method_name, http_method="GET", params=None):
    """Calls the Wizard XRay panel API. Returns parsed JSON dict or None on failure."""
    url = API_BASE_URL.rstrip("/") + "/" + method_name
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    try:
        if http_method.upper() == "GET":
            resp = requests.get(url, headers=headers, params=params or {}, timeout=15)
        else:
            resp = requests.post(url, headers=headers, data=params or {}, timeout=15)
        return resp.json()
    except Exception as e:
        logger.error(f"API request failed [{method_name}]: {e}")
        return None


def api_create_service(gig, day, test=0):
    return api_request("create", "POST", {"gig": gig, "day": day, "test": test})


def api_find_service(username):
    return api_request("find", "POST", {"username": username})


def api_status():
    return api_request("status", "GET")


def api_time_upgrade(username, day):
    return api_request("time_upg", "POST", {"username": username, "day": day})


def api_size_upgrade(username, gig):
    return api_request("size_upg", "POST", {"username": username, "gig": gig})


# ============================================================
# CONFIG DELIVERY (QR code + copyable text)
# ============================================================
def generate_qr_bytes(data):
    """Returns a PNG image (BytesIO) encoding the given text as a QR code."""
    qr = qrcode.QRCode(border=2, box_size=8)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    buf.name = "config.png"
    return buf


async def deliver_service_to_user(bot, chat_id, plan_name, service_username, service_data, extra_note=None):
    """Sends the buyer a QR code (scannable) plus the raw config text (copy-pasteable)."""
    sub_link = service_data.get("sub_link")
    tak_links = service_data.get("tak_links") or []
    primary_config = tak_links[0] if tak_links else sub_link

    if primary_config:
        qr_image = generate_qr_bytes(primary_config)
        caption = f"🎉 سرویس «{plan_name}» آماده شد!\n📷 این QR رو تو اپلیکیشن V2Ray اسکن کن، یا متن کانفیگ رو از پیام بعدی کپی کن."
        try:
            await bot.send_photo(chat_id=chat_id, photo=qr_image, caption=caption)
        except Exception as e:
            logger.error(f"Failed to send QR code to {chat_id}: {e}")

    lines = [f"👤 یوزرنیم: `{service_username}`"]
    if primary_config:
        lines.append(f"\n🔗 کانفیگ (برای Import دستی — همینو کپی کن):\n`{primary_config}`")
    if sub_link:
        lines.append(f"\n🔗 لینک اشتراک (Subscription — همه‌ی سرورها رو خودکار میاره):\n`{sub_link}`")
    if extra_note:
        lines.append(f"\n{extra_note}")
    text = "\n".join(lines)

    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Failed to send config text to {chat_id}: {e}")


# ============================================================
# UI HELPERS
# ============================================================
def reply_main_keyboard(is_admin=False):
    rows = [
        [BTN_BUY],
        [BTN_SERVICES, BTN_ACCOUNT],
        [BTN_TOPUP, BTN_TRIAL, BTN_REFERRAL],
        [BTN_HELP],
    ]
    if is_admin:
        rows.append([BTN_ADMIN])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("🛒 خرید سرویس", callback_data="buy_menu")],
        [InlineKeyboardButton("📦 سرویس‌های من", callback_data="my_services")],
        [InlineKeyboardButton("💰 کیف پول من", callback_data="my_wallet")],
        [InlineKeyboardButton("🔗 زیرمجموعه‌گیری", callback_data="referral_info")],
        [InlineKeyboardButton("☎️ پشتیبانی", callback_data="support")],
    ]
    return InlineKeyboardMarkup(keyboard)


def plans_keyboard():
    keyboard = []
    for plan in get_active_plans():
        label = f"{plan['name']} - {plan['price']:,} تومان"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"plan_{plan['id']}")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)


def admin_decision_keyboard(order_id):
    keyboard = [
        [
            InlineKeyboardButton("✅ تایید", callback_data=f"approve_{order_id}"),
            InlineKeyboardButton("❌ رد", callback_data=f"reject_{order_id}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def topup_decision_keyboard(request_id):
    keyboard = [
        [
            InlineKeyboardButton("✅ تایید شارژ", callback_data=f"topup_approve_{request_id}"),
            InlineKeyboardButton("❌ رد", callback_data=f"topup_reject_{request_id}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def admin_plans_keyboard():
    keyboard = []
    for plan in get_all_plans():
        status = "" if plan["active"] else " (غیرفعال)"
        label = f"{plan['name']} - {plan['price']:,}ت{status}"
        keyboard.append([
            InlineKeyboardButton(label, callback_data=f"noop"),
        ])
        keyboard.append([
            InlineKeyboardButton("✏️ ویرایش", callback_data=f"adm_edit_{plan['id']}"),
            InlineKeyboardButton("🗑 حذف", callback_data=f"adm_del_{plan['id']}"),
        ])
    keyboard.append([InlineKeyboardButton("➕ افزودن پلن جدید", callback_data="adm_add")])
    keyboard.append([InlineKeyboardButton("🔙 پنل ادمین", callback_data="admin_home")])
    return InlineKeyboardMarkup(keyboard)


def admin_home_keyboard():
    keyboard = [
        [InlineKeyboardButton("📦 مدیریت پلن‌ها", callback_data="admin_plans_menu")],
        [InlineKeyboardButton("👥 مدیریت موجودی کاربران", callback_data="adm_wallet_start")],
        [InlineKeyboardButton("📊 آمار فروش", callback_data="admin_stats")],
    ]
    return InlineKeyboardMarkup(keyboard)


def user_services_keyboard(services):
    keyboard = []
    for s in services:
        keyboard.append([InlineKeyboardButton(
            f"📊 مصرف: {s['plan_name']}", callback_data=f"svc_usage_{s['service_username']}"
        )])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)


def service_usage_keyboard(service_username):
    keyboard = [
        [
            InlineKeyboardButton(f"➕ افزودن روز ({PRICE_PER_DAY:,}ت/روز)", callback_data=f"svc_addday_{service_username}"),
            InlineKeyboardButton(f"➕ افزودن گیگ ({PRICE_PER_GIG:,}ت/گیگ)", callback_data=f"svc_addgig_{service_username}"),
        ],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="my_services")],
    ]
    return InlineKeyboardMarkup(keyboard)


# ============================================================
# USER HANDLERS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    referrer_id = None
    if context.args:
        payload = context.args[0]
        if payload.startswith("ref_"):
            try:
                candidate = int(payload[4:])
                if candidate != user.id:
                    referrer_id = candidate
            except ValueError:
                pass
    register_user(user.id, user.username or user.first_name, referrer_id=referrer_id)
    text = (
        "🔰 به فروشگاه سرویس VPN خوش اومدی!\n\n"
        "از دکمه‌های پایین صفحه استفاده کن:"
    )
    await update.message.reply_text(text, reply_markup=reply_main_keyboard(is_admin=user.id in ADMIN_IDS))


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await update.message.reply_text("🛠 پنل مدیریت:", reply_markup=admin_home_keyboard())


HELP_TEXT = (
    "📖 راهنمای اتصال:\n\n"
    "۱. یکی از این اپلیکیشن‌ها رو نصب کن:\n"
    "• اندروید: v2rayNG یا NekoBox\n"
    "• آیفون: Streisand یا FairVPN\n"
    "• ویندوز: v2rayN یا NekoRay\n"
    "• مک: V2rayU\n\n"
    "۲. کانفیگی که ربات فرستاده رو (یا با اسکن QR، یا با کپی متن) وارد اپ کن.\n"
    "۳. سرور مورد نظرت رو از لیست انتخاب کن و وصل شو.\n\n"
    "☎️ برای پشتیبانی بیشتر: @your_support_username"
)


async def reply_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username or user.first_name)
    text = update.message.text

    if text == BTN_BUY:
        await update.message.reply_text("یکی از پلن‌های زیر رو انتخاب کن:", reply_markup=plans_keyboard())

    elif text == BTN_SERVICES:
        services = get_user_services(user.id)
        if not services:
            await update.message.reply_text("هنوز هیچ سرویسی نداری.")
        else:
            await update.message.reply_text(
                "📦 سرویس‌های تو — برای دیدن مصرف روی هرکدوم بزن:",
                reply_markup=user_services_keyboard(services)
            )

    elif text == BTN_ACCOUNT:
        balance = get_balance(user.id)
        services_count = len(get_user_services(user.id))
        await update.message.reply_text(
            f"👤 حساب کاربری\n\n"
            f"آیدی عددی: {user.id}\n"
            f"💰 موجودی کیف پول: {balance:,} تومان\n"
            f"📦 تعداد سرویس‌ها: {services_count}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🧾 تاریخچه تراکنش‌ها", callback_data="wallet_history")]
            ])
        )

    elif text == BTN_TOPUP:
        PENDING_ACTIONS[user.id] = {"action": "topup_amount_input"}
        await update.message.reply_text("چقدر می‌خوای به کیف پولت شارژ کنی؟ (به تومان، مثلاً 50000):")

    elif text == BTN_TRIAL:
        await handle_free_trial(update, context)

    elif text == BTN_REFERRAL:
        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start=ref_{user.id}"
        count = get_referral_count(user.id)
        earnings = get_referral_earnings(user.id)
        await update.message.reply_text(
            f"👥 شارژ رایگان (زیرمجموعه‌گیری)\n\n"
            f"لینک اختصاصی تو:\n{link}\n\n"
            f"هر کسی با این لینک بیاد و خرید کنه، {COMMISSION_PERCENT:.0f}٪ مبلغ خریدش "
            f"به‌صورت خودکار به کیف پولت اضافه می‌شه.\n\n"
            f"👥 تعداد زیرمجموعه‌ها: {count}\n"
            f"💰 مجموع دریافتی: {earnings:,} تومان"
        )

    elif text == BTN_HELP:
        await update.message.reply_text(HELP_TEXT)

    elif text == BTN_ADMIN:
        if user.id in ADMIN_IDS:
            await update.message.reply_text("🛠 پنل مدیریت:", reply_markup=admin_home_keyboard())


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "main_menu":
        await query.edit_message_text(
            "🔰 به فروشگاه سرویس VPN خوش اومدی!\n\nاز منوی زیر یکی از گزینه‌ها رو انتخاب کن:",
            reply_markup=main_menu_keyboard()
        )

    elif data == "buy_menu":
        await query.edit_message_text("یکی از پلن‌های زیر رو انتخاب کن:", reply_markup=plans_keyboard())

    elif data.startswith("plan_"):
        plan_id = int(data.split("_")[1])
        plan = get_plan(plan_id)
        if plan is None or not plan["active"]:
            await query.answer("این پلن دیگه در دسترس نیست.", show_alert=True)
            return
        balance = get_balance(user_id)
        text = (
            f"✅ پلن انتخابی: {plan['name']}\n"
            f"💰 مبلغ: {plan['price']:,} تومان\n"
            f"💳 موجودی کیف پول تو: {balance:,} تومان\n\n"
            f"روش پرداخت رو انتخاب کن:"
        )
        keyboard_rows = []
        if balance >= plan["price"]:
            keyboard_rows.append([InlineKeyboardButton(
                "💰 پرداخت از کیف پول (آنی)", callback_data=f"pay_wallet_{plan_id}"
            )])
        keyboard_rows.append([InlineKeyboardButton(
            "💳 کارت به کارت", callback_data=f"pay_card_{plan_id}"
        )])
        keyboard_rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="buy_menu")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard_rows))

    elif data.startswith("pay_card_"):
        plan_id = int(data.split("_")[2])
        plan = get_plan(plan_id)
        if plan is None or not plan["active"]:
            await query.answer("این پلن دیگه در دسترس نیست.", show_alert=True)
            return
        PENDING_ACTIONS[user_id] = {"action": "awaiting_receipt", "plan_id": plan_id}
        text = (
            f"✅ پلن انتخابی: {plan['name']}\n"
            f"💰 مبلغ: {plan['price']:,} تومان\n\n"
            f"لطفاً مبلغ رو به شماره کارت زیر واریز کن و سپس عکس رسید رو همینجا ارسال کن:\n\n"
            f"💳 {CARD_NUMBER}\n👤 به نام: {CARD_HOLDER}\n\n"
            f"⚠️ بعد از ارسال رسید، سفارش برای بررسی به ادمین ارسال می‌شه."
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="buy_menu")]])
        await query.edit_message_text(text, reply_markup=keyboard)

    elif data.startswith("pay_wallet_"):
        plan_id = int(data.split("_")[2])
        await handle_wallet_purchase(query, context, plan_id)

    elif data == "my_services":
        services = get_user_services(user_id)
        if not services:
            text = "هنوز هیچ سرویسی نداری."
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="main_menu")]])
        else:
            text = "📦 سرویس‌های تو — برای دیدن مصرف روی هرکدوم بزن:"
            keyboard = user_services_keyboard(services)
        await query.edit_message_text(text, reply_markup=keyboard)

    elif data.startswith("svc_usage_"):
        service_username = data[len("svc_usage_"):]
        await handle_service_usage(query, service_username)

    elif data.startswith("svc_addday_"):
        service_username = data[len("svc_addday_"):]
        PENDING_ACTIONS[user_id] = {"action": "renew_input", "mode": "day", "service_username": service_username}
        await query.edit_message_text(f"چند روز می‌خوای اضافه کنی؟ (هر روز {PRICE_PER_DAY:,} تومان)")

    elif data.startswith("svc_addgig_"):
        service_username = data[len("svc_addgig_"):]
        PENDING_ACTIONS[user_id] = {"action": "renew_input", "mode": "gig", "service_username": service_username}
        await query.edit_message_text(f"چند گیگ می‌خوای اضافه کنی؟ (هر گیگ {PRICE_PER_GIG:,} تومان)")

    elif data == "my_wallet":
        balance = get_balance(user_id)
        text = f"💰 موجودی کیف پول تو: {balance:,} تومان"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧾 تاریخچه تراکنش‌ها", callback_data="wallet_history")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="main_menu")],
        ])
        await query.edit_message_text(text, reply_markup=keyboard)

    elif data == "wallet_history":
        history = get_wallet_history(user_id)
        if not history:
            text = "هنوز هیچ تراکنشی نداری."
        else:
            lines = ["🧾 آخرین تراکنش‌ها:\n"]
            for h in history:
                sign = "➕" if h["amount"] >= 0 else "➖"
                lines.append(f"{sign} {abs(h['amount']):,} تومان — {h['reason'] or '-'}")
            text = "\n".join(lines)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="my_wallet")]])
        await query.edit_message_text(text, reply_markup=keyboard)

    elif data == "referral_info":
        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        count = get_referral_count(user_id)
        earnings = get_referral_earnings(user_id)
        text = (
            f"🔗 لینک زیرمجموعه‌گیری تو:\n{link}\n\n"
            f"هر کسی با این لینک وارد بشه و خرید کنه، {COMMISSION_PERCENT:.0f}٪ مبلغ خریدش "
            f"به‌صورت خودکار به کیف پول تو اضافه می‌شه.\n\n"
            f"👥 تعداد زیرمجموعه‌ها: {count}\n"
            f"💰 مجموع کمیسیون دریافتی: {earnings:,} تومان"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="main_menu")]])
        await query.edit_message_text(text, reply_markup=keyboard)

    elif data == "admin_stats":
        if user_id not in ADMIN_IDS:
            return
        stats = get_sales_stats()
        panel_status = api_status()
        text = (
            f"📊 آمار فروش (این ربات):\n"
            f"✅ سفارش‌های تایید شده: {stats['approved_count']}\n"
            f"💰 درآمد کل: {stats['approved_revenue']:,} تومان\n"
            f"⏳ سفارش‌های در انتظار: {stats['pending_count']}\n"
            f"👥 تعداد کاربران ثبت‌شده: {stats['users_count']}\n"
        )
        if panel_status and panel_status.get("ok"):
            text += f"\n🖥 وضعیت پنل:\n{panel_status.get('result')}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_home")]])
        await query.edit_message_text(text, reply_markup=keyboard)

    elif data == "support":
        text = "برای پشتیبانی با ادمین در ارتباط باش: @your_support_username"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="main_menu")]])
        await query.edit_message_text(text, reply_markup=keyboard)

    elif data == "noop":
        pass  # plan name label row, not clickable

    elif data == "admin_home":
        if user_id not in ADMIN_IDS:
            return
        await query.edit_message_text("🛠 پنل مدیریت:", reply_markup=admin_home_keyboard())

    elif data == "admin_plans_menu":
        if user_id not in ADMIN_IDS:
            return
        await query.edit_message_text(
            "🛠 پنل مدیریت پلن‌ها:\nاز اینجا می‌تونی حجم، زمان و قیمت پلن‌ها رو تنظیم کنی.",
            reply_markup=admin_plans_keyboard()
        )

    elif data == "adm_wallet_start":
        if user_id not in ADMIN_IDS:
            return
        PENDING_ACTIONS[user_id] = {"action": "admin_wallet_input", "step": "user_id", "data": {}}
        await query.edit_message_text(
            "آیدی عددی کاربر رو بفرست (همون عددی که تو پیام سفارش‌ها به عنوان ID می‌بینی):"
        )

    elif data == "adm_add":
        if user_id not in ADMIN_IDS:
            return
        PENDING_ACTIONS[user_id] = {"action": "admin_plan_input", "step": "name", "plan_id": None, "data": {}}
        await query.edit_message_text("نام پلن جدید رو بفرست (مثلاً: ۱۵ گیگ / ۳۰ روز):")

    elif data.startswith("adm_edit_"):
        if user_id not in ADMIN_IDS:
            return
        plan_id = int(data.split("_")[2])
        plan = get_plan(plan_id)
        if plan is None:
            await query.answer("پلن یافت نشد.", show_alert=True)
            return
        PENDING_ACTIONS[user_id] = {"action": "admin_plan_input", "step": "name", "plan_id": plan_id, "data": {}}
        await query.edit_message_text(
            f"در حال ویرایش «{plan['name']}».\nنام جدید پلن رو بفرست (یا همین اسم فعلی رو دوباره بفرست):"
        )

    elif data.startswith("adm_del_"):
        if user_id not in ADMIN_IDS:
            return
        plan_id = int(data.split("_")[2])
        delete_plan(plan_id)
        await query.edit_message_text("🛠 پنل مدیریت پلن‌ها:", reply_markup=admin_plans_keyboard())

    elif data.startswith("approve_") or data.startswith("reject_"):
        await handle_admin_decision(query, context, data)

    elif data.startswith("topup_approve_") or data.startswith("topup_reject_"):
        await handle_topup_decision(query, context, data)


async def receipt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = PENDING_ACTIONS.get(user_id)

    if not state or state.get("action") not in ("awaiting_receipt", "topup_receipt"):
        return  # not expecting a receipt right now, ignore

    if not update.message.photo:
        await update.message.reply_text("لطفاً عکس رسید رو ارسال کن.")
        return

    if state["action"] == "topup_receipt":
        await _handle_topup_receipt(update, context, user_id, state)
        return

    plan = get_plan(state["plan_id"])
    if plan is None:
        await update.message.reply_text("این پلن دیگه در دسترس نیست، لطفاً دوباره از منو انتخاب کن.")
        del PENDING_ACTIONS[user_id]
        return

    file_id = update.message.photo[-1].file_id
    username = update.effective_user.username or update.effective_user.first_name

    order_id = create_order(user_id, username, plan)
    set_order_receipt(order_id, file_id)
    del PENDING_ACTIONS[user_id]

    await update.message.reply_text(
        "✅ رسید دریافت شد و برای بررسی ارسال شد. به محض تایید، کانفیگ برات ارسال می‌شه."
    )

    admin_text = (
        f"🆕 سفارش جدید #{order_id}\n"
        f"👤 کاربر: @{username} (ID: {user_id})\n"
        f"📦 پلن: {plan['name']}\n"
        f"💰 مبلغ: {plan['price']:,} تومان"
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                chat_id=admin_id, photo=file_id, caption=admin_text,
                reply_markup=admin_decision_keyboard(order_id)
            )
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")


async def _handle_topup_receipt(update, context, user_id, state):
    file_id = update.message.photo[-1].file_id
    username = update.effective_user.username or update.effective_user.first_name
    amount = state["amount"]

    request_id = create_topup_request(user_id, username, amount)
    set_topup_receipt(request_id, file_id)
    del PENDING_ACTIONS[user_id]

    await update.message.reply_text(
        "✅ رسید دریافت شد و برای بررسی ارسال شد. به محض تایید، مبلغ به کیف پولت اضافه می‌شه."
    )

    admin_text = (
        f"💳 درخواست شارژ کیف پول #{request_id}\n"
        f"👤 کاربر: @{username} (ID: {user_id})\n"
        f"💰 مبلغ: {amount:,} تومان"
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                chat_id=admin_id, photo=file_id, caption=admin_text,
                reply_markup=topup_decision_keyboard(request_id)
            )
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id} of topup request: {e}")


# ============================================================
# WALLET PURCHASE (instant, no manual approval needed)
# ============================================================
async def handle_wallet_purchase(query, context, plan_id):
    user_id = query.from_user.id
    plan = get_plan(plan_id)

    if plan is None or not plan["active"]:
        await query.answer("این پلن دیگه در دسترس نیست.", show_alert=True)
        return

    balance = get_balance(user_id)
    if balance < plan["price"]:
        await query.answer("موجودی کیف پول کافی نیست.", show_alert=True)
        return

    # Deduct first, then attempt to create the service; refund on failure.
    adjust_balance(user_id, -plan["price"], reason=f"خرید پلن: {plan['name']}")

    result = api_create_service(plan["gig"], plan["day"], test=0)

    if not result or not result.get("ok"):
        adjust_balance(user_id, plan["price"], reason=f"بازگشت وجه (خطا در ساخت سرویس): {plan['name']}")
        error_msg = result.get("error") if result else "بدون پاسخ از API"
        await query.edit_message_text(f"⚠️ خطا در ساخت سرویس: {error_msg}\nمبلغ به کیف پولت برگشت داده شد.")
        return

    service_data = result.get("result", {})
    service_username = service_data.get("username")
    config_link = service_data.get("sub_link")
    expiry_time = service_data.get("expiryTime")

    username = query.from_user.username or query.from_user.first_name
    order_id = create_order(user_id, username, plan)
    update_order_status(order_id, "approved", service_username, config_link)
    add_service(user_id, service_username, plan["name"], expiry_time=expiry_time)
    credit_referral_commission(user_id, plan["price"])

    new_balance = get_balance(user_id)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="main_menu")]])
    await query.edit_message_text(
        f"✅ سرویس «{plan['name']}» ساخته شد. کانفیگ رو الان براتون می‌فرستم...\n"
        f"💰 موجودی باقیمانده: {new_balance:,} تومان",
        reply_markup=keyboard
    )
    await deliver_service_to_user(context.bot, user_id, plan["name"], service_username, service_data)


# ============================================================
# FREE TRIAL (250MB / 1 day, one per Telegram account)
# ============================================================
async def handle_free_trial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if has_used_free_trial(user_id):
        await update.message.reply_text(
            "قبلاً از تست رایگان استفاده کردی. هر حساب فقط یک بار می‌تونه تست رایگان بگیره."
        )
        return

    # NOTE: the panel's own 'test' flag is fixed at 150MB/1day per the docs, so to give the
    # requested 250MB/1day we create a tiny real (non-test) service. This draws a small amount
    # from the reseller's own panel wallet balance, not from the customer's bot wallet.
    result = api_create_service(0.25, 1, test=0)

    if not result or not result.get("ok"):
        error_msg = result.get("error") if result else "بدون پاسخ از API"
        await update.message.reply_text(f"⚠️ خطا در ساخت سرویس تست: {error_msg}")
        return

    service_data = result.get("result", {})
    service_username = service_data.get("username")
    expiry_time = service_data.get("expiryTime")

    add_service(user_id, service_username, "تست رایگان (250MB/1روز)", expiry_time=expiry_time)
    mark_free_trial_used(user_id)

    await update.message.reply_text("🎁 سرویس تست رایگانت (۲۵۰ مگابایت / ۱ روز) ساخته شد!")
    await deliver_service_to_user(context.bot, user_id, "تست رایگان", service_username, service_data)


# ============================================================
# SERVICE USAGE LOOKUP
# ============================================================
async def handle_service_usage(query, service_username):
    result = api_find_service(service_username)

    if not result or not result.get("ok"):
        error_msg = result.get("error") if result else "بدون پاسخ از API"
        text = f"⚠️ خطا در دریافت اطلاعات مصرف: {error_msg}"
    else:
        info = result.get("result", {})
        # NOTE: exact field names for usage weren't confirmed from a real 'find' response yet.
        # Showing whatever the panel returns; adjust field names below once verified.
        lines = [f"📊 مصرف سرویس `{service_username}`:\n"]
        for key in ("gig", "day", "gig_byte", "info_online", "expiryTime"):
            if key in info:
                lines.append(f"• {key}: {info[key]}")
        if len(lines) == 1:
            lines.append("اطلاعاتی از پنل برنگشت.")
        text = "\n".join(lines)

    keyboard = service_usage_keyboard(service_username)
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")


# ============================================================
# ADMIN PLAN MANAGEMENT (multi-step text input)
# ============================================================
async def text_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = PENDING_ACTIONS.get(user_id)

    if not state:
        return  # no pending multi-step flow for this user, ignore

    action = state.get("action")

    if action == "admin_plan_input":
        await _handle_admin_plan_input(update, user_id, state)
    elif action == "admin_wallet_input":
        await _handle_admin_wallet_input(update, context, user_id, state)
    elif action == "renew_input":
        await _handle_renew_input(update, user_id, state)
    elif action == "topup_amount_input":
        await _handle_topup_amount_input(update, user_id, state)
    # else: not a text-driven flow (e.g. awaiting_receipt expects a photo) — ignore


async def _handle_topup_amount_input(update, user_id, state):
    text = update.message.text.strip()
    try:
        amount = int(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("یه عدد صحیح و مثبت بفرست (مثلاً 50000):")
        return

    state["action"] = "topup_receipt"
    state["amount"] = amount
    await update.message.reply_text(
        f"مبلغ {amount:,} تومان رو به شماره کارت زیر واریز کن و بعد عکس رسید رو بفرست:\n\n"
        f"💳 {CARD_NUMBER}\n👤 به نام: {CARD_HOLDER}"
    )


async def _handle_admin_plan_input(update, user_id, state):
    if user_id not in ADMIN_IDS:
        del PENDING_ACTIONS[user_id]
        return

    text = update.message.text.strip()
    step = state["step"]
    data = state["data"]

    if step == "name":
        data["name"] = text
        state["step"] = "gig"
        await update.message.reply_text("حجم پلن به گیگ رو بفرست (مثلاً 10):")

    elif step == "gig":
        try:
            data["gig"] = float(text)
        except ValueError:
            await update.message.reply_text("عدد معتبر بفرست (مثلاً 10 یا 0.5):")
            return
        state["step"] = "day"
        await update.message.reply_text("مدت پلن به روز رو بفرست (مثلاً 30):")

    elif step == "day":
        try:
            data["day"] = int(text)
        except ValueError:
            await update.message.reply_text("عدد صحیح بفرست (مثلاً 30):")
            return
        state["step"] = "price"
        await update.message.reply_text("قیمت پلن به تومان رو بفرست (مثلاً 60000):")

    elif step == "price":
        try:
            data["price"] = int(text)
        except ValueError:
            await update.message.reply_text("عدد صحیح بفرست (مثلاً 60000):")
            return

        if state["plan_id"] is None:
            add_plan(data["name"], data["gig"], data["day"], data["price"])
            confirm = "✅ پلن جدید اضافه شد."
        else:
            update_plan(state["plan_id"], data["name"], data["gig"], data["day"], data["price"])
            confirm = "✅ پلن ویرایش شد."

        del PENDING_ACTIONS[user_id]
        await update.message.reply_text(confirm, reply_markup=admin_plans_keyboard())


async def _handle_admin_wallet_input(update, context, user_id, state):
    if user_id not in ADMIN_IDS:
        del PENDING_ACTIONS[user_id]
        return

    text = update.message.text.strip()
    step = state["step"]
    data = state["data"]

    if step == "user_id":
        try:
            data["target_user_id"] = int(text)
        except ValueError:
            await update.message.reply_text("آیدی عددی معتبر بفرست:")
            return
        state["step"] = "amount"
        current = get_balance(data["target_user_id"])
        await update.message.reply_text(
            f"موجودی فعلی این کاربر: {current:,} تومان\n"
            f"چقدر تغییر بدم؟ (برای افزایش عدد مثبت مثل 50000، برای کاهش عدد منفی مثل -20000 بفرست):"
        )

    elif step == "amount":
        try:
            amount = int(text)
        except ValueError:
            await update.message.reply_text("عدد صحیح بفرست، مثبت یا منفی:")
            return

        target_user_id = data["target_user_id"]
        new_balance = adjust_balance(
            target_user_id, amount, reason="تنظیم دستی توسط ادمین", admin_id=user_id
        )
        del PENDING_ACTIONS[user_id]
        await update.message.reply_text(
            f"✅ انجام شد. موجودی جدید کاربر {target_user_id}: {new_balance:,} تومان",
            reply_markup=admin_home_keyboard()
        )
        try:
            sign = "افزایش" if amount >= 0 else "کاهش"
            await context.bot.send_message(
                chat_id=target_user_id,
                text=f"💰 موجودی کیف پول شما {sign} یافت ({abs(amount):,} تومان).\nموجودی فعلی: {new_balance:,} تومان"
            )
        except Exception as e:
            logger.error(f"Failed to notify user of balance change: {e}")


async def _handle_renew_input(update, user_id, state):
    text = update.message.text.strip()
    mode = state["mode"]
    service_username = state["service_username"]

    try:
        amount = float(text) if mode == "gig" else int(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("یه عدد مثبت معتبر بفرست:")
        return

    price_per_unit = PRICE_PER_GIG if mode == "gig" else PRICE_PER_DAY
    cost = int(round(amount * price_per_unit))
    balance = get_balance(user_id)

    if balance < cost:
        await update.message.reply_text(
            f"موجودی کیف پولت کافی نیست. هزینه‌ی این تمدید {cost:,} تومانه، "
            f"موجودی فعلی: {balance:,} تومان."
        )
        del PENDING_ACTIONS[user_id]
        return

    if mode == "gig":
        result = api_size_upgrade(service_username, amount)
    else:
        result = api_time_upgrade(service_username, amount)

    if not result or not result.get("ok"):
        error_msg = result.get("error") if result else "بدون پاسخ از API"
        await update.message.reply_text(f"⚠️ خطا در تمدید سرویس: {error_msg}")
        del PENDING_ACTIONS[user_id]
        return

    adjust_balance(user_id, -cost, reason=f"تمدید سرویس {service_username} ({mode})")

    # Refresh expiry locally if the panel returned an updated expiryTime
    new_expiry = result.get("result", {}).get("expiryTime") if isinstance(result.get("result"), dict) else None
    if new_expiry:
        update_service_expiry(service_username, new_expiry)

    del PENDING_ACTIONS[user_id]
    unit_label = "گیگ" if mode == "gig" else "روز"
    await update.message.reply_text(
        f"✅ سرویس با موفقیت تمدید شد ({amount} {unit_label}).\n💸 هزینه: {cost:,} تومان",
        reply_markup=main_menu_keyboard()
    )


# ============================================================
# ADMIN DECISION
# ============================================================
async def handle_topup_decision(query, context, data):
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("⛔ فقط ادمین می‌تونه این کار رو انجام بده.", show_alert=True)
        return

    action, _, request_id_str = data.rpartition("_")
    request_id = int(request_id_str)
    req = get_topup_request(request_id)

    if req is None:
        await query.edit_message_caption(caption="⚠️ درخواست یافت نشد.")
        return

    if req["status"] != "pending":
        await query.edit_message_caption(caption=f"این درخواست قبلاً پردازش شده (وضعیت: {req['status']}).")
        return

    if action == "topup_reject":
        update_topup_status(request_id, "rejected")
        await query.edit_message_caption(caption=f"❌ درخواست شارژ #{request_id} رد شد.")
        try:
            await context.bot.send_message(
                chat_id=req["user_id"],
                text=f"❌ متاسفانه رسید شارژ کیف پول شما ({req['amount']:,} تومان) تایید نشد."
            )
        except Exception as e:
            logger.error(f"Failed to notify user of topup rejection: {e}")
        return

    # action == "topup_approve"
    update_topup_status(request_id, "approved")
    new_balance = adjust_balance(
        req["user_id"], req["amount"], reason="شارژ کیف پول (تایید ادمین)", admin_id=query.from_user.id
    )
    await query.edit_message_caption(caption=f"✅ درخواست شارژ #{request_id} تایید شد.")
    try:
        await context.bot.send_message(
            chat_id=req["user_id"],
            text=f"✅ کیف پولت به مبلغ {req['amount']:,} تومان شارژ شد.\nموجودی فعلی: {new_balance:,} تومان"
        )
    except Exception as e:
        logger.error(f"Failed to notify user of topup approval: {e}")


async def handle_admin_decision(query, context, data):
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("⛔ فقط ادمین می‌تونه این کار رو انجام بده.", show_alert=True)
        return

    action, order_id_str = data.split("_")
    order_id = int(order_id_str)
    order = get_order(order_id)

    if order is None:
        await query.edit_message_caption(caption="⚠️ سفارش یافت نشد.")
        return

    if order["status"] != "pending":
        await query.edit_message_caption(caption=f"این سفارش قبلاً پردازش شده (وضعیت: {order['status']}).")
        return

    if action == "reject":
        update_order_status(order_id, "rejected")
        await query.edit_message_caption(caption=f"❌ سفارش #{order_id} رد شد.")
        try:
            await context.bot.send_message(
                chat_id=order["user_id"],
                text=f"❌ متاسفانه رسید سفارش شما (پلن {order['plan_name']}) تایید نشد. با پشتیبانی تماس بگیرید."
            )
        except Exception as e:
            logger.error(f"Failed to notify user of rejection: {e}")
        return

    # action == approve -> call panel API to create the service
    result = api_create_service(order["gig"], order["day"], test=0)

    if not result or not result.get("ok"):
        error_msg = result.get("error") if result else "بدون پاسخ از API"
        await query.edit_message_caption(caption=f"⚠️ خطا در ساخت سرویس: {error_msg}")
        return

    service_data = result.get("result", {})
    service_username = service_data.get("username")
    config_link = service_data.get("sub_link")
    expiry_time = service_data.get("expiryTime")

    update_order_status(order_id, "approved", service_username, config_link)
    add_service(order["user_id"], service_username, order["plan_name"], expiry_time=expiry_time)
    credit_referral_commission(order["user_id"], order["price"])

    await query.edit_message_caption(caption=f"✅ سفارش #{order_id} تایید و سرویس ساخته شد.")

    await deliver_service_to_user(context.bot, order["user_id"], order["plan_name"], service_username, service_data)


# ============================================================
# EXPIRY REMINDERS (runs periodically via JobQueue)
# ============================================================
async def check_expiring_services(context: ContextTypes.DEFAULT_TYPE):
    services = get_services_expiring_soon(within_seconds=24 * 3600)
    for s in services:
        try:
            await context.bot.send_message(
                chat_id=s["user_id"],
                text=(
                    f"⏰ یادآوری: سرویس «{s['plan_name']}» (یوزرنیم: {s['service_username']}) "
                    f"کمتر از ۲۴ ساعت دیگه منقضی می‌شه.\n"
                    f"برای تمدید از بخش «📦 سرویس‌های من» اقدام کن."
                )
            )
        except Exception as e:
            logger.error(f"Failed to send expiry reminder to {s['user_id']}: {e}")
        mark_service_notified(s["id"])


# ============================================================
# MAIN
# ============================================================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")
    if not API_KEY:
        raise RuntimeError("API_KEY environment variable is not set.")

    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.PHOTO, receipt_handler))

    reply_button_texts = [BTN_BUY, BTN_SERVICES, BTN_ACCOUNT, BTN_TOPUP, BTN_TRIAL, BTN_REFERRAL, BTN_HELP, BTN_ADMIN]
    app.add_handler(MessageHandler(filters.Text(reply_button_texts), reply_menu_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input_handler))

    if app.job_queue is not None:
        app.job_queue.run_repeating(check_expiring_services, interval=6 * 3600, first=60)
    else:
        logger.warning(
            "JobQueue not available — expiry reminders won't run. "
            "Install with: pip install \"python-telegram-bot[job-queue]\""
        )

    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
