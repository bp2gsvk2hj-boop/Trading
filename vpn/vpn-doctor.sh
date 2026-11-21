#!/usr/bin/env bash
# =====================================================================
#  «Доктор» VPN: проверяет сервер, чинит типовые поломки и пишет отчёт.
#  Запуск (под root):   bash vpn-doctor.sh          — проверить и починить
#                       FIX=0 bash vpn-doctor.sh    — только проверить
#  Работает и на российском сервере (relay), и на зарубежном.
#  Отчёт: /root/vpn-doctor.txt и сообщение в Telegram (если бот настроен).
#  В отчёте НЕТ ключей, UUID и токена — его можно показать Claude.
# =====================================================================
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Запустите от root (sudo -i)"; exit 1; }
FIX="${FIX:-1}"
X=/usr/local/etc/xray; CFG=$X/config.json; NEWC=$X/config.new.json; ENV=$X/vpn.env
REP=/root/vpn-doctor.txt; : > "$REP"
OK=0; BAD=0; FIXED=0
log()  { echo "$*" | tee -a "$REP"; }
ok()   { log "✓ $*"; OK=$((OK+1)); }
bad()  { log "✗ $*"; BAD=$((BAD+1)); }
fixd() { log "  ↻ починено: $*"; FIXED=$((FIXED+1)); }
hdr()  { log ""; log "── $* ──"; }
# убирает из текста секреты: UUID, ключи base64, токен бота
clean() { sed -E 's/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/<uuid>/g; s/[A-Za-z0-9_+\/-]{40,}={0,2}/<key>/g; s/[0-9]{8,10}:[A-Za-z0-9_-]{30,}/<token>/g'; }
xtest() { xray run -test -config "$1" >/tmp/xt.log 2>&1; }

log "VPN-доктор · $(hostname) · $(date '+%d.%m.%Y %H:%M %Z')"
[ -f "$ENV" ] || { bad "VPN не установлен ($ENV нет)"; exit 1; }
. "$ENV"
ROLE=foreign; [ -n "${UP1_HOST:-}" ] && ROLE=relay
log "Роль: $([ $ROLE = relay ] && echo 'российский relay' || echo 'зарубежный сервер') · IP в настройках: ${SERVER_IP:-?}"

