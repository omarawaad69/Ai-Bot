import asyncio
import logging
import os
import time
import base64
import sqlite3
import subprocess
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

ADMIN_USER_ID      = int(os.getenv("ADMIN_USER_ID", "7361263893"))
DEVELOPER_NAME     = "Omar Abd El Gawaad"
DEVELOPER_USERNAME = "@omarawad68"
GEMINI_MODEL       = "gemini-3.1-flash-lite-preview"

# ─────────────────────────────────────────
#  Rate Limiting
# ─────────────────────────────────────────
RATE_LIMIT_MESSAGES = 20
RATE_LIMIT_WINDOW   = 60
RATE_LIMIT_COOLDOWN = 30

_rate_data:    dict[int, list[float]] = defaultdict(list)
_banned_until: dict[int, float]       = {}

def check_rate_limit(user_id: int) -> tuple[bool, int]:
    now = time.time()
    if user_id in _banned_until:
        remaining = int(_banned_until[user_id] - now)
        if remaining > 0:
            return False, remaining
        del _banned_until[user_id]
    _rate_data[user_id] = [t for t in _rate_data[user_id] if now - t < RATE_LIMIT_WINDOW]
    _rate_data[user_id].append(now)
    if len(_rate_data[user_id]) > RATE_LIMIT_MESSAGES:
        _banned_until[user_id] = now + RATE_LIMIT_COOLDOWN
        _rate_data[user_id].clear()
        return False, RATE_LIMIT_COOLDOWN
    return True, 0

# ─────────────────────────────────────────
#  Response Cache (لتسريع الردود المتكررة)
# ─────────────────────────────────────────
_response_cache: dict[str, tuple[str, float]] = {}
CACHE_TTL = 300  # 5 دقائق

def get_cached(prompt: str) -> str | None:
    if prompt in _response_cache:
        resp, ts = _response_cache[prompt]
        if time.time() - ts < CACHE_TTL:
            return resp
        del _response_cache[prompt]
    return None

def set_cache(prompt: str, response: str):
    if len(_response_cache) > 200:
        oldest = sorted(_response_cache.items(), key=lambda x: x[1][1])[:50]
        for k, _ in oldest:
            del _response_cache[k]
    _response_cache[prompt] = (response, time.time())

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
#  قاعدة البيانات
# ─────────────────────────────────────────
DB_PATH = "bot_stats.db"

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    try:
        with get_db() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id        INTEGER PRIMARY KEY,
                    username       TEXT,
                    first_name     TEXT,
                    last_name      TEXT,
                    joined_date    TEXT,
                    last_active    TEXT,
                    total_messages INTEGER DEFAULT 0,
                    is_banned      INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS daily_stats (
                    date           TEXT PRIMARY KEY,
                    active_users   INTEGER DEFAULT 0,
                    total_messages INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    user_id  INTEGER NOT NULL,
                    role     TEXT    NOT NULL,
                    content  TEXT    NOT NULL,
                    ts       TEXT    DEFAULT (datetime('now')),
                    PRIMARY KEY (user_id, ts, role)
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER NOT NULL,
                    rating     INTEGER NOT NULL,
                    comment    TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS error_log (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER,
                    error_type TEXT,
                    error_msg  TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
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

def log_error(user_id: int, error_type: str, error_msg: str):
    """حفظ الأخطاء في DB وتنبيه الأدمن"""
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO error_log (user_id, error_type, error_msg) VALUES (?, ?, ?)",
                (user_id, error_type, str(error_msg)[:500])
            )
    except Exception:
        pass

def is_user_banned(user_id: int) -> bool:
    try:
        with get_db() as conn:
            row = conn.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,)).fetchone()
            return bool(row and row["is_banned"])
    except Exception:
        return False

# ─────────────────────────────────────────
#  ذاكرة المحادثة الدائمة
# ─────────────────────────────────────────
MAX_HISTORY = 20

