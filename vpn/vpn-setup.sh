#!/usr/bin/env bash
# =====================================================================
#  VPN-сервер: Xray, VLESS + REALITY + XHTTP (порт 443)
#  Для Ubuntu 22.04 / 24.04. Запуск от root:
#
#     sudo bash vpn-setup.sh
#
#  Необязательные параметры (переменные окружения):
#     CLIENTS="phone tablet laptop"  имена устройств, по одному UUID на каждое
#     SNI=www.example.com            сайт для маскировки (иначе подбирается сам)
#     SERVER_IP=1.2.3.4              внешний IP (иначе определяется сам)
#     PORT=8443                      порт VPN (по умолчанию 443)
#     FORCE=1                        переустановить с новыми ключами
#                                    (старые ссылки перестанут работать)
#
#  После установки управление клиентами: vpnctl help
# =====================================================================
set -euo pipefail

CLIENTS="${CLIENTS:-phone tablet laptop}"
SNI="${SNI:-}"
SERVER_IP="${SERVER_IP:-}"
FORCE="${FORCE:-0}"
PORT="${PORT:-443}"
SNI_CANDIDATES="www.samsung.com www.nvidia.com www.amd.com www.intel.com www.asus.com"

XRAY_DIR=/usr/local/etc/xray
CFG="$XRAY_DIR/config.json"
NEW="$XRAY_DIR/config.new.json"   # Xray определяет формат по расширению
ENV_FILE="$XRAY_DIR/vpn.env"
LINKS_FILE=/root/vpn-links.txt

say()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

# ---------- 0. Проверки ----------
[ "$(id -u)" -eq 0 ] || die "Запустите от root: sudo bash $0"
grep -qi ubuntu /etc/os-release || warn "Скрипт рассчитан на Ubuntu, продолжаю на свой риск."
if [ -f "$ENV_FILE" ] && [ "$FORCE" != "1" ]; then
  die "VPN уже установлен. Ссылки: vpnctl all. Переустановка с новыми ключами: FORCE=1 bash $0"
fi
for n in $CLIENTS; do
  [[ "$n" =~ ^[A-Za-z0-9_-]+$ ]] || die "Имя клиента '$n': только латиница, цифры, - и _"
done

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
say "Генерирую ключи"
KEYS=$(xray x25519)
# Старый формат: "Private key / Public key", новый: "PrivateKey / Password"
PRIV=$(sed -n '1s/^[^:]*:[[:space:]]*//p' <<<"$KEYS" | tr -d '\r ')
PUB=$(sed -n '2s/^[^:]*:[[:space:]]*//p' <<<"$KEYS" | tr -d '\r ')
[ ${#PRIV} -ge 40 ] && [ ${#PUB} -ge 40 ] || die "Не удалось разобрать вывод 'xray x25519': $KEYS"
SID=$(openssl rand -hex 8)
XPATH="/$(openssl rand -hex 6)"

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
CLIENTS_JSON='[]'
for n in $CLIENTS; do
  CLIENTS_JSON=$(jq --arg id "$(xray uuid)" --arg n "$n" '. + [{id:$id, email:$n}]' <<<"$CLIENTS_JSON")
done

[ -f "$CFG" ] && cp "$CFG" "$CFG.bak.$(date +%s)"
jq -n \
  --argjson clients "$CLIENTS_JSON" \
  --arg sni "$SNI" --arg priv "$PRIV" --arg sid "$SID" --arg path "$XPATH" \
  --argjson port "$PORT" '
{
  log: {loglevel: "warning"},
  inbounds: [{
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
  }],
  outbounds: [
    {tag: "direct", protocol: "freedom", settings: {domainStrategy: "UseIPv4"}},
    {tag: "block",  protocol: "blackhole"}
  ],
  routing: {
    domainStrategy: "IPIfNonMatch",
    rules: [
      {type: "field", ip: ["geoip:private"], outboundTag: "block"},
      {type: "field", protocol: ["bittorrent"], outboundTag: "block"}
    ]
  }
}' > "$NEW"

if ! TEST_OUT=$(xray run -test -config "$NEW" 2>&1); then
  echo "$TEST_OUT" | tail -15
  die "Xray отклонил конфиг: $NEW (причина выше)"
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
  echo "vless://${id}@${SERVER_IP}:${PORT}?encryption=none&security=reality&sni=${SNI}&fp=chrome&pbk=${PUB}&sid=${SID}&type=xhttp&path=%2F${XPATH#/}&mode=auto#${1}"
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

SSH_PORT=$(sshd -T 2>/dev/null | awk '/^port /{print $2; exit}')
SSH_PORT=${SSH_PORT:-22}
ufw allow "$SSH_PORT/tcp" >/dev/null
ufw allow "$PORT/tcp" >/dev/null
ufw --force enable >/dev/null
echo "Открыты порты: SSH $SSH_PORT/tcp, VPN $PORT/tcp"

# ---------- 9. Запуск ----------
say "Запускаю Xray"
systemctl enable xray >/dev/null 2>&1
systemctl restart xray
sleep 2
systemctl is-active --quiet xray || { journalctl -u xray -n 30 --no-pager; die "Xray не запустился"; }

# ---------- 10. Ссылки ----------
: > "$LINKS_FILE"; chmod 600 "$LINKS_FILE"
for n in $CLIENTS; do vpnctl link "$n" >> "$LINKS_FILE"; done

say "Готово! VLESS + REALITY + XHTTP на $SERVER_IP:$PORT (маскировка: $SNI)"
vpnctl all
cat <<EOF

Ссылки сохранены в $LINKS_FILE (не публикуйте их: это ключи доступа).
Импорт: скопируйте ссылку или отсканируйте QR в Happ / v2RayTun / v2rayNG / v2rayN.

Управление:  vpnctl help
EOF
