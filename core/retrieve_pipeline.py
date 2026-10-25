"""
retrieve_pipeline.py —— 纯检索链路（不依赖 AstrBot）

把 `main.retrieve()` 里“拆词 → 字面 → 向量 → 融合 → MMR → 最早/按天补位”
抽出来，供离线评测和以后的主链路共用。

这里只做同步计算：
- 不调 LLM 扩词（由调用方先算好 expansions 传进来）
- 不决定是否注入（只返回排序后的候选）
- 不读写 vault（只读 index / vectors）

2026-10-25 第一轮检索修复：
- R01 锚点保留：字面 top N 先占位，融合/MMR 只补后面
- R02 按天模式：日期问题按当天内容重排，不再把 20 条插到精确命中前面
- R03 negative：yes/no 问句要求命中包含话题词，抽不到就不过滤
- R05 批量回表：combine 一次 get_many，不再逐条开 SQLite
"""

from __future__ import annotations

from . import query as query_mod
from . import search as search_mod


def _fp(item: dict) -> str:
    return str(item.get("fp") or "")


def _dedupe(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for item in items or []:
        fingerprint = _fp(item)
        if not fingerprint or fingerprint in seen:
            continue
        seen.add(fingerprint)
        out.append(dict(item))
    return out


def _overlap(text: str, terms: list[str]) -> int:
    body = str(text or "")
    return sum(1 for term in terms or () if term and term in body)


def _to_hit(item: dict) -> dict:
    hit = dict(item)
    hit.setdefault("score", 1.0)
    return hit


def _prepend(items: list[dict], base: list[dict], cap: int) -> list[dict]:
    chosen: list[dict] = []
    chosen_fps: set[str] = set()
    for item in items or []:
        fingerprint = _fp(item)
        if not fingerprint or fingerprint in chosen_fps:
            continue
        chosen.append(dict(item))
        chosen_fps.add(fingerprint)
        if len(chosen) >= max(0, int(cap or 0)):
            break
    rest = [item for item in base or [] if _fp(item) not in chosen_fps]
    return chosen + rest


def earliest_hits(index, uid: str = "default", limit: int = 8,
                  terms: list[str] | None = None) -> list[dict]:
    """返回最早那天的候选，按问题词重合度排；不再直接插到 hits 前面。"""
    try:
        day = index.earliest_day(uid)
        if not day:
            return []
        seeds = index.day_messages(day, limit=200, uid=uid)
        if terms:
            seeds.sort(key=lambda item: (
                _overlap(item.get("text", ""), terms),
                str(item.get("ts") or "")), reverse=True)
        return [_to_hit(item) for item in seeds[:max(1, int(limit or 8))]]
    except Exception:
        return []


def day_hits(index, day: str, uid: str = "default", limit: int = 20,
             terms: list[str] | None = None) -> list[dict]:
    """返回某一天的候选，按问题词重合度排；供“明确日期”模式使用。"""
    try:
        items = index.day_messages(day, limit=200, uid=uid)
        if not items:
            return []
        if terms:
            items.sort(key=lambda item: (
                _overlap(item.get("text", ""), terms),
                str(item.get("ts") or "")), reverse=True)
        return [_to_hit(item) for item in items[:max(1, int(limit or 20))]]
    except Exception:
        return []


def _date_mode(index, day: str, uid: str, terms: list[str],
               literal_hits: list[dict] | None = None,
               anchors: list[dict] | None = None,
               fused: list[dict] | None = None) -> list[dict]:
    """明确到某一天：只在这天里排，保留 FTS 命中，再按问题词补。"""
    day_items = day_hits(index, day, uid, limit=20, terms=terms)
    on_day = []
    for source in (anchors, literal_hits, fused):
        for item in source or []:
            if str(item.get("ts") or "")[:10] == day:
                on_day.append(item)
    candidates = _dedupe(on_day + day_items)
    if terms:
        candidates.sort(key=lambda item: (
            _overlap(item.get("text", ""), terms),
            str(item.get("ts") or "")), reverse=True)
    return candidates


def expand_hits(index, hits: list[dict], uid: str = "default",
                span: int = 1, max_hits: int = 3) -> list[dict]:
    """给最相关的前几条补前后文；命中标 is_hit，邻居标 is_context。"""
    if not hits:
        return []
    span = max(0, min(int(span or 0), 3))
    max_hits = max(0, min(int(max_hits or 0), 8))
    out: list[dict] = []
    seen: set[str] = set()
    for rank, hit in enumerate(hits):
        fp = _fp(hit)
        if not fp:
            continue
        if rank >= max_hits or span == 0:
            if fp not in seen:
                seen.add(fp)
                out.append(dict(hit))
            continue
        window = [dict(hit)]
        try:
            for item in index.neighbors(fp, span=span, uid=uid):
                neighbor = dict(item)
                neighbor["is_context"] = True
                neighbor["is_hit"] = False
                window.append(neighbor)
        except Exception:
            pass
        window.sort(key=lambda item: str(item.get("ts") or ""))
        for item in window:
            item_fp = _fp(item)
            if not item_fp or item_fp in seen:
                continue
            seen.add(item_fp)
            item.setdefault("is_hit", item_fp == fp)
            out.append(item)
    return out


def negative_evidence(hits: list[dict], text: str) -> list[dict]:
    """
    yes/no 问句的证据过滤：

    “小木喜欢喝咖啡吗” → 话题词 ["咖啡"]；没有命中包含“咖啡”就返回空。
    抽不出话题词时不过滤，避免误杀。
    """
    if not hits or not query_mod.is_yes_no(text):
        return hits
    topics = query_mod.topic_terms(text)
    if not topics:
        return hits
    return [hit for hit in hits
            if any(topic in str(hit.get("text") or "") for topic in topics)]


def fts_only(index, text: str, uid: str = "default",
             limit: int = 20) -> list[dict]:
    """当前 eval 的基线：只走字面检索，不扩词、不加权。"""
    terms = query_mod.build_terms(text, [], limit=12)
    return index.search_many(terms, limit=limit, uid=uid)


def retrieve_fts(index, text: str, uid: str = "default",
                 limit: int = 20, expansions=None,
                 boost: bool = True) -> list[dict]:
    """字面检索 + 可选的 earliest/按天模式；没有向量。"""
    terms = query_mod.build_terms(text, list(expansions or ()), limit=12)
    hits = index.search_many(terms, limit=max(10, int(limit or 20)), uid=uid)
    if not boost:
        return negative_evidence(hits, text)

    start_day, end_day = query_mod.time_window(text)
    if start_day and start_day == end_day:
        hits = _date_mode(index, start_day, uid, terms,
                          literal_hits=hits, anchors=hits, fused=hits)
    elif query_mod.is_earliest_intent(text):
        early = earliest_hits(index, uid, limit=8, terms=None)
        hits = _prepend(early, hits, cap=5)
    return negative_evidence(hits, text)


def retrieve(index, text: str, uid: str = "default", limit: int = 20,
             vectors=None, embedder=None, expansions=None,
             use_vector: bool = True, use_mmr: bool = True,
             boost: bool = True, expand_context: bool = False,
             candidate_multiplier: float = 2.0,
             anchor_count: int = 5) -> list[dict]:
    """
    完整本地链路：字面 + 向量 → RRF → MMR → 锚点保留 → earliest/按天。

    limit 是“候选数”，不是最终注入数；调用方自己再截。
    """
    text = (text or "").strip()
    if len(text) < 2:
        return []
    limit = max(1, int(limit or 20))
    multiplier = max(1.0, float(candidate_multiplier or 2.0))
    per_route = max(10, int(limit * multiplier))
    terms = query_mod.build_terms(text, list(expansions or ()))
    anchor_terms = query_mod.build_terms(text, [], limit=12)
    date_terms = query_mod.build_terms(text, [], limit=12)

    # R01：先拿“纯字面 top N”当锚点。它不参与 MMR，也不让向量把它挤掉。
    anchors: list[dict] = []
    if anchor_count > 0:
        try:
            anchors = index.search_many(
                anchor_terms, max(1, int(anchor_count)), None, uid)
        except Exception:
            anchors = []

    literal: list[list[dict]] = []
    try:
        hits = index.search_many(terms, per_route, None, uid)
        if hits:
            literal.append(hits)
    except Exception:
        literal = []

    vector_hits: list[tuple[str, float]] = []
    if use_vector and vectors is not None and embedder is not None:
        try:
            query_vector = embedder.vector(text)
            vector_hits = vectors.search(query_vector, per_route, uid)
        except Exception:
            vector_hits = []

    window = query_mod.time_window(text)
    ranked = search_mod.combine(
        literal, vector_hits,
        fetch=lambda fp: index.get(fp, uid),
        fetch_many=lambda fps: index.get_many(fps, uid),
        window=window,
        limit=max(limit * 2, limit))
    if use_mmr:
        ranked = search_mod.mmr(ranked, limit=limit)

    start_day, end_day = window
    if boost and start_day and start_day == end_day:
        ranked = _date_mode(index, start_day, uid, date_terms,
                            literal_hits=literal[0] if literal else [],
                            anchors=anchors, fused=ranked)
        if query_mod.is_earliest_intent(text):
            early = earliest_hits(index, uid, limit=8, terms=None)
            ranked = _prepend(early, ranked, cap=5)
    else:
        # 锚点在前，融合结果在后；同一 fingerprint 去重。
        ranked = _dedupe(anchors + ranked)
        if boost and query_mod.is_earliest_intent(text):
            early = earliest_hits(index, uid, limit=8, terms=None)
            ranked = _prepend(early, ranked, cap=5)

    ranked = negative_evidence(ranked, text)
    if expand_context:
        # 前后文只补在 top5 之后；绝不把锚点/日期/最早命中挤出前排。
        head_count = min(len(ranked), max(5, int(anchor_count or 5)))
        head = ranked[:head_count]
        tail = ranked[head_count:]
        ranked = head + expand_hits(index, tail, uid)
    return ranked
