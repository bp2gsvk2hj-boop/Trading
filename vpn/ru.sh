#!/usr/bin/env bash
# =====================================================================
#  СКРИПТ 2 — российский промежуточный сервер.
#  Запуск — готовой строкой, которую напечатал foreign.sh:
#     bash ru.sh 'vless://...основной...'
#  С резервным сервером и уведомлениями в Telegram:
#     TG_TOKEN=123:ABC bash ru.sh 'vless://...основной...' 'vless://...резерв...'
#  Сам подбирает российский сайт для маскировки, ставит VPN и выдаёт
#  ссылку и QR-код (одна на все устройства). Российские сайты идут напрямую
#  отсюда, всё остальное — через зарубежный сервер.
# =====================================================================
set -euo pipefail
REF=5e21f8cdd57e85460241660d78d53e633c36d763
BASE="https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/$REF/vpn"
[ "$(id -u)" -eq 0 ] || { echo "Запустите от root"; exit 1; }
UP="${1:-}"; UP2="${2:-}"
[[ "$UP" == vless://* ]] || { echo "Нужна ссылка на зарубежный сервер: bash ru.sh 'vless://...'"; exit 1; }
[ -z "$UP2" ] || [[ "$UP2" == vless://* ]] || { echo "Вторая ссылка (резерв) должна начинаться с vless://"; exit 1; }

if [ -f /usr/local/etc/xray/vpn.env ] && [ "${FORCE:-0}" != "1" ]; then
  echo "VPN здесь уже установлен. Ссылки:"; vpnctl all; exit 0
fi

curl -fsSL "$BASE/vpn-setup.sh" -o /root/vpn-setup.sh
curl -fsSL "$BASE/sni-pick.sh"  -o /root/sni-pick.sh

SNI="${SNI:-}"
if [ -z "$SNI" ]; then
  apt-get update -qq >/dev/null 2>&1 || true
  printf '\n\033[1;32m==> %s\033[0m\n' "Подбираю российский сайт для маскировки (2–3 минуты)"
  SNI=$(bash /root/sni-pick.sh --auto 2>/dev/null | tail -1 || true)
  [ -n "$SNI" ] && echo "Сайт: $SNI" || echo "Не нашёл подходящий — возьму стандартный"
fi

UPSTREAM="$UP" UPSTREAM2="$UP2" TG_TOKEN="${TG_TOKEN:-}" SNI="$SNI" CLIENTS="${CLIENTS:-VPN_Amsterdam}" FORCE=1 bash /root/vpn-setup.sh
