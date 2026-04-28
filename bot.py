import asyncio
import logging
import os
import time
import base64
import sqlite3
import subprocess
import io
from io import BytesIO
from datetime import datetime, timedelta
from PIL import Image

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from aiogram.types import FSInputFile
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()

SYSTEM_PROMPT = """
أنت "مستشار الذكاء الاصطناعي الخارق". أنت تجمع بين خبير موسوعي ومبرمج عبقري. هدفك تقديم إجابات دقيقة واحترافية في كل المجالات، مع قدرة استثنائية على البرمجة.

قواعدك الصارمة:
0.  **اللغة التلقائية:** **يجب عليك الرد بنفس لغة سؤال المستخدم.**
1.  **ممنوع المقدمات:** لا تبدأ أي إجابة بعبارات مثل "أهلاً بك"، "بصفتي...". ابدأ الإجابة مباشرة.
2.  **تنسيق الردود:** للقوائم استخدم "- ". للكود استخدم ``` مع تحديد اللغة. لا تستخدم "#" أبداً.
3.  **الهوية المزدوجة:** إذا كان السؤال برمجياً ركز على الكود، وإذا كان عاماً قدم شرحاً مباشراً.
4.  **تحليل المستندات والصور:** عند تحليل مستند أو صورة، ابدأ مباشرة بتحليل المحتوى بدون مقدمات.
5.  **الأمان:** ترفض أي طلب لإنشاء محتوى ضار أو غير قانوني.
"""

# ==================== قاعدة البيانات ====================
def init_db():
    conn = sqlite3.connect('bot_stats.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
        last_name TEXT, joined_date TEXT, last_active TEXT, total_messages INTEGER DEFAULT 0)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS daily_stats (
        date TEXT PRIMARY KEY, active_users INTEGER DEFAULT 0, total_messages INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()

def update_user_activity(user: types.User):
    conn = sqlite3.connect('bot_stats.db')
    cursor = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute('''INSERT INTO users (user_id, username, first_name, last_name, joined_date, last_active, total_messages)
        VALUES (?, ?, ?, ?, ?, ?, 1) ON CONFLICT(user_id) DO UPDATE SET
        username=excluded.username, first_name=excluded.first_name, last_name=excluded.last_name,
        last_active=excluded.last_active, total_messages=users.total_messages+1''',
        (user.id, user.username, user.first_name, user.last_name, now, now))
    cursor.execute('''INSERT INTO daily_stats (date, active_users, total_messages) VALUES (?, 1, 1)
        ON CONFLICT(date) DO UPDATE SET active_users=active_users+1, total_messages=total_messages+1''', (today,))
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect('bot_stats.db')
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM users')
    total_users = cursor.fetchone()[0]
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    cursor.execute('SELECT active_users, total_messages FROM daily_stats WHERE date=?', (today,))
    today_stats = cursor.fetchone() or (0,0)
    cursor.execute('SELECT active_users, total_messages FROM daily_stats WHERE date=?', (yesterday,))
    yesterday_stats = cursor.fetchone() or (0,0)
    conn.close()
    return total_users, today_stats, yesterday_stats

# ==================== عميل Gemini ====================
class AsyncGeminiClient:
    def __init__(self, model: str = "gemini-3.1-flash-lite-preview"):
        self.client = genai.Client()
        self.model = model

    async def generate(self, prompt: str) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_generate, prompt)

    def _sync_generate(self, prompt: str) -> str:
        for attempt in range(3):
            try:
                response = self.client.models.generate_content(
                    model=self.model, contents=prompt,
                    config=genai_types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT))
                return response.text
            except Exception as e:
                logger.error(f"Gemini error (attempt {attempt+1}): {e}")
                if attempt < 2: time.sleep(1)
                else: return "عذراً، حدث خطأ مؤقت."

    async def generate_with_media(self, prompt: str, media_parts: list) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_generate_with_media, prompt, media_parts)

    def _sync_generate_with_media(self, prompt: str, media_parts: list) -> str:
        for attempt in range(3):
            try:
                contents = media_parts + [{"text": prompt}]
                response = self.client.models.generate_content(
                    model=self.model, contents=contents,
                    config=genai_types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT))
                return response.text
            except Exception as e:
                logger.error(f"Gemini media error (attempt {attempt+1}): {e}")
                if attempt < 2: time.sleep(1)
                else: return "عذراً، حدث خطأ مؤقت."

gemini_client = AsyncGeminiClient()

