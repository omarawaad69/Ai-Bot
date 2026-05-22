FROM python:3.12-slim

# تثبيت المتطلبات النظام
RUN apt-get update -y && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        libreoffice-writer \
        libreoffice-calc \
        libreoffice-impress \
        libreoffice-draw \
        default-jre-headless \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# إعداد مجلد LibreOffice
ENV HOME=/tmp
RUN mkdir -p /tmp/libreoffice && chmod -R 777 /tmp

WORKDIR /app

# تثبيت المكتبات أولاً (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
