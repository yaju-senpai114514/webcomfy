"""Persist generated images to disk as webp, using a path template."""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
from string import Formatter
from typing import Any

from PIL import Image

from store import embed
from gen.models import SaveConfig

# Placeholders the path template may use.
TEMPLATE_KEYS = (
    "date", "time", "datetime", "seed", "index", "ext", "cname",
    "lora_triggers",
)


def encode_webp(png_bytes: bytes, quality: int, lossless: bool) -> bytes:
    """Re-encode a (PNG) image byte stream as webp."""
    with Image.open(io.BytesIO(png_bytes)) as img:
        out = io.BytesIO()
        img.save(out, format="WEBP", quality=quality, lossless=lossless)
        return out.getvalue()


def render_path(
    cfg: SaveConfig,
    seed: int,
    index: int,
    now: datetime,
    lora_triggers: list[str] | None = None,
) -> Path:
    """Resolve the path template into a concrete file path under cfg.dir."""
    values: dict[str, object] = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H%M%S"),
        "datetime": now.strftime("%Y-%m-%d_%H%M%S"),
        "seed": seed,
        "index": index,
        "ext": "webp",
        "cname": cfg.cname or "config",
        "lora_triggers": "_".join(lora_triggers or []),
    }
    # Validate placeholders up front so a typo fails loudly rather than crashing.
    for _, field, _, _ in Formatter().parse(cfg.path_template):
        if field is not None and field not in values:
            raise ValueError(
                f"unknown path placeholder {{{field}}}; allowed: {', '.join(TEMPLATE_KEYS)}"
            )
    rel = cfg.path_template.format(**values)
    return (Path(cfg.dir) / rel).resolve()


def save_image(
    png_bytes: bytes,
    cfg: SaveConfig,
    seed: int,
    index: int,
    meta: dict[str, Any] | None = None,
    lora_triggers: list[str] | None = None,
) -> Path:
    """Encode `png_bytes` to webp (optionally embedding `meta`) and write it."""
    path = render_path(cfg, seed, index, datetime.now(), lora_triggers)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = encode_webp(png_bytes, cfg.webp_quality, cfg.webp_lossless)
    if meta is not None:
        data = embed.embed_metadata(data, meta)
    path.write_bytes(data)
    return path
