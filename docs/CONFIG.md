# Конфиг и пути

## Где лежит

- Конфиг: `%APPDATA%\bypass-tray\config.json`
- Состояние: `%APPDATA%\bypass-tray\state.json`
- `%APPDATA%` обычно `C:\Users\<вы>\AppData\Roaming`.

Файлы создаются при первом запуске; пути к компонентам определяются
автоматически, но их можно вписать вручную.

## Поля config.json

| Поле | Описание | По умолчанию |
|---|---|---|
| `zapret_dir` | Папка с `general*.bat` и `bin\winws.exe` | автопоиск |
| `zapret_bat` | Какую стратегию запускать | `general (ALT).bat` |
| `tgws_exe` | Путь к `TgWsProxy_windows.exe` | автопоиск |
| `tgws_port` | Порт локального MTProto-прокси | `1443` |
| `happ_exe` | Путь к `Happ.exe` | автопоиск |
| `refresh_seconds` | Период обновления статусов, с | `5` |
| `update_check_hours` | Период проверки обновлений, ч | `24` |

## Автопоиск zapret

Проверяются каталоги (по порядку):

1. `%USERPROFILE%\zapret-discord-youtube`
2. `C:\zapret-discord-youtube`
3. `%USERPROFILE%\Desktop\zapret-discord-youtube`
4. `%USERPROFILE%\Downloads\zapret-discord-youtube`

Если ваша папка в другом месте — укажите путь в `config.json` вручную.

## Автопоиск tg-ws-proxy

Имена: `TgWsProxy_windows.exe`, `TgWsProxy.exe`, `tg-ws-proxy.exe`.
Каталоги: `Downloads`, `Desktop`, `%LOCALAPPDATA%\Programs\tg-ws-proxy`,
`%USERPROFILE%\tg-ws-proxy`.

## Автопоиск Happ

`%LOCALAPPDATA%\Programs\Happ\Happ.exe`, `%LOCALAPPDATA%\Happ\Happ.exe`,
`C:\Program Files\Happ\Happ.exe`, `C:\Program Files (x86)\Happ\Happ.exe`.

## Состояние (state.json)

| Поле | Описание |
|---|---|
| `seen_versions` | Последние увиденные теги релизов zapret/tg-ws-proxy |
| `updates_note` | Текст текущего уведомления об обновлениях |
