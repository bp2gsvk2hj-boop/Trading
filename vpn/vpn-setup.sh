#!/usr/bin/env bash
# =====================================================================
#  VPN-сервер: Xray, VLESS + REALITY + XHTTP (порт 443)
#  Для Ubuntu 22.04 / 24.04. Запуск от root:
#
#     sudo bash vpn-setup.sh
#
#  Необязательные параметры (переменные окружения):
#     CLIENTS="phone tablet laptop"  имена устройств, по одному UUID на каждое
#                                    (_ в имени показывается в приложении как пробел)
#     SNI=www.example.com            сайт для маскировки (иначе подбирается сам)
#     SERVER_IP=1.2.3.4              внешний IP (иначе определяется сам)
#     PORT=8443                      порт VPN (по умолчанию 443)
#     KEEP=1                         перенастроить, СОХРАНИВ ключи и ссылки клиентов
#                                    (например, добавить резервный сервер)
#     FORCE=1                        переустановить с новыми ключами
#                                    (старые ссылки перестанут работать)
#     UPSTREAM='vless://...'         режим ПРОМЕЖУТОЧНОГО сервера (в РФ):
#                                    ссылка на зарубежный сервер (vpnctl add relay
#                                    на нём). Российские сайты выходят напрямую
#                                    с этого сервера, остальное — через зарубежный.
#     UPSTREAM2='vless://...'        резервный зарубежный сервер: Xray сам
#                                    переключается на него, если основной недоступен
#     TG_TOKEN=123:ABC               токен Telegram-бота для уведомлений
#                                    (мониторинг включается в режиме UPSTREAM)
#
#  После установки управление клиентами: vpnctl help
# =====================================================================
set -euo pipefail

CLIENTS="${CLIENTS:-phone tablet laptop}"
SNI="${SNI:-}"
SERVER_IP="${SERVER_IP:-}"
FORCE="${FORCE:-0}"
KEEP="${KEEP:-0}"
PORT="${PORT:-443}"
UPSTREAM="${UPSTREAM:-}"
UPSTREAM2="${UPSTREAM2:-}"
TG_TOKEN="${TG_TOKEN:-}"
TG_CHAT="${TG_CHAT:-}"
SNI_CANDIDATES="www.samsung.com www.nvidia.com www.amd.com www.intel.com www.asus.com"

XRAY_DIR=/usr/local/etc/xray
CFG="$XRAY_DIR/config.json"
NEW="$XRAY_DIR/config.new.json"   # Xray определяет формат по расширению
ENV_FILE="$XRAY_DIR/vpn.env"
LINKS_FILE=/root/vpn-links.txt

say()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }
trap 'printf "\033[1;31m[x] Ошибка в строке %s: %s\033[0m\n" "$LINENO" "$BASH_COMMAND" >&2' ERR

# ---------- 0. Проверки ----------
[ "$(id -u)" -eq 0 ] || die "Запустите от root: sudo bash $0"
grep -qi ubuntu /etc/os-release || warn "Скрипт рассчитан на Ubuntu, продолжаю на свой риск."
if [ -f "$ENV_FILE" ] && [ "$FORCE" != "1" ] && [ "$KEEP" != "1" ]; then
  die "VPN уже установлен. Ссылки: vpnctl all. Перенастроить с сохранением ссылок: KEEP=1 bash $0; с новыми ключами: FORCE=1 bash $0"
fi
if [ "$KEEP" = "1" ]; then
  [ -f "$ENV_FILE" ] && [ -f "$CFG" ] || die "KEEP=1: нет прежней установки для сохранения"
  OLD_ENV=$(cat "$ENV_FILE")
  oldv() { sed -n "s/^$1=//p" <<<"$OLD_ENV" | head -1; }
fi
for n in $CLIENTS; do
  [[ "$n" =~ ^[A-Za-z0-9_-]+$ ]] || die "Имя клиента '$n': только латиница, цифры, - и _"
done

