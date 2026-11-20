"""Расшифровка голосовых сообщений локальной моделью faster-whisper (без внешних API)."""

from __future__ import annotations

import asyncio
import logging
import subprocess

from app.config import Settings

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
MAX_SECONDS = 15 * 60


class TranscribeUnavailable(Exception):
    """Расшифровка недоступна или не удалась; текст — причина для автора."""


def _decode(data: bytes):
    """Любой аудиоформат Telegram → моно 16 кГц через ffmpeg."""
    import numpy as np

    try:
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", "pipe:0", "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"],
            input=data,
            capture_output=True,
            check=True,
            timeout=120,
        )  # fmt: skip
    except FileNotFoundError:
        raise TranscribeUnavailable("на сервере нет ffmpeg") from None
    except subprocess.CalledProcessError as e:
        raise TranscribeUnavailable(f"не удалось прочитать аудио: {e.stderr.decode(errors='replace')[:200]}") from None
    except subprocess.TimeoutExpired:
        raise TranscribeUnavailable("аудио декодируется слишком долго") from None
    return np.frombuffer(proc.stdout, dtype=np.float32)


def _transcribe_sync(data: bytes, model_name: str, model_dir: str) -> str:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise TranscribeUnavailable("пакет faster-whisper не установлен") from None
    audio = _decode(data)
    if len(audio) > MAX_SECONDS * SAMPLE_RATE:
        raise TranscribeUnavailable(f"запись длиннее {MAX_SECONDS // 60} минут")
    if len(audio) < SAMPLE_RATE // 2:
        raise TranscribeUnavailable("запись слишком короткая")
    # Модель загружается на время расшифровки и выгружается после: она занимает около 1 ГБ памяти.
    model = WhisperModel(model_name, device="cpu", compute_type="int8", download_root=model_dir)
    segments, _ = model.transcribe(audio, language="ru", vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


async def transcribe(data: bytes, settings: Settings) -> str:
    if not settings.whisper_model:
        raise TranscribeUnavailable("расшифровка голосовых выключена (WHISPER_MODEL пуст)")
    try:
        text = await asyncio.to_thread(_transcribe_sync, data, settings.whisper_model, settings.whisper_dir)
    except TranscribeUnavailable:
        raise
    except Exception as e:  # загрузка модели, нехватка памяти
        log.exception("расшифровка не удалась")
        raise TranscribeUnavailable(f"{type(e).__name__}: {e}") from e
    if not text:
        raise TranscribeUnavailable("в записи не распознана речь")
    return text
