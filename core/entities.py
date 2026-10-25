"""
entities.py —— 按 id 维护的实体/别名词典（不用模型）

目的：解决“大肥鱼小木 = 小木”“B 站 = bilibili”这类私人称呼。
只存名字、类型、别名；不存向量，也不自动收代词。
"""

from __future__ import annotations

import json
import os
import re
import threading

from .ids import safe_component

PRONOUNS = {"我", "你", "他", "她", "它", "我们", "你们", "他们",
            "这个", "那个", "这里", "那里", "自己", "别人"}


class EntityStore:
    """一个 id 一份 entities/<uid>.json。"""

    def __init__(self, root: str, uid: str = "default"):
        self.root = root
        self.uid = safe_component(uid, fallback="default")
        self.dir = os.path.join(root, "entities")
        self.path = os.path.join(self.dir, self.uid + ".json")
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            data = {}
        items = data.get("entities") if isinstance(data, dict) else None
        if not isinstance(items, list):
            items = []
        cleaned = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("canonical") or "").strip()
            if not name or name in PRONOUNS:
                continue
            aliases = []
            for alias in item.get("aliases") or []:
                alias = str(alias or "").strip()
                if alias and alias != name and alias not in PRONOUNS \
                        and alias not in aliases:
                    aliases.append(alias[:32])
            cleaned.append({
                "canonical": name[:32],
                "type": str(item.get("type") or "thing")[:24],
                "aliases": aliases,
            })
        return {"entities": cleaned}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(item, aliases=list(item.get("aliases") or []))
                    for item in self._data.get("entities", [])]

    def add(self, canonical: str, aliases=None, kind: str = "thing") -> dict:
        name = str(canonical or "").strip()[:32]
        if not name or name in PRONOUNS:
            raise ValueError("实体名不能为空，也不能是代词")
        aliases = [str(a or "").strip()[:32] for a in (aliases or [])]
        aliases = [a for a in dict.fromkeys(aliases)
                   if a and a != name and a not in PRONOUNS]
        with self._lock:
            items = self._data.setdefault("entities", [])
            target = next((x for x in items if x.get("canonical") == name), None)
            if target is None:
                target = {"canonical": name, "type": kind or "thing",
                          "aliases": []}
                items.append(target)
            target["type"] = str(kind or target.get("type") or "thing")[:24]
            for alias in aliases:
                if alias not in target["aliases"]:
                    target["aliases"].append(alias)
            self._save()
            return dict(target, aliases=list(target["aliases"]))

    def remove(self, canonical: str) -> bool:
        name = str(canonical or "").strip()
        with self._lock:
            items = self._data.get("entities", [])
            kept = [x for x in items if x.get("canonical") != name]
            if len(kept) == len(items):
                return False
            self._data["entities"] = kept
            self._save()
            return True

    def expand(self, text: str, limit: int = 12) -> list[str]:
        """问题里出现别名/规范名时，补出同义词，交给检索词一起查。"""
        body = str(text or "")
        out: list[str] = []
        for item in self.all():
            name = str(item.get("canonical") or "")
            aliases = [str(a) for a in item.get("aliases") or []]
            if not name:
                continue
            if name in body or any(alias in body for alias in aliases):
                for value in [name, *aliases]:
                    if value and value not in out:
                        out.append(value)
            if len(out) >= limit:
                break
        return out[:limit]

    @staticmethod
    def suggest(facts: list[dict], limit: int = 30) -> list[str]:
        """从事实 claim 里抽“叫/名字是/昵称是”的候选，只给人看，不落库。"""
        pattern = re.compile(
            r"(?:叫|叫做|名字是|名叫|昵称是|称呼为)\s*[“\"']?"
            r"([A-Za-z0-9_\u4e00-\u9fff]{2,12})")
        out: list[str] = []
        for fact in facts or []:
            claim = str(fact.get("claim") or "")
            for match in pattern.finditer(claim):
                name = match.group(1).strip()
                if name and name not in PRONOUNS and name not in out:
                    out.append(name)
                if len(out) >= limit:
                    return out
        return out
