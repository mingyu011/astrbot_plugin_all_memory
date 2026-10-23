"""
index.py —— 第三层：索引层（可重建）

这一层不承载任何不可替代的信息。删掉、重建，随时可以。
只负责：快速判断"这条见过没有"、快速检索"哪段话相关"。
"""

from __future__ import annotations

import os
import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta

from .vault import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    fp       TEXT PRIMARY KEY,
    ts       TEXT NOT NULL,
    day      TEXT NOT NULL,
    role     TEXT NOT NULL,
    origin   TEXT NOT NULL,
    session  TEXT,
    text     TEXT NOT NULL,
    tokens   INTEGER NOT NULL,
    persona  TEXT,
    raw      TEXT,
    sender   TEXT,
    backfill INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_day     ON events(day);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session);
"""

# 老库升级：缺列就补上，不动已有数据
_MIGRATIONS = (
    ("raw", "ALTER TABLE events ADD COLUMN raw TEXT"),
    ("backfill", "ALTER TABLE events ADD COLUMN backfill INTEGER NOT NULL DEFAULT 0"),
    ("sender", "ALTER TABLE events ADD COLUMN sender TEXT"),
)


class Index:
    def __init__(self, root: str):
        self.path = os.path.join(root, "index.db")
        self._init()

    # ------------------------------------------------------------ 底层

    @contextmanager
    def _conn(self):
        """
        注意：sqlite3 的 `with conn:` 只提交事务，不会关闭连接。
        连接不关会让 Windows 上删不掉 index.db，rebuild 就会静默读到旧数据。
        所以这里自己包一层，确保一定 close。
        """
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
        with self._conn() as c:
            c.executescript(SCHEMA)
            existing = {row[1] for row in
                        c.execute("PRAGMA table_info(events)").fetchall()}
            for column, ddl in _MIGRATIONS:
                if column not in existing:
                    try:
                        c.execute(ddl)
                    except sqlite3.Error:
                        pass
            try:
                c.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5("
                    "text, content='events', content_rowid='rowid', tokenize='trigram')"
                )
            except sqlite3.OperationalError:
                # 老版本 SQLite 不支持 trigram，退回 unicode61
                c.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5("
                    "text, content='events', content_rowid='rowid', tokenize='unicode61')"
                )

    # ------------------------------------------------------------ 写入

    def has(self, fp: str) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT 1 FROM events WHERE fp=? LIMIT 1", (fp,)).fetchone()
            return row is not None

    def has_many(self, fps: list[str]) -> set[str]:
        if not fps:
            return set()
        found: set[str] = set()
        with self._conn() as c:
            for start in range(0, len(fps), 400):
                chunk = fps[start:start + 400]
                marks = ",".join("?" * len(chunk))
                rows = c.execute(
                    f"SELECT fp FROM events WHERE fp IN ({marks})", chunk
                ).fetchall()
                found.update(r[0] for r in rows)
        return found

    def add(self, events: list[dict]) -> int:
        if not events:
            return 0
        added = 0
        with self._conn() as c:
            for ev in events:
                try:
                    cur = c.execute(
                        "INSERT OR IGNORE INTO events"
                        "(fp,ts,day,role,origin,session,text,tokens,persona,raw,sender,backfill)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            ev["fp"], ev["ts"], ev["day"], ev["role"],
                            ev.get("origin", "unknown"), ev.get("session"),
                            ev["text"], ev.get("tokens", 0), ev.get("persona"),
                            ev.get("raw"), ev.get("sender"),
                            1 if ev.get("backfill") else 0,
                        ),
                    )
                    if cur.rowcount:
                        added += 1
                        c.execute(
                            "INSERT INTO events_fts(rowid, text) VALUES(?,?)",
                            (cur.lastrowid, ev["text"]),
                        )
                except sqlite3.Error:
                    # 单条失败不影响其余
                    continue
        return added

    # ------------------------------------------------------------ 重建

    def verify(self) -> dict:
        """
        检查这个索引库还能不能用。

        用来判断「从备份搬过来的 index.db 能不能直接接着用」：
        打不开、或者全文索引不可用（比如跨机器缺 trigram 分词器），都得重建。
        """
        result = {"ok": False, "events": 0, "fts": 0, "error": ""}
        try:
            with self._conn() as c:
                result["events"] = c.execute(
                    "SELECT COUNT(*) FROM events").fetchone()[0]
                try:
                    result["fts"] = c.execute(
                        "SELECT COUNT(*) FROM events_fts").fetchone()[0]
                except sqlite3.Error as exc:
                    result["error"] = f"全文索引不可用: {exc}"
                    return result
            result["ok"] = True
        except sqlite3.Error as exc:
            result["error"] = str(exc)
        return result

    def rebuild(self, vault) -> int:
        """
        从原始层全量重建。索引坏了就调这个。

        重建时会做三件修复（都不动 vault）：
          1. 用 raw 重算正文——早期版本把「只有思考/工具块」的助手消息存成了 JSON
          2. 从正文里的「Current datetime: ...」还原真实时间，并清掉回填标记
          3. 旧数据里多行共用一个指纹时按出现顺序编号（abc、abc-2……），
             否则重建出来的索引会比 vault 少几条，检索永远缺那几句
        """
        from .sync import extract_content, parse_reminder_time
        from .vault import normalize_duplicate_fps

        for suffix in ("", "-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        self._init()

        buffer: list[dict] = []
        total = 0
        carry = None          # 上一条用户消息里的真实时间
        fp_counter: dict[str, int] = {}
        fp_used: set[str] = set()

        for ev in vault.iter_events():
            row = ev
            raw = ev.get("raw")
            if isinstance(raw, str) and raw:
                try:
                    parsed = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    parsed = None
                if parsed is not None:
                    fixed, _ = extract_content(parsed)
                    if fixed and fixed != ev.get("text"):
                        row = dict(row)
                        row["text"] = fixed

            found = parse_reminder_time(row.get("text", ""))
            if found is not None:
                carry = found
            stamp = found or carry
            if stamp is not None:
                if row is ev:
                    row = dict(row)
                row["ts"] = stamp.isoformat(timespec="seconds")
                row["day"] = stamp.strftime("%Y-%m-%d")
                row["backfill"] = False

            buffer.append(row)
            if len(buffer) >= 500:
                normalize_duplicate_fps(buffer, fp_counter, fp_used)
                total += self.add(buffer)
                buffer = []
        normalize_duplicate_fps(buffer, fp_counter, fp_used)
        total += self.add(buffer)
        return total

    # ------------------------------------------------------------ 检索

    def search(self, query: str, limit: int = 12,
               session: str | None = None) -> list[dict]:
        """
        全文检索。

        session 非空时按会话过滤，但**空会话的记录一律包含**——
        那些是 /allmem_import 导进来的历史，不该因为没会话键就搜不到。
        """
        q = " ".join((query or "").split())
        if len(q) < 2:
            return []
        safe = q.replace('"', " ")
        with self._conn() as c:
            rows = self._search_fts(c, safe, limit, session)
            if not rows:
                rows = self._search_like(c, q, limit, session)
        return [{"fp": r[0], "ts": r[1], "role": r[2], "text": r[3],
                 "session": r[4]} for r in rows]

    @staticmethod
    def _scope_clause(alias: str) -> str:
        return f"({alias}.session=? OR {alias}.session IS NULL OR {alias}.session='')"

    def _search_fts(self, c, safe: str, limit: int,
                    session: str | None) -> list:
        try:
            if session:
                return c.execute(
                    "SELECT e.fp,e.ts,e.role,e.text,e.session FROM events_fts f"
                    " JOIN events e ON e.rowid=f.rowid"
                    f" WHERE events_fts MATCH ? AND {self._scope_clause('e')}"
                    " ORDER BY rank LIMIT ?",
                    (f'"{safe}"', session, limit),
                ).fetchall()
            return c.execute(
                "SELECT e.fp,e.ts,e.role,e.text,e.session FROM events_fts f"
                " JOIN events e ON e.rowid=f.rowid"
                " WHERE events_fts MATCH ? ORDER BY rank LIMIT ?",
                (f'"{safe}"', limit),
            ).fetchall()
        except sqlite3.Error:
            return []

    def _search_like(self, c, q: str, limit: int,
                     session: str | None) -> list:
        try:
            if session:
                return c.execute(
                    "SELECT fp,ts,role,text,session FROM events"
                    f" WHERE text LIKE ? AND {self._scope_clause('events')}"
                    " ORDER BY ts DESC LIMIT ?",
                    (f"%{q}%", session, limit),
                ).fetchall()
            return c.execute(
                "SELECT fp,ts,role,text,session FROM events"
                " WHERE text LIKE ? ORDER BY ts DESC LIMIT ?",
                (f"%{q}%", limit),
            ).fetchall()
        except sqlite3.Error:
            return []

    # ------------------------------------------------------------ 身份

    def sessions_of(self, fps: list[str]) -> dict[str, str]:
        """指纹 → 会话。事实没有 subject 时，靠它判断"这句话是哪个会话里说的"。"""
        out: dict[str, str] = {}
        unique = [fp for fp in dict.fromkeys(fps or []) if fp]
        if not unique:
            return out
        with self._conn() as c:
            for start in range(0, len(unique), 400):
                chunk = unique[start:start + 400]
                marks = ",".join("?" * len(chunk))
                for fp, session in c.execute(
                        f"SELECT fp, session FROM events WHERE fp IN ({marks})",
                        chunk).fetchall():
                    if session:
                        out[fp] = session
        return out

    def subjects(self, session: str) -> set[str]:
        """这个会话里出现过的发送者 ID（用来判断"这条事实能不给他看"）。"""
        if not session:
            return set()
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT sender FROM events"
                " WHERE session=? AND sender IS NOT NULL AND sender<>''",
                (session,)).fetchall()
        return {row[0] for row in rows if row[0]}

    # ------------------------------------------------------------ 单条读取

    def get(self, fp: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT fp,ts,day,role,origin,session,text,tokens,raw,backfill"
                " FROM events WHERE fp=?", (fp,)).fetchone()
        if not row:
            return None
        return {"fp": row[0], "ts": row[1], "day": row[2], "role": row[3],
                "origin": row[4], "session": row[5], "text": row[6],
                "tokens": row[7], "raw": row[8], "backfill": bool(row[9])}

    def neighbors(self, fp: str, span: int = 2) -> list[dict]:
        """取某条记忆前后的若干条，按写入顺序。用于还原上下文。"""
        span = max(0, min(int(span or 0), 10))
        if span == 0:
            return []
        with self._conn() as c:
            row = c.execute("SELECT rowid FROM events WHERE fp=?", (fp,)).fetchone()
            if not row:
                return []
            target = row[0]
            before = c.execute(
                "SELECT fp,ts,role,text FROM events WHERE rowid<?"
                " ORDER BY rowid DESC LIMIT ?", (target, span)).fetchall()
            after = c.execute(
                "SELECT fp,ts,role,text FROM events WHERE rowid>?"
                " ORDER BY rowid ASC LIMIT ?", (target, span)).fetchall()
        rows = list(reversed(before)) + after
        return [{"fp": r[0], "ts": r[1], "role": r[2], "text": r[3]}
                for r in rows]

    # ------------------------------------------------------------ 统计 / 地图

    def count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    # ------------------------------------------------------------ 会话列表 / 历史

    @staticmethod
    def _session_key(session: str | None) -> str:
        return (session or "").strip()

    def sessions(self, limit: int = 200) -> list[dict]:
        """按会话聚合，供 WebUI 左侧列表用。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT COALESCE(NULLIF(session,''),'(未分类)') AS s,"
                " COUNT(*), MIN(ts), MAX(ts)"
                " FROM events GROUP BY s ORDER BY MAX(ts) DESC LIMIT ?",
                (limit,)).fetchall()

            out = []
            for session, count, first_ts, last_ts in rows:
                sample = c.execute(
                    "SELECT substr(text,1,40) FROM events"
                    " WHERE COALESCE(NULLIF(session,''),'(未分类)')=?"
                    " AND role='user' ORDER BY ts DESC LIMIT 1",
                    (session,)).fetchone()
                out.append({
                    "session": session,
                    "count": count,
                    "first_ts": first_ts,
                    "last_ts": last_ts,
                    "sample": (sample[0].strip() if sample else ""),
                    "kind": self._guess_kind(session),
                })
        return out

    @staticmethod
    def _guess_kind(session: str) -> str:
        lowered = (session or "").lower()
        if "group" in lowered or "群" in (session or ""):
            return "group"
        if session in ("", "(未分类)"):
            return "unknown"
        return "private"

    def history(self, session: str, offset: int = 0, limit: int = 100,
                newest_first: bool = True) -> dict:
        """
        某会话的历史，分页。

        排序用 rowid（写入顺序），不用 ts——回填的历史时间戳全挤在同一秒，
        按 ts 排会互相打架。rowid 就是当时入包的先后，天然正确。

        limit <= 0 表示不分页、整段拉完（面板「一口气拖到底」就靠它）。
        """
        key = self._session_key(session) if session not in ("(未分类)",) else None
        where = "session=?" if key else "(session IS NULL OR session='')"
        params: list = [key] if key else []

        order = "DESC" if newest_first else "ASC"
        paging = ""
        tail: list = []
        if limit and int(limit) > 0:
            paging = " LIMIT ? OFFSET ?"
            tail = [int(limit), max(0, int(offset or 0))]

        with self._conn() as c:
            total = c.execute(
                f"SELECT COUNT(*) FROM events WHERE {where}", params
            ).fetchone()[0]
            rows = c.execute(
                "SELECT fp,ts,role,text,origin,tokens,raw,backfill FROM events"
                f" WHERE {where} ORDER BY rowid {order}" + paging,
                params + tail).fetchall()

        return {
            "total": total,
            "items": [{"fp": r[0], "ts": r[1], "role": r[2], "text": r[3],
                       "origin": r[4], "tokens": r[5], "raw": r[6],
                       "backfill": bool(r[7])} for r in rows],
        }

    def count_by_origin(self) -> dict:
        with self._conn() as c:
            rows = c.execute(
                "SELECT origin, COUNT(*) FROM events GROUP BY origin"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def last_fp(self, session: str) -> str:
        """这个会话最后一条记录的指纹。回复里的图片挂它身上。"""
        with self._conn() as c:
            row = c.execute(
                "SELECT fp FROM events WHERE session=? ORDER BY rowid DESC LIMIT 1",
                (session or "",)).fetchone()
        return str(row[0]) if row else ""

    def earliest_day(self) -> str:
        """最早有记录的那一天（YYYY-MM-DD），空库返回空串。"""
        with self._conn() as c:
            row = c.execute("SELECT MIN(day) FROM events").fetchone()
        return str(row[0] or "") if row else ""

    def day_count(self, day: str) -> int:
        """某一天有多少条。翻页时告诉调用方"还有没有"。"""
        if not day:
            return 0
        with self._conn() as c:
            row = c.execute("SELECT COUNT(*) FROM events WHERE day=?",
                            (str(day),)).fetchone()
        return int(row[0] or 0) if row else 0

    def day_messages(self, day: str, limit: int = 40,
                     offset: int = 0) -> list[dict]:
        """某一天的消息（按写入顺序），支持翻页。

        "中午出门、两点回来"这种问题要顺着时间线读完一整天，
        只给前 40 条不够，所以带 offset。
        """
        if not day:
            return []
        with self._conn() as c:
            rows = c.execute(
                "SELECT fp,ts,role,text FROM events WHERE day=?"
                " ORDER BY rowid ASC LIMIT ? OFFSET ?",
                (str(day), max(1, int(limit or 40)),
                 max(0, int(offset or 0)))).fetchall()
        return [{"fp": r[0], "ts": r[1], "role": r[2], "text": r[3]} for r in rows]

    def day_summary(self, days: int | None = None, per_day: int = 3) -> list[dict]:
        """
        按天统计记忆分布，供记忆地图使用。

        days=None 表示**全部历史**——地图不该有边界，从第一天到现在都算，
        装不下由上层按预算聚合，而不是在这里砍掉。
        """
        out = []
        with self._conn() as c:
            if days is None:
                rows = c.execute(
                    "SELECT day, COUNT(*) FROM events"
                    " GROUP BY day ORDER BY day DESC"
                ).fetchall()
            else:
                cutoff = (now() - timedelta(days=days)).strftime("%Y-%m-%d")
                rows = c.execute(
                    "SELECT day, COUNT(*) FROM events WHERE day>=?"
                    " GROUP BY day ORDER BY day DESC", (cutoff,)
                ).fetchall()

            for day, count in rows:
                # 样本要带"那天开头"的那句：问"第一次见面说了什么"时，
                # 只给最近三条基本永远看不到开头。
                first = c.execute(
                    "SELECT substr(text,1,30) FROM events"
                    " WHERE day=? AND role='user' ORDER BY rowid ASC LIMIT 1",
                    (day,)).fetchone()
                latest = c.execute(
                    "SELECT substr(text,1,30) FROM events"
                    " WHERE day=? AND role='user' ORDER BY rowid DESC LIMIT ?",
                    (day, max(0, int(per_day or 1) - 1)),
                ).fetchall()
                samples = []
                for row in ([first] if first else []) + latest:
                    if row and row[0] and row[0] not in samples:
                        samples.append(row[0])
                out.append({
                    "day": day,
                    "count": count,
                    "samples": [s.strip() for s in samples if s.strip()],
                })
        return out