# Разбор ссылки vless:// в outbound Xray (JSON). $1 — ссылка, $2 — тег
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
# ---------- 1. Пакеты ----------
say "Устанавливаю пакеты"
export DEBIAN_FRONTEND=noninteractive
# Новые VPS часто сами ставят обновления в фоне: ждём их и чиним прерванный dpkg
APT_OPTS="-o DPkg::Lock::Timeout=600"
dpkg --configure -a || true
apt-get $APT_OPTS update -qq
apt-get $APT_OPTS install -y -qq curl jq qrencode openssl ufw ca-certificates iproute2 >/dev/null

if ss -ltnp "sport = :$PORT" | grep -q LISTEN && ! ss -ltnp "sport = :$PORT" | grep -q xray; then
  ss -ltnp "sport = :$PORT"
  die "Порт $PORT занят другой программой (см. выше). Освободите его и запустите снова."
fi

if [ -n "$UPSTREAM" ]; then
  UP1_JSON=$(link_outbound "$UPSTREAM" foreign1) || die "UPSTREAM: ошибка в ссылке"
  UP_HOST=$(jq -r '.settings.vnext[0].address' <<<"$UP1_JSON"); UP_PORT=$(jq -r '.settings.vnext[0].port' <<<"$UP1_JSON")
  UP2_HOST=""
  if [ -n "$UPSTREAM2" ]; then
    UP2_JSON=$(link_outbound "$UPSTREAM2" foreign2) || die "UPSTREAM2: ошибка в ссылке"
    UP2_HOST=$(jq -r '.settings.vnext[0].address' <<<"$UP2_JSON"); UP2_PORT=$(jq -r '.settings.vnext[0].port' <<<"$UP2_JSON")
  fi
elif [ -n "$UPSTREAM2" ]; then
  die "UPSTREAM2 задаётся только вместе с UPSTREAM"
fi

# ---------- 2. Xray ----------
say "Устанавливаю Xray (официальный скрипт XTLS)"
bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install >/dev/null
command -v xray >/dev/null || die "Xray не установился"
XVER=$(xray version | awk 'NR==1{print $2}')
if [ "$(printf '%s\n' 24.12.0 "$XVER" | sort -V | head -1)" != "24.12.0" ]; then
  die "Xray $XVER слишком старый для XHTTP (нужен 24.12+)"
fi
echo "Xray $XVER"

