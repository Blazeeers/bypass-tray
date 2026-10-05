# Архитектура

## Файлы

```
bypass-tray/
├── run.bat / install.bat          # запуск и установка (venv)
├── requirements.txt               # pystray, Pillow, psutil
├── src/bypass_tray/
│   ├── __main__.py                # python -m bypass_tray
│   ├── app.py                     # трей: иконка, меню, цикл обновления статусов
│   ├── ui.py                      # окно-панель (Tkinter)
│   ├── status.py                  # сбор статусов (процессы, порт)
│   ├── actions.py                 # запуск/остановка компонентов
│   ├── updates.py                 # проверка релизов GitHub
│   └── config.py                  # конфиг/состояние, автопоиск путей
└── docs/…
```

## Потоки

- Главный поток — `pystray.Icon.run()` (цикл сообщений трея).
- Фоновый поток `_loop` — раз в `refresh_seconds` собирает статусы, обновляет
  иконку/меню/подсказку, раз в `update_check_hours` проверяет обновления.
- Панель Tkinter поднимается **в отдельном потоке** по действию пользователя;
  внутри — `after()`-поллинг `controller.status`. При многократном
  открытии/закрытии следить за гонками (см. TODO в SPEC).

## Схема статусов

`status.collect(cfg)` — единственный источник правды для иконки, меню и панели:

```json
{
  "happ":       {"running": true,  "process": "Happ.exe"},
  "zapret":     {"running": true,  "process": "winws.exe", "digest": "multisplit",
                 "dir": "C:\\zapret-discord-youtube", "bats": ["general.bat", "general (ALT).bat"]},
  "tgwsproxy":  {"running": true,  "port": 1443, "listening": true}
}
```

- `running` — найден процесс (psutil, fallback `tasklist`).
- `digest` — значения `--dpi-desync=…` из командной строки `winws.exe`.
- `bats` — файлы `general*.bat` в `zapret_dir` (для выпадающего списка).
- `listening` — TCP-connect на `127.0.0.1:<port>`.

## Действия

`actions.py`:

- `start_zapret(cfg, bat)` — убить `winws.exe`, затем `cmd /c <bat>` в папке zapret;
  окна консоли подавляются `CREATE_NO_WINDOW`.
- `stop_zapret()` — убить `winws.exe`.
- `start_tgws(cfg)` — запустить exe, если не запущен.
- `open_happ(cfg)` — открыть Happ.

## Обновления

`updates.check(state)`:

- тянет `releases/latest` у `Flowseal/zapret-discord-youtube` и
  `Flowseal/tg-ws-proxy` (urllib, без токена);
- сравнивает тег с последним увиденным в `state.seen_versions`;
- при новом теге — возвращает список для уведомления и заметку `updates_note`.

## Конфиг и состояние

- `%APPDATA%\bypass-tray\config.json` — пути и параметры (см. CONFIG.md).
- `%APPDATA%\bypass-tray\state.json` — `seen_versions`, `updates_note`.

## Отличия от Linux-версии

| Linux («Обходы») | Windows (bypass-tray) |
|---|---|
| Quickshell/QML виджет панели | pystray + Tkinter |
| `bypass-status` (systemctl, ip rule) | `status.py` (psutil, порт) |
| zapret nfqws + автоподбор | zapret winws + выбор `.bat` вручную |
| wowvpn (Xray+sing-box) | Happ (GUI, внешний) |
| systemd-таймер апдейтов | проверка в цикле приложения |
