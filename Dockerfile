FROM python:3.12-slim

# Deno — the JS runtime yt-dlp needs to get past YouTube protection (nsig/challenge).
# Deno — JS-рантайм, потрібен yt-dlp для обходу YouTube-захисту (nsig/challenge).
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

# ffmpeg (+ ffprobe) is required for merging, thumbnails and mp3.
# gosu drops privileges in the entrypoint — see below for why that matters.
# ffmpeg (+ ffprobe) потрібен для склейки, мініатюр і mp3.
# gosu знижує привілеї в entrypoint — навіщо саме, пояснено нижче.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py entrypoint.sh index.html ./
RUN chmod +x entrypoint.sh

# The bot runs yt-dlp against links handed to it by strangers, so the process
# that does it should not be root. The container still STARTS as root — it has
# to fix ownership of an already existing /data volume — and the entrypoint
# immediately re-executes itself as this user.
# Бот запускає yt-dlp по посиланнях, які йому дають сторонні люди, тож процес,
# що це робить, не має бути root. Контейнер усе одно СТАРТУЄ під root — треба
# полагодити права на вже наявному томі /data — а далі entrypoint одразу
# перезапускає себе під цим користувачем.
RUN useradd --create-home --shell /bin/sh --uid 10001 sdp \
    && mkdir -p /data \
    && chown -R sdp:sdp /data /app

# Local bin in PATH: the yt-dlp self-upgrade installs into the user's ~/.local.
# Локальний bin у PATH: самооновлення yt-dlp ставить пакет у ~/.local користувача.
ENV PATH=/home/sdp/.local/bin:$PATH

ENTRYPOINT ["./entrypoint.sh"]
