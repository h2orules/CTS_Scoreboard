"""Tests for ad_image.process_upload."""

import io
import struct

import pytest
from PIL import Image

import ad_image


def _png_bytes(w, h, mode='RGB'):
    img = Image.new(mode, (w, h), color=(200, 100, 50) if mode == 'RGB' else (200, 100, 50, 255))
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


def _jpeg_bytes(w, h):
    img = Image.new('RGB', (w, h), color=(123, 45, 67))
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=95)
    return buf.getvalue()


def _webp_bytes(w, h):
    img = Image.new('RGB', (w, h), color=(10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format='WEBP', quality=95)
    return buf.getvalue()


def _animated_gif_bytes(w, h, frames=3):
    imgs = [Image.new('P', (w, h), color=i) for i in range(frames)]
    buf = io.BytesIO()
    imgs[0].save(buf, format='GIF', save_all=True, append_images=imgs[1:], duration=100, loop=0)
    return buf.getvalue()


def _static_gif_bytes(w, h):
    img = Image.new('P', (w, h), color=5)
    buf = io.BytesIO()
    img.save(buf, format='GIF')
    return buf.getvalue()


def _open_size(b):
    return Image.open(io.BytesIO(b)).size


class TestResize:
    def test_resizes_large_jpeg(self):
        data = _jpeg_bytes(4000, 3000)
        out, ext, info = ad_image.process_upload(data, '.jpg', 1920)
        assert ext == '.jpg'
        assert info['resized'] is True
        assert info['original_size'] == (4000, 3000)
        assert max(info['new_size']) == 1920
        # Aspect preserved.
        assert info['new_size'] == (1920, 1440)
        assert _open_size(out) == (1920, 1440)

    def test_no_resize_when_smaller(self):
        data = _png_bytes(800, 600)
        out, ext, info = ad_image.process_upload(data, '.png', 1920)
        assert info['resized'] is False
        assert info['new_size'] == (800, 600)
        assert _open_size(out) == (800, 600)

    def test_jpeg_extension_normalized(self):
        data = _jpeg_bytes(100, 100)
        _, ext, _ = ad_image.process_upload(data, '.jpeg', 1920)
        assert ext == '.jpg'

    def test_portrait_aspect(self):
        data = _jpeg_bytes(1000, 4000)
        _, _, info = ad_image.process_upload(data, '.jpg', 1920)
        assert info['new_size'] == (480, 1920)


class TestFormats:
    def test_png_with_alpha_round_trips(self):
        data = _png_bytes(3000, 2000, mode='RGBA')
        out, ext, info = ad_image.process_upload(data, '.png', 1920)
        assert ext == '.png'
        assert info['resized'] is True
        img = Image.open(io.BytesIO(out))
        assert img.mode in ('RGBA', 'P')

    def test_webp_round_trips(self):
        data = _webp_bytes(2400, 1800)
        out, ext, info = ad_image.process_upload(data, '.webp', 1920)
        assert ext == '.webp'
        assert info['resized'] is True
        assert _open_size(out) == (1920, 1440)

    def test_animated_gif_passthrough(self):
        data = _animated_gif_bytes(800, 600, frames=4)
        out, ext, info = ad_image.process_upload(data, '.gif', 1920)
        assert ext == '.gif'
        assert info['animated'] is True
        assert info['resized'] is False
        assert out == data  # untouched

    def test_static_gif_processed(self):
        data = _static_gif_bytes(3000, 2000)
        out, ext, info = ad_image.process_upload(data, '.gif', 1920)
        assert ext == '.gif'
        assert info['resized'] is True
        assert info['animated'] is False
        assert max(_open_size(out)) == 1920


class TestErrors:
    def test_corrupt_bytes_raises(self):
        with pytest.raises(ad_image.AdImageError):
            ad_image.process_upload(b'not an image at all', '.jpg', 1920)

    def test_unknown_ext_raises(self):
        with pytest.raises(ad_image.AdImageError):
            ad_image.process_upload(_jpeg_bytes(10, 10), '.bmp', 1920)

    def test_zero_max_dim_raises(self):
        with pytest.raises(ad_image.AdImageError):
            ad_image.process_upload(_jpeg_bytes(10, 10), '.jpg', 0)


class TestFormatSize:
    def test_bytes(self):
        assert ad_image.format_size(500) == '500 B'

    def test_kb(self):
        assert ad_image.format_size(2048) == '2 KB'

    def test_mb(self):
        assert ad_image.format_size(int(1.5 * 1024 * 1024)) == '1.5 MB'

    def test_none(self):
        assert ad_image.format_size(None) == ''
