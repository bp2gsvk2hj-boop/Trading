# Мониторинг брокерского рынка

Собирает посты публичных Telegram-каналов и новости СМИ про брокерский бизнес Сбера
и рынок брокерских услуг России, отбирает по ключевым словам, убирает дубли, делает
сводку через ChatGPT и присылает дайджест в Telegram в 09:00 и 18:00 МСК.

## Установка — одна команда на сервере

    curl -fsSL https://raw.githubusercontent.com/bp2gsvk2hj-boop/Trading/main/broker-monitor/install.sh | sudo bash

Установщик сам находит выход VPN (`127.0.0.1:10810`) и Telegram-бота торгового робота
или VPN-мониторинга. Спрашивает только ключ OpenAI. Вход в Telegram не нужен.

## Управление

    sudo broker-monitor              справка
    sudo broker-monitor now          прислать дайджест сейчас
    sudo broker-monitor add banksta  добавить канал
    sudo broker-monitor channels     проверить каналы
    sudo broker-monitor config       ключевые слова, источники, инструкция для ChatGPT
    sudo broker-monitor time 08:30 19:00
    sudo broker-monitor tg-login     читать ещё и все свои подписки (по желанию)

Файлы на сервере: код — `/opt/trading/broker-monitor`, настройки и ключи — `/var/lib/broker-monitor`.
