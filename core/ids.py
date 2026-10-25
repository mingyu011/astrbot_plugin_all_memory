"""
ids.py —— 会话 → 可读的 id 文件夹名

一个私聊会话绑定一个 id。文件夹名优先用平台能读到的用户 ID（QQ 号等），
读不到就用账户名称，再没有才退回安全会话名。

同一个 session 永远映射到同一个文件夹：映射存在 state/_sessions.json，
避免历史补录时猜出来的名字和之后拿到的 ID 不一致，把同一个人拆成两个文件夹。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading

# Windows 不允许出现在文件名里的字符；顺手挡掉控制字符
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_component(value, fallback: str = "unknown", limit: int = 80) -> str:
    """把任意文本变成可以安全当文件夹名的片段。"""
    text = str(value or "").strip()
    text = _INVALID.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = fallback
    if text.upper() in _RESERVED:
        text = f"_{text}"
    text = text[:limit].strip(" .")
    return text or fallback


def infer_private_id(session: str) -> str:
    """私聊会话尾号通常就是对方 ID（aiocqhttp:FriendMessage:123456）。"""
    parts = str(session or "").split(":")
    if len(parts) >= 3 and "FriendMessage" in parts[1]:
        tail = parts[-1].strip()
        if tail:
            return tail
    return ""


def is_private_session(session: str) -> bool:
    """0.9.17 只做私聊；显式带 group 的会话一律跳过。"""
    return "group" not in str(session or "").lower()


def _digest(text: str, n: int = 6) -> str:
    return hashlib.sha1(str(text or "").encode("utf-8", "ignore")).hexdigest()[:n]


class SessionIds:
    """session -> id 文件夹名的持久映射。"""

    def __init__(self, root: str):
        self.root = root
        self.dir = os.path.join(root, "state")
        self.path = os.path.join(self.dir, "_sessions.json")
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()
        self._map = self._load()
        self._used = set(self._map.values())

    def _load(self) -> dict[str, str]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): safe_component(v) for k, v in data.items() if k and v}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(self._map, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def all(self) -> dict[str, str]:
        with self._lock:
            return dict(self._map)

    @staticmethod
    def is_private(session: str) -> bool:
        return is_private_session(session)

    def resolve(self, session: str, sender_id: str = "",
                sender_name: str = "") -> str:
        """拿到这个会话的文件夹名；第一次见到时按 ID → 名称 → 会话尾号 → 会话名选。"""
        session = str(session or "default")
        with self._lock:
            existing = self._map.get(session)
            if existing:
                return existing

            candidate = (str(sender_id or "").strip()
                         or str(sender_name or "").strip()
                         or infer_private_id(session)
                         or session)
            uid = safe_component(candidate, fallback=f"session-{_digest(session)}")
            if uid in self._used:
                # 同名账户/不同会话撞名：加会话摘要，保证一人一个文件夹
                uid = safe_component(f"{uid}-{_digest(session)}",
                                     fallback=f"session-{_digest(session)}")
            self._map[session] = uid
            self._used.add(uid)
            self._save()
            return uid

    def id_for(self, session: str) -> str:
        return self.resolve(session)

    def session_of(self, uid: str) -> str:
        target = str(uid or "")
        with self._lock:
            for session, value in self._map.items():
                if value == target:
                    return session
        return ""
