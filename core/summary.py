"""
summary.py —— 阶段纪要（滚动）

裁掉的那段原文不在眼前了，"最近发生了什么"就断了。这一层把它压成一份带日期的
纪要，补的是**叙事**，不替代原文：

    原文永远在 vault 里，纪要只是"前情提要"，模糊了还能查回去。

三条硬约束
----------
1. **滚动**：只把"这次新被裁掉的那一段"并进旧纪要，绝不每次重算全量。
   否则成本和漂移都会失控。
2. **按会话**：叙事是会话内的，两个群的经历不能串。
3. **带日期**：每条要点标时间范围，模型才能顺着时间回查原文。

纪要里明确写着"可能有偏差，引用前先核对原文"——这句话 beyond 写不了，
因为它压完就把原文丢了。
"""

from __future__ import annotations

import os
import json
import hashlib
from datetime import datetime
from typing import Awaitable, Callable

from .vault import now
from .tokens import estimate_tokens

LLMCall = Callable[[str], Awaitable[str]]

SEGMENT_MAX_LINES = 8
MAX_SUMMARY_CHARS = 2000
PROMPT_VERSION = "v1"

ROLE_NAMES = {"user": "对方", "assistant": "机器人", "tool": "工具",
              "system": "系统", "summary": "摘要", "memory": "记忆"}

# 模型可能用来表示"这段没内容"的占位，混进纪要中间会很扎眼
_EMPTY_MARKERS = {"（这段时间没什么事）", "(这段时间没什么事)",
                  "（这段时间没什么事。）", "本段无内容"}

SEGMENT_PROMPT = """你在整理一段即将从上下文里淡出的聊天记录。
目标只有一个：让机器人以后读到这份纪要时，知道"这段时间发生了什么"。

写法：
- 一行一条，**以时间开头**，例如「10-21 晚上：聊到工作压力，她情绪低落」
- 只写事件、进展、约定、情绪转折；不写寒暄，不复述原句
- **不要写"她是谁、喜欢什么、什么性格"**（那是人物卡的事，这里重复就浪费了）
- 最多 MAX_LINES 条，每条不超过 40 字
- 这段没什么值得记的，就输出空内容，不要写"没什么事"这类占位

对话（时间|角色|内容）：
LINES

只输出纪要本身，不要解释、不要 JSON。
"""

MERGE_PROMPT = """下面是同一段历史的旧纪要，和刚新增的一段。请合并成一份新纪要。

要求：
- 按时间顺序，一行一条，以时间开头
- 旧内容可以压缩，但不能丢掉重要的事件、约定、转折
- 总长度不超过 MAX_CHARS 字；超了先砍寒暄和重复，别砍事件
- 不要写"她是谁、喜欢什么"（那是人物卡的事）

旧纪要：
OLD

新增：
NEW

只输出合并后的纪要，不要解释。
"""


def _now_iso() -> str:
    return now().isoformat(timespec="seconds")


def normalize_lines(raw: str, max_lines: int = SEGMENT_MAX_LINES) -> str:
    """把模型输出收拾成"一行一条"的纯文本，去掉代码块和多余空行。"""
    text = str(raw or "").strip()
    if "```" in text:
        parts = [p for p in text.split("```") if p.strip()]
        if parts:
            text = parts[0]
            if text.strip().lower().startswith("text"):
                text = text.strip()[4:]

    lines = []
    for line in text.splitlines():
        line = line.strip().strip("-*• ").strip()
        if not line:
            continue
        # 空批次的占位句不能混进纪要中间
        if line in _EMPTY_MARKERS:
            continue
        if len(line) > 150:
            line = line[:150] + "…"
        lines.append(line)
        if max_lines and len(lines) >= max_lines:
            break
    return "\n".join(lines)


def transcript(batch: list[dict]) -> str:
    """带时间戳的对话文本，供模型写日期锚点。"""
    out = []
    for event in batch:
        ts = str(event.get("ts", "")).replace("T", " ")[:16]
        role = ROLE_NAMES.get(str(event.get("role", "")), str(event.get("role", "")))
        text = " ".join(str(event.get("text", "")).split())
        if not text:
            continue
        out.append(f"{ts}|{role}|{text}")
    return "\n".join(out)


def build_segment_prompt(batch: list[dict], max_lines: int = SEGMENT_MAX_LINES) -> str:
    return (SEGMENT_PROMPT
            .replace("MAX_LINES", str(max(1, int(max_lines or 1))))
            .replace("LINES", transcript(batch)))


def build_merge_prompt(old: str, new: str, max_chars: int = MAX_SUMMARY_CHARS) -> str:
    return (MERGE_PROMPT
            .replace("MAX_CHARS", str(max(200, int(max_chars or 200))))
            .replace("OLD", str(old or "（无）").strip())
            .replace("NEW", str(new or "（无）").strip()))


class SummaryStore:
    """一份会话一份纪要：summaries/<会话键>.json"""

    def __init__(self, root: str):
        self.dir = os.path.join(root, "summaries")
        os.makedirs(self.dir, exist_ok=True)

    @staticmethod
    def _key(session: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_"
                       for ch in (session or "default"))[:120]
        return safe or "default"

    def _path(self, session: str) -> str:
        return os.path.join(self.dir, self._key(session) + ".json")

    def load(self, session: str) -> dict:
        path = self._path(session)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, session: str, data: dict) -> None:
        path = self._path(session)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def all(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        if not os.path.isdir(self.dir):
            return out
        for name in os.listdir(self.dir):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.dir, name), "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                out[name[:-5]] = data
        return out

    def stats(self) -> dict:
        items = self.all()
        return {
            "sessions": len(items),
            "chars": sum(len(str(v.get("text", ""))) for v in items.values()),
        }


