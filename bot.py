"""
Telegram-bot for downloading videos from TikTok / YouTube Shorts / Instagram Reels
(+ full YouTube videos and MP3 extraction in private chats).

aiogram 3.x + yt-dlp, with tikwm/Cobalt fallbacks for TikTok photo carousels.

Chat behaviour:
- Group chats: only short-form (TikTok / IG Reels / FB / YouTube Shorts).
  Full YouTube (`youtube.com/watch`) links are ignored to avoid heavy downloads.
- Private chat: everything above PLUS full YouTube videos and MP3 extraction
  (write "mp3" / "аудіо" / "музика" next to the link).

Other features: integrity check before sending (re-download if corrupt / no audio),
MP3 named after the source, thumbnails + aspect ratio/duration (ffprobe/ffmpeg),
per-link file cache (only valid files), /health endpoint, smart quality fallback,
admin update notifications.
"""

import asyncio
import contextvars
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import socket
import sqlite3
import tempfile
import threading
import time
import uuid
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatAction, ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)
from aiogram.utils.chat_action import ChatActionSender

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

def env_secret(name, default=""):
    """Read a secret from <NAME>_FILE if it is set, otherwise from <NAME>.

    Anything placed in the environment is readable by everyone who can run
    `docker inspect`, and it is shown in plain text on the stack screen in
    Portainer. A file — a Docker secret, or just a file with tight permissions
    — is not, so the file wins whenever both are present.
    Усе, що покладено в оточення, читається кожним, хто може виконати
    `docker inspect`, і показується відкритим текстом на екрані стека в
    Portainer. Файл — секрет Docker або просто файл із вузькими правами —
    ні, тому за наявності обох перемагає файл.
    """
    path = os.getenv(name + "_FILE", "").strip()
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            # No logger yet at import time — stderr is what there is.
            print("[config] cannot read %s_FILE (%s): %s" % (name, path, exc))
    return os.getenv(name, default).strip()


BOT_TOKEN = env_secret("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
# LITE: drop the bot into a group and forget about it. No database, no cache,
# no Mini App, no private chat — just short videos where they are posted.
LITE = os.getenv("LITE", "0").strip().lower() in ("1", "true", "yes", "on")
# LITE only: chats the bot may work in. Empty = any group it is added to.
ALLOWED_CHATS = {
    c.strip() for c in os.getenv("ALLOWED_CHATS", "").replace(";", ",").split(",")
    if c.strip()
}
# Defaults for the two behaviour switches that LITE cannot edit at runtime.
AUDIO_TOO = os.getenv("AUDIO_TOO", "0").strip().lower() in ("1", "true", "yes", "on")
TITLES_MODE = os.getenv("TITLES_MODE", "private").strip().lower()
# Bot language when there is no database to store it in (LITE): uk | en.
BOT_LANG = os.getenv("BOT_LANG", "uk").strip().lower()
# How often to check for updates, in minutes. CHECK_INTERVAL_HOURS is still
# honoured for older deployments that set it.
_legacy_hours = os.getenv("CHECK_INTERVAL_HOURS")
CHECK_INTERVAL_MINUTES = int(
    os.getenv("CHECK_INTERVAL_MINUTES")
    or (int(_legacy_hours) * 60 if _legacy_hours else 15)
)
# Which repo to watch for updates. Defaults to upstream, so a fresh deploy
# tells its owner "time to Pull and redeploy" without any configuration.
# Point it at your own fork, or set GITHUB_REPO=off to stay silent.
UPSTREAM_REPO = "TameyAnderson/sdp-downloader"
GITHUB_REPO = os.getenv("GITHUB_REPO", UPSTREAM_REPO).strip()
if GITHUB_REPO.lower() in ("off", "no", "none", "0"):
    GITHUB_REPO = ""
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()
# Personal Access Token — потрібен ЛИШЕ для приватного репозиторію
# (fine-grained, доступ лише на читання Contents цього репо).
# Для публічного апстріму токен не потрібен взагалі.
GITHUB_TOKEN = env_secret("GITHUB_TOKEN")
# Local Bot API server lifts Telegram's 50 MB upload limit up to 2 GB.
# Set TELEGRAM_API_URL to http://telegram-bot-api:8081 after migrating (see README).
TELEGRAM_API_URL = os.getenv("TELEGRAM_API_URL", "").strip()
# With a local Bot API the limit is 2 GB; otherwise Telegram's 50 MB.
# An explicit non-empty MAX_FILE_SIZE_MB always wins.
_mb = os.getenv("MAX_FILE_SIZE_MB", "").strip()
MAX_FILE_SIZE = int(_mb or ("2000" if TELEGRAM_API_URL else "50")) * 1024 * 1024

# Where downloads are assembled. In the shipped stacks /tmp is a tmpfs, i.e.
# RAM with a hard ceiling — comfortable and fast for short clips, but a file
# larger than that ceiling cannot be downloaded at all, and the failure arrives
# as "No space left on device" from somewhere deep inside yt-dlp. Point this at
# a directory on the data volume if you raised MAX_FILE_MB for the local Bot API.
# Де збираються завантаження. У наших стеках /tmp — це tmpfs, тобто RAM із
# жорсткою стелею: зручно й швидко для коротких кліпів, але файл, більший за цю
# стелю, не завантажиться взагалі, а помилка прилетить як "No space left on
# device" звідкись із глибин yt-dlp. Якщо піднімав MAX_FILE_MB заради
# локального Bot API — вкажи тут теку на постійному томі.
WORK_DIR = os.getenv("WORK_DIR", "").strip() or tempfile.gettempdir()
# Refuse to start a job with less than this much room left. A deliberate floor
# rather than "MAX_FILE_SIZE x 2": on a small tmpfs that formula would refuse
# every job, including the short clips that fit perfectly well.
# Не братися за завдання, коли лишилось менше за це. Свідома нижня межа, а не
# "MAX_FILE_SIZE x 2": на невеликому tmpfs така формула відхиляла б геть усе,
# зокрема й короткі кліпи, які чудово вміщаються.
MIN_FREE_SPACE = int(os.getenv("MIN_FREE_MB", "256")) * 1024 * 1024
# Above this size the full decode is skipped even with VERIFY_DEEP on. 0 = never skip.
# Понад цей розмір повний декод пропускається навіть із увімкненим VERIFY_DEEP.
# 0 — не пропускати ніколи.
VERIFY_DEEP_MAX_SIZE = int(os.getenv("VERIFY_DEEP_MAX_MB", "300")) * 1024 * 1024
# Whole-job time limits, seconds. Short-form content that takes fifteen minutes
# is not going to succeed on the sixteenth.
# Ліміти часу на всю джобу, секунди. Коротке відео, яке качається п'ятнадцять
# хвилин, на шістнадцятій не дозавантажиться.
JOB_DEADLINE_SHORT = int(os.getenv("JOB_DEADLINE_SEC", "900"))
JOB_DEADLINE_LONG = int(os.getenv("JOB_DEADLINE_LONG_SEC", "3600"))
# Circuit breaker: this many failures in a row on one (platform, engine) pair
# take that engine out for a while.
# Запобіжник: стільки збоїв поспіль на парі (платформа, рушій) — і рушій
# вимикається на певний час.
ENGINE_TRIP_AFTER = int(os.getenv("ENGINE_TRIP_AFTER", "5"))
ENGINE_TRIP_SECONDS = float(os.getenv("ENGINE_TRIP_SECONDS", "600"))
# Largest .db the bot will accept back as a restore.
# Найбільший .db, який бот прийме назад як відновлення.
RESTORE_MAX_BYTES = int(os.getenv("RESTORE_MAX_MB", "64")) * 1024 * 1024
# Where to also write the session in Cobalt's own format. Empty = do not.
# Куди ще записати сесію у власному форматі Cobalt. Порожньо — не записувати.
COBALT_COOKIES_PATH = os.getenv("COBALT_COOKIES_PATH", "").strip()
# How long a running job may finish after a stop signal. Docker waits 10s by
# default before killing the container, so staying under that is the point.
# Скільки джоба, що вже виконується, може доробляти після сигналу зупинки.
# Docker типово чекає 10 с, перш ніж убити контейнер, — тож сенс у тому,
# щоб укластися в цей час.
SHUTDOWN_GRACE = float(os.getenv("SHUTDOWN_GRACE_SEC", "8"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "8"))
# Telegram allows ~20 messages/minute per chat. On 429 we wait and retry
# instead of losing the upload; sends to one chat are spaced out.
FLOOD_MAX_RETRIES = int(os.getenv("FLOOD_MAX_RETRIES", "5"))
CHAT_SEND_INTERVAL = float(os.getenv("CHAT_SEND_INTERVAL", "1.0"))
# Anti-abuse: cap links per message and parallel downloads per user.
MAX_LINKS_PER_MESSAGE = int(os.getenv("MAX_LINKS_PER_MESSAGE", "10"))
MAX_PER_USER = int(os.getenv("MAX_PER_USER", "3"))
# Retry a job that died on a transient error (network blip, timeout).
JOB_RETRIES = int(os.getenv("JOB_RETRIES", "1"))
JOB_RETRY_DELAY = float(os.getenv("JOB_RETRY_DELAY", "5"))
# Notify when a download took longer than this (seconds). 0 = never.
LONG_JOB_NOTIFY = int(os.getenv("LONG_JOB_NOTIFY", "60"))
MAX_HEIGHT = int(os.getenv("MAX_HEIGHT", "720"))
# Lives on the botdata volume by default, so an upload survives redeploys.
COOKIES_FILE = os.getenv("COOKIES_FILE", "/data/cookies.txt").strip()
# Max size of an uploaded cookies file (bytes).
COOKIES_MAX_BYTES = int(os.getenv("COOKIES_MAX_BYTES", "262144"))
# Remind the admin this many days before the cookies session expires. 0 = off.
COOKIES_WARN_DAYS = int(os.getenv("COOKIES_WARN_DAYS", "3"))
PROXY = os.getenv("PROXY", "").strip()
# TikTok: go through the mobile API instead of parsing the web page — see
# _with_auth(). Empty value = off, back to the HTML parser.
# TikTok: ходити в мобільний API замість парсингу сторінки — див. _with_auth().
# Порожнє значення = вимкнено, повертаємось до розбору HTML.
TIKTOK_API_HOSTNAME = os.getenv(
    "TIKTOK_API_HOSTNAME", "api22-normal-c-useast2a.tiktokv.com").strip()

# Health endpoint + persistent file cache.
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8080"))
CACHE_FILE = os.getenv("CACHE_FILE", "/data/cache.json").strip()
CACHE_MAX = int(os.getenv("CACHE_MAX", "1000"))
# Кеш file_id: за замовчуванням ВИМКНЕНО. Зберігає лише ID, не відео.
ENABLE_CACHE = os.getenv("ENABLE_CACHE", "0").strip().lower() in ("1", "true", "yes", "on")
CACHE_CLEAR_INTERVAL_DAYS = int(os.getenv("CACHE_CLEAR_INTERVAL_DAYS", "7"))

# Verify each downloaded video decodes cleanly before sending (slower, but no
# broken/silent files). Set to 0 to disable the full decode pass.
VERIFY_MEDIA = os.getenv("VERIFY_MEDIA", "1").strip().lower() in ("1", "true", "yes", "on")
# Full decode of the whole file (thorough, slow). Off = fast check (probe + tail).
VERIFY_DEEP = os.getenv("VERIFY_DEEP", "0").strip().lower() in ("1", "true", "yes", "on")
# yt-dlp: download N fragments of one video in parallel (speeds up YouTube/HLS).
CONCURRENT_FRAGMENTS = int(os.getenv("CONCURRENT_FRAGMENTS", "5"))
# Anti-block pauses between requests. Off by default = faster; enable if blocked.
YTDLP_SLEEP = os.getenv("YTDLP_SLEEP", "0").strip().lower() in ("1", "true", "yes", "on")
# Generate a custom thumbnail per video (nicer preview, small time cost).
THUMBNAILS = os.getenv("THUMBNAILS", "1").strip().lower() in ("1", "true", "yes", "on")
# Mini App (Telegram Web App), served over HTTPS via a Cloudflare tunnel.
WEBAPP_ENABLED = (os.getenv("WEBAPP_ENABLED", "0").strip().lower()
                  in ("1", "true", "yes", "on")) and not LITE
INDEX_HTML_PATH = os.getenv("INDEX_HTML_PATH", "/app/index.html")
# Reject Mini App requests whose signature is older than this (seconds). 0 = off.
WEBAPP_AUTH_TTL = int(os.getenv("WEBAPP_AUTH_TTL", "86400"))
# SQLite for events log / history / stats (kept on the botdata volume).
STATS_DB = os.getenv("STATS_DB", "/data/stats.db")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

ENABLE_COBALT = os.getenv("ENABLE_COBALT", "1").strip().lower() in ("1", "true", "yes", "on")
COBALT_API_URL = os.getenv("COBALT_API_URL", "http://cobalt-api:9010").strip()
# Optional secondary Cobalt endpoint (e.g. a public instance) tried as last resort.
COBALT_FALLBACK_URL = os.getenv("COBALT_FALLBACK_URL", "").strip()
# Instagram often returns a broken/partial answer that becomes fine seconds later,
# so retry the same Cobalt request before moving on to another engine.
COBALT_RETRIES = int(os.getenv("COBALT_RETRIES", "2"))
COBALT_RETRY_DELAY = float(os.getenv("COBALT_RETRY_DELAY", "3"))

ENABLE_TIKWM = os.getenv("ENABLE_TIKWM", "1").strip().lower() in ("1", "true", "yes", "on")
TIKWM_API = os.getenv("TIKWM_API", "https://tikwm.com/api/").strip()

# PO-token провайдер (bgutil) — обхід YouTube-захисту. Порожньо = вимкнено.
POT_PROVIDER_URL = os.getenv("POT_PROVIDER_URL", "http://bgutil-provider:4416").strip()


def _env_bool(name, default=True):
    return os.getenv(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


ENABLE_TIKTOK = _env_bool("ENABLE_TIKTOK", True)
ENABLE_YOUTUBE = _env_bool("ENABLE_YOUTUBE", True)
ENABLE_INSTAGRAM = _env_bool("ENABLE_INSTAGRAM", True)
ENABLE_FACEBOOK = _env_bool("ENABLE_FACEBOOK", True)


# ----------------------------------------------------------------------------
# Service registry. Each entry: URL patterns, engine order (cobalt/ytdlp),
# access tier and default state. New services default to OFF — enable them
# per-service from the Mini App (svc:<id> setting) or ENABLE_<ID> env.
# ----------------------------------------------------------------------------
SERVICES = {
    "youtube": {"label": "YouTube", "default": True, "tier": "yt", "merge": True,
                "engines": ("ytdlp", "cobalt"),
                "pats": [r"(?:www\.|m\.)?youtube\.com/shorts/[^\s]*",
                         r"(?:www\.|m\.|music\.)?youtube\.com/watch\?[^\s]*",
                         r"(?:www\.|m\.|music\.)?youtube\.com/playlist\?[^\s]*",
                         r"youtu\.be/[^\s]*"]},
    "tiktok": {"label": "TikTok", "default": True, "tier": "basic", "carousel": True,
               "engines": ("ytdlp", "cobalt"),
               "pats": [r"(?:www\.|vm\.|vt\.|m\.)?(?:tiktok\.com|douyin\.com)/[^\s]*"]},
    "instagram": {"label": "Instagram", "default": True, "tier": "basic", "strip": True,
                  "engines": ("cobalt", "ytdlp"),
                  "pats": [r"(?:www\.|m\.)?(?:instagram\.com|instagr\.am)/[^\s]*"]},
    "facebook": {"label": "Facebook", "default": True, "tier": "basic",
                 "engines": ("cobalt", "ytdlp"),
                 "pats": [r"(?:www\.|m\.|web\.)?facebook\.com/[^\s]*", r"fb\.watch/[^\s]*"]},
    "twitter": {"hidden": True, "label": "Twitter / X", "default": False, "tier": "basic",
                "engines": ("cobalt", "ytdlp"),
                "pats": [r"(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/[^\s]*"]},
    "reddit": {"hidden": True, "label": "Reddit", "default": False, "tier": "basic",
               "engines": ("cobalt", "ytdlp"),
               "pats": [r"(?:www\.|old\.)?reddit\.com/[^\s]*", r"redd\.it/[^\s]*"]},
    "pinterest": {"hidden": True, "label": "Pinterest", "default": False, "tier": "basic",
                  "engines": ("cobalt", "ytdlp"),
                  "pats": [r"(?:www\.|[a-z]{2}\.)?pinterest\.[a-z.]+/[^\s]*", r"pin\.it/[^\s]*"]},
    "tumblr": {"hidden": True, "label": "Tumblr", "default": False, "tier": "basic",
               "engines": ("cobalt", "ytdlp"),
               "pats": [r"[a-z0-9-]+\.tumblr\.com/[^\s]*", r"(?:www\.)?tumblr\.com/[^\s]*"]},
    "snapchat": {"hidden": True, "label": "Snapchat", "default": False, "tier": "basic",
                 "engines": ("cobalt", "ytdlp"),
                 "pats": [r"(?:www\.)?snapchat\.com/[^\s]*"]},
    "bluesky": {"hidden": True, "label": "Bluesky", "default": False, "tier": "basic",
                "engines": ("cobalt", "ytdlp"),
                "pats": [r"(?:www\.)?bsky\.app/[^\s]*"]},
    "vimeo": {"hidden": True, "label": "Vimeo", "default": False, "tier": "extended", "merge": True,
              "engines": ("ytdlp", "cobalt"),
              "pats": [r"(?:www\.|player\.)?vimeo\.com/[^\s]*"]},
    "dailymotion": {"hidden": True, "label": "Dailymotion", "default": False, "tier": "extended", "merge": True,
                    "engines": ("ytdlp", "cobalt"),
                    "pats": [r"(?:www\.)?dailymotion\.com/[^\s]*", r"dai\.ly/[^\s]*"]},
    "bilibili": {"hidden": True, "label": "Bilibili", "default": False, "tier": "extended", "merge": True,
                 "engines": ("ytdlp", "cobalt"),
                 "pats": [r"(?:www\.)?bilibili\.(?:com|tv)/[^\s]*", r"b23\.tv/[^\s]*"]},
    "twitch": {"label": "Twitch", "default": False, "tier": "extended", "merge": True,
               "engines": ("ytdlp", "cobalt"),
               "pats": [r"(?:www\.|clips\.|m\.)?twitch\.tv/[^\s]*"]},
    "soundcloud": {"label": "SoundCloud", "default": False, "tier": "basic", "audio": True,
                   "engines": ("ytdlp", "cobalt"),
                   "pats": [r"(?:www\.|m\.)?soundcloud\.com/[^\s]*", r"snd\.sc/[^\s]*"]},
    "spotify": {"label": "Spotify", "i18n": "svc_spotify", "default": False, "tier": "basic",
                "audio": True, "resolve": True, "engines": ("ytdlp",),
                "pats": [r"open\.spotify\.com/(?:intl-[a-z]+/)?track/[^\s]*"]},
    "deezer": {"label": "Deezer", "i18n": "svc_deezer", "default": False, "tier": "basic",
               "audio": True, "resolve": True, "engines": ("ytdlp",),
               "pats": [r"(?:www\.)?deezer\.com/(?:[a-z]{2}/)?track/[^\s]*",
                        r"deezer\.page\.link/[^\s]*"]},
    "bandcamp": {"hidden": True, "label": "Bandcamp", "default": False, "tier": "basic", "audio": True,
                 "engines": ("ytdlp", "cobalt"),
                 "pats": [r"[a-z0-9-]+\.bandcamp\.com/(?:track|album)/[^\s]*",
                          r"(?:www\.)?bandcamp\.com/[^\s]*"]},
    # Keep LAST: matches anything yt-dlp might support. Enabled = "All-in".
    "allin": {"label": "All-in", "i18n": "svc_allin", "default": False, "tier": "extended",
              "merge": True, "engines": ("ytdlp",), "pats": [r"[^\s]+"]},
}

for _sid, _svc in SERVICES.items():
    _svc["_re"] = re.compile(r"https?://(?:" + "|".join(_svc["pats"]) + r")", re.IGNORECASE)


def service_enabled(sid):
    svc = SERVICES.get(sid)
    if not svc:
        return False
    if svc.get("hidden"):
        # Niche platforms are not shown separately — they come with "All-in"
        # (and keep their own optimised engine chain instead of plain yt-dlp).
        return service_enabled("allin")
    v = setting("svc:" + sid, None)
    if v is not None:
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    env = os.getenv("ENABLE_" + sid.upper())
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    return bool(svc.get("default", False))

# Long videos (full YouTube in private): try up to 4K, step down, not below 1080.
LONG_MAX_HEIGHT = int(os.getenv("LONG_MAX_HEIGHT", "2160"))
LONG_MIN_HEIGHT = int(os.getenv("LONG_MIN_HEIGHT", "720"))
PLAYLIST_MAX = int(os.getenv("PLAYLIST_MAX", "20"))
_STEPS = (4320, 2160, 1440, 1080, 720, 480, 360)


def quality_ladder():
    """Short-form ladder, rebuilt on every job so the cap can change live."""
    cap = tunable("max_height")
    return [h for h in sorted({cap, 480, 360}, reverse=True) if h <= cap] or [cap]


def long_ladder():
    lo, hi = tunable("long_min_height"), tunable("long_max_height")
    if lo > hi:
        lo = hi
    return [h for h in _STEPS if lo <= h <= hi] or [hi]

WATCHED_PACKAGES = ("yt-dlp", "aiogram")

class _JobIdFilter(logging.Filter):
    """Stamp every line with the job it belongs to.

    Several downloads run at once and their lines interleave, so reading a log
    meant guessing which "yt-dlp 480p" belonged to which link. The job id is
    already in a contextvar; this only puts it where it can be seen.
    Кілька завантажень ідуть одночасно, і їхні рядки перемішуються — читати лог
    означало вгадувати, до якого посилання належить котре «yt-dlp 480p».
    Ідентифікатор джоби вже лежить у contextvar, тут він лише стає видимим.
    """

    def filter(self, record):
        job = _JOB.get() if "_JOB" in globals() else None
        record.job = ("[%s] " % str(job["id"])[:8]) if job else ""
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(job)s%(message)s",
)
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_JobIdFilter())
logger = logging.getLogger("video-bot")