def load_conversation(user_id: int) -> list[dict]:
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT role, content FROM conversations
                WHERE user_id = ?
                ORDER BY ts DESC LIMIT ?
            """, (user_id, MAX_HISTORY)).fetchall()
        rows = list(reversed(rows))
        return [{"role": r["role"], "parts": [{"text": r["content"]}]} for r in rows]
    except Exception as e:
        logger.error(f"Load conversation error: {e}")
        return []

def save_message(user_id: int, role: str, content: str):
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO conversations (user_id, role, content) VALUES (?, ?, ?)",
                (user_id, role, content)
            )
            conn.execute("""
                DELETE FROM conversations
                WHERE user_id = ? AND ts NOT IN (
                    SELECT ts FROM conversations
                    WHERE user_id = ?
                    ORDER BY ts DESC LIMIT ?
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
#  Gemini Client
# ─────────────────────────────────────────
class AsyncGeminiClient:
    def __init__(self, model: str = GEMINI_MODEL):
        self.client = genai.Client()
        self.model  = model

    async def generate(self, prompt: str, user_id: int, use_cache: bool = False) -> str:
        if use_cache:
            cached = get_cached(prompt)
            if cached:
                return cached
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self._sync_generate, prompt, user_id)
        if use_cache and not result.startswith("⚠️"):
            set_cache(prompt, result)
        return result

    def _sync_generate(self, prompt: str, user_id: int) -> str:
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
                save_message(user_id, "user",  prompt)
                save_message(user_id, "model", reply)
                return reply
            except Exception as e:
                logger.error(f"Gemini error (attempt {attempt + 1}): {e}")
                if attempt < 2:
                    time.sleep(1)
                else:
                    log_error(user_id, "gemini_generate", str(e))
                    return "⚠️ عذراً، حدث خطأ مؤقت. حاول مرة أخرى."

    async def generate_with_media(self, prompt: str, media_parts: list) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_generate_with_media, prompt, media_parts)

    def _sync_generate_with_media(self, prompt: str, media_parts: list) -> str:
        for attempt in range(3):
            try:
                parts = []
                for mp in media_parts:
                    if "inline_data" in mp:
                        parts.append(
                            genai_types.Part(
                                inline_data=genai_types.Blob(
                                    mime_type=mp["inline_data"]["mime_type"],
                                    data=base64.b64decode(mp["inline_data"]["data"])
                                )
                            )
                        )
                    elif "text" in mp:
                        parts.append(genai_types.Part(text=mp["text"]))
                parts.append(genai_types.Part(text=prompt))
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=[genai_types.Content(role="user", parts=parts)],
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

user_conversion_choice: dict[int, tuple] = {}
user_pending_file:      dict[int, dict]  = {}
user_awaiting_feedback: dict[int, bool]  = {}
user_awaiting_quiz:     dict[int, bool]  = {}

# ─────────────────────────────────────────
#  أدوات الملفات
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
        title_cell           = ws["A1"]
        title_cell.value     = "مستند تم إنشاؤه بواسطة البوت"
        title_cell.font      = Font(name="Arial", size=16, bold=True, color="2F5496")
        title_cell.alignment = Alignment(horizontal="center", vertical="center")
        for ci, header in enumerate(headers, 1):
            cell           = ws.cell(row=2, column=ci, value=header)
            cell.font      = header_font
            cell.fill      = header_fill
            cell.alignment = header_alignment
            cell.border    = thin_border
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
        ws["A1"]      = "النص المحول"
        ws["A1"].font = Font(name="Arial", size=14, bold=True, color="2F5496")
        for ri, line in enumerate(lines, 2):
            ws.cell(row=ri, column=1, value=line)
        ws.column_dimensions["A"].width = 50
    wb.save(filepath)

def run_libreoffice(args: list, timeout: int = 60) -> subprocess.CompletedProcess:
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
    cell_font   = Font(name="Arial", size=12)
    cell_align  = Alignment(horizontal="center", vertical="center")
    thin_border = Border(
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
        logger.error(f"PDF to Excel error: {e}")
    for col in ws.columns:
        max_length = max((len(str(c.value)) for c in col if c.value), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_length + 4, 50)
    wb.save(output_path)

async def do_file_conversion(message: types.Message, file_bytes: bytes, fname: str, target: str):
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
        log_error(user_id, "file_conversion", str(e))
        await message.reply("❌ حدث خطأ أثناء التحويل.")
    finally:
        for p in [inpath, out_path]:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

# ─────────────────────────────────────────
#  UI Keyboards
# ─────────────────────────────────────────
def get_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💬 ابدأ محادثة"),     KeyboardButton(text="🖼️ تحليل صورة")],
            [KeyboardButton(text="📄 تحويل نص لملف"),   KeyboardButton(text="📊 تحويل لإكسيل")],
            [KeyboardButton(text="🎤 إرسال صوت"),        KeyboardButton(text="🌐 ترجمة فورية")],
            [KeyboardButton(text="🔄 تحويل ملفات"),      KeyboardButton(text="📝 تلخيص نص")],
            [KeyboardButton(text="🎨 برومبت صورة"),      KeyboardButton(text="🧠 اختبار معلومات")],
            [KeyboardButton(text="⭐ تقييم البوت"),      KeyboardButton(text="👨‍💻 تواصل مع المبرمج")]
        ],
        resize_keyboard=True,
        input_field_placeholder="اختر من القائمة أو اكتب مباشرة..."
    )

