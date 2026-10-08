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
| `theme` | Оформление панели: `auto` (по системе), `light`, `dark` | `auto` |
| `happ_extra_sites` | Дополнительные домены, которые вести через туннель Happ | `[]` |
| `happ_auto_fallback` | Автоматически уводить YouTube/Discord в туннель, когда zapret или tg-ws-proxy упали | `false` |

### Собственный клиент Xray (без Happ)

| Поле | Описание | По умолчанию |
|---|---|---|
| `xray_subscription` | Ссылка на подписку; отдаёт серверы `vless://` | ваша ссылка |
| `xray_exe` | Путь к своему ядру Xray; пусто = `<проект>\bin\xray.exe` | автокопия |
| `xray_port` | Локальный порт прокси (SOCKS и HTTP на одном порту) | `10818` |
| `xray_system_proxy` | Прописывать прокси в систему, чтобы им пользовались браузеры | `true` |
| `xray_extra_domains` | Дополнительные домены, которые тоже вести через прокси | `[]` |
| `xray_stream_fallback` | Уводить YouTube/Discord в прокси, когда zapret не работает | `true` |

Маршрут по умолчанию — **напрямую**: через сервер идёт только явно
перечисленное. YouTube и Discord в прокси не входят — их закрывает zapret;
в сервер они уходят только при остановленном zapret (резервный режим,
`xray_stream_fallback`). Российские сервисы (`geosite:category-ru`,
`geoip:ru`) всегда идут напрямую.

### Выбор сервера и автопереподключение

Список серверов (`xray-servers.json`) собирается из подписки: каждый сервер
проверяется на доступность, недоступные отбрасываются, оставшиеся
сортируются по задержке. В панели этот список показан в выпадающем поле —
можно выбрать конкретный сервер и нажать «Поставить», либо «Обновить», чтобы
перепроверить.

Раз в ~30 секунд приложение проверяет туннель **по-настоящему**: запрос идёт
на заблокированный сайт, то есть обязательно через сервер. Две неудачи подряд
— сервер считается упавшим, он исключается из выбора, и Xray перезапускается
на следующем по скорости живом сервере (с уведомлением в трее).

При первом подключении ядро Xray (`xray.exe`, `geoip.dat`, `geosite.dat`)
копируется в `<проект>\bin` из папки Happ, поэтому дальше Happ не нужен вовсе.
Прокси слушает только `127.0.0.1` и не требует прав администратора.

Пример дополнительных страниц в туннель:

```json
"happ_extra_sites": ["domain:example.com", "geosite:netflix"]
```

Поддерживаются префиксы `domain:` (имя и поддомены) и `geosite:` (наборы
Loyalsoldier, которые Happ уже подгрузил).

## Автопоиск zapret

Проверяются каталоги (по порядку):

1. `%USERPROFILE%\zapret-discord-youtube`
2. `C:\zapret-discord-youtube`
3. `%USERPROFILE%\Desktop\zapret-discord-youtube`
4. `%USERPROFILE%\Downloads\zapret-discord-youtube`
5. `<диск>:\zapret-discord-youtube`, `<диск>:\Zapret`, `<диск>:\zapret`
6. Ограниченный по глубине (2 уровня) поиск по всем дискам — признак папки:
   есть `bin\winws.exe` или `general*.bat`. Это находит вложенные раскладки
   вроде `G:\Перенос с ПК\Zapret`.

Если ваша папка в другом месте — укажите путь в `config.json` вручную.

> **Важно.** Если на машине несколько копий zapret, автопоиск выбирает первую
> найденную по порядку выше, а не обязательно ту, под которую установлена
> служба `zapret`. Сверяйтесь с записью `zapret_dir` в `config.json` и при
> необходимости поправьте её вручную.

## Автопоиск tg-ws-proxy

Имена: `TgWsProxy_windows.exe`, `TgWsProxy.exe`, `tg-ws-proxy.exe`.
Каталоги: `Downloads`, `Desktop`, `%LOCALAPPDATA%\Programs\tg-ws-proxy`,
`%USERPROFILE%\tg-ws-proxy`, затем `<диск>:\tg-ws-proxy` и
`<диск>:\TgWsProxy`.

## Автопоиск Happ

