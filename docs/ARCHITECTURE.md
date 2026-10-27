# Архитектура

## Файлы

```
bypass-tray/
├── run.bat / install.bat          # запуск и установка (venv)
├── pyproject.toml                 # пакет + gui-лаунчер bypass-tray.exe
├── requirements.txt               # pystray, Pillow, psutil
├── CHANGELOG.md                   # история версий
├── tests/smoke_test.py            # дымовые проверки (без разрушительных действий)
├── src/bypass_tray/
│   ├── __main__.py                # CLI: --status, --health, --route-audit, --updates, --setup, --sweep-strategies
│   ├── app.py                     # трей: иконка, меню, фоновые проверки и действия
│   ├── ui.py                      # окно-панель (Tkinter), окраска системной рамки
│   ├── settings_ui.py             # окно настроек
│   ├── widgets.py                 # карточки, кнопки, тумблеры, прокрутка (Canvas)
│   ├── theme.py                   # светлая/тёмная схема, шрифты
│   ├── icons.py                   # иконки трея и панели (Pillow)
│   ├── status.py                  # сбор статусов (процессы, порт, служба)
│   ├── actions.py                 # действия (служба/.bat, UAC, окна консоли)
│   ├── strategy.py                # auto-подбор стратегий: команда, замер, перебор
│   ├── xray.py                    # клиент Xray: подписка, серверы, прокси, маршруты, замок
│   ├── health.py                  # проверки сервисов и туннеля, вердикт
│   ├── routes.py                  # аудит доменов, direct/proxy-оверрайды, применение
│   ├── blocked.py                 # встроенный список Re:filter: файл, обновление
│   ├── guard.py                   # надзиратель: tg-ws-proxy, маршруты
│   ├── networks.py                # ключ сети и стратегия zapret по сетям
│   ├── activity.py                # журнал действий виджета
│   ├── autostart.py               # ярлык автозапуска в Startup
│   ├── updates.py                 # релизы GitHub, версии, «Обновить всё»
│   ├── components.py              # установка zapret и tg-ws-proxy (атомарно)
│   └── config.py                  # конфиг/состояние, атомарная запись, пути
└── docs/…
```

## Потоки и расписание

- Главный поток — `pystray.Icon.run()` (цикл сообщений трея).
- Фоновый поток `_loop` раз в `refresh_seconds` собирает статусы и обновляет
  иконку/меню/подсказку. Иконка и меню пересобираются **только при изменении**
  состояния (сравнивается подпись), чтобы не дёргать оболочку зря.
- Расписание фоновых работ:
  - `status.collect` — каждые `refresh_seconds` (5 с);
  - `health.collect` — раз в `health_minutes` (10 мин) и при открытии панели;
  - `guard.heal` — раз в `guard_minutes` (10 мин), только если не идёт другая
    работа; сервер не меняет;
  - проверка туннеля в `check_proxy_health` — раз в 60 с (12-й тик), запрос
    `xray.verify(target="api.telegram.org", timeout=10)`; 3 неудачи подряд —
    смена сервера, после любой попытки смены кулдаун 10 минут;
  - аудит маршрутов — раз в `route_audit_minutes` (60 мин) внутри guard и по
    кнопке;
  - проверка обновлений — раз в `update_check_hours` (24 ч) и по кнопке.
- Проверки и действия выполняются в отдельных потоках с флагами от наложения
  (`health_running`, `guard_running`, `route_running`, `updates_running`,
  `busy`). Пока идёт обновление или действие, `_loop` пропускает резервный
  режим потока и надзирателя, чтобы не оборвать чужой перезапуск Xray.
- В `xray.py` общий `threading.RLock()` вокруг `start/stop/restart/switch_to/
  switch_server/restart_with_stream`: панель и надзиратель не поднимают два
  ядра одновременно.
- Панель Tkinter живёт в **своём** потоке. Окно создаётся один раз; закрытие
  лишь прячет его (`withdraw`), `destroy` — только при выходе из приложения.
  Поэтому повторные открытия не плодят интерпретаторы Tcl.
