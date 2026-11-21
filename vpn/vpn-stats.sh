#!/usr/bin/env bash
# =====================================================================
#  Сбор статистики VPN для страницы мониторинга (запускается на сервере Яндекса).
#
#  Установка (один раз):
#     GH_TOKEN='ghp_…' bash vpn-stats.sh install
#  Токен GitHub нужен с ЕДИНСТВЕННЫМ правом «gist» — им можно только
#  создавать/обновлять ваши gist'ы, доступа к репозиториям он не даёт.
#
#  Дальше раз в 15 минут таймер сам меряет зарубежные серверы и сервер
#  в Москве и выкладывает данные (без IP-адресов и ключей) в секретный gist,
#  откуда их читает страница мониторинга.
#     bash vpn-stats.sh        — собрать и отправить сейчас
#     bash vpn-stats.sh url    — показать адрес страницы
# =====================================================================
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Запустите от root (sudo -i)"; exit 1; }
X=/usr/local/etc/xray; ENV=$X/vpn.env; SENV=$X/stats.env
ST=/var/lib/vpn-stats; mkdir -p "$ST"; HIST=$ST/history.json
PAGE="https://bp2gsvk2hj-boop.github.io/Trading/vpn-status/"
KEEP=672            # точек истории: 7 суток по 15 минут
SPEED_EVERY=12      # замер скорости раз в 12 запусков = раз в 3 часа
[ -f "$ENV" ] || { echo "VPN не установлен"; exit 1; }
. "$ENV"; [ -f "$SENV" ] && . "$SENV"

