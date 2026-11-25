FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN useradd --create-home --uid 1000 bot && mkdir -p /data/media && chown -R bot:bot /data

COPY requirements.txt .
RUN pip install -r requirements.txt

# Host files may be 600 (as on a Mac) — without chown/chmod the bot user can't read them.
COPY --chown=bot:bot --chmod=u=rwX,go=rX bot ./bot
# text.json (all bot text) and config.json (non-secret tunables) are read at startup from /app.
COPY --chown=bot:bot --chmod=u=rwX,go=rX config.json text.json ./

USER bot

VOLUME ["/data"]
CMD ["python", "-m", "bot"]
