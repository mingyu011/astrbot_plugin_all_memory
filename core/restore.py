"""
restore.py —— 从导出包恢复

安全原则
--------
1. **只合并，不覆盖。** 已存在的条目一律跳过，绝不删除现有数据。
2. **只恢复本插件的数据**（包里的 all_memory/）。
   包里的 astrbot_data/ 不碰——那是宿主自己的目录，运行中覆盖会出事，
   要还原得停机手动做。
3. 幂等：同一个包导两次，第二次不会重复。
4. 路径校验：压缩包里的 `..` 一律拒绝。
"""

from __future__ import annotations

import os
import json
import shutil
import zipfile

from .tokens import estimate_tokens
from .vault import fp_of, normalize_duplicate_fps

PREFIX = "all_memory/"


def reconcile_index(vault, index, copied: bool) -> dict:
    """
    恢复完之后，索引该怎么办。

    搬过来的 index.db 能用、条数也和 vault 对得上 → 留着不重建（省时间）。
    打不开 / 条数不符 / 根本没搬 → 从 vault 重建。

    条数必须对得上：备份里的 index.db 可能落后于原文（导出那一刻索引还没写完），
    这种索引留着会让检索少一大截，所以必须发现并重建。
    """
    if copied:
        info = index.verify()
        try:
            expected = sum(1 for _ in vault.iter_events())
        except Exception:
            expected = -1
        if info.get("ok") and expected >= 0 and info.get("events") == expected:
            return {"index_kept": True, "index_events": expected}
    return {"index_kept": False, "index_rebuilt": index.rebuild(vault)}


def _safe_rel(name: str) -> str:
    """把压缩包内的路径规范化，挡掉目录穿越。"""
    rel = name.replace("\\", "/").lstrip("/")
    if not rel or rel.endswith("/"):
        return ""
    norm = os.path.normpath(rel).replace("\\", "/")
    if norm.startswith("..") or "/../" in f"/{norm}/":
        return ""
    return norm


def inspect_zip(path: str) -> dict:
    """只看不写：这个包里有什么、能恢复多少。"""
    info = {
        "ok": False, "error": "",
        "has_all_memory": False, "has_astrbot_data": False,
        "vault_days": 0, "vault_records": 0, "personas": 0,
        "facts": 0, "summaries": 0, "entities": 0, "synonyms": 0,
        "blobs": 0, "media_days": 0,
        "size": 0,
    }
    if not os.path.isfile(path):
        info["error"] = "文件不存在"
        return info
    try:
        info["size"] = os.path.getsize(path)
        with zipfile.ZipFile(path) as zf:
            for raw in zf.namelist():
                name = _safe_rel(raw)
                if not name:
                    continue
                if name.startswith("astrbot_data/"):
                    info["has_astrbot_data"] = True
                    continue
                if not name.startswith(PREFIX):
                    continue
                info["has_all_memory"] = True
                tail = name[len(PREFIX):]
                if tail.startswith("vault/") and tail.endswith(".jsonl"):
                    info["vault_days"] += 1
                elif tail.startswith("personas/") and tail.endswith(".txt"):
                    info["personas"] += 1
                elif tail.startswith("facts/") and tail.endswith(".json"):
                    info["facts"] += 1
                elif tail.startswith("summaries/") and tail.endswith(".json"):
                    info["summaries"] += 1
                elif tail.startswith("entities/") and tail.endswith(".json"):
                    info["entities"] += 1
                elif tail.startswith("synonyms/") and tail.endswith(".json"):
                    info["synonyms"] += 1
                elif tail.startswith("blobs/"):
                    info["blobs"] += 1
                elif tail.startswith("media/") and tail.endswith(".jsonl"):
                    info["media_days"] += 1
        info["ok"] = info["has_all_memory"]
        if not info["ok"]:
            info["error"] = "包里没有 all_memory/，不是本插件的导出包"
    except zipfile.BadZipFile:
        info["error"] = "不是有效的 zip"
    except OSError as exc:
        info["error"] = f"读取失败: {exc}"
    return info


