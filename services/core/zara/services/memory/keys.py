"""Canonical-key derivation for memory claims.

A canonical key groups restatements of the same claim so the dedup/contradiction
judge can compare them. Negation tokens are removed so ``"The launch is on
Friday"`` and ``"The launch is not on Friday"`` share a key and reach the
contradiction path.
"""

from __future__ import annotations

import re

_NEGATIONS = {
    "not",
    "never",
    "no",
    "none",
}

# Expand contractions before tokenising so removing the negation keeps the
# auxiliary verb: "cannot scale" and "can scale" must share a key, otherwise
# the contradiction judge never sees the pair.
_CONTRACTIONS = {
    "cannot": "can not",
    "can't": "can not",
    "won't": "will not",
    "isn't": "is not",
    "aren't": "are not",
    "don't": "do not",
    "doesn't": "does not",
    "didn't": "did not",
    "wasn't": "was not",
    "weren't": "were not",
}


def derive_canonical_key(text: str) -> str:
    """Lowercase, strip punctuation/negation, and bound the key length."""
    lowered = text.lower()
    for contraction, expansion in _CONTRACTIONS.items():
        lowered = lowered.replace(contraction, expansion)
    cleaned = re.sub(r"[^a-z0-9\s]", " ", lowered)
    tokens = [token for token in cleaned.split() if token not in _NEGATIONS]
    return "stmt:" + " ".join(tokens)[:180]
