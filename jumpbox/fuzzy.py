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


def best_score(query: str, texts: str | Iterable[str]) -> float | None:
    """Like `score()`, but `texts` may be several independent strings -
    returns whichever one scores best, or `None` if none of them match.

    Scoring each text separately (rather than the caller concatenating
    them into one blob first) matters for longer ones: a short query is
    far more likely to turn up as an accidental subsequence somewhere in
    one long combined string than it is to be a genuinely good match
    against any single short one.
    """
    candidates = [texts] if isinstance(texts, str) else list(texts)
    best: float | None = None
    for text in candidates:
        s = score(query, text)
        if s is not None and (best is None or s > best):
            best = s
    return best


def filter_items(
    query: str,
    items: Iterable[T],
    key,
) -> list[T]:
    """Return `items` that match `query`, best matches first.

    `key` maps an item to either the string to match against, or an
    iterable of independent candidate strings (see `best_score()`) - an
    item matches if any one of them does. With an empty query the
    original order is preserved.
    """

    if not query.strip():
        return list(items)

    scored: list[tuple[float, int, T]] = []
    for index, item in enumerate(items):
        s = best_score(query, key(item))
        if s is not None:
            # `index` keeps the sort stable for equal scores.
            scored.append((s, index, item))

    scored.sort(key=lambda triple: (-triple[0], triple[1]))
    return [item for _, _, item in scored]
