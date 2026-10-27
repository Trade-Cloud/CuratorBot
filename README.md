# beta_bot

Telegram-бот бета-чата TradeCloud (supergroup с топиками INFO / Changelog / Bug Reports / Feature Requests / Main).

## Что умеет

| Режим | Как работает |
|---|---|
| **Бекап** | Каждый апдейт из чата сохраняется целиком (`raw_updates`) и в разобранном виде (`messages`): автор, топик, время, ответ на что, цитата, пересылка, альбом, тег отправителя. Правки — в `message_versions` (старый и новый текст по каждой правке). Файлы (фото, документы, видео, голосовые, кружки, стикеры) скачиваются в `/data/media`, метаданные и `file_id` — в `attachments`. |
| **Модерация Main** | Ищет ссылки (в т.ч. `t . me/…`), @username, телефоны, почты, карточки контактов, пересылки из каналов. Жёсткие находки — удаление + предупреждение + лог админам, после N нарушений — мут. Мягкие (`пиши в лс`, мессенджеры) — по умолчанию только лог. Не трогает теги `ADMIN`/`MODER`, админов чата, анонимных админов и `ADMIN_IDS`. |
| **/bug_report** | В чате → бот пишет в ЛС и ведёт анкету: описание, шаги, фактический и ожидаемый результат, окружение (кнопка «Браузер» по умолчанию), вложения (обязательны, можно текстом логов). Превью → пост в Bug Reports: короткое описание, ID из 6 символов, автор, дата, кнопка «Подробнее». Кнопка открывает полную карточку со статистикой и вложениями в ЛС — только автору и `ADMIN_IDS`, остальным «Вам это не доступно». |
| **/feature** | Анкета в ЛС: суть, проблема, реализация. В Feature Requests публикуется всё целиком + ID, автор, дата. Без кнопки. |
| **Доступ в ЛС** | Только участники бета-чата и `ADMIN_IDS`. Остальным бот не показывает меню команд и на любое сообщение отвечает ссылкой на @trade_cloud_bot (`ACCESS_BOT_USERNAME`) — запросить доступ к бете. Меню команд выдаётся каждому участнику персонально. |
| **Changelog** | `/changelog` в ЛС (только админы): версия → текст с форматированием → превью → пост в Changelog. |
| **Статусы** | `/status A1B2C3 fixed комментарий` — статус обновится в посте, автору придёт уведомление. `/report ID` — открыть карточку. |

## Веб-просмотр бекапа

Бот сам поднимает мини-сайт: **http://127.0.0.1:8090** (порт в compose открыт только на localhost).
Лента по топикам или вся сразу, поиск по тексту, фильтр по участнику (клик по имени), история правок (клик на «изменено»), фото/видео/голосовые/файлы прямо в ленте, удалённые модерацией помечены. Отдельно — репорты, нарушения модерации, участники.

Всё в прямом эфире (Server-Sent Events): новые сообщения появляются сами, правки и докачанные файлы обновляют карточку, после обрыва связи пропущенное догружается.

Пароль: `WEB_PASSWORD` (логин `WEB_USER`, по умолчанию `admin`). Пусто — без пароля; если будешь пробрасывать порт наружу, пароль обязателен.
С сервера смотреть через SSH-туннель: `ssh -L 8090:127.0.0.1:8090 <сервер>` → http://127.0.0.1:8090.

## Запуск

```bash
cp .env.example .env   # заполнить BOT_TOKEN, CHAT_ID, ADMIN_IDS, POSTGRES_PASSWORD
docker compose up -d --build
```

Контейнеры: `beta_bot`, `beta_bot_postgres` (Postgres 16), `beta_bot_redis` (FSM анкет переживает рестарт).
Схема БД создаётся при старте (`create_all`); когда структура устаканится — перейти на alembic.

### Настройка чата

1. Добавить бота в чат **админом** с правами: удаление сообщений, бан/ограничение участников. Без админки бот не видит все сообщения и не сможет модерировать.
2. В BotFather для бота **выключить Privacy mode** (`/setprivacy` → Disable), иначе бекап получит только команды.
3. Узнать ID топиков — любым способом:
   - `python -m bot.scan_topics` (нужны только `BOT_TOKEN` и `CHAT_ID`) или `/scan_topics` в ЛС бота (для `ADMIN_IDS`): бот перебирает `message_thread_id` до 300 (`--max 1000` / `/scan_topics 1000`), в каждый найденный топик постит его ID и название, а в конце выдаёт готовый кусок `.env` по названиям INFO / Changelog / Bug Reports / Feature Requests / Main;
   - `/topic_id` в нужном топике.
   Main = General → `TOPIC_MAIN_ID=0`.
   Проверить, что ID в `.env` верные: `python -m bot.test_topics` или `/test_topics` в ЛС — в каждый топик уйдёт «Топик <id> - <название>».
