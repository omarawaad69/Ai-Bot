import asyncio
import logging
import os
import time
import base64
import sqlite3
import subprocess
import json
import glob
from io import BytesIO
from datetime import datetime, timedelta
from collections import defaultdict
from PIL import Image

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from aiogram.types import (
    FSInputFile, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from aiohttp import web

load_dotenv()

# ─────────────────────────────────────────
#  الإعدادات الأساسية
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

router = Router()

ADMIN_USER_ID     = int(os.getenv("ADMIN_USER_ID", "7361263893"))
DEVELOPER_NAME    = "Omar Abd El Gawaad"
DEVELOPER_USERNAME = "@omarawad68"
GEMINI_MODEL      = "gemini-2.0-flash"   # ✅ تصحيح: الموديل الصحيح

# ─────────────────────────────────────────
#  Rate Limiting  ⚡
# ─────────────────────────────────────────
RATE_LIMIT_MESSAGES = 20      # عدد الرسائل المسموح بها
RATE_LIMIT_WINDOW   = 60      # في كل X ثانية
RATE_LIMIT_COOLDOWN = 30      # مدة الحظر المؤقت بالثواني

_rate_data: dict[int, list[float]] = defaultdict(list)
_banned_until: dict[int, float]    = {}

def check_rate_limit(user_id: int) -> tuple[bool, int]:
    """
    يرجع (مسموح, ثواني_الانتظار).
    True  = المستخدم مسموح له بالإرسال.
    False = يجب الانتظار.
    """
    now = time.time()

    # هل هو محظور مؤقتاً؟
    if user_id in _banned_until:
        remaining = int(_banned_until[user_id] - now)
        if remaining > 0:
            return False, remaining
        del _banned_until[user_id]

    # نظّف الطوابع القديمة
    _rate_data[user_id] = [t for t in _rate_data[user_id] if now - t < RATE_LIMIT_WINDOW]
    _rate_data[user_id].append(now)

    if len(_rate_data[user_id]) > RATE_LIMIT_MESSAGES:
        _banned_until[user_id] = now + RATE_LIMIT_COOLDOWN
        _rate_data[user_id].clear()
        return False, RATE_LIMIT_COOLDOWN

    return True, 0


# ─────────────────────────────────────────
#  System Prompt
# ─────────────────────────────────────────
SYSTEM_PROMPT = """
أنت "مستشار الذكاء الاصطناعي الخارق". تجمع بين خبير موسوعي ومبرمج عبقري.
هدفك تقديم إجابات دقيقة واحترافية في كل المجالات.

قواعدك الصارمة:
0. اللغة التلقائية: يجب عليك الرد بنفس لغة سؤال المستخدم.
1. ممنوع المقدمات: لا تبدأ بـ"أهلاً بك"، "بصفتي...". ابدأ الإجابة مباشرة.
2. تنسيق الردود: للقوائم استخدم "- ". للكود استخدم ``` مع تحديد اللغة. لا تستخدم "#" أبداً.
3. الهوية المزدوجة: إذا كان السؤال برمجياً ركز على الكود، وإذا كان عاماً قدم شرحاً مباشراً.
4. تحليل المستندات والصور: ابدأ مباشرة بتحليل المحتوى بدون مقدمات.
5. الأمان: ترفض أي طلب لإنشاء محتوى ضار أو غير قانوني.
"""

# ─────────────────────────────────────────
#  قاعدة البيانات  🗄️
# ─────────────────────────────────────────
DB_PATH = "bot_stats.db"

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """إنشاء الجداول إذا لم تكن موجودة"""
    try:
        with get_db() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id      INTEGER PRIMARY KEY,
                    username     TEXT,
                    first_name   TEXT,
                    last_name    TEXT,
                    joined_date  TEXT,
                    last_active  TEXT,
                    total_messages INTEGER DEFAULT 0,
                    is_banned    INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS daily_stats (
                    date         TEXT PRIMARY KEY,
                    active_users INTEGER DEFAULT 0,
                    total_messages INTEGER DEFAULT 0
                );

                -- ✅ جدول جديد: ذاكرة المحادثات الدائمة
                CREATE TABLE IF NOT EXISTS conversations (
                    user_id  INTEGER NOT NULL,
                    role     TEXT    NOT NULL,
                    content  TEXT    NOT NULL,
                    ts       TEXT    DEFAULT (datetime('now')),
                    PRIMARY KEY (user_id, ts, role)
                );

                CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, ts);
            """)
        logger.info("Database initialized ✅")
    except Exception as e:
        logger.error(f"DB init error: {e}")

def update_user_activity(user: types.User):
    try:
        now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        today = datetime.now().strftime("%Y-%m-%d")
        with get_db() as conn:
            conn.execute("""
                INSERT INTO users (user_id, username, first_name, last_name, joined_date, last_active, total_messages)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    last_active=excluded.last_active,
                    total_messages=users.total_messages + 1
            """, (user.id, user.username, user.first_name, user.last_name, now, now))
            conn.execute("""
                INSERT INTO daily_stats (date, active_users, total_messages) VALUES (?, 1, 1)
                ON CONFLICT(date) DO UPDATE SET
                    active_users=active_users + 1,
                    total_messages=total_messages + 1
            """, (today,))
    except Exception as e:
        logger.error(f"User activity error: {e}")

def is_user_banned(user_id: int) -> bool:
    try:
        with get_db() as conn:
            row = conn.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,)).fetchone()
            return bool(row and row["is_banned"])
    except Exception:
        return False

# ─────────────────────────────────────────
#  ذاكرة المحادثة الدائمة  🧠
# ─────────────────────────────────────────
MAX_HISTORY = 20   # عدد الرسائل المحفوظة per user

def load_conversation(user_id: int) -> list[dict]:
    """تحميل آخر MAX_HISTORY رسالة من DB"""
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT role, content FROM conversations
                WHERE user_id = ?
                ORDER BY ts DESC
                LIMIT ?
            """, (user_id, MAX_HISTORY)).fetchall()
        rows = list(reversed(rows))
        return [{"role": r["role"], "parts": [{"text": r["content"]}]} for r in rows]
    except Exception as e:
        logger.error(f"Load conversation error: {e}")
        return []

