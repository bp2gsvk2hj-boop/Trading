#!/usr/bin/env bash
# =====================================================================
#  Торговый робот Тбанк на своём сервере (Ubuntu 22.04/24.04, Яндекс Облако).
#  Запуск от root:
#     curl -fsSL https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/main/server/install.sh -o install.sh
#     sudo bash install.sh
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

say "Пакеты (git, Python 3.11+)"
apt-get update -qq
apt-get install -y -qq git python3 ca-certificates >/dev/null
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
old() { [ -f "$ENVF" ] && sed -n "s/^$1=//p" "$ENVF" || true; }
ask() {  # ask NAME "prompt": hidden input, Enter keeps the old value
  local cur val; cur=$(old "$1")
  read -rsp "$2${cur:+ (Enter — оставить прежний)}: " val </dev/tty; echo
  printf '%s' "${val:-$cur}"
}
TINV=$(ask TINVEST_TOKEN "Токен T-Invest (с правом торговли)")
TGT=$(ask TELEGRAM_BOT_TOKEN "Токен Telegram-бота")
TGC=$(ask TELEGRAM_CHAT_ID "Telegram chat_id")
[ -n "$TINV" ] || { echo "Без токена T-Invest робот работать не может"; exit 1; }
umask 077
cat > "$ENVF" <<EOT
TINVEST_TOKEN=$TINV
TELEGRAM_BOT_TOKEN=$TGT
TELEGRAM_CHAT_ID=$TGC
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
