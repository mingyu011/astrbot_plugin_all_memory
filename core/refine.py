"""
refine.py —— 事实提炼（切片 → 每片抽事实 → 去重）

两条路径
--------
- append（自动裁剪）：只处理游标之后的新历史，抽到的事实追加进该 id 的
  facts 目录，不覆盖旧事实。
- replace（手动重炼 / 改了切片长度）：对该 id 的全部历史重炼，
  旧 facts 先归档，再整体换上去；注入只用新版。

缓存按 id 分目录：同一个 id 的同一批原文只调一次模型。
"""

from __future__ import annotations

import os
import json
import asyncio
import hashlib
from typing import Awaitable, Callable

from .ids import safe_component
from .extract import (normalize_kind, normalize_scope, normalize_subject,
                      parse_output, resolve_sources, claim_similarity,
                      CLAIM_SIMILARITY)
from .tokens import estimate_tokens

LLMCall = Callable[[str], Awaitable[str]]

# 改了 MAP_PROMPT 或去重口径就升这个版本，旧 facts 的 provenance 能区分。
PROMPT_VERSION = "v3"


class ExtractionCache:
    """
    按「这一批的原文」缓存提炼结果，按 id 分目录。

    key 里带上 prompt 模板，改了提示词就自动失效。
    """

    # v2：条目里补了 kind。改口径必须换版本号。
    VERSION = "v2"

    def __init__(self, root: str, uid: str = "default"):
        self.dir = os.path.join(root, "cache", "extract",
                                safe_component(uid, fallback="default"))
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
  · people —— 关于这个人的长期事实（职业、称呼、长期偏好）
  · session —— 只在这个会话里成立的事
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
    按字符把事件切批：批数 = 总字数 / chunk_chars，一条都不丢。

    超过 chunk_chars 的单条会被切成几段，每段指向同一条原文（同一个 fp）。
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
        pieces = [text[i:i + chunk_chars]
                  for i in range(0, len(text), chunk_chars)]
        for piece in pieces:
            if size + len(piece) > chunk_chars and current:
                chunks.append(current)
                current = []
                size = 0
            current.append({**event, "text": piece})
            size += len(piece)

    if current:
        chunks.append(current)
    return chunks