def save_message(user_id: int, role: str, content: str):
    """حفظ رسالة واحدة في DB"""
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO conversations (user_id, role, content) VALUES (?, ?, ?)",
                (user_id, role, content)
            )
            # احتفظ فقط بآخر MAX_HISTORY رسالة
            conn.execute("""
                DELETE FROM conversations
                WHERE user_id = ? AND ts NOT IN (
                    SELECT ts FROM conversations
                    WHERE user_id = ?
                    ORDER BY ts DESC
                    LIMIT ?
                )
            """, (user_id, user_id, MAX_HISTORY))
    except Exception as e:
        logger.error(f"Save message error: {e}")

def clear_conversation(user_id: int):
    try:
        with get_db() as conn:
            conn.execute("DELETE FROM conversations WHERE user_id=?", (user_id,))
    except Exception as e:
        logger.error(f"Clear conversation error: {e}")


# ─────────────────────────────────────────
#  Gemini Client  🤖
# ─────────────────────────────────────────
class AsyncGeminiClient:
    def __init__(self, model: str = GEMINI_MODEL):
        self.client = genai.Client()
        self.model  = model

    async def generate(self, prompt: str, user_id: int) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_generate, prompt, user_id)

    def _sync_generate(self, prompt: str, user_id: int) -> str:
        # تحميل التاريخ من DB
        history = load_conversation(user_id)

        history.append({"role": "user", "parts": [{"text": prompt}]})

        full_context = [
            {"role": "user",  "parts": [{"text": "أنت مستشار ذكي. تذكر محادثتنا."}]},
            {"role": "model", "parts": [{"text": "حسناً، سأتذكر محادثتنا."}]},
            *history
        ]

        for attempt in range(3):
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=full_context,
                    config=genai_types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT)
                )
                reply = response.text

                # حفظ الرسالة والرد في DB
                save_message(user_id, "user",  prompt)
                save_message(user_id, "model", reply)

                return reply
            except Exception as e:
                logger.error(f"Gemini error (attempt {attempt + 1}): {e}")
                if attempt < 2:
                    time.sleep(1)
                else:
                    return "⚠️ عذراً، حدث خطأ مؤقت. حاول مرة أخرى."

    async def generate_with_media(self, prompt: str, media_parts: list) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_generate_with_media, prompt, media_parts)

    def _sync_generate_with_media(self, prompt: str, media_parts: list) -> str:
        for attempt in range(3):
            try:
                contents = media_parts + [{"text": prompt}]
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=genai_types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT)
                )
                return response.text
            except Exception as e:
                logger.error(f"Gemini media error (attempt {attempt + 1}): {e}")
                if attempt < 2:
                    time.sleep(1)
                else:
                    return "⚠️ عذراً، حدث خطأ مؤقت. حاول مرة أخرى."


gemini_client = AsyncGeminiClient()

# حالة المستخدمين في الذاكرة (مؤقتة - لا تحتاج DB)
user_conversion_choice: dict[int, tuple] = {}
user_pending_file: dict[int, dict]       = {}


# ─────────────────────────────────────────
#  أدوات الملفات  📁
# ─────────────────────────────────────────
def convert_image_to_png(image_bytes: bytes) -> tuple[bytes, str]:
    try:
        img = Image.open(BytesIO(image_bytes))
        fmt = img.format
        if fmt not in ["JPEG", "PNG", "GIF"]:
            buf = BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            return buf.getvalue(), "image/png"
        mime = f"image/{fmt.lower()}"
        return image_bytes, "image/jpeg" if mime == "image/jpg" else mime
    except Exception:
        return image_bytes, "image/jpeg"


def create_docx_file(text: str, filepath: str):
    from docx import Document
    doc = Document()
    doc.add_heading("مستند تم إنشاؤه بواسطة البوت", level=1)
    for line in text.strip().split("\n"):
        doc.add_paragraph(line)
    doc.save(filepath)


def create_pdf_file(text: str, filepath: str):
    docx_path = filepath.replace(".pdf", ".docx")
    create_docx_file(text, docx_path)
    run_libreoffice(["--convert-to", "pdf", "--outdir", os.path.dirname(filepath) or "/tmp", docx_path])
    if os.path.exists(docx_path):
        os.remove(docx_path)