# ---------- 3. Ключи и параметры ----------
if [ "$KEEP" = "1" ]; then
say "Сохраняю прежние ключи и ссылки"
PRIV=$(jq -r '.inbounds[0].streamSettings.realitySettings.privateKey' "$CFG")
SID=$(jq -r '.inbounds[0].streamSettings.realitySettings.shortIds[0]' "$CFG")
XPATH=$(jq -r '.inbounds[0].streamSettings.xhttpSettings.path' "$CFG")
SNI=$(jq -r '.inbounds[0].streamSettings.realitySettings.serverNames[0]' "$CFG")
PUB=$(oldv PUB); SERVER_IP=${SERVER_IP:-$(oldv SERVER_IP)}
[ -z "$TG_TOKEN" ] && { TG_TOKEN=$(oldv TG_TOKEN); TG_CHAT=${TG_CHAT:-$(oldv TG_CHAT)}; }
[ "$TG_TOKEN" = "$(oldv TG_TOKEN)" ] && TG_CHAT=${TG_CHAT:-$(oldv TG_CHAT)}
[ ${#PRIV} -ge 40 ] && [ ${#PUB} -ge 40 ] && [ -n "$SID" ] && [ -n "$XPATH" ] || die "KEEP=1: не удалось прочитать прежние ключи"
else
say "Генерирую ключи"
KEYS=$(xray x25519)
# Старый формат: "Private key / Public key", новый: "PrivateKey / Password"
PRIV=$(sed -n '1s/^[^:]*:[[:space:]]*//p' <<<"$KEYS" | tr -d '\r ')
PUB=$(sed -n '2s/^[^:]*:[[:space:]]*//p' <<<"$KEYS" | tr -d '\r ')
[ ${#PRIV} -ge 40 ] && [ ${#PUB} -ge 40 ] || die "Не удалось разобрать вывод 'xray x25519': $KEYS"
SID=$(openssl rand -hex 8)
XPATH="/"   # путь "/" — совместим с любыми клиентами (Amnezia не передаёт path из ссылки)
fi

# ---------- 4. Сайт для маскировки ----------
sni_ok() {  # нужен TLS 1.3 и HTTP/2
  [ "$(curl -s -o /dev/null -w '%{http_version}' --tlsv1.3 --http2 --max-time 7 "https://$1/" || true)" = "2" ]
}
say "Проверяю сайт для маскировки (TLS 1.3 + HTTP/2)"
if [ -n "$SNI" ]; then
  sni_ok "$SNI" || die "$SNI не поддерживает TLS 1.3 + HTTP/2 или недоступен с сервера"
else
  for c in $SNI_CANDIDATES; do
    if sni_ok "$c"; then SNI="$c"; break; fi
    echo "  $c — не подходит"
  done
  [ -n "$SNI" ] || die "Ни один сайт не подошёл. Укажите вручную: SNI=сайт bash $0"
fi
echo "SNI: $SNI"

# ---------- 5. Внешний IP ----------
if [ -z "$SERVER_IP" ]; then
  SERVER_IP=$(curl -4 -fsS --max-time 6 https://api.ipify.org || curl -4 -fsS --max-time 6 https://ifconfig.me || true)
fi
[[ "$SERVER_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Не удалось определить IP. Укажите: SERVER_IP=x.x.x.x bash $0"
echo "IP: $SERVER_IP"

# ---------- 6. Конфиг Xray ----------
say "Пишу конфигурацию"
if [ "$KEEP" = "1" ]; then
  CLIENTS_JSON=$(jq -c '.inbounds[0].settings.clients' "$CFG")
  CLIENTS=$(jq -r '.[].email' <<<"$CLIENTS_JSON" | tr '\n' ' ')
else
  CLIENTS_JSON='[]'
  for n in $CLIENTS; do
    CLIENTS_JSON=$(jq --arg id "$(xray uuid)" --arg n "$n" '. + [{id:$id, email:$n}]' <<<"$CLIENTS_JSON")
  done
fi

BLOCK_RULES='[
  {"type":"field","ip":["geoip:private"],"outboundTag":"block"},
  {"type":"field","protocol":["bittorrent"],"outboundTag":"block"}]'
EXTRA_IN='[]'; BALANCERS='[]'; OBS='null'
if [ -z "$UPSTREAM" ]; then
  OUTBOUNDS='[{"tag":"direct","protocol":"freedom","settings":{"domainStrategy":"UseIPv4"}},
              {"tag":"block","protocol":"blackhole"}]'
  ru_rules() { echo "$BLOCK_RULES"; }
  RULES="$BLOCK_RULES"
else
  # Локальные SOCKS-входы для мониторинга (только 127.0.0.1)
  EXTRA_IN='[
    {"tag":"mon-tg","listen":"127.0.0.1","port":10810,"protocol":"socks","settings":{"udp":false}},
    {"tag":"mon1","listen":"127.0.0.1","port":10811,"protocol":"socks","settings":{"udp":false}}]'
  OUTBOUNDS=$(jq -n --argjson f1 "$UP1_JSON" '[$f1,
    {tag:"direct", protocol:"freedom", settings:{domainStrategy:"UseIPv4"}},
    {tag:"block",  protocol:"blackhole"}]')
  DEFAULT='{"outboundTag":"foreign1"}'
  MON_RULES='[{"type":"field","inboundTag":["mon1"],"outboundTag":"foreign1"}]'
  if [ -n "$UPSTREAM2" ]; then
    # Основной foreign1; если он недоступен (проверка раз в 30 с) — резерв foreign2
    EXTRA_IN=$(jq '. + [{"tag":"mon2","listen":"127.0.0.1","port":10812,"protocol":"socks","settings":{"udp":false}}]' <<<"$EXTRA_IN")
    OUTBOUNDS=$(jq --argjson f2 "$UP2_JSON" '.[:1] + [$f2] + .[1:]' <<<"$OUTBOUNDS")
    BALANCERS='[{"tag":"proxy","selector":["foreign1"],"strategy":{"type":"leastPing"},"fallbackTag":"foreign2"}]'
    OBS='{"subjectSelector":["foreign1"],"probeURL":"https://www.gstatic.com/generate_204","probeInterval":"30s","enableConcurrency":true}'
    DEFAULT='{"balancerTag":"proxy"}'
    MON_RULES=$(jq '. + [{"type":"field","inboundTag":["mon2"],"outboundTag":"foreign2"}]' <<<"$MON_RULES")
  fi
  # WireGuard-вход для Keenetic (создаётся keenetic.sh) сохраняется при перенастройке
  if [ -f "$XRAY_DIR/wg.json" ]; then
    EXTRA_IN=$(jq --slurpfile w "$XRAY_DIR/wg.json" '. + $w' <<<"$EXTRA_IN")
  fi
  ru_rules() {  # $1 = 1 — с geosite:category-ru, 0 — без него
    jq -n --argjson block "$BLOCK_RULES" --argjson mon "$MON_RULES" --argjson def "$DEFAULT" --argjson geo "$1" '
      $block + $mon
      + [({type:"field", inboundTag:["mon-tg"]} + $def)]
      + [
        {type:"field", outboundTag:"direct",
         domain:((if $geo==1 then ["geosite:category-ru"] else [] end)
                 + ["regexp:\\.ru$","regexp:\\.su$","regexp:\\.xn--p1ai$"])},
        {type:"field", outboundTag:"direct", ip:["geoip:ru"]}
      ]
      + [({type:"field", network:"tcp,udp"} + $def)]'
  }
  RULES=$(ru_rules 1)
fi

build_cfg() {
jq -n \
  --argjson clients "$CLIENTS_JSON" \
  --arg sni "$SNI" --arg priv "$PRIV" --arg sid "$SID" --arg path "$XPATH" \
  --argjson port "$PORT" --argjson outbounds "$OUTBOUNDS" --argjson rules "$RULES" \
  --argjson extra "$EXTRA_IN" --argjson bal "$BALANCERS" --argjson obs "$OBS" '
{
  log: {loglevel: "warning"},
  inbounds: ([{
    tag: "vless-in", listen: "0.0.0.0", port: $port, protocol: "vless",
    settings: {clients: $clients, decryption: "none"},
    streamSettings: {
      network: "xhttp",
      xhttpSettings: {path: $path, mode: "auto"},
      security: "reality",
      realitySettings: {
        show: false, target: ($sni + ":443"), xver: 0,
        serverNames: [$sni], privateKey: $priv, shortIds: [$sid]
      }
    },
    sniffing: {enabled: true, destOverride: ["http","tls","quic"], routeOnly: true}
  }] + $extra),
  outbounds: $outbounds,
  routing: ({domainStrategy: "IPIfNonMatch", rules: $rules}
            + (if ($bal|length) > 0 then {balancers: $bal} else {} end))
} + (if $obs != null then {observatory: $obs} else {} end)' > "$NEW"
}
[ -f "$CFG" ] && cp "$CFG" "$CFG.bak.$(date +%s)"
build_cfg
if ! TEST_OUT=$(xray run -test -config "$NEW" 2>&1); then
  if [ -n "$UPSTREAM" ] && grep -qi "category-ru" <<<"$TEST_OUT"; then
    warn "В geosite нет списка category-ru — российские сайты определяю по доменам .ru/.su/.рф и IP"
    RULES=$(ru_rules 0); build_cfg
    TEST_OUT=$(xray run -test -config "$NEW" 2>&1) || { echo "$TEST_OUT" | tail -15; die "Xray отклонил конфиг: $NEW (причина выше)"; }
  else
    echo "$TEST_OUT" | tail -15
    die "Xray отклонил конфиг: $NEW (причина выше)"
  fi
fi
mv "$NEW" "$CFG"
chmod 644 "$CFG"

cat > "$ENV_FILE" <<EOF
SERVER_IP=$SERVER_IP
PORT=$PORT
SNI=$SNI
PUB=$PUB
SID=$SID
XPATH=$XPATH
EOF
if [ -n "$UPSTREAM" ]; then
  cat >> "$ENV_FILE" <<EOF
UP1_HOST=$UP_HOST
UP2_HOST=$UP2_HOST
TG_TOKEN=$TG_TOKEN
TG_CHAT=$TG_CHAT
EOF
fi
chmod 600 "$ENV_FILE"

# ---------- 7. Утилита управления vpnctl ----------
cat > /usr/local/bin/vpnctl <<'VPNCTL'
#!/usr/bin/env bash
# Управление клиентами VPN: vpnctl help
set -euo pipefail
CFG=/usr/local/etc/xray/config.json
NEW=/usr/local/etc/xray/config.new.json
. /usr/local/etc/xray/vpn.env
[ "$(id -u)" -eq 0 ] || { echo "Нужен root: sudo vpnctl $*"; exit 1; }

uuid_of() { jq -r --arg n "$1" '.inbounds[0].settings.clients[] | select(.email==$n) | .id' "$CFG"; }
names()   { jq -r '.inbounds[0].settings.clients[].email' "$CFG"; }
link() {
  local id; id=$(uuid_of "$1"); [ -n "$id" ] || { echo "Нет клиента '$1'"; exit 1; }
  echo "vless://${id}@${SERVER_IP}:${PORT}?encryption=none&security=reality&sni=${SNI}&fp=chrome&pbk=${PUB}&sid=${SID}&type=xhttp&path=%2F${XPATH#/}&mode=auto#${1//_/%20}"
}
apply() {
  xray run -test -config "$NEW" >/tmp/xray-test.log 2>&1 || { tail -15 /tmp/xray-test.log; echo "Ошибка в конфиге, изменения не применены"; rm -f "$NEW"; exit 1; }
  cp "$CFG" "$CFG.bak"; mv "$NEW" "$CFG"; chmod 644 "$CFG"
  systemctl restart xray
}
show() { echo; echo "── $1 ──"; link "$1"; qrencode -t ansiutf8 "$(link "$1")"; }

case "${1:-help}" in
  list) names ;;
  link) link "${2:?имя}" ;;
  qr)   show "${2:?имя}" ;;
  all)  for n in $(names); do show "$n"; done ;;
  add)
    n="${2:?имя}"
    [[ "$n" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "Только латиница, цифры, - и _"; exit 1; }
    [ -z "$(uuid_of "$n")" ] || { echo "Клиент '$n' уже есть"; exit 1; }
    jq --arg id "$(xray uuid)" --arg n "$n" '.inbounds[0].settings.clients += [{id:$id, email:$n}]' "$CFG" > "$NEW"
    apply; show "$n" ;;
  del)
    n="${2:?имя}"; [ -n "$(uuid_of "$n")" ] || { echo "Нет клиента '$n'"; exit 1; }
    jq --arg n "$n" 'del(.inbounds[0].settings.clients[] | select(.email==$n))' "$CFG" > "$NEW"
    apply; echo "Клиент '$n' удалён, его ссылка больше не работает" ;;
  status) systemctl --no-pager status xray | head -5 ;;
  *) cat <<H