def get_conversion_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📄 Word → PDF",    callback_data="convert_word2pdf"),
            InlineKeyboardButton(text="📄 PDF → Word",    callback_data="convert_pdf2word")
        ],
        [
            InlineKeyboardButton(text="📊 Excel → PDF",   callback_data="convert_excel2pdf"),
            InlineKeyboardButton(text="📊 PDF → Excel",   callback_data="convert_pdf2excel")
        ],
        [
            InlineKeyboardButton(text="📊 Excel → Word",  callback_data="convert_excel2word"),
            InlineKeyboardButton(text="📄 Word → Excel",  callback_data="convert_word2excel")
        ],
        [
            InlineKeyboardButton(text="🔄 أي صيغة لأي صيغة", callback_data="convert_any")
        ]
    ])

def get_feedback_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⭐",     callback_data="rate_1"),
            InlineKeyboardButton(text="⭐⭐",   callback_data="rate_2"),
            InlineKeyboardButton(text="⭐⭐⭐", callback_data="rate_3"),
        ],
        [
            InlineKeyboardButton(text="⭐⭐⭐⭐",   callback_data="rate_4"),
            InlineKeyboardButton(text="⭐⭐⭐⭐⭐", callback_data="rate_5"),
        ]
    ])

def get_help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💬 المحادثة والأسئلة", callback_data="help_chat"),
            InlineKeyboardButton(text="📁 الملفات والتحويل",  callback_data="help_files")
        ],
        [
            InlineKeyboardButton(text="🌐 الترجمة",           callback_data="help_translate"),
            InlineKeyboardButton(text="🎨 توليد الصور",       callback_data="help_images")
        ],
        [
            InlineKeyboardButton(text="🎤 الصوت",             callback_data="help_voice"),
            InlineKeyboardButton(text="📊 الإحصائيات",        callback_data="help_stats")
        ]
    ])

def detect_conversion_intent(text: str):
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
#  Rate Limit Helper
# ─────────────────────────────────────────
async def rate_limit_check(message: types.Message) -> bool:
    user_id = message.from_user.id
    if user_id == ADMIN_USER_ID:
        return False
    allowed, wait_sec = check_rate_limit(user_id)
    if not allowed:
        await message.reply(
            f"⏳ أرسلت رسائل كثيرة جداً. انتظر *{wait_sec} ثانية* ثم حاول مرة أخرى.",
            parse_mode="Markdown"
        )
        return True
    return False

