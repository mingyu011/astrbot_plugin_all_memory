"""
synonyms.py —— 按 id 维护的同义词分组（不用模型）

只解决“累/疲惫/撑不住”这种常见改写；私人称呼放 entities.py。
"""

from __future__ import annotations

import json
import os
import threading

from .ids import safe_component

DEFAULT_GROUPS = [
    {"canonical": "累", "terms": ["疲惫", "好累", "撑不住", "没力气"]},
    {"canonical": "怕", "terms": ["害怕", "恐惧", "不敢", "怕怕"]},
    {"canonical": "喜欢", "terms": ["爱", "偏爱", "中意", "舍不得"]},
    {"canonical": "讨厌", "terms": ["不喜欢", "烦", "受不了", "反感"]},
    {"canonical": "开心", "terms": ["高兴", "快乐", "乐呵", "心情好"]},
    {"canonical": "难过", "terms": ["伤心", "委屈", "低落", "不开心"]},
    {"canonical": "生气", "terms": ["愤怒", "恼火", "火大", "不爽"]},
    {"canonical": "睡觉", "terms": ["睡了", "入睡", "休息", "晚安"]},
    {"canonical": "吃饭", "terms": ["吃东西", "用餐", "干饭", "吃饭饭"]},
]


class SynonymStore:
    """一个 id 一份 synonyms/<uid>.json。"""

    def __init__(self, root: str, uid: str = "default",
                 with_defaults: bool = True):
        self.root = root
        self.uid = safe_component(uid, fallback="default")
        self.dir = os.path.join(root, "synonyms")
        self.path = os.path.join(self.dir, self.uid + ".json")
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()
        self._data = self._load(with_defaults)

    def _load(self, with_defaults: bool) -> dict:
        existed = os.path.exists(self.path)
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            data = {}
        groups = data.get("groups") if isinstance(data, dict) else None
        if not isinstance(groups, list):
            groups = []
        cleaned = []
        for item in groups:
            if not isinstance(item, dict):
                continue
            canonical = str(item.get("canonical") or "").strip()[:32]
            if not canonical:
                continue
            terms = []
            for term in item.get("terms") or []:
                term = str(term or "").strip()[:32]
                if term and term != canonical and term not in terms:
                    terms.append(term)
            cleaned.append({"canonical": canonical, "terms": terms})
        if with_defaults and not existed:
            known = {item["canonical"] for item in cleaned}
            for item in DEFAULT_GROUPS:
                if item["canonical"] not in known:
                    cleaned.append(dict(item, terms=list(item["terms"])))
        return {"groups": cleaned}

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
            return [dict(item, terms=list(item.get("terms") or []))
                    for item in self._data.get("groups", [])]

    def add(self, canonical: str, terms=None) -> dict:
        canonical = str(canonical or "").strip()[:32]
        if not canonical:
            raise ValueError("同义词主词不能为空")
        values = [str(x or "").strip()[:32] for x in (terms or [])]
        values = [x for x in dict.fromkeys(values)
                  if x and x != canonical]
        with self._lock:
            groups = self._data.setdefault("groups", [])
            target = next((x for x in groups
                           if x.get("canonical") == canonical), None)
            if target is None:
                target = {"canonical": canonical, "terms": []}
                groups.append(target)
            for term in values:
                if term not in target["terms"]:
                    target["terms"].append(term)
            self._save()
            return dict(target, terms=list(target["terms"]))

    def remove(self, canonical: str) -> bool:
        canonical = str(canonical or "").strip()
        with self._lock:
            groups = self._data.get("groups", [])
            kept = [x for x in groups if x.get("canonical") != canonical]
            if len(kept) == len(groups):
                return False
            self._data["groups"] = kept
            self._save()
            return True

    def expand(self, text: str, limit: int = 12) -> list[str]:
        body = str(text or "")
        out: list[str] = []
        for group in self.all():
            canonical = str(group.get("canonical") or "")
            terms = [str(x) for x in group.get("terms") or []]
            if not canonical:
                continue
            if canonical in body or any(term in body for term in terms):
                for value in [canonical, *terms]:
                    if value and value not in out:
                        out.append(value)
            if len(out) >= limit:
                break
        return out[:limit]