class SummaryCache:
    """按批缓存：同一批原文的纪要永远一样，裁剪第二次就不用再调模型。"""

    VERSION = "v1"

    def __init__(self, root: str):
        self.dir = os.path.join(root, "cache", "summary")
        os.makedirs(self.dir, exist_ok=True)

    @staticmethod
    def key(batch: list[dict], max_lines: int) -> str:
        digest = hashlib.sha1()
        digest.update(f"{SummaryCache.VERSION}|{PROMPT_VERSION}|"
                      f"{max_lines}|".encode("utf-8"))
        digest.update(SEGMENT_PROMPT.encode("utf-8"))
        for event in batch:
            digest.update(b"\x00")
            digest.update(str(event.get("ts", "")).encode("utf-8"))
            digest.update(b"\x01")
            digest.update(str(event.get("text", "")).encode("utf-8"))
        return digest.hexdigest()

    def get(self, key: str):
        path = os.path.join(self.dir, key + ".json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, str) else None

    def put(self, key: str, text: str) -> None:
        path = os.path.join(self.dir, key + ".json")
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(text, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass


def boundary_index(events: list[dict], boundary_fp: str,
                   keep_recent: int = 20) -> int:
    """
    纪要覆盖到哪一条为止（不含）。

    boundary_fp 是"第一 条仍然留在上下文里的消息"，它之前的一切都该被纪要到。
    找不到边界（重建过、编号变了）就退回"最后 keep_recent 条不动"。
    """
    from .sync import base_fp

    target = base_fp(str(boundary_fp or ""))
    if target:
        for index, event in enumerate(events):
            if base_fp(str(event.get("fp", ""))) == target:
                return index
    return max(0, len(events) - max(1, int(keep_recent or 1)))


def _covered_start(events: list[dict], covered_fp: str, end: int) -> int:
    """上次纪要覆盖到哪了：从它的下一条开始，避免重复总结。"""
    from .sync import base_fp

    target = base_fp(str(covered_fp or ""))
    if not target:
        return 0
    for index, event in enumerate(events[:end]):
        if base_fp(str(event.get("fp", ""))) == target:
            return index + 1
    return 0


async def build(llm_call: LLMCall, events: list[dict], store: SummaryStore,
                session: str, boundary_fp: str = "", chunk_chars: int = 6000,
                max_chars: int = MAX_SUMMARY_CHARS, cache: "SummaryCache | None" = None,
                keep_recent: int = 20, max_lines: int = SEGMENT_MAX_LINES) -> dict:
    """
    把"新被裁掉的那一段"并进纪要。

    返回统计，供日志/面板显示。没有任何新内容时 skipped=True，不写盘。
    """
    from .refine import chunk_events

    result = {"ok": False, "skipped": True, "batches": 0, "cached": 0,
              "chars": 0, "written": False, "error": ""}

    if not events:
        result["error"] = "没有可纪要的内容"
        return result

    end = boundary_index(events, boundary_fp, keep_recent=keep_recent)
    if end <= 0:
        result["error"] = "边界太靠前，没有可纪要的内容"
        return result

    previous = store.load(session)
    start = _covered_start(events, previous.get("covered_fp", ""), end)
    fresh = events[start:end]
    if not fresh:
        result.update({"ok": True, "skipped": True,
                       "chars": len(str(previous.get("text", "")))})
        return result

    batches = chunk_events(fresh, chunk_chars=max(500, int(chunk_chars or 6000)))
    segments: list[str] = []
    failed = 0

    for batch in batches:
        cache_key = cache.key(batch, max_lines) if cache is not None else ""
        if cache is not None:
            hit = cache.get(cache_key)
            if hit is not None:
                segments.append(hit)
                result["cached"] += 1
                continue
        try:
            raw = await llm_call(build_segment_prompt(batch, max_lines=max_lines))
        except Exception:
            failed += 1
            continue
        text = normalize_lines(raw, max_lines=max_lines)
        if not text:
            continue
        segments.append(text)
        if cache is not None:
            cache.put(cache_key, text)

    result["batches"] = len(batches)
    if not segments:
        result["error"] = "所有批次都失败，旧纪要保留"
        return result

    new_text = "\n".join(segments)
    old_text = str(previous.get("text", "")).strip()

    # 首次就超过上限时也要压一次，不能硬截——硬截会把最靠后的（最近的）内容切掉
    if old_text or len(new_text) > max_chars:
        try:
            merged_raw = await llm_call(
                build_merge_prompt(old_text, new_text, max_chars=max_chars))
        except Exception:
            if old_text:
                # 合并失败不清空：旧的留着，新的不写，下次再来
                result["error"] = "合并失败，旧纪要保留"
                return result
            merged = new_text
        else:
            merged = normalize_lines(merged_raw, max_lines=0).strip()
            if not merged:
                result["error"] = "合并结果为空，旧纪要保留"
                return result
    else:
        merged = new_text

    if len(merged) > max_chars:
        merged = merged[:max_chars].rstrip() + "…"

    last = fresh[-1]
    payload = {
        "session": session,
        "text": merged,
        "chars": len(merged),
        "covered_fp": str(last.get("fp", "")),
        "covered_until": str(last.get("ts", "")),
        "updated_at": _now_iso(),
        "version": int(previous.get("version", 0) or 0) + 1,
        "batches": (int(previous.get("batches", 0) or 0) + len(batches)),
    }
    store.save(session, payload)

    result.update({"ok": True, "skipped": False, "chars": len(merged),
                   "written": True, "failed_batches": failed,
                   "covered_until": payload["covered_until"],
                   "version": payload["version"]})
    return result
