#!/bin/sh
# Run the same checks CI runs, before pushing rather than after.
# Прогнати ті самі перевірки, що й CI, — до пуша, а не після.
#
#   sh tests/run.sh          — tests only, the usual case / лише тести
#   sh tests/run.sh --all    — plus the image build and both compose files
#                              / плюс збірка образу й обидва compose
#
# Needs Python with aiogram and pyyaml. Without them it falls back to Docker,
# so nothing has to be installed on the machine itself.
# Потрібен Python з aiogram і pyyaml. Якщо їх немає — падає назад на Docker,
# тож на самій машині встановлювати нічого не треба.
set -e
cd "$(dirname "$0")/.."

run_locally() {
    python -c "import aiogram, yaml" 2>/dev/null || return 1
    echo "==> compiling"
    python -m compileall -q bot.py tests
    echo "==> tests"
    (cd tests && python -m unittest discover -s . -p "test_*.py")
}

run_in_docker() {
    command -v docker >/dev/null 2>&1 || {
        echo "Neither Python with aiogram nor Docker is available." >&2
        echo "Немає ні Python з aiogram, ні Docker." >&2
        return 2
    }
    echo "==> no local aiogram, using Docker / локального aiogram немає, беру Docker"
    docker run --rm -v "$PWD:/app" -w /app python:3.12-slim sh -c '
        pip install -q aiogram pyyaml
        python -m compileall -q bot.py tests
        cd tests && python -m unittest discover -s . -p "test_*.py"'
}

run_locally || run_in_docker

# The Mini App is a single file with one <script> block; a syntax error in it
# breaks the panel silently, so CI checks it and so does this.
# Mini App — один файл з одним блоком <script>; синтаксична помилка ламає
# панель мовчки, тож CI її перевіряє, і ця перевірка теж.
if command -v node >/dev/null 2>&1; then
    echo "==> Mini App JS"
    sed -n '/<script>/,/<\/script>/p' index.html | sed '1d;$d' > "${TMPDIR:-/tmp}/sdp-app.js"
    node --check "${TMPDIR:-/tmp}/sdp-app.js"
fi

if [ "$1" = "--all" ]; then
    echo "==> image"
    docker build -q -t sdp-downloader:local . >/dev/null
    echo "==> compose"
    BOT_TOKEN=dummy docker compose -f docker-compose.yml config --quiet
    BOT_TOKEN=dummy docker compose -f docker-compose.lite.yml config --quiet
fi

echo "OK"
