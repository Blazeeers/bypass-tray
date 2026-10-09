# Архитектура

## Файлы

```
bypass-tray/
├── run.bat / install.bat          # запуск и установка (venv, для разработки)
├── setup.bat                      # установка из исходников в один шаг
├── build.bat                      # сборка dist\bypass-tray.exe (PyInstaller)
├── pyproject.toml                 # пакет + gui-лаунчер bypass-tray.exe
├── requirements.txt               # pystray, Pillow, psutil
├── requirements-build.txt         # pyinstaller (только для сборки)
├── tests/smoke_test.py            # дымовые проверки (без разрушительных действий)
├── packaging/                     # сборка .exe: launcher.py, make_icon.py, spec
├── .github/workflows/             # сборка .exe на тег и прикрепление к релизу
├── src/bypass_tray/
│   ├── __main__.py                # python -m bypass_tray [--status]; мастер первого запуска
│   ├── runtime.py                 # исходники или .exe: пути и запуск дочерних процессов
│   ├── autostart.py               # ярлык в «Автозагрузке» (run.bat или .exe)
│   ├── wizard.py                  # мастер первого запуска: только ссылка на подписку
│   ├── app.py                     # трей: иконка, меню, цикл обновления статусов
│   ├── ui.py                      # окно-панель (Tkinter)
│   ├── widgets.py                 # карточки, индикаторы, кнопки (Canvas)
│   ├── theme.py                   # светлая/тёмная схема, шрифты
│   ├── icons.py                   # отрисовка иконок трея (Pillow)
│   ├── status.py                  # сбор статусов (процессы, порт, служба, Happ)
│   ├── actions.py                 # действия (служба/.bat, UAC, окна консоли)
│   ├── strategy.py                # auto-подбор стратегий: команда, замер, перебор
│   ├── happ.py                    # профили Happ, маршруты, включение с откатом
│   ├── updates.py                 # проверка релизов GitHub
│   ├── components.py              # установка zapret и tg-ws-proxy из релизов
│   ├── xray.py                    # клиент Xray: подписка, серверы, прокси
│   └── config.py                  # конфиг/состояние, автопоиск путей
└── docs/…
```

## Установка «в один клик»

Цель — на чистом Windows-ПК должно хватить «скачал `.exe` → запустил →
вставил ссылку на подписку». Для этого:

- **`bypass-tray.exe`** собирается PyInstaller в один файл (`packaging/`), в нём
  Python, Tkinter и зависимости; консольное окно отключено. Сборка идёт в CI
  (`.github/workflows/build-windows.yml`) на каждый тег `v*` и прикрепляется
  к релизу.
- **`runtime.py`** прячет различия «исходники / `.exe`»: постоянная папка
  `%LOCALAPPDATA%\bypass-tray` (её не стирает временная распаковка `_MEIPASS`) и
  запуск дочерних процессов (перебор стратегий, сторож Happ) — в `.exe` это сам
  файл, в исходниках `python -m bypass_tray`.
- **`wizard.py`** при первом запуске (нет `xray_subscription`) показывает окно:
  поле ссылки, галочка автозапуска, «Подключить». Мастер сам ставит компоненты и
  ядро Xray, включает автозапуск и поднимает прокси.
- **`autostart.py`** кладёт ярлык в «Автозагрузку»: на `.exe` при сборке, на
  `run.bat` при запуске из исходников.

Ядро Xray больше не требует Happ: `xray.ensure_core` берёт его из Happ, если тот
есть, иначе скачивает официальный релиз `XTLS/Xray-core`.

## Потоки

- Главный поток — `pystray.Icon.run()` (цикл сообщений трея).
- Фоновый поток `_loop` — раз в `refresh_seconds` собирает статусы и обновляет
  иконку/меню/подсказку. Иконка и меню пересобираются **только при изменении**
  состояния (сравнивается подпись), чтобы не дёргать оболочку зря.
- Проверка обновлений выполняется в отдельном потоке: сетевой запрос не
  задерживает обновление статусов.
- Панель Tkinter живёт в **своём** потоке. Окно создаётся один раз; закрытие
  лишь прячет его (`withdraw`), `destroy` — только при выходе из приложения.
  Поэтому повторные открытия не плодят интерпретаторы Tcl.
- Связь с панелью — через `queue.Queue` команд (`show`/`close`), которые
  разбираются в Tk-потоке планировщиком `after`. Ни один виджет не трогается
  из чужого потока.
- Действия из панели запускаются в отдельных потоках, чтобы UI не подвисал.

## Схема статусов

