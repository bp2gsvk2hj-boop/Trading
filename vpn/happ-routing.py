#!/usr/bin/env python3
"""Профиль маршрутизации Happ «Интернет РФ напрямую».

Российские сайты идут напрямую (мимо VPN), всё остальное — через VPN.
Тот же принцип, что и список «Интернет РФ напрямую» на роутере.
Профиль лёгкий: домены перечислены явно (без большой базы geosite),
из геобаз используется только geoip:ru — это важно для iPad, где
у VPN-модуля мало памяти.

Запуск:  python3 vpn/happ-routing.py   → печатает ссылку happ://routing/onadd/...
"""
import base64
import json

NAME = "Интернет РФ напрямую"

# Российские доменные зоны
ZONES = ["ru", "su", "xn--p1ai", "xn--80adxhks", "moscow", "tatar", "xn--d1acj3b"]

# Российские сервисы на доменах вне зон .ru/.su/.рф
SERVICES = [
    # Яндекс
    "yandex.net", "yandex.com", "yastatic.net", "yandexcloud.net", "yandex.cloud", "ya.cc",
    # VK, OK
    "vk.com", "vk.me", "vkuser.net", "userapi.com", "vk-cdn.net", "vkuseraudio.net",
    "vkuservideo.net", "mycdn.me",
    # Карты, кино
    "2gis.com", "ivi.tv", "okko.tv", "premier.one",
    # Российские CDN
    "ngenix.net", "selcdn.net",
    # Антивирусы
    "kaspersky.com", "kaspersky-labs.com", "drweb.com",
    # Банки
    "sberbank.com",
]

profile = {
    "Name": NAME,
    "GlobalProxy": "true",
    "RemoteDNSType": "DoH",
    "RemoteDNSDomain": "https://cloudflare-dns.com/dns-query",
    "RemoteDNSIP": "1.1.1.1",
    "DomesticDNSType": "DoH",
    "DomesticDNSDomain": "https://dns.google/dns-query",
    "DomesticDNSIP": "8.8.8.8",
    "Geoipurl": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat",
    "Geositeurl": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat",
    "DnsHosts": {"cloudflare-dns.com": "1.1.1.1", "dns.google": "8.8.8.8"},
    "DirectSites": ["domain:" + d for d in ZONES + SERVICES],
    "DirectIp": ["geoip:ru", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                 "169.254.0.0/16", "224.0.0.0/4", "255.255.255.255"],
    "ProxySites": [],
    "ProxyIp": [],
    "BlockSites": [],
    "BlockIp": [],
    "DomainStrategy": "AsIs",
    "FakeDNS": "false",
}

if __name__ == "__main__":
    raw = json.dumps(profile, ensure_ascii=False, separators=(",", ":")).encode()
    print("happ://routing/onadd/" + base64.b64encode(raw).decode())