# ─────────────────────────────────────────
#  الأوامر الأساسية
# ─────────────────────────────────────────
@router.message(Command("start"))
async def cmd_start(message: types.Message):
    update_user_activity(message.from_user)
    if is_user_banned(message.from_user.id):
        return await message.reply("⛔ تم حظرك من استخدام هذا البوت.")
    name = message.from_user.first_name or "صديقي"
    await message.answer(
        f"🎉 *أهلاً {name}! أنا مستشار الذكاء الاصطناعي الخارق.*\n\n"
        "✨ *ماذا يمكنني أن أفعل لك؟*\n"
        "- الإجابة عن أي سؤال بأي لغة\n"
        "- كتابة وشرح الأكواد البرمجية\n"
        "- تحويل النصوص إلى Word أو PDF أو Excel\n"
        "- تحويل الملفات بين الصيغ المختلفة\n"
        "- تحليل الصور والمستندات\n"
        "- الاستماع إلى الرسائل الصوتية\n"
        "- الترجمة الفورية لأي لغة\n"
        "- تلخيص أي نص أو مستند\n"
        "- تصميم برومبت احترافي للصور\n"
        "- اختبار معلوماتك في أي موضوع\n\n"
        "💬 *تحدث معي طبيعياً أو اختر من القائمة!*\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👨‍💻 المبرمج: {DEVELOPER_NAME}\n"
        "📖 /help للمساعدة  |  /status لحالة البوت\n"
        "━━━━━━━━━━━━━━━━━━",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

@router.message(Command("help"))
async def cmd_help(message: types.Message):
    update_user_activity(message.from_user)
    await message.answer(
        "📖 *دليل الاستخدام*\n\n"
        "اختر الفئة التي تريد معرفة المزيد عنها:",
        reply_markup=get_help_keyboard(),
        parse_mode="Markdown"
    )

@router.message(Command("reset"))
async def cmd_reset(message: types.Message):
    update_user_activity(message.from_user)
    clear_conversation(message.from_user.id)
    await message.answer(
        "🔄 *تم مسح سياق المحادثة بنجاح.*\n\nيمكنك البدء من جديد!",
        parse_mode="Markdown"
    )

@router.message(Command("status"))
async def cmd_status(message: types.Message):
    update_user_activity(message.from_user)
    try:
        with get_db() as conn:
            total_users   = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            total_msgs    = conn.execute("SELECT SUM(total_messages) FROM daily_stats").fetchone()[0] or 0
            one_day_ago   = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            online_users  = conn.execute("SELECT COUNT(*) FROM users WHERE last_active >= ?", (one_day_ago,)).fetchone()[0]
            avg_rating_row = conn.execute("SELECT AVG(rating) FROM feedback").fetchone()[0]
            avg_rating    = round(avg_rating_row, 1) if avg_rating_row else "لا يوجد"
        stars = "⭐" * int(avg_rating) if isinstance(avg_rating, float) else ""
        await message.answer(
            "📡 *حالة البوت*\n\n"
            "🟢 البوت يعمل بشكل طبيعي\n\n"
            f"👥 إجمالي المستخدمين: `{total_users}`\n"
            f"🟢 نشط آخر 24 ساعة: `{online_users}`\n"
            f"💬 إجمالي الرسائل: `{total_msgs}`\n"
            f"⭐ متوسط التقييم: `{avg_rating}` {stars}\n\n"
            f"🤖 الموديل: `{GEMINI_MODEL}`",
            parse_mode="Markdown"
        )
    except Exception as e:
        await message.answer("⚠️ حدث خطأ في جلب الحالة.")

@router.message(Command("summarize"))
async def cmd_summarize(message: types.Message):
    update_user_activity(message.from_user)
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer(
            "📝 *التلخيص التلقائي*\n\n"
            "أرسل النص بعد الأمر مباشرة:\n"
            "`/summarize النص الطويل هنا...`\n\n"
            "أو اضغط على زر *📝 تلخيص نص* من القائمة.",
            parse_mode="Markdown"
        )
    if await rate_limit_check(message):
        return
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    prompt = f"لخص النص التالي بشكل مختصر واحترافي مع الحفاظ على أهم النقاط:\n\n{parts[1]}"
    resp   = await gemini_client.generate(prompt, message.from_user.id, use_cache=True)
    await message.reply(f"📝 *الملخص:*\n\n{resp}", parse_mode="Markdown")

@router.message(Command("quiz"))
async def cmd_quiz(message: types.Message):
    update_user_activity(message.from_user)
    parts = message.text.split(maxsplit=1)
    topic = parts[1].strip() if len(parts) > 1 else "معلومات عامة"
    if await rate_limit_check(message):
        return
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    prompt = (
        f"أنشئ سؤال اختيار من متعدد (MCQ) عن موضوع: {topic}\n"
        "اكتب السؤال، ثم 4 خيارات (أ، ب، ج، د)، ثم الإجابة الصحيحة مع شرح مختصر.\n"
        "استخدم هذا التنسيق بالضبط:\n"
        "❓ السؤال: ...\n\nأ) ...\nب) ...\nج) ...\nد) ...\n\n✅ الإجابة: (الحرف) ...\n💡 الشرح: ..."
    )
    resp = await gemini_client.generate(prompt, message.from_user.id)
    await message.reply(resp)

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
#  لوحة الأدمن
# ─────────────────────────────────────────
@router.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_USER_ID:
        return await message.answer("⛔ هذا الأمر متاح فقط لمالك البوت.")
    try:
        with get_db() as conn:
            total_users    = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            total_msgs_all = conn.execute("SELECT SUM(total_messages) FROM daily_stats").fetchone()[0] or 0
            banned_count   = conn.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]
            today          = datetime.now().strftime("%Y-%m-%d")
            yesterday      = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            one_day_ago    = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            today_row      = conn.execute("SELECT active_users, total_messages FROM daily_stats WHERE date=?", (today,)).fetchone()     or (0, 0)
            yesterday_row  = conn.execute("SELECT active_users, total_messages FROM daily_stats WHERE date=?", (yesterday,)).fetchone() or (0, 0)
            online_users   = conn.execute("SELECT COUNT(*) FROM users WHERE last_active >= ?", (one_day_ago,)).fetchone()[0]
            recent_users   = conn.execute(
                "SELECT username, first_name, last_active FROM users ORDER BY last_active DESC LIMIT 5"
            ).fetchall()
            error_count    = conn.execute("SELECT COUNT(*) FROM error_log").fetchone()[0]
            avg_rating_row = conn.execute("SELECT AVG(rating) FROM feedback").fetchone()[0]
            avg_rating     = round(avg_rating_row, 1) if avg_rating_row else "لا يوجد"
            feedback_count = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]

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
            f"📆 أمس:  `{yesterday_row[0]}` نشط | `{yesterday_row[1]}` رسالة\n"
            f"💬 إجمالي الرسائل: `{total_msgs_all}`\n\n"
            f"⭐ متوسط التقييم: `{avg_rating}` ({feedback_count} تقييم)\n"
            f"🐛 الأخطاء المسجلة: `{error_count}`\n\n"
            f"🕐 *آخر 5 مستخدمين:*\n{recent_text}\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "*الأوامر المتاحة:*\n"
            "/ban `user_id` — حظر مستخدم\n"
            "/unban `user_id` — رفع الحظر\n"
            "/broadcast `النص` — إرسال لكل المستخدمين\n"
            "/users — قائمة آخر المستخدمين\n"
            "/errors — آخر الأخطاء",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Admin command error: {e}")
        await message.answer("❌ حدث خطأ في جلب الإحصائيات.")

