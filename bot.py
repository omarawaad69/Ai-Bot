import asyncio
import logging
import os
import time
import base64
import sqlite3
import subprocess
import tempfile
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

# ==================== الشخصية الشاملة والعبقرية ====================
SYSTEM_PROMPT = """
أنت "مستشار الذكاء الاصطناعي الخارق". أنت تجمع بين خبير موسوعي ومبرمج عبقري. هدفك تقديم إجابات دقيقة واحترافية في كل المجالات، مع قدرة استثنائية على البرمجة.

قواعدك الصارمة:
0.  **اللغة التلقائية:** **يجب عليك الرد بنفس لغة سؤال المستخدم.** إذا سألك بالإنجليزية، أجب بالإنجليزية. إذا سألك بالعربية، أجب بالعربية. لا تخالف هذه القاعدة أبداً.
1.  **ممنوع المقدمات:** لا تبدأ أي إجابة بعبارات مثل "أهلاً بك"، "بصفتي...". ابدأ الإجابة مباشرة بالمعلومة المطلوبة.
2.  **تنسيق الردود:** استخدم الرموز التعبيرية باعتدال. للقوائم استخدم فقط "- ". للكود استخدم ``` مع تحديد اللغة. لا تستخدم "#" أبداً.
3.  **الهوية المزدوجة:** إذا كان السؤال برمجياً ركز على الكود، وإذا كان عاماً قدم شرحاً مباشراً.
4.  **تحليل المستندات والصور:** عند تحليل مستند أو صورة، ابدأ مباشرة بتحليل المحتوى بدون مقدمات.
5.  **الأمان:** ترفض أي طلب لإنشاء محتوى ضار أو غير قانوني.
"""

