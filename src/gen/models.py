"""Pydantic schema for a generation request — the single source of truth for the
config shape shared by the web UI, the /api endpoints and the workflow builder."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LoraMode = Literal["always", "conditional"]
Toggle = Literal["enable", "disable"]

NOPROMPT = "NOPROMPT"  # candidate text sentinel: consume the trigger, insert nothing
_LEGACY_WEIGHT_RE = re.compile(r"^\|\s*([0-9]*\.?[0-9]+)\s*\|(.*)$")


def _tokenize(prompt: str) -> list[str]:
    """Split a comma-separated prompt into trimmed, non-empty tokens."""
    return [tok.strip() for tok in prompt.split(",") if tok.strip()]


def _parse_legacy_wildcards(text: str) -> list[dict[str, Any]]:
    """Upgrade the legacy multi-line candidate string into structured items.

    Mirrors the old prompt.parse_wildcards line grammar (and the frontend's
    parseWildcards): `# ` disables (comment), `|n| ` is a weight prefix, blank
    lines are skipped, and `NOPROMPT` is kept verbatim as the consume-only text.
    """
    items: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        enabled = True
        if line.startswith("#"):
            enabled = False
            line = line[1:].strip()
        weight = 1.0
        m = _LEGACY_WEIGHT_RE.match(line)
        if m:
            weight = float(m.group(1))
            line = m.group(2).strip()
        items.append({"enabled": enabled, "weight": weight, "text": line})
    return items


class _Strict(BaseModel):
    """Reject unknown keys so typos in configs/clients fail loudly."""

    model_config = ConfigDict(extra="forbid")


class ModelsConfig(_Strict):
    unet_name: str = "anima_baseV10.safetensors"
    clip_name: str = "qwen_3_06b_base.safetensors"
    vae_name: str = "qwen_image_vae.safetensors"
    weight_dtype: str = "default"
    clip_type: str = "stable_diffusion"


class SizeConfig(_Strict):
    width: int = Field(default=768, ge=64, le=8192)
    height: int = Field(default=1280, ge=64, le=8192)
    batch_size: int = Field(default=1, ge=1, le=64)


class WildcardItem(_Strict):
    """One candidate within a block. `text` of `NOPROMPT` consumes the trigger
    but inserts nothing; `enabled` False excludes it (the old `# comment`)."""

    text: str = ""
    weight: float = Field(default=1.0, ge=0.0)
    enabled: bool = True


class WildcardBlock(_Strict):
    """One substitution step in the prompt-composition pipeline, with optional
    nested sub-blocks (a recursive tree).

    `input` is a comma-separated set of trigger tokens. At resolve time every
    matching token in the running prompt is consumed and the chosen item's text
    is inserted at the first match. Afterwards every block in `children` is
    applied recursively to the running prompt — so a block scopes further
    substitutions under itself (e.g. a `__hair__` block whose child resolves the
    `__len__` token one of its candidates introduces). See prompt.resolve_positive.
    """

    input: str = ""
    items: list[WildcardItem] = Field(default_factory=list)
    children: list["WildcardBlock"] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _upgrade_legacy(cls, data: Any) -> Any:
        """Backward compat: old `{input, wildcards: str}` → `{input, items: [...]}`.

        The single compatibility seam — every load path (configs, embedded webp
        reproduce, API) flows through model_validate, so this covers them all.
        """
        if isinstance(data, dict) and "wildcards" in data:
            legacy = data["wildcards"]
            data = {k: v for k, v in data.items() if k != "wildcards"}
            if not data.get("items") and isinstance(legacy, str):
                data["items"] = _parse_legacy_wildcards(legacy)
        return data


WildcardBlock.model_rebuild()  # resolve the WildcardBlock self forward ref


class PromptSpec(_Strict):
    """A base prompt plus an ordered list of wildcard substitution blocks."""

    base: str = ""
    blocks: list[WildcardBlock] = Field(default_factory=list[WildcardBlock])

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_normalize(cls, data: Any) -> Any:
        """Backward compat: the removed `normalize` flag is ignored if present."""
        if isinstance(data, dict) and "normalize" in data:
            data = {k: v for k, v in data.items() if k != "normalize"}
        return data


class LoraConfig(_Strict):
    name: str
    strength: float = Field(default=1.0, ge=-10.0, le=10.0)
    mode: LoraMode = "conditional"
    trigger: str = ""  # conditional: applied iff this token appears in the final prompt

    def matches(self, prompt_tokens: set[str]) -> bool:
        """Whether this LoRA applies given the resolved final prompt's tokens."""
        if not self.name:
            return False
        if self.mode == "always":
            return True
        trigger = self.trigger.strip()
        return bool(trigger) and trigger in prompt_tokens


