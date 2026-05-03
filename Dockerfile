FROM python:3.12-slim

RUN apt-get update -y && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    libreoffice-core \
    libreoffice-writer \
    libreoffice-calc \
    libreoffice-impress \
    libreoffice-draw \
    libreoffice-common \
    libreoffice-java-common \
    default-jre-headless \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

ENV HOME=/tmp
RUN mkdir -p /tmp/libreoffice && chmod -R 777 /tmp

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
