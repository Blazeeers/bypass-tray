# Сторонние материалы

## Re-filter-lists

- **Что:** список доменов, заблокированных в РФ, в формате geosite
  (`geosite-refilter.dat`, категория `REFILTER`).
- **Автор/репозиторий:** [1andrevich/Re-filter-lists](https://github.com/1andrevich/Re-filter-lists)
- **Лицензия:** MIT (Copyright (c) 2024 Andrevich). Полный текст —
  [data/LICENSE-refilter.txt](data/LICENSE-refilter.txt).
- **Где используется:** `data/geosite-refilter.dat`; при первом запуске
  копируется в `bin` и подключается правилом
  `ext:geosite-refilter.dat:REFILTER` в конфиге Xray.
- **Обновление:** кнопка «Обновить список» в настройках (скачивает
  `geosite.dat` из последнего релиза) либо замена файла вручную, см.
  [data/README.md](data/README.md).

## Компоненты обхода

zapret, tg-ws-proxy и ядро Xray скачиваются отдельно из их официальных
релизов и в репозитории не хранятся; их лицензии — в соответствующих
проектах:

- [Flowseal/zapret-discord-youtube](https://github.com/Flowseal/zapret-discord-youtube)
- [Flowseal/tg-ws-proxy](https://github.com/Flowseal/tg-ws-proxy)
- [XTLS/Xray-core](https://github.com/XTLS/Xray-core)

Собственный код проекта распространяется по MIT, см. [LICENSE](LICENSE).
