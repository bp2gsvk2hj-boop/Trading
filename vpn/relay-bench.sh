#!/usr/bin/env bash
# =====================================================================
#  Сравнение двух российских серверов (relay) в течение суток и дольше.
#  Запускать на Яндексе от root. Второй сервер опрашивается по SSH.
#     bash relay-bench.sh install 135.106.176.21   — включить замеры раз в 30 мин
#     relay-bench report                           — сводка в любой момент
#     relay-bench stop                             — выключить замеры
#  Каждый замер на КАЖДОМ сервере (одинаково):
#     пинг и потери до Fornex и Aeza, задержка и скорость 20 МБ через Fornex
#     и через Aeza (тем же путём, что идёт ваш трафик), доступ к российским
#     сайтам напрямую, скорость из России (Selectel и Яндекс), нагрузка CPU и steal.
#  Сам VPN не трогает. Через 3 суток выключается сам.
# =====================================================================
set -uo pipefail
CSV=/root/relay-bench.csv
CONF=/root/relay-bench.conf
BIN=/usr/local/bin/relay-bench
F=31.172.78.78; A=193.39.245.70

sample() {   # печатает одну строку CSV для текущего сервера
  local h; h=$(hostname)
  pg() { ping -c 10 -i 0.3 -W 2 -q "$1" 2>/dev/null | awk -F'[/ ,%]+' '
      /packet loss/ {for(i=1;i<=NF;i++) if($i=="packet") loss=$(i-1)}
      /rtt/ {avg=$8} END{printf "%s,%s", (avg==""?"":sprintf("%.0f",avg)), (loss==""?100:loss)}'; }
  lat() { local v=() t; for k in 1 2 3; do
      t=$(curl -s -m 10 -x "socks5h://127.0.0.1:$1" -o /dev/null -w '%{http_code} %{time_total}' https://www.gstatic.com/generate_204)
      [ "${t%% *}" = 204 ] && v+=("${t#* }"); done
    [ ${#v[@]} -eq 0 ] && { echo ""; return; }
    printf '%s\n' "${v[@]}" | sort -n | sed -n "$(( (${#v[@]}+1)/2 ))p" | awk '{printf "%.0f", $1*1000}'; }
  dl() { local r; r=$(curl -s -m 40 "${@:2}" -o /dev/null -w '%{speed_download} %{size_download}' "$1")
    awk -v s="${r%% *}" -v n="${r#* }" 'BEGIN{ if (n+0 < 5e6) print 0; else printf "%.0f", s*8/1e6 }'; }
  cpu() { read -r _ u n s i w q sq st _ < /proc/stat; local t1=$((u+n+s+i+w+q+sq+st)) i1=$((i+w)) s1=$st
    sleep 3; read -r _ u n s i w q sq st _ < /proc/stat; local t2=$((u+n+s+i+w+q+sq+st))
    awk -v dt=$((t2-t1)) -v di=$(( (i+w)-i1 )) -v ds=$((st-s1)) 'BEGIN{ if(dt<=0){print ",";exit}
      printf "%.1f,%.1f", ds*100/dt, (dt-di-ds)*100/dt }'; }
  local ru; ru=$(curl -s -m 10 -o /dev/null -w '%{http_code} %{time_starttransfer}' https://ya.ru)
  ru=$(awk -v r="$ru" 'BEGIN{split(r,a," "); if (a[1]+0>0) printf "%.0f", a[2]*1000}')
  local gu; gu=$(curl -s -m 10 -A "Mozilla/5.0" -o /dev/null -w '%{http_code}' https://www.gosuslugi.ru)
  echo "$(date -u +%FT%TZ),$h,$(pg $F),$(pg $A),$(lat 10811),$(lat 10812),$(dl 'https://speed.cloudflare.com/__down?bytes=20000000' -x socks5h://127.0.0.1:10811),$(dl 'https://speed.cloudflare.com/__down?bytes=20000000' -x socks5h://127.0.0.1:10812),$ru,$gu,$(dl https://speedtest.selectel.ru/10MB),$(dl https://mirror.yandex.ru/debian/dists/bookworm/main/Contents-amd64.gz -r 0-9999999),$(cpu)"
}

HDR="ts,host,f_ping,f_loss,a_ping,a_loss,f_http,a_http,f_mbps,a_mbps,ru_ms,gosuslugi,sel_mbps,yan_mbps,steal,busy"

run() {
  . "$CONF"
  [ -s "$CSV" ] || echo "$HDR" > "$CSV"
  sample >> "$CSV" &
  ssh -o BatchMode=yes -o ConnectTimeout=10 "root@$REMOTE" 'bash -s -- sample' < "$BIN" >> "$CSV" \
    || echo "$(date -u +%FT%TZ),REMOTE_DOWN,,100,,100,,,0,0,,000,0,0,," >> "$CSV"
  wait
  [ "$(wc -l < "$CSV")" -gt 300 ] && stop >/dev/null
}

report() {
  [ -s "$CSV" ] || { echo "Данных пока нет — первый замер через несколько минут."; exit 0; }
  python3 - "$CSV" <<'PY'
import csv, sys, statistics as st
rows = list(csv.DictReader(open(sys.argv[1])))
hosts = sorted({r["host"] for r in rows})
def nums(rs, k):
    out = []
    for r in rs:
        try: out.append(float(r[k]))
        except: pass
    return out
def q(v, p):
    v = sorted(v); return v[min(len(v)-1, int(round(p*(len(v)-1))))] if v else None
M = [("f_ping","Пинг до Fornex, мс","low"),("f_loss","Потери до Fornex, %","loss"),
     ("a_ping","Пинг до Aeza, мс","low"),("a_loss","Потери до Aeza, %","loss"),
     ("f_http","Отклик через Fornex, мс","low"),("a_http","Отклик через Aeza, мс","low"),
     ("f_mbps","Скорость через Fornex, Мбит/с","high"),("a_mbps","Скорость через Aeza, Мбит/с","high"),
     ("ru_ms","Отклик ya.ru напрямую, мс","low"),("sel_mbps","Скорость из РФ (Selectel), Мбит/с","high"),
     ("yan_mbps","Скорость из РФ (Яндекс), Мбит/с","high"),("steal","Steal CPU, %","low"),("busy","Загрузка CPU, %","low")]
print(f"Замеров: {len(rows)//max(1,len(hosts))} на сервер, с {rows[0]['ts']} по {rows[-1]['ts']}\n")
for h in hosts:
    rs = [r for r in rows if r["host"] == h]
    print(f"══ {h}  ({len(rs)} замеров) ══")
    for k, name, kind in M:
        v = nums(rs, k)
        if not v: print(f"  {name:38s} нет данных"); continue
        if kind == "loss":
            print(f"  {name:38s} среднее {st.mean(v):5.1f}   максимум {max(v):5.0f}")
        elif kind == "high":
            ok = [x for x in v if x > 0]; fails = len(v) - len(ok)
            print(f"  {name:38s} медиана {q(ok,.5) or 0:5.0f}   худшие 10% {q(ok,.1) or 0:5.0f}   сбоев {fails}")
        else:
            print(f"  {name:38s} медиана {q(v,.5):5.0f}   худшие 10% {q(v,.9):5.0f}")
    miss = sum(1 for r in rs if not r["f_http"]) ; gs = sum(1 for r in rs if r["gosuslugi"] in ("000",""))
    print(f"  {'Нет выхода через Fornex, раз':38s} {miss}")
    print(f"  {'Госуслуги недоступны напрямую, раз':38s} {gs}\n")
PY
}

stop() { systemctl disable --now relay-bench.timer 2>/dev/null; echo "Замеры выключены. Сводка: relay-bench report"; }

install() {
  [ "$(id -u)" -eq 0 ] || { echo "Запустите от root (sudo -i)"; exit 1; }
  local ip="${1:-}"; [ -n "$ip" ] || { echo "Укажите IP второго сервера: bash relay-bench.sh install IP"; exit 1; }
  cp "$0" "$BIN" && chmod +x "$BIN"
  echo "REMOTE=$ip" > "$CONF"
  ssh -o BatchMode=yes -o ConnectTimeout=10 "root@$ip" true || { echo "Нет входа по SSH на $ip без пароля"; exit 1; }
  cat > /etc/systemd/system/relay-bench.service <<U
[Unit]
Description=relay benchmark
[Service]
Type=oneshot
ExecStart=$BIN run
U
  cat > /etc/systemd/system/relay-bench.timer <<U
[Unit]
Description=relay benchmark every 30 min
[Timer]
OnCalendar=*:05,35
Persistent=false
[Install]
WantedBy=timers.target
U
  systemctl daemon-reload && systemctl enable --now relay-bench.timer >/dev/null
  echo "Первый замер прямо сейчас (около минуты)…"; run
  echo "Готово. Замеры идут раз в 30 минут на обоих серверах, через 3 суток выключатся сами."
  echo; report
}

case "${1:-}" in
  sample) sample ;;
  run) run ;;
  report) report ;;
  stop) stop ;;
  install) install "${2:-}" ;;
  *) sed -n '2,14p' "$0" ;;
esac