def _normalize_event(raw: dict) -> dict | None:
    """把包里的记录整理成能直接写进 vault 的样子。

    正文**原样搬**，不做 strip：恢复的意义是把当初存的字节还回来，
    多一个换行都会让指纹和原文对不上，以后重扫历史时会当成新消息。
    """
    raw_text = raw.get("text", "")
    text = "" if raw_text is None else str(raw_text)
    if not text.strip():
        return None
    role = str(raw.get("role") or "user")
    fp = str(raw.get("fp") or "") or fp_of(f"{role}\x00{text}")
    ts = str(raw.get("ts") or "")
    day = str(raw.get("day") or ts[:10] or "")
    if not day:
        return None
    return {
        "fp": fp,
        "uid": raw.get("uid"),
        "ts": ts,
        "day": day,
        "role": role,
        "origin": str(raw.get("origin") or "restore"),
        "session": raw.get("session"),
        "text": text,
        "raw": raw.get("raw"),
        "tokens": int(raw.get("tokens") or estimate_tokens(text)),
        "persona": raw.get("persona"),
        "sender": raw.get("sender"),
        "backfill": bool(raw.get("backfill")),
    }


def restore_zip(path: str, root: str, vault, index,
                should_cancel=None) -> dict:
    """
    把包里的 all_memory/ 合并进当前数据目录。

    返回各项统计，用于给用户看"恢复了多少、跳过多少"。
    """
    result = {
        "ok": False, "error": "",
        "events_seen": 0, "events_added": 0, "events_skipped": 0,
        "personas_added": 0, "facts_added": 0, "blobs_added": 0,
        "summaries_added": 0,
        "entities_added": 0,
        "synonyms_added": 0,
        "media_added": 0, "media_skipped": 0,
        "settings_restored": 0, "state_added": 0, "archive_added": 0,
        "index_copied": 0,
        "astrbot_data_ignored": False,
        "sessions": [],
    }
    sessions: set[str] = set()
    index_member = ""

    info = inspect_zip(path)
    if not info["ok"]:
        result["error"] = info["error"] or "无法识别这个压缩包"
        return result

    result["astrbot_data_ignored"] = info["has_astrbot_data"]

    # 目标索引里原本有没有东西？没有才允许用备份里的索引覆盖
    had_index_events = False
    try:
        had_index_events = index.count() > 0
    except Exception:
        had_index_events = True

    # 现有 media 记录，用来去重
    existing_media = _load_media_keys(root)

    # 指纹编号跨「天的文件」共用：旧包里同一句话可能散在不同日期文件里
    fp_counter: dict[str, int] = {}
    fp_used: set[str] = set()

    try:
        with zipfile.ZipFile(path) as zf:
            for raw_name in zf.namelist():
                name = _safe_rel(raw_name)
                if not name or not name.startswith(PREFIX):
                    continue
                tail = name[len(PREFIX):]

                if tail.startswith("vault/") and tail.endswith(".jsonl"):
                    parts = tail.split("/")
                    uid = parts[1] if len(parts) > 2 else ""
                    seen, added, skipped, found = _restore_vault_member(
                        zf, raw_name, vault, index, should_cancel,
                        fp_counter, fp_used, uid)
                    result["events_seen"] += seen
                    result["events_added"] += added
                    result["events_skipped"] += skipped
                    sessions |= found

                elif tail.startswith("personas/") and tail.endswith(".txt"):
                    if _extract_if_missing(zf, raw_name, root, tail):
                        result["personas_added"] += 1

                elif tail.startswith("facts/") and tail.endswith(".json"):
                    if _extract_if_missing(zf, raw_name, root, tail):
                        result["facts_added"] += 1

                elif tail.startswith("summaries/") and tail.endswith(".json"):
                    # 纪要也是派生物，但重建要重新调模型，能搬就搬
                    if _extract_if_missing(zf, raw_name, root, tail):
                        result["summaries_added"] += 1

                elif tail.startswith("entities/") and tail.endswith(".json"):
                    if _extract_if_missing(zf, raw_name, root, tail):
                        result["entities_added"] += 1

                elif tail.startswith("synonyms/") and tail.endswith(".json"):
                    if _extract_if_missing(zf, raw_name, root, tail):
                        result["synonyms_added"] += 1

                elif tail.startswith("blobs/"):
                    if _extract_if_missing(zf, raw_name, root, tail):
                        result["blobs_added"] += 1

                elif tail.startswith("media/") and tail.endswith(".jsonl"):
                    added, skipped = _restore_media_member(
                        zf, raw_name, root, tail, existing_media)
                    result["media_added"] += added
                    result["media_skipped"] += skipped

                elif tail == "settings.json":
                    if _extract_if_missing(zf, raw_name, root, tail):
                        result["settings_restored"] += 1

                elif tail.startswith("state/") and tail.endswith(".json"):
                    if _extract_if_missing(zf, raw_name, root, tail):
                        result["state_added"] += 1

                elif tail.startswith("facts_archive/"):
                    if _extract_if_missing(zf, raw_name, root, tail):
                        result["archive_added"] += 1

                elif tail == "index.db":
                    # 最后再处理：先让 vault 合并跑完，否则索引会"认出"这些消息，
                    # 导致一条都不写进 vault
                    index_member = raw_name

                if should_cancel is not None and should_cancel():
                    result["error"] = "已取消"
                    return result

        # 索引最后处理。目标索引本来是空的（全新机器）→ 直接沿用备份里的；
        # 已经有数据（合并场景）→ 不覆盖，交给调用方重建。
        if index_member and not had_index_events:
            try:
                with zipfile.ZipFile(path) as zf:
                    if _extract_overwrite(zf, index_member, root, "index.db"):
                        result["index_copied"] = 1
            except (zipfile.BadZipFile, OSError):
                pass

        result["ok"] = True
        result["sessions"] = sorted(s for s in sessions if s)
    except zipfile.BadZipFile:
        result["error"] = "不是有效的 zip"
    except OSError as exc:
        result["error"] = f"写入失败: {exc}"
    return result


