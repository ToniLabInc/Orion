"""Pictures the models look at: a screenshot, a camera frame, a browser page, an attachment.

Every picture is decoded, turned upright, sized down to ``eyes.image_max_side`` on its longest edge and sent as a
JPEG of ``eyes.image_quality``: what a model reads stays small (Claude, for one, scales down anything over 1568
pixels anyway, and refuses more than 20 large ones in a request), and a picture that can't be decoded never reaches
a model. Pillow loads on first use.
"""

import base64
import binascii
import io
from collections.abc import Sequence
from typing import Final

from orion.base import Image

ACCEPTED: Final = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"})  # decoded as given


def prepare(data: bytes, *, label: str, max_side: int, quality: int) -> Image:
    """A picture as the models get it: upright, at most ``max_side`` pixels on its longest edge, as JPEG.

    Args:
        data: the picture's bytes, in any format Pillow reads.
        label: what it shows, in a few words.
        max_side: the longest edge allowed, in pixels.
        quality: the JPEG quality (1-100).

    Returns:
        The picture, ready to send.

    Raises:
        ValueError: the bytes aren't a picture Pillow can read.
    """
    from PIL import Image as Pil
    from PIL import ImageOps, UnidentifiedImageError

    try:
        with Pil.open(io.BytesIO(data)) as opened:
            picture = ImageOps.exif_transpose(opened)
            picture.load()
    except (UnidentifiedImageError, OSError) as e:
        raise ValueError(f"not a picture that can be read ({e})") from e
    if picture.mode not in ("RGB", "L"):
        flat = Pil.new("RGB", picture.size, (255, 255, 255))  # transparency over white, as a page would show it
        rgba = picture.convert("RGBA")
        flat.paste(rgba, mask=rgba.getchannel("A"))
        picture = flat
    picture.thumbnail((max_side, max_side), Pil.Resampling.LANCZOS)
    out = io.BytesIO()
    picture.save(out, "JPEG", quality=quality, optimize=True)
    return Image(mime="image/jpeg", data=base64.b64encode(out.getvalue()).decode("ascii"), label=label)


def from_base64(data: str, *, label: str, max_side: int, quality: int) -> Image:
    """``prepare`` for a picture that arrives as base64 (an MCP image, a HUD frame), a "data:" prefix allowed.

    Args:
        data: the base64 text.
        label: what it shows.
        max_side: the longest edge allowed.
        quality: the JPEG quality.

    Returns:
        The picture, ready to send.

    Raises:
        ValueError: it isn't base64, or not a picture.
    """
    text = data.split(",", 1)[1] if data.startswith("data:") else data
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError("not base64") from e
    return prepare(raw, label=label, max_side=max_side, quality=quality)


def note(images: Sequence[Image]) -> str:
    """What the history keeps in place of pictures a turn had: "[a picture: the screen]".

    Args:
        images: the pictures.

    Returns:
        The note ("" for none).
    """
    if not images:
        return ""
    labels = ", ".join(image.get("label") or "unlabelled" for image in images)
    return f"[{'a picture' if len(images) == 1 else f'{len(images)} pictures'}: {labels}]"


def data_url(image: Image) -> str:
    """A picture as a data: URL (OpenAI-style providers take images this way)."""
    return f"data:{image['mime']};base64,{image['data']}"
