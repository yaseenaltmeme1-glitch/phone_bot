# bot.py — PhoneBook Bot (Arabic) + Admin Analytics (Pro)
import os, re, math, io, csv, sqlite3, logging, asyncio
from typing import List, Tuple, Dict, Optional
from datetime import datetime, timedelta

from telegram import (
    Update,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputFile
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.error import RetryAfter

from openpyxl import load_workbook
# NOTE: XLSX export uses openpyxl.Workbook (imported lazily in exporter)

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Baghdad")  # كربلاء نفس توقيت بغداد
except Exception:
    TZ = None

# ================== إعدادات عامة ==================
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", BASE)

ADMIN_ID = 8099482759  # 👑 Your Telegram numeric ID
CONTACT_USERNAME = "@ya_se91"

SIGNATURE = "\n────────────\nSource: CCTV – Yaseen Al-Tamimi"

ABOUT_TEXT = (
    "ℹ️ عن البوت\n"
    "بوت دليل أرقام مستشفى الإمام الحسن المجتبى (ع).\n"
    "يدعم التصفح بالأزرار + البحث بالاسم بسرعة.\n\n"
    f"📩 لمزيد من الاستفسارات أو مقترحات التعديل: {CONTACT_USERNAME}\n"
    "────────────\n"
    "Source: CCTV – Yaseen Al-Tamimi"
)

WELCOME_BROADCAST = (
    "👋 أهلًا بكم\n"
    "هذا بوت دليل أرقام مستشفى الإمام الحسن المجتبى (ع).\n\n"
    "إذا عندكم أي مقترحات/تعديلات تحبون نضيفها للبوت، راسلوني مباشرة:\n"
    f"{CONTACT_USERNAME}\n\n"
    "شكرًا لكم 🌿"
)

DB_PATH = os.path.join(BASE, "stats.db")

# ================== تطبيع عربي ==================
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

# ================== وقت كربلاء ==================
def now_local() -> datetime:
    if TZ:
        return datetime.now(TZ)
    return datetime.utcnow() + timedelta(hours=3)

def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()

def fmt_ts(ts: str) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts)
        if TZ:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            else:
                dt = dt.astimezone(TZ)
        return dt.strftime("%Y-%m-%d  %H:%M:%S")
    except Exception:
        return ts

# ================== قاعدة البيانات ==================
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

def upsert_user(tg_user):
    if not tg_user:
        return
    uid = tg_user.id
    username = tg_user.username or ""
    full_name = (tg_user.full_name or "").strip()
    t = iso(now_local())
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id=?", (uid,))
    if cur.fetchone():
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

def log_event(event_type: str, user_id: int, chat_id: Optional[int], dept: str="", query: str="", extra: str=""):
    conn = db_conn()
    conn.execute(
        "INSERT INTO events(ts, user_id, chat_id, event_type, dept, query, extra) VALUES(?,?,?,?,?,?,?)",
        (iso(now_local()), user_id, chat_id if chat_id is not None else None, event_type, dept or "", query or "", extra or "")
    )
    conn.commit()
    conn.close()

def is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)

# ================== تحميل الإكسل ==================
DEPT_CANDIDATES  = ["القسم","قسم","الاسم","اسم القسم"]
PHONE_CANDIDATES = ["رقم الهاتف","الهاتف","رقم","موبايل","Phone"]

display_rows: List[Tuple[str,str]] = []
departments: List[str] = []
phonebook: Dict[str,str] = {}

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
    for i, h in enumerate(H):
        if h in C:
            return i
    for i, h in enumerate(H):
        for c in C:
            if c in h:
                return i
    return None

def load_phonebook() -> Tuple[int,str]:
    global display_rows, departments, phonebook
    display_rows, departments, phonebook = [], [], {}
    files = list_excel_files(DATA_DIR)
    if not files:
        return 0, f"❌ ماكو ملفات .xlsx داخل: {DATA_DIR}"
    total = 0
    for path in files:
        try:
            wb = load_workbook(path, read_only=True, data_only=True)
            ws = wb.active
            headers = read_headers(ws)
            if not headers:
                wb.close(); continue
            di = find_col_idx(headers, DEPT_CANDIDATES)
            pi = find_col_idx(headers, PHONE_CANDIDATES)
            if di is None or pi is None:
                wb.close(); continue
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row:
                    continue
                dept = str(row[di] if di < len(row) and row[di] is not None else "").strip()
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

