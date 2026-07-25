# -*- coding: utf-8 -*-
"""Спільне для тестів: імпорт bot.py у пісочниці, без запису на диск проєкту."""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_bot(**env):
    """Імпортує bot.py з чистими змінними оточення. Кожен виклик — новий модуль."""
    tmp = tempfile.mkdtemp(prefix="sdp-test-")
    base = {
        "BOT_TOKEN": "1:test",
        "ADMIN_ID": "777",
        "STATS_DB": os.path.join(tmp, "stats.db"),
        "CACHE_FILE": os.path.join(tmp, "cache.json"),
        "COOKIES_FILE": os.path.join(tmp, "cookies.txt"),
        "WEBAPP_ENABLED": "0",
        "GITHUB_REPO": "off",
    }
    base.update({k: str(v) for k, v in env.items()})

    saved = dict(os.environ)
    # Прибираємо все, що могло протекти з оточення CI або хоста.
    for k in list(os.environ):
        if k.startswith(("ENABLE_", "MAX_", "LONG_", "COBALT_", "VERIFY_", "CACHE_",
                         "WEBAPP_", "TELEGRAM_", "COOKIES_", "GITHUB_", "YTDLP_",
                         "PLAYLIST_", "THUMBNAILS", "LITE", "ALLOWED_", "AUDIO_",
                         "TITLES_", "BOT_", "ADMIN_", "STATS_", "FLOOD_", "JOB_",
                         "CHAT_", "CHECK_", "CONCURRENT_", "POT_", "PROXY", "TIKWM")):
            os.environ.pop(k, None)
    os.environ.update(base)
    try:
        spec = importlib.util.spec_from_file_location("sdp_bot", ROOT / "bot.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod._test_tmp = tmp
        return mod
    finally:
        os.environ.clear()
        os.environ.update(saved)


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


if __name__ == "__main__":  # швидка перевірка самого хелпера
    b = load_bot()
    print("bot.py імпортується, сервісів:", len(b.SERVICES))
