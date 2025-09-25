# bot.py — دليل أرقام (شبكة أقسام + بحث ذكي لكل الأقسام) + انترو مع الاعتماد
import os, logging, asyncio, math, re
from typing import Dict, List, Tuple, Optional
from openpyxl import load_workbook
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.error import RetryAfter, BadRequest, Forbidden, TimedOut, NetworkError

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", BASE)

# مرشحات أسماء الأعمدة
DEPT_CANDIDATES  = ["القسم","قسم","الاسم","اسم القسم"]
PHONE_CANDIDATES = ["رقم الهاتف","الهاتف","رقم","موبايل","Phone"]

# ذاكرة
display_rows: List[Tuple[str, str]] = []   # [(اسم القسم الأصلي، الرقم)]
phonebook: Dict[str, str] = {}             # normalize(name) -> phone
departments: List[str] = []                # أسماء الأقسام الأصلية (للعرض)

# كيبورد رئيسية + شبكة
MAIN_KB   = ReplyKeyboardMarkup([[KeyboardButton("📞 أرقام المستشفى")],
                                 [KeyboardButton("🔍 بحث بالاسم")],
                                 [KeyboardButton("◀️ رجوع للقائمة")]], resize_keyboard=True)
GRID_COLS = 3
PAGE_SIZE = 24

# ---------------- Normalization / Arabic-friendly search ----------------
ARABIC_DIACRITICS = re.compile(r"[ًٌٍَُِّْـ]")
def strip_diacritics(s: str) -> str:
    return ARABIC_DIACRITICS.sub("", s or "")

def normalize_arabic(s: str) -> str:
    s = str(s or "")
    s = s.replace("\u200f","").replace("\u200e","").replace("\ufeff","").strip()
    s = strip_diacritics(s)
    s = s.replace("آ","ا").replace("أ","ا").replace("إ","ا")  # توحيد الألف
    s = s.replace("ى","ي")                                   # توحيد الياء
    s = s.replace("ة","ه")                                   # تاء مربوطة
    s = re.sub(r"[^\w\s\u0600-\u06FF]", " ", s)              # شيل ترقيم
    s = re.sub(r"\s+"," ", s).strip()
    return s.upper()

def list_excel_files(folder: str) -> List[str]:
    try:
        return [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(".xlsx")]
    except Exception:
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

def load_phonebook() -> Tuple[int, str]:
    global display_rows, phonebook, departments
    display_rows, phonebook, departments = [], {}, []
    files = list_excel_files(DATA_DIR)
    if not files:
        return 0, f"❌ ماكو ملفات ‎.xlsx داخل: {DATA_DIR}"

    total = 0
    for path in files:
        try:
            wb = load_workbook(path, read_only=True, data_only=True)
            ws = wb.active
            headers = read_headers(ws)
            if not headers: wb.close(); continue
            di = find_col_idx(headers, DEPT_CANDIDATES)
            pi = find_col_idx(headers, PHONE_CANDIDATES)
            if di is None or pi is None: wb.close(); continue

            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row: continue
                dept  = str(row[di] if di < len(row) and row[di] is not None else "").strip()
                phone = str(row[pi] if pi < len(row) and row[pi] is not None else "").strip()
                if not dept: continue
                display_rows.append((dept, phone))
                phonebook[normalize_arabic(dept)] = phone
                total += 1
            wb.close()
        except Exception as e:
            logging.exception(f"Load error in {path}: {e}")

    display_rows.sort(key=lambda x: x[0])
    departments = [d for d,_ in display_rows]
    if total == 0:
        return 0, "❌ ما تم تحميل أي سجل. تأكد من أسماء الأعمدة."
    return total, f"✅ تم تحميل {total} سجل."

async def safe_reply(update: Update, text: str, reply_markup=None, max_attempts=3):
    attempt = 0
    while attempt < max_attempts:
        try:
            return await update.message.reply_text(text, reply_markup=reply_markup)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1); attempt += 1
        except (BadRequest, Forbidden, TimedOut, NetworkError):
            await asyncio.sleep(1.0); attempt += 1
    try:
        return await update.message.reply_text("تعذر الإرسال حالياً، جرّب مرة ثانية.", reply_markup=reply_markup)
    except Exception:
        return None

def build_intro_text() -> str:
    return (
        "👋 أهلاً بك في بوت أرقام المستشفى.\n\n"
        "اختيارات سريعة:\n"
        "• **📞 أرقام المستشفى**: تصفّح الأقسام كأزرار.\n"
        "• **🔍 بحث بالاسم**: اكتب أي جزء من اسم القسم (مثال: كاميرات / طوارئ).\n"
        "• **◀️ رجوع للقائمة**: الرجوع لهذه الصفحة.\n\n"
        "ℹ️ طريقة الاستخدام:\n"
        "1) اضغط «أرقام المستشفى» واختر القسم من المربعات.\n"
        "2) أو اكتب كلمة من اسم القسم، وسيظهر الرقم حتى لو الاسم مو كامل.\n\n"
        "✨ تم تصميم البوت من قبل وحدة الكاميرات (ياسين التميمي)."
    )