- Настройки — не отдельный Toplevel, а вторая страница панели
  (`SettingsPage`) в Canvas-stage: страницы лежат window-item'ами, x не
  меняется, видимость переключается `state` за один кадр. Высота root
  выставляется сразу, без tween/after; затем `_fit`. Шапка переключается
  в том же кадре, после — `update_idletasks`, чтобы не осталось ghost-кнопок.
- Секция серверов — `AnimatedSection` на Canvas, но без анимации: expand
  сразу ставит высоту по `content.winfo_reqheight()`, collapse — 0 и скрывает
  секцию из pack. Ни `after`, ни промежуточных кадров. Страница настроек
  строится один раз, при показе вызывается `reload()`. Высота ограничена
  рабочей областью Windows (`SystemParametersInfoW(SPI_GETWORKAREA)`), а не
  `winfo_screenheight`; при нехватке места режется страница, x/y не меняются.
- Окно создаётся скрытым (`withdraw`), геометрия и иконка выставляются до
  `deiconify`; первого кадра в (0,0) нет.
- Связь с панелью — через `queue.Queue` команд (`show`/`close`/`settings`/
  `render`), которые разбираются в Tk-потоке планировщиком `after`. Ни один
  виджет не трогается из чужого потока. Ошибка одной команды показывается в
  подвале и не останавливает цикл.
- Действия из панели запускаются в отдельных потоках, чтобы UI не подвисал;
  повторный клик во время `busy` игнорируется.
- Колесо мыши не использует `bind_all`: `ScrollBody.bind_tree()` вешает
  обработчик на canvas, внутренний фрейм и всех детей (при повторном вызове
  не дублирует). Вложенный список правил в настройках прокручивается отдельно
  от окна; внешняя прокрутка настроек игнорирует область списка.
- Системная рамка окна окрашивается через DWM (`DwmSetWindowAttribute`:
  тёмный режим, цвет заголовка и границы из `theme.header`) — на Windows 10/11,
  молча пропускается там, где атрибут недоступен.

## Схема статусов

`status.collect(cfg)` — единственный источник правды для иконки, меню и панели:

```json
{
  "zapret":     {"running": true,  "process": "winws.exe",
                 "digest": "fake fake,fakedsplit",
                 "dir": "C:\\Program Files\\Zapret",
                 "bats": ["general (ALT).bat", "…"],
                 "strategy": "general (ALT).bat",
                 "cmdline_source": "service",
                 "as_service": true},
  "tgwsproxy":  {"running": true, "port": 1443, "listening": true},
  "admin": false
}
```

- `running` — найден процесс (psutil, fallback `tasklist`).
- `digest` — уникальные значения `--dpi-desync`, поддерживаются обе формы
  записи: `--dpi-desync=fake` (.bat) и `--dpi-desync fake` (образ службы).
- `strategy` — имя `general*.bat`, сопоставленного запущенному `winws`
  (см. ниже); пусто, если совпадение неоднозначно.
- `cmdline_source` — откуда взята командная строка: `process` или `service`.
- `as_service` — zapret установлен службой Windows.
- `admin` — запущено ли приложение с правами администратора.

### Как определяется стратегия

1. Командная строка берётся у процесса `winws.exe`, если доступна.
2. Если zapret установлен службой, процесс принадлежит `SYSTEM` и его
   командная строка обычному пользователю не видна. Тогда аргументы читаются
   из реестра: `HKLM\SYSTEM\CurrentControlSet\Services\zapret` → `ImagePath`.
   Это доступно **без** прав администратора.
3. Из командной строки и из каждого `general*.bat` строится «отпечаток» —
   упорядоченный список пар `(опция --dpi-desync*, значение)`. Переменные
   `%BIN%`/`%LISTS%` вырезаются, пути сводятся к имени файла, поэтому
   аргументы `.bat` и раскрытые аргументы службы сопоставимы.
4. Побеждает `.bat` с самым длинным общим префиксом; результат принимается
   только при отрыве от второго места (иначе стратегия считается
   неопределённой). На текущей машине отрыв 50 против 7.

## Действия

`actions.py`:

- `start_zapret(cfg, bat)` — убить `winws.exe`, затем `cmd /c <bat>` в папке
  zapret; окно консоли скрывается (`EnumWindows` + `ShowWindow(SW_HIDE)`),
  потому что `.bat` запускает winws через `start /min`.
