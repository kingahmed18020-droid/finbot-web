"""بوت تيليجرام — حل منصة الثقافة المالية | النسخة v4.1
المميزات:
  • قائمة انتظار مع أولويات + إلغاء
  • لوحة أدمن متكاملة (إحصائيات + إدارة مستخدمين + حظر/فك حظر)
  • إشعارات تقدم مفصّلة (وحدة / نشاط / سؤال / اختبار)
  • دعم حسابات متعددة بالتوازي
"""
import asyncio
import html
import io
import logging
import os
import random
import threading
import time
from collections import deque
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

import core
import db
from verses import VERSES

BASE         = Path(__file__).resolve().parent
BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
DEV_USERNAME = os.getenv("DEV_USERNAME", "A7med19_7").lstrip("@")
MAX_JOBS     = int(os.getenv("MAX_JOBS", "2"))
MAX_QUEUE    = int(os.getenv("MAX_QUEUE", "10"))
JOB_TIMEOUT_MIN = int(os.getenv("JOB_TIMEOUT_MIN", "240"))
OWNER_CHAT_ID   = int(os.getenv("OWNER_CHAT_ID", "6954525749"))
ANSWERS_FILE = BASE / os.getenv("ANSWERS_FILE", "answers.txt")
ASSETS_DIR   = BASE / "assets"
WELCOME_IMAGES = sorted(ASSETS_DIR.glob("welcome_*.jpg"))
_FILE_IDS: dict = {}

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("finbot")

_AR_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def ar_num(n):
    return f"{n:,}".replace(",", "٬").translate(_AR_DIGITS)


def ar_num2(n):
    return ar_num(n).rjust(2, "٠")


def format_duration(seconds):
    seconds = max(0, int(seconds))
    h, r   = divmod(seconds, 3600)
    m, s   = divmod(r, 60)
    if h:
        return f"{ar_num(h)} س {ar_num2(m)} د {ar_num2(s)} ث"
    if m:
        return f"{ar_num(m)} د {ar_num2(s)} ث"
    return f"{ar_num(s)} ث"


def job_summary(questions, activities, elapsed):
    return (
        f"📝 الأسئلة المجابة: <b>{ar_num(questions)}</b>\n"
        f"📚 الأنشطة المكتملة: <b>{ar_num(activities)}</b>\n"
        f"⏱️ إجمالي الوقت: <b>{format_duration(elapsed)}</b>"
    )


# ── لوحة المفاتيح ──────────────────────────────────────────────────────────────

def btn(text, style=None, **kw):
    if style:
        try:
            return InlineKeyboardButton(text, style=style, **kw)
        except TypeError:
            pass
    return InlineKeyboardButton(text, **kw)


def main_menu(total):
    return InlineKeyboardMarkup([
        [btn("🚀 حل منصة الثقافة المالية", "success", callback_data="solve")],
        [
            btn("📖 طريقة الاستخدام", "primary", callback_data="help"),
            btn("🔒 الأمان والخصوصية",  "primary", callback_data="privacy"),
        ],
        [btn(f"🤍 اللهم صلِّ على سيدنا محمد ﷺ  •  {ar_num(total)}", "primary", callback_data="salat")],
        [btn(f"Dev: @{DEV_USERNAME}", url=f"https://t.me/{DEV_USERNAME}")],
    ])


def cancel_kb():
    return InlineKeyboardMarkup([[btn("❌ إلغاء", "danger", callback_data="cancel_flow")]])


def report_consent_kb():
    return InlineKeyboardMarkup([
        [btn("✅ موافق لهذه المهمة", "success", callback_data="report_consent_yes")],
        [btn("لا، بدون تقرير",        "primary", callback_data="report_consent_no")],
        [btn("❌ إلغاء",               "danger",  callback_data="cancel_flow")],
    ])


def stop_kb():
    return InlineKeyboardMarkup([[btn("⛔ إيقاف الحل", "danger", callback_data="stop")]])


def after_kb():
    return InlineKeyboardMarkup([
        [btn("🔁 تشغيل تاني", "success", callback_data="solve")],
        [btn("🏠 القائمة الرئيسية", "primary", callback_data="menu")],
    ])


# ── لوحة الأدمن ────────────────────────────────────────────────────────────────

ADMIN_MAIN_KB = InlineKeyboardMarkup([
    [btn("📊 الإحصائيات",         callback_data="adm_stats")],
    [btn("👥 قائمة المستخدمين",   callback_data="adm_users")],
    [btn("🔎 بحث بـ ID",          callback_data="adm_search")],
    [btn("🚫 حظر مستخدم",         callback_data="adm_ban_ask"),
     btn("✅ فك حظر",             callback_data="adm_unban_ask")],
    [btn("📢 إرسال رسالة جماعية", callback_data="adm_broadcast_ask")],
    [btn("❌ إغلاق",              callback_data="adm_close")],
])