def build_map_prompt(batch: list[dict], existing: list[str],
                     max_facts: int = 8) -> tuple[str, list[str], list[str]]:
    """
    返回 (prompt, 行号 -> 指纹, 行号 -> 发送者)。
    正文不截断；发送者跟着行号走。
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

    existing_text = "\n".join(f"- {c}" for c in existing[-60:]) or "（无）"
    prompt = (MAP_PROMPT
              .replace("MAX_FACTS", str(max(1, int(max_facts or 1))))
              .replace("EXISTING", existing_text)
              .replace("LINES", "\n".join(lines)))
    return prompt, fps, senders


def normalize(text: str) -> str:
    return " ".join(str(text).split()).strip().lower()


def dedupe_claims(claims: list[dict]) -> list[dict]:
    """按归一化/近义文本去重，合并来源指纹。只做并集，不截断、不设条数上限。"""
    kept: list[dict] = []
    for item in claims:
        claim = str(item.get("claim", "")).strip()
        if not claim:
            continue
        kind = normalize_kind(item.get("kind"))
        target = None
        for existing in kept:
            exact = normalize(claim) == normalize(existing.get("claim", ""))
            similar = (normalize_kind(existing.get("kind")) == kind
                       and claim_similarity(claim, existing.get("claim", ""))
                       >= CLAIM_SIMILARITY)
            if exact or similar:
                target = existing
                break
        if target is not None:
            target["source_fp"] = list(dict.fromkeys(
                target["source_fp"] + list(item.get("source_fp") or [])))
            if normalize_scope(item.get("scope")) == "people":
                target["scope"] = "people"
            if not target.get("subject"):
                target["subject"] = normalize_subject(item.get("subject"))
            if target.get("kind") == "fact" and kind != "fact":
                target["kind"] = kind
            continue
        kept.append({
            "claim": claim,
            "kind": kind,
            "subject": normalize_subject(item.get("subject")),
            "scope": normalize_scope(item.get("scope")),
            "source_fp": list(dict.fromkeys(item.get("source_fp") or [])),
        })
    return kept


def total_tokens(claims: list[dict]) -> int:
    return sum(estimate_tokens(c["claim"]) + 2 for c in claims)


def _cursor_start(events: list[dict], cursor: str,
                  cursor_index: int = 0) -> int:
    """上次提炼到哪条为止；优先条数游标，其次精确指纹，最后退回基础指纹。"""
    from .sync import base_fp

    try:
        index = int(cursor_index or 0)
    except (TypeError, ValueError):
        index = 0
    if 0 < index <= len(events):
        return index

    target = str(cursor or "")
    if target:
        for position, event in enumerate(events):
            if str(event.get("fp", "")) == target:
                return position + 1

    target = base_fp(target)
    if not target:
        return 0
    for position, event in enumerate(events):
        if base_fp(str(event.get("fp", ""))) == target:
            return position + 1
    return 0


async def run(llm_call: LLMCall, events: list[dict], store,
              target_tokens: int, model: str = "",
              chunk_chars: int = 6000, max_facts: int = 8,
              replace: bool = True, on_progress=None,
              cache: "ExtractionCache | None" = None,
              concurrency: int = 6, cursor: str = "",
              cursor_index: int = 0,
              prompt_version: str = PROMPT_VERSION) -> dict:
    """
    扫该 id 的原文提炼事实。

    replace=True：全量重炼，旧版归档后原子替换；用于手动重炼/切片长度变化。
    replace=False：从 cursor 之后增量提炼，逐条 upsert，不覆盖旧事实。
    """
    result = {"batches": 0, "claims": 0, "tokens": 0, "skipped": True,
              "written": 0, "failed_batches": 0, "cached_batches": 0,
              "target_tokens": target_tokens, "mode":
                  "replace" if replace else "append", "covered_fp": "",
              "covered_index": 0, "events_used": 0}

    if not replace:
        start = _cursor_start(events, cursor, cursor_index)
        fresh = events[start:]
    else:
        start = 0
        fresh = list(events)

    if not fresh:
        return result

    batches = chunk_events(fresh, chunk_chars=chunk_chars)
    if not batches:
        return result

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
                subject = ""
            claims.append({
                "claim": item["claim"],
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

    wrote = 0
    if replace:
        # 任何一批失败都不替换：宁可旧版完整，也不要残缺的新版。
        # 成功的批已进缓存，下次只重跑失败的那批。
        if failed:
            return {
                "batches": len(batches), "failed_batches": failed,
                "claims": len(claims), "written": 0, "tokens": tokens,
                "target_tokens": target_tokens, "replaced": False,
                "mode": "replace", "covered_fp": "",
                "events_used": len(fresh),
                "error": f"{failed} 批失败，保留旧事实",
            }
        wrote = store.replace_all(usable, model=model,
                                  prompt_version=prompt_version)
        covered = str(fresh[-1].get("fp", "")) if fresh else ""
        covered_index = len(events)
    else:
        for claim in usable:
            try:
                if store.upsert(claim["claim"], claim["source_fp"], model=model,
                                scope=claim.get("scope") or "session",
                                kind=claim.get("kind") or "fact",
                                prompt_version=prompt_version):
                    wrote += 1
            except Exception:
                continue
        # 有批次失败就不推进游标，下次重试；upsert 会合并重复来源
        if not failed and fresh:
            covered = str(fresh[-1].get("fp", ""))
            covered_index = start + len(fresh)
        else:
            covered_index = start

    result.update({
        "batches": len(batches),
        "failed_batches": failed,
        "cached_batches": cached,
        "claims": len(claims),
        "written": wrote,
        "tokens": tokens,
        "facts_per_batch": max_facts,
        "over_budget": bool(target_tokens) and tokens > target_tokens,
        "replaced": bool(replace),
        "skipped": False,
        "covered_fp": covered,
        "covered_index": covered_index,
        "events_used": len(fresh),
    })
    return result
