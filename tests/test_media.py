"""Unit tests for bot.media — audio/image processing helpers."""

import io

import pytest
from PIL import Image

from bot import media


class TestResizeImage:
    def _make_image(self, size=(2000, 1000)) -> bytes:
        img = Image.new("RGB", size, color="red")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_downsizes_large_image(self):
        original = self._make_image((2000, 1000))
        resized = media.resize_image(original)
        with Image.open(io.BytesIO(resized)) as img:
            assert max(img.size) <= media.MAX_IMAGE_SIDE
            assert img.format == "JPEG"

    def test_keeps_small_image_size(self):
        original = self._make_image((100, 50))
        resized = media.resize_image(original)
        with Image.open(io.BytesIO(resized)) as img:
            assert img.size == (100, 50)

    def test_custom_max_side(self):
        original = self._make_image((2000, 1000))
        resized = media.resize_image(original, max_side=500)
        with Image.open(io.BytesIO(resized)) as img:
            assert max(img.size) <= 500


class TestEncodeBase64:
    def test_encodes_bytes(self):
        assert media.encode_base64(b"hello") == "aGVsbG8="


class TestFindFfmpeg:
    def test_raises_when_missing(self, monkeypatch):
        monkeypatch.setenv("FFMPEG_BINARY", "definitely-not-a-real-binary-xyz")
        monkeypatch.setattr(media, "_which", lambda b: None)
        with pytest.raises(RuntimeError, match="ffmpeg"):
            media._find_ffmpeg()

    def test_uses_env_binary(self, monkeypatch):
        monkeypatch.setenv("FFMPEG_BINARY", "/usr/bin/ffmpeg")
        assert media._find_ffmpeg() == "/usr/bin/ffmpeg"
