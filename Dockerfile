FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN useradd --create-home --uid 1000 bot && mkdir -p /data/media && chown -R bot:bot /data

COPY requirements.txt .
RUN pip install -r requirements.txt

# Файлы на хосте могут быть 600 (так на Маке) — без chown/chmod пользователь bot их не прочитает.
COPY --chown=bot:bot --chmod=u=rwX,go=rX bot ./bot

USER bot

VOLUME ["/data"]
CMD ["python", "-m", "bot"]
