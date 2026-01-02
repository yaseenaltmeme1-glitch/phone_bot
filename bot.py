# bot.py — دليل أرقام المستشفى (بالعربي) + بصمة إنكليزية + احصائيات احترافية (Admin فقط)
import os, logging, asyncio, math, re, sqlite3
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
    BAGHDAD_TZ = ZoneInfo("Asia/Baghdad")
except Exception:
    BAGHDAD_TZ = None

from openpyxl import load_workbook
from telegram import (
    Update,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.error import RetryAfter

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", BASE)

# ==================== بصمتك ====================
SIGNATURE  = "\n────────────\nSource: CCTV – Yaseen Al-Tamimi"

# نص "عن البوت"
ABOUT_TEXT = (
    "ℹ️ عن البوت\n"
    "بوت دليل أرقام المستشفى، يوفّر بحث ذكي ويعرض النتائج بشكل مبسط وسريع.\n\n"
    "📩 لمزيد من الاستفسارات أو مقترحات التعديل:\n"
    "@ya_se91\n\n"
    "────────────\n"
    "Source: CCTV – Yaseen Al-Tamimi"
)

# ============= Admin Stats Settings =============
ADMIN_ID = 8099482759  # 👑 فقط هذا الـID يطلع احصائيات
DB_PATH = os.path.join(BASE, "stats.db")

# أسماء أعمدة محتملة
DEPT_CANDIDATES  = ["القسم","قسم","الاسم","اسم القسم"]
PHONE_CANDIDATES = ["رقم الهاتف","الهاتف","رقم","موبايل","Phone"]

# ذاكرة
display_rows: List[Tuple[str, str]] = []
departments:  List[str] = []
phonebook:    Dict[str, str] = {}

# كيبورد رئيسية
MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📞 أرقام المستشفى")],
        [KeyboardButton("🔍 بحث بالاسم")],
        [KeyboardButton("ℹ️ عن البوت")],
        [KeyboardButton("◀️ رجوع للقائمة")]
    ],
    resize_keyboard=True
)

# إعداد الشبكات
GRID_COLS      = 3
PAGE_SIZE_ALL  = 24
PAGE_SIZE_SRCH = 21

# ---------------- تطبيع ----------------
ARABIC_DIAC = re.compile(r"[ًٌٍَُِّْـ]")

def strip_diacritics(s: str) -> str:
    return ARABIC_DIAC.sub("", s or "")

def normalize_arabic(s: str) -> str:
    s = str(s or "")
    s = s.replace("\u200f","").replace("\u200e","").replace("\ufeff","").strip()
    s = strip_diacritics(s)
    s = s.replace("آ","ا").replace("أ","ا").replace("إ","ا")
    s = s.replace("ى","ي").replace("ة","ه")
    s = re.sub(r"[^\w\s\u0600-\u06FF]"," ", s)
    s = re.sub(r"\s+"," ", s).strip()
    return s.upper()

# ---------------- وقت بغداد ----------------
def now_baghdad() -> datetime:
    if BAGHDAD_TZ:
        return datetime.now(BAGHDAD_TZ)
    return datetime.utcnow() + timedelta(hours=3)

def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()

def period_bounds(kind: str) -> Tuple[Optional[datetime], Optional[datetime], str]:
    """returns (start, end, title). if start/end None => all-time"""
    now = now_baghdad()
    if kind == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now, "📊 إحصائيات اليوم"
    if kind == "week":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
        return start, now, "📅 إحصائيات هذا الأسبوع"
    if kind == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, now, "🗓️ إحصائيات هذا الشهر"
    if kind == "7":
        return now - timedelta(days=7), now, "📆 آخر 7 أيام"
    if kind == "30":
        return now - timedelta(days=30), now, "📆 آخر 30 يوم"
    if kind == "90":
        return now - timedelta(days=90), now, "📆 آخر 90 يوم"
    return None, None, "♾️ إحصائيات من البداية"

# ---------------- DB (SQLite) ----------------
def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_seen TEXT NOT NULL,
            last_seen  TEXT NOT NULL,
            username   TEXT,
            full_name  TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            chat_id INTEGER,
            event_type TEXT NOT NULL,
            dept TEXT,
            query TEXT,
            extra TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_dept ON events(dept)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id)")
    conn.commit()
    conn.close()

