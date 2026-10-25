"""
inject.py —— 组装并注入「记忆块」

三条铁律
--------
1. 记忆块只放 extra_user_content_parts，绝不碰 system_prompt。
   system_prompt 一变，宿主的提示词缓存全废，价格能翻 7–20 倍。
2. 记忆块放在本轮用户消息之后，所以前缀不动，缓存照常命中。
3. 每个块都带 _no_save 标记，宿主回存历史时会跳过它，不会逐轮累积副本。
"""

from __future__ import annotations

from .tokens import estimate_tokens

NO_SAVE_MARKER = "<!-- _no_save -->"

SUMMARY_HEADER = "【前情纪要】"
FACTS_HEADER = "【你已经知道的关于她的事】"
MAP_HEADER = "【记忆地图】"
RECALL_HEADER = "【相关记忆片段】"

# 纪要一定要自报"这是大概"，否则模型会拿它当原文引用。
SUMMARY_FOOTER = ("（这是更早对话的压缩纪要，可能有偏差；"
                  "要引用具体内容，先用记忆检索核对原文。）")

USAGE_HINT = (
    "以上是本地长期记忆。引用时贴近原意并注意时间。\n"
    "如果这里没找到你要的东西，别猜——用 search_memory 搜关键词，"
    "再用 read_memory 读完整原文；想不起是哪天，用 memory_timeline 看时间线。\n"
    "问到「第一次见面」「最早」「以前」这类，先看记忆地图/时间线找到最早那天，"
    "再用 read_memory(day=「YYYY-MM-DD」) 按天读出当时的原话，别凭印象编。\n"
    "问到「那天几点做了什么」这类，读那天的对话：条数多时用 offset 翻页，一直读到找到为止。\n"
    "一次没搜到就换个说法再搜一次，最多试两三次；确实查不到就说不记得。"
)


def select_facts(facts: list[dict], budget: int) -> list[dict]:
    """
    事实超过预算时的完整选择策略。

    核心事实（identity / relationship / preference）优先保留，
    事件型事实按调用方已经排好的相关度顺序补；不再“超了就直接取最近”。
    """
    items = [f for f in (facts or []) if str(f.get("claim") or "").strip()]
    if not items or budget <= 0:
        return []
    total = estimate_tokens(FACTS_HEADER) + sum(
        estimate_tokens(f"- {str(f.get('claim', '')).strip()}") + 2
        for f in items)
    if total <= budget:
        return items

    from .extract import kind_weight, normalize_kind
    core = [f for f in items
            if normalize_kind(f.get("kind")) in
            ("identity", "relationship", "preference")]
    core.sort(key=lambda f: (kind_weight(f.get("kind")),
                             str(f.get("extracted_at", ""))), reverse=True)
    rest = [f for f in items if f not in core]

    selected: list[dict] = []
    used = estimate_tokens(FACTS_HEADER)
    seen: set[str] = set()
    for fact in core + rest:
        key = str(fact.get("id") or fact.get("claim") or "")
        if key in seen:
            continue
        claim = str(fact.get("claim", "")).strip().replace("\n", " ")
        if not claim:
            continue
        cost = estimate_tokens(f"- {claim}") + 2
        if used + cost > budget:
            continue
        seen.add(key)
        selected.append(fact)
        used += cost
    return selected


def build_summary_block(text: str, budget: int) -> str:
    """
    前情纪要。排在整个记忆块最前面——先给"前情"，再给"她是谁"，最后给细节。

    纪要只追加，文件会越来越长；这里**从最新往回装**，装不下就丢最旧的，
    保证模型看到的是最近发生了什么（顺序仍然是时间正序）。
    """
    body = str(text or "").strip()
    if not body or budget <= 0:
        return ""

    used = estimate_tokens(SUMMARY_HEADER) + estimate_tokens(SUMMARY_FOOTER)
    picked: list[str] = []
    for line in reversed(body.splitlines()):
        line = line.strip()
        if not line:
            continue
        cost = estimate_tokens(line)
        if used + cost > budget:
            break
        picked.append(line)
        used += cost

    if not picked:
        return ""
    picked.reverse()
    lines = [SUMMARY_HEADER] + picked + [SUMMARY_FOOTER]
    return "\n".join(lines)