`status.collect(cfg)` — единственный источник правды для иконки, меню и панели:

```json
{
  "happ":       {"running": true,  "process": "happd.exe"},
  "zapret":     {"running": true,  "process": "winws.exe",
                 "digest": "fake fake,fakedsplit",
                 "dir": "C:\\Program Files\\Zapret",
                 "bats": ["general (ALT).bat", "…"],
                 "strategy": "general (ALT).bat",
                 "cmdline_source": "service",
                 "as_service": true},
  "tgwsproxy":  {"running": true, "port": 1443, "listening": true},
  "happ_routing": {"available": true, "active": "Только Telegram",
                   "youtube_direct": true, "socials_proxied": false,
                   "split_tunnel_ok": false, "…": "…"},
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
- `happ_routing` — только чтение `routing.json` Happ (без записи).
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
- `open_happ(cfg)` — открыть Happ.
- `kill_process(name)` — возвращает `{"killed", "denied"}`, отказ по правам
  не проглатывается молча.

## Иконка трея

`icons.make_icon(state, color)` рисует цветной кружок с буквой (`H`, `Z`, `T`,
`U`) либо галочкой для состояния «всё в порядке». Картинка рисуется в 4× и
уменьшается с `LANCZOS`, поэтому остаётся чёткой на HiDPI.

## Автоподбор стратегии zapret

`strategy.py`:

- `extract_command(bat)` — рядом с `.bat` кладётся его копия, в которой
  `start "…" /min "…winws.exe"` заменён на `echo ZAPRET_CMD`; запуск сценария
  даёт **полностью раскрытую** командную строку (учитываются `%BIN%`,
  `%LISTS%`, `%GameFilterTCP%`). Проверено: совпадает с командой службы
  по всем 50 токенам. Ручной парсинг `.bat` не нужен и не используется.
- `probe_url(url)` / `measure()` — реальная HTTPS-проверка `youtube.com` и
  `discord.com` плюс контрольный `ya.ru`. Контроль отличает «стратегия не
  работает» от «интернета нет».
- `sweep(...)` — останавливает службу, замеряет базовую линию, перебирает
  стратегии, ставит лучшую через `install_service()` и запускает службу.
  Аварийный путь: при неудаче возвращает исходную команду службы
  (`service_image_path()` из реестра).
- Запускается отдельным процессом с UAC:
  `python -m bypass_tray --sweep-strategies --out … --report …`.

## Туннелирование Happ

`happ.py`:

- `build_profiles()` — собирает профили «Обходы» и «Обходы (резерв)» из
  существующего профиля, сохраняя DNS и гео-настройки; `directSites` —
  российские сервисы, `proxySites` — Telegram/боты/заблокированные сервисы
  (в резервном добавляются YouTube и Discord).
- `enable_routing()` / `disable_routing()` — реестровые переключатели
  `useRouting`, `selectedRoutingRule`, `subsConnectOnOpen`. Без них профиль
  не применяется: Happ пишет `Connection latch: routing disabled`.
- `activate()` — включение с проверкой: снимок настроек → метка → запись
  профилей → подключение → проверка `ya.ru`/`vk.com` → при поломке откат.
- `recover_if_pending()` — если метка осталась с прошлого запуска, значит
  включение не подтвердилось; настройки откатываются при старте приложения.

## Обновления

`updates.check(state)`:

- тянет `releases/latest` у `Flowseal/zapret-discord-youtube` и
  `Flowseal/tg-ws-proxy` (urllib, без токена);
- сравнивает тег с последним увиденным в `state.seen_versions`;
- при новом теге — возвращает список для уведомления и заметку `updates_note`;
- `TrayApp.notify` при неудаче штатного `icon.notify` мигает иконкой;
  текст в любом случае виден в подсказке и в панели.

## Конфиг и состояние

- `%APPDATA%\bypass-tray\config.json` — пути и параметры (см. CONFIG.md).
- `%APPDATA%\bypass-tray\state.json` — `seen_versions`, `updates_note`.

## Отличия от Linux-версии

| Linux («Обходы») | Windows (bypass-tray) |
|---|---|
| Quickshell/QML виджет панели | pystray + Tkinter (Canvas-виджеты) |
| `bypass-status` (systemctl, ip rule) | `status.py` (psutil, порт, реестр службы) |
| zapret nfqws + автоподбор | zapret winws + выбор `.bat` вручную |
| wowvpn (Xray+sing-box) | Happ (GUI, внешний) |
| systemd-таймер апдейтов | проверка в цикле приложения |
