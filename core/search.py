"""
search.py —— 多路召回融合

    字面路：每个检索词各查一次 FTS5（整句当关键词是查不到东西的）
    向量路：按意思找（vectors.py）

融合用 RRF（倒数排名），好处是不用调权重、也不用把 BM25 和余弦对齐。
再加两条不花钱的先验：时间命中加分、用户说的话优先。
"""

from __future__ import annotations

RRF_K = 60          # 融合常数，业界默认值，调它收益很低
TIME_BONUS = 0.03   # 命中时间区间：约等于多命中两条
ROLE_WEIGHT = {"user": 1.0, "assistant": 0.88, "tool": 0.6,
               "summary": 0.9, "memory": 0.9}


def rank_score(rank: int, weight: float = 1.0) -> float:
    """RRF：第 1 名 1/61，第 2 名 1/62……加一个权重缩放（向量相似度用）。"""
    return weight / (RRF_K + max(0, rank) + 1)


def combine(literal_lists: list[list[dict]], vector_hits: list[tuple[str, float]],
            fetch=None, window: tuple[str, str] = ("", ""),
            limit: int = 20) -> list[dict]:
    """
    把几路候选合成一个排序。

    literal_lists：每次字面查询的结果（各自已按 rank 排好）
    vector_hits：[(fp, 余弦)]，已按相似度降序
    fetch：向量路捞到的 fp 在字面结果里没有时，用它补回 ts/role/text
    window：(起, 止) 日期，命中加分；猜错了只是没加分，不会排除
    """
    scores: dict[str, float] = {}
    payload: dict[str, dict] = {}

    for hits in literal_lists:
        for rank, hit in enumerate(hits):
            fp = str(hit.get("fp") or "")
            if not fp:
                continue
            scores[fp] = scores.get(fp, 0.0) + rank_score(rank)
            payload.setdefault(fp, hit)

    for rank, (fp, similarity) in enumerate(vector_hits):
        fp = str(fp or "")
        if not fp:
            continue
        weight = max(0.05, min(1.0, float(similarity or 0.0)))
        scores[fp] = scores.get(fp, 0.0) + rank_score(rank, weight)
        if fp not in payload:
            event = {}
            if callable(fetch):
                try:
                    event = fetch(fp) or {}
                except Exception:
                    event = {}
            payload[fp] = {
                "fp": fp,
                "ts": str(event.get("ts", "")),
                "role": str(event.get("role", "")),
                "text": str(event.get("text", "")),
                "session": event.get("session"),
            }

    start, end = window if window else ("", "")
    items: list[dict] = []
    for fp, score in scores.items():
        hit = dict(payload.get(fp) or {"fp": fp})
        stamp = str(hit.get("ts") or "")[:10]
        if start and end and stamp and start <= stamp <= end:
            score += TIME_BONUS
        score *= ROLE_WEIGHT.get(str(hit.get("role") or ""), 0.8)
        hit["score"] = round(score, 6)
        items.append(hit)

    items.sort(key=lambda item: item["score"], reverse=True)
    return items[:max(1, int(limit))]


def rank_facts(facts: list[dict], terms: list[str], limit: int = 0) -> list[dict]:
    """
    事实也按相关度排：和这一轮的话沾边的排前面。

    几百条全量注入确实塞得下，但"沾边"的信息放前面，模型读起来更有用；
    等以后上千条，这一步就是省预算的关键。
    """
    words = [str(t) for t in (terms or []) if len(str(t)) >= 2]

    def score(fact: dict) -> tuple:
        claim = str(fact.get("claim", ""))
        overlap = sum(1 for word in words if word and word in claim)
        weight = int(fact.get("kind_weight") or 0)
        if not weight:
            from .extract import kind_weight
            weight = kind_weight(fact.get("kind"))
        return (overlap, weight, str(fact.get("extracted_at", "")))

    ranked = sorted(facts, key=score, reverse=True)
    return ranked[:limit] if limit and limit > 0 else ranked