`%LOCALAPPDATA%\Programs\Happ\Happ.exe`, `%LOCALAPPDATA%\Happ\Happ.exe`,
`C:\Program Files\Happ\Happ.exe`, `C:\Program Files (x86)\Happ\Happ.exe`,
`C:\Program Files\FlyFrogLLC\Happ\Happ.exe`,
`C:\Program Files (x86)\FlyFrogLLC\Happ\Happ.exe`,
`%LOCALAPPDATA%\Programs\FlyFrogLLC\Happ\Happ.exe`.

Последний резерв — путь к `Happ.exe` у уже запущенного процесса (через psutil).

## Состояние (state.json)

| Поле | Описание |
|---|---|
| `seen_versions` | Последние увиденные теги релизов zapret/tg-ws-proxy |
| `updates_note` | Текст текущего уведомления об обновлениях |

## Текущая машина (развёрнуто)

| Что | Значение |
|---|---|
| Проект | `F:\Plugins\bypass-tray` |
| Python | 3.14 (`C:\Users\Egor Egorov\AppData\Local\Python\pythoncore-3.14-64`) |
| venv | `F:\Plugins\bypass-tray\.venv` |
| zapret | `C:\Program Files\Zapret` (служба `zapret`, автозапуск) |
| Happ | `C:\Program Files\FlyFrogLLC\Happ\Happ.exe` |
| tg-ws-proxy | `C:\Users\Egor Egorov\Desktop\TgWsProxy_windows.exe` |
| Автозапуск | ярлык `bypass-tray.lnk` в `shell:startup` → `run.bat` |

### Особенности zapret на этой машине

Zapret установлен **службой Windows** (`sc create zapret ... winws.exe`), поэтому:

- `winws.exe` запущен от `services.exe`, а не от пользовательской консоли;
- командная строка процесса `winws.exe` обычному пользователю не видна, но
  приложение берёт аргументы из реестра
  (`HKLM\SYSTEM\CurrentControlSet\Services\zapret` → `ImagePath`) — это
  доступно без прав администратора. Поэтому и дайджест стратегии, и её
  сопоставление с `general*.bat` работают (сейчас определяется
  `general (ALT).bat`);
- **запуск и остановка** выполняются через службу: `sc stop zapret` /
  `sc start zapret`. Без прав администратора приложение поднимает запрос UAC;
- **смена стратегии** службы делается в самом zapret: `service.bat` → пункт 1
  (переустановка службы) → выбор `.bat`. Приложение открывает `service.bat`
  от администратора по кнопке «Сменить стратегию службы» в меню трея;
- кнопки прямого запуска `.bat` в панели относятся к портативной установке
  (когда службы нет) и для этого случая не показываются.

Вторая, неактивная копия лежит в `G:\Перенос с ПК\Zapret` (перенос с другого
ПК). Автопоиск её игнорирует, так как `C:\Program Files\Zapret` находится
раньше по списку.

## Happ на этой машине

| Что | Значение |
|---|---|
| Версия | 4.2.1 (`C:\Program Files\FlyFrogLLC\Happ`) |
| Режим | TUN (`tun: true`, `tunProvider: xray`), `xray.exe` поднимает демон |
| Маршруты | `%LOCALAPPDATA%\Happ\routing.json` |
| Настройки | `HKCU\Software\Happ\OrganizationDefaults\Preferences` |
| Подписка | WOW VPN, `subs.db` (данные зашифрованы) |
| Резервная копия | `routing.json.bypass-tray-backup` |

Файл `TunnelSettings\Routing\useRouting` по умолчанию содержит строку
`@Invalid()` — это значит «маршрутизация не включалась», и профиль не
применяется. Подробности и ограничения — в
[HAPP-ROUTING.md](HAPP-ROUTING.md).

## Файлы, которые создаёт приложение

| Файл | Назначение |
|---|---|
| `%APPDATA%\bypass-tray\config.json` | настройки |
| `%APPDATA%\bypass-tray\state.json` | версии релизов, заметки, последний подбор |
| `%APPDATA%\bypass-tray\sweep-result.json` | отчёт автоподбора стратегий |
| `%APPDATA%\bypass-tray\sweep-progress.json` | ход автоподбора (для панели) |
| `%APPDATA%\bypass-tray\happ-activation.pending.json` | метка незавершённого включения туннеля |