# Set this only if you deliberately point the bot at something on your own
# network. It disables the check below entirely.
# Вмикай, лише якщо свідомо наводиш бота на щось у власній мережі.
# Це повністю вимикає перевірку нижче.
ALLOW_PRIVATE_HOSTS = os.getenv("ALLOW_PRIVATE_HOSTS", "0").strip().lower() in (
    "1", "true", "yes", "on")

# Panel API: how many requests one caller may make per window.
# API панелі: скільки запитів дозволено одному відвідувачу за вікно.
API_RATE_LIMIT = int(os.getenv("API_RATE_LIMIT", "120"))
API_RATE_WINDOW = float(os.getenv("API_RATE_WINDOW", "60"))
_api_hits = {}           # caller key -> [monotonic timestamps]


def _addr_is_public(raw):
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def _own_service_hosts():
    """Hosts that belong to this deployment and are internal on purpose."""
    hosts = set()
    for raw in (COBALT_API_URL, COBALT_FALLBACK_URL, TELEGRAM_API_URL, POT_PROVIDER_URL):
        if not raw:
            continue
        try:
            parts = urlsplit(raw if "//" in raw else "//" + raw)
        except ValueError:
            continue
        if parts.hostname:
            hosts.add((parts.hostname.lower(), parts.port))
    return hosts


async def url_is_safe(url):
    """Refuse a link that points back inside our own network.

    The All-in service matches any URL whatsoever, so a link dropped in a group
    can aim the downloader at the host itself: a router panel, a neighbouring
    container, or the cloud metadata service on 169.254.169.254, which hands
    credentials to anything asking from inside. yt-dlp would fail to extract
    such a page, but the request still goes out — and the request IS the attack.

    The check resolves the name here and now, so a server that answers with a
    public address and then flips to a private one (DNS rebinding) can still
    slip past. Stopping that needs pinning the resolved address for the whole
    download, which is not something yt-dlp lets us do from outside.

    Сервіс All-in підходить під будь-яке посилання взагалі, тож лінк, кинутий
    у групу, здатен навести завантажувач на сам сервер: панель роутера, сусідній
    контейнер або службу метаданих на 169.254.169.254, яка віддає облікові дані
    кожному, хто спитає зсередини. yt-dlp таку сторінку не розбере, але запит
    усе одно піде — а сам запит і є атакою.

    Перевірка резолвить ім'я тут і зараз, тому сервер, який спершу віддає
    публічну адресу, а потім приватну (DNS rebinding), ще може прослизнути.
    Щоб закрити й це, треба закріпити резолвлену адресу на весь час
    завантаження, а такої можливості yt-dlp ззовні не дає.
    """
    if ALLOW_PRIVATE_HOSTS:
        return True
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        logger.warning("Refused a non-http link (%s)", parts.scheme or "no scheme")
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    if (host, parts.port) in _own_service_hosts():
        return True                      # our own Cobalt / Bot API / PO provider
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, parts.port or None,
                                        0, socket.SOCK_STREAM)
    except Exception:  # noqa: BLE001
        logger.warning("Could not resolve %s — refusing the link", host)
        return False
    for info in infos:
        ip = info[4][0]
        if not _addr_is_public(ip):
            logger.warning("Refused a link aimed inside the network: %s -> %s", host, ip)
            return False
    return True


class Limiter:
    """Semaphore whose ceiling can be changed from the panel between jobs."""

    def __init__(self, key):
        self._key = key
        self._cond = asyncio.Condition()
        self._busy = 0

    def limit(self):
        return tunable(self._key)

    async def __aenter__(self):
        async with self._cond:
            await self._cond.wait_for(lambda: self._busy < self.limit())
            self._busy += 1
        return self

    async def __aexit__(self, *_exc):
        async with self._cond:
            self._busy -= 1
            self._cond.notify()
        return False

    def idle(self):
        return self._busy == 0


download_semaphore = Limiter("max_concurrent")
_user_sems = {}          # user_id -> Limiter (parallel downloads per user)
_chat_last_send = {}     # chat_id -> monotonic timestamp of the last send
_chat_send_lock = {}     # chat_id -> asyncio.Lock

# Background tasks the loop must not lose. asyncio keeps only a weak reference,
# so a task nobody else holds can be collected while it is still running and
# simply stop — with no error anywhere.
# Фонові задачі, які не можна загубити. asyncio тримає лише слабке посилання,
# тож задачу, яку більше ніхто не тримає, збирач сміття може прибрати просто
# посеред роботи, і вона мовчки зникне — без жодної помилки.
_background = set()


