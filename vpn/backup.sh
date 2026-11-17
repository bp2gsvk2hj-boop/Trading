#!/usr/bin/env bash
# =====================================================================
#  Резервная копия ключей и настроек VPN (российский relay).
#
#  Сделать копию (на работающем сервере):
#     bash backup.sh               (NOPASS=1 bash backup.sh — без пароля)
#  → спросит пароль и пришлёт вашему Telegram-боту ОДИН архив vpn-backup-*.zip:
#    ключи (зашифрованы), все скрипты и инструкция; в подписи — команда восстановления.
#
#  Восстановить (на новом/переустановленном Ubuntu):
#     TG_TOKEN='токен_бота' bash backup.sh restore FILE_ID   — скачать из Telegram
#     bash backup.sh restore /root/vpn-backup-….zip           — архив уже на сервере
#     bash backup.sh restore                                  — вставить текст keys.txt вручную
#  → расшифрует, вернёт ключи на место и переустановит VPN с прежними ключами:
#    ссылки в приложениях и настройки роутера менять не придётся.
# =====================================================================
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Запустите от root"; exit 1; }
DIR=/usr/local/etc/xray
RU_URL="https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/main/vpn/ru.sh"
say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }
command -v openssl >/dev/null || apt-get install -y -qq openssl >/dev/null
command -v jq >/dev/null || { apt-get update -qq; apt-get install -y -qq jq >/dev/null; }

tg() {  # curl к API Telegram: сначала через VPN (если поднят), потом напрямую
  local m="$1"; shift
  for via in socks5h://127.0.0.1:10810 ""; do
    out=$(curl -s -m 60 ${via:+-x "$via"} "https://api.telegram.org/bot$TG_TOKEN/$m" "$@" 2>/dev/null) || continue
    grep -q '"ok":true' <<<"$out" && { echo "$out"; return 0; }
  done
  return 1
}
ask_pass() {
  read -rsp "Пароль для архива: " P1; echo
  [ "${1:-}" = once ] && { export BKPASS="$P1"; return; }
  read -rsp "Повторите пароль: " P2; echo
  [ "$P1" = "$P2" ] && [ ${#P1} -ge 8 ] || die "Пароли не совпали или короче 8 символов"
  export BKPASS="$P1"
}

# ---------------- ВОССТАНОВЛЕНИЕ ----------------
if [ "${1:-}" = restore ]; then
  T=$(mktemp)
  if [ -n "${2:-}" ] && [ -f "$2" ]; then
    cp "$2" "$T"                                   # архив/файл уже на сервере
  elif [ -n "${2:-}" ]; then
    [ -n "${TG_TOKEN:-}" ] || die "Нужен токен бота: TG_TOKEN='...' bash backup.sh restore FILE_ID"
    say "Скачиваю копию из Telegram"
    FP=$(tg getFile --data-urlencode "file_id=$2" | jq -r .result.file_path) || die "Telegram недоступен или неверный FILE_ID/токен. Используйте: bash backup.sh restore (вставка текста keys.txt)"
    for via in socks5h://127.0.0.1:10810 ""; do
      curl -fsS -m 60 ${via:+-x "$via"} "https://api.telegram.org/file/bot$TG_TOKEN/$FP" -o "$T" 2>/dev/null && break
    done
    [ -s "$T" ] || die "Не удалось скачать файл"
  else
    echo "Вставьте содержимое keys.txt из архива и нажмите Enter, затем Ctrl+D:"
    cat > "$T"
  fi
  # если это zip-архив — достаём из него keys.txt
  if python3 -c 'import sys,zipfile; sys.exit(0 if zipfile.is_zipfile(sys.argv[1]) else 1)' "$T"; then
    python3 - "$T" <<'PY'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1]); n = [x for x in z.namelist() if x.endswith('keys.txt')][0]