def upsert_user(user) -> None:
    if not user:
        return
    uid = user.id
    username = user.username or ""
    full_name = (user.full_name or "").strip()
    t = iso(now_baghdad())
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id=?", (uid,))
    row = cur.fetchone()
    if row:
        cur.execute(
            "UPDATE users SET last_seen=?, username=?, full_name=? WHERE user_id=?",
            (t, username, full_name, uid)
        )
    else:
        cur.execute(
            "INSERT INTO users(user_id, first_seen, last_seen, username, full_name) VALUES(?,?,?,?,?)",
            (uid, t, t, username, full_name)
        )
    conn.commit()
    conn.close()

def log_event(event_type: str, user_id: int, chat_id: Optional[int], dept: str = "", query: str = "", extra: str = "") -> None:
    t = iso(now_baghdad())
    conn = db_conn()
    conn.execute(
        "INSERT INTO events(ts, user_id, chat_id, event_type, dept, query, extra) VALUES(?,?,?,?,?,?,?)",
        (t, user_id, chat_id if chat_id is not None else None, event_type, dept or "", query or "", extra or "")
    )
    conn.commit()
    conn.close()

def is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == ADMIN_ID)

# ---------------- قراءة الإكسل ----------------
def list_excel_files(folder: str) -> List[str]:
    try:
        return [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(".xlsx")]
    except:
        return []

def read_headers(ws) -> List[str]:
    for row in ws.iter_rows(min_row=1, max_row=1, values_only=True):
        return [str(c or "").strip() for c in row]
    return []

def find_col_idx(headers: List[str], candidates: List[str]) -> Optional[int]:
    H = [normalize_arabic(h) for h in headers]
    C = [normalize_arabic(c) for c in candidates]
    for i,h in enumerate(H):
        if h in C: return i
    for i,h in enumerate(H):
        for c in C:
            if c in h: return i
    return None

def load_phonebook() -> Tuple[int,str]:
    global display_rows, departments, phonebook
    display_rows, departments, phonebook = [], [], {}
    files = list_excel_files(DATA_DIR)
    if not files:
        return 0, f"❌ ماكو ملفات ‎.xlsx داخل: {DATA_DIR}"
    total = 0
    for path in files:
        try:
            wb = load_workbook(path, read_only=True, data_only=True)
            ws = wb.active
            headers = read_headers(ws)
            if not headers: 
                wb.close(); 
                continue
            di = find_col_idx(headers, DEPT_CANDIDATES)
            pi = find_col_idx(headers, PHONE_CANDIDATES)
            if di is None or pi is None: 
                wb.close(); 
                continue
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row: 
                    continue
                dept  = str(row[di] if di < len(row) and row[di] is not None else "").strip()
                phone = str(row[pi] if pi < len(row) and row[pi] is not None else "").strip()
                if not dept: 
                    continue
                display_rows.append((dept, phone))
                phonebook[normalize_arabic(dept)] = phone
                total += 1
            wb.close()
        except Exception as e:
            logging.exception(f"Load error in {path}: {e}")
    display_rows.sort(key=lambda x: x[0])
    departments = [d for d,_ in display_rows]
    return total, (f"✅ تم تحميل {total} سجل." if total else "❌ لم يتم تحميل أي سجل.")

# ---------------- أدوات إرسال ----------------
async def safe_reply(update: Update, text: str, reply_markup=None):
    text = f"{text}{SIGNATURE}"
    try:
        return await update.message.reply_text(text, reply_markup=reply_markup)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        return await update.message.reply_text(text, reply_markup=reply_markup)

async def reply_plain(msg, text: str, reply_markup=None):
    text = f"{text}{SIGNATURE}"
    try:
        return await msg.reply_text(text, reply_markup=reply_markup)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        return await msg.reply_text(text, reply_markup=reply_markup)

async def safe_edit(q, text: str, reply_markup=None):
    try:
        return await q.message.edit_text(text, reply_markup=reply_markup)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        return await q.message.edit_text(text, reply_markup=reply_markup)

# ---------------- الانترو ----------------
def build_intro() -> str:
    return (
        "👋 أهلاً بك في بوت أرقام المستشفى.\n\n"
        "📌 طريقة الاستخدام:\n"
        "• 📞 أرقام المستشفى: تصفّح الأقسام كمربعات.\n"
        "• 🔍 بحث بالاسم: اكتب أي جزء من اسم القسم.\n"
        "• ℹ️ عن البوت: معلومات عن البوت.\n"
        "• ◀️ رجوع: العودة إلى هذه القائمة.\n\n"
        "────────────\n"
        "Source: CCTV – Yaseen Al-Tamimi"
    )

