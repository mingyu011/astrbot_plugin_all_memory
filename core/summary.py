"""
summary.py —— 阶段纪要（只追加）

0.9.17 起按 id 分目录：`summaries/<id>/summary.json`。
一个私聊 id 一个滚动文件；每次裁剪只处理还没纪要过的历史，追加到文件，
不覆盖旧内容、不合并重写。

原文永远在 vault 里，纪要只是"前情提要"，模糊了还能查回去。
"""

from __future__ import annotations

import os
import json
import hashlib
import re
import difflib
from datetime import datetime
from typing import Awaitable, Callable

from .ids import safe_component
from .vault import now
from .tokens import estimate_tokens

LLMCall = Callable[[str], Awaitable[str]]

SEGMENT_MAX_LINES = 8
MAX_SUMMARY_CHARS = 2000
PROMPT_VERSION = "v2"

ROLE_NAMES = {"user": "对方", "assistant": "机器人", "tool": "工具",
              "system": "系统", "summary": "摘要", "memory": "记忆"}

# 模型可能用来表示"这段没内容"的占位，混进纪要中间会很扎眼
_EMPTY_MARKERS = {"（这段时间没什么事）", "(这段时间没什么事)",
                  "（这段时间没什么事。）", "本段无内容"}

_PLACEHOLDER_RE = re.compile(
    r"^[（(【\[]?\s*(?:这段时间)?\s*(?:没|无|空|没有)\s*"
    r"(?:什么|值得记录)?\s*(?:内容|事)?\s*[)）】\]]?$")
_PREFIX_RE = re.compile(
    r"^(\d{1,2})-(\d{1,2})"
    r"(?:\s+(凌晨|早上|上午|中午|下午|傍晚|晚上|晚|早|深夜"
    r"|\d{1,2}:\d{2}(?:-\d{1,2}(?::\d{2})?)?|\d{1,2}:\d{2}))?"
    r"\s*[：:]\s*")

_SLOT_RANK = {
    "凌晨": 1 * 60, "早上": 7 * 60, "早": 7 * 60,
    "上午": 9 * 60, "中午": 12 * 60,
    "下午": 14 * 60, "傍晚": 17 * 60, "晚上": 19 * 60, "晚": 19 * 60,
    "深夜": 23 * 60,
}


def is_placeholder(line: str) -> bool:
    """模型表示“这段没内容”的各种变体，统一挡掉。"""
    text = str(line or "").strip().strip("-*• ").strip()
    if not text:
        return True
    if text in _EMPTY_MARKERS:
        return True
    bare = text.strip("（）()【】[] 。.").strip()
    if bare in {"空", "无", "空内容", "无内容", "本段无内容",
                "没有内容", "没什么事", "这段时间没什么事",
                "无值得记录的内容", "没有值得记录的内容",
                "无值得记录的事", "没有值得记录的事"}:
        return True
    return bool(_PLACEHOLDER_RE.match(text))


def _line_key(line: str) -> tuple:
    """按 MM-DD + 时间排序；解析不了放最后。"""
    match = _PREFIX_RE.match(str(line or ""))
    if not match:
        return (99, 99, 99 * 60)
    month, day, slot = match.groups()
    if slot and ":" in slot:
        hour, minute = slot.split("-", 1)[0].split(":", 1)
        rank = int(hour) * 60 + int(minute)
    else:
        rank = _SLOT_RANK.get(slot or "", 12 * 60)
    return (int(month), int(day), rank)


def _body(line: str) -> str:
    text = _PREFIX_RE.sub("", str(line or "")).strip()
    return re.sub(r"[\s，。！？、；：,.!?;:~…\-—\"'“”‘’（）()\[\]【】{}<>《》/\\|]+",
                  "", text)


def clean_text(text: str) -> str:
    """
    清理已有纪要：去占位、去精确重复、按日期/时间排序、合并同时刻近义句。

    只处理文本，不动 covered_fp / covered_index；调用方决定是否落盘。
    """
    lines: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or is_placeholder(line):
            continue
        lines.append(line)

    unique: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = re.sub(r"\s+", "", line)
        if key in seen:
            continue
        seen.add(key)
        unique.append(line)

    unique.sort(key=_line_key)

    out: list[str] = []
    for line in unique:
        key = _line_key(line)
        body = _body(line)
        merged = False
        # 同一天同一时刻的“同义改写”只留一条；不做跨时间合并。
        if key[0] != 99 and body:
            for index, existing in enumerate(out):
                if _line_key(existing) != key:
                    continue
                existing_body = _body(existing)
                if not existing_body:
                    continue
                if body == existing_body or body in existing_body \
                        or existing_body in body:
                    if len(body) > len(existing_body):
                        out[index] = line
                    merged = True
                    break
                if difflib.SequenceMatcher(None, body, existing_body).ratio() >= 0.60:
                    if len(body) > len(existing_body):
                        out[index] = line
                    merged = True
                    break
        if not merged:
            out.append(line)
    return "\n".join(out)

