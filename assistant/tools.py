"""Инструменты, которые Claude может вызывать. Каждый проходит через Sandbox."""

from __future__ import annotations

import datetime as dt
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

import psutil

from .config import AccessDenied, Config, Sandbox

MAX_READ_CHARS = 40_000
MAX_LIST_ENTRIES = 300


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": False,
    }


S = {"type": "string"}
I = {"type": "integer"}

# Описания инструментов для API. strict=True гарантирует, что аргументы соответствуют схеме.
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_directory",
        "description": "Показать содержимое папки: имена, тип, размер, дату изменения.",
        "input_schema": _obj({"path": S}),
    },
    {
        "name": "read_file",
        "description": "Прочитать текстовый файл (до 40 000 символов).",
        "input_schema": _obj({"path": S}),
    },
    {
        "name": "search_files",
        "description": "Рекурсивный поиск файлов по glob-шаблону имени (например '*.pdf'). "
        "Если text не пустой — оставить только текстовые файлы, содержащие эту строку.",
        "input_schema": _obj({"root": S, "pattern": S, "text": S}),
    },
    {
        "name": "largest_files",
        "description": "Найти самые большие файлы внутри папки.",
        "input_schema": _obj({"root": S, "limit": I}),
    },
    {
        "name": "write_file",
        "description": "Записать текстовый файл. mode: 'overwrite' или 'append'.",
        "input_schema": _obj({"path": S, "content": S, "mode": {"type": "string", "enum": ["overwrite", "append"]}}),
    },
    {
        "name": "make_directory",
        "description": "Создать папку (вместе с родительскими).",
        "input_schema": _obj({"path": S}),
    },
    {
        "name": "move_path",
        "description": "Переместить или переименовать файл/папку. Не перезаписывает существующие файлы.",
        "input_schema": _obj({"source": S, "destination": S}),
    },
    {
        "name": "trash_path",
        "description": "Безопасно «удалить» файл/папку: переместить в корзину помощника (можно восстановить).",
        "input_schema": _obj({"path": S}),
    },
    {
        "name": "run_command",
        "description": "Запустить разрешённую программу без оболочки (без |, >, &&). "
        "cwd — рабочая папка (должна быть доступна для чтения).",
        "input_schema": _obj({"command": S, "cwd": S}),
    },
    {
        "name": "system_status",
        "description": "Текущая загрузка CPU, памяти, дисков, батареи, время работы.",
        "input_schema": _obj({}),
    },
    {
        "name": "list_processes",
        "description": "Список процессов, отсортированный по 'cpu' или 'memory'.",
        "input_schema": _obj({"sort_by": {"type": "string", "enum": ["cpu", "memory"]}, "limit": I}),
    },
    {
        "name": "remember",
        "description": "Сохранить заметку в долговременную память помощника "
        "(наблюдения, привычки пользователя, выполненные дела). Память видна в каждом следующем цикле.",
        "input_schema": _obj({"note": S}),
    },
    {
        "name": "notify_user",
        "description": "Отправить пользователю уведомление (важное наблюдение, вопрос, отчёт).",
        "input_schema": _obj({"message": S, "level": {"type": "string", "enum": ["info", "warning", "urgent"]}}),
    },
]
for _t in TOOL_DEFINITIONS:
    _t["strict"] = True

# Инструменты, меняющие что-либо на компьютере (требуют подтверждения в режиме chat).
MUTATING = {"write_file", "make_directory", "move_path", "trash_path", "run_command"}


