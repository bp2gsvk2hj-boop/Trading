#!/usr/bin/env bash
# =====================================================================
#  СКРИПТ 1 — зарубежный сервер (Aeza).   Запуск:  bash foreign.sh
#  • если VPN ещё не установлен — ставит его (одна ссылка me);
#  • создаёт подключение для российского сервера;
#  • печатает готовую команду для российского сервера.
#  Повторный запуск безопасен: ключи и ссылки не меняются.
# =====================================================================
set -euo pipefail
REF=ca421351dd8790b5496ace45e9aa43dcdb621442
BASE="https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/$REF/vpn"
RU_URL="https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/220267168518f6fa849afb2ce1b38cc6abb7babd/vpn/ru.sh"
[ "$(id -u)" -eq 0 ] || { echo "Запустите от root"; exit 1; }

if [ ! -f /usr/local/etc/xray/vpn.env ]; then
  curl -fsSL "$BASE/vpn-setup.sh" -o /root/vpn-setup.sh
  CLIENTS="${CLIENTS:-me}" bash /root/vpn-setup.sh
else
  echo "VPN на этом сервере уже установлен — ключи и ссылки не трогаю."
fi

vpnctl list | grep -qx relay || vpnctl add relay >/dev/null
RELAY=$(vpnctl link relay)

printf '\n\033[1;32m%s\033[0m\n' "Готово. Скопируйте ВСЮ строку ниже и выполните её на РОССИЙСКОМ сервере:"
echo
echo "curl -fsSL $RU_URL -o ru.sh && bash ru.sh '$RELAY'"
echo
