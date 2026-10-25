"""
budget.py —— 上下文预算与滞回裁剪

预算模型（窗口 = 1）
-------------------
    裁剪后整个请求 ≈ total_ratio（默认 0.40）
    里面装：system + facts + 记忆地图 + 检索 + 历史n轮

    历史n轮 = total_ratio − system − facts − 地图 − 检索

    也就是说历史是「剩下多少算多少」，而 facts 是唯一会随相处时间增长的部分。

    facts 预算是**固定**的：窗口的 10%（facts_ratio_for() 直接返回上限，
    不随裁剪次数爬坡）。早期库小、抽不满，历史自然还能多留一些；
    聊久了 facts 攒满 10%，历史就让位。

滞回（hysteresis）
-----------------
    平时：压力 < 触发线  →  一个字节都不改
    压力 ≥ 触发线        →  裁一次，按上面的分配重建
    然后：起点固定，让历史自己长回触发线

如果每轮都重算裁剪点，每轮前缀都不一样，宿主的提示词缓存几乎必然失效。
两次裁剪之间保持完全不动，缓存才能一路命中。
"""

from __future__ import annotations

import os
import json

from .ids import safe_component
from .tokens import estimate_tokens

# 预算默认值（都是占窗口的比例）
DEFAULTS = {
    "trigger_ratio": 0.80,   # 触发裁剪
    "total_ratio": 0.40,     # 裁剪后整个请求的目标大小
    "facts_max": 0.10,       # facts 预算（固定，不随裁剪次数增长）
    "map_ratio": 0.03,       # 记忆地图（固定）
    "recall_ratio": 0.05,    # 检索片段（固定）
    "summary_ratio": 0.05,   # 前情纪要（固定，从历史里扣）
}


def facts_ratio_for(trim_count: int) -> float:
    """
    facts 预算：固定窗口的 10%，跟裁剪了多少次无关。

    trim_count 只为保持调用签名（老代码按次数传参），返回值不随它变。
    早期库小、抽不满这个额度，历史自然还能多留一些。
    """
    return DEFAULTS["facts_max"]


def block_budget(window: int, fraction: float, ratio: float) -> int:
    """把「窗口 × 比例」换算成未校准的原始估算预算，供注入块使用。"""
    safe_ratio = max(ratio, 0.5)
    return max(0, int(window * max(0.0, fraction) / safe_ratio))


def total_request_tokens(system: int, history: int, payload: int) -> int:
    """
    一个请求的真实总量：主体（system + 历史原文）+ 注入块。

    payload 内部本来就是 facts + 记忆地图 + 检索拼起来的一整块，
    所以那三块**不能再加一遍** —— 加了会让面板上的「合计」多算一个注入块，
    拿它去调 ratio 就会被带偏。
    """
    return max(0, int(system or 0)) + max(0, int(history or 0)) \
        + max(0, int(payload or 0))


def takeover_mode(state: dict) -> str:
    """
    这个会话现在处于哪种接管状态。

    auto：还没接管，第一次裁剪时自动开（默认）
    on  ：已经接管，每轮注入记忆（手动开或首次裁剪自动开）
    off ：手动关闭，不裁剪、不注入，交回 AstrBot 原生上下文
    """
    data = state or {}
    if str(data.get("takeover_manual") or "") == "off":
        return "off"
    if data.get("trimmed_mode"):
        return "on"
    return "auto"


class Plan:
    """一次请求的预算决策结果。"""

    __slots__ = ("applied", "before", "pressure", "kept", "after",
                 "budgets", "dropped", "window", "facts_ratio")

    def __init__(self, applied: bool, before: int = 0, pressure: float = 0.0,
                 kept: list | None = None, after: int = 0,
                 budgets: dict | None = None, dropped: int = 0,
                 window: int = 0, facts_ratio: float = 0.0):
        self.applied = applied
        self.before = before
        self.pressure = pressure
        self.kept = kept or []
        self.after = after
        self.budgets = budgets or {}
        self.dropped = dropped
        self.window = window
        self.facts_ratio = facts_ratio

    def summary(self) -> str:
        if not self.applied:
            return f"压力 {self.pressure * 100:.1f}%，未触发裁剪"
        return (f"压力 {self.pressure * 100:.1f}% → 裁剪后 "
                f"{self.after} tok，保留 {len(self.kept)} 条，"
                f"丢弃 {self.dropped} 条，facts 预算 {self.facts_ratio * 100:.1f}%")