@router.message(Command("errors"))
async def cmd_errors(message: types.Message):
    if message.from_user.id != ADMIN_USER_ID:
        return
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT user_id, error_type, error_msg, created_at FROM error_log ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
        if not rows:
            return await message.reply("✅ لا توجد أخطاء مسجلة.")
        lines = ["🐛 *آخر 10 أخطاء:*\n"]
        for r in rows:
            lines.append(f"• `{r['error_type']}` — {r['created_at'][:16]}\n  {r['error_msg'][:80]}")
        await message.reply("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await message.reply(f"❌ خطأ: {e}")

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
        await asyncio.sleep(0.05)
    await status_msg.edit_text(
        f"✅ *اكتمل الإرسال*\n\n✔️ تم الإرسال: `{sent}`\n❌ فشل: `{failed}`",
        parse_mode="Markdown"
    )

# ─────────────────────────────────────────
#  Callbacks
# ─────────────────────────────────────────
CONVERSION_MAP = {
    "convert_word2pdf":   ("docx", "pdf",  "Word → PDF"),
    "convert_pdf2word":   ("pdf",  "docx", "PDF → Word"),
    "convert_excel2pdf":  ("xlsx", "pdf",  "Excel → PDF"),
    "convert_pdf2excel":  ("pdf",  "xlsx", "PDF → Excel"),
    "convert_excel2word": ("xlsx", "docx", "Excel → Word"),
    "convert_word2excel": ("docx", "xlsx", "Word → Excel"),
}

HELP_TEXTS = {
    "help_chat": (
        "💬 *المحادثة والأسئلة*\n\n"
        "- اكتب أي سؤال مباشرة وسأجيبك\n"
        "- يمكنك الحديث بأي لغة\n"
        "- البوت يتذكر سياق المحادثة\n"
        "- `/reset` لمسح السياق والبدء من جديد\n"
        "- `/quiz موضوع` لاختبار معلوماتك"
    ),
    "help_files": (
        "📁 *الملفات والتحويل*\n\n"
        "- أرسل ملفاً مع كتابة الصيغة المطلوبة في التعليق\n"
        "- مثال: أرسل ملف Word وأكتب 'pdf' في التعليق\n"
        "- أو استخدم زر 🔄 تحويل ملفات من القائمة\n"
        "- الصيغ المدعومة: PDF, Word, Excel, PowerPoint"
    ),
    "help_translate": (
        "🌐 *الترجمة الفورية*\n\n"
        "اكتب: `ترجم إلى [اللغة]: [النص]`\n\n"
        "أمثلة:\n"
        "- ترجم إلى الإنجليزية: مرحباً\n"
        "- ترجم إلى الفرنسية: كيف حالك\n"
        "- ترجم إلى الإسبانية: شكراً"
    ),
    "help_images": (
        "🎨 *برومبت توليد الصور*\n\n"
        "- اكتب: `اعمل صورة [الوصف]`\n"
        "- أو استخدم زر 🎨 برومبت صورة\n\n"
        "البوت سيحول وصفك إلى برومبت احترافي\n"
        "يمكنك نسخه ولصقه في Midjourney أو DALL-E"
    ),
    "help_voice": (
        "🎤 *الرسائل الصوتية*\n\n"
        "- أرسل أي رسالة صوتية\n"
        "- البوت سيحولها إلى نص ويرد عليها\n"
        "- يدعم العربية والإنجليزية تلقائياً"
    ),
    "help_stats": (
        "📊 *الإحصائيات*\n\n"
        "- `/status` لعرض حالة البوت\n"
        "- يمكنك تقييم البوت بالضغط على ⭐ تقييم البوت\n"
        "- تقييماتك تساعدنا على التطوير!"
    ),
}

@router.callback_query()
async def handle_all_callbacks(callback: CallbackQuery):
    user_id = callback.from_user.id
    data    = callback.data

    # ── تحويل الملفات ──
    if data == "convert_any":
        user_conversion_choice[user_id] = ("any", None, "أي صيغة لأي صيغة")
        await callback.message.answer(
            "📁 *أرسل الملف مع كتابة الصيغة المطلوبة في التعليق.*\n"
            "مثال: اكتب `pdf` أو `word` أو `excel` كتعليق للملف.",
            parse_mode="Markdown"
        )
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
        return

    # ── تقييم البوت ──
    if data.startswith("rate_"):
        rating = int(data.split("_")[1])
        stars  = "⭐" * rating
        try:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO feedback (user_id, rating) VALUES (?, ?)",
                    (user_id, rating)
                )
            await callback.message.edit_text(
                f"شكراً على تقييمك! {stars}\n\n"
                "تقييمك يساعدنا على تحسين البوت باستمرار. 🙏"
            )
            # إشعار الأدمن بالتقييمات المنخفضة
            if rating <= 2:
                try:
                    bot = callback.bot
                    await bot.send_message(
                        ADMIN_USER_ID,
                        f"⚠️ *تقييم منخفض!*\n\nمستخدم `{user_id}` أعطى تقييم {stars}",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
        except Exception as e:
            await callback.answer("❌ حدث خطأ في حفظ التقييم.")
        await callback.answer(f"تم التقييم بـ {stars}")
        return

    # ── مساعدة ──
    if data in HELP_TEXTS:
        await callback.message.answer(HELP_TEXTS[data], parse_mode="Markdown")
        await callback.answer()
        return

    await callback.answer()

# ─────────────────────────────────────────
#  أزرار القائمة الرئيسية
# ─────────────────────────────────────────
BUTTON_TEXTS = {
    "💬 ابدأ محادثة", "🖼️ تحليل صورة", "📄 تحويل نص لملف",
    "📊 تحويل لإكسيل", "🎤 إرسال صوت", "👨‍💻 تواصل مع المبرمج",
    "🔄 تحويل ملفات", "🌐 ترجمة فورية", "📝 تلخيص نص",
    "🎨 برومبت صورة",  "🧠 اختبار معلومات", "⭐ تقييم البوت"
}

@router.message(F.text.in_(BUTTON_TEXTS))
async def handle_buttons(message: types.Message):
    update_user_activity(message.from_user)
    t = message.text

    if t == "🔄 تحويل ملفات":
        await message.answer("🔄 *اختر نوع التحويل:*", parse_mode="Markdown", reply_markup=get_conversion_keyboard())

    elif t == "💬 ابدأ محادثة":
        await message.answer("📝 أنا جاهز! أرسل سؤالك أو طلبك وسأجيبك فوراً.")

    elif t == "🖼️ تحليل صورة":
        await message.answer("🖼️ أرسل لي الصورة التي تريد تحليلها وسأصفها بالتفصيل.")

    elif t == "📄 تحويل نص لملف":
        await message.answer(
            "📄 *تحويل النص إلى ملف*\n\n"
            "أرسل لي النص مع نوع الملف المطلوب:\n\n"
            "• *وورد:* حولي النص دا لملف وورد: ...\n"
            "• *PDF:*  حولي النص دا لملف PDF: ...\n"
            "• *اكسيل:* حولي النص دا لملف اكسيل: ...",
            parse_mode="Markdown"
        )

    elif t == "📊 تحويل لإكسيل":
        await message.answer(
            "📊 *تحويل النص إلى Excel*\n\n"
            "أرسل النص بهذا الشكل:\n"
            "`حولي النص دا لملف اكسيل: الاسم, العمر, المدينة\nأحمد, 25, القاهرة\nمحمد, 30, الإسكندرية`",
            parse_mode="Markdown"
        )

    elif t == "🎤 إرسال صوت":
        await message.answer(
            "🎤 *الرسائل الصوتية*\n\n"
            "أرسل لي رسالة صوتية وسأقوم بـ:\n\n"
            "1️⃣ تحويلها إلى نص مكتوب\n"
            "2️⃣ الرد على محتواها\n"
            "3️⃣ يمكنك طلب إنشاء ملف من النص"
        )

    elif t == "🌐 ترجمة فورية":
        await message.answer(
            "🌐 *الترجمة الفورية*\n\n"
            "اكتب: `ترجم إلى [اللغة]: [النص]`\n\n"
            "أمثلة:\n"
            "- ترجم إلى الإنجليزية: مرحباً\n"
            "- ترجم إلى الفرنسية: كيف حالك",
            parse_mode="Markdown"
        )

    elif t == "📝 تلخيص نص":
        await message.answer(
            "📝 *تلخيص النص*\n\n"
            "أرسل النص الذي تريد تلخيصه مباشرة، أو استخدم:\n"
            "`/summarize النص هنا...`\n\n"
            "يمكنك أيضاً إرسال ملف PDF أو Word وسألخصه لك!",
            parse_mode="Markdown"
        )

    elif t == "🎨 برومبت صورة":
        await message.answer(
            "🎨 *برومبت توليد الصور*\n\n"
            "اكتب وصفاً للصورة وسأحوله إلى برومبت احترافي:\n\n"
            "مثال: `اعمل صورة لمدينة مستقبلية تحت الماء`",
            parse_mode="Markdown"
        )

    elif t == "🧠 اختبار معلومات":
        await message.answer(
            "🧠 *اختبار المعلومات*\n\n"
            "استخدم الأمر التالي مع اختيار الموضوع:\n"
            "`/quiz الموضوع هنا`\n\n"
            "أمثلة:\n"
            "- /quiz تاريخ مصر\n"
            "- /quiz برمجة Python\n"
            "- /quiz علم الفضاء",
            parse_mode="Markdown"
        )

    elif t == "⭐ تقييم البوت":
        await message.answer(
            "⭐ *قيّم تجربتك مع البوت*\n\nاختر عدد النجوم:",
            reply_markup=get_feedback_keyboard(),
            parse_mode="Markdown"
        )

    elif t == "👨‍💻 تواصل مع المبرمج":
        await message.answer(
            f"👨‍💻 *المبرمج:* {DEVELOPER_NAME}\n\n"
            f"📧 *للتواصل:* {DEVELOPER_USERNAME}",
            parse_mode="Markdown"
        )

# ─────────────────────────────────────────
#  معالج الرسائل النصية
# ─────────────────────────────────────────
@router.message(F.text)
async def handle_message(message: types.Message):
    if message.text in BUTTON_TEXTS:
        return

    update_user_activity(message.from_user)

    if await rate_limit_check(message):
        return
    if is_user_banned(message.from_user.id):
        return await message.reply("⛔ تم حظرك من استخدام هذا البوت.")

    user_id    = message.from_user.id
    user_text  = message.text
    text_lower = user_text.lower()

    # ── حالة انتظار اختيار صيغة الملف ──
    if user_id in user_pending_file:
        fmt_map = {"pdf": "pdf", "word": "docx", "docx": "docx", "excel": "xlsx", "xlsx": "xlsx"}
        chosen  = fmt_map.get(text_lower.strip())
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

    if intent in ("excel", "docx", "pdf") and content:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_document")
        ext_map   = {"excel": "xlsx", "docx": "docx", "pdf": "pdf"}
        cap_map   = {"excel": "📊 ملف Excel جاهز!", "docx": "📄 ملف Word جاهز!", "pdf": "📕 ملف PDF جاهز!"}
        func_map  = {"excel": create_excel_file, "docx": create_docx_file, "pdf": create_pdf_file}
        ext       = ext_map[intent]
        path      = f"/tmp/{user_id}_doc.{ext}"
        try:
            func_map[intent](content, path)
            await message.reply_document(FSInputFile(path), caption=cap_map[intent])
            os.remove(path)
        except Exception as e:
            logger.error(f"{intent} creation error: {e}")
            log_error(user_id, f"create_{intent}", str(e))
            await message.reply(f"❌ حدث خطأ في إنشاء الملف. تفاصيل الخطأ سُجِّلت.")
        return

    # ── تلخيص ──
    summarize_triggers = ["لخص", "لخصلي", "تلخيص", "summarize", "ملخص"]
    if any(kw in text_lower for kw in summarize_triggers):
        await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        prompt = f"لخص النص التالي بشكل مختصر واحترافي مع الحفاظ على أهم النقاط:\n\n{user_text}"
        resp   = await gemini_client.generate(prompt, user_id, use_cache=True)
        await message.reply(f"📝 *الملخص:*\n\n{resp}", parse_mode="Markdown")
        return

    # ── برومبت صورة ──
    image_keywords = ["اعملي صورة", "اعمل صورة", "ارسم", "صمملي", "تخيل", "صورلي",
                      "توليد صورة", "انشاء صورة", "صمم صورة"]
    if any(kw in text_lower for kw in image_keywords):
        await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        prompt = (
            f"حوّل الطلب التالي إلى برومبت إبداعي واحترافي باللغة العربية لتوليد الصور بالذكاء الاصطناعي. "
            f"أضف تفاصيل عن الإضاءة والألوان والزاوية والجو العام.\n\n"
            f"طلب المستخدم: {user_text}\n\n"
            f"اكتب فقط نص البرومبت بدون أي مقدمات أو شرح."
        )
        generated = await gemini_client.generate(prompt, user_id, use_cache=True)
        await message.reply(
            f"🎨 *برومبت احترافي لطلبك:*\n\n`{generated}`\n\n"
            "🖼️ انسخ هذا النص ولصقه في Midjourney أو DALL-E أو أي أداة توليد صور.",
            parse_mode="Markdown"
        )
        return

    # ── ترجمة ──
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
                    translation = await gemini_client.generate(prompt, user_id, use_cache=True)
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
        b64     = base64.b64encode(img_bytes).decode("utf-8")
        caption = message.caption or "حلل هذه الصورة وصفها بالتفصيل."
        resp    = await gemini_client.generate_with_media(
            caption, [{"inline_data": {"mime_type": mime, "data": b64}}]
        )
        for i in range(0, len(resp), 4000):
            await message.reply(resp[i:i + 4000])
    except Exception as e:
        logger.error(f"Photo error: {e}")
        log_error(message.from_user.id, "photo_analysis", str(e))
        await message.reply("⚠️ عذراً، حدث خطأ أثناء تحليل الصورة. حاول مرة أخرى.")

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
        if source == "any" and not target:
            c = cap.lower()
            if   "pdf"   in c: target = "pdf"
            elif "word"  in c or "docx" in c: target = "docx"
            elif "excel" in c or "xlsx" in c: target = "xlsx"
        if target:
            await bot.send_chat_action(chat_id=message.chat.id, action="upload_document")
            file_info  = await bot.get_file(doc.file_id)
            file_bytes = await bot.download_file(file_info.file_path)
            del user_conversion_choice[user_id]
            await do_file_conversion(message, file_bytes.read(), fname, target)
            return
        else:
            file_info  = await bot.get_file(doc.file_id)
            file_bytes = await bot.download_file(file_info.file_path)
            user_pending_file[user_id] = {"file_bytes": file_bytes.read(), "filename": fname}
            await message.reply(
                "📝 *إلى أي صيغة تريد التحويل؟*\n\n• `pdf`\n• `word`\n• `excel`",
                parse_mode="Markdown"
            )
            return

    # ── كشف نية التحويل من التعليق ──
    if cap:
        c      = cap.lower()
        target = None
        if   "pdf"   in c: target = "pdf"
        elif "word"  in c or "docx" in c: target = "docx"
        elif "excel" in c or "xlsx" in c: target = "xlsx"
        elif "ppt"   in c or "pptx" in c: target = "pptx"
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
        return await message.reply(
            "⚠️ *نوع الملف غير مدعوم للتحليل.*\n\n"
            "الصيغ المدعومة: PDF, Word, Excel, CSV, TXT",
            parse_mode="Markdown"
        )

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
            import pypdf
            reader = pypdf.PdfReader(BytesIO(fb))
            for page in reader.pages:
                text += page.extract_text() or ""
        elif "word" in mime:
            import docx as dx
            doc_obj = dx.Document(BytesIO(fb))
            text    = "\n".join(p.text for p in doc_obj.paragraphs)
        elif "excel" in mime or "spreadsheet" in mime:
            from openpyxl import load_workbook
            wb   = load_workbook(BytesIO(fb), read_only=True)
            ws   = wb.active
            text = "\n".join(" | ".join(str(c) if c else "" for c in row) for row in ws.iter_rows(values_only=True))

        if not text.strip():
            return await message.reply("⚠️ لم أستطع استخراج نص من هذا الملف. قد يكون الملف مشفراً أو يحتوي على صور فقط.")

        prompt = f"حلل هذا المستند ({fname}). {cap or 'قدم ملخصاً شاملاً للمحتوى مع أبرز النقاط.'}\n\n{text[:10000]}"
        resp   = await gemini_client.generate(prompt, user_id)
        for i in range(0, len(resp), 4000):
            await message.reply(resp[i:i + 4000])
    except Exception as e:
        logger.error(f"Document analysis error: {e}")
        log_error(user_id, "document_analysis", str(e))
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
            return await message.reply("🎤 عذراً، فشل تحويل الصوت. تأكد من أن الملف الصوتي سليم.")

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
                return await message.reply("⚠️ خدمة التعرف على الصوت غير متاحة حالياً. حاول مرة أخرى.")

        if not text:
            return await message.reply("🎤 لم أتمكن من فهم الصوت. جرب مرة أخرى بصوت أوضح وبدون ضوضاء.")

        await message.reply(f"🎤 *لقد فهمت:* _{text}_", parse_mode="Markdown")
        resp = await gemini_client.generate(text, user_id)
        for i in range(0, len(resp), 4000):
            await message.answer(resp[i:i + 4000])

    except ImportError:
        await message.reply("⚠️ مكتبة التعرف على الصوت غير مثبتة.")
    except Exception as e:
        logger.error(f"Voice error: {e}")
        log_error(user_id, "voice_processing", str(e))
        await message.reply("🎤 عذراً، حدث خطأ أثناء معالجة الصوت.")
    finally:
        for p in [ogg_path, wav_path]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

# ─────────────────────────────────────────
#  Web Server
# ─────────────────────────────────────────
async def handle_health(request):
    try:
        with get_db() as conn:
            total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    except Exception:
        total_users = 0
    return web.json_response({
        "status":      "ok",
        "bot":         "running",
        "model":       GEMINI_MODEL,
        "total_users": total_users
    })

async def handle_web_chat(request):
    try:
        data      = await request.json()
        user_text = data.get("content", "")
        user_id_s = request.headers.get("X-User-Id", "web_user")
        user_id   = hash(user_id_s) % (10**9)
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
