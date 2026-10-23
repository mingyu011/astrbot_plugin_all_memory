"""
sync.py —— 把宿主此刻的完整记忆视图灌进原始层

为什么不用读 DB：on_llm_request 拿到的 req.contexts 就是宿主准备发给模型的
完整历史，包含压缩后的摘要。所以只要盯着这个视图，"宿主怎么变"都无所谓。

内容形态：AstrBot 的 content 常常是「块数组」（think / text / tool_use），
这里抽成可读文本，同时把原始结构留在 raw 里——一个字都不丢。
"""

from __future__ import annotations

import re
import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from .tokens import estimate_tokens
from .vault import TZ, fp_of, now


def base_fp(fp: str) -> str:
    """去掉重复编号后缀：abc-2 → abc。用于「这句话以前出现过没有」的判断。"""
    return fp.split("-", 1)[0] if fp else ""


def assign_unique_fps(index, events: list[dict], taken: set[str]) -> int:
    """
    给每条事件一个唯一指纹。

    同一句话重复出现时编成 base、base-2、base-3……
    这样「晚安」说十次就是十条记录，而不是被当成重复删掉九条；
    同时下一次重扫历史时，只要 base 已经在库里，整句就会被跳过
    （历史是累积重发的，不跳就会每轮翻倍）。

    taken：调用方已经确认在库里存在的 base 指纹。
    返回实际加了后缀的条数。
    """
    used = set(taken or ())
    renamed = 0
    for event in events:
        base = event["fp"]
        if base not in used:
            used.add(base)
            continue

        n = 2
        while True:
            candidate = f"{base}-{n}"
            if candidate not in used and not index.has(candidate):
                used.add(candidate)
                event["fp"] = candidate
                renamed += 1
                break
            n += 1
            if n > 5000:                       # 极端情况兜底，别死循环
                candidate = f"{base}-x{uuid4().hex[:6]}"
                used.add(candidate)
                event["fp"] = candidate
                renamed += 1
                break
    return renamed

# 宿主注入的临时块常带这类标记，见到了要跳过，否则会把自己注入的东西又存一遍
_NO_SAVE_MARKERS = re.compile(
    r"(_no_save|\[no_save\]|<!--\s*no_save\s*-->)", re.IGNORECASE
)

# AstrBot 每轮都会在用户消息里塞一句「Current datetime: 2026-10-20 21:56 (CST)」。
# 这是唯一能拿到的真实发生时间，用它还原历史时间戳。
_REMINDER_RE = re.compile(
    r"Current\s+datetime:\s*(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})",
    re.IGNORECASE,
)

# 只做展示、不计入正文的块类型
_SILENT_BLOCKS = {"think", "thinking", "reasoning", "reasoning_content"}


def extract_content(value: Any) -> tuple[str, str | None]:
    """
    把各种形态的 content 变成 (可读文本, 原始 JSON 或 None)。

    字符串直接返回；块数组抽出所有 text，其余块（思考、工具调用）
    保留在 raw 里，避免丢数据。
    """
    if value is None:
        return "", None
    if isinstance(value, str):
        return value, None

    if isinstance(value, list):
        texts: list[str] = []
        thinking: list[str] = []
        tools: list[str] = []
        results = 0
        keep_raw = False

        for block in value:
            if isinstance(block, str):
                texts.append(block)
                continue
            if not isinstance(block, dict):
                continue

            kind = block.get("type")
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text)
                continue

            if kind in _SILENT_BLOCKS:
                thought = block.get("think") or block.get("reasoning") or ""
                if isinstance(thought, str) and thought.strip():
                    thinking.append(thought.strip())
                keep_raw = True
            elif kind in ("tool_use", "tool_call", "function_call"):
                name = block.get("name") or block.get("tool_name") or "工具"
                tools.append(str(name))
                keep_raw = True
            elif kind in ("tool_result", "tool_call_result"):
                results += 1
                keep_raw = True
            elif isinstance(block.get("content"), str):
                texts.append(block["content"])
            elif kind:
                keep_raw = True

        text = "\n".join(t.strip() for t in texts if t.strip())
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")) \
            if keep_raw else None

        if text:
            return text, raw

        # 没有正文（纯思考 / 纯工具调用）：给一段能读的摘要，别把 JSON 塞进去
        parts = []
        if thinking:
            joined = " ".join(thinking)
            if len(joined) > 200:
                joined = joined[:200] + "…"
            parts.append(f"[思考] {joined}")
        if tools:
            parts.append("[工具] " + ", ".join(tools))
        if parts:
            return "\n".join(parts), raw
        if results:
            # 只有工具结果：正文没法可读化，原始块留在 raw 里，正文保持原样
            return (raw or ""), None
        # 走到这里只剩下「空的 think 块」这类什么内容都没有的骨架。
        # 它不是她说过的话，返回空让调用方跳过，别把 JSON 写进原始层。
        return "", None

    if isinstance(value, dict):
        kind = value.get("type")
        text = value.get("text")
        if isinstance(text, str) and text.strip():
            return text, None
        inner = value.get("content")
        if isinstance(inner, str) and inner.strip():
            return inner, None
        dumped = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return dumped, (dumped if kind else None)

    return str(value), None


