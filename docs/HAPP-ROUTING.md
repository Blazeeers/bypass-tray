# Happ: сплит-туннель (предлагаемая схема)

Задача: через **Happ** должны идти только заблокированные в РФ сервисы и западные
соцсети; **YouTube и Discord — напрямую**, их закрывает zapret (`winws.exe`);
Telegram Desktop — через `tg-ws-proxy` (вне VPN).

Это **черновик для агента**: применить правила в Happ (через редактор профиля /
импорт конфигурации), проверить и доработать под реальные подписки.

## Правила маршрутизации Xray (ориентир)

Happ работает на Xray-core, значит маршрутизация — это `routing.rules`. Идея:

1. **direct** — YouTube, Discord и российские сервисы (их закроет zapret или они
   и так доступны);
2. **proxy** — западные соцсети/сервисы (Instagram, X/Twitter, Facebook и т.п.);
3. **proxy** — всё остальное (по умолчанию).

Пример фрагмента конфига (имена outbound’ов зависят от профиля: обычно
`proxy` — сервер, `direct`/`freedom` — прямое соединение):

```json
{
  "routing": {
    "domainStrategy": "IPIfNonMatch",
    "rules": [
      {
        "type": "field",
        "outboundTag": "direct",
        "domain": [
          "youtube.com", "youtu.be", "ytimg.com", "googlevideo.com",
          "youtube-nocookie.com", "ytstatic.com", "youtube.googleapis.com",
          "discord.com", "discord.gg", "discordapp.com", "discordapp.net",
          "discord.media", "discordcdn.com", "gateway.discord.gg",
          "max.ru", "yandex.ru", "ya.ru", "yandex.net", "yastatic.net",
          "gosuslugi.ru", "mail.ru", "vk.com", "ok.ru", "sberbank.ru", "tbank.ru"
        ]
      },
      {
        "type": "field",
        "outboundTag": "proxy",
        "domain": [
          "instagram.com", "facebook.com", "fb.com", "x.com", "twitter.com",
          "tiktok.com", "linkedin.com", "reddit.com", "medium.com",
          "bbc.com", "cnn.com", "dw.com", "nytimes.com", "wsj.com",
          "openai.com", "chatgpt.com", "anthropic.com"
        ]
      },
      { "type": "field", "outboundTag": "proxy", "network": "tcp,udp" }
    ]
  }
}
```

Замечания:

- Список «западных соцсетей» дополнить по факту. Можно использовать
  `geosite:…`/`geoip:…`, если профиль Happ их поддерживает.
- Telegram Desktop не должен уходить в VPN: в Linux-версии это делается
  исключением процессов `tg-ws-proxy`/`Telegram`. В Happ это можно сделать
  правилом по процессу (если поддерживается) или просто не включать VPN, когда
  Telegram не нужен; на практике tg-ws-proxy сам ходит по WebSocket — важно,
  чтобы его трафик шёл напрямую.
- **DNS:** включите Secure DNS (DoH) в браузере/Happ, иначе провайдерский DNS
  может отдавать заблокированные IP (см. рекомендации Flowseal). Для direct-
  доменов DNS тоже должен резолвиться нормально.

## Почему YouTube/Discord «напрямую»

- Их блокировка снимается zapret (winws) на уровне пакетов; если загнать их в
  VPN, zapret не увидит трафик (он идёт в туннель), а VPN добавит задержку.
- Поэтому в Happ они в `direct`, а `winws.exe` должен быть запущен.

## Как проверить (критерии)

1. Happ **включён**, `winws.exe` запущен →
   YouTube и Discord открываются; их трафик не идёт через VPN
   (проверить: `Get-NetTCPConnection`/Resource Monitor — удалённые IP не сервера
   VPN; или отключить Happ и убедиться, что они по-прежнему работают).
2. Happ **включён** → Instagram/X открываются; при выключенном Happ — нет.
3. `tg-ws-proxy` работает всегда; Telegram Desktop подключён к `127.0.0.1:1443`.

Если Happ не даёт применить эти правила автоматически — приложение должно
показывать инструкцию/напоминание, а в README — раздел «ручная настройка Happ».