# ---------------- الشبكات ----------------
def build_grid(indices: List[int], page: int, page_size: int, cols: int, mode: str) -> InlineKeyboardMarkup:
    total = len(indices)
    pages = max(1, math.ceil(total / page_size))
    page  = max(0, min(page, pages-1))
    start, end = page*page_size, min(page*page_size + page_size, total)
    slice_idx = indices[start:end]

    rows, row = [], []
    for idx in slice_idx:
        name = departments[idx]
        row.append(InlineKeyboardButton(name, callback_data=f"dept:{idx}"))
        if len(row) == cols:
            rows.append(row); row = []
    if row: rows.append(row)

    if pages > 1:
        ctrl = []
        if page > 0:             ctrl.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"{mode}:{page-1}"))
        ctrl.append(InlineKeyboardButton(f"صفحة {page+1}/{pages}", callback_data="noop"))
        if page < pages-1:       ctrl.append(InlineKeyboardButton("التالي ➡️", callback_data=f"{mode}:{page+1}"))
        rows.append(ctrl)
    rows.append([InlineKeyboardButton("◀️ رجوع للقائمة", callback_data="home")])
    return InlineKeyboardMarkup(rows)

def grid_all(page:int=0) -> InlineKeyboardMarkup:
    return build_grid(list(range(len(departments))), page, PAGE_SIZE_ALL, GRID_COLS, "allp")

def grid_search(matches: List[int], page:int=0) -> InlineKeyboardMarkup:
    return build_grid(matches, page, PAGE_SIZE_SRCH, GRID_COLS, "srchp")

# ---------------- البحث ----------------
def search_indices(query: str) -> List[int]:
    qn = normalize_arabic(query)
    if not qn: return []
    matches = []
    for i, name in enumerate(departments):
        if qn in normalize_arabic(name):
            matches.append(i)
    return matches

# ---------------- Admin لوحة الاحصائيات ----------------
def admin_menu() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("📊 اليوم",  callback_data="stats:today"),
            InlineKeyboardButton("📅 أسبوع", callback_data="stats:week"),
            InlineKeyboardButton("🗓️ شهر",  callback_data="stats:month"),
        ],
        [
            InlineKeyboardButton("📆 آخر 7",  callback_data="stats:7"),
            InlineKeyboardButton("📆 آخر 30", callback_data="stats:30"),
            InlineKeyboardButton("📆 آخر 90", callback_data="stats:90"),
        ],
        [
            InlineKeyboardButton("♾️ من البداية", callback_data="stats:all"),
        ],
        [
            InlineKeyboardButton("🏆 Top 15 أقسام (اليوم)", callback_data="top:today"),
            InlineKeyboardButton("🏆 Top 15 أقسام (أسبوع)", callback_data="top:week"),
        ],
        [
            InlineKeyboardButton("🏆 Top 15 أقسام (شهر)", callback_data="top:month"),
            InlineKeyboardButton("🏆 Top 15 أقسام (من البداية)", callback_data="top:all"),
        ],
        [
            InlineKeyboardButton("🔎 Top 15 استعلام (اليوم)", callback_data="topq:today"),
            InlineKeyboardButton("🔎 Top 15 استعلام (من البداية)", callback_data="topq:all"),
        ],
        [
            InlineKeyboardButton("👥 Top 15 مستخدم (اليوم)", callback_data="topu:today"),
            InlineKeyboardButton("👥 Top 15 مستخدم (من البداية)", callback_data="topu:all"),
        ],
        [InlineKeyboardButton("◀️ رجوع للقائمة", callback_data="home")]
    ]
    return InlineKeyboardMarkup(rows)

def _where_ts(start: datetime, end: datetime) -> Tuple[str, Tuple]:
    return "WHERE ts >= ? AND ts <= ?", (iso(start), iso(end))