def adm_user_kb(tg_id, is_banned_flag):
    action = ("✅ فك الحظر", f"adm_unban_{tg_id}") if is_banned_flag else ("🚫 حظر", f"adm_ban_{tg_id}")
    return InlineKeyboardMarkup([
        [btn(action[0], callback_data=action[1])],
        [btn("🔙 رجوع", callback_data="adm_users")],
    ])


# ── رسائل أساسية ───────────────────────────────────────────────────────────────

HELP_TEXT = (
    "📖 <b>طريقة الاستخدام</b>\n<i>أربع خطوات وخلاص</i>\n\n"
    "١) اضغط «حل منصة الثقافة المالية».\n"
    "٢) ابعت كود الدخول بتاعك (بيتحفظ عشان متكتبوش كل مرة).\n"
    "٣) ابعت الباسورد.\n"
    "٤) البوت يدخل حسابك ويحل الدروس بالترتيب، وبيبعتلك تقدّمه في رسالة واحدة بتتحدّث.\n\n"
    "• الدروس اللي كنت مخلصها قبل كده بيعدّي عليها.\n"
    "• تقدر توقف الحل في أي وقت بزر «إيقاف».\n"
    "• ممكن تقفل تيليجرام — الحل بيكمّل على السيرفر."
)

PRIVACY_TEXT = (
    "🔒 <b>الأمان والخصوصية</b>\n<i>شفافية كاملة</i>\n\n"
    "• <b>الكود</b> بيتحفظ عشان متكتبوش كل مرة.\n"
    "• <b>الباسورد</b> بيتستخدم مرة واحدة بس لتسجيل الدخول، ومبيتكتبش في أي ملف أو قاعدة بيانات، "
    "وبيتشال من الذاكرة لما الحل يخلص. والبوت بيمسح رسالتك اللي فيها الباسورد أول ما يستلمها.\n"
    "• قبل كل مهمة، تقدر توافق اختياريًا على إرسال تقرير عطل ولقطة شاشة منقّحة لمطوّر البوت."
)

QUEUE_FULL_TEXT = "🚦 البوت مشغول دلوقتي والطابور ممتلئ. جرّب تاني بعد شوية 🌿"
BANNED_TEXT     = "🚫 حسابك موقوف. تواصل مع المطوّر لو في خطأ."


async def send_welcome(chat, name):
    idx     = random.randrange(len(VERSES))
    v       = VERSES[idx]
    caption = (
        f"✦ ━━━━━━━━━━━━ ✦\n"
        f"<b>أهلاً بيك يا {html.escape(name)}</b>\n\n"
        f"<blockquote>{html.escape(v['shatr1'])}\n{html.escape(v['shatr2'])}\n"
        f"<i>{html.escape(v['poet'])}</i></blockquote>\n\n"
        "⏱️ وقتك أغلى من إنك تقعد تدوس «التالي» ساعات.\n"
        "اضغط الزر اللي تحت وسيب الباقي عليّ 👇\n\n"
        "🤍 <b>صلِّ على النبي ﷺ</b>"
    )
    markup = main_menu(db.get_salawat())
    image  = WELCOME_IMAGES[idx] if idx < len(WELCOME_IMAGES) else None
    if image:
        photo = _FILE_IDS.get(image.name) or image.read_bytes()
        try:
            sent = await chat.send_photo(photo, caption=caption, parse_mode=ParseMode.HTML, reply_markup=markup)
            _FILE_IDS[image.name] = sent.photo[-1].file_id
            return
        except TelegramError as exc:
            log.warning("welcome photo failed (%s) — fallback to text", exc)
            _FILE_IDS.pop(image.name, None)
    await chat.send_message(caption, parse_mode=ParseMode.HTML, reply_markup=markup)


# ── هاندلرز عامة ───────────────────────────────────────────────────────────────

async def on_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.touch_user(user.id, user.full_name)
    ctx.user_data.clear()
    if db.is_banned(user.id):
        await update.message.reply_text(BANNED_TEXT)
        return
    await send_welcome(update.effective_chat, user.first_name or "يا غالي")


async def on_cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("تمام، اتلغى. اكتب /start عشان ترجع للقائمة.")


async def on_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_html(HELP_TEXT)


async def on_privacy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_html(PRIVACY_TEXT)


