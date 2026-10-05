# bypass-tray

Windows-трей-приложение, которое собирает в одном месте состояние всех «обходов»
на ПК, позволяет управлять ими и оповещает об обновлениях:

- **Happ** — VPN-клиент (Xray), работает не всегда; через него должны идти только
  заблокированные в РФ сервисы и западные соцсети;
- **zapret** (`general (ALT).bat`, `winws.exe`) — обход DPI для YouTube и Discord
  (они должны идти **напрямую**, не через VPN);
- **tg-ws-proxy** — локальный MTProto-прокси для Telegram Desktop (работает всегда).

Иконка в трее меняется по состоянию (Happ выключен / zapret не запущен /
tg-ws-proxy не работает / всё ок / есть обновления). По клику — панель со
статусами и кнопками; в меню — быстрые действия и выбор стратегии zapret.

> Это Windows-аналог Linux-виджета «Обходы» из проекта
> `VPN-TGWSProxy-Zapret-orchestrate`. Бэкенд другой: на Windows нет nfqws,
> systemd и т.п.

## Установка

Требуется Python 3.10+ (при установке отметьте «Add Python to PATH»).

```bat
git clone <repo-url> bypass-tray
cd bypass-tray
install.bat
run.bat
```

`install.bat` создаёт `.venv` и ставит зависимости (`pystray`, `Pillow`, `psutil`).
`run.bat` запускает приложение без окна консоли.

Автозапуск: положите ярлык на `run.bat` в `shell:startup`
(Win+R → `shell:startup`).

## Настройка

При первом запуске создаётся `%APPDATA%\bypass-tray\config.json`; пути
(`zapret_dir`, `tgws_exe`, `happ_exe`) определяются автоматически по типовым
местам, но их можно вписать вручную. Полное описание — [docs/CONFIG.md](docs/CONFIG.md).

## Документация

- [docs/SPEC.md](docs/SPEC.md) — полное функциональное описание (для агента:
  что должно получиться, критерии приёмки, роадмап).
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — модули, схема данных, API статусов.
- [docs/HAPP-ROUTING.md](docs/HAPP-ROUTING.md) — предлагаемая схема
  сплит-туннеля Happ (YouTube/Discord — напрямую под zapret, западные соцсети — в VPN).
- [docs/CONFIG.md](docs/CONFIG.md) — конфиг и пути.

## Статус

Каркас/прототип. Работает на Windows, но требует проверки на конкретной машине
(пути, имена процессов, поведение `.bat`). Задачи для доработки — в конце
[docs/SPEC.md](docs/SPEC.md).

## Лицензия

MIT (см. [LICENSE](LICENSE)).