def create_excel_file(text: str, filepath: str):
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "البيانات"

    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]

    header_font      = Font(name="Arial", size=14, bold=True, color="FFFFFF")
    header_fill      = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
    header_alignment = Alignment(horizontal="center", vertical="center")
    cell_font        = Font(name="Arial", size=12)
    cell_alignment   = Alignment(horizontal="center", vertical="center")
    thin_border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin")
    )

    if len(lines) >= 2:
        headers   = [h.strip() for h in lines[0].replace("،", ",").split(",")]
        data_rows = [[c.strip() for c in l.replace("،", ",").split(",")] for l in lines[1:]]

        if headers:
            ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
        title_cell = ws["A1"]
        title_cell.value     = "مستند تم إنشاؤه بواسطة البوت"
        title_cell.font      = Font(name="Arial", size=16, bold=True, color="2F5496")
        title_cell.alignment = Alignment(horizontal="center", vertical="center")

        for ci, header in enumerate(headers, 1):
            cell            = ws.cell(row=2, column=ci, value=header)
            cell.font       = header_font
            cell.fill       = header_fill
            cell.alignment  = header_alignment
            cell.border     = thin_border

        for ri, row_data in enumerate(data_rows, 3):
            for ci, value in enumerate(row_data, 1):
                cell           = ws.cell(row=ri, column=ci, value=value)
                cell.font      = cell_font
                cell.alignment = cell_alignment
                cell.border    = thin_border
                if ri % 2 == 0:
                    cell.fill = PatternFill(start_color="D6E4F0", end_color="D6E4F0", fill_type="solid")

        for col in ws.columns:
            max_length = max((len(str(c.value)) for c in col if c.value), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(max_length + 4, 50)

        if headers:
            ws.auto_filter.ref = ws.dimensions
    else:
        ws["A1"] = "النص المحول"
        ws["A1"].font = Font(name="Arial", size=14, bold=True, color="2F5496")
        for ri, line in enumerate(lines, 2):
            ws.cell(row=ri, column=1, value=line)
        ws.column_dimensions["A"].width = 50

    wb.save(filepath)


def run_libreoffice(args: list, timeout: int = 60) -> subprocess.CompletedProcess:
    """تشغيل LibreOffice بالإعدادات الصحيحة للصلاحيات"""
    full_args = ["libreoffice", "--headless", "-env:UserInstallation=file:///tmp/libreoffice"]
    if any(a.lower().endswith(".pdf") and os.path.exists(a) for a in args):
        full_args.append('--infilter="writer_pdf_import"')
    full_args.extend(args)
    return subprocess.run(
        full_args,
        capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "HOME": "/tmp", "USERPROFILE": "/tmp"}
    )


def convert_pdf_to_excel(input_path: str, output_path: str):
    """تحويل PDF إلى Excel مع دعم النص العربي"""
    import pdfplumber
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    import arabic_reshaper
    from bidi.algorithm import get_display

    def fix_arabic(text) -> str:
        if not text:
            return ""
        try:
            return get_display(arabic_reshaper.reshape(str(text)))
        except Exception:
            return str(text)

    wb = Workbook()
    ws = wb.active
    ws.title = "PDF Data"
    ws.sheet_view.rightToLeft = True

    header_font  = Font(name="Arial", size=14, bold=True, color="FFFFFF")
    header_fill  = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
    cell_font    = Font(name="Arial", size=12)
    cell_align   = Alignment(horizontal="center", vertical="center")
    thin_border  = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin")
    )

    try:
        with pdfplumber.open(input_path) as pdf:
            current_row = 1
            for page in pdf.pages:
                tables = page.extract_tables()
                if tables:
                    for table in tables:
                        for row in (table or []):
                            for ci, val in enumerate(row, 1):
                                cell           = ws.cell(row=current_row, column=ci, value=fix_arabic(val))
                                cell.font      = cell_font
                                cell.alignment = cell_align
                                cell.border    = thin_border
                            current_row += 1
                        current_row += 1
                else:
                    text = page.extract_text()
                    if text:
                        for line in text.split("\n"):
                            c           = ws.cell(row=current_row, column=1, value=fix_arabic(line))
                            c.font      = cell_font
                            c.alignment = cell_align
                            current_row += 1
    except Exception as e:
        logger.error(f"PDF→Excel extraction error: {e}")

    for col in ws.columns:
        max_length = max((len(str(c.value)) for c in col if c.value), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_length + 4, 50)

    wb.save(output_path)


# ─────────────────────────────────────────
#  دالة مشتركة لتحويل الملفات  🔄  (✅ إزالة التكرار)
# ─────────────────────────────────────────
async def do_file_conversion(message: types.Message, file_bytes: bytes, fname: str, target: str):
    """تحويل أي ملف إلى الصيغة المطلوبة وإرساله"""
    user_id  = message.from_user.id
    inpath   = f"/tmp/{user_id}_{fname}"
    base     = os.path.splitext(fname)[0]
    out_path = f"/tmp/{base}.{target}"

    try:
        with open(inpath, "wb") as f:
            f.write(file_bytes)

        if target == "xlsx" and inpath.lower().endswith(".pdf"):
            convert_pdf_to_excel(inpath, out_path)
        else:
            run_libreoffice(["--convert-to", target, "--outdir", "/tmp/", inpath])

        # ابحث عن الملف المحوَّل
        result_path = None
        if os.path.exists(out_path) and os.path.getsize(out_path) > 100:
            result_path = out_path
        else:
            for pf in glob.glob(f"/tmp/*.{target}"):
                if os.path.getsize(pf) > 100:
                    result_path = pf
                    break

        if result_path:
            await message.reply_document(
                FSInputFile(result_path),
                caption=f"✅ تم التحويل إلى {target.upper()} بنجاح!"
            )
        else:
            await message.reply("❌ فشل التحويل. تأكد من أن الملف سليم وغير مشفر.")

    except Exception as e:
        logger.error(f"File conversion error ({fname} → {target}): {e}")
        await message.reply("❌ حدث خطأ أثناء التحويل.")
    finally:
        for p in [inpath, out_path]:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


# ─────────────────────────────────────────
#  أدوات مساعدة للـ UI
# ─────────────────────────────────────────
def get_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💬 ابدأ محادثة"),    KeyboardButton(text="🖼️ تحليل صورة")],
            [KeyboardButton(text="📄 تحويل نص لملف"),  KeyboardButton(text="📊 تحويل لإكسيل")],
            [KeyboardButton(text="🎤 إرسال صوت"),       KeyboardButton(text="🌐 ترجمة فورية")],
            [KeyboardButton(text="🔄 تحويل ملفات"),     KeyboardButton(text="👨‍💻 تواصل مع المبرمج")]
        ],
        resize_keyboard=True,
        input_field_placeholder="اختر من القائمة..."
    )

