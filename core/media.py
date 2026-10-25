"""
media.py —— 图片 / 语音 / 文件等附件的归档

正文里只存文字，附件按内容哈希落到 blobs/ 下，事件里只引用哈希。
这样：
  - 同一个表情反复出现只占一份空间
  - 导出时把 blobs/ 一起打包就是全量
  - 正文仍然是纯文本，检索不受影响

消息组件来自 AstrBot 的消息链（Image / Record / Video / File），
不同版本字段名不一致，这里做尽量全的探测。
"""

from __future__ import annotations

import os
import json
import hashlib
import asyncio
import mimetypes
from urllib.parse import urlparse

from .ids import safe_component
from .vault import now

# 常见的附件组件名 -> 归类
MEDIA_KINDS = {
    "image": "image",
    "record": "voice",
    "video": "video",
    "file": "file",
}

_SOURCE_ATTRS = ("url", "file", "path", "file_", "src")


def base_fp(fp: str) -> str:
    """abc-2 → abc。同一句话重复说时，附件的键只算基础指纹。"""
    return str(fp or "").split("-", 1)[0]


def stats_of(root: str) -> dict:
    """所有 id 共用的附件本体统计（blobs 是全局去重的）。"""
    count = 0
    total = 0
    blob_root = os.path.join(root, "blobs")
    for dirpath, _dirs, files in os.walk(blob_root):
        for name in files:
            count += 1
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return {"files": count, "bytes": total}


def guess_kind(component) -> str:
    name = type(component).__name__.lower()
    return MEDIA_KINDS.get(name, "")


def source_of(component) -> str:
    for attr in _SOURCE_ATTRS:
        value = getattr(component, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
        # 有些版本把内容放在 .data / .content
    for attr in ("data", "content"):
        value = getattr(component, attr, None)
        if isinstance(value, (bytes, bytearray)) and value:
            return "bytes://"
    return ""


def collect(event) -> list[dict]:
    """从本轮消息链里收集附件来源。返回 [{kind, source, name}]。"""
    items = []
    try:
        chain = getattr(getattr(event, "message_obj", None), "message", None) or []
    except Exception:
        return items

    for comp in chain:
        kind = guess_kind(comp)
        if not kind:
            continue
        source = source_of(comp)
        if not source:
            continue
        name = ""
        for attr in ("name", "filename", "file_name"):
            value = getattr(comp, attr, None)
            if isinstance(value, str) and value.strip():
                name = os.path.basename(value.strip())
                break
        items.append({"kind": kind, "source": source, "name": name})
    return items


def _ext_from(source: str, kind: str) -> str:
    guess = ""
    if "://" in source and not source.startswith("bytes://"):
        guess = os.path.splitext(urlparse(source).path)[1]
    else:
        guess = os.path.splitext(source)[1]
    guess = guess.split("?")[0][:8]
    if guess and guess.startswith(".") and guess[1:].isalnum():
        return guess.lower()
    return {
        "image": ".jpg", "voice": ".amr", "video": ".mp4", "file": ".bin",
    }.get(kind, ".bin")


def save_blob(root: str, data: bytes, ext: str) -> tuple[str, str]:
    """按内容哈希落盘。返回 (hash, 相对路径)。"""
    digest = hashlib.sha256(data).hexdigest()
    rel = os.path.join("blobs", digest[:2], f"{digest}{ext}")
    path = os.path.join(root, rel)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    return digest, rel.replace("\\", "/")


async def _read_bytes(source: str) -> bytes:
    if source.startswith("bytes://"):
        return b""
    if os.path.isfile(source):
        with open(source, "rb") as f:
            return f.read()
    if source.startswith(("http://", "https://")):
        # 优先用 aiohttp（AstrBot 自带），失败再退回线程里的 urllib
        try:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(source) as resp:
                    if resp.status == 200:
                        return await resp.read()
        except Exception:
            pass
        return await asyncio.get_event_loop().run_in_executor(
            None, _urllib_get, source)
    return b""


def _urllib_get(url: str) -> bytes:
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "all-memory/0.1"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception:
        return b""


class MediaStore:
    def __init__(self, root: str, uid: str = "default"):
        self.root = root
        self.uid = safe_component(uid, fallback="default")
        self.dir = os.path.join(root, "media", self.uid)
        os.makedirs(self.dir, exist_ok=True)
        os.makedirs(os.path.join(root, "blobs"), exist_ok=True)

    def append_record(self, msg_fp: str, session: str, items: list[dict],
                      ts=None) -> None:
        """写一条附件索引。ts 不传就是"现在"；回填历史时要传当时的时刻。"""
        if not items:
            return
        moment = ts or now()
        record = {"fu": msg_fp, "session": session or None,
                  "ts": moment.isoformat(timespec="seconds"), "items": items}
        path = os.path.join(self.dir, f"{moment.strftime('%Y-%m-%d')}.jsonl")
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False,
                               separators=(",", ":")) + "\n")

    async def ingest(self, msg_fp: str, session: str,
                     sources: list[dict]) -> int:
        """下载并落盘，返回成功保存的数量。"""
        saved = []
        for item in sources:
            try:
                data = await _read_bytes(item["source"])
            except Exception:
                data = b""
            if not data:
                continue
            ext = _ext_from(item["source"], item["kind"])
            digest, rel = save_blob(self.root, data, ext)
            saved.append({
                "kind": item["kind"],
                "hash": digest,
                "path": rel,
                "size": len(data),
                "name": item.get("name") or f"{digest[:8]}{ext}",
                "mime": mimetypes.guess_type(rel)[0] or "",
            })
        if saved:
            self.append_record(msg_fp, session, saved)
        return len(saved)

    # ---------------------------------------------------------- 读取

    def records_for(self, msg_fps: set[str]) -> dict[str, list[dict]]:
        """
        按消息指纹取附件索引，供 WebUI 拼历史用。

        按**基础指纹**匹配：同一句话重复说时，库里可能存成 abc / abc-2，
        但附件的键只算到基础指纹，否则第二张图就挂不上。
        """
        out: dict[str, list[dict]] = {}
        if not msg_fps or not os.path.isdir(self.dir):
            return out
        wanted = {base_fp(fp) for fp in msg_fps if fp}
        if not wanted:
            return out
        for name in sorted(os.listdir(self.dir)):
            if not name.endswith(".jsonl"):
                continue
            try:
                with open(os.path.join(self.dir, name), "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        key = base_fp(str(row.get("fu") or ""))
                        if key in wanted:
                            out.setdefault(key, []).extend(row.get("items") or [])
            except OSError:
                continue
        return out

    def stats(self) -> dict:
        return stats_of(self.root)