# ------------------------------------------------------------------ 内部

def _restore_vault_member(zf, member, vault, index, should_cancel,
                          fp_counter=None, fp_used=None, uid: str = ""):
    seen = added = skipped = 0
    events: list[dict] = []
    sessions: set[str] = set()
    with zf.open(member) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line.decode("utf-8", "ignore"))
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(raw, dict):
                continue
            seen += 1
            event = _normalize_event(raw)
            if event:
                if uid:
                    event["uid"] = uid
                events.append(event)
                if event.get("session"):
                    sessions.add(str(event["session"]))

    if not events:
        return seen, 0, seen, sessions

    # 旧包里可能有多行共用一个指纹，先按出现顺序编号，别在恢复时被合并掉
    normalize_duplicate_fps(events, fp_counter, fp_used)
    scope = str(events[0].get("uid") or "") or None
    fingerprints = [e["fp"] for e in events]
    known = index.has_many(fingerprints, scope)
    fresh = [e for e in events if e["fp"] not in known]
    skipped = len(events) - len(fresh)

    if fresh:
        vault.append(fresh)
        index.add(fresh)
        added = len(fresh)
    return seen, added, skipped, sessions


def _extract_if_missing(zf, member, root, tail) -> bool:
    target = os.path.join(root, tail.replace("/", os.sep))
    if os.path.exists(target):
        return False
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with zf.open(member) as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.replace(tmp, target)
    return True


def _extract_overwrite(zf, member, root, tail) -> bool:
    """强制写入（先写临时文件再替换），用于接管一个空索引。"""
    target = os.path.join(root, tail.replace("/", os.sep))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".restore.tmp"
    with zf.open(member) as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst)
    # WAL/SHM 是旧库的伴生文件，留着会和新库对不上
    for suffix in ("-wal", "-shm"):
        side = target + suffix
        if os.path.exists(side):
            try:
                os.remove(side)
            except OSError:
                pass
    os.replace(tmp, target)
    return True


def _load_media_keys(root: str) -> set:
    keys = set()
    media_dir = os.path.join(root, "media")
    if not os.path.isdir(media_dir):
        return keys
    for dirpath, _dirnames, filenames in os.walk(media_dir):
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            try:
                with open(os.path.join(dirpath, name), "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        key = _media_key(row)
                        if key:
                            keys.add(key)
            except OSError:
                continue
    return keys


def _media_key(row: dict) -> str:
    fu = str(row.get("fu") or "")
    hashes = sorted(str(i.get("hash", "")) for i in (row.get("items") or []))
    return f"{fu}|{','.join(hashes)}" if fu else ""


def _restore_media_member(zf, member, root, tail: str, existing: set):
    rows = []
    with zf.open(member) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line.decode("utf-8", "ignore"))
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(row, dict):
                continue
            key = _media_key(row)
            if not key or key in existing:
                continue
            existing.add(key)
            rows.append(row)

    if not rows:
        return 0, 0

    parts = tail.replace("\\", "/").split("/")
    # 新结构 media/<id>/<day>.jsonl；老结构 media/<day>.jsonl
    uid = parts[1] if len(parts) > 2 else "default"
    media_dir = os.path.join(root, "media", uid)
    os.makedirs(media_dir, exist_ok=True)
    by_day: dict[str, list[dict]] = {}
    for row in rows:
        day = str(row.get("ts", ""))[:10] or "unknown"
        by_day.setdefault(day, []).append(row)
    for day, items in by_day.items():
        path = os.path.join(media_dir, f"{day}.jsonl")
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            for row in items:
                f.write(json.dumps(row, ensure_ascii=False,
                                   separators=(",", ":")) + "\n")
    return len(rows), 0
