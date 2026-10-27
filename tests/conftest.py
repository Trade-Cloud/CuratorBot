import os

# Settings не читаются в тестах, но на всякий случай, чтобы импорт config не падал.
os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("CHAT_ID", "-1001")