# ================== الواجهة ==================
MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📞 أرقام المستشفى")],
        [KeyboardButton("🔍 بحث بالاسم")],
        [KeyboardButton("ℹ️ عن البوت")],
        [KeyboardButton("◀️ رجوع للقائمة")]
    ],
    resize_keyboard=True
)

GRID_COLS = 3
PAGE_SIZE_ALL = 24
PAGE_SIZE_SRCH = 21

def build_intro() -> str:
    return (
        "👋 أهلاً بك في بوت أرقام مستشفى الإمام الحسن المجتبى (ع).\n\n"
        "📌 طريقة الاستخدام:\n"
        "• 📞 أرقام المستشفى: تصفّح الأقسام كمربعات.\n"
        "• 🔍 بحث بالاسم: اكتب جزء من اسم القسم.\n"
        "• ℹ️ عن البوت: معلومات.\n\n"
        f"📩 للاقتراحات والتعديل: {CONTACT_USERNAME}\n"
        "────────────\n"
        "Source: CCTV – Yaseen Al-Tamimi"
    )

def search_indices(query: str) -> List[int]:
    qn = normalize_arabic(query)
    if not qn:
        return []
    return [i for i, name in enumerate(departments) if qn in normalize_arabic(name)]

def build_grid(indices: List[int], page: int, page_size: int, cols: int, mode: str) -> InlineKeyboardMarkup:
    total = len(indices)
    pages = max(1, math.ceil(total / page_size))
    page = max(0, min(page, pages-1))
    start, end = page*page_size, min(page*page_size + page_size, total)
    slice_idx = indices[start:end]

    rows, row = [], []
    for idx in slice_idx:
        name = departments[idx]
        row.append(InlineKeyboardButton(name, callback_data=f"dept:{idx}"))
        if len(row) == cols:
            rows.append(row); row = []
    if row:
        rows.append(row)

    if pages > 1:
        ctrl = []
        if page > 0:
            ctrl.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"{mode}:{page-1}"))
        ctrl.append(InlineKeyboardButton(f"صفحة {page+1}/{pages}", callback_data="noop"))
        if page < pages-1:
            ctrl.append(InlineKeyboardButton("التالي ➡️", callback_data=f"{mode}:{page+1}"))
        rows.append(ctrl)

    rows.append([InlineKeyboardButton("◀️ رجوع للقائمة", callback_data="home")])
    return InlineKeyboardMarkup(rows)

def grid_all(page: int=0) -> InlineKeyboardMarkup:
    return build_grid(list(range(len(departments))), page, PAGE_SIZE_ALL, GRID_COLS, "allp")

def grid_search(matches: List[int], page: int=0) -> InlineKeyboardMarkup:
    return build_grid(matches, page, PAGE_SIZE_SRCH, GRID_COLS, "srchp")

# ================== ارسال آمن ==================
async def safe_reply_msg(msg, text: str, reply_markup=None):
    text = f"{text}{SIGNATURE}"
    try:
        return await msg.reply_text(text, reply_markup=reply_markup)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        return await msg.reply_text(text, reply_markup=reply_markup)

async def safe_reply(update: Update, text: str, reply_markup=None):
    return await safe_reply_msg(update.message, text, reply_markup=reply_markup)

# ================== Admin: منيو + تقارير ==================
def admin_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🏆 Top 10 أقسام (من البداية)", callback_data="adm:top10_depts")],
        [
            InlineKeyboardButton("👥 عدد المستخدمين الكلي", callback_data="adm:users_total"),
            InlineKeyboardButton("🧾 قائمة كل المستخدمين", callback_data="adm:users_list")
        ],
        [
            InlineKeyboardButton("👥 Top 15 مستخدم (استخدام)", callback_data="adm:top15_users"),
            InlineKeyboardButton("🕒 آخر 25 مستخدم (نشاط)", callback_data="adm:recent25")
        ],
        [InlineKeyboardButton("🕒 آخر نشاط", callback_data="adm:last_activity")],
        [InlineKeyboardButton("📥 تصدير التقارير", callback_data="adm:export_menu")],
        [InlineKeyboardButton("📣 إرسال رسالة ترحيب/اقتراحات", callback_data="adm:broadcast")],
        [InlineKeyboardButton("◀️ رجوع للقائمة", callback_data="home")]
    ]
    return InlineKeyboardMarkup(rows)

