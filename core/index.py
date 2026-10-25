"""
index.py —— 第三层：索引层（可重建）

这一层不承载任何不可替代的信息。删掉、重建，随时可以。
只负责：快速判断"这条见过没有"、快速检索"哪段话相关"。

0.9.17 起按 uid 隔离：同一句话在不同 id 里是两条记录，
数据库主键用 `uid + 0 + fp`，但对外返回的 fp 仍是原指纹。
"""

from __future__ import annotations

import os
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import timedelta

from .ids import safe_component
from .vault import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    key      TEXT PRIMARY KEY,
    fp       TEXT NOT NULL,
    uid      TEXT NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_events_fp      ON events(fp);
CREATE INDEX IF NOT EXISTS idx_events_uid     ON events(uid);
CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_day     ON events(day);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session);
"""


def make_key(uid: str, fp: str) -> str:
    return f"{safe_component(uid, fallback='default')}\x00{fp}"


_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")
_ASCII_WORD = re.compile(r"[A-Za-z0-9_]+")


def tokenize_words(text: str) -> str:
    """
    给 unicode61 词级 FTS 用的预分词。

    trigram 对两字中文词基本不命中；这里把中文切成重叠 bigram，
    英文/数字按整词，两字词就有了真正的 BM25 索引。
    """
    body = str(text or "")
    tokens: list[str] = []
    for match in _CJK_RUN.finditer(body):
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    tokens.extend(m.group(0).lower() for m in _ASCII_WORD.finditer(body))
    return " ".join(tokens)


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
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            # journal_mode 是持久设置，只在 _init 里设一次。
            # 每轮都执行 PRAGMA journal_mode=WAL 会在别的连接写入时等锁，
            # 实测能把事件循环卡 30 秒。
            conn.execute("PRAGMA busy_timeout=3000")
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
            try:
                c.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
            c.executescript(SCHEMA)
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
            c.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS events_fts_words USING fts5("
                "tokens, tokenize='unicode61')"
            )
            # 0.9.17 早期版本误用了 external content 的词级表（会读回原文，
            # 实际没索引 bigram）。旧表统一丢掉，换成 contentless。
            try:
                c.execute("DROP TABLE IF EXISTS events_fts_word")
            except sqlite3.Error:
                pass
            try:
                events = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                words = c.execute(
                    "SELECT COUNT(*) FROM events_fts_words").fetchone()[0]
                if events != words:
                    c.execute("DROP TABLE IF EXISTS events_fts_words")
                    c.execute(
                        "CREATE VIRTUAL TABLE events_fts_words USING fts5("
                        "tokens, tokenize='unicode61')")
                    rows = c.execute("SELECT rowid, text FROM events").fetchall()
                    for rowid, text in rows:
                        c.execute(
                            "INSERT INTO events_fts_words(rowid, tokens)"
                            " VALUES(?,?)",
                            (rowid, tokenize_words(text)))
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------ 写入

    @staticmethod
    def _uid(event: dict) -> str:
        return safe_component(event.get("uid") or "default", fallback="default")

    def has(self, fp: str, uid: str | None = None) -> bool:
        with self._conn() as c:
            if uid:
                row = c.execute(
                    "SELECT 1 FROM events WHERE key=? LIMIT 1",
                    (make_key(uid, fp),)).fetchone()
            else:
                row = c.execute(
                    "SELECT 1 FROM events WHERE fp=? LIMIT 1", (fp,)).fetchone()
            return row is not None

    def has_many(self, fps: list[str], uid: str | None = None) -> set[str]:
        if not fps:
            return set()
        found: set[str] = set()
        with self._conn() as c:
            for start in range(0, len(fps), 400):
                chunk = fps[start:start + 400]
                if uid:
                    keys = [make_key(uid, fp) for fp in chunk]
                    marks = ",".join("?" * len(keys))
                    rows = c.execute(
                        f"SELECT fp FROM events WHERE key IN ({marks})", keys
                    ).fetchall()
                else:
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
                uid = self._uid(ev)
                fp = str(ev["fp"])
                key = make_key(uid, fp)
                try:
                    cur = c.execute(
                        "INSERT OR IGNORE INTO events"
                        "(key,fp,uid,ts,day,role,origin,session,text,tokens,"
                        "persona,raw,sender,backfill)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            key, fp, uid, ev["ts"], ev["day"], ev["role"],
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
                        c.execute(
                            "INSERT INTO events_fts_words(rowid, tokens) VALUES(?,?)",
                            (cur.lastrowid, tokenize_words(ev["text"])),
                        )
                except sqlite3.Error:
                    # 单条失败不影响其余
                    continue
        return added

    # ------------------------------------------------------------ 重建

    def verify(self) -> dict:
        """检查这个索引库还能不能用。"""
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

        重建时做两件修复（都不动 vault）：
          1. 用 raw 重算正文——早期版本把「只有思考/工具块」的助手消息存成了 JSON
          2. 从正文里的「Current datetime: ...」还原真实时间，并清掉回填标记
          3. 旧数据里多行共用一个指纹时按出现顺序编号（abc、abc-2……），
             每个 id 单独编号
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

        total = 0
        for uid in vault.ids():
            buffer: list[dict] = []
            carry = None
            fp_counter: dict[str, int] = {}
            fp_used: set[str] = set()

            for ev in vault.iter_events(uid):
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
                row["uid"] = uid

                buffer.append(row)
                if len(buffer) >= 500:
                    normalize_duplicate_fps(buffer, fp_counter, fp_used)
                    total += self.add(buffer)
                    buffer = []
            normalize_duplicate_fps(buffer, fp_counter, fp_used)
            total += self.add(buffer)
        return total

    # ------------------------------------------------------------ 检索

    @staticmethod
    def _filters(uid: str | None, session: str | None,
                 alias: str = "e") -> tuple[str, list]:
        clauses: list[str] = []
        params: list = []
        if uid:
            clauses.append(f"{alias}.uid=?")
            params.append(safe_component(uid, fallback="default"))
        if session:
            clauses.append(
                f"({alias}.session=? OR {alias}.session IS NULL "
                f"OR {alias}.session='')")
            params.append(session)
        return (" AND ".join(clauses), params)

    def search(self, query: str, limit: int = 12,
               session: str | None = None,
               uid: str | None = None) -> list[dict]:
        """全文检索。uid 非空时只查这个 id。"""
        q = " ".join((query or "").split())
        if len(q) < 2:
            return []
        safe = q.replace('"', " ")
        with self._conn() as c:
            rows = self._search_fts(c, safe, limit, session, uid)
            if not rows:
                rows = self._search_like(c, q, limit, session, uid)
        return [{"fp": r[0], "ts": r[1], "role": r[2], "text": r[3],
                 "session": r[4], "uid": r[5]} for r in rows]

    def search_many(self, terms: list[str], limit: int = 12,
                    session: str | None = None,
                    uid: str | None = None) -> list[dict]:
        """
        多词检索：trigram 管 3 字以上，词级 bigram FTS 管两字词。

        以前是每个词各查一次再 RRF：常见三字窗口和罕见专有名词同权，
        排序会被泛词带偏。现在每张 FTS 各做一次 OR/BM25，再在索引内
        做一次 RRF，最后才交给上层和向量路融合。
        """
        cleaned: list[str] = []
        for term in terms or []:
            value = " ".join(str(term or "").split()).strip()
            if len(value) < 2 or value in cleaned:
                continue
            cleaned.append(value[:48])
        if not cleaned:
            return []

        word_terms = [t for t in cleaned if len(t) == 2]
        long_terms = [t for t in cleaned if len(t) >= 3]
        routes: list[list] = []
        short_rows: list = []

        with self._conn() as c:
            # 只保留相对罕见一点的词：很多问题里“小木/主人/什么”会命中半库，
            # 让它们参与 OR 会把真正的专有名词（A7M4、权限、B站）挤下去。
            where, params = self._filters(uid, session, "e")
            total_sql = "SELECT COUNT(*) FROM events e"
            if where:
                total_sql += " WHERE " + where
            try:
                total = c.execute(total_sql, params).fetchone()[0]
            except sqlite3.Error:
                total = 0
            if total > 0 and len(cleaned) > 2:
                threshold = max(12, int(total * 0.25))
                rare: list[str] = []
                for term in cleaned:
                    table = ("events_fts_words" if len(term) == 2
                             else "events_fts")
                    sql = (f"SELECT COUNT(*) FROM {table} f"
                           " JOIN events e ON e.rowid=f.rowid"
                           f" WHERE {table} MATCH ?")
                    values = [f'"{term.replace(chr(34), " ")}"']
                    if where:
                        sql += " AND " + where
                        values.extend(params)
                    try:
                        df = c.execute(sql, values).fetchone()[0]
                    except sqlite3.Error:
                        df = 0
                    if 0 < df <= threshold:
                        rare.append(term)
                if rare:
                    cleaned = rare
                    word_terms = [t for t in cleaned if len(t) == 2]
                    long_terms = [t for t in cleaned if len(t) >= 3]

            if word_terms:
                expression = " OR ".join(
                    f'"{t.replace(chr(34), " ")}"' for t in word_terms[:12])
                rows = self._search_fts_expr(
                    c, expression, max(4, limit), session, uid,
                    table="events_fts_words")
                if rows:
                    routes.append(rows)
            if long_terms:
                expression = " OR ".join(
                    f'"{t.replace(chr(34), " ")}"' for t in long_terms[:12])
                rows = self._search_fts_expr(
                    c, expression, max(4, limit), session, uid,
                    table="events_fts")
                if rows:
                    routes.append(rows)

            # 词级 FTS 没覆盖到的两字词，再用 LIKE 兜底一次。
            if not routes:
                for term in cleaned:
                    short_rows.extend(self._search_like(
                        c, term, max(4, limit), session, uid))

        if short_rows:
            routes.append(short_rows)
        if not routes:
            return []

        # 索引内 RRF：同一 fp 在 trigram / word 两路都命中时加分。
        scores: dict[str, float] = {}
        payload: dict[str, tuple] = {}
        for rows in routes:
            for rank, row in enumerate(rows):
                fp = row[0]
                scores[fp] = scores.get(fp, 0.0) + 1.0 / (61 + rank)
                payload.setdefault(fp, row)
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        out = []
        for fp, _score in ordered[:max(1, int(limit))]:
            row = payload[fp]
            out.append({"fp": row[0], "ts": row[1], "role": row[2],
                        "text": row[3], "session": row[4], "uid": row[5]})
        return out

    def _search_fts_expr(self, c, expression: str, limit: int,
                         session: str | None, uid: str | None,
                         table: str = "events_fts") -> list:
        where, params = self._filters(uid, session, "e")
        sql = (f"SELECT e.fp,e.ts,e.role,e.text,e.session,e.uid FROM {table} f"
               " JOIN events e ON e.rowid=f.rowid"
               f" WHERE {table} MATCH ?")
        if where:
            sql += " AND " + where
        sql += " ORDER BY rank LIMIT ?"
        try:
            return c.execute(sql, [expression] + params + [limit]).fetchall()
        except sqlite3.Error:
            return []

    def _search_fts(self, c, safe: str, limit: int,
                    session: str | None, uid: str | None) -> list:
        where, params = self._filters(uid, session, "e")
        sql = ("SELECT e.fp,e.ts,e.role,e.text,e.session,e.uid FROM events_fts f"
               " JOIN events e ON e.rowid=f.rowid"
               " WHERE events_fts MATCH ?")
        if where:
            sql += " AND " + where
        sql += " ORDER BY rank LIMIT ?"
        try:
            return c.execute(sql, [f'"{safe}"'] + params + [limit]).fetchall()
        except sqlite3.Error:
            return []

    def _search_like(self, c, q: str, limit: int,
                     session: str | None, uid: str | None) -> list:
        where, params = self._filters(uid, session, "events")
        sql = "SELECT fp,ts,role,text,session,uid FROM events WHERE text LIKE ?"
        if where:
            sql += " AND " + where
        sql += " ORDER BY ts DESC LIMIT ?"
        try:
            return c.execute(sql, [f"%{q}%"] + params + [limit]).fetchall()
        except sqlite3.Error:
            return []

    # ------------------------------------------------------------ 身份

    def sessions_of(self, fps: list[str], uid: str | None = None) -> dict[str, str]:
        """指纹 → 会话。事实没有 subject 时，靠它判断"这句话是哪个会话里说的"。"""
        out: dict[str, str] = {}
        unique = [fp for fp in dict.fromkeys(fps or []) if fp]
        if not unique:
            return out
        with self._conn() as c:
            for start in range(0, len(unique), 400):
                chunk = unique[start:start + 400]
                if uid:
                    keys = [make_key(uid, fp) for fp in chunk]
                    marks = ",".join("?" * len(keys))
                    rows = c.execute(
                        f"SELECT fp, session FROM events WHERE key IN ({marks})",
                        keys).fetchall()
                else:
                    marks = ",".join("?" * len(chunk))
                    rows = c.execute(
                        f"SELECT fp, session FROM events WHERE fp IN ({marks})",
                        chunk).fetchall()
                for fp, session in rows:
                    if session:
                        out[fp] = session
        return out

    def subjects(self, session: str, uid: str | None = None) -> set[str]:
        """这个会话里出现过的发送者 ID。"""
        if not session:
            return set()
        with self._conn() as c:
            sql = ("SELECT DISTINCT sender FROM events"
                   " WHERE session=? AND sender IS NOT NULL AND sender<>''")
            params: list = [session]
            if uid:
                sql += " AND uid=?"
                params.append(safe_component(uid, fallback="default"))
            rows = c.execute(sql, params).fetchall()
        return {row[0] for row in rows if row[0]}

    # ------------------------------------------------------------ 单条读取

    def get(self, fp: str, uid: str | None = None) -> dict | None:
        with self._conn() as c:
            if uid:
                row = c.execute(
                    "SELECT fp,ts,day,role,origin,session,text,tokens,raw,"
                    "backfill,uid FROM events WHERE key=?",
                    (make_key(uid, fp),)).fetchone()
            else:
                row = c.execute(
                    "SELECT fp,ts,day,role,origin,session,text,tokens,raw,"
                    "backfill,uid FROM events WHERE fp=? LIMIT 1", (fp,)).fetchone()
        if not row:
            return None
        return {"fp": row[0], "ts": row[1], "day": row[2], "role": row[3],
                "origin": row[4], "session": row[5], "text": row[6],
                "tokens": row[7], "raw": row[8], "backfill": bool(row[9]),
                "uid": row[10]}

    def get_many(self, fps: list[str], uid: str | None = None) -> dict[str, dict]:
        """
        一次取多条；给融合层补正文用。

        以前 combine 对每个向量候选各开一次连接（100 条 = 100 次），
        在 628 条时约 150ms；这里按 400 条一批查完。
        """
        unique = [fp for fp in dict.fromkeys(fps or []) if fp]
        if not unique:
            return {}
        out: dict[str, dict] = {}
        with self._conn() as c:
            for start in range(0, len(unique), 400):
                chunk = unique[start:start + 400]
                if uid:
                    keys = [make_key(uid, fp) for fp in chunk]
                    marks = ",".join("?" * len(keys))
                    rows = c.execute(
                        "SELECT fp,ts,day,role,origin,session,text,tokens,raw,"
                        "backfill,uid FROM events WHERE key IN (" + marks + ")",
                        keys).fetchall()
                else:
                    marks = ",".join("?" * len(chunk))
                    rows = c.execute(
                        "SELECT fp,ts,day,role,origin,session,text,tokens,raw,"
                        "backfill,uid FROM events WHERE fp IN (" + marks + ")",
                        chunk).fetchall()
                for row in rows:
                    out[str(row[0])] = {
                        "fp": row[0], "ts": row[1], "day": row[2],
                        "role": row[3], "origin": row[4], "session": row[5],
                        "text": row[6], "tokens": row[7], "raw": row[8],
                        "backfill": bool(row[9]), "uid": row[10],
                    }
        return out

    def neighbors(self, fp: str, span: int = 2,
                  uid: str | None = None) -> list[dict]:
        """取某条记忆前后的若干条，按写入顺序。用于还原上下文。"""
        span = max(0, min(int(span or 0), 10))
        if span == 0:
            return []
        with self._conn() as c:
            if uid:
                row = c.execute("SELECT rowid FROM events WHERE key=?",
                                (make_key(uid, fp),)).fetchone()
            else:
                row = c.execute("SELECT rowid FROM events WHERE fp=? LIMIT 1",
                                (fp,)).fetchone()
            if not row:
                return []
            target = row[0]
            scope = " AND uid=?" if uid else ""
            scope_p = [safe_component(uid, fallback="default")] if uid else []
            before = c.execute(
                "SELECT fp,ts,role,text FROM events WHERE rowid<?" + scope +
                " ORDER BY rowid DESC LIMIT ?",
                [target] + scope_p + [span]).fetchall()
            after = c.execute(
                "SELECT fp,ts,role,text FROM events WHERE rowid>?" + scope +
                " ORDER BY rowid ASC LIMIT ?",
                [target] + scope_p + [span]).fetchall()
        rows = list(reversed(before)) + after
        return [{"fp": r[0], "ts": r[1], "role": r[2], "text": r[3]}
                for r in rows]

    # ------------------------------------------------------------ 统计 / 地图

    def count(self, uid: str | None = None) -> int:
        with self._conn() as c:
            if uid:
                return c.execute(
                    "SELECT COUNT(*) FROM events WHERE uid=?",
                    (safe_component(uid, fallback="default"),)).fetchone()[0]
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
                " COUNT(*), MIN(ts), MAX(ts), MAX(uid)"
                " FROM events GROUP BY s ORDER BY MAX(ts) DESC LIMIT ?",
                (limit,)).fetchall()

            out = []
            for session, count, first_ts, last_ts, uid in rows:
                sample = c.execute(
                    "SELECT substr(text,1,40) FROM events"
                    " WHERE COALESCE(NULLIF(session,''),'(未分类)')=?"
                    " AND role='user' ORDER BY ts DESC LIMIT 1",
                    (session,)).fetchone()
                out.append({
                    "session": session,
                    "uid": uid or "",
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

        limit <= 0 表示不分页、整段拉完。
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

    def count_by_origin(self, uid: str | None = None) -> dict:
        with self._conn() as c:
            if uid:
                rows = c.execute(
                    "SELECT origin, COUNT(*) FROM events WHERE uid=?"
                    " GROUP BY origin",
                    (safe_component(uid, fallback="default"),)).fetchall()
            else:
                rows = c.execute(
                    "SELECT origin, COUNT(*) FROM events GROUP BY origin"
                ).fetchall()
        return {r[0]: r[1] for r in rows}

    def last_fp(self, session: str, uid: str | None = None) -> str:
        """这个会话最后一条记录的指纹。回复里的图片挂它身上。"""
        with self._conn() as c:
            sql = ("SELECT fp FROM events WHERE session=?"
                   " ORDER BY rowid DESC LIMIT 1")
            params: list = [session or ""]
            if uid:
                sql = ("SELECT fp FROM events WHERE session=? AND uid=?"
                       " ORDER BY rowid DESC LIMIT 1")
                params.append(safe_component(uid, fallback="default"))
            row = c.execute(sql, params).fetchone()
        return str(row[0]) if row else ""

    def earliest_day(self, uid: str | None = None) -> str:
        """最早有记录的那一天（YYYY-MM-DD），空库返回空串。"""
        with self._conn() as c:
            if uid:
                row = c.execute(
                    "SELECT MIN(day) FROM events WHERE uid=?",
                    (safe_component(uid, fallback="default"),)).fetchone()
            else:
                row = c.execute("SELECT MIN(day) FROM events").fetchone()
        return str(row[0] or "") if row else ""

    def day_count(self, day: str, uid: str | None = None) -> int:
        """某一天有多少条。翻页时告诉调用方"还有没有"。"""
        if not day:
            return 0
        with self._conn() as c:
            if uid:
                row = c.execute(
                    "SELECT COUNT(*) FROM events WHERE day=? AND uid=?",
                    (str(day), safe_component(uid, fallback="default"))).fetchone()
            else:
                row = c.execute("SELECT COUNT(*) FROM events WHERE day=?",
                                (str(day),)).fetchone()
        return int(row[0] or 0) if row else 0

    def day_messages(self, day: str, limit: int = 40,
                     offset: int = 0, uid: str | None = None) -> list[dict]:
        """某一天的消息（按写入顺序），支持翻页。"""
        if not day:
            return []
        with self._conn() as c:
            if uid:
                rows = c.execute(
                    "SELECT fp,ts,role,text FROM events WHERE day=? AND uid=?"
                    " ORDER BY rowid ASC LIMIT ? OFFSET ?",
                    (str(day), safe_component(uid, fallback="default"),
                     max(1, int(limit or 40)), max(0, int(offset or 0)))).fetchall()
            else:
                rows = c.execute(
                    "SELECT fp,ts,role,text FROM events WHERE day=?"
                    " ORDER BY rowid ASC LIMIT ? OFFSET ?",
                    (str(day), max(1, int(limit or 40)),
                     max(0, int(offset or 0)))).fetchall()
        return [{"fp": r[0], "ts": r[1], "role": r[2], "text": r[3]} for r in rows]

    def day_summary(self, days: int | None = None, per_day: int = 3,
                    uid: str | None = None) -> list[dict]:
        """
        按天统计记忆分布，供记忆地图使用。

        days=None 表示全部历史——地图不该有边界，从第一天到现在都算，
        装不下由上层按预算聚合，而不是在这里砍掉。
        """
        out = []
        scope = " AND uid=?" if uid else ""
        scope_p = [safe_component(uid, fallback="default")] if uid else []
        with self._conn() as c:
            if days is None:
                rows = c.execute(
                    "SELECT day, COUNT(*) FROM events WHERE 1=1" + scope +
                    " GROUP BY day ORDER BY day DESC", scope_p
                ).fetchall()
            else:
                cutoff = (now() - timedelta(days=days)).strftime("%Y-%m-%d")
                rows = c.execute(
                    "SELECT day, COUNT(*) FROM events WHERE day>=?" + scope +
                    " GROUP BY day ORDER BY day DESC", [cutoff] + scope_p
                ).fetchall()

            for day, count in rows:
                first = c.execute(
                    "SELECT substr(text,1,30) FROM events"
                    " WHERE day=? AND role='user'" + scope +
                    " ORDER BY rowid ASC LIMIT 1",
                    [day] + scope_p).fetchone()
                latest = c.execute(
                    "SELECT substr(text,1,30) FROM events"
                    " WHERE day=? AND role='user'" + scope +
                    " ORDER BY rowid DESC LIMIT ?",
                    [day] + scope_p + [max(0, int(per_day or 1) - 1)],
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
