#!/usr/bin/env bash
# Смена сайта для маскировки (SNI) на уже установленном сервере.
#   bash sni-change.sh            — подобрать автоматически (сайт из той же сети, что и сервер)
#   bash sni-change.sh домен.ru   — задать вручную
# Ключи и UUID не меняются, но в ссылке меняется sni — в приложениях ссылку нужно обновить.
set -euo pipefail
CFG=/usr/local/etc/xray/config.json; ENV=/usr/local/etc/xray/vpn.env; NEWCFG=/usr/local/etc/xray/config.new.json
[ "$(id -u)" -eq 0 ] || { echo "Запустите от root (sudo -i)"; exit 1; }
[ -f "$CFG" ] && [ -f "$ENV" ] || { echo "VPN на этом сервере не установлен"; exit 1; }
OLD=$(jq -r '.inbounds[0].streamSettings.realitySettings.serverNames[0]' "$CFG")
NEW="${1:-}"
if [ -z "$NEW" ]; then
  echo "==> Подбираю сайт из той же сети, что и сервер (2–3 минуты)…"
  curl -fsSL https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/main/vpn/sni-pick.sh -o /root/sni-pick.sh
  NEW=$(bash /root/sni-pick.sh --auto 2>/dev/null | tail -1 || true)
  [ -n "$NEW" ] || { echo "Не нашёл подходящий сайт. Задайте вручную: bash sni-change.sh домен"; exit 1; }
fi
[ "$NEW" != "$OLD" ] || { echo "Сайт и так $OLD — менять нечего"; exit 0; }
v=$(curl -s -o /dev/null -w '%{http_version}' --tlsv1.3 --http2 --max-time 8 "https://$NEW/" || true)
[ "$v" = "2" ] || { echo "$NEW не поддерживает TLS 1.3 + HTTP/2 — не подходит"; exit 1; }
jq --arg s "$NEW" '.inbounds[0].streamSettings.realitySettings.serverNames=[$s]
                 | .inbounds[0].streamSettings.realitySettings.target=($s+":443")
                 | del(.inbounds[0].streamSettings.realitySettings.dest)' "$CFG" > "$NEWCFG"
if ! out=$(xray run -test -config "$NEWCFG" 2>&1); then echo "$out" | tail -5; echo "Xray не принял конфиг — ничего не меняю"; rm -f "$NEWCFG"; exit 1; fi
cp "$CFG" "$CFG.bak"; mv "$NEWCFG" "$CFG"; chmod 644 "$CFG"
sed -i "s/^SNI=.*/SNI=$NEW/" "$ENV"
systemctl restart xray; sleep 2
systemctl is-active --quiet xray || { echo "Xray не запустился — возвращаю прежний конфиг"; cp "$CFG.bak" "$CFG"; sed -i "s/^SNI=.*/SNI=$OLD/" "$ENV"; systemctl restart xray; exit 1; }
journalctl -u xray -n 20 --no-pager | grep -i "increase the likelihood" && echo "[!] Xray считает этот сайт рискованным — лучше выбрать другой" || true
echo
echo "Готово: сайт маскировки $OLD → $NEW"
echo "Обновите ссылку в приложениях (в Amnezia достаточно поменять поле SNI на $NEW):"
vpnctl all