# ==================== دوال مساعدة ====================
def convert_image_to_png(image_bytes: bytes) -> tuple[bytes, str]:
    try:
        img = Image.open(BytesIO(image_bytes))
        fmt = img.format
        if fmt not in ['JPEG','PNG','GIF']:
            buf = BytesIO()
            img.convert('RGB').save(buf, format='PNG')
            return buf.getvalue(), "image/png"
        mime = f"image/{fmt.lower()}"
        return image_bytes, "image/jpeg" if mime=="image/jpg" else mime
    except:
        return image_bytes, "image/jpeg"

def text_to_docx(text: str, filepath: str):
    from docx import Document
    doc = Document()
    doc.add_heading('مستند تم إنشاؤه بواسطة البوت', level=1)
    doc.add_paragraph(text)
    doc.save(filepath)

def text_to_pdf(text: str, filepath: str):
    text_to_docx(text, filepath.replace('.pdf', '.docx'))
    subprocess.run(['libreoffice','--headless','--convert-to','pdf',
                    '--outdir', os.path.dirname(filepath),
                    filepath.replace('.pdf', '.docx')], check=True, timeout=30)
    os.remove(filepath.replace('.pdf', '.docx'))

# ==================== أوامر التحويل إلى ملفات ====================
@router.message(Command("toword"))
async def cmd_toword(message: types.Message, bot: Bot):
    update_user_activity(message.from_user)
    text = message.text.replace('/toword', '').strip()
    if not text:
        return await message.reply("📝 مثال: `/toword هذا نص التقرير`", parse_mode="Markdown")
    try:
        path = f"/tmp/{message.from_user.id}_doc.docx"
        text_to_docx(text, path)
        await message.reply_document(FSInputFile(path), caption="📄 ملف Word جاهز!")
        os.remove(path)
    except Exception as e:
        logger.error(f"toword error: {e}")
        await message.reply("❌ حدث خطأ أثناء إنشاء ملف Word.")

@router.message(Command("topdf"))
async def cmd_topdf(message: types.Message, bot: Bot):
    update_user_activity(message.from_user)
    text = message.text.replace('/topdf', '').strip()
    if not text:
        return await message.reply("📝 مثال: `/topdf هذا نص التقرير`", parse_mode="Markdown")
    try:
        path = f"/tmp/{message.from_user.id}_doc.pdf"
        text_to_pdf(text, path)
        await message.reply_document(FSInputFile(path), caption="📕 ملف PDF جاهز!")
        os.remove(path)
    except Exception as e:
        logger.error(f"topdf error: {e}")
        await message.reply("❌ حدث خطأ أثناء إنشاء ملف PDF. تأكد من تثبيت LibreOffice.")

# ==================== الأوامر العامة ====================
@router.message(Command("start"))
async def cmd_start(message: types.Message):
    update_user_activity(message.from_user)
    await message.answer(
        "🎉 أهلاً بك! أنا مستشار الذكاء الاصطناعي الخارق.\n\n"
        "✨ ماذا يمكنني أن أفعل لك؟\n"
        "- الإجابة عن أي سؤال\n"
        "- كتابة وشرح الأكواد البرمجية\n"
        "- تحليل الصور والمستندات\n"
        "- تحويل النصوص إلى ملفات Word أو PDF\n"
        "- الاستماع إلى الرسائل الصوتية\n\n"
        "📝 /toword نص → إنشاء ملف Word\n"
        "📕 /topdf نص → إنشاء ملف PDF\n"
        "📊 /admin → إحصائيات البوت\n"
        "🔄 /reset → مسح السياق"
    )

@router.message(Command("admin"))
async def cmd_admin(message: types.Message):
    update_user_activity(message.from_user)
    u, t, y = get_stats()
    await message.answer(
        f"📊 *لوحة الإحصائيات*\n\n"
        f"👥 إجمالي المستخدمين: {u}\n"
        f"📅 اليوم: {t[0]} نشط | {t[1]} رسالة\n"
        f"📆 أمس: {y[0]} نشط | {y[1]} رسالة",
        parse_mode="Markdown"
    )

@router.message(Command("reset"))
async def cmd_reset(message: types.Message):
    update_user_activity(message.from_user)
    await message.answer("🔄 تم مسح سياق المحادثة.")

# ==================== معالج الصور ====================
@router.message(F.photo)
async def handle_photo(message: types.Message, bot: Bot):
    update_user_activity(message.from_user)
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        bio = BytesIO()
        await bot.download_file(file_info.file_path, bio)
        bio.seek(0)
        img_bytes, mime = convert_image_to_png(bio.read())
        b64 = base64.b64encode(img_bytes).decode()
        caption = message.caption or "حلل هذه الصورة"
        resp = await gemini_client.generate_with_media(caption, [
            {"inline_data": {"mime_type": mime, "data": b64}}
        ])
        for i in range(0, len(resp), 4000):
            await message.reply(resp[i:i+4000])
    except Exception as e:
        logger.error(f"Photo error: {e}")
        await message.reply("عذراً، حدث خطأ.")

