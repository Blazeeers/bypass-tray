# AGENTS.md — инструкция для агента на Windows

Коротко: это Windows-трей-приложение на Python (pystray + Tkinter), которое
показывает статус обходов (Happ, zapret/winws, tg-ws-proxy), управляет ими и
уведомляет об обновлениях. Нужно развернуть, проверить и довести до критериев
приёмки. Полное ТЗ — [docs/SPEC.md](docs/SPEC.md).

## Быстрый старт

```bat
install.bat
run.bat
```

Проверка без GUI (удобно для тестов):

```bat
.venv\Scripts\python.exe -m bypass_tray --status
```

Конфиг: `%APPDATA%\bypass-tray\config.json` (пути к zapret/tg-ws-proxy/Happ), при
необходимости поправь вручную.

## Что сделать в первую очередь

1. Запустить `--status` и убедиться, что пути и процессы определяются
   (Happ/winws/tg-ws-proxy). Поправить автопоиск/конфиг, если нет.
2. Проверить трей и панель: запуск/остановка zapret, выбор `general*.bat`,
   запуск tg-ws-proxy, открытие Happ, иконка по состояниям.
3. Настроить сплит-туннель Happ по [docs/HAPP-ROUTING.md](docs/HAPP-ROUTING.md):
   YouTube/Discord — напрямую (под zapret), западные соцсети — через VPN.
   Проверить критерии из SPEC §5.
4. Проверить проверку обновлений (в т.ч. всплывающее уведомление).
5. Доработать по списку TODO в [docs/SPEC.md](docs/SPEC.md) §6.

## Критерии готовности

См. чек-лист в [docs/SPEC.md](docs/SPEC.md) §5. Главное: при включённом Happ
YouTube/Discord работают через zapret (не через VPN), а заблокированные сервисы —
только через VPN.

## Заметки

- Код в `src/bypass_tray/`; после правок проверяй
  `.venv\Scripts\python.exe -m compileall src`.
- Не коммить `.venv/`.
- Если меняешь автопоиск путей — обнови [docs/CONFIG.md](docs/CONFIG.md).