def stats_summary(kind: str) -> str:
    start, end, title = period_bounds(kind if kind != "all" else "all")

    conn = db_conn()
    cur = conn.cursor()

    if start is None or end is None:
        cur.execute("SELECT COUNT(*) FROM users")
        total_users = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(DISTINCT user_id) FROM events")
        active_users = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM events WHERE event_type IN ('search_text','dept_select')")
        total_search = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM events WHERE event_type='search_text'")
        total_text_search = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM events WHERE event_type='dept_select'")
        total_button_search = cur.fetchone()[0] or 0

        cur.execute("SELECT MAX(ts) FROM events")
        last_ts = cur.fetchone()[0] or ""

        conn.close()
        return (
            f"{title}\n"
            f"• 👥 مجموع المستخدمين: {total_users}\n"
            f"• ✅ مستخدمين نشطين: {active_users}\n"
            f"• 🔎 مجموع عمليات البحث: {total_search}\n"
            f"   - ✍️ بحث كتابة: {total_text_search}\n"
            f"   - 🧩 اختيار زر: {total_button_search}\n"
            f"• 🕒 آخر نشاط: {last_ts if last_ts else '—'}"
        )

    where, params = _where_ts(start, end)

    cur.execute("SELECT COUNT(*) FROM users WHERE first_seen >= ? AND first_seen <= ?", (iso(start), iso(end)))
    new_users = cur.fetchone()[0] or 0

    cur.execute(f"SELECT COUNT(DISTINCT user_id) FROM events {where}", params)
    active_users = cur.fetchone()[0] or 0

    cur.execute(
        f"SELECT COUNT(*) FROM events {where} AND event_type IN ('search_text','dept_select')",
        params
    )
    total_search = cur.fetchone()[0] or 0

    cur.execute(
        f"SELECT COUNT(*) FROM events {where} AND event_type='search_text'",
        params
    )
    total_text_search = cur.fetchone()[0] or 0

    cur.execute(
        f"SELECT COUNT(*) FROM events {where} AND event_type='dept_select'",
        params
    )
    total_button_search = cur.fetchone()[0] or 0

    cur.execute(
        f"SELECT COUNT(*) FROM events {where} AND event_type='not_found'",
        params
    )
    not_found = cur.fetchone()[0] or 0

    cur.execute(f"SELECT MAX(ts) FROM events {where}", params)
    last_ts = cur.fetchone()[0] or ""

    conn.close()
    return (
        f"{title}\n"
        f"• 👤 مستخدمين جدد: {new_users}\n"
        f"• ✅ مستخدمين نشطين: {active_users}\n"
        f"• 🔎 عمليات البحث: {total_search}\n"
        f"   - ✍️ بحث كتابة: {total_text_search}\n"
        f"   - 🧩 اختيار زر: {total_button_search}\n"
        f"• ❌ بدون نتيجة: {not_found}\n"
        f"• 🕒 آخر نشاط: {last_ts if last_ts else '—'}"
    )

def top15_departments(kind: str) -> str:
    start, end, title0 = period_bounds(kind if kind != "all" else "all")
    title = f"🏆 Top 15 أقسام — {title0.replace('إحصائيات','').strip()}" if start else "🏆 Top 15 أقسام — من البداية"

    conn = db_conn()
    cur = conn.cursor()

    if start and end:
        where, params = _where_ts(start, end)
        cur.execute(
            f"""
            SELECT dept, COUNT(*) AS c
            FROM events
            {where} AND event_type='dept_select' AND dept <> ''
            GROUP BY dept
            ORDER BY c DESC
            LIMIT 15
            """,
            params
        )
    else:
        cur.execute(
            """
            SELECT dept, COUNT(*) AS c
            FROM events
            WHERE event_type='dept_select' AND dept <> ''
            GROUP BY dept
            ORDER BY c DESC
            LIMIT 15
            """
        )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return f"{title}\n❌ لا توجد بيانات كافية."

    lines = [title]
    for i, (dept, c) in enumerate(rows, 1):
        lines.append(f"{i}) {dept} — {c}")
    return "\n".join(lines)