def spawn(coro):
    """Start a background task and keep a strong reference until it finishes."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


def _user_gate(user_id):
    """Per-user gate for parallel downloads.

    Limiter re-reads its ceiling on every entry, so a change from the panel
    reaches the jobs already queued. Rebuilding a Semaphore instead — as this
    used to do — left the running jobs holding the old gate while a brand-new,
    completely free one stood next to it, and the limit was briefly doubled.
    Ліміт паралельних завантажень на користувача. Limiter перечитує стелю на
    кожному вході, тож зміна з панелі діє й на вже поставлені в чергу джоби.
    Перестворення Semaphore, як було раніше, лишало запущені джоби на старому
    воротарі, а поряд ставав новий і геть порожній — ліміт на мить подвоювався.
    """
    gate = _user_sems.get(user_id)
    if gate is None:
        gate = _user_sems[user_id] = Limiter("max_per_user")
    return gate


class FloodMiddleware:
    """Outgoing-request middleware: survives Telegram's rate limit (429) and
    spaces out messages sent to the same chat."""

    async def __call__(self, make_request, bot, method):
        chat_id = getattr(method, "chat_id", None)
        max_retries = tunable("flood_retries")

        async def space_out():
            """Hold the gap before an attempt and stamp the attempt that runs.

            Doing this once before the loop meant a retry after a 429 went out
            with no gap at all, and the stored timestamp kept pointing at the
            attempt Telegram had just rejected — so the next message measured
            its wait from the wrong moment and could fire too early as well.
            Витримати паузу перед спробою і зафіксувати ту спробу, що реально
            йде. Коли це робилось один раз до циклу, повтор після 429 летів
            узагалі без паузи, а збережена мітка часу лишалась на спробі, яку
            Telegram щойно відхилив, — тож наступне повідомлення рахувало
            паузу від неправильного моменту й теж могло піти зарано.
            """
            interval = tunable("chat_interval")
            if chat_id is None or interval <= 0:
                return
            lock = _chat_send_lock.setdefault(chat_id, asyncio.Lock())
            async with lock:
                last = _chat_last_send.get(chat_id, 0.0)
                wait = interval - (time.monotonic() - last)
                if wait > 0:
                    await asyncio.sleep(wait)
                _chat_last_send[chat_id] = time.monotonic()

        for attempt in range(max_retries + 1):
            await space_out()
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as exc:
                if attempt >= max_retries:
                    logger.error("Flood limit: giving up after %d retries", attempt)
                    raise
                delay = getattr(exc, "retry_after", 5) + 1
                logger.warning("Flood limit hit, waiting %ss (retry %d/%d)",
                               delay, attempt + 1, max_retries)
                await asyncio.sleep(delay)
        raise RuntimeError("unreachable")

_notified = {}


# ----------------------------------------------------------------------------
# i18n (UK / EN) - global language, switched from the Mini App settings
# ----------------------------------------------------------------------------

T = {
    "uk": {
        "too_big_media": "⚠️ Медіа завелике (ліміт {mb} МБ).",
        "too_big_video": "⚠️ Відео завелике навіть у мінімальній якості (ліміт {mb} МБ).",
        "cant_whole_file": "⚠️ не вдалося отримати цілий файл, спробуй трохи пізніше.",
        "too_big_audio": "⚠️ Аудіо завелике (ліміт {mb} МБ).",
        "cant_audio": "⚠️ не вдалося витягти аудіо.",
        "cant_link": "⚠️ не вдалося обробити посилання.",
        "no_space": "⚠️ На сервері закінчилось місце. Спробуй трохи пізніше.",
        "err_private": "🔒 Пост закритий — потрібен доступ до акаунта.",
        "err_age": "🔞 Віковий доступ. Потрібні cookies залогіненого акаунта.",
        "err_geo": "🌍 Недоступно з країни, де стоїть сервер.",
        "err_gone": "🗑 Пост видалено або він більше не існує.",
        "err_extractor": "🛠 Сервіс змінив розмітку — чекаємо оновлення yt-dlp.",
        "cant_carousel": "⚠️ не вдалося завантажити карусель.",
        "cant_video": "⚠️ не вдалося завантажити відео за цим посиланням.",
        "proc_error": "⚠️ Сталася помилка під час обробки.",
        "no_access": "⛔ У вас немає доступу до цього бота.",
        "start_private": ("Надішли посилання на відео (TikTok, YouTube, Instagram, "
                          "Facebook) - поверну відео.\nДодай слово «mp3» поруч із "
                          "YouTube-силкою - витягну аудіо."),
        "start_group": "Кидай короткі відео (TikTok / Reels / Shorts) - завантажу.",
        "checking_versions": "Перевіряю версії…",
        "your_id": "Твій id: {id}",
        "bot_started": "✅ s.d.p працює\n\nВерсії:\n{r}",
        "lite_private": ("Я працюю в групах. Додай мене в чат — і я качатиму звідти\nкороткі відео з посилань."),
        "err_timeout": "час очікування вичерпано.",
        "err_internal": "внутрішня помилка: {err}",
        "err_download": "не вдалося завантажити за цим посиланням.",
        "err_nofile": "файл не знайдено після завантаження.",
        "rep_version": "\U0001f534 нова версія: {sha} — {msg}",
        "rep_lib": "\U0001f534 {name}: {cur} \u2192 {latest} (є оновлення)",
        "rep_ck_forever": "\U0001f7e2 cookies: без терміну дії",
        "rep_ck_expired": "\U0001f534 cookies: ПРОТЕРМІНОВАНІ",
        "rep_ck_soon": "\U0001f7e1 cookies: лишилось ~{days} дн.",
        "rep_ck_ok": "\U0001f7e2 cookies: ще ~{days} дн.",
        "no_platforms": "ЖОДНОЇ (всі вимкнено!)",
        "svc_spotify": "Spotify (пошук на YouTube)",
        "svc_deezer": "Deezer (пошук на YouTube)",
        "svc_allin": "All-in — усі інші сайти (1500+)",
        "new_version": ("🆕 Вийшла нова версія s.d.p — час оновитись.\n"
                        "Зробіть Pull and redeploy у Portainer.\n{what}"),
        "new_lib": "🆕 Вийшла нова версія {name} ({cur} → {latest}) — зробіть Pull and redeploy.",
        "done_long": "✅ Готово (зайняло {s} с).",
        "ck_saved": ("🍪 Cookies збережено: {n} записів ({sites}).\n"
                     "Ключі: {auth}. Діють ще ~{days} дн.\n"
                     "yt-dlp тепер основний для Instagram/Facebook."),
        "ck_status": ("🍪 Cookies активні: {n} записів ({sites}).\n"
                      "Ключі: {auth}. Діють ще ~{days} дн."),
        "ck_absent": "Cookies не підключені. Надішли файл cookies.txt сюди — я збережу.",
        "ck_no_auth": "⚠️ У файлі немає ключів сесії (sessionid/c_user). Експортуй cookies із залогіненого браузера.",
        "ck_bad": "⚠️ Не вдалося прочитати файл. Потрібен формат Netscape (cookies.txt).",
        "ck_fetch_fail": ("⚠️ Не вдалося забрати файл із Telegram: {err}\n"
                          "Обхід: відкрий cookies.txt у блокноті, скопіюй увесь текст "
                          "і надішли його сюди звичайним повідомленням."),
        "ck_too_big": "⚠️ Файл завеликий для cookies.",
        "ck_deleted": "🍪 Cookies видалено. Instagram знову працюватиме лише з публічними постами.",
        "bk_ok": "💾 Резервна копія бази ({kb} КБ): статистика, налаштування, білі списки.",
        "bk_none": "Базу ще не створено — немає що зберігати.",
        "rs_ok": ("♻️ Базу відновлено з копії: {n} записів.\n"
                  "Стару збережено поруч як stats.db.replaced."),
        "rs_bad": "⚠️ Не вдалося відновити базу: {why}. Поточна лишилась без змін.",
        "rs_too_big": "⚠️ Файл завеликий для відновлення.",
        "bk_fail": "⚠️ Не вдалося зробити резервну копію.",
        "ck_expiring": ("⏳ Cookies спливають приблизно через {days} дн.\n"
                        "Онови сесію: зайди в акаунт, експортуй cookies.txt "
                        "і надішли файл сюди — я збережу."),
        "ck_expired": ("🔴 Cookies протерміновані — приватні пости знову недоступні.\n"
                       "Надішли свіжий cookies.txt сюди, щоб відновити."),
        "ck_delete_fail": "⚠️ Не вдалося видалити файл cookies.",
        "cobalt_on": "увімк.",
        "cobalt_off": "вимк.",
    },
    "en": {
        "too_big_media": "⚠️ Media too large (limit {mb} MB).",
        "too_big_video": "⚠️ Video too large even at the lowest quality (limit {mb} MB).",
        "cant_whole_file": "⚠️ couldn't fetch a complete file, please try again a bit later.",
        "too_big_audio": "⚠️ Audio too large (limit {mb} MB).",
        "cant_audio": "⚠️ couldn't extract audio.",
        "cant_link": "⚠️ couldn't process the link.",
        "no_space": "⚠️ The server has run out of space. Try again a bit later.",
        "err_private": "🔒 The post is private — it needs account access.",
        "err_age": "🔞 Age-restricted. Cookies from a logged-in account are needed.",
        "err_geo": "🌍 Not available from the country the server sits in.",
        "err_gone": "🗑 The post was deleted or no longer exists.",
        "err_extractor": "🛠 The site changed its markup — waiting for a yt-dlp update.",
        "cant_carousel": "⚠️ couldn't download the carousel.",
        "cant_video": "⚠️ couldn't download the video from this link.",
        "proc_error": "⚠️ Something went wrong while processing.",
        "no_access": "⛔ You don't have access to this bot.",
        "start_private": ("Send a video link (TikTok, YouTube, Instagram, Facebook) - "
                          "I'll return the video.\nAdd the word 'mp3' next to a YouTube "
                          "link - I'll extract the audio."),
        "start_group": "Drop short videos (TikTok / Reels / Shorts) - I'll download them.",
        "checking_versions": "Checking versions…",
        "your_id": "Your id: {id}",
        "bot_started": "✅ s.d.p is running\n\nVersions:\n{r}",
        "lite_private": ("I work in groups. Add me to a chat and I will grab\nshort videos from the links posted there."),
        "err_timeout": "timed out.",
        "err_internal": "internal error: {err}",
        "err_download": "could not download from this link.",
        "err_nofile": "no file found after the download.",
        "rep_version": "\U0001f534 new version: {sha} — {msg}",
        "rep_lib": "\U0001f534 {name}: {cur} \u2192 {latest} (update available)",
        "rep_ck_forever": "\U0001f7e2 cookies: no expiry date",
        "rep_ck_expired": "\U0001f534 cookies: EXPIRED",
        "rep_ck_soon": "\U0001f7e1 cookies: ~{days} days left",
        "rep_ck_ok": "\U0001f7e2 cookies: ~{days} days left",
        "no_platforms": "NONE (everything is disabled!)",
        "svc_spotify": "Spotify (searched on YouTube)",
        "svc_deezer": "Deezer (searched on YouTube)",
        "svc_allin": "All-in — every other site (1500+)",
        "new_version": ("🆕 A new s.d.p version is out — time to update.\n"
                        "Run Pull and redeploy in Portainer.\n{what}"),
        "new_lib": "🆕 A new {name} version is out ({cur} → {latest}) — please run Pull and redeploy.",
        "done_long": "✅ Done (took {s}s).",
        "ck_saved": ("🍪 Cookies saved: {n} entries ({sites}).\n"
                     "Keys: {auth}. Valid for ~{days} more days.\n"
                     "yt-dlp is now the primary engine for Instagram/Facebook."),
        "ck_status": ("🍪 Cookies active: {n} entries ({sites}).\n"
                      "Keys: {auth}. Valid for ~{days} more days."),
        "ck_absent": "No cookies yet. Send a cookies.txt file here and I will store it.",
        "ck_no_auth": "⚠️ No session keys in the file (sessionid/c_user). Export cookies from a logged-in browser.",
        "ck_bad": "⚠️ Could not read the file. Netscape format (cookies.txt) is required.",
        "ck_fetch_fail": ("⚠️ Could not fetch the file from Telegram: {err}\n"
                          "Workaround: open cookies.txt, copy the whole text "
                          "and send it here as a normal message."),
        "ck_too_big": "⚠️ That file is too large for cookies.",
        "ck_deleted": "🍪 Cookies removed. Instagram will fall back to public posts only.",
        "bk_ok": "💾 Database backup ({kb} KB): stats, settings, access lists.",
        "bk_none": "No database yet — nothing to back up.",
        "rs_ok": ("♻️ Database restored from the backup: {n} records.\n"
                  "The old one is kept next to it as stats.db.replaced."),
        "rs_bad": "⚠️ Could not restore the database: {why}. The current one is untouched.",
        "rs_too_big": "⚠️ That file is too large to restore from.",
        "bk_fail": "⚠️ Backup failed.",
        "ck_expiring": ("⏳ Cookies expire in about {days} days.\n"
                        "Refresh the session: log in, export cookies.txt "
                        "and send the file here — I will store it."),
        "ck_expired": ("🔴 Cookies have expired — private posts are unavailable again.\n"
                       "Send a fresh cookies.txt here to restore access."),
        "ck_delete_fail": "⚠️ Could not delete the cookies file.",
        "cobalt_on": "on",
        "cobalt_off": "off",
    },
}


def svc_label(svc):
    """Service name in the current language (falls back to the plain label)."""
    key = svc.get("i18n")
    return t(key) if key else svc["label"]


def cur_lang():
    lang = (setting("lang", BOT_LANG) or BOT_LANG).lower()
    return lang if lang in T else "uk"


def t(key, **kw):
    d = T.get(cur_lang(), T["uk"])
    s = d.get(key) or T["uk"].get(key, key)
    return s.format(**kw) if kw else s


# ----------------------------------------------------------------------------
# Live download progress (in-memory job registry, streamed from yt-dlp)
# ----------------------------------------------------------------------------

_JOB = contextvars.ContextVar("dl_job", default=None)
# True when an engine returned a file we refused for quality reasons
# (silent / image instead of video) — only then a lenient retry makes sense.
_SOFT_REJECT = contextvars.ContextVar("soft_reject", default=False)
_TRIM = contextvars.ContextVar("dl_trim", default=None)
_QUALITY = contextvars.ContextVar("dl_quality", default=None)   # max height
_TITLE = contextvars.ContextVar("dl_title", default=None)       # caption for the file
_ABR = contextvars.ContextVar("dl_abr", default=None)           # mp3 kbps
_TRIM_RE = re.compile(r"(\d{1,2}:\d{2}(?::\d{2})?|\d{1,4})\s*-\s*(\d{1,2}:\d{2}(?::\d{2})?|\d{1,4})")


def _parse_ts(s):
    s = s.strip()
    if ":" in s:
        sec = 0
        for p in s.split(":"):
            sec = sec * 60 + int(p or 0)
        return sec
    return int(s or 0)


def trim_active():
    return _TRIM.get() is not None


def _extract_trim(text, urls=()):
    scrub = text or ""
    for u in urls:
        scrub = scrub.replace(u, " ")
    m = _TRIM_RE.search(scrub)
    if not m:
        return None
    try:
        a, b = _parse_ts(m.group(1)), _parse_ts(m.group(2))
    except Exception:  # noqa: BLE001
        return None
    if 0 <= a < b and (b - a) <= 3600:
        return (a, b)
    return None
_progress = {}  # job_id -> dict
PROGRESS_TEMPLATE = ("download:JBP %(progress.downloaded_bytes)s "
                     "%(progress.total_bytes)s %(progress.total_bytes_estimate)s "
                     "%(progress.eta)s %(progress.speed)s")


def _new_job(message, source, mode):
    chat = getattr(message, "chat", None)
    fu = getattr(message, "from_user", None)
    uid = getattr(fu, "id", None) if fu else getattr(chat, "id", None)
    job = {
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "updated": time.time(),
        "user_id": uid,
        "chat_id": getattr(chat, "id", None),
        "chat_title": (getattr(chat, "title", None) or getattr(chat, "first_name", None)
                       or getattr(chat, "username", None)),
        "source": source,
        "mode": mode,
        "status": "queued",
        "pct": None,
        "eta": None,
        "speed": None,
        "size": None,
        "thumb": None,
    }
    _progress[job["id"]] = job
    return job


def _num(x):
    try:
        v = float(x)
        return v if v == v else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _eta_to_sec(s):
    total = 0
    for val, unit in re.findall(r"(\d+)\s*([hms])", s):
        total += int(val) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total or _num(s)


def _size_to_bytes(num, unit):
    mult = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3,
            "K": 1000, "M": 1000**2, "G": 1000**3}.get(unit, 1)
    v = _num(num)
    return v * mult if v is not None else None


def _update_job_progress(job, line):
    line = line.strip()
    if not line:
        return
    if line.startswith("JBP "):
        p = line.split()
        dl = _num(p[1]) if len(p) > 1 else None
        total = (_num(p[2]) if len(p) > 2 else None) or (_num(p[3]) if len(p) > 3 else None)
        eta = _num(p[4]) if len(p) > 4 else None
        spd = _num(p[5]) if len(p) > 5 else None
        if total and dl is not None and total > 0:
            job["pct"] = max(0.0, min(100.0, round(dl / total * 100, 1)))
        if eta is not None:
            job["eta"] = int(eta)
        if spd is not None:
            job["speed"] = spd
        job["status"] = "downloading"
        job["updated"] = time.time()
        return
    m = re.search(r"\((\d+(?:\.\d+)?)%\)", line)  # aria2c external downloader
    if m:
        job["pct"] = float(m.group(1))
        e = re.search(r"ETA:\s*([0-9hms]+)", line)
        if e:
            job["eta"] = _eta_to_sec(e.group(1))
        d = re.search(r"DL:\s*([0-9.]+)\s*([KMGT]?i?B)", line)
        if d:
            job["speed"] = _size_to_bytes(d.group(1), d.group(2))
        job["status"] = "downloading"
        job["updated"] = time.time()


def _finish_job(job, status):
    if status in ("sent", "sent_silent"):
        job["status"] = "done"
        job["pct"] = 100.0
    elif status == "toobig":
        job["status"] = "toobig"
    else:
        job["status"] = "error"
    job["eta"] = 0
    job["updated"] = time.time()

    async def _drop():
        await asyncio.sleep(25)
        _progress.pop(job["id"], None)

    spawn(_drop())


# ----------------------------------------------------------------------------
# Persistent file-id cache (url -> {kind, file_id})
# ----------------------------------------------------------------------------

_cache = {}


def titles_mode():
    """Where to show the post/video title: off | private | all."""
    v = str(setting("titles_mode", TITLES_MODE) or TITLES_MODE).lower()
    return v if v in ("off", "private", "all") else "private"


def audio_too_enabled():
    """Also send the soundtrack as a separate MP3 next to short videos."""
    v = setting("audio_too", None)
    if v is None:
        return AUDIO_TOO
    return str(v).lower() in ("1", "true", "yes", "on")


def cookies_ready():
    """True when a usable cookies file is mounted."""
    return bool(COOKIES_FILE and Path(COOKIES_FILE).exists()
                and Path(COOKIES_FILE).stat().st_size > 0)


_engine_state = {}       # (platform, engine) -> [failures in a row, open until]
_inflight = {}           # cache key -> asyncio.Lock, one download per link


def _inflight_lock(key):
    lock = _inflight.get(key)
    if lock is None:
        lock = _inflight[key] = asyncio.Lock()
    return lock


def engine_available(plat, eng):
    """False while an engine is tripped for this platform.

    When a site changes something, one engine starts failing on every link of
    that platform while the other still works. Without this the bot spends the
    full ladder on the broken one first, every single time, and every download
    becomes a minute slower for as long as the breakage lasts.
    Коли сайт щось міняє, один рушій починає падати на кожному посиланні цієї
    платформи, тоді як інший ще працює. Без цього бот щоразу спершу витрачає
    цілу драбину на зламаному, і кожне завантаження стає на хвилину довшим на
    весь час поломки.
    """
    if ENGINE_TRIP_AFTER <= 0:
        return True
    fails, until = _engine_state.get((plat, eng), (0, 0.0))
    if until and time.monotonic() < until:
        return False
    return True


def engine_result(plat, eng, ok):
    key = (plat, eng)
    if ok:
        _engine_state.pop(key, None)
        return
    if ENGINE_TRIP_AFTER <= 0:
        return
    fails, until = _engine_state.get(key, (0, 0.0))
    fails += 1
    if fails >= ENGINE_TRIP_AFTER:
        until = time.monotonic() + ENGINE_TRIP_SECONDS
        fails = 0
        logger.warning("Engine %s for %s failed %d times in a row — pausing it for %ds",
                       eng, plat, ENGINE_TRIP_AFTER, int(ENGINE_TRIP_SECONDS))
    _engine_state[key] = (fails, until)


def job_deadline(url):
    """Whole-job time limit. Long YouTube gets the generous one.

    Mirrors the per-subprocess timeouts: a full YouTube video may legitimately
    take half an hour, a short clip never should.
    Повторює таймаути окремих підпроцесів: повне відео з YouTube законно може
    качатись пів години, короткий кліп — ніколи.
    """
    return JOB_DEADLINE_LONG if _is_full_youtube(url) else JOB_DEADLINE_SHORT


def engine_order(sid, svc):
    """Engine order for a service. For Instagram/Facebook it adapts:
    with cookies yt-dlp is stronger (stable 1080p, proper metadata),
    without them Cobalt is the only thing that works at all.
    Override from the panel: engine_pref = auto | cobalt | ytdlp."""
    engines = list(svc.get("engines", ("ytdlp", "cobalt")))
    if sid not in ("instagram", "facebook"):
        return engines
    pref = str(setting("engine_pref", "auto") or "auto").lower()
    if pref == "auto":
        pref = "ytdlp" if cookies_ready() else "cobalt"
    if pref in ("ytdlp", "cobalt"):
        engines = [pref] + [e for e in engines if e != pref]
    return engines


def cache_enabled():
    if LITE:
        return False           # nothing is written to disk in LITE
    v = setting("cache", None)
    if v is None:
        return ENABLE_CACHE
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# Bumped whenever the shape of a key changes, so old entries are dropped
# instead of never matching again and sitting there until eviction.
# Піднімається щоразу, коли міняється формат ключа, — щоб старі записи
# викидались, а не висіли мертвим вантажем до витіснення.
CACHE_FORMAT = 2


def _api_fingerprint():
    """Which Telegram API issued the file_ids in this cache.

    A file_id only works on the server that handed it out: move from the cloud
    API to a local Bot API server (or back) and every stored id turns into
    "wrong file identifier" — a cache that looks fine and answers with errors.
    Який саме Telegram API видав ці file_id. Ідентифікатор дійсний лише на
    тому сервері, що його видав: переїзд із хмарного API на локальний Bot API
    (чи назад) перетворює кожен збережений id на "wrong file identifier" —
    кеш виглядає справним, а віддає помилки.
    """
    return TELEGRAM_API_URL or "cloud"


def cache_load():
    global _cache
    _cache = {}
    if not cache_enabled():
        return
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return
    if not isinstance(data, dict) or "items" not in data:
        logger.info("Cache from an older format dropped")
        return
    if data.get("api") != _api_fingerprint():
        logger.warning("Telegram API changed (%s -> %s): cache dropped, "
                       "its file_id values belong to the other server",
                       data.get("api"), _api_fingerprint())
        return
    if data.get("format") != CACHE_FORMAT:
        logger.info("Cache format %s -> %s: dropped", data.get("format"), CACHE_FORMAT)
        return
    _cache = data["items"]
    logger.info("Cache loaded: %d entries", len(_cache))


def cache_save():
    try:
        os.makedirs(os.path.dirname(CACHE_FILE) or ".", exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"format": CACHE_FORMAT, "api": _api_fingerprint(),
                       "items": _cache}, f)
        os.replace(tmp, CACHE_FILE)
    except Exception:  # noqa: BLE001
        logger.warning("Cache save failed")


_cache_dirty = False


def cache_save_soon():
    """Mark the cache as changed; the write happens off the event loop.

    json.dump of the whole cache ran inline on every stored file_id — blocking
    I/O in the middle of the loop, on the path of every single download. With
    no loop running (tests, shutdown) there is nothing to block, so the write
    happens immediately and behaviour stays predictable.
    json.dump усього кеша виконувався прямо на місці при кожному збереженому
    file_id — блокуючий запис посеред циклу подій, на шляху кожного окремого
    завантаження. Коли циклу немає (тести, зупинка), блокувати нічого, тож
    запис іде одразу й поведінка лишається передбачуваною.
    """
    global _cache_dirty
    _cache_dirty = True
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        cache_flush_sync()


def cache_flush_sync():
    global _cache_dirty
    if _cache_dirty:
        _cache_dirty = False
        cache_save()


async def cache_flush():
    global _cache_dirty
    if _cache_dirty:
        _cache_dirty = False
        await asyncio.to_thread(cache_save)


def _ckey(url, audio):
    """Quality is part of the identity of a cached file, not a detail.

    Without it one download at 480p answers every later request for the same
    link — including one asking for maximum quality — and the user gets 480p
    with nothing to indicate why.
    Якість — частина того, ЩО саме лежить у кеші, а не подробиця. Без неї одне
    завантаження в 480p відповідає на всі наступні запити того самого лінка,
    зокрема на запит максимальної якості, і користувач отримує 480p без жодної
    підказки, чому.
    """
    if audio:
        return "a:%s:%s" % (_ABR.get() or "auto", url)
    return "v:%s:%s" % (_QUALITY.get() or "auto", url)


def cache_get(url, audio):
    if not cache_enabled() or trim_active():
        return None
    return _cache.get(_ckey(url, audio))


def cache_set(url, audio, kind, file_id):
    if not cache_enabled() or trim_active():
        return
    _cache[_ckey(url, audio)] = {"kind": kind, "file_id": file_id}
    while len(_cache) > tunable("cache_max"):
        _cache.pop(next(iter(_cache)))
    cache_save_soon()


def cache_del(url, audio):
    _cache.pop(_ckey(url, audio), None)
    cache_save_soon()


def cache_clear():
    _cache.clear()
    cache_save_soon()


def cache_info():
    """What the panel shows: entries kept and the size of cache.json on disk."""
    try:
        size = Path(CACHE_FILE).stat().st_size
    except Exception:  # noqa: BLE001
        size = 0
    return {"entries": len(_cache), "bytes": int(size), "max": tunable("cache_max")}


# How long a chat or a user may sit idle before its bookkeeping is dropped.
# Скільки чат чи користувач можуть простоювати, поки їхній облік не приберуть.
STATE_TTL = 3600.0


def prune_state():
    """Drop per-chat and per-user bookkeeping nobody is using any more.

    Each of these is keyed by a chat or a user and nothing ever removed an
    entry: a bot living in a busy group collects one lock, one timestamp and
    one gate per participant and keeps them until it restarts. Each is tiny;
    together they are a process that only grows.
    (_notified is deliberately absent — its keys are the fixed set of package
    names, so it cannot grow.)
    Кожен із цих словників має ключем чат або користувача, і жоден запис ніколи
    не видалявся: бот у жвавій групі збирає по замку, мітці часу й воротарю на
    кожного учасника й тримає їх до перезапуску. Кожен запис крихітний, разом —
    процес, який лише росте.
    (_notified тут свідомо немає — його ключі це фіксований набір імен пакетів,
    тож він рости не може.)
    """
    now = time.monotonic()
    for chat_id, last in list(_chat_last_send.items()):
        if now - last < STATE_TTL:
            continue
        lock = _chat_send_lock.get(chat_id)
        if lock is not None and lock.locked():
            continue                      # a send is going out right now
        _chat_last_send.pop(chat_id, None)
        _chat_send_lock.pop(chat_id, None)
    for chat_id, lock in list(_chat_send_lock.items()):
        if chat_id not in _chat_last_send and not lock.locked():
            _chat_send_lock.pop(chat_id, None)     # lock without a timestamp
    for uid, gate in list(_user_sems.items()):
        if gate.idle():
            _user_sems.pop(uid, None)
    for key, lock in list(_inflight.items()):
        if not lock.locked():
            _inflight.pop(key, None)
    for key, hits in list(_api_hits.items()):
        fresh = [t for t in hits if now - t < API_RATE_WINDOW]
        if fresh:
            _api_hits[key] = fresh
        else:
            _api_hits.pop(key, None)


def free_space():
    """Bytes left in the work directory, or None if it cannot be measured."""
    try:
        return shutil.disk_usage(WORK_DIR).free
    except OSError:
        return None


def check_workdir_capacity():
    """Say at startup whether the work directory can hold what we promise.

    In the shipped stacks /tmp is a 1 GB tmpfs while MAX_FILE_MB goes up to
    2000 with the local Bot API. Nothing checks that those two agree, so the
    mismatch surfaces much later as "No space left on device" from inside
    yt-dlp, on a link that looks perfectly ordinary.
    Сказати на старті, чи вміщає робоча тека те, що ми обіцяємо. У наших стеках
    /tmp — це tmpfs на 1 ГБ, тоді як MAX_FILE_MB із локальним Bot API сягає
    2000. Ніщо не звіряє ці два числа, тож розбіжність спливає значно пізніше —
    як "No space left on device" зсередини yt-dlp, на цілком звичайному лінку.
    """
    try:
        total = shutil.disk_usage(WORK_DIR).total
    except OSError:
        logger.warning("Work directory %s is not reachable", WORK_DIR)
        return
    need = MAX_FILE_SIZE * 2          # video + audio, then the merged result
    mb = 1024 * 1024
    if total < need:
        logger.warning(
            "Work dir %s holds %d MB, but MAX_FILE_MB=%d needs about %d MB while "
            "merging. Large downloads will fail with 'No space left on device' — "
            "raise the tmpfs size or point WORK_DIR at a disk.",
            WORK_DIR, total // mb, MAX_FILE_SIZE // mb, need // mb)
    else:
        logger.info("Work dir: %s (%d MB)", WORK_DIR, total // mb)


async def housekeeping_loop():
    """Sweep the in-memory bookkeeping and flush the cache to disk."""
    while True:
        await asyncio.sleep(300)
        try:
            prune_state()
            await cache_flush()
        except Exception:  # noqa: BLE001
            logger.exception("housekeeping failed")


async def cache_cleaner_loop():
    """Periodically wipe the file-id cache; the period is editable in the panel."""
    while True:
        days = tunable("cache_clear_days")
        if days <= 0 or not cache_enabled():
            await asyncio.sleep(3600)      # off for now — look again in an hour
            continue
        await asyncio.sleep(days * 86400)
        if tunable("cache_clear_days") > 0 and cache_enabled():
            cache_clear()
            logger.info("Cache cleared (scheduled every %d days)", days)


# ----------------------------------------------------------------------------
# Stats / events (SQLite)
# ----------------------------------------------------------------------------


_db_local = threading.local()
# Bumped when the file underneath is swapped (a restore). Every thread notices
# on its next call and reopens instead of holding a handle to a deleted file.
# Піднімається, коли файл під нами підмінили (відновлення). Кожен потік помічає
# це на наступному виклику й перевідкриває зʼєднання замість того, щоб тримати
# дескриптор видаленого файлу.
_db_generation = 0


def db_conn():
    """A connection per thread, reused.

    Every query used to open, configure and close its own connection. All the
    DB work happens inside asyncio.to_thread, and that pool reuses a handful of
    threads, so one connection each is both correct — SQLite objects belong to
    the thread that made them — and much cheaper.
    Раніше кожен запит відкривав, налаштовував і закривав власне зʼєднання. Уся
    робота з базою йде через asyncio.to_thread, а цей пул перевикористовує
    кілька потоків, тож по зʼєднанню на потік і коректно — обʼєкти SQLite
    належать потоку, що їх створив, — і значно дешевше.
    """
    con = getattr(_db_local, "con", None)
    if con is not None and getattr(_db_local, "gen", -1) == _db_generation:
        return con
    if con is not None:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
    con = sqlite3.connect(STATS_DB, timeout=10)
    con.row_factory = sqlite3.Row
    # WAL lets readers work while a write is in flight; without it the panel
    # blocks every time an event is recorded.
    # WAL дозволяє читати під час запису; без нього панель блокується щоразу,
    # коли записується подія.
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    _db_local.con = con
    _db_local.gen = _db_generation
    return con


def db_reopen():
    """Tell every thread its connection is stale."""
    global _db_generation
    _db_generation += 1


# Schema history. Each step runs once, in order, and the file remembers how far
# it got in PRAGMA user_version.
# Історія схеми. Кожен крок виконується один раз і по порядку, а файл памʼятає,
# де зупинився, у PRAGMA user_version.
def _mig_1_base(con):
    con.execute(
        "CREATE TABLE IF NOT EXISTS events("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, user_id INTEGER, "
        "chat_id INTEGER, chat_type TEXT, chat_title TEXT, platform TEXT, "
        "source TEXT, url TEXT, status TEXT, via TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS access("
        "kind TEXT, ident TEXT, level TEXT, label TEXT, PRIMARY KEY(kind, ident))")
    # Columns added after the first release. Asking what is there beats trying
    # and swallowing the error: a failed ALTER used to be indistinguishable
    # from a locked database or a corrupt file.
    # Стовпці, додані після першого релізу. Спитати, що вже є, краще, ніж
    # спробувати й проковтнути помилку: невдалий ALTER не відрізнявся від
    # заблокованої бази чи зіпсованого файлу.
    have = {r[1] for r in con.execute("PRAGMA table_info(events)")}
    for name, decl in (("size", "INTEGER"), ("thumb_id", "TEXT")):
        if name not in have:
            con.execute("ALTER TABLE events ADD COLUMN %s %s" % (name, decl))


def _mig_2_whitelist(con):
    """The one-time move to the simple model: a whitelist of chats, no levels."""
    row = con.execute("SELECT value FROM settings WHERE key='whitelist'").fetchone()
    if row is not None:
        return
    old = con.execute("SELECT value FROM settings WHERE key='access_mode'").fetchone()
    con.execute("INSERT INTO settings(key,value) VALUES('whitelist',?)",
                ("0" if (not old or old[0] == "open") else "1",))
    con.execute("DELETE FROM access WHERE kind<>'chat'")
    con.execute("UPDATE access SET level='full'")
    logger.info("Access model migrated to the chat whitelist")


def _mig_3_indexes(con):
    """The panel groups by these columns; without indexes each was a full scan."""
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_chat ON events(chat_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_source ON events(source)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_status ON events(status)")


MIGRATIONS = (_mig_1_base, _mig_2_whitelist, _mig_3_indexes)

# What every event row is written with. Used to vet a restored file before it
# replaces the live one — see restore_backup().
# Чим записується кожна подія. Використовується, щоб перевірити файл
# відновлення, перш ніж він замінить робочий — див. restore_backup().
EVENT_COLUMNS = frozenset((
    "ts", "user_id", "chat_id", "chat_type", "chat_title",
    "platform", "source", "url", "status", "via",
))


def db_migrate(con):
    """Run the steps this file has not seen yet. Returns the resulting version."""
    version = con.execute("PRAGMA user_version").fetchone()[0]
    for number, step in enumerate(MIGRATIONS, start=1):
        if version >= number:
            continue
        step(con)
        con.execute("PRAGMA user_version=%d" % number)
        con.commit()
        logger.info("Database migrated to version %d", number)
        version = number
    return version


def db_init():
    if LITE:
        logger.info("LITE: no database, nothing is stored")
        return
    try:
        con = db_conn()
        db_migrate(con)
        logger.info("Stats DB ready: %s (schema v%d)", STATS_DB,
                    con.execute("PRAGMA user_version").fetchone()[0])
    except Exception:  # noqa: BLE001
        logger.exception("db_init failed")


def _db_record_sync(row):
    if LITE:
        return
    try:
        con = db_conn()
        con.execute(
            "INSERT INTO events(ts,user_id,chat_id,chat_type,chat_title,platform,"
            "source,url,status,via,size,thumb_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", row,
        )
        con.commit()
    except Exception:  # noqa: BLE001
        logger.exception("db_record failed")


async def record_event(message, url, platform, source, status, via):
    try:
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        ctype = getattr(chat, "type", None)
        ctype = getattr(ctype, "value", ctype)
        title = (
            getattr(chat, "title", None)
            or getattr(chat, "username", None)
            or getattr(chat, "first_name", None)
        )
        fu = getattr(message, "from_user", None)
        user_id = getattr(fu, "id", None) if fu else None
        job = _JOB.get() or {}
        row = (int(time.time()), user_id, chat_id, str(ctype) if ctype else None,
               title, platform, source, url, status, via,
               job.get("size"), job.get("thumb"))
        await asyncio.to_thread(_db_record_sync, row)
    except Exception:  # noqa: BLE001
        logger.exception("record_event failed")


def _source_label(url, platform, want_audio):
    if want_audio:
        return "audio"
    if platform == "youtube":
        return "youtube_shorts" if "/shorts/" in url.lower() else "youtube_video"
    if platform == "tiktok":
        return "tiktok_video"
    return platform or "other"


def _db_query(sql, params=()):
    try:
        rows = [dict(r) for r in db_conn().execute(sql, params).fetchall()]
        return rows
    except Exception:  # noqa: BLE001
        logger.exception("db_query failed")
        return []


def db_stats_sync():
    total = _db_query("SELECT COUNT(*) c FROM events")
    ok = _db_query("SELECT COUNT(*) c FROM events WHERE status IN ('sent','sent_silent')")
    fail = _db_query("SELECT COUNT(*) c FROM events WHERE status IN ('fail','error','toobig')")
    by_source = _db_query(
        "SELECT source, COUNT(*) c FROM events GROUP BY source ORDER BY c DESC"
    )
    by_chat = _db_query(
        "SELECT COALESCE(chat_title, CAST(chat_id AS TEXT)) name, chat_type, COUNT(*) c "
        "FROM events GROUP BY chat_id ORDER BY c DESC LIMIT 50"
    )
    return {
        "total": total[0]["c"] if total else 0,
        "ok": ok[0]["c"] if ok else 0,
        "fail": fail[0]["c"] if fail else 0,
        "by_source": by_source,
        "by_chat": by_chat,
    }


def db_events_sync(limit):
    return _db_query(
        "SELECT ts, source, status, url, chat_title, chat_type, via, size, thumb_id "
        "FROM events ORDER BY id DESC LIMIT ?", (int(limit),)
    )


# ----------------------------------------------------------------------------
# Access control & live settings
# ----------------------------------------------------------------------------

_settings = {}
_access = {}  # ("chat", chat_id) -> "full"  — the whitelist, no tiers


def settings_load_sync():
    global _settings, _access
    if LITE:
        rebuild_url_pattern()
        return
    try:
        con = db_conn()
        _settings = {r[0]: r[1] for r in con.execute("SELECT key,value FROM settings")}
        _access = {(r[0], r[1]): r[2] for r in con.execute("SELECT kind,ident,level FROM access")}
        rebuild_url_pattern()
    except Exception:  # noqa: BLE001
        logger.exception("settings_load failed")


def setting(key, default=None):
    return _settings.get(key, default)


# Knobs editable from the Mini App: key -> (env default, type, min, max)
TUNABLES = {
    "flood_retries":     (lambda: FLOOD_MAX_RETRIES, int, 0, 20),
    "chat_interval":     (lambda: CHAT_SEND_INTERVAL, float, 0.0, 10.0),
    "max_links":         (lambda: MAX_LINKS_PER_MESSAGE, int, 1, 50),
    "max_per_user":      (lambda: MAX_PER_USER, int, 1, 20),
    "job_retries":       (lambda: JOB_RETRIES, int, 0, 5),
    "job_retry_delay":   (lambda: JOB_RETRY_DELAY, float, 0.0, 60.0),
    "cobalt_retries":    (lambda: COBALT_RETRIES, int, 1, 5),
    "cobalt_retry_delay": (lambda: COBALT_RETRY_DELAY, float, 0.0, 30.0),
    "cookies_warn_days": (lambda: COOKIES_WARN_DAYS, int, 0, 60),
    # quality / scope
    "max_height":        (lambda: MAX_HEIGHT, int, 240, 4320),
    "long_max_height":   (lambda: LONG_MAX_HEIGHT, int, 360, 4320),
    "long_min_height":   (lambda: LONG_MIN_HEIGHT, int, 144, 4320),
    "playlist_max":      (lambda: PLAYLIST_MAX, int, 1, 100),
    "long_job_notify":   (lambda: LONG_JOB_NOTIFY, int, 0, 600),
    # load
    "max_concurrent":    (lambda: MAX_CONCURRENT_DOWNLOADS, int, 1, 32),
    "frag_concurrency":  (lambda: CONCURRENT_FRAGMENTS, int, 1, 16),
    # cache / upkeep
    "cache_max":         (lambda: CACHE_MAX, int, 10, 100000),
    "cache_clear_days":  (lambda: CACHE_CLEAR_INTERVAL_DAYS, int, 0, 365),
    "check_minutes":     (lambda: CHECK_INTERVAL_MINUTES, int, 0, 10080),
}


# Live on/off switches: DB value wins, otherwise the env default.
FLAGS = {
    "verify_media":  lambda: VERIFY_MEDIA,
    "verify_deep":   lambda: VERIFY_DEEP,
    "thumbnails":    lambda: THUMBNAILS,
    "ytdlp_sleep":   lambda: YTDLP_SLEEP,
    "eng_cobalt":    lambda: ENABLE_COBALT,
    "eng_tikwm":     lambda: ENABLE_TIKWM,
}


def flag(key):
    raw = _settings.get(key)
    if raw is None:
        return bool(FLAGS[key]())
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def verify_media():
    return flag("verify_media")


def cobalt_on():
    return flag("eng_cobalt")


def tunable(key):
    """Live value: DB setting if present, otherwise the env/default one."""
    env_default, cast, lo, hi = TUNABLES[key]
    raw = _settings.get(key)
    if raw is None:
        return env_default()
    try:
        return max(lo, min(hi, cast(raw)))
    except (TypeError, ValueError):
        return env_default()


def set_setting_sync(key, value):
    if LITE:
        return
    try:
        con = db_conn()
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value))
        )
        con.commit()
    except Exception:  # noqa: BLE001
        logger.exception("set_setting failed")


def access_add_sync(kind, ident, level, label):
    try:
        con = db_conn()
        con.execute(
            "INSERT INTO access(kind,ident,level,label) VALUES(?,?,?,?) "
            "ON CONFLICT(kind,ident) DO UPDATE SET level=excluded.level, label=excluded.label",
            (kind, ident, level, label),
        )
        con.commit()
    except Exception:  # noqa: BLE001
        logger.exception("access_add failed")


def access_remove_sync(kind, ident):
    try:
        con = db_conn()
        con.execute("DELETE FROM access WHERE kind=? AND ident=?", (kind, ident))
        con.commit()
    except Exception:  # noqa: BLE001
        logger.exception("access_remove failed")


def access_list_sync():
    return _db_query(
        "SELECT kind,ident,level,label FROM access WHERE kind='chat' ORDER BY label, ident")


def norm_ident(kind, raw):
    """A chat id: -1001234567890 (groups) or a plain number."""
    raw = (raw or "").strip().replace(" ", "")
    return raw if raw.lstrip("-").isdigit() else ""


def whitelist_on():
    """Whitelist off -> the bot works everywhere; on -> only listed chats."""
    return str(setting("whitelist", "0") or "0").lower() in ("1", "true", "yes", "on")


def chat_allowed(chat_id):
    return ("chat", str(chat_id)) in _access


def resolve_access(user_id, username, chat_id, is_group=False):
    """Return 'admin', 'extended' (full access), 'lite' or 'none'."""
    if LITE:
        # Groups only, and only the ones allowed (empty list = any group).
        if not is_group:
            return "none"
        if ALLOWED_CHATS and str(chat_id) not in ALLOWED_CHATS:
            return "none"
        return "lite"
    if ADMIN_ID and user_id == ADMIN_ID:
        return "admin"
    if not whitelist_on():
        return "extended"
    if is_group:
        return "extended" if chat_allowed(chat_id) else "none"
    # Whitelist on: private chats are the admin's only.
    return "none"


# ----------------------------------------------------------------------------
# Link detection
# ----------------------------------------------------------------------------


def _build_url_pattern():
    parts = []
    for sid, svc in SERVICES.items():
        if service_enabled(sid):
            parts.extend(svc["pats"])
    if not parts:
        return re.compile(r"(?!x)x")
    body = "|".join(parts)
    return re.compile(r"(https?://(?:" + body + r"))", re.IGNORECASE)


URL_PATTERN = _build_url_pattern()


def rebuild_url_pattern():
    global URL_PATTERN
    URL_PATTERN = _build_url_pattern()
_AUDIO_RE = re.compile(r"\bmp3\b|\bмп3\b|\baudio\b|\bаудіо\b|\bмузик\w*", re.IGNORECASE)


def extract_urls(text):
    if not text:
        return []
    seen = set()
    result = []
    for url in URL_PATTERN.findall(text):
        url = url.rstrip(").,!?»\"'")
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def _wants_audio(text):
    return bool(_AUDIO_RE.search(text or ""))


def _platform(url):
    for sid, svc in SERVICES.items():
        if svc["_re"].search(url):
            return sid
    return None


def _is_long_youtube(url):
    """Full YouTube video (watch) — heavy, allowed only in private chats."""
    return "youtube.com/watch" in url.lower()


def _is_full_youtube(url):
    """A full (non-Shorts) YouTube video — watch page or youtu.be short link."""
    u = url.lower()
    return ("youtube.com/watch" in u or "youtu.be/" in u) and "/shorts/" not in u


def _expects_video(url):
    """URL that must yield a video (reel/short/watch) — an image is a failure."""
    u = url.lower()
    return any(k in u for k in ("/reel/", "/reels/", "/video/", "/shorts/",
                                "/watch", "/tv/", "/clip/"))


def _is_youtube_music(url):
    """YouTube Music link — always delivered as audio, allowed at basic level."""
    return "music.youtube.com" in url.lower()


def _needs_extended(url):
    """Content requiring extended access: full YouTube + extended-tier services.
    YouTube Music is exempt — it is audio, allowed for everyone (incl. groups)."""
    if _is_youtube_music(url):
        return False
    if _is_long_youtube(url):
        return True
    svc = SERVICES.get(_platform(url) or "", {})
    if svc.get("audio"):
        return False
    return svc.get("tier") == "extended"


def _is_playlist(url):
    u = url.lower()
    return "/playlist?" in u or ("list=" in u and "watch?v=" not in u and "/shorts/" not in u)


async def ytdlp_playlist_entries(url):
    args = ["yt-dlp", "--flat-playlist", "--no-warnings",
            "--playlist-end", str(tunable("playlist_max")), "--print", "%(url)s"]
    _with_auth(args, url)
    args.append(url)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
        lines = out.decode("utf-8", "ignore").splitlines()
        return [ln.strip() for ln in lines
                if ln.strip().startswith("http")][:tunable("playlist_max")]
    except Exception:  # noqa: BLE001
        logger.warning("playlist expand failed: %s", url)
        return []


async def expand_targets(urls, allow_playlist):
    out = []
    for u in urls:
        if _is_playlist(u):
            if not allow_playlist:
                continue
            out.extend(await ytdlp_playlist_entries(u) or [])
            continue
        out.append(u)
    seen, res = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            res.append(u)
    return res[:25]


async def resolve_redirect(url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": USER_AGENT},
            ) as resp:
                return str(resp.url)
    except Exception:  # noqa: BLE001
        logger.warning("Could not resolve redirect for %s", url)
        return url


# ----------------------------------------------------------------------------
# yt-dlp (async subprocess)
# ----------------------------------------------------------------------------

_COMMON_YTDLP = [
    "--no-playlist",
    "--no-warnings",
    "--restrict-filenames",
    "--no-progress",
    "--retries",
    "3",
    "--fragment-retries",
    "3",
    "--user-agent",
    USER_AGENT,
    "--geo-bypass",
]

# Підключаємо PO-token провайдера (обхід YouTube-захисту) до всіх викликів yt-dlp.
if POT_PROVIDER_URL:
    _COMMON_YTDLP += [
        "--extractor-args",
        "youtubepot-bgutilhttp:base_url=" + POT_PROVIDER_URL,
    ]

def common_ytdlp():
    """Base yt-dlp args + the two knobs that can change without a restart."""
    args = list(_COMMON_YTDLP)
    frags = tunable("frag_concurrency")
    if frags > 1:
        args += ["--concurrent-fragments", str(frags)]
    if flag("ytdlp_sleep"):
        args += ["--sleep-requests", "1",
                 "--min-sleep-interval", "1",
                 "--max-sleep-interval", "3"]
    return args


def _merge_fmt(height):
    # best video (<=height) + best audio, merged. Final "/b" grabs any best
    # format — required for TikTok/IG whose formats carry no height metadata.
    return "bv*[height<={h}]+ba/b[height<={h}]/b".format(h=height)


def _combined_fmt(height):
    # a single progressive file (almost always carries audio) — used as a retry
    # when the merged result came out silent or corrupt.
    return "b[height<={h}]/b".format(h=height)


def _with_auth(args, url=None):
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        args += ["--cookies", COOKIES_FILE]
    if PROXY:
        args += ["--proxy", PROXY]
    # TikTok serves a logged-in visitor a different page layout than yt-dlp's
    # HTML parser expects, and the extraction dies on "Unable to extract
    # universal data for rehydration". Pointing it at the mobile API skips the
    # page entirely. Set TIKTOK_API_HOSTNAME to an empty value to turn this off.
    # Залогіненому відвідувачу TikTok віддає іншу верстку, ніж очікує HTML-парсер
    # yt-dlp, і видобування падає з "Unable to extract universal data for
    # rehydration". Похід у мобільний API обходить сторінку взагалі. Щоб
    # вимкнути — лиши TIKTOK_API_HOSTNAME порожнім.
    if url and TIKTOK_API_HOSTNAME and "tiktok" in url.lower():
        args += ["--extractor-args", "tiktok:api_hostname=" + TIKTOK_API_HOSTNAME]
    return args


def _build_ytdlp_args(url, out_template, fmt):
    args = [
        "yt-dlp",
        "-f",
        fmt,
        "--merge-output-format",
        "mp4",
        "-o",
        out_template,
    ] + common_ytdlp()
    _with_auth(args, url)
    trim = _TRIM.get()
    if trim:
        # Trim to [start-end]; native downloader only (aria2 has no sections).
        args += ["--download-sections", "*%d-%d" % (trim[0], trim[1]),
                 "--force-keyframes-at-cuts"]
    args.append(url)
    return args


def _build_ytdlp_audio_args(url, out_template):
    args = [
        "yt-dlp",
        "-f",
        "bestaudio/best",
        "-x",
        "--audio-format",
        "mp3",
        "--audio-quality",
        (str(_ABR.get()) + "K") if _ABR.get() else "0",
        "--embed-metadata",  # записати назву/виконавця в ID3-теги
        "-o",
        out_template,
    ] + common_ytdlp()
    _with_auth(args, url)
    trim = _TRIM.get()
    if trim:
        args += ["--download-sections", "*%d-%d" % (trim[0], trim[1]),
                 "--force-keyframes-at-cuts"]
    args.append(url)
    return args


# Why a download failed. One message for every cause told the user nothing and
# sent them to the logs — which, for a self-hosted bot, means asking the owner.
# Order matters: the first match wins, so the specific ones come first.
# Чому завантаження впало. Одне повідомлення на всі причини не говорило
# користувачу нічого й відправляло його в логи — а для self-hosted бота це
# означає «питай власника». Порядок важливий: перемагає перший збіг, тож
# конкретні йдуть раніше.
_FAIL_PATTERNS = (
    ("err_age", ("age-restricted", "confirm your age", "18 years old",
                 "not be comfortable for some audiences")),
    ("err_private", ("private", "login required", "log in for access",
                     "sign in to confirm", "requires authentication",
                     "this account is private")),
    # "available in your country" covers both the plain "not available in your
    # country" and yt-dlp's actual wording, "has not made this video available
    # in your country" — the negation sits far from the phrase.
    # "available in your country" покриває і просте "not available in your
    # country", і справжнє формулювання yt-dlp "has not made this video
    # available in your country" — заперечення там стоїть далеко від фрази.
    ("err_geo", ("not available from your location", "geo restrict", "geo-restrict",
                 "blocked it in your country", "available in your country")),
    ("err_gone", ("video unavailable", "has been removed", "no longer available",
                  "does not exist", "account has been terminated",
                  "http error 404", "post not found")),
    ("err_extractor", ("unable to extract", "unexpected response",
                       "please report this issue", "no video formats found",
                       "unsupported url")),
)

# The reason for the last failure inside this job, so the final message can
# name it instead of shrugging.
# Причина останнього збою в межах цієї джоби — щоб фінальне повідомлення могло
# її назвати, а не розвести руками.
_FAIL_REASON = contextvars.ContextVar("fail_reason", default=None)


def classify_failure(text):
    """Map an engine's complaint onto an i18n key."""
    low = (text or "").lower()
    for key, needles in _FAIL_PATTERNS:
        if any(n in low for n in needles):
            return key
    return "err_download"


