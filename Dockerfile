FROM python:3.12-slim

# Deno — the JS runtime yt-dlp needs to get past YouTube protection (nsig/challenge).
# Deno — JS-рантайм, потрібен yt-dlp для обходу YouTube-захисту (nsig/challenge).
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

# ffmpeg (+ ffprobe) is required for merging, thumbnails and mp3.
# ffmpeg (+ ffprobe) потрібен для склейки, мініатюр і mp3.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py entrypoint.sh index.html ./
RUN chmod +x entrypoint.sh

# Make sure the data directory exists and is writable (SQLite / cache).
# Гарантуємо, що тека даних існує й доступна для запису (SQLite / кеш).
RUN mkdir -p /data
# Local bin in PATH (for the yt-dlp self-upgrade). The container runs as root
# so it can write into the mounted /data volume.
# Локальний bin у PATH (для авто-апгрейду yt-dlp). Контейнер працює під root,
# щоб мати право писати у примонтований volume /data.
ENV PATH=/root/.local/bin:$PATH

ENTRYPOINT ["./entrypoint.sh"]
