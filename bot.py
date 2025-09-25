# bot.py — بوت أرقام الهاتف (شبكة أقسام + بحث) — بدون إظهار مصدر الملف + انترو مع معلومات المصمّم
import os, logging, asyncio, traceback, math
from typing import Dict, List, Tuple, Optional
from openpyxl import load_workbook
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.error import RetryAfter, BadRequest, Forbidden, TimedOut, NetworkError

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", BASE)

# أعمدة محتملة
DEPT_CANDIDATES = ["القسم", "قسم", "الاسم", "اسم القسم"]
PHONE_CANDIDATES = ["رقم الهاتف", "الهاتف", "رقم", "موبايل", "Phone"]

# الذاكرة
display_rows: List[Tuple[str, str]] = []   # [(اسم القسم، رقم)]
phonebook: Dict[str, str] = {}             # UPPER(name) -> phone
departments: List[str] = []                # أسماء الأقسام مرتبة
loaded_from: List[str] = []                # لأغراض التشخيص فقط (لا تُعرض للمستخدم)

# واجهة رئيسية
MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📞 أرقام المستشفى")],
        [KeyboardButton("🔍 بحث بالاسم")],
        [KeyboardButton("◀️ رجوع للقائمة")],
    ],
    resize_keyboard=True
)

# إعداد شبكة الأزرار
GRID_COLS = 3
PAGE_SIZE = 24

def norm(s: str) -> str:
    return str(s).replace("\u200f","").replace("\u200e","").replace("\ufeff","").strip()

def up(s: str) -> str:
    return norm(s).upper()

def list_excel_files(folder: str) -> List[str]:
    try:
        return [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(".xlsx")]
    except Exception:
        return []

def read_headers(ws) -> List[str]:
    for row in ws.iter_rows(min_row=1, max_row=1, values_only=True):
        return [norm(c if c is not None else "") for c in row]
    return []

def find_col_idx(headers: List[str], candidates: List[str]) -> Optional[int]:
    H = [up(h) for h in headers]
    C = [up(c) for c in candidates]
    for i, h in enumerate(H):
        if h in C:
            return i
    for i, h in enumerate(H):
        for c in C:
            if c in h:
                return i
    return None

def load_phonebook() -> Tuple[int, str]:
    """يحمل كل ملفات .xlsx في DATA_DIR. يرجّع (عدد السجلات، رسالة)."""
    global display_rows, phonebook, departments, loaded_from
    display_rows, phonebook, departments, loaded_from = [], {}, [], []

    files = list_excel_files(DATA_DIR)
    if not files:
        return 0, f"❌ ماكو أي ملفات .xlsx داخل:\n{DATA_DIR}"

    total = 0
    for path in files:
        try:
            from openpyxl import load_workbook
            wb = load_workbook(path, read_only=True, data_only=True)
            ws = wb.active
            headers = read_headers(ws)
            if not headers:
                wb.close(); continue
            dept_idx = find_col_idx(headers, DEPT_CANDIDATES)
            phone_idx = find_col_idx(headers, PHONE_CANDIDATES)
            if dept_idx is None or phone_idx is None:
                wb.close(); continue

            loaded_from.append(os.path.basename(path))  # للتشخيص فقط

            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row: continue
                dept = norm(row[dept_idx] if dept_idx < len(row) and row[dept_idx] is not None else "")
                phone = norm(row[phone_idx] if phone_idx < len(row) and row[phone_idx] is not None else "")
                if not dept: continue
                display_rows.append((dept, phone))
                phonebook[up(dept)] = phone
                total += 1
            wb.close()
        except Exception as e:
            logging.exception(f"Load error in {path}: {e}")

    display_rows.sort(key=lambda x: x[0])
    departments = [d for (d, _) in display_rows]
    if total == 0:
        return 0, "❌ ما تم تحميل أي سجل. تأكد من أسماء الأعمدة: (القسم/اسم القسم) و(رقم الهاتف/الهاتف/رقم)."
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
    # ملاحظة: لا نعرض أسماء الملفات للمستخدم
    return (
        "👋 أهلاً بك في بوت أرقام المستشفى.\n\n"
        "اختيارات سريعة:\n"
        "• **📞 أرقام المستشفى**: تصفّح الأقسام كأزرار.\n"
        "• **🔍 بحث بالاسم**: اكتب اسم القسم مباشرة (مثال: الطوارئ).\n"
        "• **◀️ رجوع للقائمة**: العودة لهذه الصفحة.\n\n"
        "ℹ️ **طريقة الاستخدام**:\n"
        "1) اضغط «أرقام المستشفى» واختر القسم من المربعات.\n"
        "2) أو اكتب اسم القسم وسيتم عرض رقمه فورًا.\n\n"
        "✨ **تم تصميم البوت من قبل وحدة الكاميرات** (ياسين التميمي)."
    )

