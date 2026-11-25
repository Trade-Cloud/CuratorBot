# TradeCloud Curator

Telegram bot for the TradeCloud beta chat (a topic supergroup): chat backup, moderation,
bug/feature forms, and changelog.

## Features

- **Backup** — every update is stored raw and parsed (author, topic, reply, forward, album, edit history); files are downloaded to `/data/media`. A built-in web viewer streams it live.
- **Moderation** (Main topic) — detects links, `@mentions`, phones, emails, contact cards and channel forwards; deletes and warns, mutes repeat offenders. Skips `ADMIN`/`MODER` tags and chat admins.
- **Bug reports** (`/bug_report`) — a private-chat form; posts a short summary to the Bug Reports topic with a "Details" button for the author and admins. A local LLM writes the summary (optional — falls back to a trimmed description).
- **Feature requests** (`/feature`) — a short form, published in full.
- **Changelog** (`/changelog`) — admins publish versioned notes.
- **Statuses** (`/status`) — update a report's status; the author is notified.
- **Admin menu** (`/admin`) — form quick-reply buttons, connected people, drafts, notification toggles, member actions (mute/ban/kick).
- **Access** — the private chat works only for beta members and admins; everyone else gets a link to request access.

## Stack

Python 3.12, aiogram 3, SQLAlchemy 2 (async, Postgres), Redis (form state), aiohttp (web viewer). Runs in Docker.

## Configuration

Three files at the project root:

- **`.env`** — secrets and ids only: bot token, chat id, topic ids, admin ids, database/Redis, passwords, API keys. Copy from `.env.example`.
- **`config.json`** — non-secret tunables: moderation thresholds, limits, ports, timezone, LLM settings. Any value here can be overridden by the matching `.env` variable.
- **`text.json`** — every string the bot outputs, log lines included. Edit text here, no code changes needed.

## Run

```bash
cp .env.example .env     # fill BOT_TOKEN, CHAT_ID, ADMIN_IDS, POSTGRES_PASSWORD
docker compose up -d --build
```

Containers: `beta_bot`, Postgres, Redis. The database schema is created on startup.

### Chat setup

1. Add the bot to the chat as an **admin** with "delete messages" and "ban/restrict users" rights.
2. In BotFather, disable the bot's **Privacy mode** (`/setprivacy` → Disable), so it receives all messages.
3. Get topic ids: run `/scan_topics` in the bot's private chat (or `/topic_id` inside a topic) and copy the result into `.env`. Main = General → `TOPIC_MAIN_ID=0`.
4. Restart: `docker compose up -d`.

## Web viewer

The bot serves a backup viewer at **http://127.0.0.1:8090** (localhost only): live feed by topic,
search, edit history, media, moderation log, and report/member actions. Set `WEB_PASSWORD` if you
expose the port.

## Development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Run the bot from the host against Postgres/Redis in Docker:

```bash
docker compose up -d beta_bot_postgres beta_bot_redis
set -a; source .env; set +a
DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:55432/${POSTGRES_DB}" \
REDIS_URL="redis://127.0.0.1:56379/0" MEDIA_DIR=./data/media \
python -m bot
```

## Bot API limits

- Deletions aren't reported to bots — only the bot's own deletions are marked.
- Files over 20 MB aren't downloaded without a self-hosted `telegram-bot-api` (`TELEGRAM_API_URL`); their metadata is still saved.
- Only messages received after startup are captured — there is no history backfill.

## Layout

```
bot/
  __main__.py     entry point, Dispatcher wiring
  config.py       settings (config.json defaults + .env overrides)
  texts.py        loads text.json
  db/             models, schema-on-startup
  middlewares/    backup, db session, access
  handlers/       group, private, forms, edit, admin, admin_menu, moderation
  services/       backup, media, moderation, reports, notify, member_tags, topic_scan, llm, drafts, prefs, members
  web/            backup viewer (API + SSE + static)
config.json       non-secret tunables
text.json         all bot text
```

## License

MIT — see [LICENSE](LICENSE).
