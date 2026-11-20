#!/usr/bin/env bash
# Дамп базы бота в backups/ рядом с проектом. Хранит последние 14 дней.
# Запуск по cron: 30 3 * * * /opt/ml-telegram/scripts/backup.sh >> /opt/ml-telegram/backups/backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
file="backups/mlbot-$(date +%F).sql.gz"
docker compose exec -T db pg_dump -U mlbot mlbot | gzip > "$file.tmp"
# пустой или обрезанный дамп не должен затирать вчерашний
if [ "$(gzip -dc "$file.tmp" | tail -c 200 | grep -c 'PostgreSQL database dump complete')" -ne 1 ]; then
  echo "$(date -Is) дамп неполный, оставляю прежние файлы" >&2
  rm -f "$file.tmp"
  exit 1
fi
mv "$file.tmp" "$file"
find backups -name 'mlbot-*.sql.gz' -mtime +14 -delete
echo "$(date -Is) ok: $file ($(du -h "$file" | cut -f1))"
