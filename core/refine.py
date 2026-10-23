"""
refine.py —— 全量提炼（切片 → 每片抽事实 → 合并）

流程
----
    map    把全量库按字符切成小段，每段一次调用，抽出若干条带出处的事实
    合并   按文本去重、合并来源指纹 —— 只做并集，不再压缩
    替换   旧版归档后整体换上去（扫的是全量库，输出本来就覆盖上次）

两条硬规矩
----------
1. **不设批数上限**：批数 = 总字数 / extract_chunk_chars，历史再长也全部读到。
   单条超长消息会按字符切段，每段仍指向同一条原文（同一个 fp），
   既能把全文送进模型，又不会让单次调用撑爆上下文。
2. **不做压缩**：抽到的事实原样保留，只去重、合并出处；不再有"合并压缩"这一轮。
   每批能出多少条，由 facts 预算（窗口 10%）除以每批字数算出来，写在 prompt 里。
"""

from __future__ import annotations

import os
import json
import asyncio
import hashlib
from typing import Awaitable, Callable

from .extract import (normalize_kind, normalize_scope, normalize_subject,
                      parse_output, resolve_sources)
from .tokens import estimate_tokens

LLMCall = Callable[[str], Awaitable[str]]


class ExtractionCache:
    """
    按「这一批的原文」缓存提炼结果。

    历史只追加，前面的批永远不会变，所以第二次裁剪时只有尾部那批是新的，
    其余的批直接复用上次的结果——调用次数只跟"新内容"有关，跟库有多大无关。
    缓存里存的是已经带 source_fp 的条目，复用时不用再回溯行号。

    key 里带上 prompt 模板，改了提示词就自动失效，不会拿旧口径的结果凑数。
    """

    # v2：条目里补了 kind。改口径必须换版本号，否则会拿 v1 的缓存（没有 kind）
    # 继续写出一堆 fact，看起来像"修了但没生效"。
    VERSION = "v2"

    def __init__(self, root: str):
        self.dir = os.path.join(root, "cache", "extract")
        os.makedirs(self.dir, exist_ok=True)

    @staticmethod
    def key(batch: list[dict], max_facts: int) -> str:
        digest = hashlib.sha1()
        digest.update(f"{ExtractionCache.VERSION}|{max_facts}|".encode("utf-8"))
        digest.update(MAP_PROMPT.encode("utf-8"))
        for event in batch:
            digest.update(b"\x00")
            digest.update(str(event.get("role", "")).encode("utf-8"))
            digest.update(b"\x01")
            digest.update(str(event.get("text", "")).encode("utf-8"))
        return digest.hexdigest()

    def get(self, key: str):
        """命中返回条目列表（可能是空列表），没命中返回 None。"""
        path = os.path.join(self.dir, key + ".json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, list):
            return None
        return [item for item in data if isinstance(item, dict)
                and item.get("claim") and item.get("source_fp")]

    def put(self, key: str, claims: list[dict]) -> None:
        path = os.path.join(self.dir, key + ".json")
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(claims, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

MAP_PROMPT = """你在整理一段聊天记录里的长期记忆。

只提取「换个时间、换个场合依然成立」的信息：
- 身份与背景（在考研、独居、养了只猫）
- 稳定偏好（怕黑、喜欢被叫小名、讨厌说教）
- 重要关系（和谁亲近、和谁有过节）
- 长期目标与进行中的事

不要提取：一次性的情绪、当天天气、临时安排、你自己的回复。

输出严格 JSON 数组：
[{"claim": "对方怕黑，睡觉要留灯", "kind": "preference", "subject": "发送者ID", "scope": "people", "sources": [行号, ...]}]

规则：
- claim 用第三人称、不超过 40 字
- kind 只能是：identity（身份）/ relationship（关系）/ preference（偏好）/ fact（其他长期事实）/ event（一次性事件）
- subject 填这条事实说的是谁：只能是下面对话里**出现过**的发送者 ID，拿不准就留空字符串
- scope 二选一：
  · people —— 关于这个人的长期事实（职业、称呼、长期偏好），他在别的会话里也该被记住
  · session —— 只在这个群里成立的事（本群角色、群内约定、与主线无关的私事）
- sources 填来源行号
- 最多 MAX_FACTS 条，宁缺毋滥
- 没有值得记的就输出 []

已有事实（不要重复）：
EXISTING

对话（编号|发送者|角色|内容）：
LINES
"""


def chunk_events(events: list[dict], chunk_chars: int = 6000) -> list[list[dict]]:
    """
    按字符把全量事件切批：批数 = 总字数 / chunk_chars，一条都不丢。

    超过 chunk_chars 的单条会被切成几段，每段指向同一条原文（同一个 fp）——
    原文只存一份，只是送进模型时分几次看。
    """
    chunk_chars = max(1, int(chunk_chars or 6000))
    chunks: list[list[dict]] = []
    current: list[dict] = []
    size = 0

    for event in events:
        text = str(event.get("text", "")).strip()
        if not text:
            continue
        fp = str(event.get("fp", ""))
        role = event.get("role", "")
        pieces = [text[i:i + chunk_chars]
                  for i in range(0, len(text), chunk_chars)]
        for piece in pieces:
            if size + len(piece) > chunk_chars and current:
                chunks.append(current)
                current = []
                size = 0
            # 整条复制：ts / sender / session 都要带进批里，
            # 否则提炼拿不到发送者（subject 全空）、纪要拿不到时间锚
            current.append({**event, "text": piece})
            size += len(piece)

    if current:
        chunks.append(current)
    return chunks


def build_map_prompt(batch: list[dict], existing: list[str],
                     max_facts: int = 8) -> tuple[str, list[str], list[str]]:
    """
    返回 (prompt, 行号 -> 指纹, 行号 -> 发送者)。

    正文**不截断**：切批时已经按 chunk_chars 控制过大小，这里再截就白切了。
    max_facts：这一批最多让它出几条，由 facts 预算换算而来。
    发送者跟着行号走：模型只能从这些 ID 里挑，挑不出来就留空。
    """
    lines = []
    fps = []
    senders = []
    for index, event in enumerate(batch, 1):
        text = " ".join(str(event.get("text", "")).split())
        if not text:
            continue
        sender = str(event.get("sender") or "").strip() or "未知"
        lines.append(f"{index}|{sender}|{event.get('role', '')}|{text}")
        fps.append(str(event.get("fp", "")))
        senders.append(str(event.get("sender") or ""))

    existing_text = "\n".join(f"- {c}" for c in existing[-40:]) or "（无）"
    prompt = (MAP_PROMPT
              .replace("MAX_FACTS", str(max(1, int(max_facts or 1))))
              .replace("EXISTING", existing_text)
              .replace("LINES", "\n".join(lines)))
    return prompt, fps, senders


def normalize(text: str) -> str:
    return " ".join(str(text).split()).strip().lower()


def dedupe_claims(claims: list[dict]) -> list[dict]:
    """按归一化文本去重，合并来源指纹。只做并集，不截断、不设条数上限。"""
    seen: dict[str, dict] = {}
    order: list[str] = []
    for item in claims:
        claim = str(item.get("claim", "")).strip()
        if not claim:
            continue
        key = normalize(claim)
        if key in seen:
            merged = seen[key]
            merged["source_fp"] = list(dict.fromkeys(
                merged["source_fp"] + list(item.get("source_fp") or [])))
            # 同一句话在多批里出现时，有一批标了更具体的类型就用它，
            # 别让先到的那条 "fact" 把 identity/preference 盖掉
            if merged["kind"] == "fact":
                merged["kind"] = normalize_kind(item.get("kind"))
            # 主角和可见范围同理：空的让位给有值的，session 让位给 people
            if not merged.get("subject"):
                merged["subject"] = normalize_subject(item.get("subject"))
            if normalize_scope(item.get("scope")) == "people":
                merged["scope"] = "people"
            continue
        seen[key] = {
            "claim": claim,
            "kind": normalize_kind(item.get("kind")),
            "subject": normalize_subject(item.get("subject")),
            "scope": normalize_scope(item.get("scope")),
            "source_fp": list(dict.fromkeys(item.get("source_fp") or [])),
        }
        order.append(key)
    return [seen[k] for k in order]


def total_tokens(claims: list[dict]) -> int:
    return sum(estimate_tokens(c["claim"]) + 2 for c in claims)


async def run(llm_call: LLMCall, events: list[dict], store,
              target_tokens: int, model: str = "",
              chunk_chars: int = 6000, max_facts: int = 8,
              replace: bool = True, on_progress=None,
              cache: "ExtractionCache | None" = None,
              concurrency: int = 6) -> dict:
    """
    扫全量库提炼事实，写进 store。

    replace=True（默认）：扫的是全量库，输出本来就覆盖上次的一切，
        所以用「归档旧版 → 原子替换」而不是合并，避免过时条目越积越多。
    replace=False：退回按 claim 合并，用于增量补充场景。

    target_tokens：facts 预算（窗口 × 10%），只用于统计，不再拿来压缩。
    max_facts：每批最多抽几条（预算 ÷ 每批字数），写进 prompt。
    cache：按批缓存提炼结果。历史只追加，前面的批不会变，
        命中缓存就不调模型——全量覆盖、成本只跟新内容有关。
    concurrency：并发批数。串行跑 20 批要三分钟，6 路并发不到一分钟。
        每批是独立的一次调用，除了"已有事实"这一栏，批次之间互不依赖；
        所以这里只传 store 里现成的事实，不传本轮的累积结果——
        代价是跨批可能重复，交给 dedupe_claims 合并，不影响正确性。
    """
    batches = chunk_events(events, chunk_chars=chunk_chars)
    if not batches:
        return {"batches": 0, "claims": 0, "tokens": 0, "skipped": True}

    existing = store.current_claims()
    collected: list[dict] = []
    failed = 0
    cached = 0
    semaphore = asyncio.Semaphore(max(1, int(concurrency or 1)))

    async def work(index: int, batch: list[dict]):
        cache_key = cache.key(batch, max_facts) if cache is not None else ""
        if cache is not None:
            hit = cache.get(cache_key)
            if hit is not None:
                return index, hit, True

        prompt, fps, senders = build_map_prompt(
            batch, existing, max_facts=max_facts)
        try:
            async with semaphore:
                raw = await llm_call(prompt)
        except Exception:
            return index, None, False

        sender_set = {s for s in senders if s}
        claims = []
        for item in parse_output(raw):
            sources = resolve_sources(item.get("sources", []), fps)
            if not sources:
                continue          # 没出处的事实一律不要
            subject = normalize_subject(item.get("subject"))
            if subject and subject not in sender_set:
                subject = ""      # 模型编了一个这批里不存在的 ID，不能信
            claims.append({
                "claim": item["claim"],
                # 类型必须一起带下来：replace_all 只看这个字段，
                # 漏了就全被 normalize_kind 兜底成 "fact"，权重排序等于没有
                "kind": item.get("kind") or "fact",
                "subject": subject,
                "scope": normalize_scope(item.get("scope")),
                "source_fp": sources,
            })

        if cache is not None:
            cache.put(cache_key, claims)
        return index, claims, False

    results = await asyncio.gather(*(work(i, b) for i, b in enumerate(batches)))
    for index, claims, from_cache in sorted(results, key=lambda row: row[0]):
        if claims is None:
            failed += 1
        else:
            if from_cache:
                cached += 1
            collected.extend(claims)
        if on_progress:
            on_progress(index + 1, len(batches))

    claims = dedupe_claims(collected)
    usable = [c for c in claims if c.get("source_fp")]
    tokens = total_tokens(usable)

    # 一批都没成功时不要替换，否则会把已有认知清空成残缺集合
    all_failed = failed >= len(batches)
    wrote = 0
    if replace and not all_failed:
        wrote = store.replace_all(usable, model=model)
    elif replace and all_failed:
        return {
            "batches": len(batches), "failed_batches": failed,
            "claims": len(claims), "written": 0,
            "tokens": tokens,
            "target_tokens": target_tokens,
            "replaced": False,
            "error": "全部批次失败，保留旧事实",
        }
    else:
        for claim in usable:
            if store.upsert(claim["claim"], claim["source_fp"], model=model,
                            kind=claim.get("kind") or "fact"):
                wrote += 1

    return {
        "batches": len(batches),
        "failed_batches": failed,
        "cached_batches": cached,
        "claims": len(claims),
        "written": wrote,
        "tokens": tokens,
        "target_tokens": target_tokens,
        "facts_per_batch": max_facts,
        "over_budget": bool(target_tokens) and tokens > target_tokens,
        "replaced": replace,
    }
