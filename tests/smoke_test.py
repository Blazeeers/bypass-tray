"""Дымовые проверки bypass-tray без разрушительных действий.

Запуск:
    .venv\\Scripts\\python.exe tests\\smoke_test.py

Проверяет схему статусов, определение стратегии, иконки, меню и отрисовку
панели. Ничего не останавливает и не запускает: обход не затрагивается.

Примечание: при выходе CPython 3.14 может напечатать
``RuntimeWarning: the Tcl interpreter is leaked ...`` (gh-83274) — это
особенность завершения процесса с Tk-интерпретатором, а не сбой проверок.
Код возврата остаётся 0, если все проверки прошли.
"""

from __future__ import annotations

import gc
import sys
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bypass_tray import actions, config, icons, status, strategy, theme, updates  # noqa: E402
from bypass_tray.app import TrayApp  # noqa: E402
from bypass_tray.ui import Panel  # noqa: E402

RESULTS: list[tuple[bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append((bool(condition), name))
    mark = "OK  " if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    return bool(condition)


def main() -> int:
    print("=" * 68)
    print("bypass-tray: дымовые проверки")
    print("=" * 68)

    cfg = config.load_config()

    # --- 1. конфиг и автопоиск ------------------------------------------
    print("\n-- конфиг --")
    check("zapret_dir найден", bool(cfg.get("zapret_dir")), cfg.get("zapret_dir", ""))
    check("tgws_exe найден", bool(cfg.get("tgws_exe")), cfg.get("tgws_exe", ""))
    check("happ_exe найден", bool(cfg.get("happ_exe")), cfg.get("happ_exe", ""))
    check("папка zapret существует", Path(cfg.get("zapret_dir") or "").is_dir())

    # --- 2. схема статусов (SPEC §3) ------------------------------------
    print("\n-- статусы --")
    state = status.collect(cfg)
    for section in ("happ", "zapret", "tgwsproxy"):
        check(f"секция {section} присутствует", section in state)
    check("happ.running — bool", isinstance(state["happ"]["running"], bool))
    check("zapret.running — bool", isinstance(state["zapret"]["running"], bool))
    check("zapret.bats — список", isinstance(state["zapret"]["bats"], list))
    check("zapret.bats непуст", len(state["zapret"]["bats"]) > 0,
          f"{len(state['zapret']['bats'])} стратегий")
    check("tgwsproxy.listening — bool", isinstance(state["tgwsproxy"]["listening"], bool))

    # --- 3. определение запущенной стратегии ----------------------------
    print("\n-- стратегия (SPEC §6 п.3) --")
    zapret = state["zapret"]
    check("командная строка получена", bool(zapret["cmdline_source"]), zapret["cmdline_source"])
    check("дайджест стратегии непуст", bool(zapret["digest"]), zapret["digest"])
    check("стратегия сопоставлена с .bat", bool(zapret["strategy"]), zapret["strategy"])
    check("найденная стратегия входит в список", zapret["strategy"] in zapret["bats"],
          zapret["strategy"])

    # --- 4. порт tg-ws-proxy --------------------------------------------
    print("\n-- tg-ws-proxy --")
    check("порт слушается", state["tgwsproxy"]["listening"],
          f":{state['tgwsproxy']['port']}")

    # --- 5. маршруты Happ -----------------------------------------------
    print("\n-- Happ --")
    routing = state["happ_routing"]
    check("routing.json прочитан", routing["available"])
    check("активный профиль определён", bool(routing["active"]), routing["active"])

    # --- 6. иконки -------------------------------------------------------
    print("\n-- иконки --")
    for key, color in (("happ", theme.STATE_COLORS["bad"]),
                       ("zapret", theme.STATE_COLORS["warn"]),
                       ("tgws", theme.STATE_COLORS["warn"]),
                       ("update", theme.STATE_COLORS["warn"]),
                       ("ok", theme.STATE_COLORS["ok"]),
                       ("idle", theme.STATE_COLORS["dim"])):
        image = icons.make_icon(key, color)
        check(f"иконка «{key}» {image.size[0]}px", image.size == (64, 64) and image.mode == "RGBA")

    # --- 7. приложение, меню, здоровье -----------------------------------
    print("\n-- приложение --")
    controller = TrayApp()
    controller.last_check = time.time()
    controller.refresh()
    key, color, title = controller.health()
    check("health() возвращает состояние", key in
          ("happ", "zapret", "tgws", "update", "ok", "idle"), f"{key} / {title}")
    menu = controller.build_menu()
    items = list(menu.items)
    check("меню собрано", len(items) >= 10, f"{len(items)} пунктов")
    if zapret["as_service"]:
        labels = [it.text for it in items]
        check("пункт «zapret» в меню", any("zapret" in (t or "").lower() for t in labels))

    # --- 8. обновления (SPEC §2.4) ---------------------------------------
    print("\n-- обновления --")
    result = updates.check(dict(controller.state))
    components = result["result"]["components"]
    check("проверка обновлений выполнена", "checked" in result["result"])
    check("опрошены оба репозитория", set(components) == {"zapret", "tgwsproxy"},
          ", ".join(sorted(components)))
    for name, info in components.items():
        check(f"релиз {name} получен", bool(info["latest"]), info["latest"] or "нет ответа")

    # --- 9. панель -------------------------------------------------------
    print("\n-- панель --")
    panel = Panel(controller)
    root = tk.Tk()
    panel._root = root
    panel._build(root, theme.Theme(False))
    for _ in range(8):
        root.update()
        time.sleep(0.05)
    height = root.winfo_reqheight()
    check("панель отрисована", height > 600, f"высота {height}px")
    check("карточки не схлопнуты", height > 500)
    for row_key in ("happ", "zapret", "tgws", "updates", "route"):
        row = panel._rows.get(row_key)
        check(f"строка «{row_key}» заполнена",
              bool(row and row.get("value") and row["value"].cget("text")))
    combo = panel._combobox
    check("список стратегий в UI", combo is not None and len(combo["values"]) > 0,
          f"{len(combo['values']) if combo else 0} значений")
    root.destroy()
    # Панель держит PhotoImage, а те через ссылку — интерпретатор Tcl. Пока они
    # живы, интерпретатор соберётся позже — возможно, уже в потоке панели, и
    # Python сообщит об утечке. Поэтому отпускаем их здесь, в своём потоке.
    panel._release()
    del root
    gc.collect()

    # --- 10. безопасность действий ---------------------------------------
    print("\n-- действия (не разрушительные) --")
    check("tg-ws-proxy уже запущен → повтор не создаётся", actions.start_tgws(cfg) is False)
    check("kill_process возвращает отчёт",
          set(actions.kill_process("__нет_такого__")) == {"killed", "denied"})
    check("hide_console_windows не падает",
          isinstance(actions.hide_console_windows("__нет_такого__"), int))
    if zapret["as_service"]:
        check("служба zapret обнаружена", actions.service_state() in ("running", "stopped"),
              actions.service_state())
    check("is_admin() доступен", isinstance(state["admin"], bool), str(state["admin"]))

    # --- 11. панель: показ, повторный показ, закрытие (SPEC §6 п.4) ------
    print("\n-- панель: жизненный цикл --")
    live = Panel(controller)
    live.open()
    time.sleep(1.6)
    first = live._root
    check("панель открылась и видима",
          first is not None and bool(first.viewable() if hasattr(first, "viewable") else first.winfo_viewable()))
    live.open()  # повторный показ должен быть безопасным
    time.sleep(0.8)
    check("повторный показ не создаёт второе окно", live._root is first)
    live.close()
    time.sleep(1.5)
    check("панель закрылась и освободила объекты", live._root is None)

    # --- 12. Happ и стратегии (безопасно, без изменений в системе) --------
    print("\n-- Happ --")
    from bypass_tray import happ  # noqa: E402

    routing_data = happ.read_routing()
    check("routing.json читается", bool(routing_data.get("routings")))
    check("маршруты Happ оставлены выключенными", not happ.routing_enabled(),
          "клиент Xray работает без Happ")
    settings = happ.routing_settings()
    check("настройки Happ читаются", "useRouting" in settings, str(settings.get("useRouting")))
    check("routing_enabled() — bool", isinstance(happ.routing_enabled(), bool),
          "вкл" if happ.routing_enabled() else "выкл")
    check("is_connected() — bool", isinstance(happ.is_connected(), bool))
    check("пустой метки незавершённого включения", not happ.pending_activation())

    normal = happ.build_profiles(routing_data)
    check("оба профиля собираются", len(normal) == 2)
    if len(normal) == 2:
        plain, fallback = normal
        check("в обычном профиле YouTube не в туннеле",
              not any("youtube" in s for s in plain["proxySites"]))
        check("в резервном профиле YouTube в туннеле",
              any("youtube" in s for s in fallback["proxySites"]))
        check("Telegram в туннеле всегда",
              all(any("telegram" in s for s in p["proxySites"]) for p in normal))
        check("российские сервисы только напрямую",
              any("ya.ru" in s for s in plain["directSites"])
              and not any("ya.ru" in s for s in plain["proxySites"]))
        check("globalProxy выключен", not plain.get("globalProxy"))
        assets = [p.get("locationAssetPath") for p in normal]
        check("geo-файлы указывают на существующий каталог",
              all(Path(a).is_dir() for a in assets if a), str(assets[0]))

    # --- 13. разбор командной строки стратегии ----------------------------
    print("\n-- стратегии --")
    exe, rest = strategy.split_command('"C:\\Program Files\\Zapret\\bin\\winws.exe" --wf-tcp=80,443')
    check("split_command: путь к exe", exe == r"C:\Program Files\Zapret\bin\winws.exe", exe)
    check("split_command: аргументы", rest == "--wf-tcp=80,443", rest)
    exe2, rest2 = strategy.split_command("")
    check("split_command: пустая строка", exe2 == "" and rest2 == "")
    check("цели проверки заданы", len(strategy.TARGETS) >= 2)
    check("контрольный адрес задан", bool(strategy.CONTROL[1]))
    check("кандидаты найдены", len(strategy.default_bats(cfg["zapret_dir"])) > 0,
          f"{len(strategy.default_bats(cfg['zapret_dir']))} шт.")

    probe = strategy.measure(timeout=10)
    check("замер связи выполняется", probe["total"] >= 2,
          f"{probe['score']}/{probe['total']}, контроль {'ок' if probe['control_ok'] else 'нет'}")

    # --- 14. логика резервного профиля ------------------------------------
    print("\n-- резервный профиль --")
    check("пока маршруты выключены, резерв не нужен",
          controller.auto_fallback_needed() is False)
    controller.happ = {"routing": True, "connected": True, "active": "Обходы"}
    controller.status = {"zapret": {"running": False}, "tgwsproxy": {"listening": True}}
    check("zapret упал → нужен резерв", controller.auto_fallback_needed() is True)
    controller.status = {"zapret": {"running": True}, "tgwsproxy": {"listening": False}}
    check("прокси упал → нужен резерв", controller.auto_fallback_needed() is True)
    controller.status = {"zapret": {"running": True}, "tgwsproxy": {"listening": True}}
    check("всё работает → резерв не нужен", controller.auto_fallback_needed() is False)
    controller.happ = {"routing": False, "connected": False, "active": ""}
    controller.status = {"zapret": {"running": False}, "tgwsproxy": {"listening": False}}
    check("маршруты выключены → резерв не нужен", controller.auto_fallback_needed() is False)

    # --- 15. собственный клиент Xray --------------------------------------
    print("\n-- прокси Xray (без Happ) --")
    from bypass_tray import xray  # noqa: E402

    check("ядро Xray доступно", xray.ensure_core() is not None, str(xray.bin_dir()))
    links = xray.fetch_subscription()
    check("подписка отдаёт серверы", len(links) > 0, f"{len(links)} шт.")
    if links:
        parsed = xray.parse_vless(links[0])
        check("ссылка vless разобрана", bool(parsed["host"]) and parsed["port"] > 0,
              f"{parsed['host']}:{parsed['port']}")
        check("outbound содержит vless", parsed["outbound"]["protocol"] == "vless")
        alive = xray.rank_servers(links)
        check("есть доступные серверы", len(alive) > 0,
              f"{len(alive)} из {len(links)}, лучший {alive[0]['latency']}мс"
              if alive else "нет доступных")
        if alive:
            config_json = xray.build_config(alive[0], 10899)
            rules = config_json["routing"]["rules"]
            check("конфиг: РФ напрямую",
                  any("geosite:category-ru" in (r.get("domain") or []) for r in rules))
            check("конфиг: заблокированное через прокси",
                  any(r.get("outboundTag") == "proxy" for r in rules))
            check("конфиг: прокси слушает только 127.0.0.1",
                  config_json["inbounds"][0]["listen"] == "127.0.0.1")
            check("конфиг: маршрут по умолчанию — напрямую",
                  config_json["outbounds"][0]["tag"] == "direct")
            proxy_rule = next(r for r in rules if r.get("outboundTag") == "proxy")
            check("YouTube/Discord НЕ в прокси (идут через zapret)",
                  not any(("youtube" in d or "discord" in d)
                          for d in proxy_rule["domain"]))
            check("Instagram в прокси",
                  any("instagram" in d for d in proxy_rule["domain"]))

            stream_cfg = xray.build_config(alive[0], 10899, None, True)
            stream_rule = next(r for r in stream_cfg["routing"]["rules"]
                               if r.get("outboundTag") == "proxy")
            check("резервный режим: YouTube/Discord уходят в прокси",
                  any("youtube" in d for d in stream_rule["domain"])
                  and any("discord" in d for d in stream_rule["domain"]))
            check("резервный режим: россияне всё равно напрямую",
                  all(r.get("outboundTag") == "direct"
                      for r in stream_cfg["routing"]["rules"][:2]))
    proxy_status = xray.status()
    check("status() возвращает состояние",
          "running" in proxy_status and "port" in proxy_status,
          "подключён" if proxy_status.get("running") else "выключен")
    check("system_proxy_enabled() — bool", isinstance(xray.system_proxy_enabled(), bool))

    # --- итог -------------------------------------------------------------
    failed = [name for ok, name in RESULTS if not ok]
    print("\n" + "=" * 68)
    print(f"Пройдено: {len(RESULTS) - len(failed)} из {len(RESULTS)}")
    if failed:
        print("Провалено:")
        for name in failed:
            print(f"  - {name}")
    print("=" * 68)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
