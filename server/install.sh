#!/usr/bin/env bash
# =====================================================================
#  Торговый робот Тбанк на своём сервере (Ubuntu 22.04/24.04, Яндекс Облако).
#  Одна команда на сервере:
#     curl -fsSL https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/main/server/install.sh | sudo bash
#  Спросит токены (T-Invest, Telegram-бот, chat_id) и поставит таймеры:
#   - проход каждые 15 минут по будням 10:02–18:47 МСК;
#   - ребаланс LQDT в 19:35 и 22:35 МСК.
#  Повторный запуск обновляет установку; токены можно оставить прежними (Enter).
#  Проверка без торговли:  systemctl start trading-robot@check; journalctl -u trading-robot@check -n 30
#  Логи:                   journalctl -u trading-robot@pass -n 100
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
for try in 1 2 3; do
  TINV=$(ask "Токен T-Invest (с правом торговли)" "$TINV" | tr -d '[:space:]')
  code=$(tcheck "$TINV")
  [ "$code" = 200 ] && { echo "Токен T-Invest подошёл"; break; }
  echo "Брокер не принял токен (HTTP $code, длина ${#TINV} символов). Скопируйте его целиком и вставьте ещё раз."
  TINV=""
  [ "$try" = 3 ] && { echo "Робот НЕ включён"; exit 1; }
done
# Telegram: the robot's previous settings, else the VPN monitor's bot on this server.
TGT=$(old TELEGRAM_BOT_TOKEN); TGC=$(old TELEGRAM_CHAT_ID)
[ -n "$TGT" ] || { TGT=$(vpn TG_TOKEN); TGC=$(vpn TG_CHAT); }
# The VPN's local SOCKS exit to Telegram (127.0.0.1:10810), if there is one.
TGP=""; ss -ltn 'sport = :10810' | grep -q LISTEN && TGP=socks5h://127.0.0.1:10810
TGT=$(printf '%s' "$TGT" | tr -d "\"' ")
# Ask until Telegram accepts the bot token (getMe); an empty answer skips notifications.
for try in 1 2 3; do
  TGT=$(ask "Токен Telegram-бота (от @BotFather, вида 123456:ABC...)" "$TGT" | tr -d "[:space:]\"'")
  [ -n "$TGT" ] || { echo "Без Telegram: робот будет торговать без уведомлений"; break; }
  curl -s -m 20 ${TGP:+-x "$TGP"} "https://api.telegram.org/bot$TGT/getMe" | grep -q '"ok":true' \
    && { echo "Токен Telegram подошёл"; break; }
  echo "Telegram не принял токен бота. Возьмите его в @BotFather: /mybots → бот → API Token."
  TGT=""
done
TGC=$(ask "Telegram chat_id (ваш Id из @userinfobot)" "$TGC" | tr -d "[:space:]\"'")
[ -n "$TINV" ] || { echo "Без токена T-Invest робот работать не может"; exit 1; }
umask 077
cat > "$ENVF" <<EOT
TINVEST_TOKEN=$TINV
TELEGRAM_BOT_TOKEN=$TGT
TELEGRAM_CHAT_ID=$TGC
TELEGRAM_PROXY=$TGP
ROBOT_LIVE=yes
PYTHON=$PY
EOT
umask 022

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
Description=Торговый робот: ребаланс LQDT (будни 19:35 и 22:35 МСК)

[Timer]
OnCalendar=Mon..Fri *-*-* 19,22:35:00 Europe/Moscow
Unit=trading-robot@collateral.service
AccuracySec=10s

[Install]
WantedBy=timers.target
EOT
systemctl daemon-reload

say "Проверка связи с брокером и Telegram (без торговли)"
if systemctl start trading-robot@check.service; then
  journalctl -u trading-robot@check -n 30 --no-pager -o cat
  systemctl enable --now trading-robot-pass.timer trading-robot-collateral.timer
  echo "Готово, робот включён. Ближайшие запуски:"
  systemctl list-timers 'trading-robot-*' --no-pager
else
  journalctl -u trading-robot@check -n 30 --no-pager -o cat
  echo "Проверка не прошла — робот НЕ включён. Исправьте ошибку выше и запустите install.sh ещё раз."
fi
}
main "$@"