- `stop_zapret(cfg)` — остановить службу, если она есть, иначе убить `winws.exe`.
- `control_service(action)` — `sc stop|start|restart zapret`; при отсутствии
  прав поднимает UAC через `ShellExecuteW(..., "runas", ...)`.
- `open_service_bat(cfg)` — открыть `service.bat` от администратора (смена
  стратегии службы делает сам zapret).
- `start_tgws(cfg)` — запустить прокси, если он ещё не запущен (повтор не
  создаёт второй процесс).
- `kill_process(name)` — возвращает `{"killed", "denied"}`, отказ по правам
  не проглатывается молча.

## Проверки сервисов, надзиратель и маршруты

`health.py`:

- `_http(url, proxy_port=None, timeout=5)` — GET напрямую (системный прокси
  принудительно отключён) или через `127.0.0.1:<port>`; успех — любой ответ
  младше 500.
- Пункты: `zapret` (стратегия из статуса), `youtube` (страница + CDN),
  `discord` (страница + TCP-шлюз), `deepseek` (напрямую, при неудаче через
  прокси), `telegram` (Bot API через прокси), `tgwsproxy` (порт), `vpn`
  (`xray.verify()` через сервер). Пробы идут параллельно, не больше четырёх
  потоков; у прокси-проб одна повторная попытка.
- `collect()` возвращает `{"checked", "items", "verdict"}`; `verdict` —
  `fail` при любом `bad`, `warn` при `warn`, иначе `ok`. Результат
  кэшируется в `health.json`.

`guard.py`:

- `heal(cfg, status_state, proxy, health)` — не чаще `guard_minutes`:
  поднимает tg-ws-proxy (если установлен) и ждёт порт до 3 с; при состоянии
  `vpn: bad` в свежем health-отчёте переключает сервер; при `routes.due(...)`
  делает аудит и сохраняет оверрайды.
- Надзиратель **не вызывает UAC** (zapret только предупреждает) и **не
  перезапускает Xray** из-за маршрутов: оверрайды применятся при следующем
  подключении. Предупреждения пишутся в журнал не чаще раза в час.
- Устаревший health-отчёт (старше двух `health_minutes`) для переключения
  сервера не используется.

`routes.py`:

- `probe()` — домен напрямую или через прокси; `decide_route(direct_ok,
  proxy_checked, proxy_ok)` → `direct` / `proxy` / `broken` / `unknown`.
- `audit()` проверяет домены из `DEFAULT_DOMAINS` (DeepSeek, Telegram Bot API,
  Instagram, Hugging Face, GitHub, Яндекс), пропускает домены YouTube/Discord.
- `apply_audit()` пишет `route_overrides.json` в формате
  `{"at": …, "routes": {домен: {route, since, checked}}}`; `unknown` не
  сохраняется, запись удаляется только если домен выпал из аудита.
  Пользовательские домены из `xray_routes` не перезаписываются: своё правило
  сильнее, а старый оверрайд для такого домена удаляется.
- `clean_domain()` срезает схему, путь, порт, `*.`; префиксы `geosite:` и
  подобные сохраняются. Прямые домены вычитаются из списка прокси в
  `xray.build_config`.
- `apply_now(cfg)` — явное применение: аудит, сохранение, перезапуск Xray при
  изменениях. Вызывается **только** из панели после подтверждения; guard его
  не использует.

`networks.py`:

- ключ сети — MAC шлюза (`arp -a`), иначе адрес шлюза (реестр TCP/IP, затем
  `ipconfig`), иначе SSID (`netsh wlan show interfaces`), иначе `default`;
- `remember()` сохраняет стратегию в `networks.json`, `strategy_for()` читает;
  при смене сети раз в минуту сохранённая стратегия применяется и пишется в
  журнал.

`settings_ui.py` — окно настроек: «Поведение», подписка VPN, «Список
заблокированных сайтов» (тумблер, статус, «Обновить список»), «Маршруты
сайтов» (`xray_routes`: добавление домена/ссылки, выбор «Напрямую/Через VPN»,
список с переключением и удалением, внутренняя прокрутка после ~6 правил),
компоненты и пути. Правила пишутся в конфиг по «Сохранить», старые списки
очищаются; затем прокси перезапускается.

