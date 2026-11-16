#!/usr/bin/env bash
# Закрепляет шлюз по умолчанию на сервере Selectel и проверяет связь.
# Запуск в консоли Selectel:
#   ip r add default via 135.106.164.1; curl -fsSL https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/main/vpn/gw.sh | bash
GW="${GW:-135.106.164.1}"
AEZA=193.233.208.41
ok()  { printf '\033[1;32m[✓] %s\033[0m\n' "$*"; }
bad() { printf '\033[1;31m[x] %s\033[0m\n' "$*"; }

cat > /etc/netplan/60-gw.yaml <<YAML
network:
  version: 2
  ethernets:
    eth0:
      routes:
        - to: default
          via: $GW
YAML
chmod 600 /etc/netplan/60-gw.yaml
netplan apply 2>/dev/null
sleep 3

if ip r | grep -q "^default via $GW"; then ok "Шлюз $GW закреплён (переживёт перезагрузку)"; else bad "Шлюз не применился"; ip r; fi
if ping -c 4 -W 2 8.8.8.8 >/dev/null 2>&1; then ok "Интернет есть"; else bad "Интернета нет"; fi
if curl -m 7 -sk -o /dev/null https://$AEZA; then ok "Aeza ($AEZA) доступен с этого сервера"; else bad "Aeza ($AEZA) НЕ доступен с этого сервера"; fi
echo
echo "Теперь на Aeza проверьте обратное направление:"
echo "  timeout 7 bash -c '</dev/tcp/$(ip -4 -o a show eth0 | awk '{print $4}' | cut -d/ -f1)/22' && echo ОТКРЫТ || echo НЕ ДОСТУПЕН"