vpnctl list          — список устройств
vpnctl qr ИМЯ        — ссылка и QR-код устройства
vpnctl all           — ссылки и QR всех устройств
vpnctl add ИМЯ       — добавить устройство (новый UUID)
vpnctl del ИМЯ       — отключить устройство
vpnctl status        — состояние Xray
H
  ;;
esac
VPNCTL
chmod 755 /usr/local/bin/vpnctl

# ---------- 8. Сеть: BBR и файрвол ----------
say "Включаю BBR и настраиваю файрвол"
cat > /etc/sysctl.d/99-vpn-bbr.conf <<EOF
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
EOF
sysctl --system >/dev/null 2>&1 || true

# Порт текущей SSH-сессии надёжнее всего; запасной путь — sshd -T (падает на Ubuntu 24.04 без /run/sshd)
SSH_PORT=$(echo "${SSH_CONNECTION:-}" | awk '{print $4}')
[ -n "$SSH_PORT" ] || SSH_PORT=$( { sshd -T 2>/dev/null || true; } | awk '/^port /{print $2; exit}')
SSH_PORT=${SSH_PORT:-22}
ufw allow "$SSH_PORT/tcp" >/dev/null
ufw allow "$PORT/tcp" >/dev/null
[ -f "$XRAY_DIR/wg.json" ] && ufw allow "$(jq -r .port "$XRAY_DIR/wg.json")/udp" >/dev/null
ufw --force enable >/dev/null
echo "Открыты порты: SSH $SSH_PORT/tcp, VPN $PORT/tcp"

