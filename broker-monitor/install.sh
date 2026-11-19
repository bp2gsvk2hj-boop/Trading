#!/usr/bin/env bash
# =====================================================================
#  Мониторинг брокерского рынка (Сбер + рынок брокерских услуг РФ).
#  Telegram-каналы + новости СМИ -> сводка ChatGPT -> дайджест в Telegram.
#  Одна команда на сервере:
#     curl -fsSL https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/main/broker-monitor/install.sh | sudo bash
#  Сам находит на сервере: выход VPN (127.0.0.1:10810) и Telegram-бота
#  (торгового робота или VPN-мониторинга). Спросит только ключ OpenAI.
#  Повторный запуск обновляет код; настройки и ключи сохраняются.
#  Управление после установки: sudo broker-monitor
# =====================================================================
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Запустите от root: curl ... | sudo bash"; exit 1; }
DIR=/opt/trading
APP=$DIR/broker-monitor
HOMED=/var/lib/broker-monitor
ENVF=$HOMED/.env
USR=brokermon
REPO=https://github.com/bp2gsvk2hj-boop/Trading.git
say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
# Всё в функции: при `curl … | bash` скрипт читается целиком до запуска apt-get.
main() {

say "Пакеты"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 python3-venv curl ca-certificates nano >/dev/null

say "Код в $DIR"
if [ -d "$DIR/.git" ]; then
  OWNER=$(stat -c %U "$DIR")
  sudo -u "$OWNER" git -C "$DIR" pull -q --ff-only origin main || git -C "$DIR" pull -q --ff-only origin main
else
  git clone -q "$REPO" "$DIR"
fi
id "$USR" >/dev/null 2>&1 || useradd --system --home "$HOMED" --shell /usr/sbin/nologin "$USR"
install -d -o "$USR" -g "$USR" -m 750 "$HOMED"
[ -f "$HOMED/config.yaml" ] || install -o "$USR" -g "$USR" -m 640 "$APP/config.yaml" "$HOMED/config.yaml"

say "Python-окружение"
[ -x "$HOMED/venv/bin/python" ] || python3 -m venv "$HOMED/venv"
"$HOMED/venv/bin/pip" install -q --upgrade pip
"$HOMED/venv/bin/pip" install -q -r "$APP/requirements.txt"
chown -R "$USR:$USR" "$HOMED"

say "Настройки (хранятся в $ENVF)"
old() { { [ -f "$ENVF" ] && sed -n "s/^$1=//p" "$ENVF"; } || true; }
from() { { [ -f "$1" ] && sed -n "s/^$2=//p" "$1"; } || true; }
ask() { local v; read -rsp "$1: " v </dev/tty; echo >&2; printf '%s' "$v"; }

# Выход VPN за рубеж: российские сайты Xray отправит напрямую, остальное — через зарубежный сервер
PROXY=$(old PROXY)
if [ -z "$PROXY" ] && ss -ltn 'sport = :10810' | grep -q LISTEN; then PROXY=socks5h://127.0.0.1:10810; fi
[ -n "$PROXY" ] && echo "Выход в интернет через VPN: $PROXY" || echo "VPN-выход 127.0.0.1:10810 не найден — работаю напрямую"
cx() { curl -s -m 25 ${PROXY:+-x "$PROXY"} "$@"; }

# Telegram-бот: прежний, иначе бот торгового робота, иначе бот VPN-мониторинга
TGT=$(old BOT_TOKEN); TGC=$(old BOT_CHAT_ID)
[ -n "$TGT" ] || { TGT=$(from /etc/trading-robot.env TELEGRAM_BOT_TOKEN); TGC=$(from /etc/trading-robot.env TELEGRAM_CHAT_ID); }
[ -n "$TGT" ] || { TGT=$(from /usr/local/etc/xray/vpn.env TG_TOKEN); TGC=$(from /usr/local/etc/xray/vpn.env TG_CHAT); }
TGT=$(printf '%s' "$TGT" | tr -d "[:space:]\"'"); TGC=$(printf '%s' "$TGC" | tr -d "[:space:]\"'")
botname() { cx "https://api.telegram.org/bot$1/getMe" | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r["result"]["username"] if r.get("ok") else "")' 2>/dev/null || true; }
BN=""; [ -n "$TGT" ] && BN=$(botname "$TGT")
if [ -n "$BN" ]; then echo "Дайджест будет приходить от бота @$BN"
else for try in 1 2 3; do
  TGT=$(ask "Токен Telegram-бота (создайте в @BotFather: /newbot)" | tr -d "[:space:]\"'")
  BN=$(botname "$TGT"); [ -n "$BN" ] && { echo "Бот @$BN подошёл"; break; }
  echo "Telegram не принял токен, попробуйте ещё раз."; TGT=""
  [ "$try" = 3 ] && { echo "Без бота дайджест некуда отправлять"; exit 1; }
done; TGC=""; fi
if [ -z "$TGC" ] || ! cx "https://api.telegram.org/bot$TGT/getChat?chat_id=$TGC" | grep -q '"ok":true'; then
  for try in 1 2 3; do
    read -rp "Откройте @$BN в Telegram, нажмите «Старт», затем Enter здесь " _ </dev/tty
    TGC=$(cx "https://api.telegram.org/bot$TGT/getUpdates" | python3 -c '
import json, sys
r = json.load(sys.stdin).get("result", [])
ids = [(u.get("message") or u.get("my_chat_member") or {}).get("chat", {}).get("id") for u in r]
ids = [i for i in ids if i]
print(ids[-1] if ids else "")' 2>/dev/null || true)
    [ -n "$TGC" ] && { echo "chat_id: $TGC"; break; }
    echo "Бот пока не видит сообщений."
  done
  [ -n "$TGC" ] || { echo "Не получил chat_id — запустите установку ещё раз"; exit 1; }
fi

# OpenAI: прежний ключ, если работает; иначе спросить
OAI=$(old OPENAI_API_KEY)
oaiok() { [ "$(cx -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $1" https://api.openai.com/v1/models)" = 200 ]; }
if [ -n "$OAI" ] && oaiok "$OAI"; then echo "Ключ OpenAI: сохранённый, подходит"
else for try in 1 2 3; do
  OAI=$(ask "Ключ OpenAI API (sk-..., Enter — без сводки, только список ссылок)" | tr -d '[:space:]')
  [ -n "$OAI" ] || { echo "Без OpenAI: дайджест будет списком ссылок"; break; }
  oaiok "$OAI" && { echo "Ключ OpenAI подошёл"; break; }
  echo "OpenAI не принял ключ (или нет доступа к api.openai.com через VPN)."; OAI=""
done; fi
LLM=none; [ -n "$OAI" ] && LLM=openai

# Прежние необязательные настройки (вход в Telegram, модель) сохраняем
KEEP=$( { [ -f "$ENVF" ] && grep -E '^(TG_API_ID|TG_API_HASH|LLM_MODEL|LLM_PROXY|OPENAI_BASE_URL)=' "$ENVF"; } || true)
umask 077
cat > "$ENVF" <<EOT
PROXY=$PROXY
BOT_TOKEN=$TGT
BOT_CHAT_ID=$TGC
LLM_PROVIDER=$LLM
OPENAI_API_KEY=$OAI
$KEEP
EOT
umask 022
chown "$USR:$USR" "$ENVF"; chmod 600 "$ENVF"

say "Команда управления: broker-monitor"
install -m 755 "$APP/broker-monitor.sh" /usr/local/bin/broker-monitor

say "Расписание: 09:00 и 18:00 МСК"
cat > /etc/systemd/system/broker-monitor.service <<EOT
[Unit]
Description=Мониторинг брокерского рынка: дайджест
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$USR
Environment=BM_HOME=$HOMED
WorkingDirectory=$HOMED
ExecStart=$HOMED/venv/bin/python $APP/monitor.py run
TimeoutStartSec=1800
EOT
if [ ! -f /etc/systemd/system/broker-monitor.timer ]; then
cat > /etc/systemd/system/broker-monitor.timer <<'EOT'
[Unit]
Description=Мониторинг брокерского рынка: расписание

[Timer]
OnCalendar=*-*-* 09:00:00 Europe/Moscow
OnCalendar=*-*-* 18:00:00 Europe/Moscow
Persistent=true

[Install]
WantedBy=timers.target
EOT
fi
systemctl daemon-reload
systemctl enable --now broker-monitor.timer >/dev/null

say "Проверка Telegram-каналов"
broker-monitor channels || true

say "Пробный дайджест за сутки — придёт в Telegram"
if broker-monitor now 24; then
  echo; echo "Готово. Дайджест будет приходить в 09:00 и 18:00 МСК. Управление: sudo broker-monitor"
else
  echo "Пробный запуск не удался — смотрите сообщения выше. Повторить: sudo broker-monitor now 24"
fi
}
main "$@"
