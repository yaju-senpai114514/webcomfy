"""Reproduce a generation from a webp's embedded (config + master seed).

    uv run python repro.py <image.webp> [out.webp]

Reads the gz-compressed config + master seed from the RIFF chunk, rebuilds the
exact pipeline, regenerates via ComfyUI, and writes the reproduced image
(re-embedding the same metadata).
"""

from __future__ import annotations

import sys
from pathlib import Path

import comfy
import embed
import pipeline
import servers
import storage
from models import GenerationConfig
from workflow import BuildInfo


def reproduce(webp_bytes: bytes) -> tuple[bytes, GenerationConfig, int, BuildInfo]:
    """Regenerate the final image from a webp's embedded metadata.

    Returns (final_png, config, master_seed, info). Raises ValueError if absent.
    """
    result = embed.extract(webp_bytes)
    if result is None:
        raise ValueError("no embedded reproduction metadata in this webp")
    cfg, master_seed = result
    workflow, labels, info = pipeline.prepare(cfg, master_seed)

    images: dict[str, bytes] = {}

    def sink(ev: comfy.Event) -> None:
        if ev["type"] == "image":
            images[ev["label"]] = ev["data"]

    # CLI reproduction targets the default server (registry, else COMFY_BASE_URL).
    servers.ensure_seeded()
    entry = servers.default_server()
    client = comfy.ComfyClient(entry.base_url if entry else comfy.COMFY_BASE_URL)
    client.run(workflow, labels, sink)
    final = images.get("final")
    if final is None:
        raise RuntimeError("ComfyUI returned no final image")
    return final, cfg, master_seed, info


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    src = Path(argv[0])
    final_png, cfg, master_seed, info = reproduce(src.read_bytes())

    print(f"master_seed = {master_seed}")
    print(f"seed1={info['seed1']}  seed2={info['seed2']}")
    print(f"loras = {info['loras']}")
    print(f"positive = {info['positive'][:100]}...")

    out = Path(argv[1]) if len(argv) > 1 else src.with_name(src.stem + "_repro.webp")
    out.parent.mkdir(parents=True, exist_ok=True)
    webp = storage.encode_webp(final_png, cfg.save.webp_quality, cfg.save.webp_lossless)
    out.write_bytes(embed.embed_metadata(webp, embed.build_meta(cfg, master_seed, info)))
    print(f"reproduced → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