def build_facts_block(facts: list[dict], budget: int) -> str:
    """
    facts: [{claim, source_fp, extracted_at, ...}]
    常驻块——每轮都带，这是「聪明」的主要来源。

    超过预算时按「最近」取：新的优先，旧事实让位（原文仍在 vault 里）。
    """
    if not facts or budget <= 0:
        return ""

    items = list(facts)
    total = estimate_tokens(FACTS_HEADER) + sum(
        estimate_tokens(f"- {str(f.get('claim', '')).strip()}") + 2
        for f in items if str(f.get("claim", "")).strip())
    if total > budget:
        items.sort(key=lambda f: (
            str(f.get("extracted_at") or f.get("updated_at") or ""),
            str(f.get("id") or "")), reverse=True)

    lines = [FACTS_HEADER]
    used = estimate_tokens(FACTS_HEADER)
    for fact in items:
        claim = str(fact.get("claim", "")).strip().replace("\n", " ")
        if not claim:
            continue
        line = f"- {claim}"
        cost = estimate_tokens(line)
        if used + cost > budget:
            break
        lines.append(line)
        used += cost
    return "\n".join(lines) if len(lines) > 1 else ""


def build_map_block(day_summary: list[dict], budget: int,
                    detail_days: int = 30) -> str:
    """
    记忆地图：从第一天到现在，**全部历史都在**，只是越久越粗。

      最近 detail_days 天 → 按天列，带片段
      更早的            → 按月份汇总

    装不下时逐级聚合，而不是砍掉早期记忆。
    """
    if not day_summary or budget <= 0:
        return ""

    rows = sorted(day_summary, key=lambda r: str(r.get("day", "")), reverse=True)
    if not rows:
        return ""

    first_day = rows[-1].get("day", "")
    last_day = rows[0].get("day", "")
    total_days = len(rows)
    total_events = sum(int(r.get("count", 0)) for r in rows)

    header = (f"【记忆地图】共 {total_days} 天 / {total_events} 条，"
              f"{first_day} 至今")

    # 先按天，装不下再退到按月
    day_lines = _map_day_lines(rows, int(detail_days or 30))
    block = _fit(header, day_lines, budget)
    if block:
        return block

    month_lines = _map_month_lines(rows)
    block = _fit(header, month_lines, budget)
    if block:
        return block

    # 还装不下就只给月份区间
    return _fit(header, [f"- {first_day} ~ {last_day}：共 {total_events} 条"],
                budget) or header


def _map_day_lines(rows: list[dict], detail_days: int) -> list[tuple[str, int]]:
    """
    最近 N 天按天列；更早的**按月汇总**列出来，保证早期记忆也在图上。
    """
    detail_days = max(1, int(detail_days or 30))
    detail = rows[:detail_days]
    older = rows[detail_days:]

    lines: list[tuple[str, int]] = []
    for row in detail:
        day = str(row.get("day", ""))
        count = int(row.get("count", 0))
        samples = [s for s in row.get("samples", []) if s]
        line = f"- {day}：{count} 条"
        if samples:
            line += "｜" + " / ".join(s[:20] for s in samples[:3])
        lines.append((line, 0))

    if older:
        months: dict[str, int] = {}
        for row in older:
            month = str(row.get("day", ""))[:7]
            if len(month) == 7:
                months[month] = months.get(month, 0) + int(row.get("count", 0))
        ordered = sorted(months.items(), reverse=True)
        shown = ordered[:6]
        text = " / ".join(f"{m}：{c} 条" for m, c in shown)
        if len(ordered) > len(shown):
            text += f" / 其余 {len(ordered) - len(shown)} 个月"
        if text:
            lines.append((f"- 更早（按月）：{text}", 0))
    return lines


