FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libreoffice-core \
    libreoffice-writer \
    ffmpeg \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# تثبيت المكتبات الصوتية بشكل صريح
RUN pip install SpeechRecognition pydub

COPY . .

CMD ["python", "bot.py"]
