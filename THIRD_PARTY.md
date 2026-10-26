# Сторонние проекты и лицензии

Приложение «Обходы» (bypass-tray) — MIT, см. [LICENSE](LICENSE).
Ниже — компоненты, которые оно скачивает или поставляет, с авторами,
лицензиями и ссылками. Тексты лицензий лежат в
[data/licenses/](data/licenses/) и попадают в релизный архив.

## Ядро VPN

- **Xray-core** — ядро прокси/VPN, на нём работает подключение.
  - Авторы: команда **XTLS** (проект [XTLS/Xray-core](https://github.com/XTLS/Xray-core),
    автор и основатель — RPRX).
  - Лицензия: **Mozilla Public License 2.0** —
    [data/licenses/Xray-core-LICENSE.txt](data/licenses/Xray-core-LICENSE.txt).
  - Где используется: `bin/xray.exe`, `bin/geoip.dat`, `bin/geosite.dat`;
    при подключении поднимается локальный прокси `127.0.0.1:10818`.
  - Бинарник скачивается с официальных релизов XTLS/Xray-core без изменений.

## Обход DPI для YouTube и Discord

- **zapret** — оригинальный проект обхода DPI.
  - Автор: **bol-van** — [bol-van/zapret](https://github.com/bol-van/zapret).
  - Лицензия: **MIT**, Copyright (c) 2016-2026 bol-van.
- **zapret-discord-youtube** — Windows-сборка zapret и набор готовых стратегий,
  которую использует приложение.
  - Автор сборки: **Flowseal** —
    [Flowseal/zapret-discord-youtube](https://github.com/Flowseal/zapret-discord-youtube)
    (продолжение проекта bol-van).
  - Лицензия: **MIT**, Copyright (c) 2016-2026 bol-van,
    Copyright (c) 2024-2026 Flowseal —
    [data/licenses/zapret-LICENSE.txt](data/licenses/zapret-LICENSE.txt).
  - Где используется: `winws.exe` и `general*.bat` в папке zapret.

## Telegram

- **tg-ws-proxy** — локальный MTProto-прокси для Telegram Desktop.
  - Автор: **Flowseal** —
    [Flowseal/tg-ws-proxy](https://github.com/Flowseal/tg-ws-proxy).
  - Лицензия: **MIT** —
    [data/licenses/tg-ws-proxy-LICENSE.txt](data/licenses/tg-ws-proxy-LICENSE.txt).
  - Где используется: `TgWsProxy_windows.exe`, порт `127.0.0.1:1443`.

## Список заблокированных сайтов

- **Re:filter** (`Re-filter-lists`) — список заблокированных доменов в формате
  geosite.
  - Автор: **Andrevich** —
    [1andrevich/Re-filter-lists](https://github.com/1andrevich/Re-filter-lists).
  - Лицензия: **MIT**, Copyright (c) 2024 Andrevich —
    [data/licenses/Re-filter-LICENSE.txt](data/licenses/Re-filter-LICENSE.txt)
    (копия рядом со списком: [data/LICENSE-refilter.txt](data/LICENSE-refilter.txt)).
  - Где используется: `bin/geosite-refilter.dat` (категория `REFILTER`),
    обновляется с релизов 1andrevich/Re-filter-lists.

## Библиотеки Python

Приложение собрано на Python и использует:

- **pystray** — значок в системном трее (LGPL-3.0);
- **Pillow** — отрисовка значка и работа с изображениями (MIT-CMU);
- **psutil** — сведения о процессах (BSD-3-Clause).

Их лицензии входят в состав соответствующих пакетов.

## Что приложение не поставляет

Своего VPN-сервера, подписок и учётных данных в поставке нет: ссылку на
подписку пользователь получает у своего провайдера. Приложение лишь
подключается к ней через Xray.