SEGMENT_PROMPT = """你在整理一段聊天记录，写进这个人的长期前情纪要。

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
        if is_placeholder(line):
            continue
        if len(line) > 150:
            line = line[:150] + "…"
        lines.append(line)
        if max_lines and len(lines) >= max_lines:
            break
    lines.sort(key=_line_key)
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


class SummaryStore:
    """一个 id 一份滚动纪要：summaries/<id>/summary.json"""

    def __init__(self, root: str, uid: str = "default"):
        self.root = root
        self.uid = safe_component(uid, fallback="default")
        self.dir = os.path.join(root, "summaries")
        self._dir(self.uid)

    def _dir(self, uid: str) -> str:
        path = os.path.join(self.dir, safe_component(uid, fallback="default"))
        os.makedirs(path, exist_ok=True)
        return path

    def _path(self, uid: str | None = None) -> str:
        scope = safe_component(uid or self.uid, fallback="default")
        return os.path.join(self._dir(scope), "summary.json")

    def load(self, uid: str | None = None) -> dict:
        path = self._path(uid)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        # 旧文件里可能混着占位符、重复句、乱序行；读的时候先清一遍，
        # 保证注入到模型的是干净版本。repair() 才会落盘。
        if str(data.get("text") or "").strip():
            data["text"] = clean_text(str(data.get("text") or ""))
            data["chars"] = len(data["text"])
        return data

    def save(self, data: dict, uid: str | None = None) -> None:
        path = self._path(uid)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def repair(self, uid: str | None = None) -> dict:
        """把当前 summary.json 里的占位/重复/乱序清掉并落盘。"""
        path = self._path(uid)
        if not os.path.exists(path):
            return {"ok": False, "error": "还没有纪要", "before": 0, "after": 0}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": str(exc), "before": 0, "after": 0}
        if not isinstance(data, dict):
            return {"ok": False, "error": "summary 不是对象", "before": 0, "after": 0}
        before = str(data.get("text") or "")
        after = clean_text(before)
        data["text"] = after
        data["chars"] = len(after)
        data["repaired_at"] = _now_iso()
        self.save(data, uid)
        return {"ok": True, "before": len(before), "after": len(after),
                "removed": max(0, len(before) - len(after)),
                "path": path}

    def all(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        if not os.path.isdir(self.dir):
            return out
        for name in os.listdir(self.dir):
            path = os.path.join(self.dir, name, "summary.json")
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                out[name] = data
        return out

    def stats(self) -> dict:
        items = self.all()
        return {
            "sessions": len(items),
            "chars": sum(len(str(v.get("text", ""))) for v in items.values()),
        }


class SummaryCache:
    """按批缓存：同一批原文的纪要永远一样。按 id 分目录。"""

    VERSION = "v2"

    def __init__(self, root: str, uid: str = "default"):
        self.dir = os.path.join(root, "cache", "summary",
                                safe_component(uid, fallback="default"))
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


def _covered_start(events: list[dict], covered_fp: str,
                   covered_index: int = 0) -> int:
    """
    上次纪要覆盖到哪了：优先用条数游标，其次精确指纹，最后才退回基础指纹。

    旧实现只按 base_fp 找，遇到 abc / abc-2 这种重复句会退回到 abc，
    把已经纪要过的尾部重新总结一遍，越追加越重复。
    """
    from .sync import base_fp

    try:
        index = int(covered_index or 0)
    except (TypeError, ValueError):
        index = 0
    if 0 < index <= len(events):
        return index

    target = str(covered_fp or "")
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


async def build(llm_call: LLMCall, events: list[dict], store: SummaryStore,
                chunk_chars: int = 6000, cache: "SummaryCache | None" = None,
                max_lines: int = SEGMENT_MAX_LINES,
                uid: str | None = None) -> dict:
    """
    把该 id 还没纪要过的历史，追加进滚动纪要。

    只追加、不覆盖、不合并重写；没有新内容时 skipped=True，不写盘。
    """
    from .refine import chunk_events

    result = {"ok": False, "skipped": True, "batches": 0, "cached": 0,
              "chars": 0, "written": False, "error": ""}

    if not events:
        result["error"] = "没有可纪要的内容"
        return result

    previous = store.load(uid)
    start = _covered_start(events, previous.get("covered_fp", ""),
                           previous.get("covered_index", 0))
    fresh = events[start:]
    if not fresh:
        result.update({"ok": True, "skipped": True,
                       "chars": len(str(previous.get("text", "")))})
        return result

    batches = chunk_events(fresh, chunk_chars=max(500, int(chunk_chars or 6000)))
    segments: list[str] = []
    cached = 0

    for batch in batches:
        cache_key = cache.key(batch, max_lines) if cache is not None else ""
        if cache is not None:
            hit = cache.get(cache_key)
            if hit is not None:
                if hit.strip():
                    segments.append(hit)
                cached += 1
                continue
        try:
            raw = await llm_call(build_segment_prompt(batch, max_lines=max_lines))
        except Exception:
            result["error"] = "有些批次失败，旧纪要保留，下次重试"
            return result
        text = normalize_lines(raw, max_lines=max_lines)
        if text:
            segments.append(text)
            if cache is not None:
                cache.put(cache_key, text)

    result["batches"] = len(batches)
    result["cached"] = cached
    if not segments:
        result["error"] = "模型没有生成纪要内容，旧纪要保留"
        return result

    new_text = "\n".join(segments).strip()
    old_text = str(previous.get("text", "")).strip()
    merged = clean_text((old_text + "\n" + new_text).strip() if old_text else new_text)

    last = fresh[-1]
    payload = {
        "uid": store.uid,
        "text": merged,
        "chars": len(merged),
        "covered_fp": str(last.get("fp", "")),
        "covered_index": len(events),
        "covered_until": str(last.get("ts", "")),
        "updated_at": _now_iso(),
        "version": int(previous.get("version", 0) or 0) + 1,
        "batches": int(previous.get("batches", 0) or 0) + len(batches),
        "prompt_version": PROMPT_VERSION,
    }
    store.save(payload, uid)

    result.update({"ok": True, "skipped": False, "chars": len(merged),
                   "written": True, "covered_until": payload["covered_until"],
                   "covered_index": payload["covered_index"],
                   "version": payload["version"]})
    return result