# ---------- 1. Система ----------
hdr "Система"
PUB=$(curl -s -m 8 -4 https://api.ipify.org || true)
if [ -z "$PUB" ]; then bad "Нет выхода в интернет с сервера (api.ipify.org не отвечает)"
elif [ "$PUB" = "${SERVER_IP:-}" ]; then ok "Внешний IP совпадает с настройками ($PUB)"
else bad "Внешний IP $PUB, а в настройках ${SERVER_IP:-?} — ссылки устарели. Нужно: sed -i \"s/^SERVER_IP=.*/SERVER_IP=$PUB/\" $ENV && vpnctl all"; fi
getent hosts www.google.com >/dev/null && ok "DNS сервера работает" || bad "DNS на сервере не работает"
D=$(df -P / | awk 'NR==2{print $5}' | tr -d %); [ "${D:-0}" -lt 90 ] && ok "Диск занят на ${D}%" || bad "Диск занят на ${D}%"
M=$(free -m | awk '/Mem:/{print $7}'); [ "${M:-0}" -gt 80 ] && ok "Свободно памяти ${M} МБ" || bad "Мало памяти: ${M} МБ"
# REALITY чувствителен к часам: расхождение больше ~90 с ломает подключение
if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then ok "Часы синхронизированы"
else bad "Часы не синхронизированы (REALITY может не пускать клиентов)"
  [ "$FIX" = 1 ] && { timedatectl set-ntp true 2>/dev/null; systemctl restart systemd-timesyncd 2>/dev/null; fixd "включил синхронизацию времени"; }; fi

# ---------- 2. Xray ----------
hdr "Xray"
log "Версия: $(xray version 2>/dev/null | head -1 | awk '{print $2}')"
if xtest "$CFG"; then ok "Конфиг корректен"
else bad "Конфиг НЕ проходит проверку: $(tail -2 /tmp/xt.log | clean | tr '\n' ' ')"
  if [ "$FIX" = 1 ]; then
    for b in $(ls -t $X/config.json.bak* 2>/dev/null); do
      if xtest "$b"; then cp "$CFG" "$CFG.broken.$(date +%s)"; cp "$b" "$CFG"; fixd "вернул рабочую копию $(basename "$b")"; break; fi
    done
  fi
fi
if systemctl is-active --quiet xray; then ok "Xray запущен"
else bad "Xray не запущен"
  if [ "$FIX" = 1 ]; then systemctl restart xray; sleep 3
    systemctl is-active --quiet xray && fixd "перезапустил Xray" || log "  не запускается: $(journalctl -u xray -n 5 --no-pager -o cat | clean | tr '\n' ' ')"; fi
fi
systemctl is-enabled --quiet xray || { bad "Xray не в автозапуске"; [ "$FIX" = 1 ] && systemctl enable xray >/dev/null 2>&1 && fixd "включил автозапуск"; }
P=${PORT:-443}
ss -ltn "sport = :$P" | grep -q LISTEN && ok "Порт $P/tcp слушается" || bad "Порт $P/tcp никто не слушает"
WGP=""; [ -f $X/wg.json ] && WGP=$(jq -r .port $X/wg.json)
if [ -n "$WGP" ]; then
  jq -e '.inbounds[]|select(.tag=="wg-in")' "$CFG" >/dev/null && ok "WireGuard для роутера в конфиге (порт $WGP/udp)" || bad "WireGuard для роутера пропал из конфига (запустите: bash keenetic.sh)"
  ss -lun "sport = :$WGP" | grep -q . && ok "Порт $WGP/udp слушается" || bad "Порт $WGP/udp никто не слушает"
fi
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  for r in "$P/tcp" ${WGP:+"$WGP/udp"}; do
    ufw status | grep -q "^$r" && ok "Файрвол пропускает $r" || { bad "Файрвол не пропускает $r"; [ "$FIX" = 1 ] && ufw allow "$r" >/dev/null && fixd "открыл $r"; }
  done
fi
E=$(journalctl -u xray --since "-1h" --no-pager -o cat 2>/dev/null | grep -ciE "error|failed")
[ "${E:-0}" -lt 20 ] && ok "Ошибок Xray за час: ${E:-0}" || { bad "Много ошибок Xray за час: $E"; journalctl -u xray --since "-1h" --no-pager -o cat | grep -iE "error|failed" | tail -5 | clean | tee -a "$REP" >/dev/null; }

# ---------- 3. Маскировка ----------
hdr "Маскировка (REALITY)"
if [ -n "${SNI:-}" ]; then
  if out=$(echo | timeout 8 openssl s_client -connect "$SNI:443" -servername "$SNI" -tls1_3 -alpn h2 2>/dev/null); then
    grep -q "ALPN protocol: h2" <<<"$out" && ok "Сайт маскировки $SNI отвечает (TLS 1.3 + h2)" || bad "Сайт маскировки $SNI отвечает без HTTP/2 — нужен другой (sni-change.sh)"
  else bad "Сайт маскировки $SNI не отвечает с сервера — подключения не пройдут. Смените: bash sni-change.sh"; fi
fi

# ---------- 4. Только relay: маршруты и зарубежные серверы ----------
if [ $ROLE = relay ]; then
  hdr "Зарубежные серверы (проверка через этот сервер)"
  probe() { curl -s -o /dev/null -m 12 -w '%{http_code} %{time_total}' -x "socks5h://127.0.0.1:$1" https://www.gstatic.com/generate_204; }
  for i in 1 2; do
    H=$(eval echo "\${UP${i}_HOST:-}"); [ -n "$H" ] || continue
    NAME=$([ $i = 1 ] && echo "VPS 1 основной" || echo "VPS 2 резерв")
    r=$(probe $((10810+i)))
    if [ "${r%% *}" = 204 ]; then ok "$NAME ($H): работает, ответ за ${r#* } с"
    else
      timeout 5 bash -c "</dev/tcp/$H/443" 2>/dev/null \
        && bad "$NAME ($H): порт 443 открыт, но VPN не отвечает — проблема в Xray на том сервере или в ключах" \
        || bad "$NAME ($H): недоступен из России (IP заблокирован или сервер выключен) — сменить IP"
    fi
  done
  r=$(probe 10810); [ "${r%% *}" = 204 ] && ok "Балансер выдаёт рабочий выход" || bad "Балансер не даёт выхода за рубеж — VPN для зарубежных сайтов не работает"
  hdr "Правила маршрутизации"
  jq -e '.routing.rules[]|select(.network=="udp" and .port=="443" and .outboundTag=="block")' "$CFG" >/dev/null && ok "QUIC блокируется" || {
    bad "Нет блокировки QUIC (сайты могут открываться медленно)"
    if [ "$FIX" = 1 ]; then jq '.routing.rules=[{"type":"field","network":"udp","port":"443","outboundTag":"block"}]+.routing.rules' "$CFG" > "$NEWC" \
      && xtest "$NEWC" && cp "$CFG" "$CFG.bak" && mv "$NEWC" "$CFG" && systemctl restart xray && fixd "добавил блокировку QUIC"; fi; }
  jq -e '[.routing.rules[]|select(.outboundTag=="direct")|.domain[]?]|any(.=="domain:yandex.net")' "$CFG" >/dev/null && ok "Яндекс/Кинопоиск идут напрямую" || bad "Нет прямого правила для Яндекса (переустановить: bash ru.sh … по команде из backup/инструкции)"
  jq -e '.routing.balancers[0].fallbackTag' "$CFG" >/dev/null 2>&1 && ok "Резерв настроен (fallback)" || log "· резервного сервера нет"
  r=$(curl -s -o /dev/null -m 8 -w '%{http_code}' https://ya.ru); [ "$r" != 000 ] && ok "Российские сайты с сервера открываются" || bad "Российские сайты с сервера не открываются"
  hdr "Мониторинг"
  systemctl is-active --quiet vpn-monitor.timer && ok "Таймер проверки (раз в 15 мин) работает" || { bad "Таймер мониторинга выключен"; [ "$FIX" = 1 ] && systemctl enable --now vpn-monitor.timer >/dev/null 2>&1 && fixd "включил таймер"; }
  [ -n "${TG_TOKEN:-}" ] && [ -n "${TG_CHAT:-}" ] && ok "Telegram-бот настроен" || bad "Telegram-бот не настроен"
  [ -s /var/lib/vpn-monitor/unsent.log ] && log "· неотправленные уведомления: $(wc -l < /var/lib/vpn-monitor/unsent.log), последнее: $(tail -1 /var/lib/vpn-monitor/unsent.log | cut -c1-19)"
fi

# ---------- Итог ----------
hdr "Итог"
if [ $BAD = 0 ]; then log "🟢 Всё в порядке ($OK проверок)"
elif [ $FIXED -ge $BAD ]; then log "🟡 Найдено проблем: $BAD, все починены автоматически. Запустите доктора ещё раз для контроля."
else log "🔴 Проблем: $BAD, починено: $FIXED. Пришлите этот отчёт Claude."; fi
log "Отчёт: $REP"

if [ -z "${NO_TG:-}" ] && [ -n "${TG_TOKEN:-}" ] && [ -n "${TG_CHAT:-}" ]; then
  TXT=$(clean < "$REP" | head -c 3900)
  for via in socks5h://127.0.0.1:10810 ""; do
    curl -s -m 20 ${via:+-x "$via"} "https://api.telegram.org/bot$TG_TOKEN/sendMessage" \
      --data-urlencode "chat_id=$TG_CHAT" --data-urlencode "text=$TXT" | grep -q '"ok":true' && { echo "(отчёт отправлен в Telegram)"; break; }
  done
fi
