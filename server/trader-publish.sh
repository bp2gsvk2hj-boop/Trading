#!/usr/bin/env bash
# =====================================================================
#  «Помощник трейдера» на GitHub Pages: данные с сервера робота.
#     bash trader-publish.sh install   — включить (раз в 5 минут, если данные обновились)
#     trader-publish                   — отправить сейчас
#  Берёт /var/www/trader/data.json (его собирает робот) и кладёт в тот же
#  секретный gist, что и страница мониторинга VPN (токен из vpn-stats).
#  Код робота не трогает. Адрес приложения:
#     https://bp2gsvk2hj-boop.github.io/Trading/trader/#<код gist>
# =====================================================================
set -uo pipefail
SENV=/usr/local/etc/xray/stats.env
SRC=/var/www/trader/data.json
STAMP=/var/lib/trader-publish.stamp
BIN=/usr/local/bin/trader-publish
PAGE="https://bp2gsvk2hj-boop.github.io/Trading/trader/"

[ -f "$SENV" ] || { echo "Нет $SENV — сначала нужна страница мониторинга (vpn-stats)"; exit 1; }
. "$SENV"
[ -n "${GH_TOKEN:-}" ] && [ -n "${GIST_ID:-}" ] || { echo "В $SENV нет токена или кода gist"; exit 1; }

send() {
  [ -s "$SRC" ] || { echo "Нет $SRC"; return 1; }
  local m; m=$(stat -c %Y "$SRC")
  [ "${1:-}" != force ] && [ "$m" = "$(cat "$STAMP" 2>/dev/null)" ] && return 0
  jq -n --rawfile c "$SRC" '{files: {"trader-data.json": {content: $c}}}' > /tmp/trader-publish.json || return 1
  local via; for via in "" socks5h://127.0.0.1:10810; do
    if curl -s -m 30 ${via:+-x "$via"} -X PATCH "https://api.github.com/gists/$GIST_ID" \
         -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" \
         --data-binary @/tmp/trader-publish.json | jq -e .id >/dev/null 2>&1; then
      echo "$m" > "$STAMP"; echo "Данные отправлены"; return 0
    fi
  done
  echo "Не удалось отправить в GitHub"; return 1
}

if [ "${1:-}" = install ]; then
  [ "$(id -u)" -eq 0 ] || { echo "Запустите от root (sudo -i)"; exit 1; }
  cp "$0" "$BIN" && chmod 755 "$BIN"
  cat > /etc/systemd/system/trader-publish.service <<U
[Unit]
Description=Данные «Помощника трейдера» в gist для GitHub Pages
[Service]
Type=oneshot
ExecStart=$BIN
U
  cat > /etc/systemd/system/trader-publish.timer <<U
[Unit]
Description=Данные «Помощника трейдера» раз в 5 минут
[Timer]
OnCalendar=*:0/5
[Install]
WantedBy=timers.target
U
  systemctl daemon-reload && systemctl enable --now trader-publish.timer >/dev/null
  send force
  echo; echo "«Помощник трейдера» (откройте и добавьте в закладки):"; echo "${PAGE}#${GIST_ID}"
  exit 0
fi
send "${1:-}"