class Stage1Config(_Strict):
    seed: int = Field(default=-1, ge=-1)  # -1 = pick a fresh random seed at build time
    steps: int = Field(default=40, ge=1, le=1000)
    cfg: float = Field(default=4.0, ge=0.0, le=100.0)
    denoise: float = Field(default=1.0, ge=0.0, le=1.0)
    sampler_name: str = "er_sde"
    scheduler: str = "sgm_uniform"


class Stage2Config(_Strict):
    noise_seed: int = Field(default=-1, ge=-1)
    steps: int = Field(default=45, ge=1, le=1000)
    start_at_step: int = Field(default=40, ge=0, le=1000)
    end_at_step: int = Field(default=10000, ge=0)
    cfg: float = Field(default=4.0, ge=0.0, le=100.0)
    add_noise: Toggle = "enable"
    return_with_leftover_noise: Toggle = "disable"
    sampler_name: str = "er_sde"
    scheduler: str = "sgm_uniform"


class UpscaleConfig(_Strict):
    model_name: str = "RealESRGAN_x4plus_anime_6B.pth"
    method: str = "nearest-exact"
    scale_by: float = Field(default=0.5, gt=0.0, le=8.0)


class AdvancedConfig(_Strict):
    shift: float = Field(default=2.0, ge=0.0)
    smc_preset: str = "Off"
    lambda_l: float = 0.08
    lambda_h: float = 0.018
    alpha_l: float = 0.0
    alpha_h: float = 0.0
    smc_lambda: float = 6.0
    smc_k: float = 0.1
    dcw_enabled: bool = True
    cwm_enabled: bool = True


class SaveConfig(_Strict):
    """Where/how generated images are written to disk (webp)."""

    enabled: bool = False  # save interactive single generations too (AFK always saves)
    dir: str = "output"
    path_template: str = "{date}/{time}-{seed}.{ext}"
    webp_quality: int = Field(default=90, ge=1, le=100)
    webp_lossless: bool = False
    afk_count: int = Field(default=0, ge=0)  # AFK target image count; 0 = until stopped
    cname: str = ""  # current config name, for the {cname} path placeholder


class GenerationConfig(_Strict):
    positive: PromptSpec = Field(default_factory=PromptSpec)
    negative: str = ""
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    size: SizeConfig = Field(default_factory=SizeConfig)
    loras: list[LoraConfig] = Field(default_factory=list[LoraConfig])
    stage1: Stage1Config = Field(default_factory=Stage1Config)
    upscale: UpscaleConfig = Field(default_factory=UpscaleConfig)
    stage2: Stage2Config = Field(default_factory=Stage2Config)
    advanced: AdvancedConfig = Field(default_factory=AdvancedConfig)
    save: SaveConfig = Field(default_factory=SaveConfig)

    def matched_loras(self, final_positive: str) -> list[LoraConfig]:
        """LoRAs that apply for a resolved prompt: always-on + token-matched."""
        tokens = set(_tokenize(final_positive))
        return [lora for lora in self.loras if lora.matches(tokens)]
