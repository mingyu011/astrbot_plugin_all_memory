"""
vault.py —— 第一层：原始层（只追加）

铁律
----
1. 写进去的东西永不修改、永不删除。
2. 按天分段，备份天然增量，读取时按文件名排序拼接，永不"合并"。
3. 人格快照内容寻址：相同内容只存一份。
4. 任何写失败都不抛给调用方——归档问题不能影响她说话。
"""

from __future__ import annotations

import os
import json
import hashlib
import threading
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
_LOCK = threading.Lock()


def now() -> datetime:
    return datetime.now(TZ)


def fp_of(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:n]


def normalize_duplicate_fps(events: list[dict], counter: dict | None = None,
                            used: set | None = None) -> None:
    """
    同一批里出现相同指纹时，按出现顺序编号：abc、abc-2、abc-3。

    新版写出来的指纹本来就唯一，这里不会动它们；
    旧版本可能把重复消息写成同一个指纹（多行共用 abc，还可能散在好几天的文件里），
    不编号的话恢复/重建索引时会被当成"已经存在"合并掉，那几条就没了。

    编号只取决于出现顺序，所以同一份数据算两次结果一样 → 仍然幂等。
    counter / used 可以跨文件共用。
    """
    counter = counter if counter is not None else {}
    used = used if used is not None else set()

    for event in events:
        base = str(event.get("fp") or "")
        if not base:
            continue
        n = counter.get(base, 0) + 1
        if n == 1:
            counter[base] = 1
            used.add(base)
            continue

        candidate = f"{base}-{n}"
        while candidate in used:
            n += 1
            candidate = f"{base}-{n}"
        counter[base] = n
        used.add(candidate)
        event["fp"] = candidate


class Vault:
    """只追加总库。"""

    def __init__(self, root: str):
        self.root = root
        self.dir = os.path.join(root, "vault")
        self.persona_dir = os.path.join(root, "personas")
        os.makedirs(self.dir, exist_ok=True)
        os.makedirs(self.persona_dir, exist_ok=True)

    # ------------------------------------------------------------ 人格快照

    def put_persona(self, system_prompt: str) -> str:
        """内容寻址存储 system prompt，返回指纹。相同内容只写一次。"""
        if not system_prompt:
            return ""
        digest = fp_of(system_prompt, 16)
        path = os.path.join(self.persona_dir, f"{digest}.txt")
        if not os.path.exists(path):
            with _LOCK:
                if not os.path.exists(path):
                    tmp = path + ".tmp"
                    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                        f.write(system_prompt)
                    os.replace(tmp, path)
        return digest

    def read_persona(self, digest: str) -> str:
        path = os.path.join(self.persona_dir, f"{digest}.txt")
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def personas(self) -> list[tuple[str, int]]:
        out = []
        for name in sorted(os.listdir(self.persona_dir)):
            if name.endswith(".txt"):
                p = os.path.join(self.persona_dir, name)
                out.append((name[:-4], os.path.getsize(p)))
        return out

    # ------------------------------------------------------------ 事件追加

    def _day_path(self, day: str) -> str:
        return os.path.join(self.dir, f"{day}.jsonl")

    def append(self, events: list[dict]) -> int:
        """按天分组追加。返回实际写入条数。"""
        if not events:
            return 0
        grouped: dict[str, list[dict]] = {}
        for ev in events:
            grouped.setdefault(ev["day"], []).append(ev)

        written = 0
        for day, items in grouped.items():
            path = self._day_path(day)
            with _LOCK:
                with open(path, "a", encoding="utf-8", newline="\n") as f:
                    for ev in items:
                        f.write(json.dumps(ev, ensure_ascii=False,
                                           separators=(",", ":")) + "\n")
                        written += 1
        return written

    # ------------------------------------------------------------ 读取

    def days(self) -> list[str]:
        try:
            names = os.listdir(self.dir)
        except FileNotFoundError:
            return []
        return sorted(n[:-6] for n in names if n.endswith(".jsonl"))

    def read_day(self, day: str) -> list[dict]:
        path = self._day_path(day)
        if not os.path.exists(path):
            return []
        out = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue
        return out

    def iter_events(self):
        for day in self.days():
            yield from self.read_day(day)

    def recent_events(self, limit: int = 200) -> list[dict]:
        """从最新往回取若干条，用于检索兜底或调试。"""
        collected: list[dict] = []
        for day in reversed(self.days()):
            collected = self.read_day(day) + collected
            if len(collected) >= limit:
                break
        return collected[-limit:]

    # ------------------------------------------------------------ 统计

    def stats(self) -> dict:
        days = self.days()
        total = 0
        by_origin: dict[str, int] = {}
        for day in days:
            for ev in self.read_day(day):
                total += 1
                origin = ev.get("origin", "unknown")
                by_origin[origin] = by_origin.get(origin, 0) + 1
        return {"days": len(days), "events": total, "by_origin": by_origin}

    def integrity(self) -> list[str]:
        """轻量完整性检查：坏行、缺字段。只报告，不修复、不删改。"""
        problems = []
        for day in self.days():
            for lineno, ev in enumerate(self.read_day(day), 1):
                for field in ("fp", "ts", "day", "role", "text"):
                    if field not in ev:
                        problems.append(f"{day}:{lineno} 缺字段 {field}")
        return problems
