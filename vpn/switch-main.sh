#!/usr/bin/env bash
# =====================================================================
#  Смена основного зарубежного сервера (запускать на сервере Яндекса):
#     bash switch-main.sh 'vless://…НОВЫЙ_ОСНОВНОЙ#relay' [новый_сайт_маскировки_резерва]
#  1) проверяет новый основной и резерв ДО переключения (если не работают — ничего не меняет);
#  2) переключает: новый — основной, текущий резерв остаётся резервом;
#  3) проверяет работу; 4) делает резервную копию в Telegram (без пароля).
#  Ключи, ссылки на устройствах и роутер не меняются.
# =====================================================================
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Запустите от root (sudo -i)"; exit 1; }
NEW="${1:?Укажите ссылку нового основного сервера}"; RSNI="${2:-}"
CFG=/usr/local/etc/xray/config.json
B=https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/main/vpn
say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
cd /root
for f in ru.sh test-upstream.sh vpn-doctor.sh backup.sh; do curl -fsSL "$B/$f" -o "$f"; done

A2=$(jq -r '.outbounds[]|select(.tag=="foreign2")|.settings.vnext[0] as $v|.streamSettings as $s|"vless://\($v.users[0].id)@\($v.address):\($v.port)?encryption=none&security=reality&sni=\($s.realitySettings.serverName)&fp=\($s.realitySettings.fingerprint)&pbk=\($s.realitySettings.publicKey)&sid=\($s.realitySettings.shortId)&type=xhttp&path=\($s.xhttpSettings.path|@uri)&mode=\($s.xhttpSettings.mode)#relay"' "$CFG")
[ -n "$A2" ] || { echo "Резервный сервер в настройках не найден"; exit 1; }
[ -n "$RSNI" ] && A2=$(sed "s/sni=[^&]*/sni=$RSNI/" <<<"$A2")

say "1/4 Проверяю новый основной и резерв до переключения"
OUT=$(ONLY=1 bash test-upstream.sh "$NEW" "$A2" 2>&1); echo "$OUT"
if grep -q "НЕ работает\|НЕДОСТУПЕН" <<<"$OUT"; then
  echo; echo "✗ Один из серверов не работает с новыми настройками — НИЧЕГО НЕ МЕНЯЮ."
  echo "  Проверьте, что сайт маскировки сменён на зарубежных серверах, и запустите снова."; exit 1
fi

say "2/4 Переключаю: новый — основной, $(sed 's/.*@\([^:]*\):.*/\1/' <<<"$A2") — резерв"
bash ru.sh "$NEW" "$A2"

say "3/4 Проверка после переключения (ждём 40 секунд, пока проверятся серверы)"
sleep 40
vpn-monitor status || true
FIX=0 bash vpn-doctor.sh || true

say "4/4 Резервная копия в Telegram"
NOPASS=1 bash backup.sh || echo "Копию сделать не удалось — запустите позже: NOPASS=1 bash backup.sh"
say "Готово. На телефоне ifconfig.me должен показать IP нового основного сервера."
