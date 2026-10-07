"""Точка входа.

    python -m assistant chat        — диалог: вы даёте задачи, помощник выполняет
    python -m assistant auto        — автономный режим: сам наблюдает и действует
    python -m assistant auto --once — один цикл анализа (удобно для проверки)
    python -m assistant voice       — голосовой режим: слушает, отвечает вслух, подсказывает сам

Чтобы приостановить автономный режим, создайте файл STOP в папке data_dir
(по умолчанию ~/.ai-assistant/STOP) или нажмите Ctrl+C.
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
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


class HourlyLimit:
    """Не больше N циклов анализа за последний час (ограничение расходов на API)."""

    def __init__(self, per_hour: int):
        self.per_hour = per_hour
        self.times: deque[float] = deque()

    def try_acquire(self) -> bool:
        now = time.time()
        while self.times and now - self.times[0] > 3600:
            self.times.popleft()
        if len(self.times) >= self.per_hour:
            return False
        self.times.append(now)
        return True


def run_auto(cfg: Config, once: bool) -> None:
    auto = cfg.autonomous
    tools = ToolBox(cfg, confirm=None)
    agent = Agent(cfg, tools)
    observer = Observer(cfg)
    stop_file = cfg.data_dir / "STOP"
    limit = HourlyLimit(auto.max_cycles_per_hour)
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
            if events and not limit.try_acquire():
                print("Лимит циклов в час исчерпан — события отложены.")
                pending = events
            elif events:
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


VOICE_STYLE = """
Сейчас ты общаешься с пользователем ГОЛОСОМ: твой ответ читает синтезатор речи.
- Отвечай коротко: 1–3 предложения, разговорно, как живой собеседник.
- Никаких списков, таблиц, markdown, эмодзи и длинных путей — называй только имя файла или папки.
- Можно давать советы по любым вопросам, не только про компьютер.
- Если для ответа нужно действие на компьютере — выполни его инструментами и коротко скажи результат.
"""

ADVICE_PROMPT = (
    "\n\nТы общаешься с пользователем голосом. Если есть что-то, о чём действительно стоит сказать "
    "или что посоветовать прямо сейчас, ответь 1–2 короткими предложениями. "
    "Если говорить не о чем — ответь ровно одним символом «-»."
)

EXIT_PHRASES = ("выход", "выключись", "до свидания", "пока пока", "заверши работу")


def run_voice(cfg: Config) -> None:
    try:
        from .voice import Voice, VoiceConfig
    except ImportError as e:
        sys.exit(f"Не установлены голосовые библиотеки ({e.name}). Выполните: pip install -r requirements-voice.txt")

    vcfg = VoiceConfig.from_dict(cfg.voice)
    voice = Voice(vcfg)
    tools = ToolBox(cfg, confirm=voice.confirm if cfg.confirm_in_chat else None,
                    notify=lambda msg, level: voice.speak(msg))
    agent = Agent(cfg, tools, extra_instructions=VOICE_STYLE)
    messages: list[dict] = []
    wake = vcfg.wake_word.lower().strip()

    # Наблюдатель работает в фоне и кладёт события в очередь; разговор их не блокирует.
    events_q: queue.Queue[list[str]] = queue.Queue()
    limit = HourlyLimit(cfg.autonomous.max_cycles_per_hour)
    stop_file = cfg.data_dir / "STOP"
    if vcfg.advise:
        observer = Observer(cfg)

        def watch() -> None:
            while True:
                time.sleep(cfg.autonomous.interval_seconds)
                if not stop_file.exists():
                    events = observer.observe()
                    if events:
                        events_q.put(events)

        threading.Thread(target=watch, daemon=True).start()

    greeting = "Я вас слушаю."
    if wake:
        greeting += f" Обращайтесь ко мне словом «{vcfg.wake_word}»."
    voice.speak(greeting)
    print("(Скажите «выход», чтобы завершить. Ctrl+C — экстренная остановка.)")

    try:
        while True:
            text = voice.listen(wait_seconds=2)
            if text:
                lowered = text.lower()
                if wake:
                    if wake not in lowered:
                        continue  # фраза не нам
                    text = lowered.split(wake, 1)[1].strip(" ,.!?") or "Я слушаю?"
                if any(p in lowered for p in EXIT_PHRASES):
                    voice.speak("До встречи!")
                    return
                if len(messages) > 80:  # длинный разговор начинаем заново; важное остаётся в памяти
                    messages = []
                checkpoint = len(messages)
                messages.append({"role": "user", "content": text})
                try:
                    voice.speak(agent.run(messages) or "Готово.")
                except anthropic.APIError as e:
                    print(f"Ошибка API: {e}")
                    del messages[checkpoint:]
                    voice.speak("Не получилось связаться с сервером, попробуйте ещё раз.")
                continue

            # Тишина — самое время подсказать, если на компьютере что-то произошло.
            try:
                events = events_q.get_nowait()
            except queue.Empty:
                continue
            if not limit.try_acquire():
                continue
            print(f"\n=== {time.strftime('%H:%M:%S')} — замечено событий: {len(events)} ===")
            try:
                advice = agent.autonomous_cycle(events, extra=ADVICE_PROMPT).strip()
            except anthropic.APIError as e:
                print(f"Ошибка API: {e}")
                continue
            if advice and advice.strip("-–— .") != "":
                voice.speak(advice)
    except KeyboardInterrupt:
        print("\nОстановлено.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="assistant", description="Автономный AI-помощник для компьютера")
    parser.add_argument("mode", choices=["chat", "auto", "voice"])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true", help="в режиме auto: выполнить один цикл и выйти")
    args = parser.parse_args()
    try:
        cfg = Config.load(args.config)
    except FileNotFoundError:
        sys.exit(f"Не найден {args.config}. Скопируйте config.example.yaml в config.yaml и настройте.")
    if args.mode == "chat":
        run_chat(cfg)
    elif args.mode == "voice":
        run_voice(cfg)
    else:
        run_auto(cfg, args.once)


if __name__ == "__main__":
    main()
