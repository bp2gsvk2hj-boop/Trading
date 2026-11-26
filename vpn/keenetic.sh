#!/usr/bin/env bash
# =====================================================================
#  WireGuard-вход для роутера Keenetic на российском промежуточном сервере.
#  Запуск на российском промежуточном сервере (под sudo -i):  bash keenetic.sh
#  Keenetic подключается штатным WireGuard к этому серверу (внутри России),
#  дальше трафик идёт по тем же правилам: российские сайты — напрямую,
#  остальное — через зарубежный сервер (и резерв, если настроен).
#  Повторный запуск не меняет ключи (NEWKEYS=1 — выпустить новые).
# =====================================================================
set -euo pipefail
X=/usr/local/etc/xray; CFG=$X/config.json; WG=$X/wg.json; CENV=$X/wg-client.env; NEWCFG=$X/config.new.json
PORT="${WG_PORT:-51820}"
[ "$(id -u)" -eq 0 ] || { echo "Запустите от root (sudo -i)"; exit 1; }
[ -f $X/vpn.env ] && grep -q '^UP1_HOST=.\+' $X/vpn.env || { echo "Сначала установите промежуточный сервер (ru.sh)"; exit 1; }
. $X/vpn.env

genkey() {  # печатает "приватный публичный" в base64, как в WireGuard
  local pem; pem=$(openssl genpkey -algorithm X25519)
  echo "$(openssl pkey -outform DER <<<"$pem" | tail -c 32 | base64) $(openssl pkey -pubout -outform DER <<<"$pem" | tail -c 32 | base64)"
}

if [ ! -f "$WG" ] || [ ! -f "$CENV" ] || [ "${NEWKEYS:-0}" = 1 ]; then
  read -r SPRIV SPUB < <(genkey)
  read -r CPRIV CPUB < <(genkey)
  jq -n --arg sk "$SPRIV" --arg cp "$CPUB" --argjson port "$PORT" '{
    tag:"wg-in", listen:"0.0.0.0", port:$port, protocol:"wireguard",
    settings:{secretKey:$sk, peers:[{publicKey:$cp, allowedIPs:["10.66.66.2/32"]}], mtu:1280},
    sniffing:{enabled:true, destOverride:["http","tls","quic"], routeOnly:false}}' > "$WG"
  printf 'CPRIV=%s\nSPUB=%s\nPORT=%s\n' "$CPRIV" "$SPUB" "$PORT" > "$CENV"
  chmod 600 "$WG" "$CENV"
fi
. "$CENV"

jq --slurpfile w "$WG" '.inbounds = ([.inbounds[] | select(.tag != "wg-in")] + $w)' "$CFG" > "$NEWCFG"
if ! out=$(xray run -test -config "$NEWCFG" 2>&1); then echo "$out" | tail -8; echo "Xray не принял конфиг — ничего не меняю"; rm -f "$NEWCFG"; exit 1; fi
cp "$CFG" "$CFG.bak"; mv "$NEWCFG" "$CFG"; chmod 644 "$CFG"
command -v ufw >/dev/null && ufw allow "$PORT/udp" >/dev/null || true
systemctl restart xray; sleep 2
systemctl is-active --quiet xray || { echo "Xray не запустился — возвращаю прежний конфиг"; cp "$CFG.bak" "$CFG"; systemctl restart xray; exit 1; }

cat > /root/keenetic.conf <<CONF
[Interface]
PrivateKey = $CPRIV
Address = 10.66.66.2/32
DNS = 1.1.1.1
MTU = 1280

[Peer]
PublicKey = $SPUB
AllowedIPs = 0.0.0.0/0
Endpoint = $SERVER_IP:$PORT
PersistentKeepalive = 25
CONF
chmod 600 /root/keenetic.conf

printf '\n\033[1;32m==> Готово: WireGuard для Keenetic на %s:%s/udp\033[0m\n' "$SERVER_IP" "$PORT"
SENT=0
if [ -n "${TG_TOKEN:-}" ] && [ -n "${TG_CHAT:-}" ]; then
  for via in "socks5h://127.0.0.1:10810" ""; do
    if curl -s -m 30 ${via:+-x "$via"} -F "chat_id=$TG_CHAT" -F "document=@/root/keenetic.conf" \
         -F "caption=Файл для Keenetic: Интернет → Другие подключения → WireGuard → Загрузить из файла. Никому не пересылайте." \
         "https://api.telegram.org/bot$TG_TOKEN/sendDocument" | grep -q '"ok":true'; then SENT=1; break; fi
  done
fi
if [ "$SENT" = 1 ]; then
  echo "Файл keenetic.conf отправлен вам в Telegram (в чат с вашим ботом)."
else
  echo "Не удалось отправить в Telegram. Файл лежит на сервере: /root/keenetic.conf"
fi
echo "Если роутер не подключится: проверьте, что у провайдера сервера открыт входящий UDP $PORT (файрвол в панели облака)."
