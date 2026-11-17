#!/usr/bin/env bash
# =====================================================================
#  Резервная копия ключей и настроек VPN (российский relay).
#
#  Сделать копию (на работающем сервере):
#     bash backup.sh               (NOPASS=1 bash backup.sh — без пароля)
#  → спросит пароль, зашифрует /usr/local/etc/xray и /root/keenetic.conf
#    и пришлёт файл vpn-backup-*.txt вашему Telegram-боту.
#
#  Восстановить (на новом/переустановленном Ubuntu):
#     TG_TOKEN='токен_бота' bash backup.sh restore FILE_ID   — скачать из Telegram
#     bash backup.sh restore                                  — вставить текст файла вручную
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
  if [ -n "${2:-}" ]; then
    [ -n "${TG_TOKEN:-}" ] || die "Нужен токен бота: TG_TOKEN='...' bash backup.sh restore FILE_ID"
    say "Скачиваю копию из Telegram"
    FP=$(tg getFile --data-urlencode "file_id=$2" | jq -r .result.file_path) || die "Telegram недоступен или неверный FILE_ID/токен. Используйте: bash backup.sh restore (вставка текста)"
    for via in socks5h://127.0.0.1:10810 ""; do
      curl -fsS -m 60 ${via:+-x "$via"} "https://api.telegram.org/file/bot$TG_TOKEN/$FP" -o "$T" 2>/dev/null && break
    done
    [ -s "$T" ] || die "Не удалось скачать файл"
  else
    echo "Вставьте содержимое файла vpn-backup-*.txt и нажмите Enter, затем Ctrl+D:"
    cat > "$T"
  fi
  echo "(если копия делалась без пароля — просто нажмите Enter)"
  ask_pass once
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
F="/root/vpn-backup-$(hostname)-$(date +%Y%m%d).txt"
if [ "${NOPASS:-0}" = 1 ]; then BKPASS=""; else ask_pass; fi
FILES=("${DIR#/}")
[ -f /root/keenetic.conf ] && FILES+=(root/keenetic.conf)
enc() { if [ -n "$BKPASS" ]; then openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:BKPASS; else cat; fi; }
tar -czf - -C / --exclude='*.bak*' --exclude='config.new.json' "${FILES[@]}" | enc | base64 -w 76 > "$F"
chmod 600 "$F"
say "Копия: $F ($(wc -c <"$F") байт, $([ -n "$BKPASS" ] && echo зашифрована || echo БЕЗ пароля))"

if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ]; then
  R=$(tg sendDocument -F "chat_id=$TG_CHAT" -F "document=@$F" \
      -F "caption=Резервная копия VPN $(date +%d.%m.%Y). $([ -n "$BKPASS" ] && echo "Пароль — только у вас." || echo "БЕЗ пароля: не пересылайте этот файл.")") || die "Не удалось отправить в Telegram. Файл лежит в $F"
  FID=$(jq -r .result.document.file_id <<<"$R")
  tg sendMessage --data-urlencode "chat_id=$TG_CHAT" --data-urlencode "text=Восстановление на новом сервере (под root):
curl -fsSL https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/main/vpn/backup.sh -o backup.sh && TG_TOKEN='ТОКЕН_БОТА' bash backup.sh restore $FID" >/dev/null || true
  say "Отправлено в Telegram. Сохраните файл в «Файлы» на iPad и запомните пароль."
else
  echo "Telegram не настроен — скопируйте содержимое файла вручную: cat $F"
fi