def norm_content(value: Any) -> str:
    """兼容旧调用：只要可读文本。"""
    return extract_content(value)[0]


def infer_sender(session: str, known_senders=()) -> str:
    """
    历史消息没有 sender 时，尽量推一个出来。

    优先级：
      1. 这个会话里只有一种真实发送者 → 就是他（最可靠）
      2. 没有真实值，但会话是私聊（FriendMessage）→ 尾号就是对方 ID
      3. 群聊 / 多人 / 推不出来 → 空，不猜

    这是**读取时**的推算，不写库：vault 只追加，索引重建后结果一致。
    """
    known = {str(s).strip() for s in (known_senders or ()) if str(s).strip()}
    if len(known) == 1:
        return next(iter(known))
    if known:
        return ""                      # 多人会话，猜错比不猜更糟

    parts = str(session or "").split(":")
    if len(parts) >= 3 and "FriendMessage" in parts[1]:
        tail = parts[-1].strip()
        if tail.isdigit():
            return tail
    return ""


def _part_blocks(content) -> list:
    """
    把 Message.content 里的内容块转成 extract_content 认得的 dict 列表。

    只留能变成文字的块：图片/音频不产生正文，直接跳过，
    也就不会把 base64 那坨东西搬进内存。
    带 _no_save 的块是宿主「只喂模型、不回存」的临时内容（含我们自己注入的记忆块），
    一律跳过——宿主保存历史时也是这么丢的。
    """
    if not isinstance(content, (list, tuple)):
        return []

    blocks = []
    for part in content:
        if isinstance(part, dict):
            if part.get("_no_save"):
                continue
            kind = part.get("type")
            if kind == "text":
                blocks.append({"type": "text", "text": str(part.get("text") or "")})
            elif kind in _SILENT_BLOCKS:
                thought = part.get("think") or part.get("reasoning") or ""
                blocks.append({"type": "think", "think": str(thought)})
            continue

        if getattr(part, "_no_save", False):
            continue
        kind = getattr(part, "type", None)
        if kind == "text":
            blocks.append({"type": "text", "text": str(getattr(part, "text", "") or "")})
        elif kind in _SILENT_BLOCKS:
            blocks.append({"type": "think",
                           "think": str(getattr(part, "think", "") or "")})
    return blocks


def message_text(message) -> str:
    """把一条 Message（或它 dump 出来的 dict）变成原始层要存的可读正文。"""
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = getattr(message, "content", None)

    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, (list, tuple)):
        blocks = _part_blocks(content)
        return extract_content(blocks)[0] if blocks else ""
    return extract_content(content)[0]


def message_fp(message) -> str:
    """一条 Message 的**基础**指纹（和 parse_contexts 那一侧完全同口径）。

    注意不要 strip：sync 存的是 parse_contexts 出来的原文，
    这里多剪一个空格，指纹就对不上，下一轮会把同一条消息再存一遍。
    """
    if isinstance(message, dict):
        role_raw = str(message.get("role") or "user")
    else:
        role_raw = str(getattr(message, "role", "user") or "user")
    role = _role_of(role_raw)
    text = message_text(message)
    if not text.strip():
        return ""
    return fp_of(f"{role}\x00{text}")


