"""Tie wildcard resolution and graph building to a single master seed.

One `random.Random(master_seed)` drives the whole pipeline in a fixed order —
first every wildcard selection (prompt.resolve), then any `-1` sampler seeds
(build_workflow). So `(config, master_seed)` reproduces a generation exactly.
"""

from __future__ import annotations

import random

import prompt
from models import GenerationConfig
from workflow import BuildInfo, Graph, build_workflow

# Cap the master seed at JS's safe-integer max (2^53-1) so it round-trips through
# the browser (JSON numbers parse as float64) without precision loss — otherwise
# the seed handed back for reproduction differs from the one used.
MASTER_MAX = 2**53 - 1


def new_master_seed() -> int:
    """A fresh master seed, within JS's safe-integer range for lossless transport."""
    return random.SystemRandom().randint(0, MASTER_MAX)


def prepare(
    cfg: GenerationConfig, master_seed: int
) -> tuple[Graph, dict[str, str], BuildInfo]:
    """Resolve wildcards + build the ComfyUI graph from one seeded RNG."""
    rng = random.Random(master_seed)
    final_positive, loras = prompt.resolve(cfg, rng)
    return build_workflow(cfg, final_positive, loras, rng)
