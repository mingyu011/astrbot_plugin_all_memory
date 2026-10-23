"""
pending.py —— 归档失败的重试队列

on_agent_done 归档时如果抛异常，这一轮就永久丢了，日志里也只剩一行 warning。
这里先把没写进去的内容落到 failed/ 下，下一轮请求进来时补写，
补写成功才删文件。宁可晚一点，不能悄悄少一段。
"""

from __future__ import annotations

import os
import json
import time
import uuid


class FailedQueue:
    """落盘的重试队列：一个失败轮次一个 JSON 文件。"""

    def __init__(self, root: str):
        self.dir = os.path.join(root, "failed")
        os.makedirs(self.dir, exist_ok=True)

    @staticmethod
    def _key(session: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_"
                       for ch in (session or "default"))[:60]
        return safe or "default"

    # ------------------------------------------------------------ 写

    def push(self, session: str, items: list[dict], persona: str = "",
             error: str = "") -> str:
        """把一轮没写进去的消息存下来。返回文件路径（空 = 没东西可存）。"""
        clean = [{"role": str(item.get("role") or "user"),
                  "content": str(item.get("content") or "")}
                 for item in items or []
                 if str(item.get("content") or "").strip()]
        if not clean:
            return ""

        name = (f"{self._key(session)}-{int(time.time() * 1000)}"
                f"-{uuid.uuid4().hex[:6]}.json")
        path = os.path.join(self.dir, name)
        tmp = path + ".tmp"
        payload = {
            "session": session or "",
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "persona": persona or "",
            "error": error or "",
            "items": clean,
        }
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError:
            return ""
        return path

    # ------------------------------------------------------------ 读

    def pending(self, session: str | None = None, limit: int = 3) -> list[tuple[str, dict]]:
        """按时间顺序取出待补写的条目。session=None 表示不限会话。"""
        out: list[tuple[str, dict]] = []
        if not os.path.isdir(self.dir):
            return out

        for name in sorted(os.listdir(self.dir)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.dir, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            if session is not None and str(data.get("session") or "") != (session or ""):
                continue
            out.append((path, data))
            if len(out) >= max(1, limit):
                break
        return out

    def discard(self, path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    def count(self) -> int:
        if not os.path.isdir(self.dir):
            return 0
        return sum(1 for name in os.listdir(self.dir) if name.endswith(".json"))

    def age_seconds(self) -> float:
        """最老那条待补写的时间（秒）；空队列返回 0。"""
        if not os.path.isdir(self.dir):
            return 0.0
        oldest = None
        for name in os.listdir(self.dir):
            if not name.endswith(".json"):
                continue
            try:
                stamp = os.path.getmtime(os.path.join(self.dir, name))
            except OSError:
                continue
            oldest = stamp if oldest is None else min(oldest, stamp)
        return max(0.0, time.time() - oldest) if oldest else 0.0
