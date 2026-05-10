#!/usr/bin/env bash
# Обновить репозиторий и один раз запустить парсер.
# В cron, например каждые 30 минут (подставьте свой путь к клону):
#   */30 * * * * /opt/parserIT/deploy/update-and-run.sh >>/opt/parserIT/cron.log 2>&1
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -d .git ]]; then
  git pull --ff-only
else
  echo "Нет каталога .git в $ROOT — ожидается git clone, а не просто копия файлов." >&2
  exit 1
fi

if [[ -f requirements.txt ]]; then
  python3 -m pip install -q -r requirements.txt
fi

exec python3 "$ROOT/it_parser.py"