def get_conversion_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📄 Word → PDF",   callback_data="convert_word2pdf"),
            InlineKeyboardButton(text="📄 PDF → Word",   callback_data="convert_pdf2word")
        ],
        [
            InlineKeyboardButton(text="📊 Excel → PDF",  callback_data="convert_excel2pdf"),
            InlineKeyboardButton(text="📊 PDF → Excel",  callback_data="convert_pdf2excel")
        ],
        [
            InlineKeyboardButton(text="📊 Excel → Word", callback_data="convert_excel2word"),
            InlineKeyboardButton(text="📄 Word → Excel", callback_data="convert_word2excel")
        ],
        [
            InlineKeyboardButton(text="🔄 أي صيغة لأي صيغة", callback_data="convert_any")
        ]
    ])


def detect_conversion_intent(text: str):
    """يكشف نية التحويل ويرجع (نوع_الملف | كود_الحالة, المحتوى)"""
    t = text.lower()

    patterns = {
        "excel": [
            "حولي النص التالي لملف اكسيل", "حولي لملف اكسيل", "حول لملف اكسيل",
            "ملف اكسيل", "ملف excel", "اكسيل", "xlsx",
            "اعملي ملف اكسيل", "اعمل ملف excel",
        ],
        "docx": [
            "حولي النص التالي لملف وورد", "حولي لملف وورد", "حول لملف وورد",
            "ملف وورد", "ملف word", "وورد", "docx",
            "اعملي ملف وورد", "اعمل ملف word",
        ],
        "pdf": [
            "حولي النص التالي لملف pdf", "حولي لملف pdf", "حول لملف pdf",
            "ملف pdf", "بي دي اف",
            "اعملي ملف pdf", "اعمل ملف بي دي اف",
        ],
    }

    prefixes = ["حولي", "حول", "حوّل", "خلي", "اعمل", "سوي", "ابعتلي", "انزلي", "حملي"]

    for fmt, plist in patterns.items():
        for p in plist:
            if p in t:
                idx     = t.find(p)
                content = text[idx + len(p):].strip()
                if not content:
                    content = text[:idx].strip()
                    for prefix in prefixes:
                        if content.startswith(prefix):
                            content = content[len(prefix):].strip()
                            break
                if content:
                    return fmt, content
                else:
                    return f"{fmt.upper()}_NEED_TEXT", ""

    return None, None


# ─────────────────────────────────────────
#  Middleware: Rate Limit تلقائي
# ─────────────────────────────────────────
async def rate_limit_check(message: types.Message) -> bool:
    """يرجع True إذا يجب إيقاف المعالجة (تجاوز الحد)"""
    user_id = message.from_user.id
    if user_id == ADMIN_USER_ID:
        return False   # الأدمن مستثنى

    allowed, wait_sec = check_rate_limit(user_id)
    if not allowed:
        await message.reply(
            f"⏳ أرسلت رسائل كثيرة جداً. انتظر **{wait_sec} ثانية** ثم حاول مرة أخرى.",
            parse_mode="Markdown"
        )
        return True
    return False


# ─────────────────────────────────────────
#  أوامر البوت  /commands
# ─────────────────────────────────────────
@router.message(Command("start"))
async def cmd_start(message: types.Message):
    update_user_activity(message.from_user)
    if is_user_banned(message.from_user.id):
        return await message.reply("⛔ تم حظرك من استخدام هذا البوت.")

    await message.answer(
        "🎉 *أهلاً بك! أنا مستشار الذكاء الاصطناعي الخارق.*\n\n"
        "✨ *ماذا يمكنني أن أفعل لك؟*\n"
        "- الإجابة عن أي سؤال\n"
        "- كتابة وشرح الأكواد البرمجية\n"
        "- تحويل النصوص إلى Word أو PDF أو Excel\n"
        "- تحويل الملفات بين الصيغ\n"
        "- تحليل الصور والمستندات\n"
        "- الاستماع إلى الرسائل الصوتية\n"
        "- الترجمة الفورية لأي لغة\n"
        "- تصميم برومبت احترافي للصور\n\n"
        "💬 *تحدث معي طبيعياً وسأفهمك!*\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👨‍💻 المبرمج: {DEVELOPER_NAME}\n"
        "━━━━━━━━━━━━━━━━━━",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )


@router.message(Command("reset"))
async def cmd_reset(message: types.Message):
    update_user_activity(message.from_user)
    clear_conversation(message.from_user.id)
    await message.answer("🔄 تم مسح سياق المحادثة بنجاح.")


@router.message(Command("translate"))
async def cmd_translate(message: types.Message):
    update_user_activity(message.from_user)
    await message.answer(
        "🌐 *الترجمة الفورية*\n\n"
        "أرسل النص بهذا الشكل:\n"
        "`ترجم إلى الفرنسية: مرحباً، كيف حالك؟`\n\n"
        "📝 *أمثلة:*\n"
        "- ترجم إلى الإنجليزية: النص\n"
        "- ترجم إلى الإسبانية: النص\n"
        "- ترجم إلى الألمانية: النص",
        parse_mode="Markdown"
    )


