#!/usr/bin/env bash
# =====================================================================
#  СКРИПТ 1 — зарубежный сервер (Aeza).   Запуск:  bash foreign.sh
#  • если VPN ещё не установлен — ставит его (одна ссылка «VPN Amsterdam»);
#  • создаёт подключение для российского сервера;
#  • печатает готовую команду для российского сервера.
#  Повторный запуск безопасен: ключи и ссылки не меняются.
# =====================================================================
set -euo pipefail
REF=5e21f8cdd57e85460241660d78d53e633c36d763
BASE="https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/$REF/vpn"
RU_URL="https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/b05656bf1bebc54d4b5ed08360dc3c535cb01545/vpn/ru.sh"
[ "$(id -u)" -eq 0 ] || { echo "Запустите от root"; exit 1; }

if [ ! -f /usr/local/etc/xray/vpn.env ]; then
  curl -fsSL "$BASE/vpn-setup.sh" -o /root/vpn-setup.sh
  CLIENTS="${CLIENTS:-VPN_Amsterdam}" bash /root/vpn-setup.sh
else
  echo "VPN на этом сервере уже установлен — ключи и ссылки не трогаю."
fi

vpnctl list | grep -qx relay || vpnctl add relay >/dev/null
RELAY=$(vpnctl link relay)

printf '\n\033[1;32m%s\033[0m\n' "Готово. Скопируйте ВСЮ строку ниже и выполните её на РОССИЙСКОМ сервере:"
echo
echo "curl -fsSL $RU_URL -o ru.sh && bash ru.sh '$RELAY'"
echo