class ToolBox:
    def __init__(self, cfg: Config, confirm: Callable[[str, dict], bool] | None = None,
                 notify: Callable[[str, str], None] | None = None):
        self.cfg = cfg
        self.sb = Sandbox(cfg)
        self.confirm = confirm
        self.notify = notify or (lambda msg, level: print(f"\n🔔 [{level}] {msg}\n"))
        self.memory_file = cfg.data_dir / "memory.md"
        self.audit_file = cfg.data_dir / "audit.jsonl"
        self.trash_dir = cfg.data_dir / "trash"

    # ---------- общий вход ----------
    def execute(self, name: str, args: dict) -> tuple[str, bool]:
        """Возвращает (результат, is_error). Все действия пишутся в журнал audit.jsonl."""
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return f"Неизвестный инструмент: {name}", True
        if name in MUTATING and self.confirm and not self.confirm(name, args):
            result, is_error = "Пользователь отклонил это действие.", True
        else:
            try:
                result, is_error = handler(**args), False
            except AccessDenied as e:
                result, is_error = f"Доступ запрещён: {e}", True
            except Exception as e:  # ошибку возвращаем модели, чтобы она адаптировалась
                result, is_error = f"Ошибка: {type(e).__name__}: {e}", True
        self._audit(name, args, result, is_error)
        return result, is_error

    def _audit(self, name: str, args: dict, result: str, is_error: bool) -> None:
        entry = {
            "time": dt.datetime.now().isoformat(timespec="seconds"),
            "tool": name,
            "args": {k: (v[:200] + "…" if isinstance(v, str) and len(v) > 200 else v) for k, v in args.items()},
            "error": is_error,
            "result": result[:300],
        }
        with self.audit_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read_memory(self) -> str:
        return self.memory_file.read_text(encoding="utf-8") if self.memory_file.exists() else "(пока пусто)"

    # ---------- файловые инструменты ----------
    def _t_list_directory(self, path: str) -> str:
        p = self.sb.readable(path)
        rows = []
        for i, entry in enumerate(sorted(p.iterdir(), key=lambda e: e.name.lower())):
            if i >= MAX_LIST_ENTRIES:
                rows.append(f"… и ещё записи (показаны первые {MAX_LIST_ENTRIES})")
                break
            try:
                st = entry.stat()
                kind = "DIR " if entry.is_dir() else "FILE"
                mtime = dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
                rows.append(f"{kind} {st.st_size:>12} {mtime} {entry.name}")
            except OSError:
                rows.append(f"???  {entry.name}")
        return f"{p}\n" + ("\n".join(rows) or "(пусто)")

    def _t_read_file(self, path: str) -> str:
        p = self.sb.readable(path)
        text = p.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_READ_CHARS:
            return text[:MAX_READ_CHARS] + f"\n… [обрезано, всего {len(text)} символов]"
        return text

    def _walk(self, root: Path):
        for dirpath, dirnames, filenames in os.walk(root):
            # не заходим в запрещённые папки
            dirnames[:] = [d for d in dirnames if self._allowed_read(Path(dirpath) / d)]
            for name in filenames:
                fp = Path(dirpath) / name
                if self._allowed_read(fp):
                    yield fp

    def _allowed_read(self, p: Path) -> bool:
        try:
            self.sb.readable(str(p))
            return True
        except AccessDenied:
            return False

    def _t_search_files(self, root: str, pattern: str, text: str) -> str:
        base = self.sb.readable(root)
        hits = []
        for fp in self._walk(base):
            if not fp.match(pattern or "*"):
                continue
            if text:
                try:
                    if fp.stat().st_size > 5_000_000 or text not in fp.read_text(encoding="utf-8", errors="ignore"):
                        continue
                except OSError:
                    continue
            hits.append(str(fp))
            if len(hits) >= 200:
                hits.append("… (показаны первые 200)")
                break
        return "\n".join(hits) or "Ничего не найдено."

    def _t_largest_files(self, root: str, limit: int) -> str:
        base = self.sb.readable(root)
        sizes = []
        for fp in self._walk(base):
            try:
                sizes.append((fp.stat().st_size, fp))
            except OSError:
                pass
        sizes.sort(reverse=True)
        return "\n".join(f"{s / 1_048_576:10.1f} MB  {p}" for s, p in sizes[: max(1, min(limit, 100))]) or "Файлов нет."

    def _t_write_file(self, path: str, content: str, mode: str) -> str:
        p = self.sb.writable(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if mode == "overwrite" and p.exists():
            self._backup(p)
        with p.open("a" if mode == "append" else "w", encoding="utf-8") as f:
            f.write(content)
        return f"Записано {len(content)} символов в {p}"

    def _t_make_directory(self, path: str) -> str:
        p = self.sb.writable(path)
        p.mkdir(parents=True, exist_ok=True)
        return f"Папка создана: {p}"

    def _t_move_path(self, source: str, destination: str) -> str:
        src, dst = self.sb.writable(source), self.sb.writable(destination)
        if dst.is_dir():
            dst = dst / src.name
        if dst.exists():
            raise FileExistsError(f"{dst} уже существует — выберите другое имя")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"Перемещено: {src} → {dst}"

    def _t_trash_path(self, path: str) -> str:
        p = self.sb.writable(path)
        dest = self._backup(p, move=True)
        return f"Перемещено в корзину помощника: {dest}"

    def _backup(self, p: Path, move: bool = False) -> Path:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = self.trash_dir / stamp / p.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        (shutil.move if move else shutil.copy2)(str(p), str(dest))
        return dest

    # ---------- команды ----------
    def _t_run_command(self, command: str, cwd: str) -> str:
        if not self.cfg.allowed_commands:
            raise AccessDenied("Запуск команд отключён в конфигурации")
        if any(ch in command for ch in "|&;><`$"):
            raise AccessDenied("Операторы оболочки (|, &, ;, >, <, `, $) запрещены")
        argv = shlex.split(command, posix=os.name != "nt")
        program = Path(argv[0]).stem.lower() if argv else ""
        if program not in [c.lower() for c in self.cfg.allowed_commands]:
            raise AccessDenied(f"Программа '{program}' не в списке разрешённых: {self.cfg.allowed_commands}")
        workdir = self.sb.readable(cwd) if cwd else self.cfg.read_roots[0]
        for arg in argv[1:]:  # пути в аргументах тоже должны быть в песочнице
            if os.sep in arg or "/" in arg or arg.startswith("~") or arg.startswith(".."):
                self.sb.readable(str(workdir / os.path.expanduser(arg)))
        proc = subprocess.run(argv, cwd=workdir, capture_output=True, text=True, timeout=120, shell=False)
        out = (proc.stdout + ("\n[stderr]\n" + proc.stderr if proc.stderr else ""))[-MAX_READ_CHARS:]
        return f"exit code {proc.returncode}\n{out}"

    # ---------- система ----------
    def _t_system_status(self) -> str:
        info: dict[str, Any] = {
            "time": dt.datetime.now().isoformat(timespec="seconds"),
            "cpu_percent": psutil.cpu_percent(interval=0.5),
            "memory_percent": psutil.virtual_memory().percent,
            "uptime_hours": round((dt.datetime.now().timestamp() - psutil.boot_time()) / 3600, 1),
            "disks": {},
        }
        for part in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(part.mountpoint)
                info["disks"][part.mountpoint] = {"percent": u.percent, "free_gb": round(u.free / 1e9, 1)}
            except OSError:
                pass
        battery = getattr(psutil, "sensors_battery", lambda: None)()
        if battery:
            info["battery"] = {"percent": battery.percent, "plugged": battery.power_plugged}
        return json.dumps(info, ensure_ascii=False, indent=1)

    def _t_list_processes(self, sort_by: str, limit: int) -> str:
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            procs.append(p.info)
        key = "cpu_percent" if sort_by == "cpu" else "memory_percent"
        procs.sort(key=lambda x: x.get(key) or 0, reverse=True)
        return "\n".join(
            f"{p['pid']:>7} cpu={p.get('cpu_percent') or 0:5.1f}% mem={p.get('memory_percent') or 0:5.1f}% {p['name']}"
            for p in procs[: max(1, min(limit, 50))]
        )

    # ---------- память и уведомления ----------
    def _t_remember(self, note: str) -> str:
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        with self.memory_file.open("a", encoding="utf-8") as f:
            f.write(f"- [{stamp}] {note}\n")
        # держим память компактной: только последние 200 заметок
        lines = self.memory_file.read_text(encoding="utf-8").splitlines()
        if len(lines) > 200:
            self.memory_file.write_text("\n".join(lines[-200:]) + "\n", encoding="utf-8")
        return "Сохранено в память."

    def _t_notify_user(self, message: str, level: str) -> str:
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        with (self.cfg.data_dir / "notifications.log").open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] [{level}] {message}\n")
        self.notify(message, level)
        return "Уведомление отправлено."
