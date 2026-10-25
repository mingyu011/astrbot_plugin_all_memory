"""
importer.py —— 超集导入器

目标：vault ⊇ AstrBot 存档 ∪ memory_beyond 数据目录

铁律：**只读源，只写 vault。** 绝不修改 AstrBot 的数据库，
绝不改动 memory_beyond 的任何文件。

两个导入器都是"尽量认"策略：认不出结构就按纯文本收进来，
宁可多存，不可漏存——原始层的价值就在于多。
"""

from __future__ import annotations

import os
import json
import base64
import hashlib
import sqlite3
from datetime import datetime

from .sync import extract_content, norm_content, parse_reminder_time, sync_message
from .vault import fp_of

# 这些列名看起来像正文
# 注意没有 "value"：AstrBot 的 preferences 表是 key/value 配置，
# 收进来只会得到 {"val": true} 这种垃圾事件，不算"可能漏存的对话"。
TEXT_COLUMNS = ("content", "text", "message", "message_str", "prompt", "body")
# 这些列名看起来像说话人
ROLE_COLUMNS = ("role", "sender", "sender_name", "user_id", "nickname", "type")
# 时间列
TIME_COLUMNS = ("created_at", "timestamp", "create_time", "time", "ts", "updated_at")


class _Emitter:
    """
    导入的唯一出口。

    dry_run=True 时**一个字都不写**：只按 sync_message 完全相同的口径
    （norm_content → strip → fp_of(role\\0text)）算出候选指纹，攒起来。
    /allmem_verify 靠它把「来源里有什么」和「库里有什么」对上——
    这样校验逻辑和导入逻辑永远是同一份，不会各写一套然后各说各话。
    """

    def __init__(self, vault, index, origin: str, session: str = "",
                 dry_run: bool = False, backfill: bool = False,
                 uid: str = "default"):
        self.vault = vault
        self.index = index
        self.origin = origin
        self.session = session
        self.dry_run = bool(dry_run)
        self.backfill = bool(backfill)
        self.uid = uid
        self.fps: list[str] = []

    def __call__(self, role: str, text, ts=None, backfill=None) -> bool:
        if self.dry_run:
            clean = norm_content(text).strip()
            if not clean:
                return False
            self.fps.append(fp_of(f"{role}\x00{clean}"))
            return True
        if backfill is None:
            backfill = self.backfill
        return sync_message(self.vault, self.index, role, text,
                            session=self.session, origin=self.origin,
                            ts=ts, backfill=backfill, uid=self.uid)


def missing_fps(index, fps: list[str], uid: str | None = None) -> int:
    """候选指纹里还有多少条不在索引里（= 还没进库）。"""
    unique = list(dict.fromkeys(fps or []))
    if not unique:
        return 0
    return len(unique) - len(index.has_many(unique, uid))


