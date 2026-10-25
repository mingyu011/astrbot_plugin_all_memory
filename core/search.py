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
            limit: int = 20, fetch_many=None) -> list[dict]:
    """
    把几路候选合成一个排序。

    literal_lists：每次字面查询的结果（各自已按 rank 排好）
    vector_hits：[(fp, 余弦)]，已按相似度降序
    fetch：向量路捞到的 fp 在字面结果里没有时，用它补回 ts/role/text
    fetch_many：可选，一次取多条；有它时不再逐条 fetch
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

    # 向量路独有的 fp：先批量回表，避免 100 条候选开 100 次 SQLite。
    if callable(fetch_many):
        missing = [str(fp) for fp, _score in vector_hits
                   if fp and str(fp) not in payload]
        if missing:
            try:
                fetched = fetch_many(list(dict.fromkeys(missing))) or {}
            except Exception:
                fetched = {}
            for fp, event in fetched.items():
                if not isinstance(event, dict):
                    continue
                payload[str(fp)] = {
                    "fp": str(fp),
                    "ts": str(event.get("ts", "")),
                    "role": str(event.get("role", "")),
                    "text": str(event.get("text", "")),
                    "session": event.get("session"),
                }

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


_SIM_CHARS = 1200


def _bigrams(text: str) -> set[str]:
    body = str(text or "")[:_SIM_CHARS]
    chars = [c for c in body if "\u4e00" <= c <= "\u9fff"]
    grams = {"".join(chars[i:i + 2]) for i in range(len(chars) - 1)}
    for word in body.split():
        if len(word) >= 2:
            grams.add(word.lower())
    return grams


def _similarity_sets(ga: set[str], gb: set[str]) -> float:
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / max(1, len(ga | gb))


def _similarity(a: str, b: str) -> float:
    return _similarity_sets(_bigrams(a), _bigrams(b))


def mmr(items: list[dict], limit: int = 20,
        lambda_: float = 0.75) -> list[dict]:
    """
    多样性重排：去掉近似重复，避免前五条都是同一段话。

    不用模型，用字符 bigram 的 Jaccard 相似度做惩罚。
    每条文本只算一次 bigram，长工具输出截断到 1200 字；
    两两相似度带缓存，避免在选择循环里反复算同一对
    （旧实现 100 条候选、每条几万字时会把事件循环卡几十秒）。
    """
    if not items or limit <= 0:
        return []
    ranked = list(items)[:max(40, int(limit) * 2)]
    grams = [_bigrams(item.get("text", "")) for item in ranked]
    max_score = max(float(item.get("score") or 0)
                    for item in ranked) or 1.0

    similarity_cache: dict[tuple[int, int], float] = {}

    def similarity(left: int, right: int) -> float:
        key = (left, right) if left < right else (right, left)
        value = similarity_cache.get(key)
        if value is None:
            value = _similarity_sets(grams[left], grams[right])
            similarity_cache[key] = value
        return value

    remaining = list(enumerate(ranked))
    selected: list[dict] = []
    selected_indices: list[int] = []
    while remaining and len(selected) < limit:
        best_index = 0
        best_value = -1e9
        for offset, (original_index, item) in enumerate(remaining):
            relevance = float(item.get("score") or 0) / max_score
            sim = max((similarity(original_index, chosen)
                       for chosen in selected_indices), default=0.0)
            value = (1.0 - lambda_) * relevance - lambda_ * sim
            if value > best_value:
                best_value = value
                best_index = offset
        original_index, item = remaining.pop(best_index)
        selected.append(item)
        selected_indices.append(original_index)
    return selected


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
