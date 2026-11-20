FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    WHISPER_DIR=/srv/whisper

WORKDIR /srv

# ffmpeg — декодирование голосовых; шрифты — кириллица и эмодзи на картинках-шпаргалках
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-inter fonts-dejavu-core fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY prompts ./prompts
COPY data ./data

# /srv/whisper — кэш модели распознавания речи (том), скачивается при первом голосовом
RUN useradd --create-home --uid 1000 bot && mkdir -p /srv/whisper && chown bot:bot /srv/whisper
USER bot

CMD ["python", "-m", "app.main"]