def build_dept_grid(page: int = 0) -> InlineKeyboardMarkup:
    total = len(departments)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    start, end = page * PAGE_SIZE, min(page * PAGE_SIZE + PAGE_SIZE, total)
    slice_items = departments[start:end]

    rows, row = [], []
    for i, name in enumerate(slice_items):
        idx = start + i
        row.append(InlineKeyboardButton(name, callback_data=f"dept:{idx}"))
        if len(row) == GRID_COLS:
            rows.append(row); row = []
    if row: rows.append(row)

    if pages > 1:
        ctrl = []
        if page > 0:            ctrl.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"page:{page-1}"))
        ctrl.append(InlineKeyboardButton(f"صفحة {page+1}/{pages}", callback_data="noop"))
        if page < pages - 1:    ctrl.append(InlineKeyboardButton("التالي ➡️", callback_data=f"page:{page+1}"))
        rows.append(ctrl)
    rows.append([InlineKeyboardButton("◀️ رجوع للقائمة", callback_data="home")])
    return InlineKeyboardMarkup(rows)

# ---------------- أوامر ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_intro_text(), reply_markup=MAIN_KB)

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update, f"Loaded: {len(display_rows)} سجل\nDATA_DIR: {DATA_DIR}", reply_markup=MAIN_KB)

async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n, msg = load_phonebook()
    await safe_reply(update, msg, reply_markup=MAIN_KB)

async def list_depts(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    if not departments:
        await safe_reply(update, "❌ لا توجد سجلات محمّلة. استخدم /reload بعد التأكد من ملف الإكسل.", reply_markup=MAIN_KB); return
    kb = build_dept_grid(page)
    await update.message.reply_text("اختر القسم من القائمة التالية:", reply_markup=kb)

# ---------------- بحث ذكي لكل الأقسام ----------------
async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if txt == "📞 أرقام المستشفى":      await list_depts(update, context, 0); return
    if txt == "🔍 بحث بالاسم":          await safe_reply(update, "✍️ اكتب أي جزء من اسم القسم.", reply_markup=MAIN_KB); return
    if txt == "◀️ رجوع للقائمة":        await safe_reply(update, build_intro_text(), reply_markup=MAIN_KB); return
    await handle_search(update, context)

async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip()
    if not raw:
        await safe_reply(update, "اكتب اسم القسم للبحث.", reply_markup=MAIN_KB); return

    q = normalize_arabic(raw)
    q_alt = q[:-2] if q.endswith("ات") else q  # معالجة جمع بسيطة

    exact, contains, word = [], [], []
    q_words = [w for w in q.split() if len(w) >= 2]

    for dept, phone in display_rows:
        d = normalize_arabic(dept)
        d_alt = d[:-2] if d.endswith("ات") else d

        if q == d or q == d_alt or q_alt == d:
            exact.append((dept, phone)); continue
        if q in d or q in d_alt or q_alt in d:
            contains.append((dept, phone)); continue
        if any(w in d for w in q_words):
            word.append((dept, phone)); continue

    results = exact + contains + word
    if not results:
        await safe_reply(update, "❌ لم يتم العثور على هذا القسم.", reply_markup=MAIN_KB); return

    if len(results) == 1:
        d, p = results[0]
        await safe_reply(update, f"✅ {d} — {p if p else '—'}", reply_markup=MAIN_KB); return

    names = "\n".join([f"• {d}" for d,_ in results[:80]])
    await safe_reply(update, "🔎 تم العثور على عدة نتائج:\n\n" + names + "\n\nاكتب الاسم أدق أو اختر من «أرقام المستشفى».", reply_markup=MAIN_KB)

# ---------------- أزرار الشبكة ----------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data if q else ""
    try:
        if data.startswith("dept:"):
            idx = int(data.split(":")[1])
            if 0 <= idx < len(departments):
                name = departments[idx]
                num  = phonebook.get(normalize_arabic(name), "")
                await q.answer(text=f"{name}: {num if num else '—'}", show_alert=False)
                await q.message.reply_text(f"📞 {name} — {num if num else '—'}")
            else:
                await q.answer("خيار غير صالح.", show_alert=False)
        elif data.startswith("page:"):
            page = int(data.split(":")[1])
            await q.answer()
            await q.message.edit_text("اختر القسم من القائمة التالية:", reply_markup=build_dept_grid(page))
        elif data == "home":
            await q.answer()
            await q.message.edit_text(build_intro_text(), reply_markup=None)
            await q.message.reply_text("رجعت إلى القائمة الرئيسية.", reply_markup=MAIN_KB)
        else:
            await q.answer()
    except Exception as e:
        logging.error(f"Callback error: {e}")
        try: await q.answer("صار خطأ بسيط، جرّب مرة ثانية.", show_alert=False)
        except: pass

# ---------------- تشغيل ----------------
def read_token() -> Optional[str]:
    tok = os.getenv("TELEGRAM_BOT_TOKEN")
    if tok: return tok.strip()
    path = os.path.join(BASE, "token.txt")
    if os.path.exists(path):
        return open(path, "r", encoding="utf-8").read().strip()
    return None

if __name__ == "__main__":
    n, msg = load_phonebook(); logging.info(msg)
    token = read_token()
    if not token:
        print("❌ لا يوجد توكن: ضع TELEGRAM_BOT_TOKEN (Render) أو token.txt محلياً."); raise SystemExit(1)

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("reload", reload_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    app.add_handler(CallbackQueryHandler(on_callback))

    print("📞 PhoneBook Bot (grid + smart search) running…")
    app.run_polling()