def decide(parsed: list[dict], system_tokens: int, window: int,
           calibrator, trigger_ratio: float = 0.80,
           total_ratio: float = 0.40,
           facts_ratio: float = 0.01,
           map_ratio: float = 0.03,
           recall_ratio: float = 0.05,
           summary_ratio: float = 0.05,
           trusted_tokens: int = 0) -> Plan:
    """
    决定这一轮要不要裁剪，以及裁到什么程度。

    parsed         : [{idx, role, content}]，按原始顺序
    system_tokens  : system prompt 的原始估算（插件不改它，但算进压力和预算）
    window         : 真实上下文窗口
    trusted_tokens : AstrBot 上一次请求的真实 token 总量；>0 时优先用它判断压力，
                     不再用字符估算。这是「按宿主实际注入量算 80%」的入口。
    facts_ratio    : 本轮 facts 预算（由裁剪次数决定）
    summary_ratio  : 前情纪要预算。它是叙事层，和 facts 各管一头：
                     纪要讲"发生了什么"，人物卡讲"她是谁"。
    """
    if window <= 0:
        return Plan(applied=False)

    ratio = getattr(calibrator, "ratio", 1.0) or 1.0
    safe_ratio = max(ratio, 0.5)

    raw_history = sum(estimate_tokens(m["content"]) for m in parsed)
    raw_total = raw_history + system_tokens
    if trusted_tokens and trusted_tokens > 0:
        measured = int(trusted_tokens)
    else:
        measured = calibrator.apply(raw_total)
    pressure = measured / window

    if pressure < trigger_ratio:
        return Plan(applied=False, before=measured, pressure=pressure,
                    window=window, facts_ratio=facts_ratio)

    # 全部换算回「未校准的原始估算」单位，和 estimate_tokens 对齐
    def raw_of(fraction: float) -> int:
        return max(0, int(window * fraction / safe_ratio))

    total_budget = raw_of(total_ratio)
    facts_budget = raw_of(facts_ratio)
    map_budget = raw_of(map_ratio)
    recall_budget = raw_of(recall_ratio)
    summary_budget = raw_of(summary_ratio)

    # 历史拿剩下的。system 是硬开销，先扣掉。
    history_budget = max(
        0, total_budget - system_tokens - facts_budget - map_budget
        - recall_budget - summary_budget)

    kept: list[dict] = []
    used = 0
    for msg in reversed(parsed):
        cost = estimate_tokens(msg["content"]) + 4
        if used + cost > history_budget:
            break
        kept.append(msg)
        used += cost
    kept.reverse()

    composed_raw = used + system_tokens + facts_budget + map_budget \
        + recall_budget + summary_budget

    return Plan(
        applied=True,
        before=measured,
        pressure=pressure,
        kept=kept,
        after=calibrator.apply(composed_raw),
        budgets={
            "total": total_budget,
            "history": history_budget,
            "facts": facts_budget,
            "map": map_budget,
            "recall": recall_budget,
            "summary": summary_budget,
        },
        dropped=len(parsed) - len(kept),
        window=window,
        facts_ratio=facts_ratio,
    )


class StateStore:
    """每个 id 一份轻量状态：校准比例、裁剪次数、上次用量。"""

    def __init__(self, root: str):
        self.dir = os.path.join(root, "state")
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, uid: str) -> str:
        safe = safe_component(uid, fallback="default")
        return os.path.join(self.dir, safe, "state.json")

    def load(self, uid: str) -> dict:
        path = self._path(uid)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, uid: str, data: dict) -> None:
        path = self._path(uid)
        tmp = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError:
            pass

    def all_states(self) -> dict[str, dict]:
        """读全部 id 状态，供 WebUI 展示用量。"""
        out: dict[str, dict] = {}
        if not os.path.isdir(self.dir):
            return out
        for name in os.listdir(self.dir):
            path = os.path.join(self.dir, name)
            if not os.path.isdir(path):
                continue
            try:
                with open(os.path.join(path, "state.json"),
                          "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                out[name] = data
        return out