def _map_month_lines(rows: list[dict]) -> list[tuple[str, int]]:
    """按月聚合，覆盖全部历史。"""
    buckets: dict[str, int] = {}
    for row in rows:
        month = str(row.get("day", ""))[:7]
        if len(month) == 7:
            buckets[month] = buckets.get(month, 0) + int(row.get("count", 0))
    return [(f"- {month}：{count} 条", 0)
            for month, count in sorted(buckets.items(), reverse=True)]


def _fit(header: str, lines: list[tuple[str, int]], budget: int) -> str:
    """把行塞进预算；一行都塞不下就返回空，交给上层降级。"""
    out = [header]
    used = estimate_tokens(header) + estimate_tokens("需要细节时按时间或关键词检索。")
    for line, _ in lines:
        cost = estimate_tokens(line)
        if used + cost > budget:
            break
        out.append(line)
        used += cost
    if len(out) <= 1:
        return ""
    out.append("需要细节时按时间或关键词检索。")
    return "\n".join(out)


def build_recall_block(hits: list[dict], budget: int,
                       max_chars: int = 800) -> str:
    """检索片段：按相关度排序，全部带时间戳，**装满预算为止**。"""
    if not hits or budget <= 0:
        return ""

    lines = [RECALL_HEADER]
    used = estimate_tokens(RECALL_HEADER)
    for hit in hits:
        text = " ".join(str(hit.get("text", "")).split())
        if not text:
            continue
        if len(text) > max_chars:
            text = text[:max_chars] + "…"
        role = hit.get("role", "")
        prefix = "【上下文】" if hit.get("is_context") else ""
        line = f"- {prefix}[{hit.get('ts', '')}] {role}：{text}"
        cost = estimate_tokens(line)
        if used + cost > budget:
            break
        lines.append(line)
        used += cost

    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def drop_known(hits: list[dict], skip_fps: set) -> list[dict]:
    """
    剔除已经在本轮上下文里的条目。

    最近 N 轮是原样发过去的，如果检索又把它们捞回来，就是纯浪费预算，
    还会让同一句话在上下文里出现两遍。这里按内容指纹去重。

    注意比的是「基础指纹」：同一句话重复说过时，库里存的是 abc、abc-2、abc-3，
    而上下文里算出来的是 abc —— 只看前缀就能把它们全剔掉。
    """
    if not skip_fps:
        return list(hits)
    from .sync import base_fp

    skip = {base_fp(fp) for fp in skip_fps}
    return [hit for hit in hits
            if base_fp(str(hit.get("fp") or "")) not in skip]


def compose(blocks: list[str], with_hint: bool = True) -> str:
    """把若干块拼成最终 payload，并盖上 _no_save 戳。"""
    usable = [b for b in blocks if b and b.strip()]
    if not usable:
        return ""
    parts = list(usable)
    if with_hint:
        parts.append(USAGE_HINT)
    parts.append(NO_SAVE_MARKER)
    return "\n\n".join(parts)


def attach(req, payload: str) -> bool:
    """
    把 payload 挂到本轮请求上。返回是否成功。

    优先用 extra_user_content_parts（放在用户消息之后，不破坏前缀）。
    拿不到 TextPart 时返回 False，由调用方降级处理——绝不把内容拼进
    system_prompt 或用户消息正文里。
    """
    if not payload:
        return False

    try:
        from astrbot.core.agent.message import TextPart
    except Exception:
        return False

    parts = getattr(req, "extra_user_content_parts", None)
    if parts is None:
        return False

    try:
        part = TextPart(text=payload)
        # 临时内容：只参与本轮，不写入会话历史
        mark = getattr(part, "mark_as_temp", None)
        if callable(mark):
            mark()
        parts.append(part)
        return True
    except Exception:
        return False
