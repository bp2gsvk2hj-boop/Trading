#!/usr/bin/env bash
# Разовое сравнение двух российских серверов-посредников.
# Запуск на Яндексе (под sudo -i):  bash compare.sh 135.106.176.21 [замеров=3]
# Замеры идут на обоих серверах одновременно, в таблице — медиана.
set -uo pipefail
IP="${1:?укажите IP второго сервера}"; N="${2:-3}"
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
    awk -v s="${r%% *}" -v n="${r#* }" 'BEGIN{ printf "%s %d", (n+0 < 5e6 ? 0 : sprintf("%.0f", s*8/1e6)), n }'; }
  cpu() { read -r _ u n s i w q sq st _ < /proc/stat; local t1=$((u+n+s+i+w+q+sq+st)) i1=$((i+w)) s1=$st
    sleep 3; read -r _ u n s i w q sq st _ < /proc/stat; local t2=$((u+n+s+i+w+q+sq+st))
    awk -v dt=$((t2-t1)) -v di=$(( (i+w)-i1 )) -v ds=$((st-s1)) 'BEGIN{ if(dt<=0){print ",";exit}
      printf "%.1f,%.1f", ds*100/dt, (dt-di-ds)*100/dt }'; }
  local c1 fp ap fh ah fm am ru gu sm ym
  fp=$(pg $F); ap=$(pg $A); fh=$(lat 10811); ah=$(lat 10812)
  fm=$(dl 'https://speed.cloudflare.com/__down?bytes=20000000' -x socks5h://127.0.0.1:10811)
  am=$(dl 'https://speed.cloudflare.com/__down?bytes=20000000' -x socks5h://127.0.0.1:10812)
  # эти байты прошли через VPN-сервер — страница мониторинга вычтет их из «вашего» трафика
  [ -d /var/lib/vpn-stats ] && echo "${fm#* } ${am#* }" >> /var/lib/vpn-stats/bench-bytes
  ru=$(curl -s -m 10 -o /dev/null -w '%{http_code} %{time_starttransfer}' https://ya.ru)
  ru=$(awk -v r="$ru" 'BEGIN{split(r,a," "); if (a[1]+0>0) printf "%.0f", a[2]*1000}')
  gu=$(curl -s -m 10 -A "Mozilla/5.0" -o /dev/null -w '%{http_code}' https://www.gosuslugi.ru)
  sm=$(dl https://speedtest.selectel.ru/10MB)
  ym=$(dl https://mirror.yandex.ru/debian/dists/bookworm/main/Contents-amd64.gz -r 0-9999999)
  c1=$(cpu)
  echo "$(date -u +%FT%TZ),$h,$fp,$ap,$fh,$ah,${fm%% *},${am%% *},$ru,$gu,${sm%% *},${ym%% *},$c1"
}

if [ "${MODE:-}" = sample ]; then for i in $(seq "$N"); do sample; done; exit 0; fi

SELF=$(readlink -f "$0")
echo "Замеряю $N раза на каждом сервере, займёт 2–4 минуты…"
MODE=sample bash "$SELF" x "$N" > /tmp/cmp-a.csv &
ssh -o BatchMode=yes -o ConnectTimeout=10 root@"$IP" "MODE=sample N=$N bash -s -- x $N" < "$SELF" > /tmp/cmp-b.csv &
wait
python3 - /tmp/cmp-a.csv /tmp/cmp-b.csv <<'PY'
import csv, sys, statistics as st
names = ["ts","host","f_ping","f_loss","a_ping","a_loss","f_http","a_http","f_mbps","a_mbps","ru_ms","gu","sel_mbps","yan_mbps","steal","busy"]
rows = [list(csv.reader(open(p))) for p in sys.argv[1:]]
def med(rs, k):
    v = [float(r[names.index(k)]) for r in rs if len(r) == len(names) and r[names.index(k)] not in ("",)]
    return st.median(v) if v else None
labels = [("f_mbps","Скорость через Fornex, Мбит/с",1),("a_mbps","Скорость через Aeza, Мбит/с",1),
          ("f_http","Отклик сайта через Fornex, мс",-1),("a_http","Отклик сайта через Aeza, мс",-1),
          ("f_ping","Пинг до Fornex, мс",-1),("a_ping","Пинг до Aeza, мс",-1),
          ("f_loss","Потери до Fornex, %",-1),("a_loss","Потери до Aeza, %",-1),
          ("ru_ms","Отклик ya.ru напрямую, мс",-1),("sel_mbps","Скачивание с зеркала Selectel, Мбит/с",1),
          ("yan_mbps","Скачивание с зеркала Яндекса, Мбит/с",1),("steal","Steal процессора, %",-1),("busy","Загрузка процессора, %",-1)]
h = [r[0][1] if r and len(r[0]) > 1 else "?" for r in rows]
print(f"\n{'Показатель':40s}{h[0]:>12s}{h[1]:>12s}")
for k, t, better in labels:
    a, b = med(rows[0], k), med(rows[1], k)
    fa = "—" if a is None else f"{a:.0f}" if a >= 10 else f"{a:.1f}"
    fb = "—" if b is None else f"{b:.0f}" if b >= 10 else f"{b:.1f}"
    mark = ""
    if a is not None and b is not None and max(a, b) > 0 and abs(a - b) / max(a, b) > 0.05 and (abs(a - b) >= 1 or k not in ('f_loss','a_loss','steal','busy')):
        mark = "  ← " + (h[0] if (a - b) * better > 0 else h[1])
    print(f"{t:40s}{fa:>12s}{fb:>12s}{mark}")
print("\n← отмечен сервер, который лучше больше чем на 5%")
PY