def note_failure(text):
    key = classify_failure(text)
    if key != "err_download" or _FAIL_REASON.get() is None:
        _FAIL_REASON.set(key)
    return key


async def _run_ytdlp(args, work_dir, label, url):
    job = _JOB.get()
    if job is not None:
        # Enable machine-readable progress lines (disable the quiet flag).
        args = [a for a in args if a != "--no-progress"]
        tail_url = args[-1]
        args = args[:-1] + ["--newline", "--progress-template", PROGRESS_TEMPLATE, tail_url]
        job["status"] = "downloading"
        job["updated"] = time.time()

    timeout = 1800 if _is_full_youtube(url) else 600
    async with download_semaphore:
        logger.info("yt-dlp %s: %s", label, url)
        stderr_buf = []
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            async def _drain_stdout():
                buf = b""
                while True:
                    chunk = await proc.stdout.read(512)
                    if not chunk:
                        break
                    buf = (buf + chunk).replace(b"\r", b"\n")
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        if job is not None:
                            _update_job_progress(job, raw.decode("utf-8", "ignore"))

            async def _drain_stderr():
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        break
                    stderr_buf.append(chunk)

            await asyncio.wait_for(
                asyncio.gather(_drain_stdout(), _drain_stderr()), timeout=timeout
            )
            await asyncio.wait_for(proc.wait(), timeout=30)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            shutil.rmtree(work_dir, ignore_errors=True)
            return None, t("err_timeout")
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(work_dir, ignore_errors=True)
            logger.exception("yt-dlp launch error")
            return None, t("err_internal", err=exc)

    if proc.returncode != 0:
        shutil.rmtree(work_dir, ignore_errors=True)
        err = b"".join(stderr_buf).decode("utf-8", "ignore").strip().splitlines()
        logger.warning("yt-dlp fail (%s): %s", url, err[-1] if err else "?")
        return None, t(note_failure(" ".join(err[-4:])))

    files = [p for p in work_dir.iterdir() if p.is_file()]
    if not files:
        shutil.rmtree(work_dir, ignore_errors=True)
        return None, t("err_nofile")
    return max(files, key=lambda p: p.stat().st_size), None


async def ytdlp_download(url, height, fmt=None):
    work_dir = Path(WORK_DIR) / ("vbot_" + uuid.uuid4().hex)
    work_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(work_dir / "%(id)s.%(ext)s")
    return await _run_ytdlp(
        _build_ytdlp_args(url, out_template, fmt or _merge_fmt(height)),
        work_dir, "%dp" % height, url,
    )


# The video we just sent, kept aside so the separate MP3 can be cut out of it.
# Відео, яке щойно відправили, лишається збоку — щоб вирізати з нього MP3.
_AUDIO_SRC = contextvars.ContextVar("audio_src", default=None)


def keep_for_soundtrack(path):
    """Move the sent file aside instead of deleting it right away.

    TikTok throttles a second extraction of the same post moments later and
    replies with something yt-dlp cannot parse ("Unexpected response from
    webpage request"), so the video went through while the separate MP3 failed.
    ffmpeg on the file we already hold needs no network at all.
    TikTok притискає повторне видобування того самого поста за кілька секунд і
    віддає щось, чого yt-dlp не розбирає ("Unexpected response from webpage
    request"): відео йшло, а окремий MP3 падав. ffmpeg по вже наявному файлу
    не ходить у мережу взагалі.
    """
    if path is None or not audio_too_enabled():
        return
    try:
        keep = Path(WORK_DIR) / ("vbot_snd_" + uuid.uuid4().hex + path.suffix)
        shutil.move(str(path), str(keep))
        _AUDIO_SRC.set(keep)
    except Exception:  # noqa: BLE001
        logger.info("could not keep the file for the soundtrack")


async def ffmpeg_extract_mp3(src):
    """Cut an MP3 out of a local file. Returns the path or None."""
    out = src.with_name(src.stem + "-audio.mp3")
    args = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn",
            "-acodec", "libmp3lame",
            "-b:a", "%dk" % (_ABR.get() or 192)]
    title = _TITLE.get()
    if title:
        args += ["-metadata", "title=" + title[:120]]
    args.append(str(out))
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), timeout=120)
        if out.exists() and out.stat().st_size > 0:
            return out
        logger.info("ffmpeg gave no audio: %s", (err or b"").decode("utf-8", "ignore")[:200])
    except Exception:  # noqa: BLE001
        logger.info("ffmpeg audio extraction failed")
    return None


async def ytdlp_audio(url):
    work_dir = Path(WORK_DIR) / ("vbot_" + uuid.uuid4().hex)
    work_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(work_dir / "%(id)s.%(ext)s")
    return await _run_ytdlp(_build_ytdlp_audio_args(url, out_template), work_dir, "audio", url)


def cleanup(path):
    if path is None:
        return
    try:
        parent = path.parent
        if parent.exists() and parent.name.startswith("vbot_"):
            shutil.rmtree(parent, ignore_errors=True)
        elif path.exists():
            path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        logger.exception("Cleanup failed for %s", path)


# ----------------------------------------------------------------------------
# ffprobe / ffmpeg (metadata, integrity check, thumbnail)
# ----------------------------------------------------------------------------


async def _ffprobe_json(path, entries):
    cmd = ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "json", str(path)]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    return json.loads(out or b"{}")


