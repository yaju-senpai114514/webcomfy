"""Build a ComfyUI /prompt graph from a validated GenerationConfig.

This replaces the static `api_prompt_template.json` (and its PPWCLoraDetector
trigger tree): since we assemble the graph ourselves, every LoRA strength is
written directly and the detector nodes are unnecessary.

Pipeline reproduced from the template:

    EmptyLatentImage -> KSampler (stage 1) -> VAEDecode -> [base image]
        -> UpscaleWithModel(4x) -> ImageScaleBy -> VAEEncode
        -> KSamplerAdvanced (stage 2, hires fix) -> VAEDecode -> [final image]

Model chain: UNET -> N x LoraLoaderModelOnly -> ModelSamplingAuraFlow
             -> DCWModelPatch -> (both samplers).
"""

from __future__ import annotations

import random
from typing import TypedDict

from gen.models import GenerationConfig, LoraConfig

# A ComfyUI link is [node_id, output_index]; a node is class_type + inputs.
Link = list[str | int]
Node = dict[str, object]
Graph = dict[str, Node]

# Label for each SaveImageWebsocket node, used by the client to tag frames.
OUTPUT_LABELS: dict[str, str] = {
    "save_base": "intermediate",
    "save_final": "final",
}

MAX_SEED = 2**63 - 1


class BuildInfo(TypedDict):
    """Concrete values the builder resolved, for saving/templating/reporting."""

    seed1: int
    seed2: int
    positive: str
    loras: list[str]


def _seed(value: int, rng: random.Random) -> int:
    """Resolve a seed; -1 means "draw from the single pipeline RNG"."""
    return rng.randint(0, MAX_SEED) if value < 0 else value


def build_workflow(
    cfg: GenerationConfig,
    positive: str,
    loras: list[LoraConfig],
    rng: random.Random,
) -> tuple[Graph, dict[str, str], BuildInfo]:
    """Return (graph, output_labels, info).

    `positive` is the already-resolved positive prompt and `loras` the LoRAs it
    triggered (see prompt.resolve); the builder writes them in verbatim. `rng` is
    the same RNG that drove wildcard selection, so any `-1` (random) sampler seed
    is drawn from it — making the whole pipeline reproducible from one master seed.
    """
    m, size, s1, s2, ups, adv = (
        cfg.models, cfg.size, cfg.stage1, cfg.stage2, cfg.upscale, cfg.advanced,
    )
    seed1 = _seed(s1.seed, rng)
    seed2 = _seed(s2.noise_seed, rng)
    g: Graph = {}

    # --- loaders -----------------------------------------------------------
    g["unet"] = {
        "class_type": "UNETLoader",
        "inputs": {"unet_name": m.unet_name, "weight_dtype": m.weight_dtype},
    }
    g["clip"] = {
        "class_type": "CLIPLoader",
        "inputs": {"clip_name": m.clip_name, "type": m.clip_type, "device": "default"},
    }
    g["vae"] = {
        "class_type": "VAELoader",
        "inputs": {"vae_name": m.vae_name},
    }

    # --- LoRA chain --------------------------------------------------------
    model_ref: Link = ["unet", 0]
    for i, lora in enumerate(loras):
        nid = f"lora_{i}"
        g[nid] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "lora_name": lora.name,
                "strength_model": lora.strength,
                "model": model_ref,
            },
        }
        model_ref = [nid, 0]

    g["model_sampling"] = {
        "class_type": "ModelSamplingAuraFlow",
        "inputs": {"shift": adv.shift, "model": model_ref},
    }
    g["dcw"] = {
        "class_type": "DCWModelPatch",
        "inputs": {
            "lambda_l": adv.lambda_l,
            "lambda_h": adv.lambda_h,
            "dcw_enabled": adv.dcw_enabled,
            "alpha_l": adv.alpha_l,
            "alpha_h": adv.alpha_h,
            "cwm_enabled": adv.cwm_enabled,
            "smc_preset": adv.smc_preset,
            "smc_lambda": adv.smc_lambda,
            "smc_k": adv.smc_k,
            "model": ["model_sampling", 0],
        },
    }
    patched: Link = ["dcw", 0]

    # --- conditioning ------------------------------------------------------
    g["pos"] = {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": positive, "clip": ["clip", 0]},
    }
    g["neg"] = {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": cfg.negative, "clip": ["clip", 0]},
    }

    # --- stage 1: base generation -----------------------------------------
    g["latent"] = {
        "class_type": "EmptyLatentImage",
        "inputs": {
            "width": size.width,
            "height": size.height,
            "batch_size": size.batch_size,
        },
    }
    g["ksampler1"] = {
        "class_type": "KSampler",
        "inputs": {
            "seed": seed1,
            "steps": s1.steps,
            "cfg": s1.cfg,
            "sampler_name": s1.sampler_name,
            "scheduler": s1.scheduler,
            "denoise": s1.denoise,
            "model": patched,
            "positive": ["pos", 0],
            "negative": ["neg", 0],
            "latent_image": ["latent", 0],
        },
    }
    g["vaedecode1"] = {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["ksampler1", 0], "vae": ["vae", 0]},
    }
    g["save_base"] = {
        "class_type": "SaveImageWebsocket",
        "inputs": {"images": ["vaedecode1", 0]},
    }

    # --- upscale + re-encode ----------------------------------------------
    g["upscale_model"] = {
        "class_type": "UpscaleModelLoader",
        "inputs": {"model_name": ups.model_name},
    }
    g["upscale_apply"] = {
        "class_type": "ImageUpscaleWithModel",
        "inputs": {"upscale_model": ["upscale_model", 0], "image": ["vaedecode1", 0]},
    }
    g["downscale"] = {
        "class_type": "ImageScaleBy",
        "inputs": {
            "upscale_method": ups.method,
            "scale_by": ups.scale_by,
            "image": ["upscale_apply", 0],
        },
    }
    g["vaeencode"] = {
        "class_type": "VAEEncode",
        "inputs": {"pixels": ["downscale", 0], "vae": ["vae", 0]},
    }

    # --- stage 2: hires fix ------------------------------------------------
    g["ksampler2"] = {
        "class_type": "KSamplerAdvanced",
        "inputs": {
            "add_noise": s2.add_noise,
            "noise_seed": seed2,
            "steps": s2.steps,
            "cfg": s2.cfg,
            "sampler_name": s2.sampler_name,
            "scheduler": s2.scheduler,
            "start_at_step": s2.start_at_step,
            "end_at_step": s2.end_at_step,
            "return_with_leftover_noise": s2.return_with_leftover_noise,
            "model": patched,
            "positive": ["pos", 0],
            "negative": ["neg", 0],
            "latent_image": ["vaeencode", 0],
        },
    }
    g["vaedecode2"] = {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["ksampler2", 0], "vae": ["vae", 0]},
    }
    g["save_final"] = {
        "class_type": "SaveImageWebsocket",
        "inputs": {"images": ["vaedecode2", 0]},
    }

    info: BuildInfo = {
        "seed1": seed1,
        "seed2": seed2,
        "positive": positive,
        "loras": [lora.name for lora in loras],
    }
    return g, dict(OUTPUT_LABELS), info
