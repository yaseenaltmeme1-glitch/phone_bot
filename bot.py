# bot.py — دليل أرقام المستشفى (بالعربي) + بصمة إنكليزية
import os, logging, asyncio, math, re
from typing import Dict, List, Tuple, Optional
from openpyxl import load_workbook
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
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
    return total, (f"✅ تم تحميل {total} سجل." if total else "❌ لم يتم تحميل أي سجل.")

# ---------------- أدوات إرسال ----------------
async def safe_reply(update: Update, text: str, reply_markup=None):
    text = f"{text}{SIGNATURE}"
    try:
        return await update.message.reply_text(text, reply_markup=reply_markup)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        return await update.message.reply_text(text, reply_markup=reply_markup)

async def reply_plain(update_or_msg, text: str, reply_markup=None):
    text = f"{text}{SIGNATURE}"
    return await update_or_msg.reply_text(text, reply_markup=reply_markup)

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
    return build_grid(list(range(len(departments))), page, 24, 3, "allp")

def grid_search(matches: List[int], page:int=0) -> InlineKeyboardMarkup:
    return build_grid(matches, page, 21, 3, "srchp")

# ---------------- البحث ----------------
def search_indices(query: str) -> List[int]:
    qn = normalize_arabic(query)
    if not qn: return []
    matches = []
    for i, name in enumerate(departments):
        if qn in normalize_arabic(name):
            matches.append(i)
    return matches

# ---------------- Handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_intro(), reply_markup=MAIN_KB)

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update, ABOUT_TEXT, reply_markup=MAIN_KB)

async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n,msg = load_phonebook()
    await safe_reply(update, msg, reply_markup=MAIN_KB)

async def list_depts(update: Update, context: ContextTypes.DEFAULT_TYPE, page:int=0):
    if not departments:
        await safe_reply(update, "❌ لا توجد سجلات. استخدم /reload بعد التأكد من ملف الإكسل.", reply_markup=MAIN_KB); return
    await reply_plain(update.message, "اختر القسم من القائمة:", reply_markup=grid_all(page))

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if txt == "📞 أرقام المستشفى":  await list_depts(update, context, 0); return
    if txt == "🔍 بحث بالاسم":      await safe_reply(update, "✍️ اكتب أي جزء من اسم القسم.", reply_markup=MAIN_KB); return
    if txt == "ℹ️ عن البوت":        await safe_reply(update, ABOUT_TEXT, reply_markup=MAIN_KB); return
    if txt == "◀️ رجوع للقائمة":    await safe_reply(update, build_intro(), reply_markup=MAIN_KB); return

    matches = search_indices(txt)
    if not matches:
        await safe_reply(update, "❌ لم يتم العثور على هذا القسم.", reply_markup=MAIN_KB); return
    if len(matches) == 1:
        idx = matches[0]; name = departments[idx]; num = phonebook.get(normalize_arabic(name), "")
        await safe_reply(update, f"✅ {name} — {num if num else '—'}", reply_markup=MAIN_KB); return

    context.user_data["last_search_indices"] = matches
    await reply_plain(update.message, "🔎 تم العثور على عدة نتائج، اختر القسم:", reply_markup=grid_search(matches, 0))

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data if q else ""
    try:
        if data.startswith("dept:"):
            idx = int(data.split(":")[1])
            if 0 <= idx < len(departments):
                name = departments[idx]; num = phonebook.get(normalize_arabic(name), "")
                await q.answer(text=f"{name}: {num if num else '—'}", show_alert=False)
                await reply_plain(q.message, f"📞 {name} — {num if num else '—'}")
            else:
                await q.answer("خيار غير صالح.", show_alert=False)

        elif data.startswith("allp:"):
            page = int(data.split(":")[1])
            await q.answer()
            await q.message.edit_text("اختر القسم من القائمة:", reply_markup=grid_all(page))

        elif data.startswith("srchp:"):
            page = int(data.split(":")[1])
            matches = context.user_data.get("last_search_indices", [])
            if not matches: matches = []
            await q.answer()
            await q.message.edit_text("🔎 تم العثور على عدة نتائج، اختر القسم:", reply_markup=grid_search(matches, page))

        elif data == "home":
            await q.answer()
            await q.message.edit_text(build_intro(), reply_markup=None)
            await reply_plain(q.message, "رجعت إلى القائمة الرئيسية.", reply_markup=MAIN_KB)

        elif data == "noop":
            await q.answer()

        else:
            await q.answer()

    except Exception:
        try: await q.answer("صار خطأ بسيط، جرّب مرة ثانية.", show_alert=False)
        except: pass

# ---------------- تشغيل ----------------
def read_token() -> Optional[str]:
    tok = os.getenv("TELEGRAM_BOT_TOKEN")
    if tok: return tok.strip()
    path = os.path.join(BASE, "token.txt")
    if os.path.exists(path): return open(path, "r", encoding="utf-8").read().strip()
    return None

if __name__ == "__main__":
    n, msg = load_phonebook(); logging.info(msg)
    token = read_token()
    if not token:
        print("❌ لا يوجد توكن: ضع TELEGRAM_BOT_TOKEN (Render) أو token.txt محلياً."); raise SystemExit(1)

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("about", about_cmd))
    app.add_handler(CommandHandler("reload", reload_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    app.add_handler(CallbackQueryHandler(on_callback))

    print("📞 PhoneBook Bot running…")
    app.run_polling()
