# -*- coding: utf-8 -*-
"""Shared bits: import bot.py in a sandbox, writing nothing into the project."""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_bot(**env):
    """Import bot.py with a clean environment. Every call is a fresh module."""
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
    # Clear anything that could have leaked in from the CI or host environment.
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


class _LazyBot:
    """Imports bot.py on first attribute access, not when the test file loads.

    A large share of these tests only read source files and need none of the
    bot's dependencies. Importing eagerly at module level made those tests
    unrunnable anywhere aiogram is missing — including every quick check
    before a push, which is exactly when a broken assertion should surface.
    """

    _mod = None

    def __getattr__(self, name):
        if _LazyBot._mod is None:
            _LazyBot._mod = load_bot()
        return getattr(_LazyBot._mod, name)


BOT = _LazyBot()


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


if __name__ == "__main__":  # quick check of the helper itself
    b = load_bot()
    print("bot.py imports fine, services:", len(b.SERVICES))
