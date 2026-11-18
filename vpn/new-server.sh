#!/usr/bin/env bash
# =====================================================================
#  Новый зарубежный сервер ОДНОЙ командой (под root на этом сервере):
#     curl -fsSL https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/main/vpn/new-server.sh -o new-server.sh && bash new-server.sh
#  Делает всё сам: ставит VPN, подбирает сайт маскировки среди соседей
#  по сети, печатает ссылку и готовую команду проверки для сервера Яндекса.
# =====================================================================
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Запустите от root"; exit 1; }
B=https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/main/vpn
TEST=https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/b4f4b773f7191532932ec8fa3f93f5f6afcc76cc/vpn/test-upstream.sh
say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
cd /root

say "1/3 Устанавливаю VPN (2–3 минуты)"
curl -fsSL "$B/foreign.sh" -o foreign.sh
curl -fsSL "$B/sni-change.sh" -o sni-change.sh
bash foreign.sh > /root/new-server.log 2>&1 || { tail -25 /root/new-server.log; echo "Установка не удалась (полный лог: /root/new-server.log)"; exit 1; }
IP=$(sed -n 's/^SERVER_IP=//p' /usr/local/etc/xray/vpn.env)

say "2/3 Ищу сайт маскировки среди соседей $IP (около 2 минут)"
[ -x /root/rts ] || { curl -fsSL -o /root/rts https://github.com/XTLS/RealiTLScanner/releases/download/v0.2.3/RealiTLScanner-linux-amd64 && chmod +x /root/rts; }
rm -f /tmp/sites.csv
( cd /tmp && timeout 150 /root/rts -addr "$IP" -port 443 -thread 50 -timeout 5 -out /tmp/sites.csv >/dev/null 2>&1 ) || true
SKIP='google|gstatic|youtube|apple|icloud|microsoft|azure|office|live\.com|amazon|aws|cloudflare|cloudfront|facebook|instagram|whatsapp|akamai|samsung|yahoo|github|fastly|tiktok|twitter|x\.com|telegram|netflix|openai|chatgpt|localhost|example|invalid|traefik|plesk|cpanel|ispmanager|fornex|aeza|hetzner'
NEW=""
if [ -s /tmp/sites.csv ]; then
  COL=$(head -1 /tmp/sites.csv | tr ',' '\n' | grep -ni domain | head -1 | cut -d: -f1)
  for d in $(awk -F, -v c="${COL:-3}" 'NR>1{print $c}' /tmp/sites.csv | tr -d '"' | sed 's/^\*\.//' \
             | grep -E '^[a-z0-9.-]+\.[a-z]{2,}$' | grep -viE "$SKIP" | sort -u | head -40); do
    v=$(curl -s -o /dev/null -w '%{http_version}' --tlsv1.3 --http2 --max-time 6 "https://$d/" || true)
    [ "$v" = 2 ] && { NEW=$d; break; }
  done
fi
CUR=$(sed -n 's/^SNI=//p' /usr/local/etc/xray/vpn.env)
if [ -n "$NEW" ] && [ "$NEW" != "$CUR" ]; then
  bash sni-change.sh "$NEW" >> /root/new-server.log 2>&1 && echo "Сайт маскировки: $NEW" || { echo "Не удалось сменить сайт, оставляю $CUR"; NEW=$CUR; }
else
  NEW=$CUR; echo "Подходящего соседа не нашёл — оставляю $CUR (работает, но сосед по сети лучше)"
fi

say "3/3 Готово"
LINK=$(vpnctl link relay)
echo
echo "Ссылка этого сервера для Яндекса:"
echo "$LINK"
echo
echo "Теперь на сервере Яндекса (sudo -i) — проверка в сравнении с текущими серверами:"
echo
echo "curl -fsSL $TEST -o test-upstream.sh && bash test-upstream.sh '$LINK'"
echo
