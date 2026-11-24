#!/usr/bin/env bash
# =====================================================================
#  Переезд с Яндекса на новый российский сервер (Selectel).
#  Запускать на ЯНДЕКСЕ от root, вечером или в выходной (вне торговой сессии):
#     bash migrate.sh 135.106.176.21
#  VPN на новом сервере уже стоит (восстановлен из копии). Скрипт переносит:
#   1) торгового робота: выключает таймеры здесь, копирует токены и состояние,
#      ставит робота там (без веб-сайта) — робот ни на минуту не работает дважды;
#   2) страницу мониторинга VPN и данные «Помощника трейдера» (тот же gist);
#  Яндекс остаётся запасным VPN-сервером, пока вы не удалите его сами.
#  В конце печатает новую ссылку для Happ и что поменять на Keenetic.
# =====================================================================
set -uo pipefail
NEW="${1:-}"; [ -n "$NEW" ] || { echo "Укажите IP нового сервера: bash migrate.sh IP"; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "Запустите от root (sudo -i)"; exit 1; }
RAW=https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/main
SSH="ssh -o BatchMode=yes -o ConnectTimeout=10 root@$NEW"
say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m[x] %s\033[0m\n' "$*"; exit 1; }

say "Проверяю новый сервер"
$SSH true || die "Нет входа по SSH на $NEW"
$SSH 'systemctl is-active -q xray' || die "На $NEW не работает VPN (xray) — сначала восстановите его из копии"
[ -f /etc/trading-robot.env ] || die "Здесь нет /etc/trading-robot.env — робот не установлен?"

say "1/4 Останавливаю робота здесь"
TIMERS="trading-robot-pass.timer trading-robot-collateral.timer trading-robot-trader.timer trading-robot-news.timer"
systemctl disable --now $TIMERS >/dev/null 2>&1
for i in $(seq 60); do   # ждём, если проход как раз идёт
  systemctl list-units 'trading-robot@*' --state=activating,active --no-legend | grep -q . || break; sleep 5
done
echo "Робот здесь выключен"

say "2/4 Копирую токены и состояние робота"
tar -C / -czf /tmp/migrate.tgz etc/trading-robot.env opt/trading/state usr/local/etc/xray/stats.env var/lib/vpn-stats 2>/dev/null \
  || tar -C / -czf /tmp/migrate.tgz etc/trading-robot.env opt/trading/state
scp -q -o BatchMode=yes /tmp/migrate.tgz "root@$NEW:/tmp/migrate.tgz" || die "Не удалось скопировать — робот здесь выключен, включите обратно: systemctl enable --now $TIMERS"
rm -f /tmp/migrate.tgz

say "3/4 Ставлю робота на новом сервере"
$SSH -t "set -e
id robot >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin robot
apt-get install -y -qq git >/dev/null
[ -d /opt/trading/.git ] || { rm -rf /opt/trading; git clone -q https://github.com/bp2gsvk2hj-boop/Trading.git /opt/trading; }
tar -C / -xzf /tmp/migrate.tgz && rm -f /tmp/migrate.tgz
chown -R robot:robot /opt/trading; chmod 600 /etc/trading-robot.env
curl -fsSL $RAW/server/install.sh | bash
# сайт не нужен: «Помощник трейдера» на GitHub Pages
rm -f /etc/nginx/sites-enabled/trader /etc/nginx/sites-available/trader
systemctl disable --now nginx >/dev/null 2>&1 || true
ufw delete allow 8080/tcp >/dev/null 2>&1 || true" \
  || die "Установка робота на $NEW не прошла. Чтобы вернуть робота сюда: systemctl enable --now $TIMERS"

say "4/4 Мониторинг VPN и данные «Помощника трейдера» — теперь с нового сервера"
for u in vpn-stats.timer trader-publish.timer; do systemctl disable --now "$u" >/dev/null 2>&1; done
$SSH "set -e
[ -f /usr/local/etc/xray/stats.env ] || exit 0
curl -fsSL $RAW/vpn/vpn-stats.sh -o /tmp/vpn-stats.sh && bash /tmp/vpn-stats.sh install >/dev/null
curl -fsSL $RAW/server/trader-publish.sh -o /tmp/trader-publish.sh && bash /tmp/trader-publish.sh install" \
  || echo "[!] Мониторинг на новом сервере не включился — напишите мне"

say "Готово. Осталось переключить устройства"
echo "Робот, мониторинг и «Помощник трейдера» работают на $NEW."
echo
echo "Keenetic (Маршрутизация + Другие подключения → WireGuard «VPN Основной»):"
echo "  1) статический маршрут: $NEW через Ростелеком"
echo "  2) в подключении поменять адрес сервера на $NEW:443"
echo
echo "Happ на iPhone и iPad — новая ссылка (замените старый сервер):"
$SSH "vpnctl all" 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' | grep -o 'vless://[^ ]*' | head -1
echo
echo "Яндекс пока остаётся запасным. Через неделю без проблем удалите его вместе со статическим IP."