async def on_help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(HELP_TEXT)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("unhandled error", exc_info=ctx.error)


async def on_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await send_welcome(q.message.chat, q.from_user.first_name or "يا غالي")


async def on_salat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    now = time.monotonic()
    if now - ctx.user_data.get("last_salat", 0) < 0.35:
        await q.answer()
        return
    ctx.user_data["last_salat"] = now
    total = db.add_salawat()
    await q.answer("اللهم صلِّ وسلِّم وبارك على سيدنا محمد ﷺ")
    try:
        await q.edit_message_reply_markup(main_menu(total))
    except (BadRequest, RetryAfter):
        pass


# ── تدفق الدخول ────────────────────────────────────────────────────────────────

async def on_solve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    await q.answer()
    db.touch_user(uid, q.from_user.full_name)
    if db.is_banned(uid):
        await q.message.reply_text(BANNED_TEXT)
        return
    if uid in ctx.application.bot_data["cancels"]:
        await q.message.reply_text("⏳ في حل شغّال لحسابك دلوقتي. استنى لما يخلص أو اضغط «إيقاف».")
        return
    if _queue_full(ctx.application):
        await q.message.reply_text(QUEUE_FULL_TEXT)
        return
    ctx.user_data["state"] = "await_report_consent"
    ctx.user_data.pop("report_consent", None)
    await q.message.reply_html(
        "🔎 <b>تقارير الأعطال اختيارية</b>\n\n"
        "لو وافقت لهذه المهمة، عند حدوث مشكلة فقط سأرسل لمطوّر البوت "
        f"(Telegram ID: <code>{OWNER_CHAT_ID}</code>) تقريرًا تقنيًا ولقطة شاشة.\n"
        "سأحجب الشريط العلوي وحقول الإدخال، لكن قد يظهر محتوى المنصة في الصورة. "
        "لن أرسل كود الدخول أو كلمة المرور. الرفض لا يمنع تشغيل الحل.",
        reply_markup=report_consent_kb(),
    )


async def _continue_solve_flow(message, ctx):
    uid   = message.chat.id
    saved = db.get_username(uid)
    if saved:
        kb = InlineKeyboardMarkup([
            [btn("✅ استخدم الكود ده", "success", callback_data="use_saved")],
            [btn("✏️ كود تاني",        "primary", callback_data="new_user")],
        ])
        await message.reply_html(f"عندي كود محفوظ لحسابك:\n<code>{html.escape(saved)}</code>\n\nتستخدمه؟", reply_markup=kb)
    else:
        await _ask_username(message, ctx)


async def on_report_consent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.message.chat.type != "private" or q.from_user.id != q.message.chat.id:
        return
    ctx.user_data.pop("state", None)
    ctx.user_data["report_consent"] = q.data == "report_consent_yes"
    await q.message.reply_text(
        "✅ هنبعت تقرير منقّح عند حدوث مشكلة فقط."
        if ctx.user_data["report_consent"] else
        "تمام، مش هنبعت أي تقرير أو لقطة شاشة."
    )
    await _continue_solve_flow(q.message, ctx)


async def _ask_username(message, ctx):
    ctx.user_data["state"] = "await_user"
    await message.reply_html("ابعتلي <b>كود الدخول</b> (Login ID) بتاع حسابك على المنصة 👇", reply_markup=cancel_kb())


async def _ask_password(message, ctx, username):
    ctx.user_data["state"]    = "await_pass"
    ctx.user_data["username"] = username
    await message.reply_html(
        f"تمام ✅ الكود: <code>{html.escape(username)}</code>\n\n"
        "دلوقتي ابعت <b>الباسورد</b> 🔑\n"
        "<i>هتتمسح رسالتك أول ما أستلمها.</i>",
        reply_markup=cancel_kb(),
    )


async def on_new_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await _ask_username(q.message, ctx)