async def _count_video_frames(path):
    """Count decoded video frames (ffprobe -count_frames). -1 = unknown/error."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
           "-show_entries", "stream=nb_read_frames",
           "-of", "default=nokey=1:noprint_wrappers=1", str(path)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        txt = (out or b"").decode("utf-8", "ignore").strip()
        return int(txt) if txt.isdigit() else -1
    except Exception:  # noqa: BLE001
        return -1


async def validate_media(path, deep=None):
    """Return "ok" (video+audio, decodes clean), "no_audio", or "bad" (corrupt).
    deep=True forces a full-file decode + strict duration/frame checks — used for
    Cobalt/third-party items, which are small and prone to truncation."""
    if deep is None:
        deep = flag("verify_deep")
    # A full decode costs roughly real playback time per gigabyte on a desktop
    # CPU, and it exists to catch truncated third-party files, which are small.
    # On a 2 GB download it would burn minutes of CPU to confirm something the
    # cheap checks already established.
    # Повний декод коштує приблизно стільки ж, скільки триває саме відтворення,
    # на гігабайт на десктопному процесорі, а потрібен він для обрізаних файлів
    # від сторонніх сервісів, які маленькі. На завантаженні у 2 ГБ він спалив би
    # хвилини процесорного часу, підтверджуючи те, що дешеві перевірки вже й так
    # показали.
    if deep and VERIFY_DEEP_MAX_SIZE:
        try:
            if path.stat().st_size > VERIFY_DEEP_MAX_SIZE:
                logger.info("Skipping the deep decode of a %d MB file",
                            path.stat().st_size // 1024 // 1024)
                deep = False
        except OSError:
            pass
    try:
        data = await _ffprobe_json(path, "stream=codec_type,duration:format=duration")
        streams = data.get("streams", [])
        types = [s.get("codec_type") for s in streams]
    except Exception:  # noqa: BLE001
        return "bad"
    if "video" not in types:
        return "bad"

    def _f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    dur = _f((data.get("format", {}) or {}).get("duration"))
    if dur is None:
        vs = next((s for s in streams if s.get("codec_type") == "video"), {})
        dur = _f(vs.get("duration"))
    if dur is not None and dur < 0.5:
        logger.warning("Rejecting near-zero duration (%.2fs): %s", dur, path.name)
        return "bad"

    if verify_media():
        # deep -> decode the whole file; otherwise a fast tail decode (~3s).
        if deep:
            cmd = ["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"]
        else:
            cmd = ["ffmpeg", "-v", "error", "-xerror", "-sseof", "-3",
                   "-i", str(path), "-f", "null", "-"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode != 0 or (err and err.strip()):
                logger.warning("Decode errors in %s: %s", path.name, (err or b"")[:150])
                return "bad"
        except Exception:  # noqa: BLE001
            return "bad"

    # Unknown duration on a deep check -> make sure the file actually has frames.
    if deep and dur is None:
        nb = await _count_video_frames(path)
        if 0 <= nb < 3:
            logger.warning("Rejecting near-empty video (%d frames): %s", nb, path.name)
            return "bad"

    return "ok" if "audio" in types else "no_audio"


async def probe_video(path):
    """Return (width, height, duration_seconds) or (None, None, None)."""
    try:
        data = await _ffprobe_json(path, "stream=width,height:format=duration")
        stream = next(
            (s for s in data.get("streams", []) if s.get("width")), {}
        )
        dur = data.get("format", {}).get("duration")
        return stream.get("width"), stream.get("height"), int(float(dur)) if dur else None
    except Exception:  # noqa: BLE001
        return None, None, None


async def probe_tags(path):
    """Return (title, artist) from the file's metadata tags."""
    try:
        data = await _ffprobe_json(path, "format_tags=title,artist,album")
        tags = (data.get("format", {}) or {}).get("tags", {}) or {}
        return tags.get("title"), tags.get("artist")
    except Exception:  # noqa: BLE001
        return None, None