`blocked.py` — встроенный список Re:filter:

- `packaged_path()` — `data/geosite-refilter.dat` (в PyInstaller —
  `sys._MEIPASS/data`), `bin_path()` — рядом с ядром;
- `ensure()` — офлайн-копия поставки в `bin` (атомарно, `.part` + `replace`);
- `update()` — скачивание релиза `geosite.dat`, разбор protobuf GeoSiteList
  (категория `REFILTER`, число доменов), проверка `xray.exe -test` на
  временном конфиге, атомарная замена, состояние в `blocked-list.json`
  (`last_attempt` ограничивает автоскачивание разом в час);
- `info()` — включён/установлен/размер/дата для настроек.

`activity.py` — журнал действий виджета (`activity.json`, последние 60
записей), дополняется автоматическими и ручными действиями.

## Иконка трея

`icons.make_icon(state, color)` рисует цветной кружок с белым глифом:
`ok` — галочка, `warn`/`bad` — «!», `busy` — две стрелки, `off`/`shield` —
щит, `update` — стрелка вниз; старые ключи `zapret`/`tgws`/`proxy`/`idle`
остаются совместимыми. `make_app_icon()` — щит с галочкой на акцентном фоне.
Картинки рисуются в 4× и уменьшаются с `LANCZOS`, поэтому остаются чёткими на
HiDPI.

## Автоподбор стратегии zapret

`strategy.py`:

- `extract_command(bat)` — рядом с `.bat` кладётся его копия, в которой
  `start "…" /min "…winws.exe"` заменён на `echo ZAPRET_CMD`; запуск сценария
  даёт **полностью раскрытую** командную строку (учитываются `%BIN%`,
  `%LISTS%`, `%GameFilterTCP%`). Проверено: совпадает с командой службы
  по всем 50 токенам. Ручной парсинг `.bat` не нужен и не используется.
- `probe_url(url, timeout, limit=524288)` — скачивает до 512 КБ и возвращает
  `ok/status/ms/bytes/kbps`; `measure()` засчитывает цель только при ответе
  и не менее 64 КБ, контроль — по доступности. Так «страница открылась»
  не выигрывает у реальной загрузки.
- `sweep(...)` — останавливает службу, замеряет базовую линию, перебирает
  стратегии, ставит лучшую через `install_service()` и запускает службу.
  Лучшая — по баллу, затем по суммарным байтам, затем по меньшему времени.
  Аварийный путь: при неудаче возвращает исходную команду службы
  (`service_image_path()` из реестра).
- Запускается отдельным процессом с UAC:
  `python -m bypass_tray --sweep-strategies --out … --report …`.

## Клиент Xray

`xray.py`:

- `links_from_text()` / `fetch_subscription()` — достаёт ссылки `vless`,
  `vmess`, `trojan`, `ss` из текста, файла-подписки или base64; мусор
  отбрасывается, повторы убираются, на битые данные исключений нет.
- `user_links()` / `collect_links()` — свои конфиги из `xray_links` плюс
  подписка одним списком. Подписка необязательна: её сетевой сбой не
  обнуляет свои ссылки; пустой набор — `ValueError` с понятным текстом.
- `parse_link()` — диспетчер по схеме. `parse_vless()` (grpc/ws,
  reality/tls), `parse_vmess()` (base64 JSON, ws/grpc/kcp, tls),
  `parse_trojan()` (tls/reality, ws/grpc, flow), `parse_ss()` (оба вида
  ссылок) собирают outbound для ядра. `build_config()` протоколу
  безразличен: ему передаётся готовый `server["outbound"]`.
- `rank_servers()` / `pick_server()` — TCP-проверка задержки, сортировка,
  выбор самого быстрого; список кэшируется в `xray-servers.json`.
- `build_config()` — конфиг ядра: mixed-инбаунд на `127.0.0.1:<port>`,
  outbounds `direct` (по умолчанию) → `proxy` → `block`. Порядок правил:
  1) пользовательские `direct` (из `xray_routes`), 2) пользовательские
  `proxy`, 3) audit direct, 4) YouTube/Discord напрямую (если не резервный
  режим), 5) preset `ext:geosite-refilter.dat:REFILTER` через прокси,
  6) РФ/private напрямую, 7) встроенные `PROXY_DOMAINS` + stream (в
  резервном режиме) + audit proxy. Пользовательский `direct` вычитается из
  всех прокси-списков, `proxy` — из прямых; дубли не допускаются.
