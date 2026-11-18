#!/usr/bin/env bash
# Показывает текущий сайт маскировки и подходящих соседей по сети (TLS 1.3 + HTTP/2).
#   bash sni-scan.sh
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Запустите от root"; exit 1; }
ENV=/usr/local/etc/xray/vpn.env
IP=$(sed -n 's/^SERVER_IP=//p' "$ENV" 2>/dev/null); [ -n "$IP" ] || IP=$(curl -s -4 -m 8 https://api.ipify.org)
echo "Сервер: $IP · текущий сайт маскировки: $(sed -n 's/^SNI=//p' "$ENV" 2>/dev/null)"
[ -x /root/rts ] || { curl -fsSL -o /root/rts https://github.com/XTLS/RealiTLScanner/releases/download/v0.2.3/RealiTLScanner-linux-amd64 && chmod +x /root/rts; }
echo "Сканирую соседей (около 2 минут)…"
( cd /tmp && rm -f sites.csv && timeout 150 /root/rts -addr "$IP" -port 443 -thread 50 -timeout 5 -out /tmp/sites.csv >/dev/null 2>&1 ) || true
[ -s /tmp/sites.csv ] || { echo "Сканер ничего не нашёл"; exit 1; }
COL=$(head -1 /tmp/sites.csv | tr ',' '\n' | grep -ni domain | head -1 | cut -d: -f1)
echo; echo "Подходящие соседи (TLS 1.3 + HTTP/2, сайт открывается):"
n=0
for d in $(awk -F, -v c="${COL:-3}" 'NR>1{print $c}' /tmp/sites.csv | tr -d '"' | sed 's/^\*\.//' \
           | grep -E '^[a-z0-9.-]+\.[a-z]{2,}$' | grep -viE 'localhost|example|invalid|traefik|plesk|cpanel|ispmanager' | sort -u | head -80); do
  r=$(curl -s -o /dev/null -w '%{http_version} %{http_code}' --tlsv1.3 --http2 --max-time 6 "https://$d/" || true)
  [ "${r%% *}" = 2 ] || continue
  a=$(getent ahostsv4 "$d" | awk 'NR==1{print $1}')
  echo "  $d   (ответ ${r#* }, IP $a)"; n=$((n+1)); [ $n -ge 25 ] && break
done
[ $n -gt 0 ] || echo "  не найдено"
