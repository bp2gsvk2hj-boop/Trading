#!/usr/bin/env bash
# =====================================================================
#  Торговый робот Тбанк на своём сервере (Ubuntu 22.04/24.04, Яндекс Облако).
#  Одна команда на сервере:
#     curl -fsSL https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/main/server/install.sh | sudo bash
#  При первой установке спросит токены (T-Invest, Telegram-бот), потом ничего не спрашивает.
#  Ставит таймеры:
#   - проход каждые 15 минут по будням 10:02–18:47 МСК;
#   - ребаланс LQDT в 18:45 МСК (запасные 19:35 и 22:35);
#   - дайджест брокерского рынка в 09:00 и 18:00 МСК (brokernews, нужен ключ OpenAI).
#  Повторный запуск обновляет установку; сохранённые рабочие токены берёт без вопросов.
#  Проверка без торговли:  systemctl start trading-robot@check; journalctl -u trading-robot@check -n 30
#  Логи:                   journalctl -u trading-robot@pass -n 100
#  Помощник трейдера:      http://<IP сервера>:8080/
#  Дайджесты рынка:        http://<IP сервера>:8080/news/ ; сейчас: systemctl start trading-robot@news
#  Остановить робота:      systemctl disable --now trading-robot-pass.timer trading-robot-collateral.timer
# =====================================================================
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Запустите от root: sudo bash install.sh"; exit 1; }
DIR=/opt/trading
ENVF=/etc/trading-robot.env
REPO=https://github.com/bp2gsvk2hj-boop/Trading.git
say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
# Everything in a function: with `curl … | bash` the whole script is read before apt-get or git
# can swallow the rest of it from stdin.
main() {

say "Пакеты (git, Python 3.11+)"
apt-get update -qq
apt-get install -y -qq git python3 curl ca-certificates >/dev/null
PY=python3
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  apt-get install -y -qq python3.11 >/dev/null
  PY=python3.11
fi
echo "Python: $($PY --version)"

say "Пользователь robot и код в $DIR"
id robot >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin robot
if [ -d "$DIR/.git" ]; then
  sudo -u robot git -C "$DIR" pull -q --ff-only origin main
else
  git clone -q "$REPO" "$DIR"
  chown -R robot:robot "$DIR"
fi

say "Токены (хранятся в $ENVF, доступ только root)"
VPNENV=/usr/local/etc/xray/vpn.env
old() { { [ -f "$ENVF" ] && sed -n "s/^$1=//p" "$ENVF"; } || true; }
vpn() { { [ -f "$VPNENV" ] && sed -n "s/^$1=//p" "$VPNENV"; } || true; }
ask() {  # ask "prompt" default: hidden input, Enter keeps the default
  local val
  read -rsp "$1${2:+ (Enter — оставить прежний)}: " val </dev/tty; echo >&2
  printf '%s' "${val:-$2}"
}
# Ask until the broker accepts the token (a pasted token often carries a space or a line break).
tcheck() {
  curl -s -o /dev/null -w '%{http_code}' -m 20 --cacert "$DIR/certs/russian_trusted_ca.pem" \
    -H "Authorization: Bearer $1" -H 'Content-Type: application/json' -d '{}' \
    https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1.UsersService/GetAccounts
}
TINV=$(old TINVEST_TOKEN)
# A saved token that still works is kept without asking: a re-run needs no input at all.
if [ -n "$TINV" ] && [ "$(tcheck "$TINV")" = 200 ]; then echo "Токен T-Invest: сохранённый, подходит"
else for try in 1 2 3; do
  TINV=$(ask "Токен T-Invest (с правом торговли)" "$TINV" | tr -d '[:space:]')
  code=$(tcheck "$TINV")
  [ "$code" = 200 ] && { echo "Токен T-Invest подошёл"; break; }
  echo "Брокер не принял токен (HTTP $code, длина ${#TINV} символов). Скопируйте его целиком и вставьте ещё раз."
  TINV=""
  [ "$try" = 3 ] && { echo "Робот НЕ включён"; exit 1; }
done; fi
# Telegram: the robot's previous settings, else the VPN monitor's bot on this server.
TGT=$(old TELEGRAM_BOT_TOKEN); TGC=$(old TELEGRAM_CHAT_ID)
[ -n "$TGT" ] || { TGT=$(vpn TG_TOKEN); TGC=$(vpn TG_CHAT); }
# The VPN's local SOCKS exit to Telegram (127.0.0.1:10810), if there is one.
TGP=""; ss -ltn 'sport = :10810' | grep -q LISTEN && TGP=socks5h://127.0.0.1:10810
TGT=$(printf '%s' "$TGT" | tr -d "\"' ")
tgok() { curl -s -m 20 ${TGP:+-x "$TGP"} "https://api.telegram.org/bot$TGT/$1" | grep -q '"ok":true'; }
# Ask until Telegram accepts the bot token (getMe); an empty answer skips notifications.
if [ -n "$TGT" ] && tgok getMe; then echo "Токен Telegram: сохранённый, подходит"
else for try in 1 2 3; do
  TGT=$(ask "Токен Telegram-бота (от @BotFather, вида 123456:ABC...)" "$TGT" | tr -d "[:space:]\"'")
  [ -n "$TGT" ] || { echo "Без Telegram: робот будет торговать без уведомлений"; break; }
  curl -s -m 20 ${TGP:+-x "$TGP"} "https://api.telegram.org/bot$TGT/getMe" | grep -q '"ok":true' \
    && { echo "Токен Telegram подошёл"; break; }
  echo "Telegram не принял токен бота. Возьмите его в @BotFather: /mybots → бот → API Token."
  TGT=""
done; fi
# chat_id: check that the bot can write there; otherwise take it from the last message to the bot.
tgsend() { curl -s -m 20 ${TGP:+-x "$TGP"} "https://api.telegram.org/bot$TGT/sendMessage" \
  --data-urlencode "chat_id=$1" --data-urlencode "text=Торговый робот: проверка связи" | grep -q '"ok":true'; }
TGC=$(printf '%s' "$TGC" | tr -d "[:space:]\"'")
if [ -n "$TGT" ] && [ -n "$TGC" ] && tgok "getChat?chat_id=$TGC"; then echo "Telegram chat_id: сохранённый, подходит"
elif [ -n "$TGT" ]; then
  for try in 1 2 3; do
    read -rp "Откройте бота в Telegram, нажмите «Старт» или напишите ему любое сообщение, затем нажмите Enter здесь " _ </dev/tty
    TGC=$(curl -s -m 20 ${TGP:+-x "$TGP"} "https://api.telegram.org/bot$TGT/getUpdates" | python3 -c '
import json, sys
r = json.load(sys.stdin).get("result", [])
ids = [(u.get("message") or u.get("my_chat_member") or {}).get("chat", {}).get("id") for u in r]
ids = [i for i in ids if i]
print(ids[-1] if ids else "")' 2>/dev/null || true)
    [ -n "$TGC" ] && tgsend "$TGC" && { echo "chat_id найден: $TGC"; break; }
    echo "Бот пока не видит ваших сообщений."
    TGC=""
  done
fi
# OpenAI for the broker market digest: a saved key that works is kept; Enter skips (digest as a list of links).
# api.openai.com is closed from Russia, so the check goes through the VPN exit.
OAI=$(old OPENAI_API_KEY)
oaiok() { [ "$(curl -s -o /dev/null -w '%{http_code}' -m 25 -x "${TGP:-socks5h://127.0.0.1:10810}" \
  -H "Authorization: Bearer $1" https://api.openai.com/v1/models)" = 200 ]; }
if [ -n "$OAI" ] && oaiok "$OAI"; then echo "Ключ OpenAI: сохранённый, подходит"
else for try in 1 2 3; do
  OAI=$(ask "Ключ OpenAI для дайджеста брокерского рынка (sk-..., Enter — без ChatGPT)" "$OAI" | tr -d '[:space:]')
  [ -n "$OAI" ] || { echo "Без OpenAI: дайджест придёт списком ссылок без сводки"; break; }
  oaiok "$OAI" && { echo "Ключ OpenAI подошёл"; break; }
  echo "OpenAI не принял ключ (или api.openai.com недоступен через VPN)."
  OAI=""
done; fi
[ -n "$TINV" ] || { echo "Без токена T-Invest робот работать не может"; exit 1; }
umask 077
cat > "$ENVF" <<EOT
TINVEST_TOKEN=$TINV
TELEGRAM_BOT_TOKEN=$TGT
TELEGRAM_CHAT_ID=$TGC
TELEGRAM_PROXY=$TGP
OPENAI_API_KEY=$OAI
TRADER_WEB=/var/www/trader
ROBOT_LIVE=yes
PYTHON=$PY
EOT
umask 022

say "Помощник трейдера: http://<IP сервера>:8080/"
apt-get install -y -qq nginx >/dev/null
# the stock site listens on port 80 and is not needed here
rm -f /etc/nginx/sites-enabled/default /etc/nginx/trader.htpasswd
install -d -o robot -g robot /var/www/trader /var/www/trader/news
cp "$DIR/trader/index.html" /var/www/trader/index.html && chown robot:robot /var/www/trader/index.html
cat > /etc/nginx/sites-available/trader <<'EOT'
server {
    listen 8080;
    root /var/www/trader;
    index index.html;
    location = /data.json { add_header Cache-Control "no-store"; }
    location = /news/data.json { add_header Cache-Control "no-store"; }
}
EOT
ln -sf /etc/nginx/sites-available/trader /etc/nginx/sites-enabled/trader
nginx -t -q && systemctl enable -q nginx && systemctl reload-or-restart nginx
# the VPN setup turns on ufw with only its own ports open
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow 8080/tcp >/dev/null; fi
# first data right away (also rebuilt after every robot pass)
systemd-run --wait --quiet -p User=robot -p EnvironmentFile="$ENVF" -p WorkingDirectory="$DIR" \
  "${PY}" trader/build_data.py --out /var/www/trader/data.json \
  && echo "Данные помощника собраны" || echo "[warn] данные помощника пока не собраны — соберутся после прохода робота"

say "Таймеры systemd"
cat > /etc/systemd/system/trading-robot@.service <<EOT
[Unit]
Description=Торговый робот Тбанк (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=robot
WorkingDirectory=$DIR
EnvironmentFile=$ENVF
ExecStart=$DIR/server/pass.sh %i
TimeoutStartSec=600
EOT
cat > /etc/systemd/system/trading-robot-pass.timer <<'EOT'
[Unit]
Description=Торговый робот: проход каждые 15 минут (будни 10:02-18:47 МСК)

[Timer]
OnCalendar=Mon..Fri *-*-* 10..18:02/15:00 Europe/Moscow
Unit=trading-robot@pass.service
AccuracySec=10s

[Install]
WantedBy=timers.target
EOT
cat > /etc/systemd/system/trading-robot-collateral.timer <<'EOT'
[Unit]
Description=Торговый робот: ребаланс LQDT (будни 18:45, запасные 19:35 и 22:35 МСК)

[Timer]
OnCalendar=Mon..Fri *-*-* 18:45:00 Europe/Moscow
OnCalendar=Mon..Fri *-*-* 19,22:35:00 Europe/Moscow
Unit=trading-robot@collateral.service
AccuracySec=10s

[Install]
WantedBy=timers.target
EOT
cat > /etc/systemd/system/trading-robot-trader.timer <<'EOT'
[Unit]
Description=Помощник трейдера: данные после вечернего клиринга (00:23 МСК)

[Timer]
OnCalendar=Tue..Sat *-*-* 00:23:00 Europe/Moscow
Unit=trading-robot@trader.service

[Install]
WantedBy=timers.target
EOT
cat > /etc/systemd/system/trading-robot-news.timer <<'EOT'
[Unit]
Description=Дайджест брокерского рынка (09:00 и 18:00 МСК)

[Timer]
OnCalendar=*-*-* 09:00:00 Europe/Moscow
OnCalendar=*-*-* 18:00:00 Europe/Moscow
Unit=trading-robot@news.service
Persistent=true

[Install]
WantedBy=timers.target
EOT
systemctl daemon-reload

say "Проверка связи с брокером и Telegram (без торговли)"
if systemctl start trading-robot@check.service; then
  journalctl -u trading-robot@check -n 30 --no-pager -o cat
  systemctl enable --now trading-robot-pass.timer trading-robot-collateral.timer trading-robot-trader.timer \
    trading-robot-news.timer
  # first digest in the background: it arrives in Telegram in a few minutes
  [ -f "$DIR/state/brokernews.db" ] || systemctl start --no-block trading-robot@news.service
  echo "Готово, робот включён. Дайджест рынка придёт в Telegram через несколько минут. Ближайшие запуски:"
  systemctl list-timers 'trading-robot-*' --no-pager
else
  journalctl -u trading-robot@check -n 30 --no-pager -o cat
  echo "Проверка не прошла — робот НЕ включён. Исправьте ошибку выше и запустите install.sh ещё раз."
fi
}
main "$@"