- `start()` — собирает `user_routes` из `cfg["xray_routes"]`, audit-домены из
  `routes.proxy_domains()`/`direct_domains()`, при
  `blocked_list_enabled` вызывает `blocked.ensure()` и добавляет ext-правило.
  Legacy `xray_extra_domains`/`xray_direct_domains` добавляются только если
  непустые (после миграции пусты). Эффективный stream —
  `stream_via_proxy or cfg["youtube_via_vpn"]`; при `stream=False` домены
  YouTube/Discord попадают в `ProxyOverride`, чтобы браузер не ходил через
  локальный Xray, а zapret обрабатывал TCP напрямую. Если пользователь
  отправил в VPN хотя бы один домен группы YouTube/Discord, из обхода
  убирается вся группа. В VPN-режиме `health` проверяет YouTube/Discord
  через прокси, а не напрямую.
- `start()` / `stop()` / `restart_with_stream()` — запуск ядра, ожидание порта,
  системный прокси в HKCU, переключение резервного режима YouTube/Discord.
- `verify()` — настоящая проверка туннеля HTTPS-запросом к заблокированному
  сайту через `127.0.0.1:<port>`.
- `switch_server()` — автопереключение на следующий живой сервер.
- `ensure_core()` — своё ядро из `bin` либо скачивание официального релиза.

## Обновления

`updates.py`:

- `REPOS` — `Flowseal/zapret-discord-youtube`, `Flowseal/tg-ws-proxy` и
  `XTLS/Xray-core`; запросы к `releases/latest` идут параллельно, без токена.
- `installed_versions()` — версии zapret/tg-ws-proxy из `state["installed"]`,
  Xray — живым запуском `xray.exe -version`, виджет — из `pyproject.toml`
  (`widget_version()`).
- `check_all(cfg, state)` возвращает
  `{"checked", "components": {имя: {latest, installed, outdated}}, "outdated"}`.
  `outdated` включается только при известной установленной версии.
- `update_all(cfg, state, progress)` останавливает Xray (если запущен),
  ставит zapret/tg-ws-proxy через `components`, перекачивает ядро Xray
  (`ensure_core(force=True)`), записывает `state["installed"]` и возвращает
  прокси на прежнем сервере. Загрузки атомарные: `.part`/временная папка,
  проверка размера и `os.replace`; при обрыве старая копия остаётся.
- `TrayApp.notify` при неудаче штатного `icon.notify` мигает иконкой; текст
  виден в подсказке и в панели.

## Конфиг и состояние

- `%APPDATA%\bypass-tray\config.json` — пути и параметры (см. CONFIG.md).
- `%APPDATA%\bypass-tray\state.json` — `seen_versions`, `updates_note`,
  `installed`, `updates`, `last_sweep`.
- Служебные файлы: `activity.json` (журнал), `health.json` (кэш проверок),
  `route_overrides.json` (маршруты), `networks.json` (стратегии по сетям),
  `xray.log` (вывод ядра), `xray-*.json` (конфиг/серверы/состояние).
- Все записи идут через `config.atomic_write` (временный файл + `os.replace`,
  до трёх попыток). Битый `config.json`/`state.json` переименовывается в
  `*.bak-<время>` и не перезаписывается.
- `load_config()` пишет файл только при первом запуске, миграции
  `xray_routes` или изменении автодетект-путей; обычное чтение mtime не
  трогает.

## Отличия от Linux-версии

| Linux («Обходы») | Windows (bypass-tray) |
|---|---|
| Quickshell/QML виджет панели | pystray + Tkinter (Canvas-виджеты) |
| `bypass-status` (systemctl, ip rule) | `status.py` (psutil, порт, реестр службы) |
| zapret nfqws + автоподбор | zapret winws + выбор `.bat` вручную |
| wowvpn (Xray+sing-box) | собственный клиент Xray (`xray.py`, ядро в `bin`) |
| systemd-таймер апдейтов | проверка в цикле приложения |