def expand_messages(text: str) -> list[tuple[str, str]]:
    """
    把「整段会话存成一段 JSON」的字段拆回逐条消息。

    AstrBot v4 的 conversations.content 就是这种形态：
    一个会话一行，正文是 [{"role": ..., "content": [...]}, ...] 的 JSON 数组。
    如果照字面收进原始层，整段几百条对话会缩成一条巨大的 JSON 正文——
    角色、时间、句子边界全丢，还直接违反「原始层只存原话」。

    拆得出来就返回 [(role, text)]；不是这种形态就返回空列表，交给上层按纯文本收。
    """
    if not isinstance(text, str):
        return []
    body = text.strip()
    if not body or body[0] not in "[{":
        return []

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("messages") or data.get("history") or []
    if not isinstance(data, list) or not data:
        return []
    if not any(isinstance(item, dict) and ("role" in item or "content" in item)
               for item in data):
        return []

    out: list[tuple[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        content = item.get("content", item.get("text"))
        value, _raw = extract_content(content)
        value = (value or "").strip()
        # 宿主回存过的记忆块会带 _no_save 戳，那些是我们自己注入的，不是她说过的话
        if not value or "_no_save" in value.lower():
            continue
        out.append((_guess_role(str(item.get("role") or "user")), value))
    return out


# ================================================================ AstrBot 数据库

def scan_sqlite(db_path: str) -> list[dict]:
    """列出库里所有表及其列，供手动确认。"""
    out = []
    if not os.path.exists(db_path):
        return out
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return out
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        for (name,) in tables:
            try:
                cols = [row[1] for row in
                        conn.execute(f'PRAGMA table_info("{name}")').fetchall()]
            except sqlite3.Error:
                continue
            out.append({"table": name, "columns": cols})
    finally:
        conn.close()
    return out


def import_astrbot(db_path: str, vault, index, session: str = "",
                   dry_run: bool = False, uid: str = "default") -> dict:
    """
    只读模式打开 AstrBot 的 SQLite，把能认出来的文本全部收进原始层。
    认不出结构的表会被跳过并记下来，不猜、不写。

    dry_run=True：只扫不写，stats["fps"] 返回这些内容本该有的指纹，
    供 /allmem_verify 做超集校验。
    """
    stats = {"tables": 0, "events": 0, "skipped_tables": [], "errors": [],
             "dry_run": bool(dry_run), "fps": []}
    if not os.path.exists(db_path):
        stats["errors"].append(f"文件不存在: {db_path}")
        return stats

    emit = _Emitter(vault, index, "import_astrbot",
                    session=session, dry_run=dry_run, uid=uid)

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        stats["errors"].append(f"打不开: {exc}")
        return stats

    try:
        carry = None          # 上一条用户消息里的真实时间，供紧随其后的回复沿用
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for table in tables:
            stats["tables"] += 1
            try:
                cols = [r[1] for r in
                        conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
            except sqlite3.Error:
                continue

            text_col = _pick(cols, TEXT_COLUMNS)
            if not text_col:
                stats["skipped_tables"].append(table)
                continue

            role_col = _pick(cols, ROLE_COLUMNS)
            time_col = _pick(cols, TIME_COLUMNS)
            select = [f'"{text_col}"']
            if role_col:
                select.append(f'"{role_col}"')
            if time_col:
                select.append(f'"{time_col}"')

            try:
                rows = conn.execute(
                    f'SELECT {", ".join(select)} FROM "{table}"').fetchall()
            except sqlite3.Error as exc:
                stats["errors"].append(f"{table}: {exc}")
                continue

            for row in rows:
                text = row[0]
                if isinstance(text, (bytes, bytearray)):
                    try:
                        text = text.decode("utf-8", "ignore")
                    except Exception:
                        continue
                text = str(text or "").strip()
                if len(text) < 2:
                    continue

                role = "user"
                if role_col:
                    raw_role = str(row[1] or "")
                    role = _guess_role(raw_role)

                # 整段会话存成 JSON 的列（v4 的 conversations.content）先拆回逐条，
                # 否则一条会话只会变成一条巨大的 JSON 正文
                expanded = expand_messages(text)
                if expanded:
                    # 时间从每条用户消息里的「Current datetime」还原；
                    # 助手回复紧随其后，沿用上一条用户消息的时间（分钟级准确）
                    carry = None
                    for item_role, item_text in expanded:
                        found = parse_reminder_time(item_text)
                        if found is not None:
                            carry = found
                        stamp = found or carry
                        if emit(item_role, item_text, ts=stamp,
                                backfill=stamp is None):
                            stats["events"] += 1
                    continue

                found = parse_reminder_time(text)
                if found is not None:
                    carry = found
                stamp = found or carry
                if emit(role, text, ts=stamp, backfill=stamp is None):
                    stats["events"] += 1
    finally:
        conn.close()
    stats["fps"] = list(emit.fps)
    return stats


# ================================================================ memory_beyond 目录

def import_beyond(root: str, vault, index, dry_run: bool = False,
                  uid: str = "default") -> dict:
    """
    读 memory_beyond 的数据目录：
        state/*.json          压缩摘要与水位线
        archive/**            压缩归档（被摘要替代的原文）
        */*.md                记忆文件
    全部转成事件写入原始层。

    dry_run=True：只扫不写，stats["fps"] 返回这些内容本该有的指纹。
    """
    stats = {"files": 0, "events": 0, "errors": [],
             "dry_run": bool(dry_run), "fps": []}
    if not os.path.isdir(root):
        stats["errors"].append(f"目录不存在: {root}")
        return stats

    # memory_beyond 的文件里没有可用的消息时间，落库时间就是「归档时刻」，
    # 所以统一标 backfill，面板上如实显示「回填」而不是假装当时发生。
    emit = _Emitter(vault, index, "import_beyond", dry_run=dry_run,
                    backfill=True, uid=uid)

    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            stats["files"] += 1
            try:
                if name.endswith(".json"):
                    stats["events"] += _import_json(path, emit, stats["errors"])
                elif name.endswith((".jsonl", ".ndjson")):
                    stats["events"] += _import_jsonl(path, emit, stats["errors"])
                elif name.endswith(".md"):
                    stats["events"] += _import_markdown(path, emit)
            except Exception as exc:  # 单个文件坏掉不影响整体
                stats["errors"].append(f"{name}: {exc}")
    stats["fps"] = list(emit.fps)
    return stats


def _import_json(path: str, emit, errors: list) -> int:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{os.path.basename(path)}: {exc}")
        return 0

    count = 0
    # 压缩状态：摘要 + 水位线
    if isinstance(data, dict):
        summary = data.get("summary") or data.get("rolling_summary")
        if isinstance(summary, str) and summary.strip():
            if emit("summary", summary):
                count += 1
        messages = data.get("messages") or data.get("history")
        if isinstance(messages, list):
            count += _import_message_list(messages, emit)
    elif isinstance(data, list):
        count += _import_message_list(data, emit)
    return count


def _import_message_list(messages: list, emit) -> int:
    count = 0
    for item in messages:
        if isinstance(item, str):
            if emit("user", item):
                count += 1
            continue
        if not isinstance(item, dict):
            continue
        text = item.get("content") or item.get("text") or item.get("message")
        if isinstance(text, (list, dict)):
            text = json.dumps(text, ensure_ascii=False)
        text = str(text or "").strip()
        if len(text) < 2:
            continue
        role = _guess_role(str(item.get("role") or "user"))
        if emit(role, text):
            count += 1
    return count


def _import_jsonl(path: str, emit, errors: list) -> int:
    count = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(item, dict):
                    text = item.get("content") or item.get("text")
                    if isinstance(text, (list, dict)):
                        text = json.dumps(text, ensure_ascii=False)
                    text = str(text or "").strip()
                    if len(text) >= 2:
                        role = _guess_role(str(item.get("role") or "user"))
                        if emit(role, text):
                            count += 1
    except OSError as exc:
        errors.append(f"{os.path.basename(path)}: {exc}")
    return count


def _import_markdown(path: str, emit) -> int:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read().strip()
    except OSError:
        return 0
    if len(text) < 4:
        return 0
    # 记忆文件整体作为一条 memory 事件；内部结构原样保留
    if emit("memory", text):
        return 1
    return 0


# ================================================================ 附件回填

_IMAGE_EXTS = {"image/jpeg": ".jpg", "image/png": ".png",
               "image/webp": ".webp", "image/gif": ".gif"}


def _decode_image_uri(url: str) -> tuple[bytes, str, str]:
    """把 data:image/...;base64,xxxx 解成 (bytes, mime, 扩展名)。"""
    if not isinstance(url, str) or not url.startswith("data:"):
        return b"", "", ""
    header, _, payload = url.partition(",")
    mime = "image/jpeg"
    if ";" in header:
        mime = header[5:].split(";", 1)[0].strip() or mime
    try:
        data = base64.b64decode(payload, validate=False)
    except Exception:
        return b"", mime, ""
    return data, mime, _IMAGE_EXTS.get(mime, ".jpg")


def _media_keys(media_dir: str) -> set:
    """已有附件记录的键（fu|hash 排序），用来去重。递归扫 id 子目录。"""
    from .media import base_fp

    keys = set()
    if not os.path.isdir(media_dir):
        return keys
    for _dirpath, _dirnames, filenames in os.walk(media_dir):
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            try:
                with open(os.path.join(_dirpath, name), "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        fu = base_fp(str(row.get("fu") or ""))
                        hashes = sorted(str(i.get("hash", "")) for i in (row.get("items") or []))
                        if fu:
                            keys.add(f"{fu}|{','.join(hashes)}")
            except OSError:
                continue
    return keys


def backfill_media_from_astrbot(db_path: str, media_store,
                                dry_run: bool = False, ids=None) -> dict:
    """
    把宿主库里内嵌的图片（data URI）解出来，补进 blobs/ 和 media/<id>/。

    用途：插件晚装、或附件归档有 bug 时，把历史图片补回来。
    AstrBot 的 conversations.content 里图片是 base64 内嵌的，所以补得回来。

    ids：SessionIds；给了就按每个会话解析 id，分别写进各自的 media 文件夹。
    只读宿主库，绝不改它。
    """
    from .media import MediaStore, base_fp, save_blob
    from .sync import parse_reminder_time
    from .vault import fp_of

    stats = {"conversations": 0, "messages": 0, "images": 0,
             "saved": 0, "skipped": 0, "bytes": 0, "errors": [], "dry_run": bool(dry_run)}
    if not os.path.exists(db_path):
        stats["errors"].append(f"文件不存在: {db_path}")
        return stats

    known_cache: dict[str, set] = {}

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        stats["errors"].append(f"打不开: {exc}")
        return stats

    try:
        rows = conn.execute(
            "SELECT user_id, content, updated_at FROM conversations").fetchall()
        for user_id, content, updated_at in rows:
            stats["conversations"] += 1
            scope = (ids.resolve(str(user_id or "")) if ids is not None
                     else getattr(media_store, "uid", "default"))
            store = (media_store if scope == getattr(media_store, "uid", "default")
                     else MediaStore(media_store.root, scope))
            if scope not in known_cache:
                known_cache[scope] = _media_keys(
                    os.path.join(media_store.root, "media", scope))
            known = known_cache[scope]

            try:
                messages = json.loads(content) if isinstance(content, str) else content
            except (json.JSONDecodeError, ValueError):
                stats["errors"].append(f"{user_id}: content 不是 JSON")
                continue
            if not isinstance(messages, list):
                continue

            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                blocks = msg.get("content")
                if not isinstance(blocks, list):
                    continue
                images = [b for b in blocks
                          if isinstance(b, dict) and b.get("type") == "image_url"]
                if not images:
                    continue
                stats["messages"] += 1

                role = _guess_role(str(msg.get("role") or "user"))
                text, _raw = extract_content(blocks)
                fp = fp_of(f"{role}\x00{text}") if text.strip() else ""
                stamp = parse_reminder_time(text or "")
                if stamp is None and isinstance(updated_at, (int, float)):
                    stamp = datetime.fromtimestamp(float(updated_at))

                for block in images:
                    stats["images"] += 1
                    url = (block.get("image_url") or {}).get("url", "")
                    data, mime, ext = _decode_image_uri(url)
                    if not data:
                        stats["skipped"] += 1
                        stats["errors"].append(f"{user_id}: 图片不是内嵌 base64")
                        continue
                    digest = hashlib.sha256(data).hexdigest()
                    key = f"{base_fp(fp or digest)}|{digest}"
                    if key in known:
                        stats["skipped"] += 1
                        continue
                    if dry_run:
                        stats["saved"] += 1
                        stats["bytes"] += len(data)
                        continue
                    _digest, rel = save_blob(media_store.root, data, ext)
                    store.append_record(
                        fp or digest, user_id or "",
                        [{"kind": "image", "hash": digest, "path": rel,
                          "size": len(data), "name": f"{digest[:8]}{ext}",
                          "mime": mime}],
                        ts=stamp)
                    known.add(key)
                    stats["saved"] += 1
                    stats["bytes"] += len(data)
    except sqlite3.Error as exc:
        stats["errors"].append(str(exc))
    finally:
        conn.close()

    return stats


# ================================================================ 辅助

def _pick(columns: list[str], candidates: tuple) -> str | None:
    lowered = {c.lower(): c for c in columns}
    for name in candidates:
        if name in lowered:
            return lowered[name]
    return None


def _guess_role(raw: str) -> str:
    value = raw.strip().lower()
    if value in ("assistant", "ai", "bot", "model", "gpt"):
        return "assistant"
    if value in ("system", "developer"):
        return "system"
    if value in ("summary", "memory", "note"):
        return value
    return "user"
