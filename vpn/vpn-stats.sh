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
SPEED_EVERY=4       # замер скорости раз в 4 запуска = раз в час
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

# ---------------- статистика Xray (трафик по направлениям и устройствам) ----------------
CFG=$X/config.json; API=127.0.0.1:10085
if ! jq -e '.stats and .api.listen' "$CFG" >/dev/null 2>&1; then
  jq --arg l "$API" '.stats = {} | .api = {tag: "api", listen: $l, services: ["StatsService"]}
      | .policy.system = ((.policy.system // {}) + {statsInboundUplink: true, statsInboundDownlink: true, statsOutboundUplink: true, statsOutboundDownlink: true})' \
    "$CFG" > "$ST/cfg.json" && xray run -test -config "$ST/cfg.json" >/dev/null 2>&1 \
    && cp "$CFG" "$CFG.bak-stats" && cp "$ST/cfg.json" "$CFG" && systemctl restart xray && sleep 3 \
    && echo "Включил статистику трафика в Xray"
fi
xstats() {  # МБ за интервал по имени счётчика (со сбросом)
  xray api statsquery -s "$API" -reset 2>/dev/null | jq -c '[.stat[]? | {(.name): ((.value // 0) | tonumber)}] | add // {}' 2>/dev/null || echo '{}'
}
XS=$(xstats)

# ---------------- замеры ----------------
lat() {  # медиана 3 проб через сервер, мс; пусто — не работает
  local v a=()
  for _ in 1 2 3; do
    v=$(curl -s -o /dev/null -m 10 -w '%{http_code} %{time_total}' -x "socks5h://127.0.0.1:$1" https://www.gstatic.com/generate_204)
    [ "${v%% *}" = 204 ] && a+=("${v#* }")
  done
  [ ${#a[@]} -gt 0 ] && printf '%s\n' "${a[@]}" | sort -n | sed -n "$(( (${#a[@]}+1)/2 ))p" | awk '{printf "%.0f", $1*1000}'
}
TESTBYTES=0
speed() {  # скачивание 20 МБ через сервер, Мбит/с
  local r; r=$(curl -s -o /dev/null -m 40 -w '%{speed_download} %{size_download}' -x "socks5h://127.0.0.1:$1" 'https://speed.cloudflare.com/__down?bytes=20000000')
  TESTBYTES=$(( TESTBYTES + ${r#* } ))
  awk -v s="${r%% *}" -v n="${r#* }" 'BEGIN{ if (n+0 >= 19000000) printf "%.0f", s*8/1e6 }'
}
services() {  # код ответа популярных сервисов через сервер: 2xx/3xx — открывается, 403 — не пускает IP
  local out='{}' k u c
  for k in youtube:https://www.youtube.com/ chatgpt:https://chatgpt.com/ instagram:https://www.instagram.com/ \
           telegram:https://web.telegram.org/ netflix:https://www.netflix.com/ google:https://www.google.com/; do
    c=$(curl -s -o /dev/null -m 12 -w '%{http_code}' -A 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)' -x "socks5h://127.0.0.1:$1" "${k#*:}")
    out=$(jq --arg k "${k%%:*}" --argjson c "${c:-0}" '.[$k] = $c' <<<"$out")
  done
  echo "$out"
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
[ -s "$ST/svc.json" ] || echo '{}' > "$ST/svc.json"

S='{}'
for i in 1 2; do
  H=$(eval echo "\${UP${i}_HOST:-}"); [ -n "$H" ] || continue
  P=$((10810+i)); MS=$(lat $P); MB=""
  if [ -n "$MS" ] && [ $DO_SPEED = 1 ]; then
    MB=$(speed $P)
    jq --arg k "s$i" --argjson r "$(services $P)" '.[$k] = {t: (now|floor), res: $r}' "$ST/svc.json" > "$ST/svc.new" && mv "$ST/svc.new" "$ST/svc.json"
  fi
  S=$(jq --arg k "s$i" --arg ms "$MS" --arg mb "$MB" --arg pl "$(place $P)" \
      '.[$k] = {ok: ($ms != ""), ms: ($ms|tonumber? // null), mbps: ($mb|tonumber? // null), place: $pl}' <<<"$S")
done

# трафик за интервал, МБ (замеры скорости вычитаем — это не ваш трафик)
TR=$(jq -c --argjson tb "$TESTBYTES" '
  def mb(a; b): (((.[a] // 0) + (.[b] // 0)) / 1048576);
  {direct: mb("outbound>>>direct>>>traffic>>>uplink"; "outbound>>>direct>>>traffic>>>downlink"),
   f1: mb("outbound>>>foreign1>>>traffic>>>uplink"; "outbound>>>foreign1>>>traffic>>>downlink"),
   f2: mb("outbound>>>foreign2>>>traffic>>>uplink"; "outbound>>>foreign2>>>traffic>>>downlink"),
   app: mb("inbound>>>vless-in>>>traffic>>>uplink"; "inbound>>>vless-in>>>traffic>>>downlink"),
   router: mb("inbound>>>wg-in>>>traffic>>>uplink"; "inbound>>>wg-in>>>traffic>>>downlink")}
  | .f1 = ([.f1 - ($tb/1048576), 0] | max)
  | map_values(. * 10 | round / 10)' <<<"$XS")

# сервер в Москве: процессор (по /proc/stat между запусками), память, подключения
read -r _ u n s idle io irq sirq steal _ < /proc/stat
TOT=$((u+n+s+idle+io+irq+sirq+steal)); BUSY=$((TOT-idle-io))
CPU=null; STEAL=null
if [ -s "$ST/cpu" ]; then read -r PT PB PS < "$ST/cpu"; DT=$((TOT-PT))
  [ $DT -gt 0 ] && CPU=$(( (BUSY-PB)*100/DT )) && STEAL=$(( (steal-PS)*100/DT )); fi
echo "$TOT $BUSY $steal" > "$ST/cpu"
MEM=$(free | awk '/Mem:/{printf "%.0f", ($2-$7)*100/$2}')
CONN=$(ss -tnH state established '( sport = :443 )' | awk '{print $4}' | sed 's/:[0-9]*$//' | sort -u | wc -l)
XR=$(systemctl is-active --quiet xray && echo true || echo false)
UP=$(awk '{printf "%.0f", $1/3600}' /proc/uptime)

# обслуживание: перезагрузка, обновления, резервная копия, ежедневная диагностика
RB=$([ -f /var/run/reboot-required ] && echo true || echo false)
UPD=$(/usr/lib/update-notifier/apt-check 2>&1 | tr ';' ' '); read -r UPD_ALL UPD_SEC <<<"${UPD:-0 0}"
BK=$(ls -t /root/keys-*.txt 2>/dev/null | head -1); BK_T=null; [ -n "$BK" ] && BK_T=$(stat -c %Y "$BK")
CFG_T=$(jq -r '[.[]|select(. != null)]|max // 0' <<<"[$(stat -c %Y $X/vpn.env), $(stat -c %Y $X/wg.json 2>/dev/null || echo null)]")
if [ ! -s "$ST/doctor.json" ] || [ -n "$(find "$ST/doctor.json" -mmin +1430 2>/dev/null)" ]; then
  grep -q NO_TG /root/vpn-doctor.sh 2>/dev/null || curl -fsSL https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/main/vpn/vpn-doctor.sh -o /root/vpn-doctor.sh
  if [ -f /root/vpn-doctor.sh ]; then
    FIX=0 NO_TG=1 bash /root/vpn-doctor.sh >/dev/null 2>&1
    R=/root/vpn-doctor.txt
    jq -n --arg ok "$(grep -c '^✓' $R)" --arg bad "$(grep -c '^✗' $R)" --rawfile r "$R" \
      '{t: (now|floor), ok: ($ok|tonumber), bad: ($bad|tonumber), issues: ($r | split("\n") | map(select(startswith("✗"))) | map(.[2:]))}' > "$ST/doctor.json"
  fi
fi

# доступность за месяц и трафик за месяц
M=$(date +%Y-%m); [ -s "$ST/month.json" ] || echo '{}' > "$ST/month.json"
jq --arg m "$M" --argjson s "$S" --argjson tr "$TR" '
  (if .cur.m != $m then {prev: .cur, cur: {m: $m, s1: {ok: 0, n: 0}, s2: {ok: 0, n: 0}, tr: {}}} else . end)
  | reduce ("s1","s2") as $k (.; if $s[$k] then .cur[$k].n += 1 | .cur[$k].ok += (if $s[$k].ok then 1 else 0 end) else . end)
  | .cur.tr = (reduce ($tr|to_entries[]) as $e (.cur.tr; .[$e.key] = ((.[$e.key] // 0) + $e.value)))' \
  "$ST/month.json" > "$ST/month.new" && mv "$ST/month.new" "$ST/month.json"

PT=$(jq -nc --argjson s "$S" --argjson cpu "$CPU" --argjson st "$STEAL" --argjson mem "$MEM" \
  --argjson c "$CONN" --argjson tr "$TR" \
  '{t: now|floor, s1: $s.s1, s2: $s.s2, cpu: $cpu, steal: $st, mem: $mem, conns: $c, tr: $tr}')
[ -s "$HIST" ] || echo '[]' > "$HIST"
jq --argjson p "$PT" --argjson k $KEEP '(. + [$p]) | if length > $k then .[length-$k:] else . end' "$HIST" > "$HIST.new" && mv "$HIST.new" "$HIST"

jq -n --slurpfile h "$HIST" --slurpfile svc "$ST/svc.json" --slurpfile mon "$ST/month.json" \
  --slurpfile doc <(cat "$ST/doctor.json" 2>/dev/null || echo null) \
  --argjson xr "$XR" --argjson up "$UP" --argjson rb "$RB" --argjson ua "${UPD_ALL:-0}" --argjson us "${UPD_SEC:-0}" \
  --argjson bk "$BK_T" --argjson cfg "${CFG_T:-0}" --arg mon "$(systemctl is-active vpn-monitor.timer 2>/dev/null)" \
  '{version: 2, updated: now|floor, every_min: 15,
    relay: {xray: $xr, uptime_h: $up, monitor: ($mon == "active"), reboot: $rb, updates: $ua, security: $us,
            backup_t: $bk, config_t: $cfg, doctor: $doc[0]},
    services: $svc[0], month: $mon[0], points: $h[0]}' > "$ST/out.json"
jq -n --rawfile c "$ST/out.json" '{files: {"vpn-status.json": {content: $c}}}' > "$ST/p.json"
gh PATCH "/gists/$GIST_ID" "$ST/p.json" >/dev/null && echo "Отправлено ($(jq '.points|length' "$ST/out.json") точек)" || echo "Не удалось отправить в GitHub"
