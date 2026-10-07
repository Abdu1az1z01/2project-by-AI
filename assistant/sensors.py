"""«Органы чувств» автономного режима: замечают изменения без обращения к помощнику."""

from __future__ import annotations

import os
from pathlib import Path

import psutil

from .config import Config


class Observer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._snapshot: dict[str, float] = self._scan()
        psutil.cpu_percent(interval=None)  # «прогрев» счётчика CPU

    def _scan(self) -> dict[str, float]:
        """Снимок: путь файла → время изменения. Обходим только верхние 2 уровня, чтобы было быстро."""
        snap: dict[str, float] = {}
        for root in self.cfg.autonomous.watch_dirs:
            if not root.exists():
                continue
            base_depth = len(root.parts)
            for dirpath, dirnames, filenames in os.walk(root):
                if len(Path(dirpath).parts) - base_depth >= 2:
                    dirnames[:] = []
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    if name.startswith(".") or name.endswith((".crdownload", ".part", ".tmp")):
                        continue  # незавершённые загрузки игнорируем
                    fp = os.path.join(dirpath, name)
                    try:
                        snap[fp] = os.path.getmtime(fp)
                    except OSError:
                        pass
        return snap

    def observe(self) -> list[str]:
        """Возвращает список значимых событий с прошлого вызова (пустой — ничего не произошло)."""
        events: list[str] = []

        new = self._scan()
        added = [p for p in new if p not in self._snapshot]
        removed = [p for p in self._snapshot if p not in new]
        modified = [p for p in new if p in self._snapshot and new[p] != self._snapshot[p]]
        self._snapshot = new
        for label, items in (("Новые файлы", added), ("Изменённые файлы", modified), ("Удалённые файлы", removed)):
            if items:
                shown = "\n  ".join(items[:30])
                more = f"\n  … и ещё {len(items) - 30}" if len(items) > 30 else ""
                events.append(f"{label} ({len(items)}):\n  {shown}{more}")

        th = self.cfg.autonomous.thresholds
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        if cpu >= th.get("cpu_percent", 101):
            events.append(f"Высокая загрузка CPU: {cpu}%")
        if mem >= th.get("memory_percent", 101):
            events.append(f"Высокая загрузка памяти: {mem}%")
        for part in psutil.disk_partitions(all=False):
            try:
                pct = psutil.disk_usage(part.mountpoint).percent
            except OSError:
                continue
            if pct >= th.get("disk_percent", 101):
                events.append(f"Диск {part.mountpoint} заполнен на {pct}%")
        return events
