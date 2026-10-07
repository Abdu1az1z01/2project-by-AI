"""Загрузка конфигурации и проверка доступа к путям (песочница)."""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p))).resolve()


@dataclass
class AutonomousConfig:
    interval_seconds: int = 120
    max_tool_calls_per_cycle: int = 25
    max_cycles_per_hour: int = 10
    watch_dirs: list[Path] = field(default_factory=list)
    thresholds: dict[str, float] = field(default_factory=dict)
    goals: str = ""


@dataclass
class Config:
    model: str
    effort: str
    read_roots: list[Path]
    write_roots: list[Path]
    deny_patterns: list[str]
    allowed_commands: list[str]
    confirm_in_chat: bool
    autonomous: AutonomousConfig
    data_dir: Path
    voice: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        auto = raw.get("autonomous") or {}
        cfg = cls(
            model=raw.get("model", "claude-opus-5-5"),
            effort=raw.get("effort", "medium"),
            read_roots=[_expand(p) for p in raw.get("read_roots", [])],
            write_roots=[_expand(p) for p in raw.get("write_roots", [])],
            deny_patterns=list(raw.get("deny_patterns", [])),
            allowed_commands=list(raw.get("allowed_commands", [])),
            confirm_in_chat=bool(raw.get("confirm_in_chat", True)),
            autonomous=AutonomousConfig(
                interval_seconds=int(auto.get("interval_seconds", 120)),
                max_tool_calls_per_cycle=int(auto.get("max_tool_calls_per_cycle", 25)),
                max_cycles_per_hour=int(auto.get("max_cycles_per_hour", 10)),
                watch_dirs=[_expand(p) for p in auto.get("watch_dirs", [])],
                thresholds=dict(auto.get("thresholds", {})),
                goals=str(auto.get("goals", "")),
            ),
            data_dir=_expand(raw.get("data_dir", "~/.ai-assistant")),
            voice=dict(raw.get("voice") or {}),
        )
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        for root in cfg.write_roots:
            if not any(_is_within(root, r) for r in cfg.read_roots):
                raise ValueError(f"write_root {root} должен находиться внутри одного из read_roots")
        return cfg


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class AccessDenied(Exception):
    pass


class Sandbox:
    """Единая точка проверки: можно ли читать/писать данный путь."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _check(self, raw: str, roots: list[Path], kind: str) -> Path:
        # resolve() раскрывает «..» и символические ссылки, поэтому выйти за пределы корня нельзя.
        path = _expand(raw)
        if _is_within(path, self.cfg.data_dir):
            raise AccessDenied(f"Служебная папка помощника недоступна: {path}")
        if not any(_is_within(path, r) for r in roots):
            allowed = ", ".join(str(r) for r in roots) or "(нет)"
            raise AccessDenied(f"Нет доступа на {kind} к {path}. Разрешено: {allowed}")
        normalized = str(path).replace("\\", "/").lower()
        for pattern in self.cfg.deny_patterns:
            if fnmatch.fnmatch(normalized, pattern.lower()):
                raise AccessDenied(f"Путь {path} запрещён правилом '{pattern}'")
        return path

    def readable(self, raw: str) -> Path:
        return self._check(raw, self.cfg.read_roots, "чтение")

    def writable(self, raw: str) -> Path:
        return self._check(raw, self.cfg.write_roots, "запись")