# ---------- 9. Запуск ----------
say "Запускаю Xray"
systemctl enable xray >/dev/null 2>&1
systemctl restart xray
sleep 2
systemctl is-active --quiet xray || { journalctl -u xray -n 30 --no-pager; die "Xray не запустился"; }

if [ -n "$UPSTREAM" ]; then
  # ---------- Мониторинг и уведомления в Telegram ----------
  cat > /usr/local/bin/vpn-monitor <<'MON'
#!/usr/bin/env bash
# Проверяет зарубежные серверы ИЗ РОССИИ (через этот сервер) и шлёт уведомления в Telegram.
# vpn-monitor          — проверка (запускается таймером раз в 15 минут)
# vpn-monitor status   — показать состояние сейчас
# vpn-monitor test     — отправить тестовое сообщение
. /usr/local/etc/xray/vpn.env
ST=/var/lib/vpn-monitor; mkdir -p "$ST"
probe() { [ "$(curl -s -o /dev/null -m 12 -w '%{http_code}' -x "socks5h://127.0.0.1:$1" https://www.gstatic.com/generate_204)" = "204" ]; }
tg() {
  [ -n "${TG_TOKEN:-}" ] && [ -n "${TG_CHAT:-}" ] || return 0
  local via
  for via in "socks5h://127.0.0.1:10810" ""; do
    curl -s -m 20 ${via:+-x "$via"} "https://api.telegram.org/bot$TG_TOKEN/sendMessage" \
      --data-urlencode "chat_id=$TG_CHAT" --data-urlencode "text=$1" | grep -q '"ok":true' && return 0
  done
  echo "$(date '+%F %T') $1" >> "$ST/unsent.log"
}
NAMES=("" "VPS 1 (основной, ${UP1_HOST})" "VPS 2 (резерв, ${UP2_HOST})")
N=1; [ -n "${UP2_HOST:-}" ] && N=2
case "${1:-}" in
  status) for i in $(seq 1 $N); do if probe $((10810+i)); then echo "✓ ${NAMES[$i]} доступен"; else echo "✗ ${NAMES[$i]} НЕ доступен"; fi; done; exit 0 ;;
  test)   tg "✅ Мониторинг VPN работает ($(hostname)). Проверяю серверы из России раз в 15 минут, пишу только при изменениях."; echo "Отправлено"; exit 0 ;;
