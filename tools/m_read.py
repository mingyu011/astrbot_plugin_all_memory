"""
m_read.py —— 独立读取 M（不依赖 AstrBot、不依赖插件运行时）

用法：

    python tools/m_read.py stats  <zip或all_memory目录>
    python tools/m_read.py search <zip或目录> "关键词" --uid <id> --limit 5
    python tools/m_read.py day    <zip或目录> 2026-10-24 --uid <id>
    python tools/m_read.py fact   <zip或目录> fact-...

只读 `all_memory/` 里的 vault / facts / summaries / media / personas；
`index.db`、`vectors.db`、`cache/`、`state/` 一律忽略，按 vault 现建临时 FTS。
只用标准库。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import uuid
import zipfile

MANIFEST_NAME = "manifest.json"
CONTAINER = "all_memory/"


def _safe_rel(name: str) -> str:
    rel = str(name or "").replace("\\", "/").strip("/")
    if not rel:
        return ""
    first = rel.split("/", 1)[0]
    if rel.startswith("/") or (len(first) == 2 and first[1] == ":"):
        return ""
    if any(part in ("", ".", "..") for part in rel.split("/")):
        return ""
    return rel


def _temp_dir(parent: str = "") -> str:
    candidates = []
    if parent:
        candidates.append(parent)
    import tempfile
    candidates.append(tempfile.gettempdir())
    last_error = None
    for base in candidates:
        path = os.path.join(base, f"allmem-read-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        try:
            os.makedirs(os.path.join(path, "probe", "sub"), exist_ok=False)
            os.rmdir(os.path.join(path, "probe", "sub"))
            os.rmdir(os.path.join(path, "probe"))
            return path
        except OSError as exc:
            last_error = exc
            shutil.rmtree(path, ignore_errors=True)
            continue
    raise OSError(f"没有可写的临时目录: {last_error}")


def _prepare(path: str) -> tuple[str, str]:
    """返回 (all_memory 根目录, 需要清理的临时目录)。"""
    path = os.path.abspath(str(path or ""))
    if os.path.isdir(path):
        if os.path.isfile(os.path.join(path, "all_memory", MANIFEST_NAME)):
            return os.path.join(path, "all_memory"), ""
        if os.path.isfile(os.path.join(path, MANIFEST_NAME)):
            return path, ""
        if os.path.isdir(os.path.join(path, "all_memory")):
            return os.path.join(path, "all_memory"), ""
        return path, ""

    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    if not zipfile.is_zipfile(path):
        raise ValueError(f"不是目录，也不是 zip: {path}")

    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if CONTAINER + MANIFEST_NAME in names or any(
                name.startswith(CONTAINER) for name in names):
            prefix = CONTAINER
        elif MANIFEST_NAME in names:
            prefix = ""
        else:
            raise ValueError(f"包里没有 {CONTAINER} 或 {MANIFEST_NAME}")

    temp = _temp_dir(os.path.dirname(path))
    root = os.path.join(temp, "all_memory") if prefix else temp
    os.makedirs(root, exist_ok=True)
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                rel = _safe_rel(info.filename)
                if not rel or not rel.startswith(prefix):
                    continue
                tail = rel[len(prefix):]
                if not tail:
                    continue
                if not prefix and tail.startswith("astrbot_data/"):
                    continue
                target = os.path.join(root, tail.replace("/", os.sep))
                parent = os.path.dirname(target)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with archive.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return root, temp


def _load_manifest(root: str) -> dict:
    path = os.path.join(root, MANIFEST_NAME)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _iter_events(root: str, uid: str = ""):
    vault = os.path.join(root, "vault")
    if not os.path.isdir(vault):
        return
    for name in sorted(os.listdir(vault)):
        uid_dir = os.path.join(vault, name)
        if not os.path.isdir(uid_dir):
            continue
        if uid and name != uid:
            continue
        for day_file in sorted(os.listdir(uid_dir)):
            if not day_file.endswith(".jsonl"):
                continue
            path = os.path.join(uid_dir, day_file)
            try:
                handle = open(path, "r", encoding="utf-8")
            except OSError:
                continue
            with handle:
                for line in handle:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        event = json.loads(text)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    event.setdefault("uid", name)
                    event.setdefault("day", day_file[:-6])
                    yield event


def _count_lines(path: str) -> int:
    total = 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    total += 1
    except OSError:
        pass
    return total


def _scan_counts(root: str) -> dict:
    counts = {
        "events": 0, "days": 0, "facts": 0, "summaries": 0,
        "personas": 0, "media_records": 0, "blobs": 0, "failed": 0,
    }
    ids: set[str] = set()
    days: set[str] = set()
    for event in _iter_events(root):
        counts["events"] += 1
        ids.add(str(event.get("uid") or "default"))
        days.add(str(event.get("day") or str(event.get("ts") or "")[:10]))
    counts["ids"] = len(ids)
    counts["days"] = len(days)

    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            if rel.startswith("facts/") and rel.endswith(".json"):
                counts["facts"] += 1
            elif rel.startswith("summaries/") and rel.endswith("/summary.json"):
                counts["summaries"] += 1
            elif rel.startswith("personas/") and rel.endswith(".txt"):
                counts["personas"] += 1
            elif rel.startswith("media/") and rel.endswith(".jsonl"):
                counts["media_records"] += _count_lines(os.path.join(dirpath, name))
            elif rel.startswith("blobs/"):
                counts["blobs"] += 1
            elif rel.startswith("failed/") and rel.endswith(".json"):
                counts["failed"] += 1
    return counts


def stats(path: str) -> dict:
    root, temp = _prepare(path)
    try:
        manifest = _load_manifest(root)
        counts = _scan_counts(root)
        return {
            "root": root,
            "manifest": bool(manifest),
            "schema": str(manifest.get("schema") or ""),
            "created_at": str(manifest.get("created_at") or ""),
            "ids": sorted({
                name for name in (
                    os.listdir(os.path.join(root, "vault"))
                    if os.path.isdir(os.path.join(root, "vault")) else [])
                if os.path.isdir(os.path.join(root, "vault", name))
            }),
            "counts": counts,
            "manifest_counts": manifest.get("counts") or {},
        }
    finally:
        if temp:
            shutil.rmtree(temp, ignore_errors=True)


def _build_index(root: str, uid: str = ""):
    conn = sqlite3.connect(":memory:")
    fts = True
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE events_fts USING fts5(text, tokenize='trigram')")
    except sqlite3.OperationalError:
        fts = False
        conn.execute("CREATE TABLE events_fts (text TEXT)")

    events = []
    for event in _iter_events(root, uid):
        events.append(event)
        conn.execute("INSERT INTO events_fts(text) VALUES(?)",
                     (str(event.get("text") or ""),))
    conn.commit()
    return conn, events, fts


def search(path: str, query: str, uid: str = "", limit: int = 5) -> list[dict]:
    query = " ".join(str(query or "").split())
    if not query:
        return []
    root, temp = _prepare(path)
    conn = None
    try:
        conn, events, fts = _build_index(root, uid)
        rows = []
        if fts and len(query) >= 3:
            expression = '"' + query.replace('"', '""') + '"'
            try:
                rows = conn.execute(
                    "SELECT rowid FROM events_fts WHERE events_fts MATCH ?"
                    " ORDER BY rank LIMIT ?", (expression, max(1, int(limit)))
                ).fetchall()
            except sqlite3.Error:
                rows = []
        if not rows:
            rows = conn.execute(
                "SELECT rowid FROM events_fts WHERE text LIKE ?"
                " ORDER BY rowid DESC LIMIT ?",
                (f"%{query}%", max(1, int(limit)))).fetchall()
        out = []
        for (rowid,) in rows:
            index = int(rowid) - 1
            if 0 <= index < len(events):
                out.append(events[index])
        return out
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        if temp:
            shutil.rmtree(temp, ignore_errors=True)


def day(path: str, day_text: str, uid: str = "", limit: int = 50,
        offset: int = 0) -> list[dict]:
    target = str(day_text or "").strip()
    root, temp = _prepare(path)
    try:
        items = [event for event in _iter_events(root, uid)
                 if str(event.get("day") or str(event.get("ts") or "")[:10]) == target]
        items.sort(key=lambda event: str(event.get("ts") or ""))
        start = max(0, int(offset or 0))
        return items[start:start + max(1, int(limit or 50))]
    finally:
        if temp:
            shutil.rmtree(temp, ignore_errors=True)


def fact(path: str, key: str, uid: str = "") -> dict | None:
    key = str(key or "").strip()
    if not key:
        return None
    root, temp = _prepare(path)
    try:
        facts_dir = os.path.join(root, "facts")
        found = None
        if os.path.isdir(facts_dir):
            for name in sorted(os.listdir(facts_dir)):
                uid_dir = os.path.join(facts_dir, name)
                if not os.path.isdir(uid_dir) or (uid and name != uid):
                    continue
                for file_name in sorted(os.listdir(uid_dir)):
                    if not file_name.endswith(".json"):
                        continue
                    try:
                        with open(os.path.join(uid_dir, file_name),
                                  "r", encoding="utf-8") as handle:
                            item = json.load(handle)
                    except (OSError, json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(item, dict):
                        continue
                    versions = item.get("versions") or []
                    current = versions[-1] if versions else {}
                    if (str(item.get("id") or "") == key
                            or key.lower() in str(current.get("claim") or "").lower()):
                        found = item
                        break
                if found:
                    break
        if not found:
            return None

        event_map = {}
        for event in _iter_events(root, uid):
            fp = str(event.get("fp") or "")
            if fp:
                event_map[fp] = event
        versions = found.get("versions") or []
        current = versions[-1] if versions else {}
        sources = []
        for fp in current.get("source_fp") or []:
            fp = str(fp)
            event = event_map.get(fp) or event_map.get(fp.split("-", 1)[0])
            sources.append({"fp": fp, "found": bool(event), "event": event})
        return {"fact": found, "sources": sources}
    finally:
        if temp:
            shutil.rmtree(temp, ignore_errors=True)


def _print_stats(path: str) -> int:
    data = stats(path)
    counts = data["counts"]
    print(f"M: {data['root']}")
    print(f"manifest: {'有' if data['manifest'] else '无（仍在用目录结构读取）'}")
    if data["schema"]:
        print(f"schema: {data['schema']}")
    if data["created_at"]:
        print(f"created_at: {data['created_at']}")
    print(f"ids: {', '.join(data['ids']) or '（空）'}")
    print(f"events: {counts['events']} / days: {counts['days']}")
    print(f"facts: {counts['facts']} / summaries: {counts['summaries']} / "
          f"personas: {counts['personas']}")
    print(f"media: {counts['media_records']} / blobs: {counts['blobs']} / "
          f"failed: {counts['failed']}")
    expected = data["manifest_counts"]
    if expected and int(expected.get("events", -1)) not in (-1, counts["events"]):
        print(f"⚠️ manifest 记录 events={expected.get('events')}，实际 {counts['events']}")
    return 0


def _print_events(items: list[dict]) -> None:
    if not items:
        print("（没有结果）")
        return
    for index, event in enumerate(items, 1):
        text = " ".join(str(event.get("text") or "").split())
        if len(text) > 200:
            text = text[:200] + "…"
        print(f"{index}. [{event.get('ts', '')}] {event.get('role', '')}：{text}")


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="独立读取 all_memory 的 M")
    sub = parser.add_subparsers(dest="command", required=True)

    item = sub.add_parser("stats", help="统计")
    item.add_argument("path")

    item = sub.add_parser("search", help="关键词搜索")
    item.add_argument("path")
    item.add_argument("query")
    item.add_argument("--uid", default="")
    item.add_argument("--limit", type=int, default=5)

    item = sub.add_parser("day", help="按天读")
    item.add_argument("path")
    item.add_argument("day")
    item.add_argument("--uid", default="")
    item.add_argument("--limit", type=int, default=50)
    item.add_argument("--offset", type=int, default=0)

    item = sub.add_parser("fact", help="查事实和出处")
    item.add_argument("path")
    item.add_argument("key")
    item.add_argument("--uid", default="")

    args = parser.parse_args(argv)
    if args.command == "stats":
        return _print_stats(args.path)
    if args.command == "search":
        _print_events(search(args.path, args.query, args.uid, args.limit))
        return 0
    if args.command == "day":
        _print_events(day(args.path, args.day, args.uid,
                          args.limit, args.offset))
        return 0
    if args.command == "fact":
        data = fact(args.path, args.key, args.uid)
        if not data:
            print("（没有找到这条事实）")
            return 1
        item = data["fact"]
        print(f"{item.get('id', '')}")
        for version in item.get("versions") or []:
            print(f"v{version.get('version')}：{version.get('claim')}")
            print(f"  kind={version.get('kind', '')} "
                  f"model={version.get('model', '')} "
                  f"prompt={version.get('prompt_version', '')}")
            print(f"  extracted_at={version.get('extracted_at', '')}")
        for source in data["sources"]:
            event = source.get("event") or {}
            print(f"- [{event.get('ts', '?')}] {event.get('role', '?')}："
                  f"{event.get('text', '（出处不在 vault）')}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
