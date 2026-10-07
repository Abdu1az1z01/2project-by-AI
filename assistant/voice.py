"""Голос: слушаем микрофон (распознавание речи) и отвечаем вслух (синтез речи)."""

from __future__ import annotations

import collections
import re
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16_000
BLOCK = 1_600  # 0.1 секунды


@dataclass
class VoiceConfig:
    language: str = "ru-RU"
    wake_word: str = ""        # если задано — реагировать только на фразы с этим словом
    speech_rate: int = 185     # скорость речи (слов в минуту)
    silence_seconds: float = 0.9   # пауза, после которой фраза считается законченной
    max_phrase_seconds: float = 30
    advise: bool = True        # самому подсказывать, когда что-то происходит на компьютере

    @classmethod
    def from_dict(cls, raw: dict | None) -> "VoiceConfig":
        raw = raw or {}
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))


def clean_for_speech(text: str) -> str:
    """Убираем разметку, которую синтезатор прочитал бы как символы."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"[*_#`>|]", "", text)
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


class Voice:
    def __init__(self, cfg: VoiceConfig):
        # Импорты здесь, чтобы режимы chat/auto работали и без голосовых библиотек.
        import pyttsx3
        import sounddevice as sd
        import speech_recognition as sr

        self.cfg = cfg
        self.sd = sd
        self.sr = sr
        self.recognizer = sr.Recognizer()
        self.tts = pyttsx3.init()
        self.tts.setProperty("rate", cfg.speech_rate)
        self._pick_voice()
        self.threshold = self._calibrate()

    def _pick_voice(self) -> None:
        lang = self.cfg.language.split("-")[0].lower()  # "ru"
        names = {"ru": ("russian", "irina", "pavel", "русск")}.get(lang, (lang,))
        for v in self.tts.getProperty("voices"):
            meta = (v.name + " " + v.id + " " + " ".join(map(str, getattr(v, "languages", [])))).lower()
            if any(n in meta for n in names) or f"{lang}-" in meta or f"{lang}_" in meta:
                self.tts.setProperty("voice", v.id)
                return
        print(f"⚠️  Голос для языка '{lang}' не найден — будет использован голос по умолчанию. "
              "Установите речевой пакет в Параметры → Время и язык → Речь.")

    def _calibrate(self) -> float:
        """1 секунду слушаем тишину, чтобы понять уровень фонового шума."""
        print("🎙️  Калибровка микрофона — помолчите секунду…")
        audio = self.sd.rec(SAMPLE_RATE, samplerate=SAMPLE_RATE, channels=1, dtype="int16")
        self.sd.wait()
        noise = _rms(audio)
        return max(noise * 3.0, 300.0)

    # ---------- речь ----------
    def speak(self, text: str) -> None:
        text = clean_for_speech(text)
        if not text:
            return
        print(f"🔊 {text}")
        self.tts.say(text)
        self.tts.runAndWait()

    # ---------- слух ----------
    def record_phrase(self, wait_seconds: float) -> bytes | None:
        """Ждёт начала речи до wait_seconds; записывает фразу до паузы. None — никто не говорил."""
        pre_roll: collections.deque[np.ndarray] = collections.deque(maxlen=3)
        frames: list[np.ndarray] = []
        silent_blocks = 0
        waited = 0
        silence_limit = int(self.cfg.silence_seconds * SAMPLE_RATE / BLOCK)
        max_blocks = int(self.cfg.max_phrase_seconds * SAMPLE_RATE / BLOCK)
        with self.sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=BLOCK) as stream:
            while True:
                block, _ = stream.read(BLOCK)
                loud = _rms(block) > self.threshold
                if not frames:
                    if loud:
                        frames = [*pre_roll, block]
                    else:
                        pre_roll.append(block)
                        waited += 1
                        if waited * BLOCK / SAMPLE_RATE >= wait_seconds:
                            return None
                    continue
                frames.append(block)
                silent_blocks = 0 if loud else silent_blocks + 1
                if silent_blocks >= silence_limit or len(frames) >= max_blocks:
                    break
        if len(frames) - silent_blocks < 3:  # короче ~0.3 с — щелчок или шум
            return None
        return np.concatenate(frames).tobytes()

    def transcribe(self, pcm: bytes) -> str | None:
        audio = self.sr.AudioData(pcm, SAMPLE_RATE, 2)
        try:
            return self.recognizer.recognize_google(audio, language=self.cfg.language)
        except self.sr.UnknownValueError:
            return None
        except self.sr.RequestError as e:
            print(f"⚠️  Сервис распознавания недоступен: {e}")
            return None

    def listen(self, wait_seconds: float = 2.0) -> str | None:
        pcm = self.record_phrase(wait_seconds)
        if pcm is None:
            return None
        text = self.transcribe(pcm)
        if text:
            print(f"👂 {text}")
        return text

    def confirm(self, name: str, args: dict) -> bool:
        """Голосовое подтверждение действия: «да» / «нет»."""
        target = next((str(v) for k, v in args.items() if k in ("path", "source", "command")), "")
        human = {
            "write_file": "записать файл", "make_directory": "создать папку", "move_path": "переместить",
            "trash_path": "удалить в корзину", "run_command": "запустить команду",
        }.get(name, name)
        print(f"❓ {name}: {args}")
        self.speak(f"Можно {human} {target.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]}? Скажите да или нет.")
        for _ in range(2):
            answer = (self.listen(wait_seconds=6) or "").lower()
            if re.search(r"\b(да|давай|можно|разрешаю|ок|окей|yes)\b", answer):
                return True
            if re.search(r"\b(нет|не надо|стоп|отмена|no)\b", answer):
                return False
            self.speak("Не расслышал. Да или нет?")
        return False