async def on_use_saved(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    saved = db.get_username(q.from_user.id)
    if not saved:
        await _ask_username(q.message, ctx)
        return
    await _ask_password(q.message, ctx, saved)


async def on_cancel_flow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    for k in ("state", "username", "report_consent"):
        ctx.user_data.pop(k, None)
    await q.answer("اتلغى")
    try:
        await q.message.delete()
    except TelegramError:
        pass


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = ctx.user_data.get("state")
    text  = (update.message.text or "").strip()
    uid   = update.effective_user.id

    # ── أدمن: انتظار ID للبحث / حظر / فك حظر / بث ──────────────────────────
    adm_state = ctx.user_data.get("adm_state")
    if adm_state and uid == OWNER_CHAT_ID:
        await _handle_adm_text(update, ctx, adm_state, text)
        return

    if state == "await_user":
        if not (2 <= len(text) <= 64) or any(c.isspace() for c in text):
            await update.message.reply_text("الكود شكله مش مظبوط (من غير مسافات). ابعته تاني.")
            return
        db.save_username(uid, text)
        await _ask_password(update.message, ctx, text)

    elif state == "await_pass":
        password = update.message.text or ""
        try:
            await update.message.delete()
        except TelegramError:
            pass
        username = ctx.user_data.pop("username", None) or db.get_username(uid)
        ctx.user_data.pop("state", None)
        if not username or not password.strip():
            await update.effective_chat.send_message("حصلت مشكلة، ابدأ من جديد بـ /start")
            return
        ctx.application.create_task(
            run_student_job(
                ctx.application,
                update.effective_chat.id,
                uid,
                username,
                password,
                ctx.user_data.pop("report_consent", False),
            ),
            update=update,
        )
        password = None

    elif state == "await_report_consent":
        await update.message.reply_text("اختار من الأزرار قبل المتابعة.", reply_markup=report_consent_kb())

    else:
        await update.message.reply_text("اكتب /start عشان تفتح القائمة 🌿")


# ── Progress مع تقدم مفصّل ─────────────────────────────────────────────────────

class Progress:
    def __init__(self, bot, chat_id, message_id):
        self.bot        = bot
        self.chat_id    = chat_id
        self.message_id = message_id
        self.lines      = deque(maxlen=12)
        self.completed  = set()
        self.questions  = 0
        self.activities = 0
        self.header     = "⚙️ <b>جاري حل المنصة...</b>"
        self.show_stop  = True
        self.dirty      = True
        self.t0         = time.monotonic()
        # تفاصيل إضافية
        self.current_unit     = ""
        self.current_activity = ""
        self.quiz_solving     = ""

    def handle(self, kind, text):
        e = html.escape(text or "")

        if kind == "q_ok":
            self.questions += 1
            self.dirty = True

        elif kind == "open":
            self.current_activity = text or ""
            self.lines.append(f"▶️ جاري حل: <b>{e}</b>")
            self.dirty = True

        elif kind == "quiz_solving":
            self.quiz_solving = text or ""
            self.lines.append(f"📝 يحل اختبار: <b>{e}</b>")
            self.dirty = True

        elif kind in ("done", "quiz_done"):
            normalized = core.normalize_text(text or "")
            if not normalized or normalized in self.completed:
                return
            self.completed.add(normalized)
            self.activities += 1
            label = "اختبار" if kind == "quiz_done" else "نشاط"
            self.lines.append(f"✅ اكتمل {label}: <b>{e}</b>")
            if self.quiz_solving and kind == "quiz_done":
                self.quiz_solving = ""
            self.dirty = True

        elif kind == "group_done":
            normalized = core.normalize_text(text or "")
            if not normalized or normalized in self.completed:
                return
            self.completed.add(normalized)
            self.lines.append(f"🏆 اكتملت الوحدة: <b>{e}</b>")
            self.dirty = True

        elif kind == "unit_open":
            self.current_unit = text or ""
            self.lines.append(f"📂 وحدة جديدة: <b>{e}</b>")
            self.dirty = True

        elif kind in ("plan_done", "group_skipped"):
            normalized = core.normalize_text(text.split("|")[0] if "|" in text else text)
            if not normalized or normalized in self.completed:
                return
            self.completed.add(normalized)
            name = e.split("|")[0] if "|" in e else e
            self.lines.append(f"✅ مكتملة مسبقًا: <b>{name}</b>")
            self.dirty = True

        elif kind == "plan_todo":
            parts = e.split("|")
            name  = parts[0]
            done  = parts[1] if len(parts) > 1 else "?"
            total = parts[2] if len(parts) > 2 else "?"
            self.lines.append(f"📌 متبقي: <b>{name}</b> ({done}/{total} عنصر)")
            self.dirty = True

        elif kind == "plan_locked":
            self.lines.append(f"🔒 مقفولة: <b>{e}</b>")
            self.dirty = True

        elif kind == "plan_tests":
            parts  = e.split("|")
            count  = parts[0]
            names  = parts[1] if len(parts) > 1 else ""
            self.lines.append(f"📝 اختبارات متبقية ({count}): {names}")
            self.dirty = True

        elif kind == "quiz_skipped":
            self.lines.append(f"⚠️ اختبار بدون إجابات: <b>{e}</b>")
            self.dirty = True

        elif kind == "login_ok":
            self.lines.append("🔐 تم تسجيل الدخول ✅")
            self.dirty = True

    def render(self):
        body = "\n".join(self.lines) or "جاري التجهيز…"
        status_line = ""
        if self.quiz_solving:
            status_line = f"\n🧩 يحل اختبار: <b>{html.escape(self.quiz_solving[:60])}</b>"
        elif self.current_activity:
            status_line = f"\n▶️ النشاط الحالي: <b>{html.escape(self.current_activity[:60])}</b>"
        return (
            f"{self.header}\n"
            "━━━━━━━━━━━━━━\n"
            f"📚 الأنشطة المكتملة: <b>{ar_num(self.activities)}</b>\n"
            f"📝 الأسئلة المجابة: <b>{ar_num(self.questions)}</b>\n"
            f"⏱️ الوقت المنقضي: <b>{format_duration(time.monotonic() - self.t0)}</b>"
            f"{status_line}\n"
            "━━━━━━━━━━━━━━\n"
            f"📌 <b>آخر التحديثات</b>\n{body}"
        )

    async def flush(self):
        self.dirty = False
        try:
            await self.bot.edit_message_text(
                self.render(),
                chat_id=self.chat_id,
                message_id=self.message_id,
                parse_mode=ParseMode.HTML,
                reply_markup=stop_kb() if self.show_stop else None,
            )
        except RetryAfter as exc:
            self.dirty = True
            await asyncio.sleep(exc.retry_after + 1)
        except BadRequest:
            pass
        except TelegramError as exc:
            log.warning("progress edit failed: %s", exc)


async def _ticker(progress, stop):
    tick = 0
    while not stop.is_set():
        tick += 1
        if tick % 10 == 0:
            progress.dirty = True
        try:
            await asyncio.wait_for(stop.wait(), timeout=3)
        except asyncio.TimeoutError:
            pass
        if progress.dirty:
            await progress.flush()


# ── Diagnostic ─────────────────────────────────────────────────────────────────

async def _send_owner_diagnostic(app, report):
    details = (
        f"🚨 تقرير عطل #{report['id']}\n"
        f"الحالة: {report['status']}\n"
        f"الوقت: {report['timestamp']}\n"
        f"الموقع: {report['page']}\n"
        f"النشاط: {report['activity']}\n"
        f"التشخيص: {report['error']}"
    )[:4000]
    try:
        await app.bot.send_message(chat_id=OWNER_CHAT_ID, text=details)
    except TelegramError as exc:
        log.warning("owner diagnostic message failed (%s)", type(exc).__name__)
    screenshot = report.get("screenshot")
    if screenshot:
        image      = io.BytesIO(screenshot)
        image.name = f"diagnostic-{report['id']}.png"
        try:
            await app.bot.send_photo(
                chat_id=OWNER_CHAT_ID,
                photo=image,
                caption="لقطة منقّحة: الشريط العلوي وحقول الإدخال محجوبة.",
            )
        except TelegramError as exc:
            log.warning("owner diagnostic screenshot failed (%s)", type(exc).__name__)


# ── تشغيل المهمة ───────────────────────────────────────────────────────────────

def _queue_full(app):
    data = app.bot_data
    return data["sem"].locked() and data["waiting"] >= MAX_QUEUE


async def run_student_job(app, chat_id, uid, username, password, report_consent=False):
    data = app.bot_data
    if uid in data["cancels"]:
        return
    if _queue_full(app):
        password = None
        await app.bot.send_message(chat_id, QUEUE_FULL_TEXT, reply_markup=after_kb())
        return

    cancel    = threading.Event()
    timed_out = threading.Event()
    data["cancels"][uid] = cancel
    started = time.time()
    loop    = asyncio.get_running_loop()

    # — رسالة بداية التقدم —
    msg      = await app.bot.send_message(chat_id, "⏳ جاري التجهيز...", reply_markup=stop_kb())
    progress = Progress(app.bot, chat_id, msg.message_id)
    stop_ev  = asyncio.Event()
    tick_t   = asyncio.create_task(_ticker(progress, stop_ev))
    timer    = None
    result   = {"status": "error", "error": "unknown"}

    def hook(kind, text):
        if kind == "diagnostic":
            loop.call_soon_threadsafe(lambda: app.create_task(_send_owner_diagnostic(app, text)))
            return
        if kind == "debug":
            safe = str(text)
            for secret in (password, username):
                if secret:
                    safe = safe.replace(secret, "***")
            log.info("solver uid=%s: %s", uid, safe[:600])
            return
        loop.call_soon_threadsafe(progress.handle, kind, text)

    def on_timeout():
        timed_out.set()
        cancel.set()

    try:
        sem    = data["sem"]
        queued = sem.locked()
        if queued:
            data["waiting"] += 1
            pos = data["waiting"]
            progress.lines.append(f"🕒 في طابور انتظار (ترتيبك {ar_num(pos)})… هبدأ أول ما يفضى مكان")
            progress.dirty = True
        try:
            await sem.acquire()
        finally:
            if queued:
                data["waiting"] -= 1

        if cancel.is_set():
            result = {"status": "stopped"}
        else:
            progress.lines.append("🔐 بسجّل الدخول…")
            progress.dirty = True
            timer  = loop.call_later(JOB_TIMEOUT_MIN * 60, on_timeout)
            result = await asyncio.to_thread(
                core.run_job,
                username,
                password,
                data["entries"],
                hook,
                cancel,
                True,
                report_consent,
            )
        sem.release()

    except Exception:
        log.exception("job crashed uid=%s", uid)
        result = {"status": "error", "error": "حصل خطأ غير متوقع في البوت."}
        try:
            data["sem"].release()
        except Exception:
            pass
    finally:
        password = None
        if timer:
            timer.cancel()
        data["cancels"].pop(uid, None)

    status  = result.get("status")
    if timed_out.is_set():
        status = "timeout"
    last    = result.get("last") or "—"
    elapsed = time.monotonic() - progress.t0
    summary = job_summary(progress.questions, progress.activities, elapsed)

    if status == "finished":
        progress.header = "🎉 <b>اكتملت الأنشطة المتاحة</b>"
        final = (
            "╭━━━━━━━━━━━━━━━━━━╮\n"
            "🎉 <b>اكتملت المهمة بنجاح</b>\n"
            "╰━━━━━━━━━━━━━━━━━━╯\n\n"
            "<i>لم يتبق نشاط مفتوح متاح للمتابعة وقت التشغيل.</i>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{summary}\n"
            f"📍 آخر نشاط: <b>{html.escape(last)}</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<i>قد تظل أنشطة مقفولة بانتظار فتحها من المنصة.</i>"
        )
    elif status == "tests_pending":
        progress.header = "📝 <b>وقفت عند اختبار — مفيش إجابات مطابقة</b>"
        test_name = html.escape((result.get("test") or "")[:120])
        final = (
            f"📝 <b>وصلت لاختبار مش موجود في بنك الإجابات:</b>\n"
            f"{('• ' + test_name) if test_name else ''}\n\n{summary}\n\n"
            "حلّه بنفسك، وبعدين دوس «حل الثقافة المالية» تاني والبوت يكمّل اللي بعده."
        )
    elif status == "login_failed":
        progress.header = "❌ <b>فشل تسجيل الدخول</b>"
        final = "❌ الكود أو الباسورد غلط، أو الحساب مش متاح دلوقتي.\nراجع بياناتك وحاول تاني."
    elif status == "stopped":
        progress.header = "⛔ <b>اتوقّف الحل</b>"
        final = f"⛔ وقفت الحل.\n\n{summary}"
    elif status == "timeout":
        progress.header = "⌛ <b>خلصت المدة القصوى</b>"
        final = f"⌛ الحل خد وقت أطول من المسموح فوقفته.\n\n{summary}\nتقدر تشغّله تاني وهيكمّل من حيث وقف."
    else:
        progress.header = "⚠️ <b>الحل وقف بسبب مشكلة</b>"
        err   = html.escape((result.get("error") or "")[:500])
        final = f"⚠️ الحل وقف بسبب مشكلة:\n<code>{err}</code>\n\n{summary}\nآخر نشاط: {html.escape(last)}"
        log.warning("job error uid=%s: %s", uid, result.get("error"))

    stop_ev.set()
    await tick_t
    progress.show_stop = False
    await progress.flush()
    await app.bot.send_message(chat_id, final, parse_mode=ParseMode.HTML, reply_markup=after_kb())
    db.log_job(uid, started, status, progress.questions, progress.activities, result.get("error", "") or "")


async def on_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q     = update.callback_query
    event = ctx.application.bot_data["cancels"].get(q.from_user.id)
    if event:
        event.set()
        await q.answer("هوقف بعد الخطوة الحالية…")
    else:
        await q.answer("مفيش حل شغّال.")


# ── لوحة الأدمن ────────────────────────────────────────────────────────────────

def _only_owner(func):
    """ديكوراتور: يرد فقط للأونر."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = (update.effective_user or update.callback_query.from_user).id
        if uid != OWNER_CHAT_ID:
            if update.callback_query:
                await update.callback_query.answer("⛔ مش مسموح.")
            return
        return await func(update, ctx)
    return wrapper


@_only_owner
async def on_admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🛠️ لوحة الأدمن", reply_markup=ADMIN_MAIN_KB)


@_only_owner
async def on_adm_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = db.get_stats()

    recent = ""
    for j in s["recent_jobs"]:
        name    = html.escape((j["tg_name"] or str(j["tg_id"]))[:20])
        status  = j["status"]
        elapsed = ""
        if j["started"] and j["finished"]:
            elapsed = format_duration(j["finished"] - j["started"])
        recent += f"  • {name} | {status} | {j['questions']}س {j['activities']}ن | {elapsed}\n"

    text = (
        "📊 <b>إحصائيات البوت</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"👥 إجمالي المستخدمين: <b>{ar_num(s['total_users'])}</b>\n"
        f"🟢 نشطون اليوم: <b>{ar_num(s['active_today'])}</b>\n"
        f"🚫 محظورون: <b>{ar_num(s['banned_users'])}</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🔢 إجمالي المهام: <b>{ar_num(s['total_jobs'])}</b>\n"
        f"✅ مكتملة: <b>{ar_num(s['done_jobs'])}</b>\n"
        f"📝 أسئلة محلولة: <b>{ar_num(s['total_q'])}</b>\n"
        f"📚 أنشطة منجزة: <b>{ar_num(s['total_a'])}</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"<b>آخر 10 مهام:</b>\n{recent or 'لا يوجد'}"
    )
    await q.message.reply_html(text, reply_markup=InlineKeyboardMarkup([[btn("🔙 رجوع", callback_data="adm_main")]]))


@_only_owner
async def on_adm_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    users = db.get_all_users(limit=30)
    lines = []
    for u in users:
        flag = "🚫" if u["is_banned"] else "✅"
        name = html.escape((u["tg_name"] or str(u["tg_id"]))[:20])
        code = html.escape(u["username"] or "-")
        lines.append(f"{flag} <code>{u['tg_id']}</code> | {name} | {code}")
    text = "👥 <b>آخر 30 مستخدم</b>\n━━━━━━━━━━━━━━\n" + "\n".join(lines or ["لا يوجد مستخدمين"])
    await q.message.reply_html(text, reply_markup=InlineKeyboardMarkup([[btn("🔙 رجوع", callback_data="adm_main")]]))


@_only_owner
async def on_adm_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["adm_state"] = "search_id"
    await q.message.reply_text("🔎 ابعت الـ Telegram ID اللي عايز تشوفه:")


@_only_owner
async def on_adm_ban_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["adm_state"] = "ban_id"
    await q.message.reply_text("🚫 ابعت الـ Telegram ID اللي عايز تحظره:")


@_only_owner
async def on_adm_unban_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["adm_state"] = "unban_id"
    await q.message.reply_text("✅ ابعت الـ Telegram ID اللي عايز تفك حظره:")


@_only_owner
async def on_adm_broadcast_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["adm_state"] = "broadcast_msg"
    await q.message.reply_text("📢 ابعت الرسالة اللي عايز تبعتها لكل المستخدمين:")


@_only_owner
async def on_adm_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        await q.message.delete()
    except TelegramError:
        pass


@_only_owner
async def on_adm_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("🛠️ لوحة الأدمن", reply_markup=ADMIN_MAIN_KB)


@_only_owner
async def on_adm_ban_direct(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """حظر مباشر من زر في قائمة المستخدم: adm_ban_{id}"""
    q      = update.callback_query
    tg_id  = int(q.data.split("_")[-1])
    db.ban_user(tg_id)
    await q.answer(f"تم حظر {tg_id}")
    await q.message.reply_text(f"🚫 تم حظر المستخدم <code>{tg_id}</code>.", parse_mode=ParseMode.HTML)


@_only_owner
async def on_adm_unban_direct(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """فك حظر مباشر: adm_unban_{id}"""
    q      = update.callback_query
    tg_id  = int(q.data.split("_")[-1])
    db.unban_user(tg_id)
    await q.answer(f"تم فك حظر {tg_id}")
    await q.message.reply_text(f"✅ تم فك حظر المستخدم <code>{tg_id}</code>.", parse_mode=ParseMode.HTML)


async def _handle_adm_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, adm_state: str, text: str):
    ctx.user_data.pop("adm_state", None)

    if adm_state == "search_id":
        try:
            tg_id = int(text)
        except ValueError:
            await update.message.reply_text("ID غلط. لازم يكون رقم.")
            return
        users = db.get_all_users(limit=1000)
        u     = next((x for x in users if x["tg_id"] == tg_id), None)
        if not u:
            await update.message.reply_text(f"مش لاقي {tg_id} في قاعدة البيانات.")
            return
        flag    = "🚫 محظور" if u["is_banned"] else "✅ نشط"
        name    = html.escape(u["tg_name"] or "-")
        code    = html.escape(u["username"] or "-")
        fs      = time.strftime("%Y-%m-%d", time.localtime(u["first_seen"])) if u["first_seen"] else "-"
        ls      = time.strftime("%Y-%m-%d", time.localtime(u["last_seen"]))  if u["last_seen"]  else "-"
        text_out = (
            f"👤 <code>{tg_id}</code>\n"
            f"الاسم: {name}\nالكود: <code>{code}</code>\n"
            f"الحالة: {flag}\nأول دخول: {fs}\nآخر نشاط: {ls}"
        )
        await update.message.reply_html(text_out, reply_markup=adm_user_kb(tg_id, u["is_banned"]))

    elif adm_state == "ban_id":
        try:
            tg_id = int(text)
        except ValueError:
            await update.message.reply_text("ID غلط.")
            return
        db.ban_user(tg_id)
        await update.message.reply_html(f"🚫 تم حظر <code>{tg_id}</code>.")

    elif adm_state == "unban_id":
        try:
            tg_id = int(text)
        except ValueError:
            await update.message.reply_text("ID غلط.")
            return
        db.unban_user(tg_id)
        await update.message.reply_html(f"✅ تم فك حظر <code>{tg_id}</code>.")

    elif adm_state == "broadcast_msg":
        users = db.get_all_users(limit=10000)
        sent  = 0
        fail  = 0
        for u in users:
            if u["is_banned"]:
                continue
            try:
                await update.effective_chat._bot.send_message(
                    u["tg_id"], f"📢 رسالة من الأدمن:\n\n{text}", parse_mode=ParseMode.HTML
                )
                sent += 1
                await asyncio.sleep(0.05)
            except TelegramError:
                fail += 1
        await update.message.reply_text(
            f"📢 تم الإرسال\n✅ ناجح: {ar_num(sent)}\n❌ فاشل: {ar_num(fail)}"
        )


# ── post_init + build ───────────────────────────────────────────────────────────

async def post_init(app: Application):
    db.init(os.getenv("DB_PATH", str(BASE / "bot.db")))
    app.bot_data["sem"]     = asyncio.Semaphore(MAX_JOBS)
    app.bot_data["cancels"] = {}
    app.bot_data["waiting"] = 0
    app.bot_data["entries"] = core.parse_answer_file(ANSWERS_FILE)
    log.info("loaded %d answers", len(app.bot_data["entries"]))


def build_app():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )
    private = filters.ChatType.PRIVATE

    # ── أوامر ──
    app.add_handler(CommandHandler("start",  on_start,     filters=private))
    app.add_handler(CommandHandler("menu",   on_start,     filters=private))
    app.add_handler(CommandHandler("cancel", on_cancel_cmd, filters=private))
    app.add_handler(CommandHandler("help",   on_help_cmd,   filters=private))
    app.add_handler(CommandHandler("admin",  on_admin_cmd,  filters=private))

    # ── ردود الأزرار ──
    btn_map = [
        (r"^solve$",                  on_solve),
        (r"^use_saved$",              on_use_saved),
        (r"^new_user$",               on_new_user),
        (r"^cancel_flow$",            on_cancel_flow),
        (r"^report_consent_(yes|no)$",on_report_consent),
        (r"^stop$",                   on_stop),
        (r"^help$",                   on_help),
        (r"^privacy$",                on_privacy),
        (r"^salat$",                  on_salat),
        (r"^menu$",                   on_menu),
        # أدمن
        (r"^adm_stats$",              on_adm_stats),
        (r"^adm_users$",              on_adm_users),
        (r"^adm_search$",             on_adm_search),
        (r"^adm_ban_ask$",            on_adm_ban_ask),
        (r"^adm_unban_ask$",          on_adm_unban_ask),
        (r"^adm_broadcast_ask$",      on_adm_broadcast_ask),
        (r"^adm_close$",              on_adm_close),
        (r"^adm_main$",               on_adm_main),
        (r"^adm_ban_\d+$",            on_adm_ban_direct),
        (r"^adm_unban_\d+$",          on_adm_unban_direct),
    ]
    for pattern, handler in btn_map:
        app.add_handler(CallbackQueryHandler(handler, pattern=pattern))

    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("حط BOT_TOKEN في ملف .env")
    build_app().run_polling()
