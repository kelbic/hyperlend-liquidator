#!/bin/bash
# Deadman для hyperlend executor (адаптация ~/.wc-bot/deadman.sh): если executor.log молчит
# >600с — бот мёртв/завис (cron-watchdog его не поднял) -> TG-алерт, максимум 1/час (штамп),
# штамп снимается при восстановлении. Cron-строку добавляет оператор, не этот скрипт.
LOG=/home/claude-agent/.hyperlend-bot/executor.log
STAMP=/home/claude-agent/.hyperlend-bot/.deadman_alerted
[ -f "$LOG" ] || exit 0
age=$(( $(date +%s) - $(stat -c %Y "$LOG") ))
if [ "$age" -gt 600 ]; then
  [ -f "$STAMP" ] && [ $(( $(date +%s) - $(stat -c %Y "$STAMP") )) -lt 3600 ] && exit 0
  token=$(grep '^TELEGRAM_BOT_TOKEN=' /home/claude-agent/.claude/channels/telegram/.env 2>/dev/null | cut -d= -f2-)
  chat=$(grep '^HL_CHAT_ID=' /home/claude-agent/.hyperlend-bot/env | head -1 | cut -d= -f2- | awk '{print $1}')
  [ -n "$token" ] && [ -n "$chat" ] && curl -sm 10 "https://api.telegram.org/bot$token/sendMessage" \
    --data-urlencode "chat_id=$chat" --data-urlencode "text=💀 hyperlend executor: лог молчит ${age}s — бот мёртв/завис" > /dev/null
  touch "$STAMP"
else
  rm -f "$STAMP"
fi