def build_dept_grid(page: int = 0) -> InlineKeyboardMarkup:
    total = len(departments)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    end = min(start + PAGE_SIZE, total)
    slice_items = departments[start:end]

    rows, row = [], []
    for i, name in enumerate(slice_items):
        idx = start + i
        row.append(InlineKeyboardButton(name, callback_data=f"dept:{idx}"))
        if len(row) == GRID_COLS:
            rows.append(row); row = []
    if row:
        rows.append(row)

    controls = []
    if pages > 1:
        if page > 0:
            controls.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"page:{page-1}"))
        controls.append(InlineKeyboardButton(f"صفحة {page+1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            controls.append(InlineKeyboardButton("التالي ➡️", callback_data=f"page:{page+1}"))
        rows.append(controls)
    rows.append([InlineKeyboardButton("◀️ رجوع للقائمة", callback_data="home")])

    return InlineKeyboardMarkup(rows)

# أوامر
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_intro_text(), reply_markup=MAIN_KB)

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # تشخيص فقط
    files = list_excel_files(DATA_DIR)
    await safe_reply(update,
        "ℹ️ معلومات تشخيصية:\n"
        f"DATA_DIR: {DATA_DIR}\n"
        f"Found XLSX: {files}\n"
        f"Loaded count: {len(display_rows)}",
        reply_markup=MAIN_KB
    )

async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n, msg = load_phonebook()
    await safe_reply(update, msg, reply_markup=MAIN_KB)

async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = list_excel_files(DATA_DIR)
    lines = [f"DATA_DIR: {DATA_DIR}", f"Found XLSX: {files}"]
    try:
        for p in files:
            from openpyxl import load_workbook
            wb = load_workbook(p, read_only=True, data_only=True)
            ws = wb.active
            headers = read_headers(ws)
            wb.close()
            lines.append(f"{os.path.basename(p)} → headers: {headers}")
    except Exception as e:
        lines.append(f"header-read error: {e}")
    await safe_reply(update, "\n".join(lines), reply_markup=MAIN_KB)

async def list_depts(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    if not departments:
        await safe_reply(update, "❌ لا توجد سجلات محمّلة. استخدم /reload بعد التأكد من ملف الإكسل.", reply_markup=MAIN_KB)
        return
    kb = build_dept_grid(page)
    await update.message.reply_text("اختر القسم من القائمة التالية:", reply_markup=kb)

# الأزرار الرئيسية والبحث النصي
async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = norm(update.message.text or "")
    if txt == "📞 أرقام المستشفى":
        await list_depts(update, context, page=0); return
    if txt == "🔍 بحث بالاسم":
        await safe_reply(update, "✍️ اكتب اسم القسم الآن.", reply_markup=MAIN_KB); return
    if txt == "◀️ رجوع للقائمة":
        await safe_reply(update, build_intro_text(), reply_markup=MAIN_KB); return
    await handle_search(update, context)

async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = up(norm(update.message.text or ""))
    if not q:
        await safe_reply(update, "اكتب اسم القسم للبحث.", reply_markup=MAIN_KB); return
    # تطابق تام
    if q in phonebook:
        num = phonebook[q]
        await safe_reply(update, f"✅ الرقم: {num if num else '—'}", reply_markup=MAIN_KB); return
    # يحتوي
    matches = [(d, phonebook[up(d)]) for (d, _) in display_rows if q in up(d)]
    if matches:
        if len(matches) == 1:
            d, p = matches[0]
            await safe_reply(update, f"✅ {d} — {p if p else '—'}", reply_markup=MAIN_KB)
        else:
            names = "\n".join([f"• {d}" for d, _ in matches])
            await safe_reply(update, "🔎 أقسام مطابقة:\n\n" + names + "\n\nاختر من القائمة أو اكتب الاسم كامل.", reply_markup=MAIN_KB)
        return
    await safe_reply(update, "❌ لم يتم العثور على هذا القسم.", reply_markup=MAIN_KB)

# ردود ضغط الأزرار المضمّنة
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data if q else ""
    try:
        if data.startswith("dept:"):
            idx = int(data.split(":")[1])
            if 0 <= idx < len(departments):
                name = departments[idx]
                number = phonebook.get(up(name), "")
                await q.answer(text=f"{name}: {number if number else '—'}", show_alert=False)
                await q.message.reply_text(f"📞 {name} — {number if number else '—'}")
            else:
                await q.answer("خيار غير صالح.", show_alert=False)

        elif data.startswith("page:"):
            page = int(data.split(":")[1])
            kb = build_dept_grid(page)
            await q.answer()
            await q.message.edit_text("اختر القسم من القائمة التالية:", reply_markup=kb)

        elif data == "home":
            await q.answer()
            await q.message.edit_text(build_intro_text(), reply_markup=None)
            await q.message.reply_text("رجعت إلى القائمة الرئيسية.", reply_markup=MAIN_KB)

        elif data == "noop":
            await q.answer()

        else:
            await q.answer()

    except Exception as e:
        logging.error(f"Callback error: {e}")
        try:
            await q.answer("صار خطأ بسيط، جرّب مرة ثانية.", show_alert=False)
        except:
            pass

# تشغيل
def read_token_from_file() -> Optional[str]:
    tok_path = os.path.join(BASE, "token.txt")
    if os.path.exists(tok_path):
        return open(tok_path, "r", encoding="utf-8").read().strip()
    return None

if __name__ == "__main__":
    cnt, status = load_phonebook()
    logging.info(status)

    token = read_token_from_file()
    if not token:
        print("❌ ضع التوكن في token.txt بجانب bot.py")
        raise SystemExit(1)

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("reload", reload_cmd))
    app.add_handler(CommandHandler("debug", debug_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    app.add_handler(CallbackQueryHandler(on_callback))

    print("📞 PhoneBook Bot (grid) running…")
    app.run_polling()