esac
for i in $(seq 1 $N); do
  if probe $((10810+i)) || { sleep 20; probe $((10810+i)); } || { sleep 20; probe $((10810+i)); }; then
    [ "$(cat "$ST/s$i" 2>/dev/null)" = down ] && {
      echo up > "$ST/s$i"
      if [ "$i" = 1 ] && [ "$N" = 2 ]; then tg "🟢 ${NAMES[1]} снова доступен из России. Трафик вернулся на основной сервер."
      else tg "🟢 ${NAMES[$i]} снова доступен из России."; fi; }
    echo 0 > "$ST/f$i"
  else
    f=$(( $(cat "$ST/f$i" 2>/dev/null || echo 0) + 1 )); echo $f > "$ST/f$i"
    if [ $f -eq 1 ]; then   # три неудачные попытки за минуту = не случайный сбой
      echo down > "$ST/s$i"
      if [ "$i" = 1 ] && [ "$N" = 2 ] && [ "$(cat "$ST/s2" 2>/dev/null)" != down ]; then
        tg "🔴 ${NAMES[1]} недоступен из России. Трафик автоматически переключён на ${NAMES[2]}. Смените IP основного сервера."
      elif [ "$i" = 1 ] && [ "$N" = 1 ]; then
        tg "🔴 ${NAMES[1]} недоступен из России — VPN не работает. Смените IP сервера."
      else
        tg "🔴 ${NAMES[$i]} недоступен из России."
      fi
    fi
  fi
