"""模糊匹配（移植自 Pi 的 `packages/tui/fuzzy.ts`，MIT）。

分数越低越好：连续命中、词边界命中加分（减分），间隔/靠后位置扣分。
`fuzzy_filter` 支持 `foo/bar`、`foo bar` 多 token，全部命中才保留。
"""
from __future__ import annotations

import re
from typing import Callable, Iterable, TypeVar

T = TypeVar('T')
_BOUNDARY = re.compile(r'[\s\-_./:]')


def fuzzy_match(query: str, text: str) -> tuple[bool, int]:
    query_lower = (query or '').casefold()
    text_lower = (text or '').casefold()
    if not query_lower:
        return True, 0
    if len(query_lower) > len(text_lower):
        return False, 0

    def score_of(normalized: str) -> tuple[bool, int]:
        index = 0
        score = 0
        last = -1
        consecutive = 0
        while index < len(normalized):
            found = text_lower.find(normalized[index], last + 1)
            if found == -1:
                break
            boundary = found == 0 or bool(_BOUNDARY.match(text_lower[found - 1]))
            if last == found - 1:
                consecutive += 1
                score -= consecutive * 5
            else:
                consecutive = 0
                if last >= 0:
                    score += (found - last - 1) * 2
            if boundary:
                score -= 10
            score += int(found * 0.1)
            last = found
            index += 1
        if index < len(normalized):
            return False, 0
        if normalized == text_lower:
            score -= 100
        return True, score

    matched, score = score_of(query_lower)
    if matched:
        return matched, score
    # `abc123` 与 `123abc` 互换个位置也算命中（模型名/版本号常见）
    match = re.fullmatch(r'([a-z]+)([0-9]+)', query_lower)
    swapped = query_lower
    if match:
        swapped = match.group(2) + match.group(1)
    else:
        match = re.fullmatch(r'([0-9]+)([a-z]+)', query_lower)
        if match:
            swapped = match.group(2) + match.group(1)
    if swapped == query_lower:
        return False, 0
    matched, score = score_of(swapped)
    return (True, score + 5) if matched else (False, 0)


def fuzzy_filter(items: Iterable[T], query: str,
                 key: Callable[[T], str]) -> list[T]:
    tokens = [token for token in re.split(r'[\s/]+', (query or '').strip())
              if token]
    if not tokens:
        return list(items)
    scored = []
    for item in items:
        text = key(item)
        total = 0
        for token in tokens:
            matched, score = fuzzy_match(token, text)
            if not matched:
                break
            total += score
        else:
            scored.append((total, item))
    scored.sort(key=lambda pair: pair[0])
    return [item for _, item in scored]