# ─────────────────────────────────────────
#  لوحة الأدمن  👑
# ─────────────────────────────────────────
@router.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_USER_ID:
        return await message.answer("⛔ هذا الأمر متاح فقط لمالك البوت.")

    try:
        with get_db() as conn:
            total_users        = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            total_msgs_all     = conn.execute("SELECT SUM(total_messages) FROM daily_stats").fetchone()[0] or 0
            banned_count       = conn.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]

            today     = datetime.now().strftime("%Y-%m-%d")
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            one_day_ago = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

            today_row     = conn.execute("SELECT active_users, total_messages FROM daily_stats WHERE date=?", (today,)).fetchone()     or (0, 0)
            yesterday_row = conn.execute("SELECT active_users, total_messages FROM daily_stats WHERE date=?", (yesterday,)).fetchone() or (0, 0)
            online_users  = conn.execute("SELECT COUNT(*) FROM users WHERE last_active >= ?", (one_day_ago,)).fetchone()[0]
            recent_users  = conn.execute(
                "SELECT username, first_name, last_active FROM users ORDER BY last_active DESC LIMIT 5"
            ).fetchall()

        recent_text = "\n".join(
            f"  • {'@'+r['username'] if r['username'] else r['first_name']} — {r['last_active'][:16]}"
            for r in recent_users
        )

        await message.answer(
            "📊 *لوحة الإحصائيات*\n\n"
            f"👥 إجمالي المستخدمين: `{total_users}`\n"
            f"🟢 نشط آخر 24 ساعة: `{online_users}`\n"
            f"🔴 غير نشط: `{total_users - online_users}`\n"
            f"🚫 محظورون: `{banned_count}`\n\n"
            f"📅 اليوم: `{today_row[0]}` نشط | `{today_row[1]}` رسالة\n"
            f"📆 أمس:  `{yesterday_row[0]}` نشط | `{yesterday_row[1]}` رسالة\n\n"
            f"💬 إجمالي الرسائل الكلي: `{total_msgs_all}`\n\n"
            f"🕐 *آخر 5 مستخدمين:*\n{recent_text}\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "*الأوامر المتاحة:*\n"
            "/ban `user_id` — حظر مستخدم\n"
            "/unban `user_id` — رفع الحظر\n"
            "/broadcast `النص` — إرسال لكل المستخدمين\n"
            "/users — قائمة آخر المستخدمين",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Admin command error: {e}")
        await message.answer("❌ حدث خطأ في جلب الإحصائيات.")


@router.message(Command("ban"))
async def cmd_ban(message: types.Message):
    if message.from_user.id != ADMIN_USER_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        return await message.reply("الاستخدام: `/ban user_id`", parse_mode="Markdown")
    target_id = int(parts[1].strip())
    try:
        with get_db() as conn:
            conn.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (target_id,))
        await message.reply(f"✅ تم حظر المستخدم `{target_id}`.", parse_mode="Markdown")
    except Exception as e:
        await message.reply(f"❌ خطأ: {e}")


@router.message(Command("unban"))
async def cmd_unban(message: types.Message):
    if message.from_user.id != ADMIN_USER_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        return await message.reply("الاستخدام: `/unban user_id`", parse_mode="Markdown")
    target_id = int(parts[1].strip())
    try:
        with get_db() as conn:
            conn.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (target_id,))
        await message.reply(f"✅ تم رفع الحظر عن `{target_id}`.", parse_mode="Markdown")
    except Exception as e:
        await message.reply(f"❌ خطأ: {e}")