def split_turn(messages, context_count: int, last_fp: str = "",
               limit: int = 400) -> tuple[list, bool]:
    """
    从 run_context.messages 里切出「这一轮」。

    context_count：请求钩子那一刻 req.contexts 的长度（裁剪后真正发出去的历史）。
    last_fp：那条最后的历史消息的基础指纹，用来验边界——宿主可能插进
    checkpoint 之类的消息，边界一歪就会把整段历史重写一遍。

    返回 (这一轮的消息, 边界是否可信)。
    边界不信时调用方要用「去重模式」写，宁可这一轮少记重复，也不能重写历史。
    """
    if not isinstance(messages, (list, tuple)) or not messages:
        return [], False

    first = messages[0]
    first_role = (first.get("role") if isinstance(first, dict)
                  else getattr(first, "role", ""))
    offset = 1 if str(first_role or "").lower() == "system" else 0
    start = offset + max(0, int(context_count or 0))
    start = min(start, len(messages))

    if last_fp:
        boundary = start - 1
        ok = (0 <= boundary < len(messages)
              and message_fp(messages[boundary]) == last_fp)
        if not ok:
            return list(messages[-limit:]), False

    turn = list(messages[start:])
    if len(turn) > limit:
        turn = turn[-limit:]
    return turn, True


def turn_messages(messages, limit: int = 200) -> list[dict]:
    """
    把一轮结束时的 run_context.messages 尾部转成可归档条目。

    为什么需要它：AstrBot 的 req.contexts 只放**历史**，本轮用户消息在
    req.prompt + extra_user_content_parts 里，宿主是整轮跑完后才把
    「用户消息 + 工具消息 + 回复」一起回存。所以请求钩子永远看不到本轮内容。

    run_context.messages 就是宿主保存历史时用的同一份列表（system 在第 0 位），
    照它抄出来的正文和下一轮 contexts 里的完全一致，指纹能对上、顺序不会错。

    limit：只取尾部。前面是历史，索引里都有；尾部才是这一轮新产生的。
    """
    if not isinstance(messages, (list, tuple)) or not messages:
        return []

    tail = list(messages[-limit:]) if limit and limit > 0 else list(messages)
    out: list[dict] = []
    for index, message in enumerate(tail):
        role = _role_of(str(
            (message.get("role") if isinstance(message, dict)
             else getattr(message, "role", "")) or "user"))
        if role == "system":
            continue
        if isinstance(message, dict):
            if message.get("_no_save"):
                continue
        elif getattr(message, "_no_save", False):
            continue

        # 原样保留，不 strip：历史那条路（parse_contexts）存的就是原样，
        # 这里多剪一个空格，两边的指纹就对不上，下一轮会把同一条再存一遍
        text = message_text(message)
        if not text.strip() or _NO_SAVE_MARKERS.search(text):
            continue
        out.append({"idx": index, "role": role, "content": text, "raw": None})
    return out


def parse_reminder_time(text: str) -> datetime | None:
    """从消息正文里抠出 AstrBot 注入的「Current datetime」精确到分钟。"""
    match = _REMINDER_RE.search(text or "")
    if not match:
        return None
    year, month, day, hour, minute = (int(g) for g in match.groups())
    try:
        return datetime(year, month, day, hour, minute, tzinfo=TZ)
    except ValueError:
        return None


def _role_of(raw_role: str) -> str:
    role = (raw_role or "").strip().lower()
    if role in ("assistant", "ai", "bot", "model"):
        return "assistant"
    if role in ("system", "developer"):
        return "system"
    if role in ("tool", "function", "tool_result"):
        return "tool"
    return role or "user"


def parse_contexts(req: Any) -> tuple[list, list[dict]]:
    """
    返回 (原始列表, [{idx, role, content, raw}])。

    idx 保留原始下标，裁剪回写时按它取原对象，避免把宿主的消息格式改掉。
    """
    raw_list = getattr(req, "contexts", None)
    if not isinstance(raw_list, list):
        return [], []

    parsed = []
    for i, item in enumerate(raw_list):
        if isinstance(item, dict):
            role = _role_of(str(item.get("role") or "user"))
            content, raw = extract_content(item.get("content"))
        else:
            role = _role_of(str(getattr(item, "role", "user") or "user"))
            content, raw = extract_content(getattr(item, "content", None))

        if not content.strip():
            continue
        if _NO_SAVE_MARKERS.search(content):
            continue
        parsed.append({"idx": i, "role": role, "content": content, "raw": raw})
    return raw_list, parsed


def system_of(req: Any) -> str:
    sp = getattr(req, "system_prompt", None)
    return sp if isinstance(sp, str) else ""