def top15_queries(kind: str) -> str:
    start, end, title0 = period_bounds(kind if kind != "all" else "all")
    title = f"🔎 Top 15 استعلام — {title0.replace('إحصائيات','').strip()}" if start else "🔎 Top 15 استعلام — من البداية"

    conn = db_conn()
    cur = conn.cursor()

    if start and end:
        where, params = _where_ts(start, end)
        cur.execute(
            f"""
            SELECT query, COUNT(*) AS c
            FROM events
            {where} AND event_type='search_text' AND query <> ''
            GROUP BY query
            ORDER BY c DESC
            LIMIT 15
            """,
            params
        )
    else:
        cur.execute(
            """
            SELECT query, COUNT(*) AS c
            FROM events
            WHERE event_type='search_text' AND query <> ''
            GROUP BY query
            ORDER BY c DESC
            LIMIT 15
            """
        )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return f"{title}\n❌ لا توجد بيانات كافية."

    lines = [title]
    for i, (q, c) in enumerate(rows, 1):
        lines.append(f"{i}) {q} — {c}")
    return "\n".join(lines)

def top15_users(kind: str) -> str:
    start, end, title0 = period_bounds(kind if kind != "all" else "all")
    title = f"👥 Top 15 مستخدم — {title0.replace('إحصائيات','').strip()}" if start else "👥 Top 15 مستخدم — من البداية"

    conn = db_conn()
    cur = conn.cursor()

    if start and end:
        where, params = _where_ts(start, end)
        cur.execute(
            f"""
            SELECT user_id, COUNT(*) AS c
            FROM events
            {where} AND event_type IN ('search_text','dept_select')
            GROUP BY user_id
            ORDER BY c DESC
            LIMIT 15
            """,
            params
        )
    else:
        cur.execute(
            """
            SELECT user_id, COUNT(*) AS c
            FROM events
            WHERE event_type IN ('search_text','dept_select')
            GROUP BY user_id
            ORDER BY c DESC
            LIMIT 15
            """
        )
    rows = cur.fetchall()

    result = []
    for uid, c in rows:
        cur.execute("SELECT full_name, username FROM users WHERE user_id=?", (uid,))
        urow = cur.fetchone()
        full_name = (urow[0] if urow and urow[0] else "").strip()
        username = (urow[1] if urow and urow[1] else "").strip()
        label = full_name if full_name else (f"@{username}" if username else str(uid))
        result.append((label, c))

    conn.close()

    if not result:
        return f"{title}\n❌ لا توجد بيانات كافية."

    lines = [title]
    for i, (label, c) in enumerate(result, 1):
        lines.append(f"{i}) {label} — {c}")
    return "\n".join(lines)

# ---------------- Handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)
    log_event("start", update.effective_user.id, update.effective_chat.id if update.effective_chat else None)
    await update.message.reply_text(build_intro(), reply_markup=MAIN_KB)

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)
    log_event("about", update.effective_user.id, update.effective_chat.id if update.effective_chat else None)
    await safe_reply(update, ABOUT_TEXT, reply_markup=MAIN_KB)

async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)
    log_event("reload", update.effective_user.id, update.effective_chat.id if update.effective_chat else None)
    n,msg = load_phonebook()
    await safe_reply(update, msg, reply_markup=MAIN_KB)

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)
    log_event("admin_open", update.effective_user.id, update.effective_chat.id if update.effective_chat else None)
    if not is_admin(update):
        await safe_reply(update, "⛔️ غير مصرح.", reply_markup=MAIN_KB)
        return
    await safe_reply(update, "👑 لوحة الإحصائيات (للأدمن فقط):", reply_markup=admin_menu())

