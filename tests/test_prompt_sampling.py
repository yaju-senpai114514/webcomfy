from __future__ import annotations

import random
import unittest

from pydantic import ValidationError

from gen.models import PromptSpec, WildcardBlock, WildcardItem
from gen.prompt import resolve_positive


class CombinatorialSamplingTests(unittest.TestCase):
    def test_legacy_config_keeps_result_and_rng_consumption(self) -> None:
        # Deliberately omit sample_count, as old configs and embedded images do.
        spec = PromptSpec.model_validate({
            "base": "start, __pick__, end",
            "blocks": [{
                "input": "__pick__",
                "items": [
                    {"text": "a", "weight": 1, "enabled": True},
                    {"text": "b", "weight": 3, "enabled": True},
                ],
                "children": [],
            }],
        })
        self.assertEqual(spec.blocks[0].sample_count, 1)

        rng = random.Random(71234)
        resolved = resolve_positive(spec, rng)
        next_after_resolve = rng.random()

        # Legacy picker: exactly one random() scaled by the total weight.
        legacy_rng = random.Random(71234)
        roll = legacy_rng.random() * 4
        legacy_choice = "a" if roll < 1 else "b"
        self.assertEqual(resolved, f"start, {legacy_choice}, end")
        self.assertEqual(next_after_resolve, legacy_rng.random())

    def test_weighted_sample_is_without_replacement(self) -> None:
        block = WildcardBlock(
            input="__pick__",
            sample_count=3,
            items=[
                WildcardItem(text="a", weight=1),
                WildcardItem(text="b", weight=2),
                WildcardItem(text="c", weight=3),
            ],
        )
        result = resolve_positive(
            PromptSpec(base="__pick__", blocks=[block]), random.Random(99)
        )
        self.assertEqual(set(result.split(", ")), {"a", "b", "c"})
        self.assertEqual(len(result.split(", ")), 3)

    def test_count_is_capped_by_selectable_pool_and_noprompt_uses_a_slot(self) -> None:
        block = WildcardBlock(
            input="__pick__",
            sample_count=4,
            items=[
                WildcardItem(text="a"),
                WildcardItem(text="NOPROMPT"),
                WildcardItem(text="disabled", enabled=False),
                WildcardItem(text="zero", weight=0),
            ],
        )
        result = resolve_positive(
            PromptSpec(base="before, __pick__, after", blocks=[block]),
            random.Random(3),
        )
        self.assertEqual(result, "before, a, after")

    def test_sample_count_range_is_validated(self) -> None:
        with self.assertRaises(ValidationError):
            WildcardBlock(sample_count=0)
        with self.assertRaises(ValidationError):
            WildcardBlock(sample_count=65)


if __name__ == "__main__":
    unittest.main()