4. Перезапустить: `docker compose up -d`.

## Тестовый запуск на Mac

Postgres и Redis в Docker, бот — прямо с хоста (быстрее править и перезапускать):

```bash
cd ~/Documents/TradeCloud/Tools/beta_bot
cp -n .env.example .env          # BOT_TOKEN, CHAT_ID, ADMIN_IDS, POSTGRES_PASSWORD

python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

pytest                           # тесты, без Telegram и без Docker

docker compose up -d beta_bot_postgres beta_bot_redis

# 1) найти топики и получить кусок для .env
python -m bot.scan_topics

# 2) запустить бота
set -a; source .env; set +a      # подтянуть POSTGRES_* в shell
DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:55432/${POSTGRES_DB}" \
REDIS_URL="redis://127.0.0.1:56379/0" \
MEDIA_DIR=./data/media \
python -m bot
```

Всё в Docker целиком: `docker compose up -d --build && docker compose logs -f beta_bot`.
Посмотреть бекап: `docker exec -it beta_bot_postgres psql -U beta_bot -d beta_bot -c 'select thread_id, user_id, text from messages order by id desc limit 20;'`.

## Ограничения Bot API

- **Удаления не видны.** Telegram не присылает ботам события удаления — в бекапе остаётся последняя версия, помечаем только то, что удалил сам бот (`deleted_by_bot`).
- **Файлы > 20 МБ** без своего [telegram-bot-api](https://github.com/tdlib/telegram-bot-api) не скачиваются: сохраняются `file_id` и метаданные со статусом `too_big`. Если нужно — поднять сервер и указать `TELEGRAM_API_URL`.
- **История до запуска** бота не подтянется — только то, что пришло после.
- **Теги участников.** Берём `message.sender_tag` (Telegram кладёт туда тег или кастомный титул админа), запасной путь — `getChatMember().tag` / `custom_title`. Стоит проверить на живом чате, что теги приходят именно как `ADMIN`/`MODER`; список задаётся в `EXEMPT_TAGS`.

## Структура

```
bot/
  __main__.py          точка входа, сборка Dispatcher
  config.py            все настройки из env
  texts.py             тексты бота
  db/models.py         таблицы
  middlewares/         backup (до хендлеров, своя транзакция), db (сессия на апдейт)
  handlers/            group (команды в чате, «Подробнее»), private (/start, deep-link),
                       forms (анкеты), admin (changelog, статусы), moderation (Main)
  web/                 веб-просмотр: server (API + SSE), queries, events, static/index.html
  services/            backup, media, moderation, member_tags, reports, notify, topic_scan, llm
  scan_topics.py       CLI: python -m bot.scan_topics
  test_topics.py       CLI: python -m bot.test_topics
  llm_check.py         CLI: python -m bot.llm_check
```

## Локальная LLM

Короткая аннотация бага для поста в Bug Reports пишется локальной моделью (llama-server `llm-qwen`, OpenAI-совместимый `/v1/chat/completions`). Промпт — в `bot/services/llm.py` (`BUG_SUMMARY_SYSTEM`): 1–2 предложения «что сломано и где», без контактов, ID, сумм, логов и шагов. Поверх ответа ещё regex-чистка ссылок/почт/телефонов/токенов. Если LLM недоступна или не ответила за `LLM_TIMEOUT` — пост уйдёт с обрезанным описанием. Аннотация считается один раз и хранится в `reports.fields.summary`.

Адрес: `LLM_BASE_URL`, если задан. Иначе автоопределение по первому живому `/health`: в Docker → `host.docker.internal:8301` (бот на сервере рядом с llm-qwen), затем `LLM_SERVER_HOST:8301`; на Маке → сразу `192.168.1.145:8301`.

Проверить связь и промпт: `python -m bot.llm_check` (на примере с «секретами») или `python -m bot.llm_check "свой текст"`.

Ещё куда можно подключить: Analyzer в модерации Main (список `analyzers` в `__main__.py`), поиск дублей.

## Тесты

```bash
pip install -r requirements-dev.txt
pytest
```
