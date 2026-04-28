import asyncio
import logging
import os
import time
import base64
import sqlite3
import subprocess
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

# ==================== معرف الأدمن ====================
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "7361263893"))

# ==================== معلومات المبرمج ====================
DEVELOPER_NAME = "Omar Abd El Gawaad"
DEVELOPER_USERNAME = "@omarawaad68"
DEVELOPER_LINK = "https://t.me/omarawaad68"

# ==================== الشخصية ====================
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
    try:
        conn = sqlite3.connect('bot_stats.db')
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            last_name TEXT, joined_date TEXT, last_active TEXT, total_messages INTEGER DEFAULT 0)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS daily_stats (
            date TEXT PRIMARY KEY, active_users INTEGER DEFAULT 0, total_messages INTEGER DEFAULT 0)''')
        conn.commit()
        conn.close()
        logger.info("Database initialized")
    except Exception as e:
        logger.error(f"DB init error: {e}")

def update_user_activity(user: types.User):
    try:
        conn = sqlite3.connect('bot_stats.db')
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        today = datetime.now().strftime("%Y-%m-%d")
        cursor.execute('''INSERT INTO users (user_id, username, first_name, last_name, joined_date, last_active, total_messages)
            VALUES (?, ?, ?, ?, ?, ?, 1) ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username, first_name=excluded.first_name, last_name=excluded.last_name,
            last_active=excluded.last_active, total_messages=users.total_messages+1''',
            (user.id, user.username, user.first_name, user.last_name, now, now))
        cursor.execute('''INSERT INTO daily_stats (date, active_users, total_messages) VALUES (?, 1, 1)
            ON CONFLICT(date) DO UPDATE SET active_users=active_users+1, total_messages=total_messages+1''', (today,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"User activity error: {e}")

def get_stats():
    try:
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
    except Exception as e:
        logger.error(f"Stats error: {e}")
        return 0, (0,0), (0,0)

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
def convert_image_to_png(image_bytes: bytes):
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

def create_docx_file(text: str, filepath: str):
    from docx import Document
    doc = Document()
    doc.add_heading('مستند تم إنشاؤه بواسطة البوت', level=1)
    doc.add_paragraph(text)
    doc.save(filepath)

def create_pdf_file(text: str, filepath: str):
    docx_path = filepath.replace('.pdf', '.docx')
    create_docx_file(text, docx_path)
    subprocess.run(['libreoffice','--headless','--convert-to','pdf',
                    '--outdir', os.path.dirname(filepath), docx_path],
                   check=True, timeout=30)
    os.remove(docx_path)

def detect_conversion_intent(text: str):
    text_lower = text.lower()
    
    word_patterns = [
        "حولي النص التالي لملف وورد", "حولي النص التالي لword",
        "حولي النص دا لملف وورد", "حولي النص دا لword",
        "حولي النص ده لملف وورد", "حولي النص ده لword",
        "حولي دا لملف وورد", "حولي دا لword",
        "حولي ده لملف وورد", "حولي ده لword",
        "حول النص التالي لملف وورد", "حول النص التالي لword",
        "حول النص دا لملف وورد", "حول النص دا لword",
        "حول النص ده لملف وورد", "حول النص ده لword",
        "حول دا لملف وورد", "حول دا لword",
        "حول ده لملف وورد", "حول ده لword",
        "حولي النص لملف وورد", "حولي النص لword",
        "حول النص لملف وورد", "حول النص لword",
        "حولي لملف وورد", "حول لملف وورد",
        "حولي لوورد", "حول لوورد",
        "حولي لword", "حول لword",
        "ملف وورد", "ملف word", "وورد", "word", "docx",
        "خليه وورد", "خليه word", "ابعتلي وورد",
        "انزله وورد", "حمله وورد",
        "اعملي ملف وورد", "اعمل ملف word",
    ]
    
    pdf_patterns = [
        "حولي النص التالي لملف pdf", "حولي النص التالي لpdf",
        "حولي النص دا لملف pdf", "حولي النص دا لpdf",
        "حولي النص ده لملف pdf", "حولي النص ده لpdf",
        "حولي دا لملف pdf", "حولي دا لpdf",
        "حولي ده لملف pdf", "حولي ده لpdf",
        "حول النص التالي لملف pdf", "حول النص التالي لpdf",
        "حول النص دا لملف pdf", "حول النص دا لpdf",
        "حول النص ده لملف pdf", "حول النص ده لpdf",
        "حول دا لملف pdf", "حول دا لpdf",
        "حول ده لملف pdf", "حول ده لpdf",
        "حولي النص لpdf", "حول النص لpdf",
        "حولي لملف pdf", "حول لملف pdf",
        "حولي لpdf", "حول لpdf",
        "حولي لبي دي اف", "حول لبي دي اف",
        "ملف pdf", "بي دي اف", "pdf",
        "خليه pdf", "خليه بي دي اف",
        "ابعتلي pdf", "انزله pdf", "حمله pdf",
        "اعملي ملف pdf", "اعمل ملف بي دي اف",
    ]
    
    for pattern in word_patterns:
        if pattern in text_lower:
            idx = text_lower.find(pattern)
            content = text[idx + len(pattern):].strip()
            if not content:
                content = text[:idx].strip()
                for prefix in ["حولي", "حول", "حوّل", "خلي", "خليك", "اعمل", "سوي", "سوّي", "ابعتلي", "انزلي", "حملي"]:
                    if content.startswith(prefix):
                        content = content[len(prefix):].strip()
                        break
            if content:
                return "docx", content
            else:
                return "WORD_NEED_TEXT", ""
    
    for pattern in pdf_patterns:
        if pattern in text_lower:
            idx = text_lower.find(pattern)
            content = text[idx + len(pattern):].strip()
            if not content:
                content = text[:idx].strip()
                for prefix in ["حولي", "حول", "حوّل", "خلي", "خليك", "اعمل", "سوي", "سوّي", "ابعتلي", "انزلي", "حملي"]:
                    if content.startswith(prefix):
                        content = content[len(prefix):].strip()
                        break
            if content:
                return "pdf", content
            else:
                return "PDF_NEED_TEXT", ""
    
    return None, None

# ==================== الأوامر العامة ====================
@router.message(Command("start"))
@router.message(Command("start"))
async def cmd_start(message: types.Message):
    update_user_activity(message.from_user)
    await message.answer(
        "🎉 أهلاً بك! أنا مستشار الذكاء الاصطناعي الخارق.\n\n"
        "✨ ماذا يمكنني أن أفعل لك؟\n"
        "- الإجابة عن أي سؤال\n"
        "- كتابة وشرح الأكواد البرمجية\n"
        "- تحويل النصوص إلى ملفات Word أو PDF\n"
        "- تحليل الصور والمستندات\n"
        "- الاستماع إلى الرسائل الصوتية\n"
        "- تصميم برومبت احترافي للصور\n\n"
        "💬 تحدث معي طبيعياً وسأفهمك!\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👨‍💻 *المبرمج:* {DEVELOPER_NAME}\n"
        f"📧 *للتواصل:* **{DEVELOPER_USERNAME}**\n"
        "━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown"
    )

@router.message(Command("developer"))
async def cmd_developer(message: types.Message):
    update_user_activity(message.from_user)
    await message.answer(
        "👨‍💻 *معلومات المبرمج*\n\n"
        f"📛 *الاسم:* {DEVELOPER_NAME}\n"
        f"📧 *للتواصل:* **{DEVELOPER_USERNAME}**\n\n"
        "💡 *للتواصل:* أرسل رسالة إلى الحساب أعلاه لأي استفسار أو اقتراح أو طلب تطوير بوت خاص.",
        parse_mode="Markdown"
    )

@router.message(Command("admin"))
async def cmd_admin(message: types.Message):
    update_user_activity(message.from_user)
    
    if message.from_user.id != ADMIN_USER_ID:
        await message.answer("⛔ عذراً، هذا الأمر متاح فقط لمالك البوت.")
        return
    
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

# ==================== معالج النصوص ====================
@router.message(F.text)
async def handle_message(message: types.Message):
    update_user_activity(message.from_user)
    user_text = message.text
    text_lower = user_text.lower()

    intent, content = detect_conversion_intent(user_text)
    
    if intent == "WORD_NEED_TEXT":
        return await message.reply("📝 ما هو النص الذي تريد تحويله إلى ملف Word؟")
    if intent == "PDF_NEED_TEXT":
        return await message.reply("📕 ما هو النص الذي تريد تحويله إلى ملف PDF؟")
    
    if intent == "docx" and content:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        try:
            path = f"/tmp/{message.from_user.id}_doc.docx"
            create_docx_file(content, path)
            await message.reply_document(FSInputFile(path), caption="📄 ملف Word جاهز!")
            os.remove(path)
            return
        except Exception as e:
            logger.error(f"Word error: {e}")
            return await message.reply("❌ حدث خطأ في إنشاء ملف Word.")
            
    if intent == "pdf" and content:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        try:
            path = f"/tmp/{message.from_user.id}_doc.pdf"
            create_pdf_file(content, path)
            await message.reply_document(FSInputFile(path), caption="📕 ملف PDF جاهز!")
            os.remove(path)
            return
        except Exception as e:
            logger.error(f"PDF error: {e}")
            return await message.reply("❌ حدث خطأ في إنشاء ملف PDF.")

    image_keywords = ["اعملي صورة", "اعمل صورة", "ارسم", "صمملي", "تخيل", "صورلي", "توليد صورة", "انشاء صورة", "صمم صورة", "generate image", "create image"]
    is_image_request = any(keyword in text_lower for keyword in image_keywords)

    if is_image_request:
        image_prompt = user_text
        for keyword in image_keywords:
            if keyword in text_lower:
                image_prompt = user_text[text_lower.find(keyword) + len(keyword):].strip().lstrip(":، ")
                break
        
        await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        prompt_request = f"""حوّل الطلب التالي إلى أمر (Prompt) إبداعي واحترافي باللغة العربية لاستخدامه مع مولدات الصور بالذكاء الاصطناعي. أضف تفاصيل عن الإضاءة، الألوان، الزاوية، والجو العام.
        
        طلب المستخدم: {user_text}
        
        اكتب فقط نص الأمر (البرومبت) بدون أي مقدمات أو شرح إضافي."""
        
        generated_prompt = await gemini_client.generate(prompt_request)
        
        final_response = f"🎨 *تم تصميم برومبت احترافي لطلبك:*\n\n`{generated_prompt}`\n\n🖼️ يمكنك نسخ هذا النص ولصقه في أي أداة لتوليد الصور بالذكاء الاصطناعي."
        await message.reply(final_response, parse_mode="Markdown")
        return

    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    resp = await gemini_client.generate(user_text)
    for i in range(0, len(resp), 4000):
        await message.answer(resp[i:i+4000])

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
    
    if target:
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
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
        except Exception as e:
            logger.error(f"Convert error: {e}")
            await message.reply("❌ حدث خطأ.")
        return
    
    supported = ["application/pdf","text/plain",
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 "application/msword"]
    if mime not in supported:
        return await message.reply("⚠️ نوع غير مدعوم.")
    
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
    
    ogg_path = f"/tmp/{message.from_user.id}_voice.ogg"
    wav_path = f"/tmp/{message.from_user.id}_voice.wav"
    
    try:
        voice = message.voice
        file_info = await bot.get_file(voice.file_id)
        bio = BytesIO()
        await bot.download_file(file_info.file_path, bio)
        bio.seek(0)
        
        with open(ogg_path, "wb") as f:
            f.write(bio.read())
        bio.close()
        
        try:
            subprocess.run(
                ['ffmpeg', '-i', ogg_path, '-ar', '16000', '-ac', '1', wav_path],
                check=True, capture_output=True, timeout=30
            )
            logger.info("Audio converted to WAV")
        except Exception as e:
            logger.error(f"ffmpeg error: {e}")
            await message.reply("🎤 عذراً، فشل تحويل الصوت.")
            return
        
        import speech_recognition as sr
        recognizer = sr.Recognizer()
        
        with sr.AudioFile(wav_path) as source:
            audio = recognizer.record(source)
        
        text = None
        for lang in ["ar-AR", "en-US", ""]:
            try:
                text = recognizer.recognize_google(audio, language=lang) if lang else recognizer.recognize_google(audio)
                if text: break
            except sr.UnknownValueError:
                continue
            except sr.RequestError as e:
                logger.error(f"Google API error: {e}")
                await message.reply("⚠️ خدمة التعرف على الصوت غير متاحة حالياً.")
                return
        
        if not text:
            await message.reply("🎤 لم أتمكن من فهم الصوت.")
            return
        
        await message.reply(f"🎤 *لقد فهمت:* _{text}_", parse_mode="Markdown")
        resp = await gemini_client.generate(text)
        for i in range(0, len(resp), 4000):
            await message.answer(resp[i:i+4000])
            
    except ImportError:
        await message.reply("⚠️ مكتبة الصوت غير مثبتة.")
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await message.reply("🎤 عذراً، حدث خطأ.")
    finally:
        if os.path.exists(ogg_path): os.remove(ogg_path)
        if os.path.exists(wav_path): os.remove(wav_path)

# ==================== الرئيسية ====================
async def main():
    init_db()
    bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