@router.message(Command("users"))
async def cmd_users(message: types.Message):
    if message.from_user.id != ADMIN_USER_ID:
        return
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT user_id, username, first_name, total_messages, last_active FROM users ORDER BY last_active DESC LIMIT 20"
            ).fetchall()
        lines = ["👥 *آخر 20 مستخدم:*\n"]
        for r in rows:
            name = f"@{r['username']}" if r["username"] else r["first_name"] or "مجهول"
            lines.append(f"• `{r['user_id']}` {name} — {r['total_messages']} رسالة")
        await message.reply("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await message.reply(f"❌ خطأ: {e}")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, bot: Bot):
    if message.from_user.id != ADMIN_USER_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("الاستخدام: `/broadcast النص هنا`", parse_mode="Markdown")

    broadcast_text = parts[1].strip()
    sent = failed = 0

    try:
        with get_db() as conn:
            user_ids = [r[0] for r in conn.execute("SELECT user_id FROM users WHERE is_banned=0").fetchall()]
    except Exception as e:
        return await message.reply(f"❌ خطأ في جلب المستخدمين: {e}")

    status_msg = await message.reply(f"📤 جارٍ الإرسال لـ {len(user_ids)} مستخدم...")

    for uid in user_ids:
        try:
            await bot.send_message(uid, broadcast_text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)   # تجنب flood

    await status_msg.edit_text(
        f"✅ *اكتمل الإرسال*\n\n"
        f"✔️ تم الإرسال: `{sent}`\n"
        f"❌ فشل: `{failed}`",
        parse_mode="Markdown"
    )


# ─────────────────────────────────────────
#  Callback: اختيار التحويل
# ─────────────────────────────────────────
CONVERSION_MAP = {
    "convert_word2pdf":  ("docx", "pdf",  "Word → PDF"),
    "convert_pdf2word":  ("pdf",  "docx", "PDF → Word"),
    "convert_excel2pdf": ("xlsx", "pdf",  "Excel → PDF"),
    "convert_pdf2excel": ("pdf",  "xlsx", "PDF → Excel"),
    "convert_excel2word":("xlsx", "docx", "Excel → Word"),
    "convert_word2excel":("docx", "xlsx", "Word → Excel"),
}

@router.callback_query()
async def handle_conversion_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    data    = callback.data

    if data == "convert_any":
        user_conversion_choice[user_id] = ("any", None, "أي صيغة لأي صيغة")
        await callback.message.answer("📁 *أرسل الملف الذي تريد تحويله مع كتابة الصيغة المطلوبة في التعليق.*", parse_mode="Markdown")
        await callback.answer("تم ✅")
        return

    if data in CONVERSION_MAP:
        source, target, label = CONVERSION_MAP[data]
        user_conversion_choice[user_id] = (source, target, label)
        await callback.message.answer(
            f"📁 *{label}*\n\nأرسل ملف *{source.upper()}* ليتم تحويله إلى *{target.upper()}*.",
            parse_mode="Markdown"
        )
        await callback.answer("تم ✅")


# ─────────────────────────────────────────
#  أزرار القائمة الرئيسية
# ─────────────────────────────────────────
BUTTON_TEXTS = {
    "💬 ابدأ محادثة", "🖼️ تحليل صورة", "📄 تحويل نص لملف",
    "📊 تحويل لإكسيل", "🎤 إرسال صوت", "👨‍💻 تواصل مع المبرمج",
    "🔄 تحويل ملفات",  "🌐 ترجمة فورية"
}

@router.message(F.text.in_(BUTTON_TEXTS))
async def handle_buttons(message: types.Message):
    update_user_activity(message.from_user)

    responses = {
        "💬 ابدأ محادثة":    "📝 أنا جاهز! أرسل سؤالك أو طلبك وسأجيبك فوراً.",
        "🖼️ تحليل صورة":    "🖼️ أرسل لي الصورة التي تريد تحليلها.",
        "📄 تحويل نص لملف": (
            "📄 أرسل لي النص الذي تريد تحويله.\n\n"
            "• *وورد:* حولي النص دا لملف وورد: ...\n"
            "• *PDF:*  حولي النص دا لملف PDF: ...\n"
            "• *اكسيل:* حولي النص دا لملف اكسيل: ..."
        ),
        "📊 تحويل لإكسيل":  (
            "📊 أرسل لي النص الذي تريد تحويله إلى Excel.\n\n"
            "مثال: *حولي النص دا لملف اكسيل: الاسم, العمر, المدينة\nأحمد, 25, القاهرة*"
        ),
        "🎤 إرسال صوت":     (
            "🎤 أرسل لي رسالة صوتية وسأقوم بما يلي:\n\n"
            "1️⃣ تحويلها إلى نص مكتوب\n"
            "2️⃣ الرد على محتواها\n"
            "3️⃣ يمكنك طلب إنشاء ملف Word/PDF/Excel من النص"
        ),
        "👨‍💻 تواصل مع المبرمج": (
            f"👨‍💻 *المبرمج:* {DEVELOPER_NAME}\n\n"
            f"📧 *للتواصل:* {DEVELOPER_USERNAME}"
        ),
        "🌐 ترجمة فورية":   (
            "🌐 *الترجمة الفورية*\n\n"
            "أرسل النص بهذا الشكل:\n"
            "`ترجم إلى الفرنسية: مرحباً، كيف حالك؟`\n\n"
            "أمثلة:\n"
            "- ترجم إلى الإنجليزية: النص\n"
            "- ترجم إلى الإسبانية: النص"
        ),
    }

    if message.text == "🔄 تحويل ملفات":
        await message.answer(
            "🔄 *اختر نوع التحويل:*",
            parse_mode="Markdown",
            reply_markup=get_conversion_keyboard()
        )
        return

    text = responses.get(message.text, "")
    if text:
        await message.answer(text, parse_mode="Markdown")


# ─────────────────────────────────────────
#  معالج الرسائل النصية
# ─────────────────────────────────────────
@router.message(F.text)
async def handle_message(message: types.Message):
    if message.text in BUTTON_TEXTS:
        return

    update_user_activity(message.from_user)

    # ✅ Rate Limit
    if await rate_limit_check(message):
        return

    # ✅ حظر
    if is_user_banned(message.from_user.id):
        return await message.reply("⛔ تم حظرك من استخدام هذا البوت.")

    user_id   = message.from_user.id
    user_text = message.text
    text_lower = user_text.lower()

    # ── حالة انتظار اختيار صيغة الملف ──
    if user_id in user_pending_file:
        fmt_map = {"pdf": "pdf", "word": "docx", "docx": "docx", "excel": "xlsx", "xlsx": "xlsx"}
        chosen = fmt_map.get(text_lower.strip())
        if chosen:
            pending = user_pending_file.pop(user_id)
            await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_document")
            await do_file_conversion(message, pending["file_bytes"], pending["filename"], chosen)
            return

    # ── كشف نية التحويل ──
    intent, content = detect_conversion_intent(user_text)

    need_text_map = {
        "EXCEL_NEED_TEXT": "📊 ما هو النص الذي تريد تحويله إلى Excel؟",
        "WORD_NEED_TEXT":  "📝 ما هو النص الذي تريد تحويله إلى Word؟",
        "PDF_NEED_TEXT":   "📕 ما هو النص الذي تريد تحويله إلى PDF؟",
    }
    if intent in need_text_map:
        return await message.reply(need_text_map[intent])

    if intent == "excel" and content:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_document")
        try:
            path = f"/tmp/{user_id}_doc.xlsx"
            create_excel_file(content, path)
            await message.reply_document(FSInputFile(path), caption="📊 ملف Excel جاهز!")
            os.remove(path)
        except Exception as e:
            logger.error(f"Excel error: {e}")
            await message.reply("❌ حدث خطأ في إنشاء ملف Excel.")
        return

    if intent == "docx" and content:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_document")
        try:
            path = f"/tmp/{user_id}_doc.docx"
            create_docx_file(content, path)
            await message.reply_document(FSInputFile(path), caption="📄 ملف Word جاهز!")
            os.remove(path)
        except Exception as e:
            logger.error(f"Word error: {e}")
            await message.reply("❌ حدث خطأ في إنشاء ملف Word.")
        return

    if intent == "pdf" and content:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_document")
        try:
            path = f"/tmp/{user_id}_doc.pdf"
            create_pdf_file(content, path)
            await message.reply_document(FSInputFile(path), caption="📕 ملف PDF جاهز!")
            os.remove(path)
        except Exception as e:
            logger.error(f"PDF error: {e}")
            await message.reply("❌ حدث خطأ في إنشاء ملف PDF.")
        return

    # ── توليد برومبت صورة ──
    image_keywords = ["اعملي صورة", "اعمل صورة", "ارسم", "صمملي", "تخيل", "صورلي",
                      "توليد صورة", "انشاء صورة", "صمم صورة"]
    if any(kw in text_lower for kw in image_keywords):
        await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        prompt_req = (
            f"حوّل الطلب التالي إلى برومبت إبداعي واحترافي باللغة العربية لتوليد الصور بالذكاء الاصطناعي. "
            f"أضف تفاصيل عن الإضاءة والألوان والزاوية والجو العام.\n\n"
            f"طلب المستخدم: {user_text}\n\n"
            f"اكتب فقط نص البرومبت بدون أي مقدمات أو شرح."
        )
        generated = await gemini_client.generate(prompt_req, user_id)
        await message.reply(
            f"🎨 *تم تصميم برومبت احترافي لطلبك:*\n\n`{generated}`\n\n"
            "🖼️ يمكنك نسخ هذا النص ولصقه في أي أداة لتوليد الصور.",
            parse_mode="Markdown"
        )
        return

    # ── ترجمة فورية ──
    translate_triggers = ["ترجم إلى", "ترجم الى", "ترجم لـ", "ترجمة إلى", "ترجمة لـ", "translate to"]
    for trigger in translate_triggers:
        if trigger in text_lower:
            idx  = text_lower.find(trigger)
            rest = user_text[idx + len(trigger):].strip()
            if ":" in rest:
                target_lang, text_to_translate = rest.split(":", 1)
                target_lang       = target_lang.strip()
                text_to_translate = text_to_translate.strip()
                if target_lang and text_to_translate:
                    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
                    prompt = f"ترجم النص التالي إلى {target_lang}. أرسل الترجمة فقط:\n\n{text_to_translate}"
                    translation = await gemini_client.generate(prompt, user_id)
                    await message.answer(f"🌐 *الترجمة إلى {target_lang}:*\n\n{translation}", parse_mode="Markdown")
                    return
            else:
                await message.reply(
                    f"🌐 أرسل النص بهذا الشكل:\n`ترجم إلى {rest}: النص هنا`",
                    parse_mode="Markdown"
                )
                return

    # ── رد Gemini العادي ──
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    resp = await gemini_client.generate(user_text, user_id)
    for i in range(0, len(resp), 4000):
        await message.answer(resp[i:i + 4000])


# ─────────────────────────────────────────
#  معالج الصور
# ─────────────────────────────────────────
@router.message(F.photo)
async def handle_photo(message: types.Message, bot: Bot):
    update_user_activity(message.from_user)
    if await rate_limit_check(message):
        return
    if is_user_banned(message.from_user.id):
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        photo     = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        bio       = BytesIO()
        await bot.download_file(file_info.file_path, bio)
        bio.seek(0)
        img_bytes, mime = convert_image_to_png(bio.read())
        b64     = base64.b64encode(img_bytes).decode()
        caption = message.caption or "حلل هذه الصورة وصفها بالتفصيل."
        resp    = await gemini_client.generate_with_media(caption, [{"inline_data": {"mime_type": mime, "data": b64}}])
        for i in range(0, len(resp), 4000):
            await message.reply(resp[i:i + 4000])
    except Exception as e:
        logger.error(f"Photo error: {e}")
        await message.reply("⚠️ عذراً، حدث خطأ أثناء تحليل الصورة.")


# ─────────────────────────────────────────
#  معالج المستندات
# ─────────────────────────────────────────
@router.message(F.document)
async def handle_document(message: types.Message, bot: Bot):
    update_user_activity(message.from_user)
    if await rate_limit_check(message):
        return
    if is_user_banned(message.from_user.id):
        return

    doc     = message.document
    fname   = doc.file_name or "document"
    mime    = doc.mime_type or ""
    cap     = message.caption or ""
    user_id = message.from_user.id

    # ── تحويل ملف بناءً على اختيار سابق ──
    if user_id in user_conversion_choice:
        source, target, label = user_conversion_choice[user_id]

        # اختيار الهدف تلقائياً من التعليق
        if source == "any" and not target:
            c = cap.lower()
            if   "pdf"  in c: target = "pdf"
            elif "word" in c or "docx" in c: target = "docx"
            elif "excel" in c or "xlsx" in c: target = "xlsx"

        if target:
            await bot.send_chat_action(chat_id=message.chat.id, action="upload_document")
            file_info  = await bot.get_file(doc.file_id)
            file_bytes = await bot.download_file(file_info.file_path)
            del user_conversion_choice[user_id]
            await do_file_conversion(message, file_bytes.read(), fname, target)
            return
        else:
            # لم يُحدَّد الهدف، احفظ الملف وانتظر
            file_info  = await bot.get_file(doc.file_id)
            file_bytes = await bot.download_file(file_info.file_path)
            user_pending_file[user_id] = {"file_bytes": file_bytes.read(), "filename": fname}
            await message.reply("📝 *إلى أي صيغة تريد التحويل؟*\n• pdf\n• word\n• excel", parse_mode="Markdown")
            return

    # ── كشف نية التحويل من التعليق ──
    if cap:
        c = cap.lower()
        target = None
        if "pdf" in c: target = "pdf"
        elif "word" in c or "docx" in c: target = "docx"
        elif "excel" in c or "xlsx" in c: target = "xlsx"
        elif "ppt" in c or "pptx" in c: target = "pptx"

        if target:
            await bot.send_chat_action(chat_id=message.chat.id, action="upload_document")
            file_info  = await bot.get_file(doc.file_id)
            file_bytes = await bot.download_file(file_info.file_path)
            await do_file_conversion(message, file_bytes.read(), fname, target)
            return

    # ── تحليل المستند بالذكاء الاصطناعي ──
    supported_mimes = {
        "application/pdf", "text/plain",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "text/csv"
    }
    if mime not in supported_mimes:
        return await message.reply("⚠️ نوع الملف غير مدعوم للتحليل.")

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        info = await bot.get_file(doc.file_id)
        bio  = BytesIO()
        await bot.download_file(info.file_path, bio)
        bio.seek(0)
        fb   = bio.read()
        text = ""

        if mime in ("text/plain", "text/csv"):
            text = fb.decode("utf-8", errors="ignore")
        elif mime == "application/pdf":
            import PyPDF2
            reader = PyPDF2.PdfReader(BytesIO(fb))
            for page in reader.pages:
                text += page.extract_text() or ""
        elif "word" in mime:
            import docx as dx
            doc_obj = dx.Document(BytesIO(fb))
            text = "\n".join(p.text for p in doc_obj.paragraphs)
        elif "excel" in mime or "spreadsheet" in mime:
            from openpyxl import load_workbook
            wb = load_workbook(BytesIO(fb), read_only=True)
            ws = wb.active
            text = "\n".join(" | ".join(str(c) if c else "" for c in row) for row in ws.iter_rows(values_only=True))

        if not text.strip():
            return await message.reply("⚠️ لم أستطع استخراج نص من هذا الملف.")

        prompt = f"حلل هذا المستند ({fname}). {cap or 'قدم ملخصاً شاملاً.'}\n\n{text[:10000]}"
        resp   = await gemini_client.generate(prompt, user_id)
        for i in range(0, len(resp), 4000):
            await message.reply(resp[i:i + 4000])
    except Exception as e:
        logger.error(f"Document analysis error: {e}")
        await message.reply("⚠️ عذراً، حدث خطأ أثناء تحليل المستند.")


# ─────────────────────────────────────────
#  معالج الصوت
# ─────────────────────────────────────────
@router.message(F.voice)
async def handle_voice(message: types.Message, bot: Bot):
    update_user_activity(message.from_user)
    if await rate_limit_check(message):
        return
    if is_user_banned(message.from_user.id):
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    user_id  = message.from_user.id
    ogg_path = f"/tmp/{user_id}_voice.ogg"
    wav_path = f"/tmp/{user_id}_voice.wav"

    try:
        file_info = await bot.get_file(message.voice.file_id)
        bio       = BytesIO()
        await bot.download_file(file_info.file_path, bio)
        bio.seek(0)
        with open(ogg_path, "wb") as f:
            f.write(bio.read())

        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", ogg_path, "-ar", "16000", "-ac", "1", wav_path],
                check=True, capture_output=True, timeout=30
            )
        except Exception as e:
            logger.error(f"ffmpeg error: {e}")
            return await message.reply("🎤 عذراً، فشل تحويل الصوت.")

        import speech_recognition as sr
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio = recognizer.record(source)

        text = None
        for lang in ["ar-AR", "en-US", ""]:
            try:
                text = recognizer.recognize_google(audio, language=lang) if lang else recognizer.recognize_google(audio)
                if text:
                    break
            except sr.UnknownValueError:
                continue
            except sr.RequestError as e:
                logger.error(f"Google STT error: {e}")
                return await message.reply("⚠️ خدمة التعرف على الصوت غير متاحة حالياً.")

        if not text:
            return await message.reply("🎤 لم أتمكن من فهم الصوت. جرب مرة أخرى بصوت أوضح.")

        await message.reply(f"🎤 *لقد فهمت:* _{text}_", parse_mode="Markdown")
        resp = await gemini_client.generate(text, user_id)
        for i in range(0, len(resp), 4000):
            await message.answer(resp[i:i + 4000])

    except ImportError:
        await message.reply("⚠️ مكتبة التعرف على الصوت غير مثبتة.")
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await message.reply("🎤 عذراً، حدث خطأ أثناء معالجة الصوت.")
    finally:
        for p in [ogg_path, wav_path]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


