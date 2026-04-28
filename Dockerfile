# استخدام صورة بايثون رسمية وخفيفة
FROM python:3.12-slim

# تثبيت LibreOffice والأدوات المساعدة
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libreoffice-core \
    libreoffice-writer \
    libreoffice-impress \
    libreoffice-calc \
    libreoffice-common \
    libreoffice-java-common \
    default-jre \
    fonts-arabeyes \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# إعداد مجلد العمل
WORKDIR /app

# نسخ ملف المتطلبات وتثبيت مكتبات بايثون
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ باقي ملفات المشروع
COPY . .

# الأمر الذي سيشغل البوت
CMD ["python", "bot.py"]