# ==================== قاعدة البيانات للمستخدمين والإحصائيات ====================
def init_db():
    conn = sqlite3.connect('bot_stats.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            joined_date TEXT,
            last_active TEXT,
            total_messages INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_stats (
            date TEXT PRIMARY KEY,
            active_users INTEGER DEFAULT 0,
            total_messages INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

def update_user_activity(user: types.User):
    conn = sqlite3.connect('bot_stats.db')
    cursor = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    cursor.execute('''
        INSERT INTO users (user_id, username, first_name, last_name, joined_date, last_active, total_messages)
        VALUES (?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            last_active = excluded.last_active,
            total_messages = users.total_messages + 1
    ''', (user.id, user.username, user.first_name, user.last_name, now, now))
    
    cursor.execute('''
        INSERT INTO daily_stats (date, active_users, total_messages)
        VALUES (?, 1, 1)
        ON CONFLICT(date) DO UPDATE SET
            active_users = active_users + 1,
            total_messages = total_messages + 1
    ''', (today,))
    
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect('bot_stats.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT COUNT(*) FROM users')
    total_users = cursor.fetchone()[0]
    
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    
    cursor.execute('SELECT active_users, total_messages FROM daily_stats WHERE date = ?', (today,))
    today_stats = cursor.fetchone() or (0, 0)
    
    cursor.execute('SELECT active_users, total_messages FROM daily_stats WHERE date = ?', (yesterday,))
    yesterday_stats = cursor.fetchone() or (0, 0)
    
    conn.close()
    return total_users, today_stats, yesterday_stats

# ==================== عميل Gemini ====================
class AsyncGeminiClient:
    def __init__(self, model: str = "gemini-3.1-flash-lite-preview"):
        self.client = genai.Client()
        self.model = model

    async def generate(self, prompt: str) -> str:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, self._sync_generate, prompt)
        return response

    def _sync_generate(self, prompt: str) -> str:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT
                    )
                )
                return response.text
            except Exception as e:
                logger.error(f"Gemini API error (attempt {attempt+1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                else:
                    return "عذراً، حدث خطأ مؤقت."

    async def generate_with_media(self, prompt: str, media_parts: list) -> str:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, self._sync_generate_with_media, prompt, media_parts)
        return response

    def _sync_generate_with_media(self, prompt: str, media_parts: list) -> str:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                contents = media_parts + [{"text": prompt}]
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT
                    )
                )
                return response.text
            except Exception as e:
                logger.error(f"Gemini API error (attempt {attempt+1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                else:
                    return "عذراً، حدث خطأ مؤقت."

gemini_client = AsyncGeminiClient()

# ==================== دوال مساعدة ====================
def convert_image_to_png(image_bytes: bytes) -> tuple[bytes, str]:
    """تحويل الصور إلى PNG إذا كانت بصيغة غير مدعومة"""
    try:
        img = Image.open(BytesIO(image_bytes))
        original_format = img.format
        if original_format not in ['JPEG', 'PNG', 'GIF']:
            output_buffer = BytesIO()
            img = img.convert('RGB')
            img.save(output_buffer, format='PNG')
            return output_buffer.getvalue(), "image/png"
        else:
            mime_type = f"image/{original_format.lower()}"
            if mime_type == "image/jpg": mime_type = "image/jpeg"
            return image_bytes, mime_type
    except Exception:
        return image_bytes, "image/jpeg"

# ==================== معالجات الأوامر ====================
@router.message(Command("start"))
async def cmd_start(message: types.Message):
    update_user_activity(message.from_user)
    await message.answer(
        "🎉 أهلاً بك! أنا مستشار الذكاء الاصطناعي الخارق.\n\n"
        "✨ ماذا يمكنني أن أفعل لك؟\n"
        "- الإجابة عن أي سؤال في أي مجال\n"
        "- كتابة وشرح الأكواد البرمجية بأي لغة\n"
        "- تحليل الصور وقراءة الأكواد منها\n"
        "- قراءة المستندات (PDF, TXT, Word)\n"
        "- الاستماع إلى الرسائل الصوتية والرد عليها\n"
        "- تحويل الملفات بين الصيغ (DOCX, PDF, TXT)\n"
        "- إنشاء ملفات Word منسقة من النصوص\n\n"
        "📊 أرسل /admin لعرض إحصائيات البوت\n"
        "🔄 أرسل /reset لمسح سياق المحادثة\n"
        "📝 أرسل /toword متبوعاً بنص لإنشاء ملف Word\n\n"
        "فقط أرسل سؤالك، صورة، ملف، أو رسالة صوتية!"
    )

@router.message(Command("admin"))
async def cmd_admin(message: types.Message):
    update_user_activity(message.from_user)
    total_users, today_stats, yesterday_stats = get_stats()
    
    stats_text = (
        "📊 *لوحة الإحصائيات*\n\n"
        f"👥 *إجمالي المستخدمين:* {total_users}\n\n"
        f"📅 *اليوم:*\n"
        f"   - المستخدمين النشطين: {today_stats[0]}\n"
        f"   - الرسائل: {today_stats[1]}\n\n"
        f"📆 *أمس:*\n"
        f"   - المستخدمين النشطين: {yesterday_stats[0]}\n"
        f"   - الرسائل: {yesterday_stats[1]}"
    )
    await message.answer(stats_text, parse_mode="Markdown")

@router.message(Command("reset"))
async def cmd_reset(message: types.Message):
    update_user_activity(message.from_user)
    await message.answer("🔄 تم مسح سياق المحادثة. أنا جاهز لأسئلتك.")

# ==================== أمر إنشاء ملف Word ====================
@router.message(Command("toword"))
async def create_docx_from_text(message: types.Message, bot: Bot):
    update_user_activity(message.from_user)
    text_to_convert = message.text.replace('/toword', '').strip()
    
    if not text_to_convert:
        return await message.reply("📝 أرسل النص بعد الأمر مباشرة. مثال:\n`/toword هذا نص التقرير`", parse_mode="Markdown")
    
    try:
        from docx import Document
        from docx.shared import Pt, Inches
        
        doc = Document()
        doc.add_heading('مستند تم إنشاؤه بواسطة البوت', level=1)
        p = doc.add_paragraph(text_to_convert)
        
        filepath = f"/tmp/{message.from_user.id}_doc.docx"
        doc.save(filepath)
        
        await message.reply_document(
            FSInputFile(filepath),
            caption="📄 هذا مستند Word الذي طلبته!"
        )
        os.remove(filepath)
    except Exception as e:
        logger.error(f"Error creating docx: {e}")
        await message.reply("❌ حدث خطأ أثناء إنشاء ملف Word.")

# ==================== معالج تحويل المستندات ====================
@router.message(F.document)
async def handle_document(message: types.Message, bot: Bot):
    update_user_activity(message.from_user)
    
    doc = message.document
    file_name = doc.file_name or "مستند"
    mime_type = doc.mime_type or ""
    caption = message.caption or ""
    
    # التحقق مما إذا كان المستخدم يريد تحويل الملف
    target_format = None
    if caption:
        cmd_text = caption.lower()
        if "pdf" in cmd_text:
            target_format = "pdf"
        elif "docx" in cmd_text or "word" in cmd_text:
            target_format = "docx"
        elif "txt" in cmd_text:
            target_format = "txt"
    
    # إذا كان طلب تحويل
    if target_format:
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        
        if doc.file_size > 20 * 1024 * 1024:
            return await message.reply("⚠️ حجم الملف كبير جداً. الحد الأقصى هو 20 ميجابايت.")
        
        try:
            file_info = await bot.get_file(doc.file_id)
            downloaded = await bot.download_file(file_info.file_path)
            
            input_path = f"/tmp/{file_name}"
            with open(input_path, 'wb') as f:
                f.write(downloaded.read())
            
            output_dir = "/tmp/converted"
            os.makedirs(output_dir, exist_ok=True)
            
            command = [
                'libreoffice', '--headless', '--convert-to', target_format,
                '--outdir', output_dir, input_path
            ]
            subprocess.run(command, check=True, timeout=60)
            
            base_name = os.path.splitext(file_name)[0]
            output_file = os.path.join(output_dir, f"{base_name}.{target_format}")
            
            if os.path.exists(output_file):
                await message.reply_document(
                    FSInputFile(output_file),
                    caption=f"✅ تم تحويل الملف إلى صيغة {target_format.upper()}"
                )
            else:
                await message.reply("❌ فشل التحويل. تأكد من أن الصيغة المصدر مدعومة.")
            
            os.remove(input_path)
            if os.path.exists(output_file):
                os.remove(output_file)
                
        except subprocess.TimeoutExpired:
            await message.reply("⏳ استغرق التحويل وقتاً طويلاً جداً. جرب ملفاً أصغر.")
        except Exception as e:
            logger.error(f"Conversion error: {e}")
            await message.reply("❌ حدث خطأ أثناء تحويل الملف.")
        return
    
    # إذا لم يكن طلب تحويل، تعامل معه كمستند للتحليل
    supported_formats = [
        "application/pdf", "text/plain", 
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword"
    ]
    
    if mime_type not in supported_formats:
        await message.reply("⚠️ هذا النوع من الملفات غير مدعوم للتحليل. الأنواع المدعومة: PDF, TXT, Word\n\n💡 يمكنك إرسال الملف مع تعليق 'حول إلى pdf' أو 'حول إلى docx' لتحويله.")
        return
    
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    
    try:
        file_info = await bot.get_file(doc.file_id)
        file_bytesio = BytesIO()
        await bot.download_file(file_info.file_path, file_bytesio)
        file_bytesio.seek(0)
        file_bytes = file_bytesio.read()
        
        text_content = ""
        
        if mime_type == "text/plain":
            text_content = file_bytes.decode('utf-8', errors='ignore')
        
        elif mime_type == "application/pdf":
            try:
                import PyPDF2
                pdf_reader = PyPDF2.PdfReader(BytesIO(file_bytes))
                for page in pdf_reader.pages:
                    text_content += page.extract_text() or ""
            except ImportError:
                await message.reply("⚠️ مكتبة PyPDF2 غير مثبتة.")
                return
        
        elif mime_type in ["application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/msword"]:
            try:
                import docx
                docx_file = docx.Document(BytesIO(file_bytes))
                text_content = "\n".join([para.text for para in docx_file.paragraphs])
            except ImportError:
                await message.reply("⚠️ مكتبة python-docx غير مثبتة.")
                return
        
        if not text_content.strip():
            await message.reply("⚠️ لم أتمكن من استخراج نص من هذا الملف.")
            return
        
        prompt = f"قم بتحليل المستند التالي ({file_name}). {caption or 'قدم ملخصاً وتحليلاً للمحتوى'}\n\nمحتوى المستند:\n{text_content[:10000]}"
        response = await gemini_client.generate(prompt)
        
        if len(response) > 4000:
            for i in range(0, len(response), 4000):
                await message.reply(response[i:i+4000])
        else:
            await message.reply(response)
            
    except Exception as e:
        logger.error(f"Error handling document: {e}")
        await message.reply("عذراً، حدث خطأ أثناء معالجة الملف.")

# ==================== معالج الصور ====================
@router.message(F.photo)
async def handle_photo(message: types.Message, bot: Bot):
    update_user_activity(message.from_user)
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        file_bytesio = BytesIO()
        await bot.download_file(file_info.file_path, file_bytesio)
        file_bytesio.seek(0)
        image_bytes = file_bytesio.read()
        
        image_bytes, mime_type = convert_image_to_png(image_bytes)
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')
        
        caption = message.caption or "حلل هذه الصورة"
        response = await gemini_client.generate_with_media(caption, [
            {"inline_data": {"mime_type": mime_type, "data": image_base64}}
        ])
        
        if len(response) > 4000:
            for i in range(0, len(response), 4000):
                await message.reply(response[i:i+4000])
        else:
            await message.reply(response)
            
    except Exception as e:
        logger.error(f"Error handling photo: {e}")
        await message.reply("عذراً، حدث خطأ أثناء معالجة الصورة.")

# ==================== معالج الرسائل الصوتية ====================
@router.message(F.voice)
async def handle_voice(message: types.Message, bot: Bot):
    update_user_activity(message.from_user)
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    
    try:
        voice = message.voice
        file_info = await bot.get_file(voice.file_id)
        file_bytesio = BytesIO()
        await bot.download_file(file_info.file_path, file_bytesio)
        file_bytesio.seek(0)
        
        temp_path = f"/tmp/voice_{message.from_user.id}.ogg"
        with open(temp_path, "wb") as f:
            f.write(file_bytesio.read())
        
        try:
            import speech_recognition as sr
            recognizer = sr.Recognizer()
            
            try:
                from pydub import AudioSegment
                audio = AudioSegment.from_file(temp_path, format="ogg")
                wav_path = temp_path.replace(".ogg", ".wav")
                audio.export(wav_path, format="wav")
                temp_path = wav_path
            except ImportError:
                pass
            
            with sr.AudioFile(temp_path) as source:
                audio_data = recognizer.record(source)
                text = recognizer.recognize_google(audio_data, language="ar-AR")
                
        except ImportError:
            await message.reply("⚠️ مكتبات الصوت غير مثبتة.")
            return
        except sr.UnknownValueError:
            await message.reply("🎤 لم أتمكن من فهم الصوت بوضوح.")
            return
        except sr.RequestError:
            await message.reply("⚠️ خدمة التعرف على الصوت غير متاحة حالياً.")
            return
        
        if not text.strip():
            await message.reply("🎤 لم أتمكن من استخراج نص من التسجيل الصوتي.")
            return
        
        await message.reply(f"🎤 *لقد فهمت:* _{text}_", parse_mode="Markdown")
        response = await gemini_client.generate(text)
        
        if len(response) > 4000:
            for i in range(0, len(response), 4000):
                await message.answer(response[i:i+4000])
        else:
            await message.answer(response)
            
        if os.path.exists(temp_path):
            os.remove(temp_path)
            
    except Exception as e:
        logger.error(f"Error handling voice: {e}")
        await message.reply("عذراً، حدث خطأ أثناء معالجة الرسالة الصوتية.")

# ==================== معالج النصوص العام ====================
@router.message(F.text)
async def handle_message(message: types.Message):
    update_user_activity(message.from_user)
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    response = await gemini_client.generate(message.text)
    if len(response) > 4000:
        for i in range(0, len(response), 4000):
            await message.answer(response[i:i+4000])
    else:
        await message.answer(response)

# ==================== الدالة الرئيسية ====================
async def main():
    init_db()
    logger.info("Database initialized successfully")
    bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