async def make_thumbnail(path, tmp_dir):
    """Extract a JPEG thumbnail (<=320px) for a nicer Telegram preview."""
    thumb = tmp_dir / "thumb.jpg"
    cmd = [
        "ffmpeg", "-y", "-ss", "00:00:01.000", "-i", str(path),
        "-vframes", "1", "-vf", "scale=320:-2", str(thumb),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
        if thumb.exists() and 0 < thumb.stat().st_size <= 200 * 1024:
            return thumb
    except Exception:  # noqa: BLE001
        pass
    return None


async def send_video_with_meta(message, path):
    """Send a video with width/height/duration + thumbnail. Returns sent Message."""
    if flag("thumbnails"):
        (width, height, duration), thumb = await asyncio.gather(
            probe_video(path), make_thumbnail(path, path.parent)
        )
    else:
        width, height, duration = await probe_video(path)
        thumb = None
    kwargs = {"disable_notification": True, "supports_streaming": True}
    cap = _TITLE.get()
    if cap:
        kwargs["caption"] = cap[:1000]
    if width:
        kwargs["width"] = width
    if height:
        kwargs["height"] = height
    if duration:
        kwargs["duration"] = duration
    if thumb:
        kwargs["thumbnail"] = FSInputFile(thumb)
    sent = await message.reply_video(FSInputFile(path), **kwargs)
    job = _JOB.get()
    if job is not None:
        try:
            job["size"] = path.stat().st_size
        except Exception:  # noqa: BLE001
            pass
        v = getattr(sent, "video", None)
        if v is not None:
            th = getattr(v, "thumbnail", None)
            job["thumb"] = getattr(th, "file_id", None) or getattr(v, "file_id", None)
    return sent


# ----------------------------------------------------------------------------
# Cobalt / tikwm (TikTok photo carousels)
# ----------------------------------------------------------------------------


async def cobalt_request(url, api_url=None):
    payload = {"url": url, "videoQuality": str(tunable("max_height")),
               "filenameStyle": "basic",
               # Pinned on purpose. "local-processing" means Cobalt hands back
               # separate streams for us to merge ourselves, which we have no
               # code for. The default is already "disabled", but stating it
               # keeps a future change of that default from quietly turning
               # every download into a response shape we cannot use.
               # Закріплено свідомо. "local-processing" означає, що Cobalt
               # віддає окремі доріжки, які треба склеїти самому, а такого коду
               # в нас немає. Типове значення й так "disabled", але явний запис
               # убезпечує від того, що зміна цього типового значення тихо
               # перетворить кожне завантаження на формат, який ми не вміємо.
               "localProcessing": "disabled"}
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                (api_url or COBALT_API_URL).rstrip("/") + "/",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                return await resp.json()
    except Exception:  # noqa: BLE001
        logger.exception("Cobalt request failed")
        return None


async def download_file(url, path):
    # These links come back from Cobalt and tikwm, i.e. from outside, so they
    # get the same treatment as a link typed by a stranger.
    # Ці посилання повертають Cobalt і tikwm, тобто зовнішні сервіси, — тож
    # ставлення до них таке саме, як до лінка від незнайомця.
    if not await url_is_safe(url):
        return False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=180)) as resp:
                if resp.status != 200:
                    return False
                expected = int(resp.headers.get("Content-Length") or 0)
                with open(path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        f.write(chunk)
        size = path.stat().st_size if path.exists() else 0
        if size <= 0:
            return False
        if expected and size < expected:
            logger.warning("Truncated download (%d/%d bytes): %s", size, expected, url)
            return False
        return True
    except Exception:  # noqa: BLE001
        logger.exception("File download failed: %s", url)
        return False


def _chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _is_image_file(path):
    """Detect an image by magic bytes (JPEG/PNG/GIF/WEBP)."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except Exception:  # noqa: BLE001
        return False
    return (head[:3] == b"\xff\xd8\xff"
            or head[:8] == b"\x89PNG\r\n\x1a\n"
            or head[:4] == b"GIF8"
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"))


async def send_media(bot, message, items, source, strict=False):
    """Download `items` [(type, url)] and send them as an album. No audio track."""
    if not items:
        return False
    tmp = Path(WORK_DIR) / ("vbot_" + uuid.uuid4().hex)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        media = []
        _vmeta = {}          # id(InputMediaVideo) -> kwargs for a single send
        skipped_big = 0
        for idx, (mtype, link) in enumerate(items):
            if not link:
                continue
            path = tmp / ("item_%d" % idx)

            if mtype == "photo":
                if not await download_file(link, path):
                    continue
                if path.stat().st_size > MAX_FILE_SIZE:
                    skipped_big += 1
                    continue
                media.append(InputMediaPhoto(media=FSInputFile(path)))
                continue

            # Video: download and verify integrity; re-download if corrupt.
            ok = False
            for attempt in range(3):
                if not await download_file(link, path):
                    continue
                if path.stat().st_size > MAX_FILE_SIZE:
                    skipped_big += 1
                    break
                if _is_image_file(path):
                    if strict:
                        # A video was requested but an image came back — let the
                        # caller fall back to another engine.
                        logger.warning("%s returned an image for a video URL: %s", source, link)
                        _SOFT_REJECT.set(True)
                        return False
                    # Instagram photo posts arrive as a "tunnel" — send as photo.
                    media.append(InputMediaPhoto(media=FSInputFile(path)))
                    ok = False
                    break
                code = await validate_media(path, deep=True)
                if code == "ok" or (code == "no_audio" and not strict):
                    ok = True
                    break
                if code == "no_audio":
                    logger.warning("%s returned a silent video, trying another engine: %s",
                                   source, link)
                    _SOFT_REJECT.set(True)
                    return False
                logger.warning("%s video corrupt, retry %d/3: %s", source, attempt + 1, link)
            if ok:
                w, h, dur = await probe_video(path)
                thumb = await make_thumbnail(path, tmp) if flag("thumbnails") else None
                vkw = {"supports_streaming": True}
                if w:
                    vkw["width"] = w
                if h:
                    vkw["height"] = h
                if dur:
                    vkw["duration"] = dur
                if thumb:
                    vkw["thumbnail"] = FSInputFile(thumb)
                media.append(InputMediaVideo(media=FSInputFile(path), **vkw))
                _vmeta[id(media[-1])] = vkw

        if not media:
            if skipped_big:
                await message.reply(t("too_big_media", mb=MAX_FILE_SIZE // 1024 // 1024))
                return True
            return False

        for group in _chunked(media, 10):
            if len(group) == 1:
                one = group[0]
                if isinstance(one, InputMediaPhoto):
                    await message.reply_photo(one.media, disable_notification=True)
                else:
                    # Telegram needs duration/size to play a video inline —
                    # without them the message looks like a broken file.
                    await message.reply_video(one.media, disable_notification=True,
                                              **_vmeta.get(id(one), {}))
            else:
                await message.reply_media_group(group, disable_notification=True)

        logger.info("%s sent %d item(s) for %s", source, len(media), message.chat.id)
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def ytdlp_photos(url):
    """Image URLs of a post that carries no video at all. [] if there are none.

    An Instagram photo post — single or a carousel — makes every video engine
    give up: yt-dlp reports "No video formats found!" and Cobalt answers
    error.api.fetch.empty. The post is still perfectly readable, it simply has
    pictures instead of a video, so we ask yt-dlp for the metadata with
    --ignore-no-formats-error (without it the same error stops the dump too)
    and take the largest thumbnail of every entry.
    Фото-пост в Instagram — одиничний чи карусель — валить усі відео-рушії:
    yt-dlp каже "No video formats found!", Cobalt — error.api.fetch.empty.
    Пост при цьому цілком читабельний, просто в ньому картинки замість відео,
    тому просимо метадані з --ignore-no-formats-error (без нього та сама
    помилка обриває і дамп) і беремо найбільшу мініатюру кожного елемента.
    """
    args = ["yt-dlp", "--dump-single-json", "--skip-download", "--no-warnings",
            "--ignore-no-formats-error", "--yes-playlist"] + common_ytdlp()
    _with_auth(args, url)
    args.append(url)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        data = json.loads((out or b"{}").decode("utf-8", "ignore") or "{}")
    except Exception:  # noqa: BLE001
        logger.info("photo lookup failed: %s", url)
        return []

    return photos_from_dump(data)[:tunable("playlist_max")]


def _best_image(node):
    """The largest thumbnail of one entry, or its direct image URL."""
    thumbs = [t for t in (node.get("thumbnails") or []) if t.get("url")]
    if thumbs:
        # yt-dlp orders thumbnails worst -> best, but the order is not
        # guaranteed, so pick by area rather than by position.
        # yt-dlp вкладає мініатюри від гіршої до кращої, але порядок не
        # гарантований — тому беремо за площею, а не за позицією.
        thumbs.sort(key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
        return thumbs[-1]["url"]
    return node.get("display_url") or node.get("thumbnail")


def photos_from_dump(data):
    """Image URLs out of a yt-dlp JSON dump, in the order of the post."""
    if not isinstance(data, dict):
        return []
    entries = data.get("entries")
    nodes = entries if isinstance(entries, list) and entries else [data]
    photos = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        # An entry that does have formats is a video — it belongs to the video
        # path, and coming through here it would arrive as a still frame.
        # Елемент, у якого формати є, — це відео: воно належить відео-шляху,
        # а через цей шлях приїхало б стоп-кадром.
        if node.get("formats") or str(node.get("url") or "").endswith(".mp4"):
            continue
        link = _best_image(node)
        if link and link not in photos:
            photos.append(link)
    return photos


async def fetch_title(url):
    """Ask yt-dlp for the title only (no download). Returns None on failure."""
    args = ["yt-dlp", "--no-playlist", "--skip-download", "--no-warnings",
            "--print", "%(title)s"] + common_ytdlp()
    _with_auth(args, url)
    args.append(url)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=45)
        title = (out or b"").decode("utf-8", "ignore").strip().splitlines()
        title = title[0].strip() if title else ""
        if title and title.upper() != "NA":
            return title[:200]
    except Exception:  # noqa: BLE001
        logger.info("title lookup failed: %s", url)
    return None


def want_title(message):
    mode = titles_mode()
    if mode == "off":
        return False
    if mode == "all":
        return True
    return getattr(getattr(message, "chat", None), "type", None) == ChatType.PRIVATE


async def resolve_track_title(url):
    """Read the PUBLIC title/artist of a Spotify/Deezer track via oEmbed.
    No login, no DRM — we only learn what the song is called."""
    plat = _platform(url)
    api = None
    if plat == "spotify":
        api = "https://open.spotify.com/oembed?url=" + url
    elif plat == "deezer":
        api = "https://api.deezer.com/oembed?url=" + url
    if not api:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api, headers={"User-Agent": USER_AGENT},
                                   timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    logger.info("oEmbed HTTP %s for %s", resp.status, url)
                    return None
                data = await resp.json(content_type=None)
    except Exception:  # noqa: BLE001
        logger.warning("oEmbed lookup failed for %s", url)
        return None
    title = (data or {}).get("title")
    if not title:
        return None
    author = (data or {}).get("author_name") or ""
    query = ("%s %s" % (title, author)).strip()
    logger.info("Resolved track: %s", query)
    return query


async def handle_cobalt(bot, message, url, api_url=None, strict=False, retries=None):
    """Try Cobalt; on a broken/empty answer retry the same request after a pause
    (Instagram frequently serves a good file a few seconds later)."""
    attempts = max(1, (tunable("cobalt_retries") if retries is None else retries))
    for attempt in range(attempts):
        if await _cobalt_once(bot, message, url, api_url, strict):
            return True
        if attempt + 1 < attempts:
            logger.info("Cobalt attempt %d/%d failed, retrying in %.1fs: %s",
                        attempt + 1, attempts, tunable("cobalt_retry_delay"), url)
            await asyncio.sleep(tunable("cobalt_retry_delay"))
    return False


async def _cobalt_once(bot, message, url, api_url=None, strict=False):
    data = await cobalt_request(url, api_url)
    if not data or data.get("status") == "error":
        code = ""
        if data and isinstance(data.get("error"), dict):
            code = data["error"].get("code", "")
        logger.warning("Cobalt error for %s: %s", url, code or "no response")
        return False

    status = data.get("status")
    logger.info("Cobalt status=%s for %s", status, url)
    items = []
    if status == "picker":
        for it in data.get("picker", []):
            items.append((it.get("type", "photo"), it.get("url")))
    elif status in ("tunnel", "redirect"):
        items.append(("video", data.get("url")))
    elif status == "local-processing":
        # Only reachable if the instance sets FORCE_LOCAL_PROCESSING=always,
        # since we ask for it to be disabled. Say so plainly instead of
        # reporting an unknown status nobody can act on.
        # Дістатись сюди можна лише якщо інстанс має FORCE_LOCAL_PROCESSING=always,
        # бо ми просимо його вимкнути. Кажемо це прямо, а не звітуємо про
        # невідомий статус, з яким нічого не зробиш.
        logger.warning(
            "Cobalt returned local-processing for %s: the instance forces "
            "on-device merging (FORCE_LOCAL_PROCESSING). Set it to 'never'.", url)
        return False
    else:
        logger.warning("Cobalt unsupported status=%s for %s", status, url)
        return False
    return await send_media(bot, message, items, "Cobalt",
                            strict=strict and _expects_video(url))


async def tikwm_request(url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                TIKWM_API,
                params={"url": url, "hd": "1"},
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                if not isinstance(data, dict) or data.get("code") != 0:
                    logger.warning("tikwm code=%s msg=%s", (data or {}).get("code"), (data or {}).get("msg"))
                    return None
                return data
    except Exception:  # noqa: BLE001
        logger.exception("tikwm request failed")
        return None


async def handle_tikwm(bot, message, url):
    data = await tikwm_request(url)
    if not data:
        return False
    d = data.get("data") or {}
    images = d.get("images") or []
    if not images:
        return False
    items = [("photo", u) for u in images]
    return await send_media(bot, message, items, "tikwm")


# ----------------------------------------------------------------------------
# yt-dlp send helpers (video + audio), with integrity check + quality fallback
# ----------------------------------------------------------------------------


async def try_ytdlp_send(bot, message, dl_url, ladder, prefer_merge, allow_silent=True):
    """
    Download a video, VERIFY it plays (video+audio, not corrupt), and send it.
    Re-downloads (different format / lower quality) if the file is broken or
    silent — better slow than a broken file. Never sends a corrupt file.

    Returns (status, sent_message): status in
    {'sent', 'sent_silent', 'toobig', 'fail'}.
    """
    video = None
    best_effort = None  # plays fine but has no audio (maybe a genuinely silent clip)
    saw_toobig = False
    try:
        fmt_order = (
            (_merge_fmt, _combined_fmt) if prefer_merge else (_combined_fmt, _merge_fmt)
        )
        for height in ladder:
            for fmt in (fn(height) for fn in fmt_order):
                f, error = await ytdlp_download(dl_url, height, fmt)
                if error:
                    break  # download failed at this height -> try next (lower) one
                if f.stat().st_size > MAX_FILE_SIZE:
                    cleanup(f)
                    saw_toobig = True
                    break  # too big -> try next (lower) height
                code = await validate_media(f)
                if code == "ok":
                    video = f
                    sent = await send_video_with_meta(message, video)
                    logger.info("Sent %s (%dp)", dl_url, height)
                    return "sent", sent
                if code == "no_audio":
                    if best_effort is None:
                        best_effort = f  # keep one playable copy as a last resort
                    else:
                        cleanup(f)
                    continue  # try the combined format (usually restores audio)
                # code == "bad" -> corrupt/truncated, discard and retry
                logger.warning("Corrupt/incomplete download, retrying: %s", dl_url)
                cleanup(f)

        # Nothing came back fully valid.
        if best_effort is not None and not allow_silent:
            # Другий рушій ще може дати відео зі звуком — не шлемо німе.
            logger.info("Silent result held back, another engine may do better: %s", dl_url)
            _SOFT_REJECT.set(True)
            return "fail", None
        if best_effort is not None:
            video = best_effort
            best_effort = None
            sent = await send_video_with_meta(message, video)
            logger.info("Sent best-effort (no audio) %s", dl_url)
            return "sent_silent", sent
        if saw_toobig:
            await message.reply(t("too_big_video", mb=MAX_FILE_SIZE // 1024 // 1024))
            return "toobig", None
        return "fail", None
    finally:
        keep_for_soundtrack(video)
        cleanup(video)
        cleanup(best_effort)


async def try_ytdlp_audio(bot, message, url):
    """Extract MP3 and send it named after the source. Returns (status, sent_msg)."""
    audio_path = None
    local_src = _AUDIO_SRC.get()
    _AUDIO_SRC.set(None)
    try:
        # Prefer the file we already downloaded — no second request, so no
        # throttling and no repeated extraction failure.
        # Спершу — файл, який уже завантажили: жодного другого запиту, отже
        # ні притискання, ні повторного падіння видобування.
        if local_src is not None and local_src.exists():
            audio_path = await ffmpeg_extract_mp3(local_src)
        if audio_path is None:
            audio_path, error = await ytdlp_audio(url)
            if error:
                return "fail", None
        if audio_path.stat().st_size > MAX_FILE_SIZE:
            await message.reply(t("too_big_audio", mb=MAX_FILE_SIZE // 1024 // 1024))
            return "toobig", None
        title, artist = await probe_tags(audio_path)
        fname = ((title or "").strip()[:120] or "audio") + ".mp3"
        sent = await message.reply_audio(
            FSInputFile(audio_path, filename=fname),
            title=title,
            performer=artist,
            disable_notification=True,
        )
        job = _JOB.get()
        if job is not None:
            try:
                job["size"] = audio_path.stat().st_size
            except Exception:  # noqa: BLE001
                pass
            a = getattr(sent, "audio", None)
            th = getattr(a, "thumbnail", None) if a is not None else None
            job["thumb"] = getattr(th, "file_id", None) if th is not None else None
        logger.info("Sent audio '%s' for %s", title or "?", url)
        return "sent", sent
    finally:
        cleanup(audio_path)
        cleanup(local_src)


async def _video_with_cache(bot, message, cache_url, dl_url, ladder, prefer_merge,
                            allow_silent=True):
    """Video via cache/yt-dlp. Returns a status string
    ('sent'/'sent_silent'/'toobig'/'fail'). Only valid videos are cached."""
    async def from_cache():
        c = cache_get(cache_url, False)
        if not c:
            return None
        try:
            await message.reply_video(c["file_id"], disable_notification=True)
            logger.info("cache hit (video) %s", cache_url)
            return "sent"
        except Exception:  # noqa: BLE001
            cache_del(cache_url, False)
        return None

    async def download():
        status, sent = await try_ytdlp_send(bot, message, dl_url, ladder, prefer_merge,
                                            allow_silent=allow_silent)
        if status == "sent" and sent and sent.video:
            cache_set(cache_url, False, "video", sent.video.file_id)
        return status

    hit = await from_cache()
    if hit:
        return hit
    if not cache_enabled():
        # Nothing to share afterwards: making the second request wait for the
        # first would only delay a download it still has to do itself.
        # Ділитись потім нічим: змусити другий запит чекати на перший означало б
        # лише відкласти завантаження, яке він усе одно виконає сам.
        return await download()

    # Two people posting the same link at the same time used to download it
    # twice, in parallel, and store the same file twice over.
    # Двоє, що кинули однакове посилання одночасно, качали його двічі
    # паралельно й двічі зберігали той самий файл.
    async with _inflight_lock(_ckey(cache_url, False)):
        hit = await from_cache()        # the first one may have just filled it
        return hit or await download()


# ----------------------------------------------------------------------------
# Route one link
# ----------------------------------------------------------------------------


async def process_url(bot, message, url, is_private, want_audio, via="chat", trim=None,
                      quality=None, abr=None):
    plat = _platform(url)
    source = _source_label(url, plat, want_audio)
    job = _new_job(message, source, "audio" if want_audio else "video")
    _JOB.set(job)
    _FAIL_REASON.set(None)
    _TRIM.set(trim)
    _QUALITY.set(quality)
    _ABR.set(abr)
    fu = getattr(message, "from_user", None)
    uid = getattr(fu, "id", None) if fu else getattr(message.chat, "id", 0)
    status = "fail"
    if not await url_is_safe(url):
        _finish_job(job, "error")
        await message.reply(t("cant_link"))
        return "fail", source
    free = free_space()
    if free is not None and free < MIN_FREE_SPACE:
        logger.warning("Only %d MB left in %s — refusing to start",
                       free // 1024 // 1024, WORK_DIR)
        _finish_job(job, "error")
        await message.reply(t("no_space"))
        return "fail", source
    try:
        async with _user_gate(uid):
            job_retries = tunable("job_retries")
            for attempt in range(job_retries + 1):
                try:
                    # A job holds a slot of the per-user gate for its whole
                    # life. Every yt-dlp call has its own timeout, but a job
                    # runs a whole ladder of them across several engines, so a
                    # link that fails slowly could occupy the slot for the sum
                    # of all of them — half an hour on a short clip.
                    # Джоба тримає місце у воротаря на весь свій час. У кожного
                    # виклику yt-dlp є власний таймаут, але джоба проганяє цілу
                    # драбину таких викликів по кількох рушіях, тож посилання,
                    # яке падає повільно, займало б слот на суму всіх — пів
                    # години на короткому кліпі.
                    status, source = await asyncio.wait_for(
                        _do_process(bot, message, url, is_private,
                                    want_audio, plat, source),
                        timeout=job_deadline(url),
                    )
                    job["source"] = source
                    break
                except asyncio.TimeoutError:
                    logger.warning("Job deadline of %ds reached: %s",
                                   job_deadline(url), url)
                    status = "error"
                    await message.reply(t("err_timeout"))
                    break
                except TelegramRetryAfter:
                    raise            # already handled by the middleware
                except Exception:  # noqa: BLE001
                    if attempt < job_retries:
                        logger.warning("Transient error on %s, retry %d/%d in %.0fs",
                                       url, attempt + 1, job_retries,
                                       tunable("job_retry_delay"))
                        await asyncio.sleep(tunable("job_retry_delay"))
                        continue
                    raise
    except Exception:  # noqa: BLE001
        logger.exception("Error processing %s", url)
        status = "error"
        try:
            await message.reply(t("proc_error"))
        except Exception:  # noqa: BLE001
            pass
    finally:
        _finish_job(job, status)
        took = time.time() - job["ts"]
        _notify_after = tunable("long_job_notify")
        if (status in ("sent", "sent_silent") and _notify_after > 0
                and took >= _notify_after):
            try:
                await message.reply(t("done_long", s=int(took)))
            except Exception:  # noqa: BLE001
                pass
        await record_event(message, url, plat, source, status, via)


async def _do_process(bot, message, url, is_private, want_audio, plat, source):
    if SERVICES.get(plat, {}).get("audio"):
        want_audio = True
    if SERVICES.get(plat, {}).get("resolve"):
        query = await resolve_track_title(url)
        if not query:
            await message.reply(t("cant_link"))
            return "fail", source
        url = "ytsearch1:" + query          # download the match from YouTube
        want_audio = True
    # --- Audio (MP3) mode ---
    if want_audio:
        async with ChatActionSender(
            bot=bot, chat_id=message.chat.id, action=ChatAction.UPLOAD_DOCUMENT
        ):
            c = cache_get(url, True)
            if c:
                try:
                    await message.reply_audio(c["file_id"], disable_notification=True)
                    return "sent", source
                except Exception:  # noqa: BLE001
                    cache_del(url, True)
            status, sent = await try_ytdlp_audio(bot, message, url)
            if status == "sent" and sent and sent.audio:
                cache_set(url, True, "audio", sent.audio.file_id)
            elif status == "fail":
                await message.reply(t("cant_audio"))
            return status, source

    # --- Video mode ---
    async with ChatActionSender(
        bot=bot, chat_id=message.chat.id, action=ChatAction.UPLOAD_VIDEO
    ):
        svc = SERVICES.get(plat, {})
        if want_title(message) and _TITLE.get() is None:
            _TITLE.set(await fetch_title(url))
        long_video = _needs_extended(url)
        ladder = long_ladder() if long_video else quality_ladder()
        want_h = _QUALITY.get()
        if want_h:
            # Requested cap first, then the usual lower rungs as fallback.
            ladder = [want_h] + [h for h in (1440, 1080, 720, 480, 360) if h < want_h]
        prefer_merge = bool(svc.get("merge"))
        dl_url = url.split("?", 1)[0] if svc.get("strip") else url

        # TikTok photo carousels: tikwm -> Cobalt -> yt-dlp (/photo/ -> /video/).
        if svc.get("carousel"):
            canon = url
            if "vt.tiktok" in url.lower() or "vm.tiktok" in url.lower():
                canon = await resolve_redirect(url)
            canon = canon.split("?", 1)[0]
            if "/photo/" in canon.lower():
                source = "tiktok_carousel"
                if flag("eng_tikwm"):
                    logger.info("TikTok carousel -> tikwm: %s", canon)
                    if await handle_tikwm(bot, message, canon):
                        return "sent", source
                    logger.info("tikwm failed -> fallback Cobalt")
                if cobalt_on() and await handle_cobalt(bot, message, canon):
                    return "sent", source
                st, _ = await try_ytdlp_send(
                    bot, message, canon.replace("/photo/", "/video/"), ladder, prefer_merge
                )
                if st in ("sent", "sent_silent", "toobig"):
                    return st, source
                await message.reply(t("cant_carousel"))
                return "fail", source
            dl_url = canon

        async def _maybe_soundtrack():
            """Optionally send the audio of a short video as a separate MP3."""
            if not audio_too_enabled() or svc.get("audio") or long_video:
                # Nobody will cut a soundtrack out of it — do not leave it lying around.
                # Ніхто не різатиме з нього доріжку — не лишаємо файл валятися.
                cleanup(_AUDIO_SRC.get())
                _AUDIO_SRC.set(None)
                return
            try:
                st_a, sent_a = await try_ytdlp_audio(bot, message, dl_url)
                if st_a == "sent":
                    logger.info("Soundtrack sent separately: %s", dl_url)
            except Exception:  # noqa: BLE001
                logger.info("soundtrack extraction failed: %s", dl_url)

        # Pass 1 — strict: every engine must return a complete video (with audio,
        # not an image). Pass 2 — lenient: accept a silent best-effort file.
        engines = engine_order(plat, svc)
        _SOFT_REJECT.set(False)
        for strict in (True, False):
            if not strict and not _SOFT_REJECT.get():
                # Every engine failed outright — a lenient retry would just
                # repeat the same requests and waste ~20 seconds.
                logger.info("All engines failed hard, skipping lenient pass: %s", dl_url)
                break
            for eng in engines:
                if not engine_available(plat, eng):
                    logger.info("Skipping %s for %s — paused after repeated failures",
                                eng, plat)
                    continue
                if eng == "cobalt":
                    if cobalt_on() and await handle_cobalt(bot, message, dl_url,
                                                            strict=strict):
                        engine_result(plat, eng, True)
                        logger.info("Sent via Cobalt%s: %s",
                                    " (strict)" if strict else "", dl_url)
                        await _maybe_soundtrack()
                        return "sent", source
                    engine_result(plat, eng, False)
                elif eng == "ytdlp":
                    logger.info("Video -> yt-dlp%s: %s",
                                " (strict)" if strict else "", dl_url)
                    st = await _video_with_cache(bot, message, dl_url, dl_url, ladder,
                                                 prefer_merge, allow_silent=not strict)
                    if st in ("sent", "sent_silent", "toobig"):
                        engine_result(plat, eng, True)
                        if st != "toobig":
                            await _maybe_soundtrack()
                        return st, source
                    engine_result(plat, eng, False)
        if COBALT_FALLBACK_URL and await handle_cobalt(bot, message, dl_url, COBALT_FALLBACK_URL):
            logger.info("Sent via secondary Cobalt: %s", dl_url)
            return "sent", source
        # Last resort: the post may simply have no video in it. Photo posts and
        # photo carousels fail every engine above with "No video formats found",
        # which reads like a breakage while the post is fine — it is pictures.
        # Остання спроба: у пості може просто не бути відео. Фото-пости й фото-
        # каруселі валять усі рушії вище з "No video formats found", і це схоже
        # на поломку, хоча пост цілий — там картинки.
        photos = await ytdlp_photos(dl_url)
        if photos:
            logger.info("No video, sending %d photo(s): %s", len(photos), dl_url)
            if await send_media(bot, message, [("photo", p) for p in photos],
                                "%s_photos" % plat):
                return "sent", "%s_photos" % plat
        # Say what actually went wrong, if any engine told us.
        # Сказати, що саме сталося, якщо хоч один рушій це повідомив.
        await message.reply(t(_FAIL_REASON.get() or "cant_video"))
        return "fail", source


# ----------------------------------------------------------------------------
# Update check + admin notification
# ----------------------------------------------------------------------------


def installed_version(package):
    try:
        return pkg_version(package)
    except PackageNotFoundError:
        return "?"


async def latest_pypi_version(session, package):
    url = "https://pypi.org/pypi/{}/json".format(package)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return data.get("info", {}).get("version")
    except Exception:  # noqa: BLE001
        logger.warning("Failed to check %s on PyPI", package)
        return None


def _version_tuple(v):
    return tuple(int(c) if c.isdigit() else 0 for c in re.split(r"[.\-]", v))


def _num_version(s):
    m = re.search(r"\d+(?:\.\d+)*", s or "")
    return m.group(0) if m else ""


async def cobalt_installed_version(session):
    try:
        async with session.get(
            COBALT_API_URL.rstrip("/") + "/", timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return data.get("cobalt", {}).get("version")
    except Exception:  # noqa: BLE001
        logger.warning("Failed to read Cobalt version")
        return None


def _gh_headers():
    h = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    if GITHUB_TOKEN:
        h["Authorization"] = "Bearer " + GITHUB_TOKEN   # avoids anonymous rate limits
    return h


async def cobalt_latest_version(session):
    """Latest Cobalt version, taken from tags.

    Cobalt does not publish GitHub releases — /releases/latest answers 404 —
    so we read the tags directly (they are named web-x.y.z / api-x.y.z).
    One request instead of two, and no 404 noise in the log.
    """
    try:
        async with session.get(
            "https://api.github.com/repos/imputnet/cobalt/tags?per_page=30",
            headers=_gh_headers(), timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                return None
            tags = await resp.json()
    except Exception:  # noqa: BLE001
        return None
    best = None
    for t in tags or []:
        name = (t or {}).get("name") or ""
        num = _num_version(name)
        if not num:
            continue
        if best is None or _version_tuple(num) > _version_tuple(_num_version(best)):
            best = name
    return best


async def notify_admin(bot, text):
    if not ADMIN_ID:
        return
    try:
        await bot.send_message(ADMIN_ID, text, disable_notification=True)
    except Exception:  # noqa: BLE001
        logger.warning("Failed to message admin %s", ADMIN_ID)


async def github_latest_commit(session):
    """Latest commit on the watched branch: (sha, short message, author date)."""
    if not GITHUB_REPO:
        return None
    url = "https://api.github.com/repos/{}/commits/{}".format(GITHUB_REPO, GITHUB_BRANCH)
    headers = _gh_headers()
    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status in (401, 403):
                logger.warning("GitHub: token rejected (HTTP %s) for %s",
                               resp.status, GITHUB_REPO)
                return None
            if resp.status == 404:
                logger.warning("GitHub: repo %s not found — a private one needs "
                               "GITHUB_TOKEN with read access", GITHUB_REPO)
                return None
            if resp.status != 200:
                logger.info("GitHub check HTTP %s for %s", resp.status, GITHUB_REPO)
                return None
            data = await resp.json()
    except Exception:  # noqa: BLE001
        logger.warning("GitHub check failed for %s", GITHUB_REPO)
        return None
    sha = (data or {}).get("sha")
    if not sha:
        return None
    commit = (data.get("commit") or {})
    msg = (commit.get("message") or "").splitlines()[0][:120]
    when = ((commit.get("author") or {}).get("date") or "")[:10]
    return sha, msg, when


def github_change_note(running, sha, msg):
    """What changed + a link to the diff, so the notice is worth acting on."""
    parts = []
    if msg:
        parts.append("\n\u2022 {}".format(msg))
    if running and sha:
        parts.append("\nhttps://github.com/{}/compare/{}...{}".format(
            GITHUB_REPO, running[:7], sha[:7]))
    return "".join(parts)


async def check_github_update(bot, session, baseline=False):
    """Notify the admin once per new commit that a redeploy is pending."""
    info = await github_latest_commit(session)
    if not info:
        return None
    sha, msg, when = info

    if baseline:
        # Startup: whatever is on the branch right now is what we just deployed.
        await asyncio.to_thread(set_setting_sync, "gh_running_sha", sha)
        await asyncio.to_thread(set_setting_sync, "gh_notified_sha", sha)
        await asyncio.to_thread(settings_load_sync)
        logger.info("Running commit: %s", sha[:7])
        return None

    running = setting("gh_running_sha")
    if running is None:
        # No baseline yet (e.g. GitHub was unreachable at startup) — set it now.
        await asyncio.to_thread(set_setting_sync, "gh_running_sha", sha)
        await asyncio.to_thread(set_setting_sync, "gh_notified_sha", sha)
        await asyncio.to_thread(settings_load_sync)
        return None

    if sha == running:
        return None            # up to date -> stay silent, no line in the report

    if setting("gh_notified_sha") != sha:
        await asyncio.to_thread(set_setting_sync, "gh_notified_sha", sha)
        await asyncio.to_thread(settings_load_sync)
        await notify_admin(bot, t("new_version", what=github_change_note(running, sha, msg)))
    return t("rep_version", sha=sha[:7], msg=msg or "")


async def check_updates(bot):
    lines = []
    async with aiohttp.ClientSession() as session:
        for pkg in WATCHED_PACKAGES:
            cur = installed_version(pkg)
            latest = await latest_pypi_version(session, pkg)
            if latest and cur != "?" and _version_tuple(latest) > _version_tuple(cur):
                lines.append(t("rep_lib", name=pkg, cur=cur, latest=latest))
                if _notified.get(pkg) != latest:
                    _notified[pkg] = latest
                    await notify_admin(
                        bot,
                        t("new_lib", name=pkg, cur=cur, latest=latest),
                    )
            else:
                # Up to date (or unknown) — no point showing "latest", it is the same.
                mark = "\U0001f7e2" if latest else "⚪️"
                lines.append("{} {}: {}".format(mark, pkg, cur))

        if cobalt_on():
            cur = await cobalt_installed_version(session)
            latest = await cobalt_latest_version(session)
            cur_n, latest_n = _num_version(cur), _num_version(latest)
            if cur_n and latest_n and _version_tuple(latest_n) > _version_tuple(cur_n):
                lines.append(t("rep_lib", name="cobalt", cur=cur, latest=latest))
                if _notified.get("cobalt") != latest:
                    _notified["cobalt"] = latest
                    await notify_admin(
                        bot,
                        t("new_lib", name="cobalt", cur=cur, latest=latest),
                    )
            else:
                mark = "\U0001f7e2" if (cur and latest) else "⚪️"
                lines.append("{} cobalt: {}".format(mark, cur or "?"))

        line = await check_cookies_expiry(bot)
        if line:
            lines.append(line)

        if GITHUB_REPO:
            gh_line = await check_github_update(bot, session)
            if gh_line:
                lines.append(gh_line)

    return "\n".join(lines)


def cookies_status():
    """Summary for the panel: present / days left / which keys / state."""
    if not cookies_ready():
        return {"present": False}
    try:
        ok, info = parse_cookies_txt(
            Path(COOKIES_FILE).read_text(encoding="utf-8", errors="ignore"))
    except Exception:  # noqa: BLE001
        return {"present": False}
    if not ok:
        return {"present": False}
    days = info.get("days")
    warn = tunable("cookies_warn_days")
    if days is None:
        state = "ok"
    elif days <= 0:
        state = "expired"
    elif warn and days <= warn:
        state = "soon"
    else:
        state = "ok"
    try:
        updated = int(Path(COOKIES_FILE).stat().st_mtime)
    except Exception:  # noqa: BLE001
        updated = None
    return {
        "present": True,
        "days": days,
        "state": state,
        "count": info.get("count"),
        "sites": info.get("domains", []),
        "keys": info.get("auth", []),
        "updated": updated,
        "expires": info.get("expires"),
        "expires_key": info.get("expires_key"),
    }


async def check_cookies_expiry(bot):
    """Warn once before the session dies, and once after it already has."""
    warn_days = tunable("cookies_warn_days")
    if warn_days <= 0:
        return None
    path = Path(COOKIES_FILE)
    if not cookies_ready():
        if setting("ck_state") not in (None, "absent"):
            await asyncio.to_thread(set_setting_sync, "ck_state", "absent")
            await asyncio.to_thread(settings_load_sync)
        return None
    try:
        ok, info = parse_cookies_txt(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:  # noqa: BLE001
        return None
    if not ok:
        return None
    days = info.get("days")
    if days is None:
        return t("rep_ck_forever")

    if days <= 0:
        state, mark = "expired", "\U0001f534"
        line = t("rep_ck_expired")
    elif days <= warn_days:
        state, mark = "soon", "\U0001f7e1"
        line = t("rep_ck_soon", days=days)
    else:
        state, mark = "ok", "\U0001f7e2"
        line = t("rep_ck_ok", days=days)

    # Notify only when the state changes, so it is never spammy.
    if state != setting("ck_state") and state in ("soon", "expired"):
        await notify_admin(
            bot,
            t("ck_expired") if state == "expired" else t("ck_expiring", days=days),
        )
    if state != setting("ck_state"):
        await asyncio.to_thread(set_setting_sync, "ck_state", state)
        await asyncio.to_thread(settings_load_sync)
    return line


async def github_baseline(bot):
    """Record the commit we are running right after start."""
    if not GITHUB_REPO:
        return
    try:
        async with aiohttp.ClientSession() as session:
            await check_github_update(bot, session, baseline=True)
    except Exception:  # noqa: BLE001
        logger.exception("GitHub baseline failed")


async def update_checker_loop(bot):
    if not ADMIN_ID:
        return
    await asyncio.sleep(10)
    while True:
        minutes = tunable("check_minutes")
        if minutes <= 0:
            await asyncio.sleep(600)       # checks disabled — poll the setting
            continue
        try:
            await check_updates(bot)
        except Exception:  # noqa: BLE001
            logger.exception("Update check error")
        await asyncio.sleep(minutes * 60)


# ----------------------------------------------------------------------------
# Web server: /health + (optional) Mini App
# ----------------------------------------------------------------------------


def verify_webapp_init_data(init_data, bot_token):
    """Verify Telegram WebApp initData signature. Returns the user dict or None."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash", "")
        if not received:
            return None
        check = "\n".join("%s=%s" % (k, pairs[k]) for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None
        if WEBAPP_AUTH_TTL > 0:
            auth_date = int(pairs.get("auth_date", "0") or 0)
            if not auth_date or (time.time() - auth_date) > WEBAPP_AUTH_TTL:
                return None
        return json.loads(pairs["user"]) if pairs.get("user") else {}
    except Exception:  # noqa: BLE001
        return None


class _Chat:
    def __init__(self, chat_id):
        self.id = chat_id
        self.type = ChatType.PRIVATE


class ChatTarget:
    """Message-like adapter so the download pipeline can send to a chat_id the
    same way it replies to an incoming Message."""

    def __init__(self, bot, chat_id):
        self._bot = bot
        self.chat = _Chat(chat_id)
        self.from_user = None

    async def reply(self, text, **kw):
        return await self._bot.send_message(self.chat.id, text, **kw)

    async def reply_video(self, video, **kw):
        return await self._bot.send_video(self.chat.id, video, **kw)

    async def reply_audio(self, audio, **kw):
        return await self._bot.send_audio(self.chat.id, audio, **kw)

    async def reply_document(self, document, **kw):
        return await self._bot.send_document(self.chat.id, document, **kw)

    async def reply_photo(self, photo, **kw):
        return await self._bot.send_photo(self.chat.id, photo, **kw)

    async def reply_media_group(self, media, **kw):
        return await self._bot.send_media_group(self.chat.id, media, **kw)


async def start_web_server(bot):
    async def health(_request):
        return web.json_response({"status": "ok"})

    async def serve_index(_request):
        try:
            with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
                return web.Response(text=f.read(), content_type="text/html")
        except Exception:  # noqa: BLE001
            return web.Response(status=404, text="Mini App not available")

    def _is_admin_user(user):
        return bool(user and ADMIN_ID and int(user.get("id", 0)) == ADMIN_ID)

    async def api_download(request):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "bad_request"}, status=400)
        user = verify_webapp_init_data(body.get("initData", ""), BOT_TOKEN)
        if not user or not user.get("id"):
            return web.json_response({"ok": False, "error": "auth_failed"}, status=403)
        uid = int(user["id"])
        level = resolve_access(uid, user.get("username"), uid, False)
        if level == "none":
            return web.json_response({"ok": False, "error": "no_access"}, status=403)
        urls = extract_urls(body.get("url", ""))
        if not urls:
            return web.json_response({"ok": False, "error": "no_link"}, status=400)
        is_extended = level in ("extended", "admin")
        base_audio = is_extended and body.get("mode") == "audio"
        try:
            q = int(body.get("quality") or 0) or None
        except (TypeError, ValueError):
            q = None
        try:
            abr = int(body.get("abr") or 0) or None
        except (TypeError, ValueError):
            abr = None
        urls = [u for u in urls if is_extended or not _needs_extended(u)]
        if not urls:
            return web.json_response({"ok": False, "error": "need_extended"}, status=403)
        urls = await expand_targets(urls, is_extended)
        if not urls:
            return web.json_response({"ok": False, "error": "need_extended"}, status=403)
        urls = urls[:tunable("max_links")]
        trim = _extract_trim(body.get("url", ""), extract_urls(body.get("url", "")))
        target = ChatTarget(bot, uid)
        for u in urls:
            spawn(process_url(
                bot, target, u, is_extended,
                _is_youtube_music(u) or base_audio, "miniapp", trim=trim,
                quality=q, abr=abr))
        logger.info("Mini App job from user %s (level=%s): %d link(s)", uid, level, len(urls))
        return web.json_response({"ok": True, "queued": len(urls)})

    async def api_stats(request):
        user = verify_webapp_init_data(request.headers.get("X-Telegram-Init-Data", ""), BOT_TOKEN)
        if not _is_admin_user(user):
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        return web.json_response({"ok": True, "stats": await asyncio.to_thread(db_stats_sync)})

    async def api_events(request):
        user = verify_webapp_init_data(request.headers.get("X-Telegram-Init-Data", ""), BOT_TOKEN)
        if not _is_admin_user(user):
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        try:
            limit = min(500, max(1, int(request.query.get("limit", "50"))))
        except Exception:  # noqa: BLE001
            limit = 50
        return web.json_response({"ok": True, "events": await asyncio.to_thread(db_events_sync, limit)})

    async def api_settings(request):
        user = verify_webapp_init_data(request.headers.get("X-Telegram-Init-Data", ""), BOT_TOKEN)
        if not _is_admin_user(user):
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        if request.method == "POST":
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                body = {}
            for k in ("whitelist", "notify_no_access", "lang", "cache", "engine_pref",
                      "titles_mode", "audio_too"):
                if k in body:
                    await asyncio.to_thread(set_setting_sync, k, body[k])
            for k in FLAGS:
                if k in body:
                    await asyncio.to_thread(set_setting_sync, k, body[k])
            for k, v in body.items():
                if k.startswith("svc:") and k[4:] in SERVICES:
                    await asyncio.to_thread(set_setting_sync, k, v)
            for k in TUNABLES:
                if k in body:
                    try:
                        _, cast, lo, hi = TUNABLES[k]
                        val = max(lo, min(hi, cast(body[k])))
                    except (TypeError, ValueError):
                        continue
                    await asyncio.to_thread(set_setting_sync, k, val)
            await asyncio.to_thread(settings_load_sync)
        return web.json_response({"ok": True, "settings": {
            "whitelist": "1" if whitelist_on() else "0",
            "notify_no_access": setting("notify_no_access", "0"),
            "lang": cur_lang(),
            "cache": "1" if cache_enabled() else "0",
            "engine_pref": str(setting("engine_pref", "auto") or "auto"),
            "titles_mode": titles_mode(),
            "audio_too": "1" if audio_too_enabled() else "0",
            "flags": {k: ("1" if flag(k) else "0") for k in FLAGS},
            "cache_info": cache_info(),
            "cookies": "1" if cookies_ready() else "0",
            "cookies_info": cookies_status(),
            "tunables": {k: tunable(k) for k in TUNABLES},
        }})

    async def api_backup(request):
        """Panel button: build the copy and push it to the admin's private chat."""
        user = verify_webapp_init_data(
            request.headers.get("X-Telegram-Init-Data", ""), BOT_TOKEN)
        if not _is_admin_user(user):
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        if not Path(STATS_DB).exists():
            return web.json_response({"ok": False, "error": "no_db"}, status=404)
        ok, kb = await send_backup(bot, ADMIN_ID)
        if not ok:
            return web.json_response({"ok": False, "error": "failed"}, status=500)
        return web.json_response({"ok": True, "kb": kb})

    async def api_cache_clear(request):
        user = verify_webapp_init_data(
            request.headers.get("X-Telegram-Init-Data", ""), BOT_TOKEN)
        if not _is_admin_user(user):
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        n = len(_cache)
        cache_clear()
        logger.info("Cache cleared from the panel (%d entries)", n)
        return web.json_response({"ok": True, "cleared": n, "info": cache_info()})

    async def api_lang(request):
        # Any authenticated Mini App user can read the UI language.
        user = verify_webapp_init_data(request.headers.get("X-Telegram-Init-Data", ""), BOT_TOKEN)
        if not user or not user.get("id"):
            return web.json_response({"ok": False, "error": "auth_failed"}, status=403)
        return web.json_response({"ok": True, "lang": cur_lang()})

    async def api_preview(request):
        """Fetch title/duration/size for a link before downloading anything."""
        user = verify_webapp_init_data(request.headers.get("X-Telegram-Init-Data", ""), BOT_TOKEN)
        if not user or not user.get("id"):
            return web.json_response({"ok": False, "error": "auth_failed"}, status=403)
        raw = request.query.get("url", "")
        urls = extract_urls(raw)
        if not urls:
            return web.json_response({"ok": False, "error": "no_link"}, status=400)
        url = urls[0]
        plat = _platform(url)
        if SERVICES.get(plat, {}).get("resolve"):
            q = await resolve_track_title(url)
            if not q:
                return web.json_response({"ok": False, "error": "not_found"}, status=404)
            return web.json_response({"ok": True, "info": {
                "title": q, "duration": None, "size": None,
                "source": _source_label(url, plat, True)}})
        args = ["yt-dlp", "--dump-single-json", "--no-playlist", "--skip-download",
                "--no-warnings"] + common_ytdlp()
        _with_auth(args, url)
        args.append(url)
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=45)
            data = json.loads(out or b"{}")
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "no_info"}, status=404)
        if not isinstance(data, dict) or not data:
            return web.json_response({"ok": False, "error": "no_info"}, status=404)
        size = data.get("filesize") or data.get("filesize_approx")
        return web.json_response({"ok": True, "info": {
            "title": data.get("title"),
            "duration": data.get("duration"),
            "size": size,
            "uploader": data.get("uploader"),
            "source": _source_label(url, plat, False),
        }})

    async def api_services(request):
        user = verify_webapp_init_data(request.headers.get("X-Telegram-Init-Data", ""), BOT_TOKEN)
        if not _is_admin_user(user):
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        out = [{"id": sid, "label": svc_label(svc), "enabled": service_enabled(sid),
                "tier": svc.get("tier", "basic"), "default": bool(svc.get("default", False))}
               for sid, svc in SERVICES.items() if not svc.get("hidden")]
        return web.json_response({"ok": True, "services": out})

    async def api_progress(request):
        user = verify_webapp_init_data(request.headers.get("X-Telegram-Init-Data", ""), BOT_TOKEN)
        if not user or not user.get("id"):
            return web.json_response({"ok": False, "error": "auth_failed"}, status=403)
        uid = int(user["id"])
        admin = _is_admin_user(user)
        now = time.time()
        for jid in list(_progress):
            j = _progress.get(jid)
            if not j:
                continue
            done = j["status"] in ("done", "error", "toobig")
            if (done and now - j["updated"] > 25) or (now - j["updated"] > 1800):
                _progress.pop(jid, None)
        jobs = [j for j in _progress.values() if admin or j.get("user_id") == uid]
        jobs.sort(key=lambda j: j["ts"], reverse=True)
        out = [{
            "id": j["id"], "source": j["source"], "mode": j["mode"],
            "status": j["status"], "pct": j["pct"], "eta": j["eta"],
            "speed": j["speed"], "chat_title": j["chat_title"], "ts": j["ts"],
        } for j in jobs[:30]]
        return web.json_response({"ok": True, "jobs": out, "admin": admin})

    async def api_thumb(request):
        user = verify_webapp_init_data(request.headers.get("X-Telegram-Init-Data", ""), BOT_TOKEN)
        if not user or not user.get("id"):
            return web.Response(status=403)
        fid = request.query.get("id", "")
        if not fid:
            return web.Response(status=404)
        try:
            data = await fetch_telegram_file(bot, type("F", (), {"file_id": fid})())
            return web.Response(body=data, content_type="image/jpeg")
        except Exception:  # noqa: BLE001
            return web.Response(status=404)

    def chat_label(ident):
        """Name the chat from what we have already seen in the events log."""
        try:
            rows = _db_query(
                "SELECT chat_title FROM events WHERE chat_id=? AND chat_title<>'' "
                "ORDER BY id DESC LIMIT 1", (int(ident),))
            return (rows[0]["chat_title"] if rows else "") or ""
        except Exception:  # noqa: BLE001
            return ""

    async def api_access(request):
        user = verify_webapp_init_data(request.headers.get("X-Telegram-Init-Data", ""), BOT_TOKEN)
        if not _is_admin_user(user):
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        if request.method == "POST":
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                body = {}
            if body.get("action") == "remove":
                await asyncio.to_thread(
                    access_remove_sync, "chat", str(body.get("ident", "")))
            else:
                ident = norm_ident("chat", body.get("ident", ""))
                if ident:
                    label = (body.get("label") or "").strip() or chat_label(ident)
                    await asyncio.to_thread(access_add_sync, "chat", ident, "full", label)
            await asyncio.to_thread(settings_load_sync)
        rows = await asyncio.to_thread(access_list_sync)
        return web.json_response({"ok": True, "access": rows})

    @web.middleware
    async def rate_limit(request, handler):
        """Cap how often one caller may hit the panel API.

        initData is signed and short-lived, so it cannot be forged — but a
        valid one belongs to the admin's own session and stays usable for its
        whole TTL. Nothing above stopped that session from being replayed in a
        loop, and several endpoints start downloads or read the database.
        Requests that fail verification share one bucket of their own, so an
        unauthenticated flood cannot spend the admin's allowance.
        initData підписана й короткоживуча, тож підробити її не можна — але
        дійсна належить сесії самого адміна й лишається придатною весь свій
        строк. Ніщо вище не заважало прокрутити її в циклі, а частина
        ендпоінтів запускає завантаження або читає базу. Запити, які не
        пройшли перевірку, мають власний спільний кошик — щоб потік без
        автентифікації не з'їдав ліміт адміна.
        """
        if not request.path.startswith("/api/"):
            return await handler(request)
        user = verify_webapp_init_data(
            request.headers.get("X-Telegram-Init-Data", ""), BOT_TOKEN)
        key = str(user.get("id")) if user and user.get("id") else "anon"
        now = time.monotonic()
        hits = [t for t in _api_hits.get(key, ()) if now - t < API_RATE_WINDOW]
        if len(hits) >= API_RATE_LIMIT:
            logger.warning("Rate limit hit by %s on %s", key, request.path)
            return web.json_response({"ok": False, "error": "rate_limited"}, status=429)
        hits.append(now)
        _api_hits[key] = hits
        return await handler(request)

    app = web.Application(middlewares=[rate_limit])
    app.router.add_get("/health", health)
    if WEBAPP_ENABLED:
        app.router.add_get("/", serve_index)
        app.router.add_post("/api/download", api_download)
        app.router.add_get("/api/stats", api_stats)
        app.router.add_get("/api/events", api_events)
        app.router.add_get("/api/settings", api_settings)
        app.router.add_post("/api/settings", api_settings)
        app.router.add_get("/api/access", api_access)
        app.router.add_post("/api/access", api_access)
        app.router.add_post("/api/cache/clear", api_cache_clear)
        app.router.add_post("/api/backup", api_backup)
        app.router.add_get("/api/lang", api_lang)
        app.router.add_get("/api/progress", api_progress)
        app.router.add_get("/api/thumb", api_thumb)
        app.router.add_get("/api/services", api_services)
        app.router.add_get("/api/preview", api_preview)
        app.router.add_post("/api/services", api_settings)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HEALTH_PORT)
    await site.start()
    logger.info(
        "Web server on :%d/health%s", HEALTH_PORT,
        " + Mini App /" if WEBAPP_ENABLED else "",
    )


