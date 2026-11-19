#!/usr/bin/env bash
# Управление мониторингом брокерского рынка. Без аргументов — справка.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || exec sudo "$0" "$@"
HOMED=/var/lib/broker-monitor
APP=/opt/trading/broker-monitor
CFG=$HOMED/config.yaml
TIMER=/etc/systemd/system/broker-monitor.timer
py() { sudo -u brokermon env BM_HOME="$HOMED" "$HOMED/venv/bin/python" "$APP/monitor.py" "$@"; }

case "${1:-help}" in
  now)      py run ${2:+--hours "$2"} ;;
  test)     py run --hours "${2:-24}" --dry-run ;;
  logs)     journalctl -u broker-monitor -n "${2:-80}" --no-pager -o cat ;;
  config)   nano "$CFG" ;;
  channels) py channels ;;
  add)      shift
            [ $# -gt 0 ] || { echo "Пример: broker-monitor add banksta @frank_media https://t.me/markettwits"; exit 1; }
            for c in "$@"; do
              c=${c##*/}; c=${c#@}
              grep -qE "^\s+- $c(\s|$)" "$CFG" && { echo "@$c уже есть"; continue; }
              sed -i "/^  channels:/a\    - $c" "$CFG" && echo "Добавлен @$c"
            done
            py channels ;;
  time)     shift
            [ $# -gt 0 ] || { echo "Пример: broker-monitor time 08:30 13:00 19:00"; exit 1; }
            { echo "[Unit]"; echo "Description=Мониторинг брокерского рынка: расписание"; echo
              echo "[Timer]"
              for t in "$@"; do echo "OnCalendar=*-*-* $t:00 Europe/Moscow"; done
              echo "Persistent=true"; echo; echo "[Install]"; echo "WantedBy=timers.target"; } > "$TIMER"
            systemctl daemon-reload && systemctl restart broker-monitor.timer
            systemctl list-timers broker-monitor.timer --no-pager ;;
  tg-login) echo "Вход в Telegram нужен, чтобы читать ВСЕ каналы, на которые вы подписаны."
            echo "Получите api_id и api_hash: https://my.telegram.org → API development tools."
            read -rp "api_id: " ID; read -rp "api_hash: " HASH
            sed -i '/^TG_API_ID=/d;/^TG_API_HASH=/d' "$HOMED/.env"
            printf 'TG_API_ID=%s\nTG_API_HASH=%s\n' "$ID" "$HASH" >> "$HOMED/.env"
            py login ;;
  off)      systemctl disable --now broker-monitor.timer && echo "Рассылка выключена" ;;
  on)       systemctl enable --now broker-monitor.timer && systemctl list-timers broker-monitor.timer --no-pager ;;
  update)   curl -fsSL https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/main/broker-monitor/install.sh | bash ;;
  status)   systemctl list-timers broker-monitor.timer --no-pager; journalctl -u broker-monitor -n 5 --no-pager -o cat ;;
  *) cat <<'EOT'
broker-monitor — мониторинг брокерского рынка
  now [часы]       прислать дайджест сейчас (по умолчанию — с прошлого раза)
  test [часы]      показать дайджест здесь, без отправки
  add канал ...    добавить Telegram-каналы (имя, @имя или ссылка t.me)
  channels         проверить, какие каналы читаются
  config           ключевые слова, источники, инструкция для ChatGPT
  time 09:00 18:00 изменить время рассылки (МСК)
  tg-login         войти в Telegram, чтобы читать все свои подписки
  status | logs    ближайший запуск и журнал
  on | off         включить / выключить рассылку
  update           обновить из GitHub
EOT
  ;;
esac
