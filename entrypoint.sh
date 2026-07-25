#!/bin/sh
set -e

# With AUTO_UPGRADE_YTDLP=1 a fresh yt-dlp is pulled on every container start
# (installed into ~/.local, which takes priority in PATH). That way yt-dlp can
# be updated by simply restarting the container, without rebuilding the image.
# Якщо AUTO_UPGRADE_YTDLP=1 — при кожному старті контейнера тягнемо свіжий yt-dlp
# (встановлюється у ~/.local і має пріоритет у PATH). Так можна оновлювати
# yt-dlp простим перезапуском контейнера, без повної перезбірки образу.
#
# Channels use the officially documented install methods. The old
# "…/releases/latest/download/yt_dlp-0.0.0-py3-none-any.whl" links are dead:
# the build repos stopped publishing that placeholder asset name and every
# upgrade silently 404'd.
# Канали беруться офіційно задокументованими способами. Старі посилання на
# "…/releases/latest/download/yt_dlp-0.0.0-py3-none-any.whl" більше не працюють:
# збіркові репозиторії перестали публікувати ассет із такою назвою, і кожне
# оновлення тихо падало з 404.
if [ "$AUTO_UPGRADE_YTDLP" = "1" ]; then
    CHAN="${YTDLP_CHANNEL:-stable}"
    case "$CHAN" in
        # nightly builds are published to PyPI as pre-releases
        # nightly-збірки публікуються на PyPI як pre-release
        nightly) set -- --pre "yt-dlp[default]" ;;
        # master: source tarball, no git needed in the image
        # master: тарбол з вихідним кодом, git в образі не потрібен
        master)  set -- "yt-dlp[default] @ https://github.com/yt-dlp/yt-dlp/archive/refs/heads/master.tar.gz" ;;
        *)       set -- "yt-dlp[default]" ;;
    esac

    echo "[entrypoint] Upgrading yt-dlp (channel: $CHAN) / Оновлюю yt-dlp (канал: $CHAN)…"
    if pip install --user --upgrade --no-cache-dir "$@"; then
        echo "[entrypoint] yt-dlp $(yt-dlp --version 2>/dev/null || echo '?')"
    else
        # Loud on purpose: a silent failure here means broken extractors stay
        # broken until someone reads the log.
        # Навмисно голосно: тиха помилка тут означає, що зламані екстрактори
        # лишаться зламаними, поки хтось не загляне в лог.
        echo "[entrypoint] !! Upgrade FAILED — running the version baked into the image:"
        echo "[entrypoint] !! Оновлення НЕ ВДАЛОСЬ — працюю з версією з образу:"
        echo "[entrypoint] !! yt-dlp $(yt-dlp --version 2>/dev/null || echo '?')"
    fi
fi

exec python -u bot.py