# ─────────────────────────────────────────
#  Web Server (Health check + Chat API)
# ─────────────────────────────────────────
async def handle_health(request):
    return web.json_response({"status": "ok", "bot": "running", "model": GEMINI_MODEL})

async def handle_web_chat(request):
    try:
        data      = await request.json()
        user_text = data.get("content", "")
        user_id_s = request.headers.get("X-User-Id", "web_user")
        user_id   = hash(user_id_s) % (10**9)   # رقم صحيح ثابت لنفس المستخدم

        if not user_text:
            return web.json_response({"status": "error", "message": "نص فارغ"}, status=400)

        response = await gemini_client.generate(user_text, user_id)
        return web.json_response({"status": "success", "response": response})
    except Exception as e:
        logger.error(f"Web chat error: {e}")
        return web.json_response({"status": "error", "message": "حدث خطأ"}, status=500)

async def init_web_server():
    app = web.Application()
    app.router.add_get("/health",    handle_health)
    app.router.add_post("/api/chat", handle_web_chat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8000)))
    await site.start()
    logger.info(f"Web server started ✅  port={os.getenv('PORT', 8000)}")


# ─────────────────────────────────────────
#  نقطة الانطلاق
# ─────────────────────────────────────────
async def main():
    init_db()
    bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
    dp  = Dispatcher()
    dp.include_router(router)

    logger.info(f"Bot starting with model: {GEMINI_MODEL}")
    await init_web_server()
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