# ----------------------------------------------------------------------------
# aiogram handlers
# ----------------------------------------------------------------------------

dp = Dispatcher()


def _is_admin(message):
    return bool(ADMIN_ID) and message.from_user and message.from_user.id == ADMIN_ID


@dp.message(Command("version"))
async def cmd_version(message, bot):
    if not _is_admin(message):
        return
    await message.reply(t("checking_versions"), disable_notification=True)
    report = await check_updates(bot)
    await message.reply(report, disable_notification=True)


_COOKIE_KEYS = ("sessionid", "ds_user_id", "csrftoken", "c_user", "xs")

# TikTok hands out short-lived technical cookies next to the login ones. They
# are bound to the browser and the IP that created them and die within minutes,
# so by the time a file reaches the server they are already stale — and a stale
# one makes TikTok answer 403 Forbidden to everything, no matter how valid the
# sessionid sitting next to it is. Worse, they stop yt-dlp from solving the JS
# challenge on its own, which is what makes TikTok work without cookies at all.
# TikTok видає короткоживучі технічні cookies поряд із логін-ключами. Вони
# прив'язані до браузера та IP, де створені, і живуть хвилини — тож на сервер
# приїжджають уже протухлими, а протухлий такий ключ змушує TikTok відповідати
# 403 Forbidden на будь-який запит, хоч би який валідний sessionid лежав поруч.
# Гірше: вони не дають yt-dlp самому розв'язати JS-виклик, а саме на цьому
# тримається завантаження з TikTok узагалі без cookies.
_VOLATILE_COOKIES = frozenset((
    "msToken", "ttwid", "tt_chain_token",
    "odin_tt", "s_v_web_id", "tt_csrf_token",
))
_VOLATILE_DOMAINS = ("tiktok", "douyin")


def strip_volatile_cookies(text):
    """Drop the short-lived TikTok cookies. Returns (clean text, dropped count).

    Only TikTok domains are touched: the same names must never be stripped
    from another site's session by accident.
    """
    kept, dropped = [], 0
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 7 and parts[5] in _VOLATILE_COOKIES \
                and any(d in parts[0].lower() for d in _VOLATILE_DOMAINS):
            dropped += 1
            continue
        kept.append(line)
    return "\n".join(kept).rstrip("\n") + "\n", dropped


def parse_cookies_txt(text):
    """Validate a Netscape cookies file. Returns (ok, summary) — never logs values."""
    rows, bad = [], 0
    for line in text.splitlines():
        line = line.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            bad += 1
            continue
        rows.append((parts[0], parts[4], parts[5], parts[6]))
    if not rows:
        return False, {"reason": "empty"}
    domains, names, soonest, soonest_key = set(), set(), None, None
    now = time.time()
    for dom, exp, name, val in rows:
        if not val:
            continue
        domains.add(dom.lstrip("."))
        names.add(name)
        try:
            e = int(exp)
        except (TypeError, ValueError):
            e = 0
        # Only auth cookies matter for the lifetime: technical ones (wd, rur…)
        # expire quickly and would understate how long the session lasts.
        if e and name in _COOKIE_KEYS and (soonest is None or e < soonest):
            soonest, soonest_key = e, name
    have = [k for k in _COOKIE_KEYS if k in names]
    if not have:
        return False, {"reason": "no_auth", "domains": sorted(domains)}
    # Round UP: 23 hours left is still "1 day", not "expired".
    if soonest:
        secs = soonest - now
        days = int((secs + 86399) // 86400) if secs > 0 else 0   # 0 = expired
    else:
        days = None
    return True, {
        "count": len(rows),
        "domains": sorted(d for d in domains if "." in d)[:4],
        "auth": have,
        "days": days,
        # Which key the countdown is taken from, and when exactly it dies.
        # Without this a frozen day count looks like a bug in the panel, while
        # it is usually a key whose expiry the site simply never renews.
        # З якого ключа рахується залишок і коли саме він помре. Без цього
        # завмерла кількість днів виглядає як баг панелі, хоча зазвичай це
        # ключ, якому сайт просто не подовжує термін.
        "expires": soonest,
        "expires_key": soonest_key,
    }


# Cobalt keeps cookies in its own shape: service name -> a list of cookie
# strings. Ours live in the Netscape format yt-dlp wants, so the same session
# has to be written out twice.
# Cobalt тримає cookies у власному вигляді: назва сервісу -> список рядків із
# cookie. Наші лежать у форматі Netscape, потрібному yt-dlp, тож одну й ту саму
# сесію доводиться записувати двічі.
_COBALT_SERVICES = (
    ("instagram", (".instagram.com",)),
    ("youtube", (".youtube.com", ".google.com")),
    ("twitter", (".twitter.com", ".x.com")),
    ("reddit", (".reddit.com",)),
)


def cobalt_cookies_from(text):
    """Turn a Netscape cookies file into Cobalt's cookies.json structure."""
    jar = {}
    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        # Exporters mark httpOnly cookies with this prefix; it is not a comment.
        # Експортери позначають httpOnly-cookies саме таким префіксом — це не коментар.
        if raw.startswith("#HttpOnly_"):
            raw = raw[len("#HttpOnly_"):]
        elif raw.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) < 7:
            continue
        domain, name, value = parts[0].lower(), parts[5], parts[6]
        if not name or not value:
            continue
        for service, suffixes in _COBALT_SERVICES:
            if any(domain == s.lstrip(".") or domain.endswith(s) for s in suffixes):
                jar.setdefault(service, {})[name] = value
                break
    return {svc: ["; ".join("%s=%s" % kv for kv in pairs.items())]
            for svc, pairs in jar.items() if pairs}


