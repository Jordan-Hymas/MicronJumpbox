"""A tiny, dependency-free fuzzy matcher.

Kept intentionally simple so the app has zero compiled dependencies and
deploys cleanly on locked-down Linux machines. Good enough for filtering
a handful of rooms/hosts as you type.
"""

from __future__ import annotations

from typing import Iterable, TypeVar

T = TypeVar("T")


def score(query: str, text: str) -> float | None:
    """Score how well `query` fuzzy-matches `text`.

    Returns a float (higher is better) or ``None`` if `query` is not a
    subsequence of `text`. An empty query matches everything with score 0.
    Matching is case-insensitive.
    """

    if not query:
        return 0.0

    q = query.lower()
    t = text.lower()

    total = 0.0
    t_index = 0
    streak = 0

    for ch in q:
        found = t.find(ch, t_index)
        if found == -1:
            return None

        # Reward consecutive matches and matches at a word boundary.
        if found == t_index and streak > 0:
            streak += 1
        else:
            streak = 1
        bonus = streak * 2.0
        if found == 0 or t[found - 1] in " -_./@:":
            bonus += 3.0

        # Penalize gaps between matched characters.
        gap = found - t_index
        total += bonus - gap * 0.1
        t_index = found + 1

    # Slightly prefer shorter strings (tighter matches).
    return total - len(t) * 0.01


def filter_items(
    query: str,
    items: Iterable[T],
    key,
) -> list[T]:
    """Return `items` that match `query`, best matches first.

    `key` maps an item to the string that should be matched against.
    With an empty query the original order is preserved.
    """

    if not query.strip():
        return list(items)

    scored: list[tuple[float, int, T]] = []
    for index, item in enumerate(items):
        s = score(query, key(item))
        if s is not None:
            # `index` keeps the sort stable for equal scores.
            scored.append((s, index, item))

    scored.sort(key=lambda triple: (-triple[0], triple[1]))
    return [item for _, _, item in scored]