def sync(vault, index, parsed: list[dict], persona: str,
         session: str = "", origin: str = "astrbot_context",
         ts_hint: datetime | None = None, force: bool = False,
         carry: datetime | None = None, sender: str = "") -> dict:
    """
    把原始层还没有的消息追加进去。

    force=True：整批照写，只按「这句话出现过没有」编号，不做整句跳过。
        用于一轮结束时的归档——她说过十次「晚安」，第十次也该留一条；
        历史重扫（force=False）才做跳过，否则每轮都会把整段历史再存一遍。

    ts_hint：本轮用户消息的真实时间（来自平台消息对象）。
    只有最后一条用得上——前面的历史我们不知道它当时几点发生，
    所以标 backfill=True，UI 会显示成「回填」而不是假装刚刚发生。

    carry：上一轮最后一句用户消息里的时间。逐轮归档时一轮里可能没有用户消息
    （纯工具轮），从这儿接上，时间才不会掉成"归档那一刻"。
    返回值里的 carry 是这一批处理完之后的游标，调用方存下来下一轮接着用。
    """
    if not parsed:
        return {"new": 0, "seen": 0, "carry": carry.isoformat(timespec="seconds")
                if carry else ""}

    bases = [fp_of(f"{m['role']}\x00{m['content']}") for m in parsed]
    known = index.has_many(list(dict.fromkeys(bases)))

    last = len(parsed) - 1
    # carry 从参数接上（上一轮最后一条用户消息的时间），循环里再往后推
    fresh = []
    for i, (msg, base) in enumerate(zip(parsed, bases)):
        found = parse_reminder_time(msg["content"])
        if found is not None:
            carry = found

        # 历史重扫：这句话以前出现过就整条跳过
        # 实时归档（force）：照写，重复的稍后由 assign_unique_fps 编号
        if not force and base in known:
            continue

        is_current = (i == last)
        if is_current and ts_hint is not None:
            ts, exact = ts_hint, True
        elif found is not None:
            ts, exact = found, True
        elif carry is not None:
            # 机器人的回复紧随用户，沿用那条用户消息的时间（分钟级准确）
            ts, exact = carry, True
        elif is_current:
            ts, exact = now(), True
        else:
            ts, exact = now(), False

        fresh.append({
            "fp": base,
            "ts": ts.isoformat(timespec="seconds"),
            "day": ts.strftime("%Y-%m-%d"),
            "role": msg["role"],
            "origin": origin,
            "session": session or None,
            "text": msg["content"],
            "raw": msg.get("raw"),
            "tokens": estimate_tokens(msg["content"]),
            "persona": persona or None,
            # 发送者只对"用户说的话"有意义；历史补录时拿不到，留空
            "sender": (sender or None) if msg["role"] == "user" else None,
            "backfill": not exact,
        })

    # 重复的同一句话：base 被占用了就编成 base-2、base-3……
    # 既保留重复，又不影响下一次按 base 去重
    renamed = assign_unique_fps(index, fresh, known)

    written = vault.append(fresh) if fresh else 0
    added = index.add(fresh) if fresh else 0
    return {"new": written, "indexed": added, "seen": len(parsed),
            "duplicate": len(parsed) - len(fresh), "renamed": renamed,
            "carry": carry.isoformat(timespec="seconds") if carry else ""}


def sync_message(vault, index, role: str, text: str, persona: str = "",
                 session: str = "", origin: str = "response_hook",
                 ts: datetime | None = None, backfill: bool = False) -> bool:
    """
    单独追加一条（用于 assistant 回复的即时入库、以及历史导入）。

    ts：从来源里还原出的真实时间；导入的历史拿不到时间时传 None，
    并由调用方传 backfill=True 让面板如实标成「回填」。
    实时回复这条路径不传 ts，时间就是现在，所以 backfill 保持 False。
    """
    text = norm_content(text).strip()
    if not text:
        return False
    fp = fp_of(f"{role}\x00{text}")
    if index.has(fp):
        return False

    stamp = ts or now()
    event = {
        "fp": fp,
        "ts": stamp.isoformat(timespec="seconds"),
        "day": stamp.strftime("%Y-%m-%d"),
        "role": role,
        "origin": origin,
        "session": session or None,
        "text": text,
        "raw": None,
        "tokens": estimate_tokens(text),
        "persona": persona or None,
        "backfill": bool(backfill),
    }
    vault.append([event])
    index.add([event])
    return True