def write_cobalt_cookies(text):
    """Hand the same session to Cobalt. Returns how many services were written.

    Cobalt is the fallback for exactly the sites that need a login, so without
    this it lost access at the moment it was supposed to help: yt-dlp fails on
    a private post, the bot falls back to Cobalt, and Cobalt is logged out.
    Cobalt — це запасний варіант саме для тих сайтів, де потрібен логін, тож без
    цього він втрачав доступ рівно тоді, коли мав виручити: yt-dlp падає на
    закритому пості, бот переходить на Cobalt, а Cobalt незалогінений.
    """
    if not COBALT_COOKIES_PATH:
        return 0
    try:
        jar = cobalt_cookies_from(text)
        path = Path(COBALT_COOKIES_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(jar, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:  # noqa: BLE001
            pass
        os.replace(tmp, path)
        logger.info("Cobalt cookies written for: %s", ", ".join(sorted(jar)) or "nothing")
        return len(jar)
    except Exception:  # noqa: BLE001
        logger.exception("could not write cookies for Cobalt")
        return 0


def store_cookies(text):
    """Write the cookies file with tight permissions. Returns the path."""
    path = Path(COOKIES_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:  # noqa: BLE001
        pass
    os.replace(tmp, path)
    write_cobalt_cookies(text)      # the fallback needs the same session
    return path


@dp.message(Command("id"))
async def cmd_id(message):
    if message.from_user:
        await message.reply(t("your_id", id=message.from_user.id), disable_notification=True)


TGAPI_ROOTS = ("/var/lib/telegram-bot-api", "/tgapi")


def _find_local_file(file_path):
    """Locate a file the local Bot API saved. Its file_path may be absolute or
    relative (e.g. "documents/file_5.txt"), so try every sensible shape and,
    as a last resort, search the mounted volume by name."""
    fp = (file_path or "").strip()
    if not fp:
        return None
    rel = fp.lstrip("/")
    tried = []
    if fp.startswith("/"):
        tried.append(Path(fp))
    for base in TGAPI_ROOTS:
        b = Path(base)
        tried.append(b / rel)
        if BOT_TOKEN:
            tried.append(b / BOT_TOKEN / rel)
    for p in tried:
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    # fall back to a name search across the volume (files live under <token>/…)
    name = Path(fp).name
    for base in TGAPI_ROOTS:
        b = Path(base)
        if not b.is_dir():
            continue
        try:
            for p in b.rglob(name):
                if p.is_file():
                    return p
        except OSError:
            continue
    return None


async def fetch_telegram_file(bot, file_obj):
    """Return the bytes of a Telegram file.

    With the local Bot API the file lives on the shared volume — HTTP download
    does not work there, so read it straight from disk.
    """
    if TELEGRAM_API_URL:
        try:
            info = await bot.get_file(file_obj.file_id)
            fp = getattr(info, "file_path", None) or ""
            p = _find_local_file(fp)
            if p is not None:
                return p.read_bytes()
            mounted = [b for b in TGAPI_ROOTS if Path(b).is_dir()]
            logger.warning(
                "Local Bot API file not found. file_path=%r, mounted roots=%s. "
                "Is tgapi-data mounted into the bot container?", fp, mounted or "NONE")
        except Exception:  # noqa: BLE001
            logger.exception("get_file failed")

    buf = io.BytesIO()
    await bot.download(file_obj, destination=buf)
    return buf.getvalue()


async def send_backup(bot, chat_id):
    """Online copy of the SQLite file, delivered to the admin in private.

    Returns (ok, kb). Used by the /backup command and by the panel button —
    the file always goes through Telegram, never over the Mini App tunnel.
    """
    if not Path(STATS_DB).exists():
        return False, 0
    tmp = Path(tempfile.gettempdir()) / ("sdp-backup-%s.db" % time.strftime("%Y%m%d-%H%M"))
    try:
        # Proper online backup — safe even while the bot is writing.
        def _dump():
            con = sqlite3.connect(STATS_DB, timeout=15)
            dst = sqlite3.connect(str(tmp))
            with dst:
                con.backup(dst)
            dst.close()
            con.close()
        await asyncio.to_thread(_dump)
        kb = max(1, tmp.stat().st_size // 1024)
        await bot.send_document(
            chat_id, FSInputFile(tmp, filename=tmp.name),
            caption=t("bk_ok", kb=kb), disable_notification=True)
        logger.info("Backup sent to admin (%d KB)", kb)
        return True, kb
    except Exception:  # noqa: BLE001
        logger.exception("backup failed")
        return False, 0
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def restore_backup(raw):
    """Put a backup file back in place. Returns (ok, reason).

    A backup nobody can restore is a false sense of safety: the file arrives,
    the admin files it away, and finds out on the day it is needed that there
    was never a way back. The file is checked before anything is replaced —
    a wrong one would otherwise leave the bot with no database at all.
    Бекап, який неможливо відновити, — це хибне відчуття безпеки: файл
    приходить, адмін його відкладає, а в день, коли він знадобиться,
    зʼясовується, що шляху назад не було. Файл перевіряється до того, як щось
    замінити: інакше помилковий лишив би бота взагалі без бази.
    """
    if not raw.startswith(b"SQLite format 3\x00"):
        return False, "not_sqlite"
    tmp = Path(STATS_DB + ".incoming")
    try:
        tmp.write_bytes(raw)
        probe = sqlite3.connect(str(tmp), timeout=10)
        try:
            if probe.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return False, "corrupt"
            names = {r[0] for r in probe.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"events", "settings", "access"} <= names:
                return False, "wrong_tables"
            # Table names alone are not enough. A file with the right three
            # tables but a different shape passes that check and then breaks on
            # the first insert, with the original already gone. size/thumb_id
            # are left out: migrations add those.
            # Самих назв таблиць мало. Файл із трьома правильними таблицями,
            # але іншою структурою пройшов би перевірку й зламався б на першому
            # ж записі — коли оригінала вже немає. size/thumb_id не перевіряємо:
            # їх додають міграції.
            cols = {r[1] for r in probe.execute("PRAGMA table_info(events)")}
            if not EVENT_COLUMNS <= cols:
                logger.warning("Restore refused: events is missing %s",
                               sorted(EVENT_COLUMNS - cols))
                return False, "wrong_columns"
        finally:
            probe.close()

        # Keep what is being replaced: a restore from the wrong file is
        # recoverable, a restore over the only copy is not.
        # Зберегти те, що заміщуємо: відновлення не з того файлу поправне,
        # відновлення поверх єдиної копії — ні.
        if Path(STATS_DB).exists():
            # A plain file copy is wrong here. With WAL on, recent writes live
            # in the -wal file next to the database, and this function deletes
            # that file a moment later — so a copied stats.db could arrive
            # missing everything written since the last checkpoint, which is
            # exactly the data somebody restoring would want back.
            # Проста копія файлу тут неправильна. З увімкненим WAL свіжі записи
            # лежать у файлі -wal поруч із базою, а ця функція видаляє його за
            # мить — тож скопійований stats.db міг лишитись без усього, що
            # записане після останньої контрольної точки, тобто саме без тих
            # даних, які людина й хотіла б повернути.
            keep_src = sqlite3.connect(STATS_DB, timeout=10)
            keep_dst = sqlite3.connect(STATS_DB + ".replaced")
            try:
                with keep_dst:
                    keep_src.backup(keep_dst)
            finally:
                keep_dst.close()
                keep_src.close()
        for suffix in ("-wal", "-shm"):
            Path(STATS_DB + suffix).unlink(missing_ok=True)
        os.replace(str(tmp), STATS_DB)
        db_reopen()
        con = db_conn()
        db_migrate(con)               # an older backup may predate a migration
        settings_load_sync()
        logger.info("Database restored from a backup (schema v%d)",
                    con.execute("PRAGMA user_version").fetchone()[0])
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        logger.exception("restore failed")
        return False, str(exc)[:120]
    finally:
        tmp.unlink(missing_ok=True)


@dp.message(Command("backup"))
async def cmd_backup(message, bot):
    """Send the SQLite database (stats, settings, access lists) to the admin."""
    if LITE or not _is_admin(message) or message.chat.type != ChatType.PRIVATE:
        return
    if not Path(STATS_DB).exists():
        await message.reply(t("bk_none"), disable_notification=True)
        return
    ok, _kb = await send_backup(bot, message.chat.id)
    if not ok:
        await message.reply(t("bk_fail"), disable_notification=True)


@dp.message(Command("cookies"))
async def cmd_cookies(message):
    """Show cookies status. /cookies delete removes the stored file."""
    if LITE or not _is_admin(message):
        return          # LITE keeps cookies read-only, mounted from the host
    arg = (message.text or "").split(maxsplit=1)
    path = Path(COOKIES_FILE)
    if len(arg) > 1 and arg[1].strip().lower() in ("delete", "remove", "видалити"):
        try:
            path.unlink(missing_ok=True)
            await message.reply(t("ck_deleted"), disable_notification=True)
        except Exception:  # noqa: BLE001
            await message.reply(t("ck_delete_fail"), disable_notification=True)
        return
    if not cookies_ready():
        await message.reply(t("ck_absent"), disable_notification=True)
        return
    try:
        ok, info = parse_cookies_txt(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:  # noqa: BLE001
        ok, info = False, {}
    if not ok:
        await message.reply(t("ck_absent"), disable_notification=True)
        return
    await message.reply(
        t("ck_status", n=info["count"], sites=", ".join(info["domains"]) or "?",
          auth=", ".join(info["auth"]),
          days=(info["days"] if info["days"] is not None else "?")),
        disable_notification=True,
    )


@dp.message(F.text.func(lambda s: bool(s) and (
    s.lstrip().startswith("# Netscape") or s.lstrip().startswith("# HTTP Cookie File")
    or ("\t" in s and ".instagram.com" in s) or ("\t" in s and "sessionid" in s))))
async def on_cookies_text(message):
    """Fallback: admin pastes the cookies file contents straight into the chat."""
    if LITE:
        return
    if not _is_admin(message) or message.chat.type != ChatType.PRIVATE:
        return
    text, dropped = strip_volatile_cookies(message.text or "")
    ok, info = parse_cookies_txt(text)
    if not ok:
        await message.reply(
            t("ck_no_auth") if info.get("reason") == "no_auth" else t("ck_bad"),
            disable_notification=True)
        return
    try:
        store_cookies(text)
    except Exception:  # noqa: BLE001
        logger.exception("cookies store failed")
        await message.reply(t("ck_bad"), disable_notification=True)
        return
    logger.info("Cookies updated by admin (pasted): %d entries, %d volatile dropped",
                info["count"], dropped)
    await asyncio.to_thread(set_setting_sync, "ck_state", "ok")
    await asyncio.to_thread(settings_load_sync)
    await message.reply(
        t("ck_saved", n=info["count"], sites=", ".join(info["domains"]) or "?",
          auth=", ".join(info["auth"]),
          days=(info["days"] if info["days"] is not None else "?")),
        disable_notification=True)
    try:
        await bot_delete_safe(message)
    except Exception:  # noqa: BLE001
        pass


async def bot_delete_safe(message):
    await message.bot.delete_message(message.chat.id, message.message_id)


async def handle_restore(message, bot, doc):
    """Admin sent a .db back — check it, put it in place, reload."""
    size = getattr(doc, "file_size", 0) or 0
    if size > RESTORE_MAX_BYTES:
        await message.reply(t("rs_too_big"), disable_notification=True)
        return
    try:
        raw = await fetch_telegram_file(bot, doc)
    except Exception as exc:  # noqa: BLE001
        await message.reply(t("ck_fetch_fail", err=str(exc)[:120]),
                            disable_notification=True)
        return
    ok, reason = await asyncio.to_thread(restore_backup, raw)
    if not ok:
        await message.reply(t("rs_bad", why=reason), disable_notification=True)
        return
    stats = await asyncio.to_thread(db_stats_sync)
    await message.reply(t("rs_ok", n=stats.get("total", 0)), disable_notification=True)


@dp.message(F.document)
async def on_cookies_document(message, bot):
    """Admin sends a cookies file in private -> validate, store, delete the message."""
    if LITE:
        return
    if not _is_admin(message) or message.chat.type != ChatType.PRIVATE:
        return
    doc = message.document
    name = (getattr(doc, "file_name", "") or "").lower()
    if name.endswith(".db"):
        await handle_restore(message, bot, doc)
        return
    if not (name.endswith(".txt") or "cookie" in name):
        return                      # not for us — stay silent
    if (getattr(doc, "file_size", 0) or 0) > COOKIES_MAX_BYTES:
        await message.reply(t("ck_too_big"), disable_notification=True)
        return
    try:
        raw = await fetch_telegram_file(bot, doc)
        text, dropped = strip_volatile_cookies(raw.decode("utf-8", "ignore"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("cookies download failed")
        await message.reply(t("ck_fetch_fail", err=str(exc)[:120]),
                            disable_notification=True)
        return
    if not text.strip():
        await message.reply(t("ck_bad"), disable_notification=True)
        return

    ok, info = parse_cookies_txt(text)
    if not ok:
        await message.reply(
            t("ck_no_auth") if info.get("reason") == "no_auth" else t("ck_bad"),
            disable_notification=True)
        return

    try:
        store_cookies(text)
    except Exception:  # noqa: BLE001
        logger.exception("cookies store failed")
        await message.reply(t("ck_bad"), disable_notification=True)
        return

    logger.info("Cookies updated by admin: %d entries, auth=%s, %d volatile dropped",
                info["count"], ",".join(info["auth"]), dropped)
    await asyncio.to_thread(set_setting_sync, "ck_state", "ok")
    await asyncio.to_thread(settings_load_sync)
    await message.reply(
        t("ck_saved", n=info["count"], sites=", ".join(info["domains"]) or "?",
          auth=", ".join(info["auth"]),
          days=(info["days"] if info["days"] is not None else "?")),
        disable_notification=True)
    # The file is a live credential — remove it from the chat history.
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:  # noqa: BLE001
        pass


@dp.message(Command("start", "help"))
async def cmd_start(message):
    is_private = message.chat.type == ChatType.PRIVATE
    if LITE:
        await message.reply(t("lite_private") if is_private else t("start_group"),
                            disable_notification=True)
        return
    await message.reply(
        t("start_private") if is_private else t("start_group"),
        disable_notification=True,
    )


@dp.message(F.text | F.caption)
async def handle_message(message, bot):
    text = message.text or message.caption or ""
    urls = extract_urls(text)
    if not urls:
        return

    is_group = message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    fu = message.from_user
    level = resolve_access(
        fu.id if fu else None, fu.username if fu else None, message.chat.id, is_group
    )
    if level == "none":
        if setting("notify_no_access", "0") == "1":
            await message.reply(t("no_access"))
        return

    is_extended = level in ("extended", "admin")
    text_audio = _wants_audio(text) and not LITE

    # Full YouTube videos (watch) need extended access; short-form is for everyone.
    targets = [u for u in urls if is_extended or not _needs_extended(u)]
    if not targets:
        return
    targets = await expand_targets(targets, is_extended)
    if not targets:
        return
    _cap = tunable("max_links")
    if len(targets) > _cap:
        logger.info("Capping %d links to %d in chat %s",
                    len(targets), _cap, message.chat.id)
        targets = targets[:_cap]
    trim = None if LITE else _extract_trim(text, urls)
    logger.info("Processing %d link(s) in chat %s (level=%s)", len(targets), message.chat.id, level)
    await asyncio.gather(
        *(process_url(bot, message, u, is_extended,
                      _is_youtube_music(u) or (is_extended and text_audio), trim=trim)
          for u in targets),
        return_exceptions=True,
    )


async def probe_http(url, timeout=5):
    """Is this service answering at all? Returns True/False."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                return r.status < 500
    except Exception:  # noqa: BLE001
        return False


async def self_check(bot, me):
    """Say out loud what is wired up and what silently is not.

    The one that matters most is group privacy. With it on — which is the
    default for a new bot — Telegram delivers only commands and replies, so the
    bot sits in the group seeing nothing, logs nothing, and looks broken in a
    way no error message explains. getMe knows the answer, so there is no
    reason to make anyone guess.
    Найважливіше тут — режим приватності в групах. Коли він увімкнений (а для
    нового бота це типово), Telegram віддає лише команди й відповіді: бот сидить
    у групі, нічого не бачить, нічого не пише в лог і виглядає зламаним так, що
    жодне повідомлення про помилку цього не пояснює. getMe знає відповідь, тож
    змушувати когось гадати немає причин.
    """
    lines = []

    if not getattr(me, "can_read_all_group_messages", False):
        logger.warning(
            "Group privacy is ON: in groups this bot only sees commands and "
            "replies to itself, so plain links will be ignored. Turn it off in "
            "@BotFather: /setprivacy -> pick the bot -> Disable, then remove the "
            "bot from the group and add it back.")
        lines.append("group privacy: ON (links in groups will be ignored)")
    else:
        lines.append("group privacy: off (sees all messages)")

    ff = shutil.which("ffmpeg") and shutil.which("ffprobe")
    if not ff:
        logger.error("ffmpeg/ffprobe not found — merging, thumbnails and mp3 will fail")
    lines.append("ffmpeg: %s" % ("ok" if ff else "MISSING"))

    if cobalt_on():
        ok = await probe_http(COBALT_API_URL.rstrip("/") + "/")
        if not ok:
            logger.warning("Cobalt at %s is not answering — the fallback engine is down",
                           COBALT_API_URL)
        lines.append("cobalt: %s" % ("ok" if ok else "unreachable"))

    if ENABLE_YOUTUBE and POT_PROVIDER_URL:
        ok = await probe_http(POT_PROVIDER_URL.rstrip("/") + "/ping")
        if not ok:
            logger.info("PO token provider at %s is not answering — YouTube may "
                        "start asking to confirm you are not a bot", POT_PROVIDER_URL)
        lines.append("po-token provider: %s" % ("ok" if ok else "unreachable"))

    # Say the number and where it comes from: "50 MB" with no explanation is
    # the single most common misunderstanding about this bot.
    # Назвати число й пояснити, звідки воно: «50 МБ» без пояснення — найчастіше
    # непорозуміння щодо цього бота.
    lines.append("file limit: %d MB (%s)" % (
        MAX_FILE_SIZE // 1024 // 1024,
        "local Bot API" if TELEGRAM_API_URL else "Telegram cloud limit; "
        "run the local Bot API for 2 GB"))

    logger.info("Self-check | %s", " | ".join(lines))
    return lines


def clean_orphans():
    """Remove work directories left behind by a previous run.

    A container killed mid-download leaves its temp directory behind. On a
    tmpfs that space is gone until reboot, and on a disk it accumulates
    quietly — nothing ever looked for these again.
    Контейнер, убитий посеред завантаження, лишає по собі тимчасову теку. На
    tmpfs це місце зникає до перезавантаження, на диску тихо накопичується —
    ніхто ніколи не приходив по ці теки знову.
    """
    removed = 0
    try:
        for path in Path(WORK_DIR).glob("vbot_*"):
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
    except OSError:
        return 0
    if removed:
        logger.info("Removed %d leftover work director%s from a previous run",
                    removed, "y" if removed == 1 else "ies")
    return removed


async def shutdown(bot):
    """Stop taking work, let what is running finish, then close cleanly.

    Docker sends SIGTERM and waits ten seconds. Without this the process was
    killed mid-download: the file went nowhere, the temp directory stayed, and
    a cache entry could be half-written.
    Docker надсилає SIGTERM і чекає десять секунд. Без цього процес убивало
    посеред завантаження: файл нікуди не йшов, тимчасова тека лишалась, а запис
    у кеш міг бути недописаний.
    """
    logger.info("Shutting down: no new jobs, finishing %d running one(s)",
                len(_background))
    try:
        await dp.stop_polling()
    except Exception:  # noqa: BLE001
        pass
    jobs = [t for t in _background if not t.done()]
    if jobs:
        done, pending = await asyncio.wait(jobs, timeout=SHUTDOWN_GRACE)
        for task in pending:
            task.cancel()
        if pending:
            logger.warning("%d job(s) did not finish in %ss and were cancelled",
                           len(pending), SHUTDOWN_GRACE)
    cache_flush_sync()
    try:
        await bot.session.close()
    except Exception:  # noqa: BLE001
        pass
    logger.info("Stopped cleanly")


async def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set.")

    db_init()
    settings_load_sync()
    cache_load()
    if TELEGRAM_API_URL:
        session = AiohttpSession(api=TelegramAPIServer.from_base(TELEGRAM_API_URL))
        bot = Bot(
            token=BOT_TOKEN,
            session=session,
            default=DefaultBotProperties(parse_mode=None),
        )
        logger.info(
            "Local Bot API: %s (limit %d MB)", TELEGRAM_API_URL, MAX_FILE_SIZE // 1024 // 1024
        )
    else:
        bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=None))
    bot.session.middleware(FloodMiddleware())
    logger.info("Instagram/Facebook engines: %s (cookies: %s)",
                " -> ".join(engine_order("instagram", SERVICES["instagram"])),
                "yes" if cookies_ready() else "no")
    logger.info("Flood control: %d retries, %.1fs between sends per chat",
                tunable("flood_retries"), tunable("chat_interval"))
    me = await bot.get_me()

    enabled = [
        name
        for name, on in (
            ("TikTok", ENABLE_TIKTOK),
            ("YouTube", ENABLE_YOUTUBE),
            ("Instagram", ENABLE_INSTAGRAM),
            ("Facebook", ENABLE_FACEBOOK),
        )
        if on
    ]
    platforms = ", ".join(enabled) if enabled else t("no_platforms")
    logger.info(
        "Bot @%s | platforms: %s | cobalt: %s | ladder: %s | verify: %s",
        me.username, platforms, "on" if cobalt_on() else "off",
        quality_ladder(), verify_media(),
    )
    if LITE:
        logger.info("LITE mode: groups only%s, no database, no cache, no Mini App",
                    " (%d allowed chats)" % len(ALLOWED_CHATS) if ALLOWED_CHATS else "")

    await start_web_server(bot)

    await github_baseline(bot)

    if ADMIN_ID:
        report = await check_updates(bot)
        await notify_admin(
            bot,
            t("bot_started", u=me.username, p=platforms,
              c=t("cobalt_on") if cobalt_on() else t("cobalt_off"), r=report),
        )

    check_workdir_capacity()
    clean_orphans()
    await self_check(bot, me)
    spawn(update_checker_loop(bot))
    spawn(cache_cleaner_loop())
    spawn(housekeeping_loop())

    # Docker sends SIGTERM on stop and SIGINT on Ctrl+C; both should mean
    # "finish what you started", not "die where you stand".
    # Docker надсилає SIGTERM при зупинці й SIGINT на Ctrl+C; обидва мають
    # означати «доробіть почате», а не «помріть де стоїте».
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stopping.set)
        except (NotImplementedError, RuntimeError):
            pass          # Windows has no such thing; polling just ends on Ctrl+C

    await bot.delete_webhook(drop_pending_updates=True)
    polling = spawn(dp.start_polling(bot, handle_signals=False))
    await asyncio.wait([polling, spawn(stopping.wait())],
                       return_when=asyncio.FIRST_COMPLETED)
    await shutdown(bot)


if __name__ == "__main__":
    asyncio.run(main())
