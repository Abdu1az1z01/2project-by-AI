"""Точка входа.

    python -m assistant chat        — диалог: вы даёте задачи, помощник выполняет
    python -m assistant auto        — автономный режим: сам наблюдает и действует
    python -m assistant auto --once — один цикл анализа (удобно для проверки)

Чтобы приостановить автономный режим, создайте файл STOP в папке data_dir
(по умолчанию ~/.ai-assistant/STOP) или нажмите Ctrl+C.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import deque

import anthropic

from .agent import Agent
from .config import Config
from .sensors import Observer
from .tools import ToolBox


def confirm_cli(name: str, args: dict) -> bool:
    print(f"\n❓ Помощник хочет выполнить: {name}")
    for k, v in args.items():
        v = str(v)
        print(f"     {k}: {v[:300] + '…' if len(v) > 300 else v}")
    return input("   Разрешить? [y/N] ").strip().lower() in {"y", "yes", "д", "да"}


def run_chat(cfg: Config) -> None:
    tools = ToolBox(cfg, confirm=confirm_cli if cfg.confirm_in_chat else None)
    agent = Agent(cfg, tools)
    messages: list[dict] = []
    print("AI-помощник готов. Пишите задачу (выход — 'exit').")
    while True:
        try:
            text = input("\n👤 ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in {"exit", "quit", "выход"}:
            break
        if not text:
            continue
        checkpoint = len(messages)
        messages.append({"role": "user", "content": text})
        try:
            agent.run(messages)
        except anthropic.APIError as e:
            print(f"Ошибка API: {e}")
            del messages[checkpoint:]  # откатываем незавершённый ход, чтобы история осталась корректной


def run_auto(cfg: Config, once: bool) -> None:
    auto = cfg.autonomous
    tools = ToolBox(cfg, confirm=None)
    agent = Agent(cfg, tools)
    observer = Observer(cfg)
    stop_file = cfg.data_dir / "STOP"
    recent_cycles: deque[float] = deque()
    print(f"Автономный режим запущен. Проверка каждые {auto.interval_seconds} с. "
          f"Пауза: создайте файл {stop_file}")

    # Первый цикл — общий осмотр, чтобы помощник «познакомился» с компьютером.
    pending = ["Первый запуск: осмотри доступные папки и состояние системы, составь план."] if once else []
    while True:
        try:
            if stop_file.exists():
                time.sleep(auto.interval_seconds)
                continue
            events = pending or observer.observe()
            pending = []
            now = time.time()
            while recent_cycles and now - recent_cycles[0] > 3600:
                recent_cycles.popleft()
            if events and len(recent_cycles) >= auto.max_cycles_per_hour:
                print("Лимит циклов в час исчерпан — события отложены.")
                pending = events
            elif events:
                recent_cycles.append(now)
                print(f"\n=== {time.strftime('%H:%M:%S')} — замечено событий: {len(events)} ===")
                for e in events:
                    print("  •", e.splitlines()[0])
                try:
                    agent.autonomous_cycle(events)
                except anthropic.APIError as e:
                    print(f"Ошибка API: {e}")
                    pending = events
            if once:
                return
            time.sleep(auto.interval_seconds)
        except KeyboardInterrupt:
            print("\nОстановлено.")
            return


def main() -> None:
    parser = argparse.ArgumentParser(prog="assistant", description="Автономный AI-помощник для компьютера")
    parser.add_argument("mode", choices=["chat", "auto"])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true", help="в режиме auto: выполнить один цикл и выйти")
    args = parser.parse_args()
    try:
        cfg = Config.load(args.config)
    except FileNotFoundError:
        sys.exit(f"Не найден {args.config}. Скопируйте config.example.yaml в config.yaml и настройте.")
    if args.mode == "chat":
        run_chat(cfg)
    else:
        run_auto(cfg, args.once)


if __name__ == "__main__":
    main()
