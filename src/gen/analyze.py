"""Static analysis of a PromptSpec's wildcard tree — catch config mistakes
without running ComfyUI.

It traverses every block (depth-first) and computes, by fixpoint, the set of
tokens that could ever appear in the running prompt (the base prompt plus every
token a *firing* block's selectable candidate can introduce). From that it flags:

  - dead_block ("never enabled branch"): a block whose input trigger token can
    never appear, so the block never fires.
  - no_candidate: a reachable block with no selectable candidate (every item is
    disabled or weight 0) — it can never substitute its trigger.
  - unsubstituted_token ("never substituted"): a __placeholder__ token that can
    be produced but has no block able to substitute it, so it leaks to the final
    prompt.

The token reachability is an order-insensitive over-approximation, so a flagged
dead_block is genuinely dead under the most generous assumptions (no false
positives); ordering-dependent dead code is not flagged.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from gen.models import NOPROMPT, PromptSpec, WildcardBlock, _tokenize

_PLACEHOLDER_RE = re.compile(r"^__.+__$")  # a wildcard placeholder token, e.g. __artist__


@dataclass
class Issue:
    severity: str  # "error" | "warning"
    kind: str  # dead_block | no_candidate | unsubstituted_token
    path: str  # human-readable location ("" for whole-spec issues)
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _walk(
    blocks: list[WildcardBlock], parent: WildcardBlock | None = None, prefix: str = ""
) -> list[tuple[WildcardBlock, str, WildcardBlock | None]]:
    """Every block in the tree with its human-readable path and parent, depth-first."""
    out: list[tuple[WildcardBlock, str, WildcardBlock | None]] = []
    for i, b in enumerate(blocks, 1):
        label = b.input.strip() or "컨테이너"
        path = f"{prefix}#{i} ({label})"
        out.append((b, path, parent))
        out.extend(_walk(b.children, parent=b, prefix=path + " ▸ "))
    return out


def _selectable(block: WildcardBlock) -> list[Any]:
    """Items that can actually be chosen (enabled and positive weight)."""
    return [it for it in block.items if it.enabled and it.weight > 0]


def _produced_by(block: WildcardBlock) -> set[str]:
    """Tokens a block can introduce when it fires. Empty-input blocks never
    substitute (so introduce nothing); NOPROMPT items insert nothing."""
    toks: set[str] = set()
    if _tokenize(block.input):
        for it in _selectable(block):
            if it.text.strip() != NOPROMPT:
                toks.update(_tokenize(it.text))
    return toks


def _clip(s: str, n: int = 40) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + "…"


def _direct_consumers(children: list[WildcardBlock]) -> set[str]:
    """Trigger tokens that some *direct* child block (with a selectable candidate)
    consumes. Under pure parent-only scope, a token a block emits can only be
    substituted by one of these — never by a sibling or anything further away."""
    cons: set[str] = set()
    for c in children:
        trig = _tokenize(c.input)
        if trig and _selectable(c):
            cons.update(trig)
    return cons


def reachable_tokens(spec: PromptSpec) -> set[str]:
    """Fixpoint of tokens that can ever appear: base + whatever firing blocks add."""
    all_blocks = [b for b, _, _ in _walk(spec.blocks)]
    reachable = set(_tokenize(spec.base))
    changed = True
    while changed:
        changed = False
        for b in all_blocks:
            trig = _tokenize(b.input)
            if trig and any(t in reachable for t in trig):
                for tok in _produced_by(b):
                    if tok not in reachable:
                        reachable.add(tok)
                        changed = True
    return reachable


def analyze_spec(spec: PromptSpec) -> list[Issue]:
    """Detect problems under *pure parent-only tree scope*: a token flows only
    from a block to its direct children — never to siblings or across the tree.

    A block fires only if its trigger is in the scope handed down by its parent
    (base for top-level; a parent's candidate output for a child); a token a block
    emits must be substituted by one of its *direct* children. The loose resolver
    is more permissive (it runs on the whole prompt), so anything flagged here is
    a tree-placement issue even when generation happens to work today.
    """
    issues: list[Issue] = []
    reach = reachable_tokens(spec)  # global: only to tell "dead" from "out of scope"

    # tokens that some reachable, selectable block could substitute *anywhere*
    # (loose). Used only to classify a leak as definite vs strict-tree.
    consumed: set[str] = set()
    for b, _, _ in _walk(spec.blocks):
        trig = _tokenize(b.input)
        if trig and any(t in reach for t in trig) and _selectable(b):
            consumed.update(trig)

    reported: set[tuple[str, str]] = set()

    def report_leak(path: str, tok: str, detail: str) -> None:
        if (path, tok) in reported:
            return
        reported.add((path, tok))
        if tok not in consumed:  # no block consumes it anywhere → leaks even in the loose resolver
            issues.append(Issue(
                "error", "unsubstituted_token", path,
                f"{detail} '{tok}'을(를) 치환할 블록이 없어 최종 프롬프트에 그대로 남습니다.",
            ))
        else:  # consumable elsewhere, but not by a direct child → strict-tree leak
            issues.append(Issue(
                "error", "strict_leak", path,
                f"{detail} '{tok}'을(를) 직속 하위 블록이 치환하지 않습니다 — 부모 전용(순수 트리) 스코프에서는 "
                f"형제·전역 토큰으로 치환할 수 없습니다(현재 순차 해석에서는 우연히 치환됨).",
            ))

    def recurse(blocks: list[WildcardBlock], scope: set[str], prefix: str) -> None:
        for i, b in enumerate(blocks, 1):
            label = b.input.strip() or "컨테이너"
            path = f"{prefix}#{i} ({label})"
            trig = _tokenize(b.input)
            if not trig:  # container: passes its own scope straight through to children
                recurse(b.children, scope, path + " ▸ ")
                continue
            if not any(t in scope for t in trig):  # cannot fire under parent-only scope
                toks = ", ".join(trig)
                if not any(t in reach for t in trig):
                    issues.append(Issue(
                        "error", "dead_block", path,
                        f"트리거 토큰({toks})이 어디서도 생성되지 않아 이 블록은 절대 실행되지 않습니다 (never enabled branch).",
                    ))
                else:
                    issues.append(Issue(
                        "warning", "out_of_scope", path,
                        f"트리거({toks})가 부모(최상단은 base)의 후보에서 생성되지 않습니다 — 부모 전용 스코프에서는 "
                        f"이 블록이 켜지지 않습니다(형제·전역 토큰에 의존; 배치 교정 권장).",
                    ))
                continue  # its subtree gets no tokens from it under parent-only scope
            if not _selectable(b):
                issues.append(Issue(
                    "warning", "no_candidate", path,
                    "트리거는 도달 가능하지만 선택 가능한 후보가 없습니다 (모든 후보가 비활성/가중치 0) — 치환되지 않습니다.",
                ))
                continue
            # b fires: every placeholder it emits must be caught by a direct child
            kids = _direct_consumers(b.children)
            for it in _selectable(b):
                if it.text.strip() == NOPROMPT:
                    continue
                for tok in _tokenize(it.text):
                    if _PLACEHOLDER_RE.match(tok) and tok not in kids:
                        report_leak(path, tok, f"후보 '{_clip(it.text)}'가 생성하는")
            recurse(b.children, _produced_by(b), path + " ▸ ")

    base_tokens = set(_tokenize(spec.base))
    top_consumers = _direct_consumers(spec.blocks)
    for tok in sorted(t for t in base_tokens if _PLACEHOLDER_RE.match(t)):
        if tok not in top_consumers:
            report_leak("base", tok, "base 프롬프트의")
    recurse(spec.blocks, base_tokens, "")

    return issues