# ==================== معالج المستندات ====================
@router.message(F.document)
async def handle_document(message: types.Message, bot: Bot):
    update_user_activity(message.from_user)
    doc = message.document
    fname = doc.file_name or "مستند"
    mime = doc.mime_type or ""
    cap = message.caption or ""
    
    target = None
    if cap:
        c = cap.lower()
        if "pdf" in c: target = "pdf"
        elif "docx" in c or "word" in c: target = "docx"
        elif "txt" in c: target = "txt"
    
    if target:
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        if doc.file_size > 20*1024*1024:
            return await message.reply("⚠️ حجم الملف كبير جداً.")
        try:
            info = await bot.get_file(doc.file_id)
            dl = await bot.download_file(info.file_path)
            inpath = f"/tmp/{fname}"
            with open(inpath,'wb') as f: f.write(dl.read())
            outdir = "/tmp/converted"
            os.makedirs(outdir, exist_ok=True)
            subprocess.run(['libreoffice','--headless','--convert-to',target,
                            '--outdir',outdir,inpath], check=True, timeout=60)
            base = os.path.splitext(fname)[0]
            outfile = os.path.join(outdir, f"{base}.{target}")
            if os.path.exists(outfile):
                await message.reply_document(FSInputFile(outfile),
                    caption=f"✅ تم التحويل إلى {target.upper()}")
            else:
                await message.reply("❌ فشل التحويل.")
            os.remove(inpath)
            if os.path.exists(outfile): os.remove(outfile)
        except subprocess.TimeoutExpired:
            await message.reply("⏳ استغرق التحويل وقتاً طويلاً.")
        except Exception as e:
            logger.error(f"Convert error: {e}")
            await message.reply("❌ حدث خطأ أثناء التحويل.")
        return
    
    supported = ["application/pdf","text/plain",
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 "application/msword"]
    if mime not in supported:
        return await message.reply("⚠️ نوع غير مدعوم. أرسل PDF, TXT, أو Word.")
    
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        info = await bot.get_file(doc.file_id)
        bio = BytesIO()
        await bot.download_file(info.file_path, bio)
        bio.seek(0)
        fb = bio.read()
        text = ""
        if mime == "text/plain":
            text = fb.decode('utf-8','ignore')
        elif mime == "application/pdf":
            import PyPDF2
            r = PyPDF2.PdfReader(BytesIO(fb))
            for p in r.pages: text += p.extract_text() or ""
        elif "word" in mime:
            import docx as dx
            dxf = dx.Document(BytesIO(fb))
            text = "\n".join([p.text for p in dxf.paragraphs])
        if not text.strip():
            return await message.reply("⚠️ لم أستطع استخراج نص.")
        prompt = f"حلل هذا المستند ({fname}). {cap or 'قدم ملخصاً'}\n\n{text[:10000]}"
        resp = await gemini_client.generate(prompt)
        for i in range(0, len(resp), 4000):
            await message.reply(resp[i:i+4000])
    except Exception as e:
        logger.error(f"Doc error: {e}")
        await message.reply("عذراً، حدث خطأ.")

# ==================== معالج الصوت ====================
@router.message(F.voice)
async def handle_voice(message: types.Message, bot: Bot):
    update_user_activity(message.from_user)
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        voice = message.voice
        info = await bot.get_file(voice.file_id)
        bio = BytesIO()
        await bot.download_file(info.file_path, bio)
        bio.seek(0)
        audio_bytes = bio.read()
        bio.close()
        audio_part = {"inline_data": {"mime_type": voice.mime_type or "audio/ogg",
                      "data": base64.b64encode(audio_bytes).decode()}}
        prompt = "استمع إلى هذا المقطع الصوتي. قم بتفريغه إلى نص، ثم أجب على محتواه."
        resp = await gemini_client.generate_with_media(prompt, [audio_part])
        for i in range(0, len(resp), 4000):
            await message.answer(resp[i:i+4000])
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await message.reply("🎤 عذراً، لم أتمكن من معالجة الصوت.")

# ==================== معالج النصوص ====================
@router.message(F.text)
async def handle_message(message: types.Message):
    update_user_activity(message.from_user)
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    resp = await gemini_client.generate(message.text)
    for i in range(0, len(resp), 4000):
        await message.answer(resp[i:i+4000])

# ==================== الرئيسية ====================
async def main():
    init_db()
    logger.info("DB ready")
    bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())                      