open(sys.argv[1] + '.k', 'wb').write(z.read(n))
PY
    mv "$T.k" "$T"
  fi
  echo "(если копия делалась без пароля — просто нажмите Enter)"
  ask_pass once
  sed -i '/^#/d' "$T"
  if [ -z "$BKPASS" ]; then
    base64 -d "$T" 2>/dev/null | tar -xzf - -C / || die "Не распаковалось: копия зашифрована (нужен пароль) или текст повреждён"
  else
    base64 -d "$T" 2>/dev/null | openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:BKPASS 2>/dev/null \
      | tar -xzf - -C / || die "Не расшифровалось: неверный пароль или повреждённый текст"
  fi
  rm -f "$T"
  say "Ключи восстановлены в $DIR"
  L(){ jq -r --arg t "$1" '.outbounds[]|select(.tag==$t)|.settings.vnext[0] as $v|.streamSettings as $s|"vless://\($v.users[0].id)@\($v.address):\($v.port)?encryption=none&security=reality&sni=\($s.realitySettings.serverName)&fp=\($s.realitySettings.fingerprint)&pbk=\($s.realitySettings.publicKey)&sid=\($s.realitySettings.shortId)&type=xhttp&path=\($s.xhttpSettings.path|@uri)&mode=\($s.xhttpSettings.mode)#relay"' "$DIR/config.json"; }
  A1=$(L foreign1); A2=$(L foreign2)
  [ -n "$A1" ] || die "В копии нет зарубежных серверов — это не relay. Переустановите: KEEP=1 bash vpn-setup.sh"
  say "Переустанавливаю VPN с прежними ключами"
  curl -fsSL "$RU_URL" -o /root/ru.sh
  bash /root/ru.sh "$A1" ${A2:+"$A2"}
  NEWIP=$(curl -s -m 10 -4 https://api.ipify.org || true)
  OLDIP=$(sed -n 's/^SERVER_IP=//p' "$DIR/vpn.env")
  if [ -n "$NEWIP" ] && [ "$NEWIP" != "$OLDIP" ]; then
    printf '\n\033[1;33m[!] IP сервера изменился: был %s, сейчас %s.\n    Выполните: sed -i "s/^SERVER_IP=.*/SERVER_IP=%s/" %s/vpn.env && vpnctl all\n    и обновите ссылку на устройствах и адрес в WireGuard1 на Keenetic.\033[0m\n' "$OLDIP" "$NEWIP" "$NEWIP" "$DIR"
  fi
  exit 0
fi

# ---------------- РЕЗЕРВНАЯ КОПИЯ ----------------
[ -f "$DIR/vpn.env" ] || die "VPN здесь не установлен — копировать нечего"
. "$DIR/vpn.env"
TG_TOKEN="${TG_TOKEN:-}"; TG_CHAT="${TG_CHAT:-}"
if [ "${NOPASS:-0}" = 1 ]; then BKPASS=""; else ask_pass; fi
FILES=("${DIR#/}")
[ -f /root/keenetic.conf ] && FILES+=(root/keenetic.conf)
enc() { if [ -n "$BKPASS" ]; then openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:BKPASS; else cat; fi; }
DATA=$(tar -czf - -C / --exclude='*.bak*' --exclude='config.new.json' "${FILES[@]}" | enc | base64 -w 76)
GET="curl -fsSL https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/main/vpn/backup.sh -o backup.sh"
W=$(mktemp -d); mkdir -p "$W/vpn-backup/scripts"
{ echo "# Ключи VPN ($(hostname), $(date +%d.%m.%Y)). $([ -n "$BKPASS" ] && echo 'Зашифрованы паролем.' || echo 'БЕЗ пароля!')"
  echo "$DATA"; } > "$W/vpn-backup/keys.txt"
for f in backup.sh vpn-doctor.sh vpn-setup.sh ru.sh foreign.sh sni-pick.sh sni-change.sh keenetic.sh; do
  [ -f "/root/$f" ] && cp "/root/$f" "$W/vpn-backup/scripts/"
done
[ -f "$0" ] && cp "$0" "$W/vpn-backup/scripts/backup.sh" 2>/dev/null || true
mkrestore() {  # $1 — команда восстановления из Telegram (или пусто)
cat > "$W/vpn-backup/ВОССТАНОВЛЕНИЕ.txt" <<TXT
Резервная копия VPN — сервер $(hostname), $(date +%d.%m.%Y)
$([ -n "$BKPASS" ] && echo "Ключи зашифрованы паролем — он нужен при восстановлении." || echo "Ключи НЕ зашифрованы: не пересылайте этот архив.")

Что внутри:
  keys.txt   — ключи VPN, WireGuard для роутера, токен бота, настройки
  scripts/   — все скрипты VPN на момент копии

Восстановление на новом сервере Ubuntu (под root, sudo -i), одной строкой:
${1:-  (команда с FILE_ID — в подписи к сообщению с архивом в Telegram)}

Если Telegram с сервера недоступен:
  $GET && bash backup.sh restore
  и вставить содержимое keys.txt, затем Ctrl+D.

Скрипт вернёт ключи и переустановит VPN с прежними ключами: ссылки в
приложениях и настройки Keenetic менять не придётся. Если IP сервера
изменился — скрипт подскажет, что поправить.
Проверка после восстановления: bash vpn-doctor.sh
TXT
( cd "$W" && rm -f "$F" && python3 -m zipfile -c "$F" vpn-backup/ )
}
F="/root/vpn-backup-$(hostname)-$(date +%Y%m%d).zip"
mkrestore ""; chmod 600 "$F"
say "Архив: $F ($(wc -c <"$F") байт, ключи $([ -n "$BKPASS" ] && echo зашифрованы || echo БЕЗ пароля))"

if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ]; then
  R=$(tg sendDocument -F "chat_id=$TG_CHAT" -F "document=@$F" -F "caption=Резервная копия VPN") || die "Не удалось отправить в Telegram. Архив лежит в $F"
  FID=$(jq -r .result.document.file_id <<<"$R"); MID=$(jq -r .result.message_id <<<"$R")
  CMD="$GET && TG_TOKEN='ТОКЕН_БОТА' bash backup.sh restore $FID"
  CAP="Резервная копия VPN $(date +%d.%m.%Y): ключи, скрипты и инструкция в одном архиве. $([ -n "$BKPASS" ] && echo 'Пароль — только у вас.' || echo 'БЕЗ пароля: не пересылайте.')

Восстановление на новом Ubuntu (под root), подставив токен бота:
$CMD"
  tg editMessageCaption --data-urlencode "chat_id=$TG_CHAT" --data-urlencode "message_id=$MID" --data-urlencode "caption=$CAP" >/dev/null \
    || tg sendMessage --data-urlencode "chat_id=$TG_CHAT" --data-urlencode "text=$CAP" >/dev/null || true
  say "Отправлено в Telegram: архив и команда восстановления — в одном сообщении."
else
  echo "Telegram не настроен — скопируйте содержимое файла вручную: cat $F"
fi
