"""Resolve a PromptSpec (base + wildcard blocks) into a concrete prompt string.

A prompt is composed as: a base prompt, then an ordered list of *wildcard
substitution blocks*. Each block has

  - `input`:    a comma-separated set of trigger tokens.
  - `items`:    a list of weighted candidates.
  - `children`: nested sub-blocks applied after this block, recursively (a tree).

Resolution (re-randomised every call, which is what AFK mode loops over):

  1. Tokenise the running prompt (comma-separated, trimmed).
  2. For each block, pick one enabled candidate at random (weighted). Remove
     every token equal to one of the block's `input` tokens; insert the chosen
     text where the *first* such token appeared. Then recursively apply the
     block's `children` to the running prompt (they typically resolve the
     `__token__`s the chosen candidate introduced).

Candidate text of `NOPROMPT` consumes the matched tokens but inserts nothing.
"""

from __future__ import annotations

import random

from gen.models import (
    NOPROMPT,
    GenerationConfig,
    LoraConfig,
    PromptSpec,
    WildcardBlock,
    WildcardItem,
    _tokenize,
)


def _choose(items: list[WildcardItem], rng: random.Random) -> WildcardItem | None:
    """Weighted random pick over enabled items; None if nothing is selectable."""
    pool = [it for it in items if it.enabled and it.weight > 0]
    total = sum(it.weight for it in pool)
    if total <= 0:
        return None
    r = rng.random() * total
    upto = 0.0
    for it in pool:
        upto += it.weight
        if r < upto:
            return it
    return pool[-1]  # float rounding fallback


def _substitute(tokens: list[str], block: WildcardBlock, rng: random.Random) -> list[str]:
    """This block's own substitution: consume input tokens, splice in a chosen item."""
    triggers = set(_tokenize(block.input))
    if not triggers:
        return tokens

    first = next((i for i, tok in enumerate(tokens) if tok in triggers), None)
    if first is None:
        return tokens  # nothing to substitute — block is a no-op for this prompt

    chosen = _choose(block.items, rng)
    if chosen is None:
        return tokens

    kept = [tok for tok in tokens if tok not in triggers]
    insert_at = sum(1 for tok in tokens[:first] if tok not in triggers)
    # NOPROMPT consumes the trigger but inserts nothing.
    replacement = [] if chosen.text.strip() == NOPROMPT else _tokenize(chosen.text)
    kept[insert_at:insert_at] = replacement
    return kept


def _apply_block(tokens: list[str], block: WildcardBlock, rng: random.Random) -> list[str]:
    """Apply a block, then recurse into its child blocks (the tree).

    Children run after this block substitutes — they usually resolve the
    `__token__`s the chosen item introduced. A block with an empty `input` is a
    pure container: it just runs its children. (No-op blocks still recurse, but
    their children's triggers won't be present, so they no-op too.)
    """
    tokens = _substitute(tokens, block, rng)
    for child in block.children:
        tokens = _apply_block(tokens, child, rng)
    return tokens


def resolve_positive(spec: PromptSpec, rng: random.Random) -> str:
    """Run the base prompt through every wildcard block, returning final tokens."""
    tokens = _tokenize(spec.base)
    for block in spec.blocks:
        tokens = _apply_block(tokens, block, rng)
    return ", ".join(tokens)


def resolve(cfg: GenerationConfig, rng: random.Random) -> tuple[str, list[LoraConfig]]:
    """Resolve the positive prompt and the LoRAs it triggers, in one shot."""
    final_positive = resolve_positive(cfg.positive, rng)
    return final_positive, cfg.matched_loras(final_positive)
