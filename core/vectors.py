"""
vectors.py —— 第二路召回：按意思找

两件事：

1. **LocalEmbedder**：零依赖兜底。字符 bigram 哈希成固定维向量，
   能抓住"累了 / 好累 / 累死"这种形态变化，抓不住"累 / 疲惫"这种真同义
   —— 那部分交给 query.py 的扩词。两者叠起来才够用。

2. **VectorStore**：`vectors.db`，fp + dim + float32 BLOB。
   刻意和 `index.db` 分开：索引层随手能删重建，向量重建要调模型、要花钱。

有宿主 EmbeddingProvider 时（main 里探测），把它的向量交给同一个 store，
查的时候两条路各自出候选，再由 search.py 融合。
"""

from __future__ import annotations

import math
import os
import sqlite3
from array import array
from contextlib import contextmanager
from hashlib import sha1

SCHEMA = """
CREATE TABLE IF NOT EXISTS vectors (
    fp    TEXT PRIMARY KEY,
    dim   INTEGER NOT NULL,
    model TEXT NOT NULL,
    vec   BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vectors_model ON vectors(model);
"""


def _features(text: str) -> list[str]:
    """中文取 bigram，英文取小写词——粗，但对"同一个词的不同写法"够用。"""
    body = " ".join(str(text or "").split()).lower()
    feats: list[str] = []
    cjk_run = []

    def flush():
        if not cjk_run:
            return
        if len(cjk_run) == 1:
            feats.append("".join(cjk_run))
        else:
            feats.extend("".join(cjk_run[i:i + 2])
                         for i in range(len(cjk_run) - 1))
        cjk_run.clear()

    word = []
    for ch in body:
        if "\u4e00" <= ch <= "\u9fff":
            if word:
                feats.append("".join(word))
                word = []
            cjk_run.append(ch)
        elif ch.isalnum():
            if cjk_run:
                flush()
            word.append(ch)
        else:
            if word:
                feats.append("".join(word))
                word = []
            flush()
    if word:
        feats.append("".join(word))
    flush()
    return feats


class LocalEmbedder:
    """字符 bigram + 特征哈希。纯 Python，不引依赖，结果确定。"""

    name = "local-hash-v1"
    DIM = 512

    def vector(self, text: str) -> list[float]:
        dim = self.DIM
        buckets = [0.0] * dim
        counts: dict[int, float] = {}
        signs: dict[int, float] = {}

        for feature in _features(text):
            digest = sha1(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % dim
            sign = 1.0 if digest[4] & 1 else -1.0
            counts[index] = counts.get(index, 0.0) + 1.0
            signs[index] = sign

        for index, count in counts.items():
            buckets[index] = signs[index] * (1.0 + math.log(count))

        norm = math.sqrt(sum(v * v for v in buckets))
        if norm <= 0:
            return [0.0] * dim
        return [v / norm for v in buckets]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def _pack(values: list[float]) -> bytes:
    return array("f", values).tobytes()


def _unpack(blob: bytes) -> list[float]:
    values = array("f")
    values.frombytes(blob)
    return list(values)


class VectorStore:
    """按内容指纹存向量。重建 = 拿 vault 重算一遍。"""

    def __init__(self, root: str, model: str = ""):
        self.path = os.path.join(root, "vectors.db")
        self.model = model or LocalEmbedder.name
        self._init()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self) -> None:
        if os.path.dirname(self.path):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # ------------------------------------------------------------ 写

    def put(self, items: list[tuple[str, list[float]]]) -> int:
        if not items:
            return 0
        written = 0
        with self._conn() as conn:
            for fp, vec in items:
                if not fp or not vec:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO vectors(fp,dim,model,vec)"
                    " VALUES(?,?,?,?)",
                    (fp, len(vec), self.model, _pack(vec)))
                written += 1
        return written

    # ------------------------------------------------------------ 读

    def has_many(self, fps: list[str]) -> set[str]:
        unique = [fp for fp in dict.fromkeys(fps or []) if fp]
        found: set[str] = set()
        if not unique:
            return found
        with self._conn() as conn:
            for start in range(0, len(unique), 400):
                chunk = unique[start:start + 400]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT fp FROM vectors WHERE fp IN ({marks}) AND model=?",
                    chunk + [self.model]).fetchall()
                found.update(row[0] for row in rows)
        return found

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM vectors WHERE model=?",
                (self.model,)).fetchone()[0]

    def search(self, query_vec: list[float], limit: int = 20) -> list[tuple[str, float]]:
        """
        全表扫点积。几百到几万条都够快；真到十万级再换 ANN，
        现在不值得为它引一个依赖。
        """
        if not query_vec:
            return []
        scored: list[tuple[str, float]] = []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT fp, dim, vec FROM vectors WHERE model=?",
                (self.model,)).fetchall()
        for fp, dim, blob in rows:
            if dim != len(query_vec):
                continue
            scored.append((fp, cosine(query_vec, _unpack(blob))))
        scored.sort(key=lambda item: item[1], reverse=True)
        return [(fp, score) for fp, score in scored[:max(1, limit)] if score > 0]

    # ------------------------------------------------------------ 重建

    def rebuild(self, vault, embedder, limit: int = 0) -> int:
        """拿 vault 重算全部向量。换 embedding 模型后必做。"""
        events = list(vault.iter_events())
        if limit and len(events) > limit:
            events = events[-limit:]
        payload = [(str(ev.get("fp") or ""), embedder.vector(ev.get("text", "")))
                   for ev in events if ev.get("fp")]
        self.clear()
        return self.put(payload)

    def clear(self) -> int:
        with self._conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM vectors WHERE model=?",
                (self.model,)).fetchone()[0]
            conn.execute("DELETE FROM vectors WHERE model=?", (self.model,))
        return count