gh() {  # $1 метод, $2 путь, $3 тело — сначала напрямую, потом через VPN
  local via out
  for via in "" socks5h://127.0.0.1:10810; do
    out=$(curl -s -m 30 ${via:+-x "$via"} -X "$1" "https://api.github.com$2" \
      -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" \
      ${3:+--data-binary @"$3"}) || continue
    jq -e '.id' >/dev/null 2>&1 <<<"$out" && { echo "$out"; return 0; }
  done
  echo "$out" >&2; return 1
}

# ---------------- установка ----------------
if [ "${1:-}" = install ]; then
  [ -n "${GH_TOKEN:-}" ] || { echo "Нужен токен: GH_TOKEN='ghp_…' bash vpn-stats.sh install"; exit 1; }
  echo '[]' > "$HIST"
  if [ -z "${GIST_ID:-}" ]; then
    jq -n '{description:"vpn status", public:false, files:{"vpn-status.json":{content:"{}"}}}' > "$ST/p.json"
    R=$(gh POST /gists "$ST/p.json") || { echo "Не удалось создать gist — проверьте токен (нужно право gist)"; exit 1; }
    GIST_ID=$(jq -r .id <<<"$R")
  fi
  umask 077; printf 'GH_TOKEN=%q\nGIST_ID=%q\n' "$GH_TOKEN" "$GIST_ID" > "$SENV"
  cp "$0" /usr/local/bin/vpn-stats 2>/dev/null || curl -fsSL https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/main/vpn/vpn-stats.sh -o /usr/local/bin/vpn-stats
  chmod 755 /usr/local/bin/vpn-stats
  cat > /etc/systemd/system/vpn-stats.service <<'EOF'
[Unit]
Description=Статистика VPN для страницы мониторинга
[Service]
Type=oneshot
ExecStart=/usr/local/bin/vpn-stats
EOF
  cat > /etc/systemd/system/vpn-stats.timer <<'EOF'
[Unit]
Description=Статистика VPN раз в 15 минут
[Timer]
OnBootSec=3min
OnUnitActiveSec=15min
[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload; systemctl enable --now vpn-stats.timer >/dev/null 2>&1
  echo "Первый замер (около минуты)…"; rm -f "$ST/n"; /usr/local/bin/vpn-stats >/dev/null 2>&1
  echo; echo "Готово. Страница мониторинга (откройте и добавьте в закладки):"
  echo "${PAGE}#${GIST_ID}"; exit 0
fi
[ "${1:-}" = url ] && { echo "${PAGE}#${GIST_ID:-}"; exit 0; }
[ -n "${GH_TOKEN:-}" ] && [ -n "${GIST_ID:-}" ] || { echo "Сначала: GH_TOKEN='…' bash vpn-stats.sh install"; exit 1; }

# ---------------- замеры ----------------
lat() {  # медиана 3 проб через сервер, мс; пусто — не работает
  local v a=()
  for _ in 1 2 3; do
    v=$(curl -s -o /dev/null -m 10 -w '%{http_code} %{time_total}' -x "socks5h://127.0.0.1:$1" https://www.gstatic.com/generate_204)
    [ "${v%% *}" = 204 ] && a+=("${v#* }")
  done
  [ ${#a[@]} -gt 0 ] && printf '%s\n' "${a[@]}" | sort -n | sed -n "$(( (${#a[@]}+1)/2 ))p" | awk '{printf "%.0f", $1*1000}'
}
speed() {  # скачивание 20 МБ через сервер, Мбит/с
  local r; r=$(curl -s -o /dev/null -m 40 -w '%{speed_download} %{size_download}' -x "socks5h://127.0.0.1:$1" 'https://speed.cloudflare.com/__down?bytes=20000000')
  awk -v s="${r%% *}" -v n="${r#* }" 'BEGIN{ if (n+0 >= 19000000) printf "%.0f", s*8/1e6 }'
}
place() {  # город выхода (кэш на сутки)
  local f=$ST/place$1
  if [ ! -s "$f" ] || [ -n "$(find "$f" -mmin +1440 2>/dev/null)" ]; then
    curl -s -m 10 -x "socks5h://127.0.0.1:$1" https://ipinfo.io/json | jq -r '[.country, .city] | map(select(.)) | join(" · ")' > "$f.new" 2>/dev/null
    [ -s "$f.new" ] && mv "$f.new" "$f"
  fi
  cat "$f" 2>/dev/null
}
N=$(( $(cat "$ST/n" 2>/dev/null || echo -1) + 1 )); echo "$N" > "$ST/n"
DO_SPEED=$(( N % SPEED_EVERY == 0 ))

S='{}'
for i in 1 2; do
  H=$(eval echo "\${UP${i}_HOST:-}"); [ -n "$H" ] || continue
  P=$((10810+i)); MS=$(lat $P); MB=""
  [ -n "$MS" ] && [ $DO_SPEED = 1 ] && MB=$(speed $P)
  S=$(jq --arg k "s$i" --arg ms "$MS" --arg mb "$MB" --arg pl "$(place $P)" \
      '.[$k] = {ok: ($ms != ""), ms: ($ms|tonumber? // null), mbps: ($mb|tonumber? // null), place: $pl}' <<<"$S")
done

# сервер в Москве: процессор (по /proc/stat между запусками), память, подключения, трафик
read -r _ u n s idle io irq sirq steal _ < /proc/stat
TOT=$((u+n+s+idle+io+irq+sirq+steal)); BUSY=$((TOT-idle-io))
CPU=null; STEAL=null
if [ -s "$ST/cpu" ]; then read -r PT PB PS < "$ST/cpu"; DT=$((TOT-PT))
  [ $DT -gt 0 ] && CPU=$(( (BUSY-PB)*100/DT )) && STEAL=$(( (steal-PS)*100/DT )); fi
echo "$TOT $BUSY $steal" > "$ST/cpu"
MEM=$(free | awk '/Mem:/{printf "%.0f", ($2-$7)*100/$2}')
CONN=$(ss -tnH state established '( sport = :443 )' | awk '{print $4}' | sed 's/:[0-9]*$//' | sort -u | wc -l)
IF=$(ip route show default | awk '{print $5; exit}')
RX=$(cat /sys/class/net/$IF/statistics/rx_bytes); TX=$(cat /sys/class/net/$IF/statistics/tx_bytes)
DRX=null; DTX=null
if [ -s "$ST/net" ]; then read -r PRX PTX < "$ST/net"
  [ "$RX" -ge "$PRX" ] && DRX=$(( (RX-PRX)/1048576 )) && DTX=$(( (TX-PTX)/1048576 )); fi
echo "$RX $TX" > "$ST/net"
XR=$(systemctl is-active --quiet xray && echo true || echo false)
UP=$(awk '{printf "%.0f", $1/3600}' /proc/uptime)

PT=$(jq -nc --argjson s "$S" --argjson cpu "$CPU" --argjson st "$STEAL" --argjson mem "$MEM" \
  --argjson c "$CONN" --argjson rx "$DRX" --argjson tx "$DTX" \
  '{t: now|floor, s1: $s.s1, s2: $s.s2, cpu: $cpu, steal: $st, mem: $mem, conns: $c, rx: $rx, tx: $tx}')
[ -s "$HIST" ] || echo '[]' > "$HIST"
jq --argjson p "$PT" --argjson k $KEEP '(. + [$p]) | if length > $k then .[length-$k:] else . end' "$HIST" > "$HIST.new" && mv "$HIST.new" "$HIST"

jq -n --slurpfile h "$HIST" --argjson xr "$XR" --argjson up "$UP" \
  --arg mon "$(systemctl is-active vpn-monitor.timer 2>/dev/null)" \
  '{version: 1, updated: now|floor, every_min: 15, relay: {xray: $xr, uptime_h: $up, monitor: ($mon == "active")}, points: $h[0]}' > "$ST/out.json"
jq -n --rawfile c "$ST/out.json" '{files: {"vpn-status.json": {content: $c}}}' > "$ST/p.json"
gh PATCH "/gists/$GIST_ID" "$ST/p.json" >/dev/null && echo "Отправлено ($(jq '.points|length' "$ST/out.json") точек)" || echo "Не удалось отправить в GitHub"
