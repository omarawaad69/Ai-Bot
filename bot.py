import asyncio
import logging
import os
import time
import base64
import io
from io import BytesIO
from PIL import Image

from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types

load_dotenv()

logging.basicConfig(level=logging.INFO)

router = Router()

# ==================== الشخصية الشاملة والعبقرية (بأسلوب أنيق) ====================
SYSTEM_PROMPT = """
أنت مساعد ذكي واحترافي. تقدم إجابات مباشرة ومركزة على سؤال المستخدم. لا تضف أي مقدمات تعريفية عن نفسك إلا إذا سألك المستخدم صراحةً "من أنت؟".

قواعدك الصارمة:

1.  **ممنوع المقدمات:** لا تبدأ أي إجابة بعبارات مثل: "أهلاً بك"، "بصفتي المستشار..."، "دعني...". ابدأ الإجابة مباشرة بالمعلومة المطلوبة.

2.  **اللغة التلقائية:** **يجب عليك الرد بنفس لغة سؤال المستخدم.** إذا سألك بالإنجليزية، أجب بالإنجليزية. إذا سألك بالعربية، أجب بالعربية. لا تخالف هذه القاعدة أبداً.

3.  **تنسيق الردود (بساطة تامة):**
    *   **للقوائم:** استخدم فقط سطراً جديداً يبدأ بشرطة ومسافة (- ) أو نجمة ومسافة (* ).
    *   **للكود:** اكتب الكود داخل علامات ``` مع تحديد اللغة.
    *   **ممنوع تماماً** استخدام رموز `#` أو `**` أو أي تنسيق معقد آخر. اجعل النص نظيفاً وواضحاً.

4.  **الهوية المزدوجة:**
    *   إذا كان السؤال برمجياً، ركز على الكود.
    *   إذا كان السؤال عاماً، قدم شرحاً مباشراً بدون عناوين جانبية كثيرة.

5.  **قراءة الصور:** إذا أرسل المستخدم صورة، قم بتحليلها. إذا كانت تحتوي على كود، فاستخرجه واشرحه أو صححه. لا تكتب مقدمة، ابدأ مباشرة بتحليل محتوى الصورة.

6.  **الأمان:** ترفض بشكل قاطع أي طلب لإنشاء محتوى ضار أو غير قانوني.
"""

# ==================== عميل Gemini ====================
class AsyncGeminiClient:
    def __init__(self, model: str = "gemini-3.1-flash-lite-preview"):
        self.client = genai.Client()
        self.model = model

    async def generate(self, prompt: str) -> str:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, self._sync_generate, prompt
        )
        return response

    def _sync_generate(self, prompt: str) -> str:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                logging.info(f"Using model: {self.model}")
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT
                    )
                )
                return response.text
            except Exception as e:
                logging.error(f"Gemini API error (attempt {attempt+1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                else:
                    return "عذراً، حدث خطأ مؤقت."

    # --- دوال تحليل الصور ---
    async def generate_with_image(self, prompt: str, image_base64: str, mime_type: str = "image/jpeg") -> str:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, self._sync_generate_with_image, prompt, image_base64, mime_type
        )
        return response

    def _sync_generate_with_image(self, prompt: str, image_base64: str, mime_type: str) -> str:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                logging.info(f"Using model for image: {self.model}")
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=[
                        {"inline_data": {"mime_type": mime_type, "data": image_base64}},
                        {"text": prompt}
                    ],
                    config=genai_types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT
                    )
                )
                return response.text
            except Exception as e:
                logging.error(f"Gemini Image API error (attempt {attempt+1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                else:
                    return "عذراً، لم أتمكن من تحليل هذه الصورة."

gemini_client = AsyncGeminiClient()

# ==================== معالجات الأوامر ====================
@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "أهلاً بك! أنا مستشار الذكاء الاصطناعي الخارق.\n\n"
        "ماذا يمكنني أن أفعل لك؟\n"
        "- الإجابة عن أي سؤال في أي مجال.\n"
        "- كتابة وشرح الأكواد البرمجية.\n"
        "- تحليل الصور وقراءة الأكواد منها.\n\n"
        "فقط أرسل لي سؤالك، أو أرسل صورة للتحليل."
    )

@router.message(Command("reset"))
async def cmd_reset(message: types.Message):
    await message.answer("تم مسح سياق المحادثة. أنا جاهز لأسئلتك.")

# ==================== معالج الصور ====================
@router.message(lambda message: message.photo)
async def handle_photo(message: types.Message, bot: Bot):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        file_bytesio = BytesIO()
        await bot.download_file(file_info.file_path, file_bytesio)
        file_bytesio.seek(0)
        image_bytes = file_bytesio.read()
        
        try:
            img = Image.open(BytesIO(image_bytes))
            original_format = img.format
            logging.info(f"Original image format: {original_format}")
            if original_format not in ['JPEG', 'PNG', 'GIF']:
                logging.info(f"Converting image from {original_format} to PNG...")
                output_buffer = BytesIO()
                img = img.convert('RGB')
                img.save(output_buffer, format='PNG')
                image_bytes = output_buffer.getvalue()
                mime_type = "image/png"
            else:
                mime_type = f"image/{original_format.lower()}"
                if mime_type == "image/jpg": mime_type = "image/jpeg"
        except Exception as img_err:
            logging.warning(f"Could not process image format, trying as JPEG: {img_err}")
            mime_type = "image/jpeg"
        
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')
        caption = message.caption or "حلل هذه الصورة. إذا كان فيها كود، اشرحه."
        response = await gemini_client.generate_with_image(caption, image_base64, mime_type)
        
        if len(response) > 4000:
            for i in range(0, len(response), 4000):
                await message.reply(response[i:i+4000])
        else:
            await message.reply(response)
            
    except Exception as e:
        logging.error(f"Error handling photo: {e}")
        await message.reply("عذراً، حدث خطأ أثناء معالجة الصورة.")

# ==================== معالج النصوص العام ====================
@router.message()
async def handle_message(message: types.Message):
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    response = await gemini_client.generate(message.text)
    if len(response) > 4000:
        for i in range(0, len(response), 4000):
            await message.answer(response[i:i+4000])
    else:
        await message.answer(response)

# ==================== الدالة الرئيسية ====================
async def main():
    bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())