def export_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📊 تقرير شامل (XLSX)", callback_data="exp:xlsx:full"),
         InlineKeyboardButton("📊 تقرير شامل (CSV)", callback_data="exp:csv:full")],
        [InlineKeyboardButton("👥 كل المستخدمين (XLSX)", callback_data="exp:xlsx:users_all"),
         InlineKeyboardButton("👥 كل المستخدمين (CSV)", callback_data="exp:csv:users_all")],
        [InlineKeyboardButton("✅ مستخدمين استعملوا البوت (XLSX)", callback_data="exp:xlsx:users_used"),
         InlineKeyboardButton("✅ مستخدمين استعملوا البوت (CSV)", callback_data="exp:csv:users_used")],
        [InlineKeyboardButton("🏆 Top10 الأقسام (XLSX)", callback_data="exp:xlsx:top_depts"),
         InlineKeyboardButton("🏆 Top10 الأقسام (CSV)", callback_data="exp:csv:top_depts")],
        [InlineKeyboardButton("👥 Top15 المستخدمين (XLSX)", callback_data="exp:xlsx:top_users"),
         InlineKeyboardButton("👥 Top15 المستخدمين (CSV)", callback_data="exp:csv:top_users")],
        [InlineKeyboardButton("🕒 آخر 25 نشاط (XLSX)", callback_data="exp:xlsx:recent25"),
         InlineKeyboardButton("🕒 آخر 25 نشاط (CSV)", callback_data="exp:csv:recent25")],
        [InlineKeyboardButton("◀️ رجوع", callback_data="adm:back")]
    ]
    return InlineKeyboardMarkup(rows)

def users_total() -> int:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    n = cur.fetchone()[0] or 0
    conn.close()
    return n

def last_activity_ts() -> str:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT MAX(ts) FROM events")
    ts = cur.fetchone()[0] or ""
    conn.close()
    return fmt_ts(ts)

def top10_depts_alltime() -> List[Tuple[str,int]]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT dept, COUNT(*) AS c
        FROM events
        WHERE event_type IN ('dept_select','search_hit') AND dept <> ''
        GROUP BY dept
        ORDER BY c DESC
        LIMIT 10
    """)
    rows = [(r[0], int(r[1])) for r in cur.fetchall()]
    conn.close()
    return rows

def top15_users_alltime() -> List[Tuple[int,str,str,int,str,str]]:
    """
    returns: (user_id, full_name, username, count, first_used, last_used)
    """
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, COUNT(*) AS c, MIN(ts) AS first_used, MAX(ts) AS last_used
        FROM events
        WHERE event_type IN ('dept_select','search_hit','search_text')
        GROUP BY user_id
        ORDER BY c DESC
        LIMIT 15
    """)
    base = cur.fetchall()
    out = []
    for uid, c, fts, lts in base:
        cur.execute("SELECT full_name, username FROM users WHERE user_id=?", (uid,))
        u = cur.fetchone() or ("","")
        full_name = (u[0] or "").strip()
        username = (u[1] or "").strip()
        out.append((int(uid), full_name, username, int(c), fmt_ts(fts), fmt_ts(lts)))
    conn.close()
    return out

def recent25_users() -> List[Tuple[int,str,str,str]]:
    """
    آخر 25 مستخدم نشط (حسب آخر حدث)
    returns: (user_id, full_name, username, last_used_ts_fmt)
    """
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, MAX(ts) AS last_used
        FROM events
        GROUP BY user_id
        ORDER BY last_used DESC
        LIMIT 25
    """)
    base = cur.fetchall()
    out = []
    for uid, lts in base:
        cur.execute("SELECT full_name, username FROM users WHERE user_id=?", (uid,))
        u = cur.fetchone() or ("","")
        out.append((int(uid), (u[0] or "").strip(), (u[1] or "").strip(), fmt_ts(lts)))
    conn.close()
    return out

def users_all_list() -> List[Tuple[int,str,str,str,str]]:
    """
    كل المستخدمين: (user_id, full_name, username, first_seen, last_seen)
    """
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, COALESCE(full_name,''), COALESCE(username,''), first_seen, last_seen
        FROM users
        ORDER BY last_seen DESC
    """)
    rows = [(int(uid), (fn or "").strip(), (un or "").strip(), fmt_ts(fs), fmt_ts(ls)) for uid, fn, un, fs, ls in cur.fetchall()]
    conn.close()
    return rows

