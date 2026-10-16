"""Telegram media helpers: download, convert, resize, and base64-encode.

Everything runs in memory / temp files that are always cleaned up — media
is processed and immediately discarded, never persisted to disk.

Functions:
- download_file: fetch a Telegram file's bytes via the bot.
- ogg_to_wav: convert Telegram voice notes (OGG/Opus) to WAV for Gemini STT.
- resize_image: downscale a photo to a max side and return JPEG bytes.
- encode_base64: base64-encode bytes for the OpenAI-compatible payloads.
"""

import base64
import io
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

# Gemini vision works well with short sides around 1024px; larger images
# cost more tokens without meaningfully better results.
MAX_IMAGE_SIDE = 1024

# WAV format Gemini's input_audio expects: 16 kHz mono PCM 16-bit.
AUDIO_SAMPLE_RATE = 16000
AUDIO_CHANNELS = 1


async def download_file(bot, file_id: str) -> bytes:
    """Download a Telegram file and return its raw bytes."""
    tg_file = await bot.get_file(file_id)
    file_bytes = await tg_file.download_as_bytearray()
    return bytes(file_bytes)


def ogg_to_wav(data: bytes) -> bytes:
    """Convert an OGG/Opus voice note to WAV (16 kHz mono) using ffmpeg.

    Args:
        data: Raw OGG/Opus bytes from a Telegram voice note.

    Returns:
        WAV bytes suitable for Gemini's input_audio payload.

    Raises:
        RuntimeError: When ffmpeg is unavailable or the conversion fails.
    """
    ffmpeg = _find_ffmpeg()
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "voice.ogg"
        target = Path(tmp) / "voice.wav"
        source.write_bytes(data)
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-ar",
                str(AUDIO_SAMPLE_RATE),
                "-ac",
                str(AUDIO_CHANNELS),
                "-f",
                "wav",
                str(target),
            ],
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0 or not target.exists():
            stderr = result.stderr.decode("utf-8", errors="replace")[:500]
            logger.warning("ffmpeg conversion failed: %s", stderr)
            raise RuntimeError("No pude convertir la nota de voz (ffmpeg).")
        return target.read_bytes()


def _find_ffmpeg() -> str:
    """Return the ffmpeg binary path or raise a clear error."""
    ffmpeg = os.environ.get("FFMPEG_BINARY", "ffmpeg")
    if not Path(ffmpeg).is_absolute() and not _which(ffmpeg):
        raise RuntimeError(
            "ffmpeg no está instalado. Añádelo al Dockerfile o instálalo "
            "localmente (apt install ffmpeg)."
        )
    return ffmpeg


def _which(binary: str) -> str | None:
    """Minimal shutil.which wrapper to keep imports light."""
    from shutil import which

    return which(binary)


def resize_image(data: bytes, max_side: int = MAX_IMAGE_SIDE) -> bytes:
    """Downscale an image to at most max_side on the longest edge.

    Args:
        data: Raw image bytes (any format Pillow can open).
        max_side: Maximum pixel length of the longest edge.

    Returns:
        Re-encoded JPEG bytes (RGB).
    """
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=85)
        return output.getvalue()


def encode_base64(data: bytes) -> str:
    """Return base64-encoded bytes as a str."""
    return base64.b64encode(data).decode("ascii")
