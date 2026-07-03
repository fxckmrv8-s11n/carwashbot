"""
Единая точка входа: запускает Telegram-бота и веб-сервер Mini App
в ОДНОМ процессе/контейнере — это гарантирует, что оба пишут и читают
одни и те же файлы данных (carwash_sessions.json и т.д.), то есть
касса, машины и мойщики синхронны между ботом и приложением без
дополнительной настройки (общий диск не нужен).

Команда запуска на Railway (Settings → Deploy → Custom Start Command):
    python run_all.py
"""
import os
import threading

import uvicorn

from bot import main as bot_main
from sessions import load_sessions


def run_web():
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("webapp.server:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    load_sessions()  # грузим данные с диска в общую память ДО старта обоих сервисов
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    print("🌐 Веб-сервер (Mini App) запущен в фоновом потоке")
    bot_main()  # блокирующий вызов — держит процесс живым