def users_used_list() -> List[Tuple[int,str,str,str,str]]:
    """
    مستخدمين استخدموا البوت فعلاً (أي بحث/اختيار)
    (user_id, full_name, username, first_used, last_used)
    """
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, MIN(ts) AS first_used, MAX(ts) AS last_used
        FROM events
        WHERE event_type IN ('dept_select','search_hit','search_text')
        GROUP BY user_id
        ORDER BY first_used ASC
    """)
    base = cur.fetchall()
    out = []
    for uid, fts, lts in base:
        cur.execute("SELECT full_name, username FROM users WHERE user_id=?", (uid,))
        u = cur.fetchone() or ("","")
        out.append((int(uid), (u[0] or "").strip(), (u[1] or "").strip(), fmt_ts(fts), fmt_ts(lts)))
    conn.close()
    return out

# ============ تصدير CSV/XLSX بشكل "مدروس" ============
def build_csv(report_kind: str) -> bytes:
    """
    report_kind: full, users_all, users_used, top_depts, top_users, recent25
    CSV UTF-8 BOM for Excel
    """
    out = io.StringIO()
    w = csv.writer(out)

    generated = fmt_ts(iso(now_local()))
    total_users = users_total()
    last_act = last_activity_ts()

    # header / summary section
    w.writerow(["Hospital", "Imam Al-Hasan Al-Mujtaba Hospital"])
    w.writerow(["Report", report_kind])
    w.writerow(["GeneratedAt (Karbala)", generated])
    w.writerow(["TotalUsers", total_users])
    w.writerow(["LastActivity (Karbala)", last_act])
    w.writerow([])

    if report_kind == "top_depts":
        w.writerow(["Top 10 Departments (All-time)"])
        w.writerow(["Rank", "Department", "SearchCount"])
        for i, (d, c) in enumerate(top10_depts_alltime(), 1):
            w.writerow([i, d, c])

    elif report_kind == "top_users":
        w.writerow(["Top 15 Users (All-time usage)"])
        w.writerow(["Rank", "UserID", "Name", "Username", "UsageCount", "FirstUsed", "LastUsed"])
        for i, (uid, fn, un, c, fts, lts) in enumerate(top15_users_alltime(), 1):
            w.writerow([i, uid, fn, f"@{un}" if un else "", c, fts, lts])

    elif report_kind == "recent25":
        w.writerow(["Recent 25 Active Users"])
        w.writerow(["Rank", "UserID", "Name", "Username", "LastUsed"])
        for i, (uid, fn, un, lts) in enumerate(recent25_users(), 1):
            w.writerow([i, uid, fn, f"@{un}" if un else "", lts])

    elif report_kind == "users_all":
        rows = users_all_list()
        w.writerow([f"All Users List (count={len(rows)})"])
        w.writerow(["#", "UserID", "Name", "Username", "FirstSeen", "LastSeen"])
        for i, (uid, fn, un, fs, ls) in enumerate(rows, 1):
            w.writerow([i, uid, fn, f"@{un}" if un else "", fs, ls])

    elif report_kind == "users_used":
        rows = users_used_list()
        w.writerow([f"Users Who Used The Bot (count={len(rows)})"])
        w.writerow(["#", "UserID", "Name", "Username", "FirstUsed", "LastUsed"])
        for i, (uid, fn, un, fts, lts) in enumerate(rows, 1):
            w.writerow([i, uid, fn, f"@{un}" if un else "", fts, lts])

    else:  # full
        # Summary sheet-equivalent in CSV: multiple blocks
        w.writerow(["Top 10 Departments (All-time)"])
        w.writerow(["Rank", "Department", "SearchCount"])
        for i, (d, c) in enumerate(top10_depts_alltime(), 1):
            w.writerow([i, d, c])
        w.writerow([])

        w.writerow(["Top 15 Users (All-time usage)"])
        w.writerow(["Rank", "UserID", "Name", "Username", "UsageCount", "FirstUsed", "LastUsed"])
        for i, (uid, fn, un, c, fts, lts) in enumerate(top15_users_alltime(), 1):
            w.writerow([i, uid, fn, f"@{un}" if un else "", c, fts, lts])
        w.writerow([])

        w.writerow(["Recent 25 Active Users"])
        w.writerow(["Rank", "UserID", "Name", "Username", "LastUsed"])
        for i, (uid, fn, un, lts) in enumerate(recent25_users(), 1):
            w.writerow([i, uid, fn, f"@{un}" if un else "", lts])
        w.writerow([])

        rows = users_used_list()
        w.writerow([f"Users Who Used The Bot (count={len(rows)})"])
        w.writerow(["#", "UserID", "Name", "Username", "FirstUsed", "LastUsed"])
        for i, (uid, fn, un, fts, lts) in enumerate(rows, 1):
            w.writerow([i, uid, fn, f"@{un}" if un else "", fts, lts])

    return out.getvalue().encode("utf-8-sig")

def _xlsx_autowidth(ws, max_col: int, max_row: int):
    # crude width calculation
    for col in range(1, max_col+1):
        max_len = 0
        for row in range(1, min(max_row, 2000)+1):
            v = ws.cell(row=row, column=col).value
            if v is None:
                continue
            s = str(v)
            if len(s) > max_len:
                max_len = len(s)
        ws.column_dimensions[chr(64+col)].width = min(max(10, max_len + 2), 45)

def build_xlsx(report_kind: str) -> bytes:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment, PatternFill
    except Exception:
        raise RuntimeError("openpyxl not installed")

    wb = Workbook()

    def add_summary():
        ws = wb.active
        ws.title = "Summary"
        ws["A1"] = "Imam Al-Hasan Al-Mujtaba Hospital"
        ws["A2"] = f"GeneratedAt (Karbala): {fmt_ts(iso(now_local()))}"
        ws["A3"] = f"Total Users: {users_total()}"
        ws["A4"] = f"Last Activity (Karbala): {last_activity_ts()}"
        ws["A1"].font = Font(bold=True, size=14)
        ws["A1"].alignment = Alignment(horizontal="left")
        ws["A6"] = "Report Kind:"
        ws["B6"] = report_kind
        ws.freeze_panes = "A7"
        return ws

    def style_header(ws, row=1):
        fill = PatternFill("solid", fgColor="1F2937")  # dark
        font = Font(bold=True, color="FFFFFF")
        for c in range(1, ws.max_column+1):
            cell = ws.cell(row=row, column=c)
            cell.fill = fill
            cell.font = font
            cell.alignment = Alignment(horizontal="center")

    def make_sheet(name: str, headers: List[str], rows: List[List]):
        ws = wb.create_sheet(title=name)
        ws.append(headers)
        for r in rows:
            ws.append(r)
        style_header(ws, 1)
        ws.freeze_panes = "A2"
        _xlsx_autowidth(ws, ws.max_column, ws.max_row)
        return ws

    add_summary()

    # Data blocks
    if report_kind in ("full", "top_depts"):
        rows = [[i, d, c] for i, (d, c) in enumerate(top10_depts_alltime(), 1)]
        make_sheet("TopDepts", ["Rank", "Department", "SearchCount"], rows)

    if report_kind in ("full", "top_users"):
        rows = []
        for i, (uid, fn, un, c, fts, lts) in enumerate(top15_users_alltime(), 1):
            rows.append([i, uid, fn, f"@{un}" if un else "", c, fts, lts])
        make_sheet("TopUsers", ["Rank","UserID","Name","Username","UsageCount","FirstUsed","LastUsed"], rows)

    if report_kind in ("full", "recent25"):
        rows = []
        for i, (uid, fn, un, lts) in enumerate(recent25_users(), 1):
            rows.append([i, uid, fn, f"@{un}" if un else "", lts])
        make_sheet("Recent25", ["Rank","UserID","Name","Username","LastUsed"], rows)

    if report_kind in ("full", "users_all"):
        rows = []
        allu = users_all_list()
        for i, (uid, fn, un, fs, ls) in enumerate(allu, 1):
            rows.append([i, uid, fn, f"@{un}" if un else "", fs, ls])
        make_sheet("UsersAll", ["#","UserID","Name","Username","FirstSeen","LastSeen"], rows)

    if report_kind in ("full", "users_used"):
        rows = []
        used = users_used_list()
        for i, (uid, fn, un, fts, lts) in enumerate(used, 1):
            rows.append([i, uid, fn, f"@{un}" if un else "", fts, lts])
        make_sheet("UsersUsed", ["#","UserID","Name","Username","FirstUsed","LastUsed"], rows)

    # Remove default empty sheet if any besides Summary
    # (Workbook creates a default sheet; we repurposed it as Summary)

    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()

# ================== Handlers ==================
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
    n, msg = load_phonebook()
    await safe_reply(update, msg, reply_markup=MAIN_KB)

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)
    log_event("admin_open", update.effective_user.id, update.effective_chat.id if update.effective_chat else None)
    if not is_admin(update):
        await safe_reply(update, "⛔️ غير مصرح.", reply_markup=MAIN_KB)
        return
    await safe_reply(update, "👑 لوحة الإدارة والإحصائيات:", reply_markup=admin_menu())

async def list_depts(update: Update, page:int=0):
    if not departments:
        await safe_reply(update, "❌ لا توجد سجلات. استخدم /reload بعد التأكد من ملف الإكسل.", reply_markup=MAIN_KB)
        return
    await safe_reply_msg(update.message, "اختر القسم من القائمة:", reply_markup=grid_all(page))

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)
    uid = update.effective_user.id
    chat_id = update.effective_chat.id if update.effective_chat else None
    txt = (update.message.text or "").strip()

    if txt == "📞 أرقام المستشفى":
        log_event("open_list", uid, chat_id)
        await list_depts(update, 0)
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

    # Search text
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
    await safe_reply_msg(update.message, "🔎 تم العثور على عدة نتائج، اختر القسم:", reply_markup=grid_search(matches, 0))

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data if q else ""
    uid = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None

    await q.answer()

    # Home / noop
    if data == "home":
        try:
            await q.message.edit_text(build_intro())
        except:
            pass
        await safe_reply_msg(q.message, "رجعت إلى القائمة الرئيسية.", reply_markup=MAIN_KB)
        return
    if data == "noop":
        return

    # Paging
    if data.startswith("allp:"):
        page = int(data.split(":")[1])
        await q.message.edit_text("اختر القسم من القائمة:", reply_markup=grid_all(page))
        return
    if data.startswith("srchp:"):
        page = int(data.split(":")[1])
        matches = context.user_data.get("last_search_indices", [])
        await q.message.edit_text("🔎 تم العثور على عدة نتائج، اختر القسم:", reply_markup=grid_search(matches, page))
        return

    # Department select
    if data.startswith("dept:"):
        idx = int(data.split(":")[1])
        if 0 <= idx < len(departments):
            name = departments[idx]
            num = phonebook.get(normalize_arabic(name), "")
            upsert_user(update.effective_user)
            log_event("dept_select", uid, chat_id, dept=name)
            await safe_reply_msg(q.message, f"📞 {name} — {num if num else '—'}")
        else:
            await safe_reply_msg(q.message, "خيار غير صالح.")
        return

    # Admin area
    if data.startswith("adm:") or data.startswith("exp:"):
        if uid != ADMIN_ID:
            await safe_reply_msg(q.message, "⛔️ غير مصرح.", reply_markup=MAIN_KB)
            return

    if data == "adm:back":
        await safe_reply_msg(q.message, "👑 لوحة الإدارة والإحصائيات:", reply_markup=admin_menu())
        return

    if data == "adm:top10_depts":
        rows = top10_depts_alltime()
        if not rows:
            await safe_reply_msg(q.message, "🏆 Top 10 أقسام (من البداية)\n❌ لا توجد بيانات كافية بعد.", reply_markup=admin_menu())
            return
        text = ["🏆 Top 10 أقسام (من البداية)"]
        for i, (d, c) in enumerate(rows, 1):
            text.append(f"{i}) {d} — {c}")
        await safe_reply_msg(q.message, "\n".join(text), reply_markup=admin_menu())
        return

    if data == "adm:users_total":
        n = users_total()
        await safe_reply_msg(q.message, f"👥 عدد المستخدمين الكلي: {n}", reply_markup=admin_menu())
        return

    if data == "adm:users_list":
        rows = users_all_list()
        n = len(rows)
        # show first 50 in chat
        lines = [f"👥 قائمة كل المستخدمين (العدد الكلي: {n})"]
        for (uid0, fn, un, fs, ls) in rows[:50]:
            uname = f"@{un}" if un else ""
            label = fn if fn else str(uid0)
            lines.append(f"• {label} {uname}".strip())
        if n > 50:
            lines.append(f"… و {n-50} آخرين (موجودين بالتصدير).")
        await safe_reply_msg(q.message, "\n".join(lines), reply_markup=admin_menu())
        return

    if data == "adm:top15_users":
        rows = top15_users_alltime()
        if not rows:
            await safe_reply_msg(q.message, "👥 Top 15 مستخدم\n❌ لا توجد بيانات كافية بعد.", reply_markup=admin_menu())
            return
        lines = ["👥 Top 15 مستخدم (من البداية)"]
        for i, (uid0, fn, un, c, fts, lts) in enumerate(rows, 1):
            uname = f"@{un}" if un else ""
            name = fn if fn else str(uid0)
            lines.append(f"{i}) {name} {uname} — {c} | آخر: {lts}")
        await safe_reply_msg(q.message, "\n".join(lines), reply_markup=admin_menu())
        return

    if data == "adm:recent25":
        rows = recent25_users()
        if not rows:
            await safe_reply_msg(q.message, "🕒 آخر 25 مستخدم\n❌ لا توجد بيانات كافية بعد.", reply_markup=admin_menu())
            return
        lines = ["🕒 آخر 25 مستخدم (نشاط)"]
        for i, (uid0, fn, un, lts) in enumerate(rows, 1):
            uname = f"@{un}" if un else ""
            name = fn if fn else str(uid0)
            lines.append(f"{i}) {name} {uname} — {lts}")
        await safe_reply_msg(q.message, "\n".join(lines), reply_markup=admin_menu())
        return

    if data == "adm:last_activity":
        await safe_reply_msg(q.message, f"🕒 آخر نشاط (Karbala): {last_activity_ts()}", reply_markup=admin_menu())
        return

    if data == "adm:export_menu":
        await safe_reply_msg(q.message, "📥 اختر نوع التقرير للتصدير:", reply_markup=export_menu())
        return

    if data == "adm:broadcast":
        context.user_data["broadcast_confirm"] = True
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ إرسال الآن", callback_data="adm:broadcast_send"),
             InlineKeyboardButton("❌ إلغاء", callback_data="adm:back")]
        ])
        await safe_reply_msg(q.message, f"📣 هذه الرسالة ستُرسل لكل المستخدمين:\n\n{WELCOME_BROADCAST}", reply_markup=kb)
        return

    if data == "adm:broadcast_send":
        if not context.user_data.get("broadcast_confirm"):
            await safe_reply_msg(q.message, "❌ لا يوجد طلب إرسال مؤكد.", reply_markup=admin_menu())
            return
        context.user_data["broadcast_confirm"] = False
        await safe_reply_msg(q.message, "⏳ جاري الإرسال…", reply_markup=admin_menu())

        # send to all users
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users")
        user_ids = [r[0] for r in cur.fetchall()]
        conn.close()

        sent = 0
        failed = 0
        for u in user_ids:
            try:
                await context.bot.send_message(chat_id=u, text=WELCOME_BROADCAST)
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1

        await safe_reply_msg(q.message, f"✅ تم الإرسال.\n• تم: {sent}\n• فشل: {failed}", reply_markup=admin_menu())
        return

    # Export
    if data.startswith("exp:"):
        _, fmt, kind = data.split(":", 2)
        filename = f"report_{kind}_{now_local().strftime('%Y%m%d_%H%M%S')}.{fmt}"
        try:
            if fmt == "csv":
                content = build_csv(kind)
                bio = io.BytesIO(content)
                bio.seek(0)
                await context.bot.send_document(
                    chat_id=q.message.chat_id,
                    document=InputFile(bio, filename=filename),
                    caption=f"📄 {kind} (CSV)"
                )
            else:  # xlsx
                content = build_xlsx(kind)
                bio = io.BytesIO(content)
                bio.seek(0)
                await context.bot.send_document(
                    chat_id=q.message.chat_id,
                    document=InputFile(bio, filename=filename),
                    caption=f"📊 {kind} (XLSX)"
                )
            await safe_reply_msg(q.message, "✅ تم تصدير التقرير.", reply_markup=admin_menu())
        except RuntimeError as e:
            await safe_reply_msg(q.message, f"❌ تصدير XLSX يحتاج openpyxl.\nنفذ على السيرفر:\npython3 -m pip install openpyxl\n\n{e}", reply_markup=admin_menu())
        except Exception as e:
            await safe_reply_msg(q.message, f"❌ فشل التصدير: {e}", reply_markup=admin_menu())
        return

# ================== Token ==================
def read_token() -> Optional[str]:
    tok = os.getenv("TELEGRAM_BOT_TOKEN")
    if tok:
        return tok.strip()
    path = os.path.join(BASE, "token.txt")
    if os.path.exists(path):
        return open(path, "r", encoding="utf-8").read().strip()
    return None

# ================== Main ==================
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
