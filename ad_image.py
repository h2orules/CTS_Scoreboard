"""Image processing for ad uploads.

`process_upload()` takes the raw bytes of an uploaded ad image and:
  * verifies the file decodes as an image,
  * downscales it with Lanczos so the longer edge is at most
    ``max_dimension`` pixels (preserving aspect ratio),
  * re-encodes JPEG/PNG/WebP in their original format, stripping EXIF and
    other metadata,
  * passes animated GIFs through unchanged so animation is preserved.

Returns ``(out_bytes, out_ext, info_dict)`` where ``info_dict`` describes
what happened. Raises ``AdImageError`` for unrecoverable input.
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps, UnidentifiedImageError


class AdImageError(Exception):
    """Raised when an uploaded ad image can't be processed."""


_FORMAT_FOR_EXT = {
    '.jpg': 'JPEG',
    '.jpeg': 'JPEG',
    '.png': 'PNG',
    '.webp': 'WEBP',
    '.gif': 'GIF',
}

_SAVE_KWARGS = {
    'JPEG': {'quality': 88, 'optimize': True, 'progressive': True},
    'PNG': {'optimize': True},
    'WEBP': {'quality': 88, 'method': 6},
}


def _is_animated(img: Image.Image) -> bool:
    return bool(getattr(img, 'is_animated', False))


def process_upload(data: bytes, ext: str, max_dimension: int) -> tuple[bytes, str, dict]:
    """Resize/re-encode an uploaded ad image.

    Parameters
    ----------
    data : raw upload bytes.
    ext  : lowercase extension including the leading dot (``.jpg`` etc.).
    max_dimension : longer-edge cap in pixels (must be > 0).

    Returns ``(bytes, ext, info)``. ``ext`` may differ from the input only
    when normalising ``.jpeg`` to ``.jpg``.

    ``info`` keys:
      * ``resized`` (bool)
      * ``original_size`` ((w, h))
      * ``new_size`` ((w, h))
      * ``original_bytes`` (int)
      * ``new_bytes`` (int)
      * ``animated`` (bool) — true if passed through unchanged for animation.
    """
    ext = ext.lower()
    if ext not in _FORMAT_FOR_EXT:
        raise AdImageError('unsupported extension: %s' % ext)
    if max_dimension <= 0:
        raise AdImageError('max_dimension must be positive')

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        raise AdImageError('could not decode image: %s' % e) from e

    orig_size = img.size
    orig_bytes = len(data)
    fmt = _FORMAT_FOR_EXT[ext]

    # Animated GIFs: pass through unchanged so we don't drop frames.
    if fmt == 'GIF' and _is_animated(img):
        return data, '.gif', {
            'resized': False,
            'original_size': orig_size,
            'new_size': orig_size,
            'original_bytes': orig_bytes,
            'new_bytes': orig_bytes,
            'animated': True,
        }

    # Apply EXIF orientation so the visible top-of-image matches the upload.
    img = ImageOps.exif_transpose(img)

    longest = max(img.size)
    resized = longest > max_dimension
    if resized:
        img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)

    out_ext = '.jpg' if ext == '.jpeg' else ext

    # Convert palette/alpha modes appropriately per output format.
    if fmt == 'JPEG':
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
    elif fmt == 'PNG':
        if img.mode not in ('RGB', 'RGBA', 'L', 'LA', 'P'):
            img = img.convert('RGBA')
    elif fmt == 'WEBP':
        if img.mode not in ('RGB', 'RGBA'):
            img = img.convert('RGBA' if 'A' in img.mode else 'RGB')
    elif fmt == 'GIF':
        # Static GIF: re-encode in palette mode.
        if img.mode != 'P':
            img = img.convert('P', palette=Image.Palette.ADAPTIVE)

    buf = io.BytesIO()
    save_kwargs = _SAVE_KWARGS.get(fmt, {})
    img.save(buf, format=fmt, **save_kwargs)
    out_bytes = buf.getvalue()

    # If re-encode somehow ballooned a non-resized JPEG/WebP, fall back to
    # the original bytes so we never make a file larger by "processing" it.
    if not resized and len(out_bytes) > orig_bytes and fmt in ('JPEG', 'WEBP'):
        return data, out_ext, {
            'resized': False,
            'original_size': orig_size,
            'new_size': orig_size,
            'original_bytes': orig_bytes,
            'new_bytes': orig_bytes,
            'animated': False,
        }

    return out_bytes, out_ext, {
        'resized': resized,
        'original_size': orig_size,
        'new_size': img.size,
        'original_bytes': orig_bytes,
        'new_bytes': len(out_bytes),
        'animated': False,
    }


def format_size(n: int) -> str:
    """Human-readable byte count: '524 KB', '1.3 MB'."""
    if n is None:
        return ''
    if n < 1024:
        return '%d B' % n
    if n < 1024 * 1024:
        return '%.0f KB' % (n / 1024)
    return '%.1f MB' % (n / (1024 * 1024))