done
if [ "$N" = 2 ] && [ "$(cat "$ST/s1" 2>/dev/null)" = down ] && [ "$(cat "$ST/s2" 2>/dev/null)" = down ] && [ ! -f "$ST/both" ]; then
  touch "$ST/both"; tg "⛔ Оба зарубежных сервера недоступны из России. VPN не работает."
elif [ -f "$ST/both" ] && { [ "$(cat "$ST/s1")" != down ] || [ "$(cat "$ST/s2")" != down ]; }; then rm -f "$ST/both"; fi
MON
  chmod 755 /usr/local/bin/vpn-monitor
  cat > /etc/systemd/system/vpn-monitor.service <<'EOF'
[Unit]
Description=Проверка зарубежных VPN-серверов
[Service]
Type=oneshot
ExecStart=/usr/local/bin/vpn-monitor
EOF
  cat > /etc/systemd/system/vpn-monitor.timer <<'EOF'
[Unit]
Description=Проверка зарубежных VPN-серверов раз в 15 минут
[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now vpn-monitor.timer >/dev/null 2>&1

  say "Проверяю зарубежные серверы (через этот сервер)"
  sleep 3
  vpn-monitor status || true

  if [ -n "$TG_TOKEN" ] && [ -z "$TG_CHAT" ]; then
    say "Подключаю Telegram"
    echo "Откройте вашего бота в Telegram и отправьте ему любое сообщение (например, «привет»). Жду до 3 минут…"
    for i in $(seq 1 36); do
      TG_CHAT=$(curl -s -m 10 -x socks5h://127.0.0.1:10810 "https://api.telegram.org/bot$TG_TOKEN/getUpdates" \
        | jq -r '[.result[]? | (.message // .channel_post) | .chat.id] | last // empty' 2>/dev/null || true)
      [ -n "$TG_CHAT" ] && break
      sleep 5
    done
    if [ -n "$TG_CHAT" ]; then
      sed -i "s/^TG_CHAT=.*/TG_CHAT=$TG_CHAT/" "$ENV_FILE"
      vpn-monitor test >/dev/null && echo "Telegram подключён — тестовое сообщение отправлено."
    else
      warn "Не дождался сообщения боту. Позже: отправьте боту сообщение и выполните FORCE=1 повторно, либо пропишите TG_CHAT в $ENV_FILE"
    fi
  elif [ -n "$TG_TOKEN" ]; then
    vpn-monitor test >/dev/null && echo "Тестовое сообщение в Telegram отправлено."
  else
    echo "Уведомления в Telegram не настроены (не передан TG_TOKEN). Мониторинг работает, состояние: vpn-monitor status"
  fi
fi

# ---------- 10. Ссылки ----------
: > "$LINKS_FILE"; chmod 600 "$LINKS_FILE"
for n in $CLIENTS; do vpnctl link "$n" >> "$LINKS_FILE"; done

say "Готово! VLESS + REALITY + XHTTP на $SERVER_IP:$PORT (маскировка: $SNI)"
if [ -n "$UPSTREAM" ]; then
  echo "Режим промежуточного сервера: российские сайты — напрямую отсюда, остальное — через $UP_HOST${UP2_HOST:+ (резерв: $UP2_HOST)}"
fi
vpnctl all
cat <<EOF

Ссылки сохранены в $LINKS_FILE (не публикуйте их: это ключи доступа).
Импорт: скопируйте ссылку или отсканируйте QR в Happ / v2RayTun / v2rayNG / v2rayN.

Управление:  vpnctl help
EOF
