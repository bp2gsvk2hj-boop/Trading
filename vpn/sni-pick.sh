#!/usr/bin/env bash
# =====================================================================
#  Подбор российского сайта для маскировки REALITY (SNI)
#  Запускать на РОССИЙСКОМ промежуточном сервере:  bash sni-pick.sh
#
#  1. Берёт ежедневно обновляемый список доменов из белых списков
#     мобильного интернета (github.com/ancored/ru-mobile-whitelist).
#  2. Отбрасывает «засвеченные» домены (Яндекс, VK, Google, Госуслуги…).
#  3. Определяет, какие сайты живут в той же сети (ASN), что и сервер.
#  4. Проверяет TLS 1.3 + HTTP/2 и печатает лучших кандидатов.
# =====================================================================
set -uo pipefail

LIST_URL="https://github.com/ancored/ru-mobile-whitelist/releases/latest/download/ru-mobile-whitelist-sni.txt"
SKIP='(^|\.)(google|gstatic|youtube|yandex|ya|yastatic|yandexcloud|vk|vkontakte|vkvideo|userapi|mail|ok|dzen|gosuslugi|gov|kremlin|government|sber|sberbank|tbank|tinkoff|vtb|ozon|wildberries|wb|avito|mts|megafon|beeline|tele2|t2|rutube|apple|icloud|microsoft|cloudflare|max)\.'
TMP=$(mktemp -d)
trap 'rm -r "$TMP"' EXIT

need() { command -v "$1" >/dev/null || { apt-get install -y -qq "$2" >/dev/null 2>&1 || true; }; }
need nc netcat-openbsd; need curl curl

echo "==> Скачиваю список доменов из белых списков"
curl -fsSL --max-time 30 "$LIST_URL" -o "$TMP/raw.txt" || { echo "Не удалось скачать список: $LIST_URL"; exit 1; }
sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^\*\.//' -e 's/^\.//' "$TMP/raw.txt" \
  | tr 'A-Z' 'a-z' | grep -E '^[a-z0-9.-]+\.[a-z]{2,}$' | grep -Ev "$SKIP" | sort -u > "$TMP/domains.txt"
echo "    доменов после фильтра: $(wc -l < "$TMP/domains.txt")"

MYIP=$(curl -4 -fsS --max-time 6 https://api.ipify.org || true)
echo "==> IP сервера: ${MYIP:-не определён}"

echo "==> Определяю IP доменов (1–2 минуты)"
resolve() { ip=$(getent ahostsv4 "$1" 2>/dev/null | awk 'NR==1{print $1}'); [ -n "$ip" ] && echo "$ip $1"; }
export -f resolve
xargs -a "$TMP/domains.txt" -P 32 -I{} bash -c 'resolve "$@"' _ {} > "$TMP/resolved.txt"
echo "    найдено адресов: $(wc -l < "$TMP/resolved.txt")"

echo "==> Определяю сети (ASN) через whois.cymru.com"
{ echo begin; echo noheader; [ -n "$MYIP" ] && echo "$MYIP"; awk '{print $1}' "$TMP/resolved.txt" | sort -u; echo end; } \
  | nc -w 30 whois.cymru.com 43 2>/dev/null | awk -F'|' '{gsub(/ /,"",$1); gsub(/ /,"",$2); print $2, $1}' > "$TMP/asn.txt" || true
MYASN=$(awk -v ip="$MYIP" '$1==ip{print $2; exit}' "$TMP/asn.txt")
MYNET=$(cut -d. -f1-2 <<<"${MYIP:-0.0}")
echo "    сеть сервера: AS${MYASN:-?}"

# Оценка: 2 — та же ASN, 1 — та же сеть /16, 0 — остальные
awk -v asn="$MYASN" -v net="$MYNET" 'NR==FNR{a[$1]=$2; next}
  { split($1,o,"."); s=0
    if (asn!="" && a[$1]==asn) s=2; else if (o[1]"."o[2]==net) s=1
    print s, $1, $2 }' "$TMP/asn.txt" "$TMP/resolved.txt" | sort -k1,1nr -k3 > "$TMP/scored.txt"

echo "==> Проверяю TLS 1.3 + HTTP/2"
tls_ok() { v=$(curl -s -o /dev/null -w '%{http_version}' --tlsv1.3 --http2 --max-time 6 "https://$3/" 2>/dev/null); [ "$v" = "2" ] && echo "$1 $2 $3"; }
export -f tls_ok
# все «соседи» + первые 150 остальных
{ awk '$1>0' "$TMP/scored.txt"; awk '$1==0' "$TMP/scored.txt" | head -150; } \
  | xargs -P 16 -L1 bash -c 'tls_ok "$@"' _ | sort -k1,1nr -k3 > "$TMP/ok.txt"

echo
same=$(awk '$1==2' "$TMP/ok.txt" | head -10)
near=$(awk '$1==1' "$TMP/ok.txt" | head -10)
other=$(awk '$1==0' "$TMP/ok.txt" | head -10)
if [ -n "$same" ]; then echo "★ ЛУЧШИЕ — в той же сети, что и сервер (AS$MYASN):"; awk '{printf "    %-40s %s\n", $3, $2}' <<<"$same"; echo; fi
if [ -n "$near" ]; then echo "☆ Рядом (та же подсеть /16):"; awk '{printf "    %-40s %s\n", $3, $2}' <<<"$near"; echo; fi
if [ -n "$other" ]; then echo "• Подходят, но из других сетей:"; awk '{printf "    %-40s %s\n", $3, $2}' <<<"$other"; echo; fi
[ -s "$TMP/ok.txt" ] || { echo "Подходящих сайтов не нашлось."; exit 1; }
echo "Выберите домен из верхней группы и подставьте в установку: SNI=домен"
