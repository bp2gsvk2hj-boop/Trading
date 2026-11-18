#!/usr/bin/env bash
# =====================================================================
#  Сравнение зарубежных серверов ИЗ РОССИИ (запускать на сервере Яндекса).
#     bash test-upstream.sh 'vless://…новый#relay'     — новый сервер + текущие
#     bash test-upstream.sh                            — только текущие
#  Работающий VPN не трогает: поднимает временный Xray на отдельных портах.
#  Меряет: задержку, скорость скачивания/отдачи, долгую загрузку (≈ видео),
#  откуда выход (страна) и открываются ли Google, YouTube, ChatGPT, Netflix.
# =====================================================================
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Запустите от root (sudo -i)"; exit 1; }
CFG=/usr/local/etc/xray/config.json
link_outbound() {
  local L="$1" tag="$2" l r hp q id host port
  [[ "$L" == vless://* ]] || { echo "ссылка должна начинаться с vless://" >&2; return 1; }
  l="${L#vless://}"; l="${l%%#*}"
  id="${l%%@*}"; r="${l#*@}"
  hp="${r%%\?*}"; q="${r#*\?}"
  host="${hp%:*}"; port="${hp##*:}"
  qp() { tr '&' '\n' <<<"$q" | sed -n "s/^$1=//p" | head -1; }
  urldec() { local v="${1//+/ }"; printf '%b' "${v//%/\\x}"; }
  local sni pbk sid fp path mode type sec
  sni=$(qp sni); pbk=$(qp pbk); sid=$(qp sid); fp=$(qp fp)
  path=$(urldec "$(qp path)"); mode=$(qp mode); type=$(qp type); sec=$(qp security)
  [ "$type" = "xhttp" ] && [ "$sec" = "reality" ] || { echo "нужна ссылка VLESS + REALITY + XHTTP" >&2; return 1; }
  [ -n "$id" ] && [ -n "$host" ] && [ -n "$pbk" ] && [ -n "$sni" ] || { echo "в ссылке не хватает параметров" >&2; return 1; }
  [[ "$port" =~ ^[0-9]+$ ]] || { echo "не удалось разобрать порт" >&2; return 1; }
  jq -n --arg tag "$tag" --arg h "$host" --argjson p "$port" --arg id "$id" \
    --arg sni "$sni" --arg pbk "$pbk" --arg sid "$sid" --arg fp "${fp:-chrome}" \
    --arg path "${path:-/}" --arg mode "${mode:-auto}" '
    {tag:$tag, protocol:"vless",
     settings:{vnext:[{address:$h, port:$p, users:[{id:$id, encryption:"none"}]}]},
     streamSettings:{network:"xhttp", xhttpSettings:{path:$path, mode:$mode},
       security:"reality",
       realitySettings:{serverName:$sni, fingerprint:$fp, publicKey:$pbk, shortId:$sid}}}'
}

OUTS='[]'; NAMES=()
add() { OUTS=$(jq --argjson o "$1" '. + [$o]' <<<"$OUTS"); NAMES+=("$2"); }
for t in $([ "${ONLY:-0}" = 1 ] || echo foreign1 foreign2); do
  o=$(jq -c --arg t "$t" '.outbounds[]|select(.tag==$t)' "$CFG" 2>/dev/null)
  [ -n "$o" ] && add "$(jq -c --arg n "t${#NAMES[@]}" '.tag=$n' <<<"$o")" \
    "$([ $t = foreign1 ] && echo 'Текущий основной' || echo 'Текущий резерв') ($(jq -r '.settings.vnext[0].address' <<<"$o"))"
done
for L in "$@"; do
  o=$(link_outbound "$L" "t${#NAMES[@]}") || exit 1
  add "$o" "Новый ($(jq -r '.settings.vnext[0].address' <<<"$o"))"
done
[ ${#NAMES[@]} -gt 0 ] || { echo "Нечего проверять"; exit 1; }

T=$(mktemp -d); IN='[]'; RU='[]'
for i in "${!NAMES[@]}"; do
  IN=$(jq --argjson i "$i" '. + [{tag:"s\($i)", listen:"127.0.0.1", port:(10900+$i), protocol:"socks", settings:{udp:false}}]' <<<"$IN")
  RU=$(jq --argjson i "$i" '. + [{type:"field", inboundTag:["s\($i)"], outboundTag:"t\($i)"}]' <<<"$RU")
done
jq -n --argjson i "$IN" --argjson o "$OUTS" --argjson r "$RU" '{log:{loglevel:"error"}, inbounds:$i, outbounds:$o, routing:{rules:$r}}' > "$T/c.json"
xray run -test -config "$T/c.json" >/dev/null 2>&1 || { echo "Временный конфиг не принят Xray — проверьте ссылку"; exit 1; }
xray run -config "$T/c.json" >/dev/null 2>&1 & XP=$!
trap 'kill $XP 2>/dev/null; rm -rf "$T"' EXIT
sleep 2

c() { curl -s -m "${2:-15}" -x "socks5h://127.0.0.1:$P" "${@:3}" "$1"; }
code() { c "$1" 12 -o /dev/null -w '%{http_code}'; }
for i in "${!NAMES[@]}"; do
  P=$((10900+i)); H=$(jq -r ".[$i].settings.vnext[0].address" <<<"$OUTS")
  echo; echo "══ ${NAMES[$i]} ══"
  tc=$(curl -s -m 5 -o /dev/null -w '%{time_connect}' "telnet://$H:443" 2>/dev/null </dev/null || true)
  echo "  TCP до сервера:        $(awk -v t="${tc:-0}" 'BEGIN{ if (t+0==0) print "НЕДОСТУПЕН из России"; else printf "%.0f мс", t*1000 }')"
  L=(); for k in 1 2 3 4 5; do v=$(c https://www.gstatic.com/generate_204 10 -o /dev/null -w '%{http_code} %{time_total}'); [ "${v%% *}" = 204 ] && L+=("${v#* }"); done
  if [ ${#L[@]} -eq 0 ]; then echo "  ✗ VPN через сервер НЕ работает"; continue; fi
  echo "  Задержка (медиана 5): $(printf '%s\n' "${L[@]}" | sort -n | sed -n "$(( (${#L[@]}+1)/2 ))p" | awk '{printf "%.0f мс", $1*1000}')  (успешно ${#L[@]}/5)"
  d=$(c "https://speed.cloudflare.com/__down?bytes=50000000" 60 -o /dev/null -w '%{speed_download} %{size_download}')
  echo "  Скачивание 50 МБ:      $(awk -v s="${d%% *}" -v n="${d#* }" 'BEGIN{ if (n<50000000) printf "оборвалось на %.1f МБ ✗", n/1e6; else printf "%.0f Мбит/с", s*8/1e6 }')"
  dd if=/dev/urandom of="$T/up" bs=1M count=10 2>/dev/null
  u=$(c "https://speed.cloudflare.com/__up" 60 -o /dev/null -w '%{speed_upload}' --data-binary @"$T/up")
  echo "  Отдача 10 МБ:          $(awk -v s="${u:-0}" 'BEGIN{printf "%.0f Мбит/с", s*8/1e6}')"
  echo "  Выход:                 $(c https://ipinfo.io/json 10 | jq -r '"\(.ip)  \(.country) \(.city)  \(.org)"' 2>/dev/null)"
  printf '  Сервисы:               '
  for s in google.com:https://www.google.com/ youtube:https://www.youtube.com/ chatgpt:https://chatgpt.com/ netflix:https://www.netflix.com/; do
    r=$(code "${s#*:}"); case "$r" in 2??|3??) printf '%s ✓  ' "${s%%:*}";; 403) printf '%s ✗(403)  ' "${s%%:*}";; *) printf '%s ✗(%s)  ' "${s%%:*}" "$r";; esac
  done; echo
done
echo; echo "Чем меньше задержка и выше скорость — тем лучше. Сервисы должны быть ✓ (403 = сервис не пускает этот IP)."