async def list_depts(update: Update, context: ContextTypes.DEFAULT_TYPE, page:int=0):
    if not departments:
        await safe_reply(update, "❌ لا توجد سجلات. استخدم /reload بعد التأكد من ملف الإكسل.", reply_markup=MAIN_KB)
        return
    await reply_plain(update.message, "اختر القسم من القائمة:", reply_markup=grid_all(page))

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)
    txt = (update.message.text or "").strip()
    chat_id = update.effective_chat.id if update.effective_chat else None
    uid = update.effective_user.id

    if txt == "📞 أرقام المستشفى":
        log_event("open_list", uid, chat_id)
        await list_depts(update, context, 0)
        return

    if txt == "🔍 بحث بالاسم":
        log_event("prompt_search", uid, chat_id)
        await safe_reply(update, "✍️ اكتب أي جزء من اسم القسم.", reply_markup=MAIN_KB)
        return

    if txt == "ℹ️ عن البوت":
        log_event("about_btn", uid, chat_id)
        await safe_reply(update, ABOUT_TEXT, reply_markup=MAIN_KB)
        return

    if txt == "◀️ رجوع للقائمة":
        log_event("back_home", uid, chat_id)
        await safe_reply(update, build_intro(), reply_markup=MAIN_KB)
        return

    matches = search_indices(txt)
    log_event("search_text", uid, chat_id, query=txt, extra=f"matches={len(matches)}")

    if not matches:
        log_event("not_found", uid, chat_id, query=txt)
        await safe_reply(update, "❌ لم يتم العثور على هذا القسم.", reply_markup=MAIN_KB)
        return

    if len(matches) == 1:
        idx = matches[0]
        name = departments[idx]
        num = phonebook.get(normalize_arabic(name), "")
        log_event("search_hit", uid, chat_id, dept=name, query=txt)
        await safe_reply(update, f"✅ {name} — {num if num else '—'}", reply_markup=MAIN_KB)
        return

    context.user_data["last_search_indices"] = matches
    await reply_plain(update.message, "🔎 تم العثور على عدة نتائج، اختر القسم:", reply_markup=grid_search(matches, 0))

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data if q else ""
    uid = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None

    try:
        # ADMIN callbacks
        if data.startswith(("stats:","top:","topq:","topu:")):
            await q.answer()
            if not (update.effective_user and update.effective_user.id == ADMIN_ID):
                await reply_plain(q.message, "⛔️ غير مصرح.", reply_markup=MAIN_KB)
                return

            if data.startswith("stats:"):
                kind = data.split(":")[1]
                text = stats_summary(kind)
                await reply_plain(q.message, text, reply_markup=admin_menu())
                return

            if data.startswith("top:"):
                kind = data.split(":")[1]
                text = top15_departments(kind)
                await reply_plain(q.message, text, reply_markup=admin_menu())
                return

            if data.startswith("topq:"):
                kind = data.split(":")[1]
                text = top15_queries(kind)
                await reply_plain(q.message, text, reply_markup=admin_menu())
                return

            if data.startswith("topu:"):
                kind = data.split(":")[1]
                text = top15_users(kind)
                await reply_plain(q.message, text, reply_markup=admin_menu())
                return

        # regular bot callbacks
        if data.startswith("dept:"):
            idx = int(data.split(":")[1])
            if 0 <= idx < len(departments):
                name = departments[idx]
                num = phonebook.get(normalize_arabic(name), "")
                if update.effective_user:
                    upsert_user(update.effective_user)
                    log_event("dept_select", update.effective_user.id, chat_id, dept=name)
                await q.answer(text=f"{name}: {num if num else '—'}", show_alert=False)
                await reply_plain(q.message, f"📞 {name} — {num if num else '—'}")
            else:
                await q.answer("خيار غير صالح.", show_alert=False)
            return

        if data.startswith("allp:"):
            page = int(data.split(":")[1])
            await q.answer()
            await safe_edit(q, "اختر القسم من القائمة:", reply_markup=grid_all(page))
            return

        if data.startswith("srchp:"):
            page = int(data.split(":")[1])
            matches = context.user_data.get("last_search_indices", [])
            await q.answer()
            await safe_edit(q, "🔎 تم العثور على عدة نتائج، اختر القسم:", reply_markup=grid_search(matches, page))
            return

        if data == "home":
            await q.answer()
            try:
                await q.message.edit_text(build_intro(), reply_markup=None)
            except:
                pass
            await reply_plain(q.message, "رجعت إلى القائمة الرئيسية.", reply_markup=MAIN_KB)
            return

        if data == "noop":
            await q.answer()
            return

        await q.answer()

    except Exception:
        try:
            await q.answer("صار خطأ بسيط، جرّب مرة ثانية.", show_alert=False)
        except:
            pass

# ---------------- تشغيل ----------------
def read_token() -> Optional[str]:
    tok = os.getenv("TELEGRAM_BOT_TOKEN")
    if tok:
        return tok.strip()
    path = os.path.join(BASE, "token.txt")
    if os.path.exists(path):
        return open(path, "r", encoding="utf-8").read().strip()
    return None

if __name__ == "__main__":
    init_db()
    n, msg = load_phonebook()
    logging.info(msg)

    token = read_token()
    if not token:
        print("❌ لا يوجد توكن: ضع TELEGRAM_BOT_TOKEN أو token.txt.")
        raise SystemExit(1)

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("about", about_cmd))
    app.add_handler(CommandHandler("reload", reload_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(on_callback))

    print("📞 PhoneBook Bot running…")
    app.run_polling()
