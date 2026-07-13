"""Embed reproduction metadata (gz-compressed config + master seed) inside a
webp, and read it back.

WebP is a RIFF container: `'RIFF' <u32 size> 'WEBP' <chunk>...`, where each chunk
is `<FourCC> <u32 size LE> <payload (padded to even)>`. We append one custom
chunk and fix the top-level RIFF size; decoders skip unknown chunks, so the image
still renders everywhere while carrying everything needed to reproduce it.
"""

from __future__ import annotations

import gzip
import json
import struct
from typing import Any

from gen.models import GenerationConfig
from gen.workflow import BuildInfo

CHUNK_FOURCC = b"cMTA"  # custom "comfy metadata" chunk
# v1 = legacy WildcardBlock {input, wildcards: str}; v2 = tree {input, items:[...]}.
# extract() just feeds config to GenerationConfig.model_validate, whose mode=before
# validator upgrades v1 in place, so reads of either version Just Work.
META_VERSION = 2


def build_meta(cfg: GenerationConfig, master_seed: int, info: BuildInfo) -> dict[str, Any]:
    """Assemble the metadata dict embedded in a saved image."""
    return {
        "v": META_VERSION,
        "master_seed": master_seed,
        "config": cfg.model_dump(),
        "resolved": {
            "positive": info["positive"],
            "seed1": info["seed1"],
            "seed2": info["seed2"],
            "loras": info["loras"],
        },
    }


def embed_metadata(webp: bytes, meta: dict[str, Any]) -> bytes:
    """Append `meta` (gz-compressed JSON) as a custom RIFF chunk."""
    if webp[:4] != b"RIFF" or webp[8:12] != b"WEBP":
        raise ValueError("not a RIFF/WEBP byte stream")
    payload = gzip.compress(json.dumps(meta, ensure_ascii=False).encode("utf-8"))
    chunk = CHUNK_FOURCC + struct.pack("<I", len(payload)) + payload
    if len(payload) & 1:
        chunk += b"\x00"  # chunks are padded to an even size
    out = bytearray(webp)
    out.extend(chunk)
    struct.pack_into("<I", out, 4, len(out) - 8)  # RIFF size = filesize - 8
    return bytes(out)


def extract_raw(webp: bytes) -> dict[str, Any] | None:
    """Return the embedded metadata dict, or None if absent."""
    if webp[:4] != b"RIFF" or webp[8:12] != b"WEBP":
        return None
    end = min(len(webp), 8 + struct.unpack("<I", webp[4:8])[0])
    pos = 12
    while pos + 8 <= end:
        fourcc = webp[pos:pos + 4]
        size = struct.unpack("<I", webp[pos + 4:pos + 8])[0]
        if fourcc == CHUNK_FOURCC:
            body = webp[pos + 8:pos + 8 + size]
            return json.loads(gzip.decompress(body).decode("utf-8"))
        pos += 8 + size + (size & 1)
    return None


def extract(webp: bytes) -> tuple[GenerationConfig, int] | None:
    """Recover (config, master_seed) from an embedded webp, or None."""
    meta = extract_raw(webp)
    if meta is None:
        return None
    return GenerationConfig.model_validate(meta["config"]), int(meta["master_seed"])
