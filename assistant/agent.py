"""Цикл «думай → действуй → наблюдай результат» поверх Claude API."""

from __future__ import annotations

import datetime as dt
import platform
from pathlib import Path

import anthropic

from .config import Config
from .tools import TOOL_DEFINITIONS, ToolBox

SYSTEM_PROMPT = """Ты — персональный AI-помощник, работающий прямо на компьютере пользователя.
У тебя ограниченный доступ: только к папкам и программам, перечисленным ниже. Всё остальное
недоступно — если инструмент вернул «Доступ запрещён», не пытайся обойти запрет, а найди другой
путь или сообщи пользователю.

Как работать:
- Сначала осмотрись (list_directory, system_status и т.п.), потом действуй. Не угадывай пути.
- Анализируй результат каждого действия и адаптируй план, если что-то пошло не так.
- Предпочитай обратимые действия: перемещай в корзину (trash_path), а не удаляй; не перезаписывай
  чужие файлы без явной причины.
- Никогда не трогай личные данные, пароли, ключи, финансовые документы, если тебя явно не просили.
- Сохраняй через remember то, что поможет действовать лучше в будущем (привычки, договорённости).
- Сообщай пользователю (notify_user) только о действительно важном.
- Отвечай по-русски, кратко.

Окружение:
- ОС: {os}
- Чтение разрешено в: {read_roots}
- Запись разрешена в: {write_roots}
- Разрешённые программы: {commands}
"""


class Agent:
    def __init__(self, cfg: Config, tools: ToolBox, verbose: bool = True, extra_instructions: str = ""):
        self.cfg = cfg
        self.tools = tools
        self.verbose = verbose
        self.client = anthropic.Anthropic()  # ключ берётся из ANTHROPIC_API_KEY
        self.system = SYSTEM_PROMPT.format(
            os=f"{platform.system()} {platform.release()}",
            read_roots=", ".join(map(str, cfg.read_roots)) or "(нет)",
            write_roots=", ".join(map(str, cfg.write_roots)) or "(нет)",
            commands=", ".join(cfg.allowed_commands) or "(запуск команд запрещён)",
        ) + extra_instructions

    def _log(self, text: str) -> None:
        if self.verbose:
            print(text, flush=True)

    def run(self, messages: list[dict], max_tool_calls: int = 40) -> str:
        """Гоняет цикл инструментов, пока Claude не закончит. Дополняет messages на месте."""
        calls = 0
        while True:
            response = self.client.beta.messages.create(
                model=self.cfg.model,
                max_tokens=16000,
                system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                tools=TOOL_DEFINITIONS,
                messages=messages,
                thinking={"type": "adaptive"},
                output_config={"effort": self.cfg.effort},
                # если фильтр безопасности отклонит запрос, сервер сам повторит его на запасной модели
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            # Добавляем ответ ЦЕЛИКОМ (вместе с блоками размышлений) — так требует API.
            messages.append({"role": "assistant", "content": response.content})

            for block in response.content:
                if block.type == "text" and block.text.strip():
                    self._log(f"\n🤖 {block.text}")

            if response.stop_reason == "refusal":
                return "Запрос отклонён моделью."
            if response.stop_reason == "max_tokens":
                return "Ответ оборвался по лимиту токенов."
            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason != "tool_use":
                return "\n".join(b.text for b in response.content if b.type == "text")

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                calls += 1
                if calls > max_tool_calls:
                    result, is_error = "Достигнут лимит действий в этом цикле. Заверши работу и подведи итог.", True
                else:
                    self._log(f"  🔧 {block.name}({_short(block.input)})")
                    result, is_error = self.tools.execute(block.name, block.input)
                    if is_error:
                        self._log(f"     ⚠️  {result[:200]}")
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": result, "is_error": is_error})
            messages.append({"role": "user", "content": results})

    # ---------- автономный цикл ----------
    def autonomous_cycle(self, events: list[str], extra: str = "") -> str:
        """Один самостоятельный цикл анализа. Каждый цикл — новый диалог; опыт переносится через память."""
        auto = self.cfg.autonomous
        prompt = (
            f"Сейчас {dt.datetime.now():%Y-%m-%d %H:%M}. Ты работаешь в автономном режиме — пользователя рядом нет.\n\n"
            f"<goals>\n{auto.goals}\n</goals>\n\n"
            f"<memory>\n{self.tools.read_memory()}\n</memory>\n\n"
            f"<events>\n" + "\n".join(events) + "\n</events>\n\n"
            "Проанализируй, что произошло, и реши, нужно ли что-то делать ради целей. "
            "Если ничего делать не нужно — просто кратко ответь почему. "
            "В конце сохрани через remember короткий итог, если узнал что-то полезное."
            + extra
        )
        return self.run([{"role": "user", "content": prompt}], max_tool_calls=auto.max_tool_calls_per_cycle)


def _short(data: dict) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in data.items())
    return text if len(text) < 160 else text[:160] + "…"
